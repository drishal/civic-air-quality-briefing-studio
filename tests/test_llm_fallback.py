"""Offline tests for the LLM layer. No network: the client is mocked or has no key."""
from __future__ import annotations

import re
import types

import pandas as pd
import pytest
import yaml

from core.models import (
    Brief,
    ColumnMapping,
    ColumnRole,
    DiffResult,
    Finding,
    QualitySummary,
    StationAQI,
    StationReliability,
    StationChange,
)
from llm import client, demo_cache, fallback, stages
from llm.schemas import (
    Action,
    ActionPlan,
    Advisory,
    CareBlock,
    InternalNote,
    Interpretation,
    Point,
    VerifyIssue,
)


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """No key, no retries, cache in a temp dir."""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(client, "MAX_ATTEMPTS", 1)
    monkeypatch.setattr(client, "BACKOFF_SECONDS", 0)


@pytest.fixture
def brief() -> Brief:
    quality = QualitySummary(
        n_rows_in=12,
        n_readings=10,
        flag_counts={"STUCK": 1, "SENTINEL": 2},
        stations=[
            StationReliability(
                station="Anand Vihar",
                n_readings=6,
                coverage=0.9,
                flagged_share=0.1,
                reliability=0.81,
                label="reliable",
            ),
            StationReliability(
                station="Shivajinagar",
                n_readings=4,
                coverage=0.4,
                flagged_share=0.3,
                reliability=0.28,
                label="unreliable",
                notes=["stuck sensor"],
            ),
        ],
        gaps=["Station Shivajinagar has no readings between 02:00 and 09:00 on 2026-11-03"],
        cleaning_log=["Converted NO2 from ppb to ug/m3 at one station"],
    )
    findings = [
        Finding(
            id="F1",
            type="hotspot",
            title="Shivajinagar is the worst station at AQI 214",
            facts={"aqi": 214, "delta_vs_mean": 46},
            formula="latest valid daily AQI per station",
            evidence_row_ids=["R1"],
            stations=["Shivajinagar"],
            confidence="medium",
            caveats=["station flagged unreliable"],
        ),
        Finding(
            id="F2",
            type="exceedance",
            title="PM2.5 is 3.4 times the WHO 24-hour limit",
            facts={"who_multiple": 3.4, "pm25": 51},
            formula="24-hour mean divided by the WHO 24-hour guideline",
            evidence_row_ids=["R2", "R3"],
            stations=["Shivajinagar"],
            confidence="high",
        ),
        Finding(
            id="F3",
            type="trend",
            title="AQI is rising at Shivajinagar",
            facts={"trend_per_day": 8},
            formula="linear fit over valid days",
            evidence_row_ids=["R4"],
            stations=["Shivajinagar"],
            confidence="low",
        ),
        Finding(
            id="F4",
            type="pattern",
            title="Evening peak between 19:00 and 22:00, consistent with traffic exhaust",
            facts={"peak_window": "19:00-22:00"},
            formula="hour-of-day mean over the period",
            evidence_row_ids=["R5"],
            stations=["Anand Vihar"],
            confidence="medium",
        ),
        Finding(
            id="F5",
            type="gap",
            title="Shivajinagar has a 7 hour data gap",
            facts={"gap_hours": 7},
            formula="expected minus received hourly readings",
            evidence_row_ids=[],
            stations=["Shivajinagar"],
            confidence="high",
        ),
    ]
    station_aqi = [
        StationAQI(
            station="Shivajinagar",
            ward="Ward 12",
            aqi=214,
            category="Poor",
            dominant="PM2.5",
            subindices={"PM2.5": 214, "PM10": 160},
            latest_date="2026-11-03",
            n_valid_days=3,
            reliability=0.28,
            confidence="low",
            mean_aqi_period=190,
            trend_per_day=8,
        ),
        StationAQI(
            station="Anand Vihar",
            ward="Ward 4",
            aqi=132,
            category="Moderately Polluted",
            dominant="PM10",
            subindices={"PM10": 132},
            latest_date="2026-11-03",
            n_valid_days=3,
            reliability=0.81,
            confidence="high",
            mean_aqi_period=120,
            trend_per_day=-2,
        ),
    ]
    return Brief(
        brief_id="b1",
        created_at="2026-11-04T08:00:00",
        area="Pune",
        source_name="sample_week2.csv",
        source_hash="abc123",
        period_start="2026-11-01",
        period_end="2026-11-03",
        station_aqi=station_aqi,
        findings=findings,
        quality=quality,
    )


@pytest.fixture
def diff() -> DiffResult:
    return DiffResult(
        has_previous=True,
        previous_brief_id="b0",
        station_changes=[
            StationChange(
                station="Shivajinagar",
                aqi_prev=180,
                aqi_now=214,
                delta=34,
                category_prev="Moderately Polluted",
                category_now="Poor",
                direction="worse",
            )
        ],
        new_hotspots=["Shivajinagar"],
        resolved_hotspots=[],
        category_transitions=["Shivajinagar: Moderately Polluted -> Poor"],
        quality_changes=["Shivajinagar: use with caution -> unreliable"],
        new_finding_titles=["Shivajinagar is the worst station at AQI 214"],
        summary_facts={"n_worse": 1, "n_better": 0, "mean_delta": 34},
    )


def make_raw() -> types.SimpleNamespace:
    """Duck-typed RawTable: only df, source_name and notes are used."""
    frame = pd.DataFrame(
        [
            ["Site", "PM2.5 (ug/m3)", "Ozone", "When"],
            ["Anand Vihar", "85", "40", "03/11/2026 07:00"],
            ["Shivajinagar", "NA", "52", "03/11/2026 08:00"],
        ]
    )
    frame.columns = frame.iloc[0]
    frame = frame.iloc[1:].reset_index(drop=True)
    return types.SimpleNamespace(
        df=frame, source_name="messy.csv", notes=["subtitle row skipped"]
    )


class _FakeConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _HttpError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class _FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return types.SimpleNamespace(text=item)


class _FakeClient:
    def __init__(self, responses):
        self.models = _FakeModels(responses)


def fake_sdk(monkeypatch, fake_client):
    """Make the client use a fake SDK and client instead of google-genai."""
    monkeypatch.setattr(
        client, "_sdk", lambda: (types.SimpleNamespace(), types.SimpleNamespace(GenerateContentConfig=_FakeConfig))
    )
    monkeypatch.setattr(client, "_new_client", lambda key: fake_client)


# ------------------------------------------------------------------ client


def test_client_reports_missing_key():
    assert client.llm_available() is False
    with pytest.raises(client.LLMUnavailable):
        client.generate_json("prompt", "system", Interpretation)


def test_google_api_key_works_as_alias(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    assert client.llm_available() is True
    assert client.api_key() == "test-key"


def test_gemini_model_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "custom-model")
    models = client.model_list()
    assert models[0] == "custom-model"
    assert set(models[1:]) == set(client.MODELS)


def test_generate_json_passes_schema_and_config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = Interpretation(
        headline="Shivajinagar is worst [F1].",
        trend_points=[Point(text="AQI is rising [F3].", cites=["F3"])],
        hotspot_points=[],
        cannot_tell=["No previous brief."],
    )
    fake_client = _FakeClient([answer.model_dump_json()])
    fake_sdk(monkeypatch, fake_client)

    result = client.generate_json("prompt text", "system text", Interpretation, temperature=0.7)

    assert isinstance(result, Interpretation)
    assert result.headline == "Shivajinagar is worst [F1]."
    call = fake_client.models.calls[0]
    config = call["config"].kwargs
    assert call["contents"] == "prompt text"
    assert config["system_instruction"] == "system text"
    assert config["response_mime_type"] == "application/json"
    assert config["temperature"] == 0.7
    assert config["response_json_schema"]["properties"]["headline"]


def test_generate_json_walks_models_on_404(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(client, "MODELS", ["model-a", "model-b"])
    answer = Interpretation(headline="ok [F1].", trend_points=[], hotspot_points=[], cannot_tell=[])
    fake_client = _FakeClient([_HttpError(404), answer.model_dump_json()])
    fake_sdk(monkeypatch, fake_client)

    result = client.generate_json("p", "s", Interpretation)

    assert result.headline == "ok [F1]."
    assert [call["model"] for call in fake_client.models.calls] == ["model-a", "model-b"]


def test_generate_json_raises_when_every_model_fails(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    fake_client = _FakeClient([RuntimeError("boom")])
    fake_sdk(monkeypatch, fake_client)

    with pytest.raises(client.LLMUnavailable):
        client.generate_json("p", "s", Interpretation)


# ------------------------------------------------------------------ demo cache


def test_demo_cache_roundtrip():
    key = demo_cache.make_key("interpret", {"a": 1})
    assert demo_cache.load_run(key) is None
    assert demo_cache.save_run(key, {"headline": "cached"}) is not None
    assert demo_cache.load_run(key) == {"headline": "cached"}
    assert demo_cache.load_run("missing-key") is None


# ------------------------------------------------------------------ P1 mapper


def test_map_columns_offline_returns_none():
    assert stages.map_columns(make_raw()) is None


def test_map_columns_validates_and_canonicalises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    mapping = ColumnMapping(
        layout="wide",
        columns=[
            ColumnRole(column="Site", role="station"),
            ColumnRole(column="PM2.5 (ug/m3)", role="pollutant_value", pollutant="pm25", unit="ug/m3"),
            ColumnRole(column="Ozone", role="pollutant_value", pollutant="ozone", unit="ppb"),
            ColumnRole(column="Ghost", role="pollutant_value", pollutant="unknown stuff"),
            ColumnRole(column="When", role="timestamp"),
        ],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: mapping)

    result = stages.map_columns(make_raw())

    assert result is not None
    assert result.source == "llm"
    by_column = {role.column: role for role in result.columns}
    assert "Ghost" not in by_column
    assert by_column["PM2.5 (ug/m3)"].pollutant == "PM2.5"
    assert by_column["Ozone"].pollutant == "O3"


def test_map_columns_rejects_mapping_without_values(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    mapping = ColumnMapping(
        layout="wide",
        columns=[
            ColumnRole(column="Site", role="station"),
            ColumnRole(column="When", role="timestamp"),
        ],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: mapping)
    assert stages.map_columns(make_raw()) is None


# ------------------------------------------------------------------ P2 interpreter


def test_interpret_offline_uses_fallback(brief, diff):
    result = stages.interpret(brief, diff)

    assert isinstance(result, Interpretation)
    assert result.used_fallback is True
    assert result.headline
    known = {finding.id for finding in brief.findings}
    points = result.trend_points + result.hotspot_points
    assert points
    for point in points:
        assert point.cites and set(point.cites) <= known
        assert point.text.rstrip().endswith("]") or "[" in point.text
    assert any("previous brief" in item.lower() for item in result.cannot_tell) is False
    assert any("Shivajinagar has no readings" in item for item in result.cannot_tell)


def test_interpret_offline_without_previous_brief(brief):
    result = stages.interpret(brief, DiffResult(has_previous=False))
    assert any("previous brief" in item.lower() for item in result.cannot_tell)


def test_interpret_strips_unknown_citations(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = Interpretation(
        headline="Shivajinagar is the worst station [F1].",
        trend_points=[Point(text="AQI is rising [F3][F99].", cites=["F3", "F99"])],
        hotspot_points=[Point(text="Evening peak [F4].", cites=["[F4]"])],
        cannot_tell=["Data gap at Shivajinagar."],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: answer)

    result = stages.interpret(brief, diff)

    assert result.used_fallback is False
    assert result.trend_points[0].cites == ["F3"]
    assert result.hotspot_points[0].cites == ["F4"]


def test_interpret_falls_back_on_client_error(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def boom(*args, **kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr(client, "generate_json", boom)
    result = stages.interpret(brief, diff)
    assert result.used_fallback is True
    assert result.headline


def test_successful_run_is_cached(monkeypatch, brief, diff, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = Interpretation(
        headline="Cached style headline [F1].",
        trend_points=[Point(text="Rising [F3].", cites=["F3"])],
        hotspot_points=[],
        cannot_tell=[],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: answer)
    stages.interpret(brief, diff)
    assert list((tmp_path / "cache").glob("interpret-*.json"))


def test_cached_replay_when_api_unavailable(brief, diff):
    key = stages.stage_key(
        "interpret",
        "p2_interpreter",
        "Interpretation",
        stages.brief_fingerprint(brief),
        stages.diff_fingerprint(diff),
    )
    demo_cache.save_run(
        key,
        Interpretation(
            headline="Replayed headline [F1].",
            trend_points=[Point(text="Rising [F3].", cites=["F3"])],
            hotspot_points=[],
            cannot_tell=[],
        ),
    )
    result = stages.interpret(brief, diff)
    assert result.headline == "Replayed headline [F1]."
    assert result.used_fallback is False


# ------------------------------------------------------------------ P3 actioner


def test_plan_actions_offline_for_every_persona(brief, diff):
    for persona in ("ward_officer", "housing_society", "school"):
        plan = stages.plan_actions(brief, diff, persona)
        assert isinstance(plan, ActionPlan)
        assert plan.persona == persona
        assert plan.used_fallback is True
        assert 1 <= len(plan.actions) <= 5
        assert [action.rank for action in plan.actions] == list(range(1, len(plan.actions) + 1))
        known = {finding.id for finding in brief.findings}
        library_ids = {item["id"] for item in fallback.library_for_persona(persona)}
        for action in plan.actions:
            assert action.cites and set(action.cites) <= known
            assert action.library_id in library_ids | {"suggested_new"}
            assert action.time_window in {"today", "this week", "this month"}
            assert action.effort in {"low", "medium", "high"}
            assert action.owner


def test_plan_ranks_pm25_actions_first_for_pm25_hotspot(brief, diff):
    plan = fallback.plan_fallback(brief, diff, "ward_officer")
    assert plan.actions[0].cites
    assert set(plan.actions[0].cites) <= {finding.id for finding in brief.findings}
    assert "PM2.5" in plan.actions[0].why_now
    assert plan.actions[0].data_gap_action  # unreliable station present


def test_plan_drops_invented_citations_and_renumbers(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    library_id = fallback.library_for_persona("ward_officer")[0]["id"]
    plan = ActionPlan(
        persona="ward_officer",
        actions=[
            Action(
                rank=1,
                action="Invented action",
                why_now="because of bad air [F99]",
                cites=["F99"],
                owner="ward officer",
                effort="low",
                time_window="today",
                data_gap_action="",
                library_id=library_id,
            ),
            Action(
                rank=3,
                action="Patrol for burning",
                why_now="evening peak [F4]",
                cites=["F4"],
                owner="ward officer",
                effort="low",
                time_window="today",
                data_gap_action="",
                library_id="not-in-library",
            ),
            Action(
                rank=7,
                action="Inspect construction sites",
                why_now="PM2.5 dominant [F1]",
                cites=["F1"],
                owner="ward officer",
                effort="medium",
                time_window="this week",
                data_gap_action="",
                library_id=library_id,
            ),
        ],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: plan)

    result = stages.plan_actions(brief, diff, "ward_officer")

    assert result.used_fallback is False
    assert [action.rank for action in result.actions] == [1, 2]
    assert [action.library_id for action in result.actions] == ["suggested_new", library_id]

def test_plan_with_only_invented_citations_falls_back(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    plan = ActionPlan(
        persona="school",
        actions=[
            Action(
                rank=1,
                action="Something",
                why_now="[F99]",
                cites=["F99"],
                owner="principal",
                effort="low",
                time_window="today",
                data_gap_action="",
                library_id="fallback-sc-1",
            )
        ],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: plan)
    result = stages.plan_actions(brief, diff, "school")
    assert result.used_fallback is True
    assert result.actions


# ------------------------------------------------------------------ P4 internal note


def test_internal_note_offline_has_fixed_sections(brief, diff):
    interp = stages.interpret(brief, diff)
    plan = stages.plan_actions(brief, diff, "ward_officer")
    note = stages.write_internal_note(brief, diff, interp, plan)

    assert isinstance(note, InternalNote)
    assert note.used_fallback is True
    for section in (
        "## Situation",
        "## What changed since last brief",
        "## Hotspots",
        "## Data caveats",
        "## Actions by owner",
        "## Open questions",
    ):
        assert section in note.markdown
    assert "[F1]" in note.markdown
    assert "unreliable" in note.markdown.lower()


def test_internal_note_without_previous_brief_says_so(brief):
    diff = DiffResult(has_previous=False)
    interp = stages.interpret(brief, diff)
    plan = stages.plan_actions(brief, diff, "school")
    note = stages.write_internal_note(brief, diff, interp, plan)
    assert "no previous brief" in note.markdown.lower()


# ------------------------------------------------------------------ P5 advisory


def test_advisory_english_offline(brief):
    advisory = stages.write_advisory(brief, "English", "standard", "Pune")

    assert isinstance(advisory, Advisory)
    assert advisory.used_fallback is True
    assert advisory.category == "Poor"
    assert "214" in advisory.today_message
    assert len(advisory.what_to_do) >= 3
    assert advisory.care.children and advisory.care.older_adults and advisory.care.sensitive_groups
    assert "Source:" in advisory.source_line
    assert "2026-11-03" in advisory.source_line
    assert "Anand Vihar" in advisory.source_line
    assert "not medical advice" in advisory.source_line


def test_advisory_hindi_keeps_numbers_and_places(brief):
    english = stages.write_advisory(brief, "English", "standard", "Pune")
    hindi = stages.write_advisory(brief, "Hindi", "standard", "Pune")

    assert hindi.language == "Hindi"
    assert hindi.category == english.category
    assert "214" in hindi.today_message
    assert "214" in english.today_message
    assert hindi.today_message != english.today_message
    assert "Anand Vihar" in hindi.source_line


def test_advisory_unknown_language_falls_back_to_english(brief):
    advisory = stages.write_advisory(brief, "Tamil", "simple", "Pune")
    assert advisory.language == "Tamil"
    assert "Translation unavailable" in advisory.source_line


def test_advisory_reading_levels_differ(brief):
    simple = stages.write_advisory(brief, "English", "simple", "Pune")
    technical = stages.write_advisory(brief, "English", "technical", "Pune")
    assert len(simple.what_to_do) <= len(technical.what_to_do)
    assert "Lead pollutant: PM2.5" in technical.today_message
    assert "Lead pollutant" not in simple.today_message
    standard = stages.write_advisory(brief, "English", "standard", "Pune")
    assert len(simple.today_message) < len(standard.today_message)


def test_advisory_category_comes_from_aqi(brief):
    clean = brief.model_copy(
        update={
            "station_aqi": [
                StationAQI(station="Anand Vihar", aqi=42, category="Good", dominant="PM10")
            ]
        }
    )
    good = stages.write_advisory(clean, "English", "standard", "Pune")
    poor = stages.write_advisory(brief, "English", "standard", "Pune")
    assert good.category == "Good"
    assert good.today_message != poor.today_message
    assert "Children can play outside" in good.care.children
    assert "Children should stay indoors" in fallback.advisory_fallback(
        brief.model_copy(
            update={
                "station_aqi": [
                    StationAQI(station="Anand Vihar", aqi=330, category="Very Poor", dominant="PM2.5")
                ]
            }
        ),
        "English",
        "standard",
        "Pune",
    ).care.children


def test_advisory_repairs_advice_that_contradicts_the_category(monkeypatch, brief):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    unsafe = Advisory(
        language="English",
        reading_level="standard",
        headline="Air is fine today.",
        category="Good",
        today_message="Go for a long run outdoors.",
        what_to_do=["Run outside"],
        care=CareBlock(
            children="Children can play outside.",
            older_adults="ok",
            sensitive_groups="ok",
        ),
        source_line="Source: today.",
        cites=[],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: unsafe)

    result = stages.write_advisory(brief, "English", "standard", "Pune", station=None)

    # Code owns the category; the contradicting advice is replaced, not shipped.
    assert result.category == "Poor"
    assert "go for a long run" not in result.today_message.lower()
    assert "run outside" not in " ".join(result.what_to_do).lower()
    assert "Keep children indoors" in result.care.children
    assert "Poor" in result.headline


def test_advisory_accepts_matching_model_answer(monkeypatch, brief):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = Advisory(
        language="Hindi",
        reading_level="standard",
        headline="आज हवा खराब है।",
        category="Poor",
        today_message="आज सूचकांक 214 है। बाहर की गतिविधि कम रखें [F1]।",
        what_to_do=["खिड़कियाँ बंद रखें।", "बाहर मास्क पहनें।"],
        care=CareBlock(
            children="बच्चे घर में रहें।",
            older_adults="बुज़ुर्ग घर में रहें।",
            sensitive_groups="दवा साथ रखें।",
        ),
        source_line="स्रोत: 2026-11-03। स्टेशन: Shivajinagar।",
        cites=["F1", "F99"],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: answer)

    result = stages.write_advisory(brief, "Hindi", "standard", "Pune")

    assert result.used_fallback is False
    assert result.language == "Hindi"
    assert result.cites == ["F1"]


# ------------------------------------------------------------------ P6 verifier


def test_verify_draft_offline_returns_empty(brief):
    assert stages.verify_draft("Shivajinagar is at AQI 214 [F1].", brief) == []
    assert stages.verify_draft("", brief) == []


def test_verify_draft_returns_mocked_issues(monkeypatch, brief):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    report = types.SimpleNamespace(
        issues=[VerifyIssue(sentence="Go for a run.", problem="Advice does not fit the Poor category.")]
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: report)
    issues = stages.verify_draft("Go for a run.", brief)
    assert issues and issues[0].problem.startswith("Advice")


# ------------------------------------------------------------------ prompts and library


@pytest.mark.parametrize(
    "name",
    [
        "p1_mapper",
        "p2_interpreter",
        "p3_actioner",
        "p4_internal_note",
        "p5_advisory",
        "p6_verifier",
    ],
)
def test_prompt_files_have_system_and_user_sections(name):
    system, user = stages.load_prompt(name)
    assert len(system) > 200
    assert "{" in user and "}" in user
    assert "JSON" in user


def test_prompt_placeholders_are_rendered():
    _, user = stages.load_prompt("p2_interpreter")
    prompt = fallback.fill_template(user, AREA="Pune", FINDINGS="[]", QUALITY="{}", DIFF="{}")
    assert "Pune" in prompt
    assert "{AREA}" not in prompt
    assert "{PERIOD}" in prompt  # not supplied, so it stays visible


def test_prompt_placeholders_match_what_stages_supply():
    expected = {
        "p1_mapper": {"COLUMNS", "POLLUTANT_LIST", "SAMPLE_ROWS"},
        "p2_interpreter": {"AREA", "PERIOD", "FINDINGS", "QUALITY", "DIFF"},
        "p3_actioner": {
            "PERSONA", "AUTHORITY", "LIMITS", "CATEGORY", "PERIOD",
            "FINDINGS", "QUALITY", "DIFF", "ACTION_LIBRARY",
        },
        "p4_internal_note": {
            "AREA", "PERIOD", "FINDINGS", "QUALITY", "DIFF",
            "INTERPRETATION", "ACTION_PLAN",
        },
        "p5_advisory": {
            "LANGUAGE", "READING_LEVEL", "SCOPE", "STATION", "AREA", "DATE", "CATEGORY",
            "AQI", "DOMINANT", "STATIONS", "RANGE", "PEAK_WINDOW", "GAP_NOTE",
            "FINDINGS", "QUALITY", "GROUPS",
        },
        "p6_verifier": {"CATEGORY", "ALLOWED_NUMBERS", "FINDINGS", "DRAFT"},
        "p6b_repair": {"STAGE", "CATEGORY", "ISSUES", "DRAFT", "FINDINGS", "QUALITY"},
        "p7_ask": {
            "PERSONA", "AUTHORITY", "LIMITS", "CATEGORY", "PEAK_WINDOW", "QUESTION",
            "FINDINGS", "QUALITY", "DIFF",
        },
    }
    for name, placeholders in expected.items():
        system, user = stages.load_prompt(name)
        found = set(re.findall(r"\{([A-Z_]+)\}", user))
        assert found == placeholders, name
        assert not re.findall(r"\{[A-Z_]+\}", system), f"{name} system has placeholders"


def test_builtin_action_library_covers_every_persona():
    for persona in ("ward_officer", "housing_society", "school"):
        items = fallback.library_for_persona(persona)
        assert len(items) >= 3
        ids = [item["id"] for item in items]
        assert len(ids) == len(set(ids))
        for item in items:
            assert item["action"]
            assert item["effort"] in {"low", "medium", "high"}


def test_yaml_action_library_is_loaded(tmp_path):
    path = tmp_path / "action_library.yaml"
    items = [
        {
            "id": f"y{i}",
            "persona": ["ward_officer"],
            "title": f"Action {i}",
            "action": f"Do practical thing {i}",
            "trigger": {"pollutants": ["PM2.5"], "min_category": "Poor"},
            "effort": "low",
        }
        for i in range(8)
    ]
    path.write_text(yaml.safe_dump(items), encoding="utf-8")

    merged = fallback.load_action_library(path)

    assert {"y0", "y7"} <= {item["id"] for item in merged}
    assert len(fallback.library_for_persona("housing_society")) >= 3


def test_short_yaml_falls_back_to_builtin(tmp_path):
    path = tmp_path / "action_library.yaml"
    path.write_text(yaml.safe_dump([{"id": "one", "persona": ["school"], "action": "X"}]), encoding="utf-8")
    merged = fallback.load_action_library(path)
    assert len(merged) >= 6


# ------------------------------------------------------------------ empty input


def test_stages_never_raise_on_empty_brief():
    brief = Brief(
        brief_id="b0",
        created_at="2026-11-04T00:00:00",
        area="Pune",
        source_name="empty.csv",
        source_hash="h",
        quality=QualitySummary(n_rows_in=0, n_readings=0),
    )
    diff = DiffResult(has_previous=False)

    interp = stages.interpret(brief, diff)
    plan = stages.plan_actions(brief, diff, "school")
    note = stages.write_internal_note(brief, diff, interp, plan)
    advisory = stages.write_advisory(brief, "Hindi", "simple", "Pune")

    assert interp.headline
    assert plan.actions
    assert "## Situation" in note.markdown
    assert advisory.category == "Data unavailable"
    assert advisory.care.children
    assert advisory.source_line


def test_no_stage_raises_when_client_raises(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def boom(*args, **kwargs):
        raise client.LLMUnavailable("no route")

    monkeypatch.setattr(client, "generate_json", boom)

    interp = stages.interpret(brief, diff)
    plan = stages.plan_actions(brief, diff, "ward_officer")
    note = stages.write_internal_note(brief, diff, interp, plan)
    advisory = stages.write_advisory(brief, "English", "technical", "Pune")
    issues = stages.verify_draft("Some draft [F1].", brief)

    assert interp.used_fallback and plan.used_fallback and note.used_fallback
    assert advisory.used_fallback
    assert issues == []
