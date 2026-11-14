"""Local JSON brief history: save, list, and load the latest brief.

One file per brief under `directory` (default `.briefs`), named from
`created_at`. Corrupt or foreign files are skipped, never fatal.
"""
from __future__ import annotations

from pathlib import Path

from core.models import Brief

DEFAULT_DIRECTORY = ".briefs"


def _safe_name(created_at: str) -> str:
    """Filesystem-safe file stem from an ISO timestamp."""
    stem = (created_at or "").strip().replace(":", "-").replace("+", "-").replace(" ", "T")
    stem = stem.replace("/", "-").replace("\\", "-")
    return stem or "brief"


def save_brief(brief: Brief, directory: str | Path = DEFAULT_DIRECTORY) -> Path:
    """Write `brief` as JSON and return the file path."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(brief.created_at)
    path = folder / f"{stem}.json"
    suffix = 2
    while path.exists():
        try:
            existing = Brief.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            break  # unreadable file: overwrite it
        if existing.brief_id == brief.brief_id:
            break  # idempotent re-save of the same brief
        path = folder / f"{stem}-{suffix}.json"
        suffix += 1
    path.write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    return path


def _load_all(directory: str | Path) -> list[Brief]:
    folder = Path(directory)
    if not folder.is_dir():
        return []
    briefs: list[Brief] = []
    for path in folder.glob("*.json"):
        try:
            briefs.append(Brief.model_validate_json(path.read_text(encoding="utf-8")))
        except Exception:
            continue  # corrupt or not a Brief: ignore it
    briefs.sort(key=lambda b: (b.created_at, b.brief_id), reverse=True)
    return briefs


def list_briefs(directory: str | Path = DEFAULT_DIRECTORY) -> list[Brief]:
    """All readable briefs, newest first."""
    return _load_all(directory)


def load_latest(
    directory: str | Path = DEFAULT_DIRECTORY, exclude_id: str | None = None
) -> Brief | None:
    """Newest readable brief, skipping `exclude_id`; None when none exists."""
    for brief in _load_all(directory):
        if exclude_id is not None and brief.brief_id == exclude_id:
            continue
        return brief
    return None
