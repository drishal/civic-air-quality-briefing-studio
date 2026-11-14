"""Small offline eval harness for the prompt layer.

Runs the numeric pipeline over the two sample weeks, generates every stage offline, and scores
four things:

* planted-fault detection against `data/sample_manifest.md` (sentinels, stuck sensor, negative,
  spikes, duplicates and the outage station),
* citation validity of every stage output,
* unsupported numbers (numbers no finding or extra fact supports),
* category contradictions in the advisories.

Run it from the repo root inside `devenv shell`:

    .venv/bin/python -m llm.eval
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core import analytics, clean, ingest, quality
from core.models import Brief, DiffResult
from llm import ask, fallback, stages

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
MANIFEST = DATA_DIR / "sample_manifest.md"
SAMPLES = ("sample_week1.csv", "sample_week2.csv")

#: Scores the offline path must keep. A regression here is a prompt or grounding regression.
FLOORS = {
    "fault_recall": 0.9,
    "citation_validity": 1.0,
    "unsupported_numbers": 0,
    "category_contradictions": 0,
}

#: Manifest fault kind -> the reading flag that proves detection.
_FAULT_FLAGS = {
    "SENTINEL": "SENTINEL",
    "MISSING": "MISSING",
    "MISSING_BLANK": "MISSING",
    "CENSORED": "CENSORED",
    "NEGATIVE": "NEGATIVE",
    "SPIKE": "OUT_OF_RANGE",
    "STUCK": "STUCK",
    "DUPLICATE": "DUPLICATE",
}
_ROW_RANGE = re.compile(r"R(\d+)\s*[-–]\s*R(\d+)")
_ROW_SINGLE = re.compile(r"\bR(\d+)\b")
_GAP_STATION = re.compile(r"GAP\s*\(([A-Za-z][A-Za-z ]*?)(?:\s+outage|:|\))")


# ------------------------------------------------------------------ data


def build_brief(filename: str) -> Brief:
    """Same numeric path as core.pipeline, minus the LLM mapper and the store."""
    content = (DATA_DIR / filename).read_bytes()
    raw = ingest.read_input(file_bytes=content, filename=filename)
    mapping = ingest.heuristic_mapping(raw)
    tidy, cleaning_log = clean.apply_mapping(raw, mapping)
    tidy = quality.flag_readings(tidy)
    summary = quality.quality_summary(raw, tidy, cleaning_log)
    return analytics.build_brief(
        tidy,
        summary,
        area="Pune",
        source_name=filename,
        source_hash=filename,
    )


def parse_manifest(text: str) -> dict[str, dict[str, Any]]:
    """Expected planted faults per week, from the machine-readable index block."""
    weeks: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    counts: dict[str, int] = {}
    gaps: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in ("week1:", "week2:"):
            if current is not None:
                current["counts"], current["gap_stations"] = counts, gaps
            current = {}
            weeks[stripped.rstrip(":")] = current
            counts, gaps = {}, []
            continue
        if current is None:
            continue
        match = re.match(r"^([A-Z_]+)\b(.*)$", stripped)
        if not match:
            continue
        kind, rest = match.group(1), match.group(2)
        if kind == "GAP":
            station = _GAP_STATION.search(f"GAP{rest}")
            if station:
                gaps.append(station.group(1).strip())
            continue
        if kind not in _FAULT_FLAGS:
            continue
        counts[kind] = counts.get(kind, 0) + _rows_in(rest)
    if current is not None:
        current["counts"], current["gap_stations"] = counts, gaps
    return weeks


def _rows_in(text: str) -> int:
    """How many data rows one manifest line covers (a range counts every row)."""
    span = _ROW_RANGE.search(text)
    if span:
        return int(span.group(2)) - int(span.group(1)) + 1
    return max(1, len(_ROW_SINGLE.findall(text.split("(")[0])))


# ------------------------------------------------------------------ scoring


def _outputs(brief: Brief, diff: DiffResult) -> list[Any]:
    """Every stage output for one brief, all offline."""
    interp = stages.interpret(brief, diff)
    plans = [stages.plan_actions(brief, diff, persona) for persona in ("ward_officer", "housing_society", "school")]
    note = stages.write_internal_note(brief, diff, interp, plans[0])
    advisories = [
        stages.write_advisory(brief, "English", "simple", "Pune"),
        stages.write_advisory(brief, "English", "standard", "Pune"),
        stages.write_advisory(brief, "Hindi", "simple", "Pune"),
    ]
    worst = fallback.brief_snapshot(brief)["worst_station"]
    if worst:
        advisories.append(stages.write_advisory(brief, "English", "standard", "Pune", station=worst))
    questions = ask.starter_questions(brief, diff, "ward_officer")
    answers = [ask.answer(questions[0], brief, diff, "ward_officer")] if questions else []
    return [interp, *plans, note, *advisories, *answers]


def _cites(outputs: list[Any]) -> list[str]:
    """Every finding ID cited anywhere in the outputs (including note markdown)."""
    found: list[str] = []
    for output in outputs:
        for text in stages._strings(output.model_dump()):
            found += re.findall(r"\[([A-Z]\d{1,3})\]", text)
    return found


def _fault_scores(brief: Brief, expected: dict[str, Any]) -> dict[str, Any]:
    """Planted-fault recall: flags actually raised against the manifest's expectations."""
    counts: dict[str, int] = expected.get("counts") or {}
    flag_counts = brief.quality.flag_counts or {}
    per_kind: dict[str, list[int]] = {}
    for kind, wanted in counts.items():
        if wanted <= 0:
            continue
        per_kind[kind] = [wanted, min(wanted, int(flag_counts.get(_FAULT_FLAGS[kind], 0)))]
    gap_stations = expected.get("gap_stations") or []
    gaps_found = 0
    for station in gap_stations:
        if any(station.lower() in gap.lower() and "no readings" in gap.lower() for gap in brief.quality.gaps):
            gaps_found += 1
    if gap_stations:
        per_kind["GAP"] = [len(gap_stations), gaps_found]
    wanted_total = sum(pair[0] for pair in per_kind.values())
    found_total = sum(pair[1] for pair in per_kind.values())
    return {
        "kinds": per_kind,
        "recall": (found_total / wanted_total) if wanted_total else 1.0,
    }


def _score_brief(name: str, brief: Brief, diff: DiffResult, expected: dict[str, Any]) -> dict[str, Any]:
    outputs = _outputs(brief, diff)
    known = {finding.id for finding in brief.findings}
    cited = _cites(outputs)
    valid = [cite for cite in cited if cite in known]
    unsupported = 0
    remaining = 0
    contradictions = 0
    for output in outputs:
        issues = stages.verification_issues(output, brief, diff)
        unsupported += sum(1 for issue in issues if issue.kind == "unsupported_number")
        remaining += int(output.verification.get("remaining", 0))
        if output.__class__.__name__ == "Advisory":
            contradictions += len(
                stages.category_contradictions(" ".join(stages._strings(output.model_dump())), output.category)
            )
    return {
        "file": name,
        "faults": _fault_scores(brief, expected),
        "citations": valid,
        "citation_validity": (len(valid) / len(cited)) if cited else 1.0,
        "unsupported_numbers": unsupported,
        "category_contradictions": contradictions,
        "verification_remaining": remaining,
        "outputs": len(outputs),
    }


def run_eval(live: bool = False) -> dict[str, Any]:
    """Score both sample weeks. Offline by default: no key, no network, cached replays only."""
    from llm import client

    original = client.llm_available
    if not live:
        client.llm_available = lambda: False  # type: ignore[assignment]
    try:
        manifest = parse_manifest(MANIFEST.read_text(encoding="utf-8"))
        briefs = {name: build_brief(name) for name in SAMPLES}
        week1, week2 = briefs[SAMPLES[0]], briefs[SAMPLES[1]]
        first_diff = DiffResult(has_previous=False)
        second_diff = _diff(week2, week1)
        results = [
            _score_brief(SAMPLES[0], week1, first_diff, manifest.get("week1", {})),
            _score_brief(SAMPLES[1], week2, second_diff, manifest.get("week2", {})),
        ]
    finally:
        client.llm_available = original  # type: ignore[assignment]

    overall = {
        "fault_recall": min(result["faults"]["recall"] for result in results),
        "citation_validity": min(result["citation_validity"] for result in results),
        "unsupported_numbers": max(result["unsupported_numbers"] for result in results),
        "category_contradictions": max(result["category_contradictions"] for result in results),
        "verification_remaining": sum(result["verification_remaining"] for result in results),
    }
    report = {"files": results, "overall": overall, "floors": dict(FLOORS)}
    print(format_table(report))
    return report


def _diff(current: Brief, previous: Brief) -> DiffResult:
    from core import diff as diff_module

    return diff_module.diff_briefs(current, previous)


def format_table(report: dict[str, Any]) -> str:
    """Plain text scoreboard for the terminal (and the pitch)."""
    widths = (22, 26, 9, 12, 15)
    header = (
        f"{'file'.ljust(widths[0])}{'faults detected'.ljust(widths[1])}"
        f"{'cites'.ljust(widths[2])}{'unsupported'.ljust(widths[3])}contradictions"
    )
    lines = ["== promptwars prompt eval (offline) ==", header, "-" * sum(widths)]
    for result in report["files"]:
        faults = result["faults"]
        found = sum(pair[1] for pair in faults["kinds"].values())
        wanted = sum(pair[0] for pair in faults["kinds"].values())
        faults_text = f"{found}/{wanted} ({faults['recall']:.0%})"
        cites = str(len(result["citations"]))
        lines.append(
            f"{result['file'].ljust(widths[0])}{faults_text.ljust(widths[1])}"
            f"{cites.ljust(widths[2])}{str(result['unsupported_numbers']).ljust(widths[3])}"
            f"{result['category_contradictions']}"
        )
    lines.append("-" * sum(widths))
    overall = report["overall"]
    lines.append(
        "overall: "
        f"fault recall {overall['fault_recall']:.0%} · "
        f"citation validity {overall['citation_validity']:.0%} · "
        f"unsupported numbers {overall['unsupported_numbers']} · "
        f"category contradictions {overall['category_contradictions']} · "
        f"unrepaired issues {overall['verification_remaining']}"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    result = run_eval()
    overall = result["overall"]
    failed = [
        key
        for key, floor in FLOORS.items()
        if (overall[key] < floor if key in ("fault_recall", "citation_validity") else overall[key] > floor)
    ]
    raise SystemExit(1 if failed else 0)
