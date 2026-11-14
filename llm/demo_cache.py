"""JSON cache of real Gemini runs, replayed when the API is unavailable.

A cache key must survive a new app start: the same data loaded again gets a fresh
`brief_id` and `created_at`, so those fields (and the diff's `previous_brief_id`)
are excluded from every key. `run_key` builds a key from the stage, the prompt
version, the schema name and the canonical JSON of the payload; `make_key` is the
raw hash used by `run_key`. Set DEMO_CACHE_DIR to move the cache.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_DIR = ".demo_cache"

#: Fields that change between runs of the same data, so they never enter a cache key.
VOLATILE_FIELDS = frozenset(
    {
        "brief_id",
        "created_at",
        "previous_brief_id",
        "used_fallback",
        "verification",
        "generated_by",
        "prompt_version",
    }
)


def cache_dir() -> Path:
    """Cache directory; DEMO_CACHE_DIR overrides the default."""
    return Path(os.environ.get("DEMO_CACHE_DIR") or DEFAULT_DIR)


def strip_volatile(value: Any) -> Any:
    """Drop volatile fields at every level so the same data always hashes the same."""
    if isinstance(value, dict):
        return {key: strip_volatile(item) for key, item in value.items() if key not in VOLATILE_FIELDS}
    if isinstance(value, (list, tuple)):
        return [strip_volatile(item) for item in value]
    return value


def make_key(stage: str, payload: Any) -> str:
    """Stable key from a stage name and any JSON-able payload (volatile fields dropped)."""
    try:
        body = json.dumps(strip_volatile(payload), sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        body = repr(payload)
    digest = hashlib.sha256(f"{stage}|{body}".encode("utf-8")).hexdigest()
    return f"{stage}-{digest[:24]}"


def run_key(stage: str, prompt_version: str, schema_name: str, payload: Any) -> str:
    """Key for one stage call: stage + prompt version + schema name + data fingerprint.

    The stage name stays the filename prefix; the prompt version and schema name are part of
    the hashed body, so a prompt or schema change invalidates the entry.
    """
    try:
        body = json.dumps(strip_volatile(payload), sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        body = repr(payload)
    digest = hashlib.sha256(
        f"{stage}|{prompt_version}|{schema_name}|{body}".encode("utf-8")
    ).hexdigest()
    return f"{stage}-{digest[:24]}"


def _payload(obj: Any) -> Any:
    """Pydantic model, dict or plain value as JSON-able data."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def save_run(key: str, obj: Any) -> Path | None:
    """Write one run to the cache. Returns the path, or None on failure."""
    try:
        directory = cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_safe(key)}.json"
        path.write_text(
            json.dumps(_payload(obj), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return path
    except OSError:
        return None


def load_run(key: str) -> Any | None:
    """Read one cached run. Returns None when absent or unreadable."""
    try:
        path = cache_dir() / f"{_safe(key)}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _safe(key: str) -> str:
    """Filesystem-safe key (keys are hashes, but be defensive)."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)[:120]
