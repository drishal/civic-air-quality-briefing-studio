"""Pre-warm the demo cache with real Gemini runs for both sample weeks.

The offline demo replays ``.demo_cache/`` (llm/demo_cache.py), so this script fills
it: it runs the numeric pipeline on data/sample_week1.csv and
data/sample_week2.csv, then prefetches every generative stage for
3 personas x 8 languages x 3 reading levels (OPUS_REVIEW.md bug 1 and bug 2).

Usage (inside the devenv shell, which provides the numpy/zlib runtime):
    devenv shell -- .venv/bin/python scripts/prewarm_cache.py

Without GEMINI_API_KEY it prints a clear warning and still exits 0: the stages
fall back to their templates and nothing is cached, so the demo would show
fallback text.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python scripts/prewarm_cache.py` from the repo root
    sys.path.insert(0, str(ROOT))

from core.models import Brief  # noqa: E402 - after the sys.path bootstrap
from core.pipeline import PipelineResult, run_pipeline  # noqa: E402
from llm import client, demo_cache  # noqa: E402
from llm.prefetch import prefetch  # noqa: E402

DATA_DIR = ROOT / "data"
AREA = "Pune"  # same default as app.AREA_DEFAULT, so the cache keys match the demo
PERSONAS = ("ward_officer", "housing_society", "school")
LANGUAGES = ("English", "Hindi", "Marathi", "Kannada", "Tamil", "Bengali", "Telugu", "Gujarati")
LEVELS = ("simple", "standard", "technical")


def _run_pipeline(filename: str, previous: Brief | None, store_dir: str) -> PipelineResult:
    """One sample week through the numeric pipeline, exactly as app.py loads it."""
    path = DATA_DIR / filename
    return run_pipeline(
        file_bytes=path.read_bytes(),
        filename=path.name,
        area=AREA,
        use_llm_mapper=client.llm_available(),
        store_dir=store_dir,
        save=False,
        previous=previous,
    )


def _cache_files() -> int:
    """How many run files the demo cache holds now."""
    directory = demo_cache.cache_dir()
    return len(list(directory.glob("*.json"))) if directory.is_dir() else 0


def _prefetch_week(label: str, result: PipelineResult) -> int:
    """Prefetch every stage combination for one week; returns the error count."""
    print(
        f"[{label}] prefetch: {len(PERSONAS)} personas x {len(LANGUAGES)} languages "
        f"x {len(LEVELS)} levels ...",
        flush=True,
    )
    outcome = prefetch(
        result.brief,
        result.diff,
        PERSONAS,
        LANGUAGES,
        LEVELS,
        result.brief.area or AREA,
    )
    print(
        f"[{label}] done in {outcome['elapsed_s']:g}s: "
        f"interpretation={'yes' if outcome['interpretation'] else 'no'} "
        f"plans={len(outcome['plans'])}/{len(PERSONAS)} "
        f"note={'yes' if outcome['note'] else 'no'} "
        f"advisories={len(outcome['advisories'])}/{len(LANGUAGES) * len(LEVELS)} "
        f"errors={len(outcome['errors'])}",
        flush=True,
    )
    for line in outcome["errors"][:5]:
        print(f"[{label}]   error: {line}", file=sys.stderr)
    if len(outcome["errors"]) > 5:
        print(f"[{label}]   ... {len(outcome['errors']) - 5} more errors", file=sys.stderr)
    return len(outcome["errors"])


def main() -> int:
    """Populate the demo cache for week 1 (no previous) and week 2 (week 1 previous)."""
    if not client.llm_available():
        print(
            "WARNING: no GEMINI_API_KEY (or GOOGLE_API_KEY) is set, so every stage will use "
            "its template fallback and .demo_cache/ will not be filled. Set the key and rerun.",
            file=sys.stderr,
        )
    before = _cache_files()
    print(f"demo cache: {demo_cache.cache_dir().resolve()} ({before} run files before)", flush=True)

    # An empty store dir makes run_pipeline see no previous brief for week 1.
    with tempfile.TemporaryDirectory(prefix="prewarm-") as empty_store:
        week1 = _run_pipeline("sample_week1.csv", previous=None, store_dir=empty_store)
    week2 = _run_pipeline("sample_week2.csv", previous=week1.brief, store_dir=str(ROOT / ".briefs"))
    print(
        f"pipelines ready: week 1 {week1.brief.period_start}..{week1.brief.period_end}, "
        f"week 2 {week2.brief.period_start}..{week2.brief.period_end} "
        f"(has previous: {week2.diff.has_previous})",
        flush=True,
    )

    errors = _prefetch_week("week 2", week2)
    errors += _prefetch_week("week 1", week1)

    after = _cache_files()
    print(
        f"\nSummary: 2 weeks x ({len(PERSONAS)} persona plans + 1 interpretation + 1 note + "
        f"{len(LANGUAGES) * len(LEVELS)} advisories); {before} -> {after} cache run files "
        f"in {demo_cache.cache_dir().resolve()} (+{after - before}), {errors} stage errors.",
        flush=True,
    )
    if errors:
        print("Some stages failed; rerun when the network is stable to fill the gaps.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
