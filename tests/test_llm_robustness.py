"""Robustness tests for the Gemini client and the concurrent prefetch.

No network and no key: google-genai is replaced by small fakes, and the clock is
patched so the 60 s circuit cooldown and the 35 s call budget are exercised
without real waiting.
"""
from __future__ import annotations

import time
import types

import pytest

from core.models import Brief, DiffResult, Finding, QualitySummary
from llm import client, stages
from llm.prefetch import prefetch
from llm.schemas import ActionPlan, Advisory, CareBlock, InternalNote, Interpretation, Point


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Closed breaker, no env overrides, one fake key."""
    client.reset_circuit()
    for name in ("GEMINI_TIMEOUT_S", "GEMINI_TOTAL_BUDGET_S", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    yield
    client.reset_circuit()


@pytest.fixture
def brief() -> Brief:
    return Brief(
        brief_id="b1",
        created_at="2026-11-04T08:00:00",
        area="Pune",
        source_name="week.csv",
        source_hash="h",
        quality=QualitySummary(n_rows_in=2, n_readings=2),
        findings=[
            Finding(
                id="F1",
                type="hotspot",
                title="Shivajinagar is the worst station at AQI 214",
                facts={"aqi": 214},
                formula="latest valid daily AQI per station",
                confidence="high",
            )
        ],
    )


@pytest.fixture
def diff() -> DiffResult:
    return DiffResult(has_previous=False)


# ------------------------------------------------------------------ fakes


class _FakeConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeHttpOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


_FAKE_TYPES = types.SimpleNamespace(
    GenerateContentConfig=_FakeConfig,
    HttpOptions=_FakeHttpOptions,
)


class _HttpError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class _Clock:
    """Fake monotonic clock; ``sleep`` advances it like the real one would."""

    def __init__(self, start: float = 1000.0):
        self.value = start

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _FakeModels:
    def __init__(self, responses, clock: _Clock | None = None, clock_step: float = 0.0):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.clock = clock
        self.clock_step = clock_step

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.clock is not None:
            self.clock.value += self.clock_step
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return types.SimpleNamespace(text=item)


class _FakeClient:
    def __init__(self, responses, clock: _Clock | None = None, clock_step: float = 0.0):
        self.models = _FakeModels(responses, clock, clock_step)


def _install(monkeypatch, responses, clock: _Clock | None = None, clock_step: float = 0.0) -> _FakeClient:
    """Point the client at fake SDK types and a fake genai client."""
    fake = _FakeClient(responses, clock, clock_step)
    monkeypatch.setattr(client, "_sdk", lambda: (types.SimpleNamespace(), _FAKE_TYPES))
    monkeypatch.setattr(client, "_new_client", lambda key: fake)
    monkeypatch.setattr(client, "_backoff", lambda attempt: 0.0)  # no real pauses
    return fake


def _interpretation() -> Interpretation:
    return Interpretation(
        headline="Shivajinagar is worst [F1].",
        trend_points=[Point(text="AQI is rising [F1].", cites=["F1"])],
        hotspot_points=[],
        cannot_tell=[],
    )


def _advisory(language: str, level: str) -> Advisory:
    return Advisory(
        language=language,
        reading_level=level,
        headline="Air is poor today.",
        category="Poor",
        today_message="AQI 214 today.",
        what_to_do=["Keep windows closed."],
        care=CareBlock(children="Short play.", older_adults="Short trips.", sensitive_groups="Keep medicine."),
        source_line="Source: sample data.",
        cites=["F1"],
    )


# ------------------------------------------------------------------ timeout config


def test_default_request_timeout_is_passed_in_milliseconds(monkeypatch):
    answer = _interpretation()
    fake = _install(monkeypatch, [answer.model_dump_json()])

    client.generate_json("prompt", "system", Interpretation)

    config = fake.models.calls[0]["config"].kwargs
    assert config["http_options"].kwargs["timeout"] == 20000


def test_request_timeout_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_S", "7.5")
    answer = _interpretation()
    fake = _install(monkeypatch, [answer.model_dump_json()])

    client.generate_json("prompt", "system", Interpretation)

    config = fake.models.calls[0]["config"].kwargs
    assert config["http_options"].kwargs["timeout"] == 7500


def test_bad_timeout_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_S", "not-a-number")
    monkeypatch.setenv("GEMINI_TOTAL_BUDGET_S", "-3")
    assert client.request_timeout_s() == client.DEFAULT_TIMEOUT_S
    assert client.total_budget_s() == client.DEFAULT_TOTAL_BUDGET_S


# ------------------------------------------------------------------ retries and budget


def test_retry_cap_is_two_attempts_per_model(monkeypatch):
    monkeypatch.setattr(client, "MODELS", ["model-a"])
    fake = _install(monkeypatch, [_HttpError(503)])

    with pytest.raises(client.LLMUnavailable):
        client.generate_json("prompt", "system", Interpretation)

    assert client.MAX_ATTEMPTS == 2
    assert [call["model"] for call in fake.models.calls] == ["model-a", "model-a"]


def test_backoff_is_jittered_and_capped(monkeypatch):
    monkeypatch.setattr(client, "BACKOFF_SECONDS", 1.5)
    for attempt in range(1, 8):
        assert client._backoff(attempt) <= client.BACKOFF_CAP_SECONDS
    assert client.BACKOFF_CAP_SECONDS == 4.0
    monkeypatch.setattr(client, "BACKOFF_SECONDS", 10.0)
    assert client._backoff(3) == client.BACKOFF_CAP_SECONDS


def test_total_budget_stops_the_call_and_caps_the_request_timeout(monkeypatch):
    monkeypatch.setattr(client, "MODELS", ["model-a", "model-b", "model-c"])
    monkeypatch.setenv("GEMINI_TOTAL_BUDGET_S", "5")
    monkeypatch.setenv("GEMINI_TIMEOUT_S", "20")
    clock = _Clock()
    monkeypatch.setattr(client, "_now", clock.now)
    # Every SDK call burns 3 s of the 5 s budget and fails.
    fake = _install(monkeypatch, [RuntimeError("no route")], clock=clock, clock_step=3.0)

    with pytest.raises(client.LLMUnavailable) as excinfo:
        client.generate_json("prompt", "system", Interpretation)

    assert "budget" in str(excinfo.value)
    assert len(fake.models.calls) == 2  # third call would exceed the deadline
    second_timeout = fake.models.calls[1]["config"].kwargs["http_options"].kwargs["timeout"]
    assert second_timeout == 2000  # remaining budget capped it below 20 s


# ------------------------------------------------------------------ circuit breaker


def test_llm_available_makes_no_network_call(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("llm_available must not touch the network")

    monkeypatch.setattr(client, "_sdk", boom)
    monkeypatch.setattr(client, "_new_client", boom)
    assert client.llm_available() is True

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert client.llm_available() is False
    assert client.circuit_open() is False


def test_circuit_opens_after_two_total_failures(monkeypatch):
    monkeypatch.setattr(client, "MODELS", ["model-a"])
    clock = _Clock()
    monkeypatch.setattr(client, "_now", clock.now)
    fake = _install(monkeypatch, [RuntimeError("no route")], clock=clock)

    for _ in range(client.CIRCUIT_THRESHOLD):
        with pytest.raises(client.LLMUnavailable):
            client.generate_json("prompt", "system", Interpretation)
    assert client.circuit_open() is True

    calls_before = len(fake.models.calls)
    with pytest.raises(client.LLMUnavailable) as excinfo:
        client.generate_json("prompt", "system", Interpretation)
    assert "marked down" in str(excinfo.value)
    assert len(fake.models.calls) == calls_before  # no SDK call while the breaker is open


def test_circuit_closes_after_sixty_seconds(monkeypatch):
    monkeypatch.setattr(client, "MODELS", ["model-a"])
    clock = _Clock()
    monkeypatch.setattr(client, "_now", clock.now)
    fake = _install(monkeypatch, [RuntimeError("no route")], clock=clock)

    for _ in range(client.CIRCUIT_THRESHOLD):
        with pytest.raises(client.LLMUnavailable):
            client.generate_json("prompt", "system", Interpretation)
    assert client.circuit_open() is True

    clock.value += client.CIRCUIT_COOLDOWN_S - 1
    assert client.circuit_open() is True  # still inside the cooldown

    clock.value += 2
    assert client.circuit_open() is False
    fake.models.responses = [_interpretation().model_dump_json()]
    result = client.generate_json("prompt", "system", Interpretation)
    assert isinstance(result, Interpretation)
    assert client.circuit_open() is False


def test_reset_circuit_reopens_the_api(monkeypatch):
    monkeypatch.setattr(client, "MODELS", ["model-a"])
    monkeypatch.setattr(client, "_now", _Clock().now)
    _install(monkeypatch, [RuntimeError("no route")])

    for _ in range(client.CIRCUIT_THRESHOLD):
        with pytest.raises(client.LLMUnavailable):
            client.generate_json("prompt", "system", Interpretation)
    assert client.circuit_open() is True

    client.reset_circuit()

    assert client.circuit_open() is False


def test_missing_key_does_not_touch_the_breaker(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    for _ in range(client.CIRCUIT_THRESHOLD + 1):
        with pytest.raises(client.LLMUnavailable):
            client.generate_json("prompt", "system", Interpretation)
    assert client.circuit_open() is False


# ------------------------------------------------------------------ prefetch


def _install_fake_stages(monkeypatch, delay: float = 0.0, fail: set[str] | None = None) -> None:
    """Replace the stage functions; ``delay`` makes each call slow (for concurrency)."""
    fail = fail or set()

    def slow(name, make):
        def run(*args, **kwargs):
            if delay:
                time.sleep(delay)
            if name in fail:
                raise RuntimeError(f"{name} exploded")
            return make(*args, **kwargs)

        return run

    monkeypatch.setattr(stages, "interpret", slow("interpret", lambda brief, diff: _interpretation()))
    monkeypatch.setattr(
        stages,
        "plan_actions",
        slow("plan_actions", lambda brief, diff, persona: ActionPlan(persona=persona, actions=[])),
    )
    monkeypatch.setattr(
        stages,
        "write_internal_note",
        slow("note", lambda brief, diff, interp, plan: InternalNote(markdown="## Situation\nok")),
    )
    monkeypatch.setattr(
        stages,
        "write_advisory",
        slow("advisory", lambda brief, language, level, area: _advisory(language, level)),
    )


def test_prefetch_returns_every_key(monkeypatch, brief, diff):
    _install_fake_stages(monkeypatch)

    out = prefetch(
        brief, diff, ["school", "ward_officer"], ["English", "Hindi"], ["simple", "standard"], "Pune"
    )

    assert set(out) == {"interpretation", "plans", "note", "advisories", "errors", "elapsed_s"}
    assert set(out["plans"]) == {"school", "ward_officer"}
    assert set(out["advisories"]) == {
        ("English", "simple"),
        ("English", "standard"),
        ("Hindi", "simple"),
        ("Hindi", "standard"),
    }
    assert out["advisories"][("Hindi", "simple")].reading_level == "simple"
    assert out["interpretation"] is not None
    assert out["note"] is not None
    assert out["errors"] == []
    assert isinstance(out["elapsed_s"], float)


def test_prefetch_runs_stages_concurrently(monkeypatch, brief, diff):
    delay = 0.2
    _install_fake_stages(monkeypatch, delay=delay)
    # interpret + 1 plan, then note + 2 languages x 2 levels = 8 slow calls.
    serial_seconds = delay * 8

    out = prefetch(brief, diff, ["school"], ["English", "Hindi"], ["simple", "standard"], "Pune")

    assert out["errors"] == []
    assert out["elapsed_s"] < serial_seconds / 2
    assert out["elapsed_s"] >= delay  # at least the first wave really ran


def test_prefetch_never_raises_when_a_stage_raises(monkeypatch, brief, diff):
    _install_fake_stages(monkeypatch, fail={"interpret", "advisory"})

    out = prefetch(brief, diff, ["school"], ["English"], ["simple"], "Pune")

    assert out["interpretation"] is None
    assert out["plans"]  # the plan still ran
    assert out["advisories"] == {}
    assert any(line.startswith("interpret") for line in out["errors"])
    assert any(line.startswith("write_advisory") for line in out["errors"])


def test_prefetch_respects_the_global_deadline(monkeypatch, brief, diff):
    _install_fake_stages(monkeypatch, delay=0.5)

    started = time.monotonic()
    out = prefetch(brief, diff, ["school"], ["English"], ["simple"], "Pune", timeout_s=0.1)
    waited = time.monotonic() - started

    assert waited < 1.0
    assert out["errors"]
    assert out["note"] is None
