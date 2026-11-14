"""Gemini wrapper: JSON-schema structured output, bounded retries, model fallback.

Wall-time rules (OPUS_REVIEW.md bug 2):
- Every request carries a timeout: GEMINI_TIMEOUT_S (default 20 s), passed to the
  SDK as HttpOptions.timeout in milliseconds.
- Each model gets at most MAX_ATTEMPTS (2) tries; the pause between tries is
  jittered and capped at BACKOFF_CAP_SECONDS (4 s).
- One generate_json call never runs longer than GEMINI_TOTAL_BUDGET_S (35 s); the
  remaining budget also caps the per-request timeout, so a slow model cannot eat
  the whole budget.
- After CIRCUIT_THRESHOLD (2) consecutive calls that failed on every model, the
  API is treated as down for CIRCUIT_COOLDOWN_S (60 s). Later calls raise
  LLMUnavailable at once, so a dead network does not stall every UI tab.
- llm_available() only reads the environment: it never makes a network call.

The SDK is imported lazily, so importing this module (and running the offline
fallbacks) never needs the package or a key.
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

# Pin the model IDs in one place. GEMINI_MODEL overrides the first entry.
MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
MAX_ATTEMPTS = 2            # per model (one first try plus one retry)
BACKOFF_SECONDS = 1.5       # doubled per attempt, then capped
BACKOFF_CAP_SECONDS = 4.0   # hard ceiling for one pause

DEFAULT_TIMEOUT_S = 20.0        # one HTTP request, env GEMINI_TIMEOUT_S
DEFAULT_TOTAL_BUDGET_S = 35.0   # one generate_json call, env GEMINI_TOTAL_BUDGET_S
CIRCUIT_THRESHOLD = 2           # consecutive total failures that open the breaker
CIRCUIT_COOLDOWN_S = 60.0       # how long the API stays marked down

# Retry on these (rate limit, server trouble); walk to the next model on 404.
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

T = TypeVar("T", bound=BaseModel)

# Circuit-breaker state, guarded because stages run in a thread pool.
_STATE_LOCK = threading.Lock()
_FAILURES = 0        # consecutive generate_json calls that failed on every model
_OPEN_UNTIL = 0.0    # monotonic time until which the API is considered down


class LLMUnavailable(RuntimeError):
    """No API key, every model failed, or the circuit breaker is open."""


def api_key() -> str | None:
    """Key from GEMINI_API_KEY, else GOOGLE_API_KEY."""
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None


def llm_available() -> bool:
    """True when a key is configured. Cheap and offline: no network call."""
    return api_key() is not None


def model_list() -> list[str]:
    """MODELS, with GEMINI_MODEL first when it is set."""
    override = (os.environ.get("GEMINI_MODEL") or "").strip()
    if not override:
        return list(MODELS)
    return [override] + [m for m in MODELS if m != override]


def _now() -> float:
    """Monotonic clock in seconds. Patched in tests to fake the cooldown."""
    return time.monotonic()


def _env_seconds(name: str, default: float) -> float:
    """Positive float from the environment; the default when unset or unusable."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def request_timeout_s() -> float:
    """Per-request timeout from GEMINI_TIMEOUT_S, default 20 s."""
    return _env_seconds("GEMINI_TIMEOUT_S", DEFAULT_TIMEOUT_S)


def total_budget_s() -> float:
    """Per-call wall-time budget from GEMINI_TOTAL_BUDGET_S, default 35 s."""
    return _env_seconds("GEMINI_TOTAL_BUDGET_S", DEFAULT_TOTAL_BUDGET_S)


def _backoff(attempt: int) -> float:
    """Jittered pause before the next attempt, never above BACKOFF_CAP_SECONDS."""
    base = BACKOFF_SECONDS * (2 ** (attempt - 1))
    return min(BACKOFF_CAP_SECONDS, base + random.uniform(0.0, base * 0.25))


# ------------------------------------------------------------------ circuit breaker


def circuit_open() -> bool:
    """True while the API is marked down. No network call."""
    global _FAILURES, _OPEN_UNTIL
    with _STATE_LOCK:
        if _OPEN_UNTIL <= 0.0:
            return False
        if _now() < _OPEN_UNTIL:
            return True
        # Cooldown over: allow a probe call.
        _OPEN_UNTIL = 0.0
        _FAILURES = 0
        return False


def reset_circuit() -> None:
    """Close the breaker and clear the failure count (used by tests and the UI)."""
    global _FAILURES, _OPEN_UNTIL
    with _STATE_LOCK:
        _FAILURES = 0
        _OPEN_UNTIL = 0.0


def _record_success() -> None:
    global _FAILURES, _OPEN_UNTIL
    with _STATE_LOCK:
        _FAILURES = 0
        _OPEN_UNTIL = 0.0


def _record_failure() -> None:
    """Count one total failure; open the breaker at the threshold."""
    global _FAILURES, _OPEN_UNTIL
    with _STATE_LOCK:
        _FAILURES += 1
        if _FAILURES >= CIRCUIT_THRESHOLD:
            _OPEN_UNTIL = _now() + CIRCUIT_COOLDOWN_S


# ------------------------------------------------------------------ SDK plumbing


def status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status from an SDK exception."""
    for attr in ("status_code", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    match = re.search(r"\b(400|401|403|404|408|429|500|502|503|504)\b", str(exc))
    return int(match.group(1)) if match else None


def _sdk():
    """Import google-genai lazily so offline code never needs it."""
    from google import genai
    from google.genai import types

    return genai, types


def _new_client(key: str):
    """Create a genai client. Patched in tests."""
    genai, _ = _sdk()
    return genai.Client(api_key=key)


def _http_options(types, timeout_s: float):
    """HttpOptions with the timeout in ms; None when the SDK has no such class."""
    factory = getattr(types, "HttpOptions", None)
    if factory is None:
        return None
    return factory(timeout=max(1, int(timeout_s * 1000)))


def _config(
    types,
    system: str,
    schema: type[BaseModel],
    temperature: float,
    timeout_s: float | None = None,
):
    """Structured-output config: JSON mime type, the pydantic schema, a timeout."""
    http_options = _http_options(types, timeout_s) if timeout_s is not None else None
    options = {"http_options": http_options} if http_options is not None else {}
    return types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema.model_json_schema(),
        temperature=temperature,
        **options,
    )


# ------------------------------------------------------------------ entry point


def generate_json(
    prompt: str,
    system: str,
    schema: type[T],
    temperature: float = 0.2,
) -> T:
    """One structured-output call with bounded retries and model fallback.

    Raises LLMUnavailable when no key is set, the breaker is open, the total
    budget is used up, or every model failed.
    """
    key = api_key()
    if not key:
        raise LLMUnavailable("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
    if circuit_open():
        raise LLMUnavailable(
            f"Gemini is marked down after {CIRCUIT_THRESHOLD} failed calls; "
            f"retrying is paused for {CIRCUIT_COOLDOWN_S:g}s"
        )
    _, types = _sdk()
    model_client = _new_client(key)
    request_timeout = request_timeout_s()
    budget = total_budget_s()
    deadline = _now() + budget

    errors: list[str] = []
    for model in model_list():
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - _now()
            if remaining <= 0:
                errors.append(f"{model}: total budget of {budget:g}s used up")
                raise _unavailable(errors)
            config = _config(
                types, system, schema, temperature, min(request_timeout, remaining)
            )
            retryable = True
            try:
                response = model_client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                text = getattr(response, "text", None) or ""
                if not text.strip():
                    raise ValueError("empty response text")
                result = schema.model_validate_json(text)
                _record_success()
                return result
            except ValidationError as exc:
                errors.append(f"{model}: invalid JSON ({exc.error_count()} errors)")
            except Exception as exc:  # noqa: BLE001 - SDK raises a wide range
                errors.append(f"{model}: {type(exc).__name__}: {exc}")
                status = status_of(exc)
                if status == 404:
                    break  # model missing: try the next one
                retryable = status is None or status in RETRY_STATUSES
            if not retryable:
                break  # bad request / auth: another attempt will not help
            if attempt < MAX_ATTEMPTS:
                pause = min(_backoff(attempt), max(0.0, deadline - _now()))
                if pause > 0:
                    time.sleep(pause)
    raise _unavailable(errors)


def _unavailable(errors: list[str]) -> LLMUnavailable:
    """Record one total failure and build the exception to raise."""
    _record_failure()
    detail = errors[-1] if errors else "no model tried"
    return LLMUnavailable(f"all models failed: {detail}")
