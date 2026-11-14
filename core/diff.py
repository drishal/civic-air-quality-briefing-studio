"""Compare a current Brief with the previous one ("what changed").

All numbers are computed here and exposed in `summary_facts` so generated
text never has to calculate anything itself. Findings are compared by a stable
key (type + stations + pollutant), not by title, because titles carry numbers
that change every week. Indicative AQIs are compared only when a brief has no
official AQI at all, and a station that becomes a hotspot while its data is not
fully reliable is labelled "unverified" instead of being promoted silently.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

from core.models import Brief, DiffResult, StationAQI, StationChange

DELTA_THRESHOLD = 10.0  # AQI points; smaller moves count as "same"
HOTSPOT_N = 3

_DIRECTION_ORDER = {
    "worse": 0,
    "new": 1,
    "unknown": 2,
    "same": 3,
    "better": 4,
    "dropped": 5,
}
_FINDING_STATUS_ORDER = {"new": 0, "resolved": 1, "changed": 2, "persisting": 3}
_NUMBERS = re.compile(r"\d+(?:\.\d+)?")
_HEADLINE_KEYS = (
    "n_worse",
    "n_better",
    "n_same",
    "mean_delta",
    "worst_station",
    "worst_delta",
    "best_station",
    "best_delta",
    "max_aqi_now",
    "n_category_transitions",
    "n_new_hotspots",
    "n_resolved_hotspots",
    "n_new_findings",
    "n_resolved_findings",
)


def _num(value: Any, nd: int = 1) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def _comparable(brief: Brief) -> dict[str, StationAQI]:
    """Stations as they may be compared: indicative values count only when nothing is official."""
def _has_official(brief: Brief) -> bool:
    return any(s.aqi is not None and s.aqi_kind == "official" for s in brief.station_aqi)


def _comparable(brief: Brief, mask_indicative: bool | None = None) -> dict[str, StationAQI]:
    """Stations as they may be compared: indicative values count only when nothing is official."""
    if mask_indicative is None:
        mask_indicative = _has_official(brief)
    view: dict[str, StationAQI] = {}
    for s in brief.station_aqi:
        if mask_indicative and s.aqi_kind == "indicative":
            # An indicative AQI is not comparable with an official one: show no value.
            view[s.station] = s.model_copy(update={"aqi": None, "category": None})
        else:
            view[s.station] = s
    return view


def _top_hotspots(brief: Brief, mask_indicative: bool | None = None) -> list[tuple[str, bool]]:
    """Top stations as (name, unverified): unverified when confidence is not high."""
    ranked = sorted(
        (s for s in _comparable(brief, mask_indicative).values() if s.aqi is not None),
        key=lambda s: (-s.aqi, s.station),
    )
    return [(s.station, s.confidence != "high") for s in ranked[:HOTSPOT_N]]

def _hotspot_label(entry: tuple[str, bool]) -> str:
    name, unverified = entry
    return f"{name} (unverified)" if unverified else name


def _finding_key(finding: Any) -> str:
    """Stable identity of a finding: type + stations + pollutant (never the title's numbers)."""
    pollutant = str(finding.facts.get("pollutant") or "")
    if finding.stations:
        subject = ",".join(sorted(str(station) for station in finding.stations))
    else:
        # Nothing to identify it by but the title, with its changeable numbers removed.
        subject = " ".join(_NUMBERS.sub("", (finding.title or "").lower()).replace(":", " ").split())
    return f"{finding.type}|{subject}|{pollutant}"



def _family_key(finding: Any) -> Optional[str]:
    """Looser identity for findings whose station list moves as stations cross a threshold."""
    if finding.type not in ("exceedance", "pattern"):
        return None
    pollutant = str(finding.facts.get("pollutant") or "")
    return f"{finding.type}|{pollutant}"

def _change(prev: Optional[StationAQI], now: Optional[StationAQI]) -> StationChange:
    """One station's change row, with `new`/`dropped` for one-sided stations."""
    if prev is None and now is not None:
        return StationChange(
            station=now.station,
            aqi_prev=None,
            aqi_now=now.aqi,
            delta=None,
            category_prev=None,
            category_now=now.category,
            direction="new",
        )
    if now is None and prev is not None:
        return StationChange(
            station=prev.station,
            aqi_prev=prev.aqi,
            aqi_now=None,
            delta=None,
            category_prev=prev.category,
            category_now=None,
            direction="dropped",
        )
    assert prev is not None and now is not None
    delta = None
    if prev.aqi is not None and now.aqi is not None:
        delta = _num(now.aqi - prev.aqi)
    if delta is None:
        direction = "unknown"
    elif delta > DELTA_THRESHOLD:
        direction = "worse"
    elif delta < -DELTA_THRESHOLD:
        direction = "better"
    else:
        direction = "same"
    return StationChange(
        station=now.station,
        aqi_prev=prev.aqi,
        aqi_now=now.aqi,
        delta=delta,
        category_prev=prev.category,
        category_now=now.category,
        direction=direction,
    )


def _finding_changes(
    current: Brief, previous: Brief
) -> tuple[list[str], list[str], list[dict[str, Optional[str]]], dict[str, int]]:
    """(new titles, resolved titles, per-key changes, counts) compared by stable key.

    Exact keys identify a finding. City-wide findings (an exceedance per pollutant,
    a time-of-day pattern) also match on a family key, because their station set
    changes as stations cross the threshold: that is "changed", not new.
    """
    prev_by_key: dict[str, Any] = {}
    for finding in previous.findings:
        prev_by_key.setdefault(_finding_key(finding), finding)
    now_by_key: dict[str, Any] = {}
    for finding in current.findings:
        now_by_key.setdefault(_finding_key(finding), finding)

    prev_families: dict[str, list[str]] = {}
    for key, finding in prev_by_key.items():
        family = _family_key(finding)
        if family is not None:
            prev_families.setdefault(family, []).append(key)

    changes: list[dict[str, Optional[str]]] = []
    counts = {"new": 0, "resolved": 0, "changed": 0, "persisting": 0}
    matched_prev: set[str] = set()
    for key, finding in now_by_key.items():
        prev = prev_by_key.get(key)
        matched_key = key
        if prev is None:
            # Second chance: same city-wide finding with a different station list.
            family = _family_key(finding)
            candidates = prev_families.get(family, []) if family else []
            open_candidates = [candidate for candidate in candidates if candidate not in matched_prev]
            if open_candidates:
                matched_key = open_candidates[0]
                prev = prev_by_key[matched_key]
        if prev is None:
            status = "new"
        else:
            matched_prev.add(matched_key)
            status = "changed" if prev.title != finding.title else "persisting"
        counts[status] += 1
        changes.append(
            {
                "key": key,
                "status": status,
                "title_now": finding.title,
                "title_prev": prev.title if prev is not None else None,
            }
        )
    for key, prev in prev_by_key.items():
        if key in matched_prev:
            continue
        counts["resolved"] += 1
        changes.append(
            {"key": key, "status": "resolved", "title_now": None, "title_prev": prev.title}
        )
    changes.sort(key=lambda change: (_FINDING_STATUS_ORDER.get(str(change["status"]), 9), str(change["key"])))

    new_titles = [change["title_now"] for change in changes if change["status"] == "new" and change["title_now"]]
    resolved_titles = [
        change["title_prev"] for change in changes if change["status"] == "resolved" and change["title_prev"]
    ]
    return new_titles, resolved_titles, changes, counts


def _headline_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """The citable subset a writer should quote, always consistent with station_changes."""
    return {key: facts[key] for key in _HEADLINE_KEYS if key in facts}


def _max_aqi(brief: Brief, mask_indicative: bool | None = None) -> Optional[float]:
    values = [s.aqi for s in _comparable(brief, mask_indicative).values() if s.aqi is not None]
    return _num(max(values)) if values else None


def diff_briefs(current: Brief, previous: Optional[Brief]) -> DiffResult:
    """Diff two briefs; with no previous brief return a usable first-run result."""
    if previous is None:
        hotspots = _top_hotspots(current)
        facts: dict[str, Any] = {
            "n_stations": len(current.station_aqi),
            "n_findings": len(current.findings),
            "n_hotspots": len(hotspots),
            "delta_threshold": DELTA_THRESHOLD,
            "max_aqi_now": _max_aqi(current),
        }
        if hotspots:
            worst = next(s for s in _comparable(current).values() if s.station == hotspots[0][0])
            facts["max_aqi"] = worst.aqi
            facts["worst_station"] = worst.station
        facts = {k: v for k, v in facts.items() if v is not None}
        facts["headline_facts"] = _headline_facts(facts)
        return DiffResult(
            has_previous=False,
            # Nothing is "new" against no baseline; the hotspot list is in headline_facts.
            new_hotspots=[],
            summary_facts=facts,
        )

    # Compare like with like: if either brief has an official AQI, indicative values are
    # left out of the deltas and the hotspot ranking rather than mixed in silently.
    mask_indicative = _has_official(current) or _has_official(previous)
    prev_by = _comparable(previous, mask_indicative)
    now_by = _comparable(current, mask_indicative)
    changes = [
        _change(prev_by.get(name), now_by.get(name)) for name in sorted(set(prev_by) | set(now_by))
    ]
    changes.sort(
        key=lambda c: (
            _DIRECTION_ORDER.get(c.direction, 9),
            -(c.delta if c.delta is not None else 0.0),
            c.station,
        )
    )

    prev_top = _top_hotspots(previous, mask_indicative)
    now_top = _top_hotspots(current, mask_indicative)
    prev_names = [entry[0] for entry in prev_top]
    now_names = [entry[0] for entry in now_top]
    new_hotspots = [_hotspot_label(entry) for entry in now_top if entry[0] not in prev_names]
    resolved_hotspots = [entry[0] for entry in prev_top if entry[0] not in now_names]

    transitions = [
        f"{name}: {prev_by[name].category} -> {now_by[name].category}"
        for name in sorted(set(prev_by) & set(now_by))
        if prev_by[name].category
        and now_by[name].category
        and prev_by[name].category != now_by[name].category
    ]

    prev_rel = {s.station: s for s in previous.quality.stations}
    now_rel = {s.station: s for s in current.quality.stations}
    quality_changes: list[str] = []
    for name in sorted(set(prev_rel) & set(now_rel)):
        if prev_rel[name].label != now_rel[name].label:
            quality_changes.append(f"{name} reliability: {prev_rel[name].label} -> {now_rel[name].label}")
    if len(previous.quality.gaps) != len(current.quality.gaps):
        quality_changes.append(
            f"Data gap notes: {len(previous.quality.gaps)} -> {len(current.quality.gaps)}"
        )

    new_titles, resolved_titles, finding_changes, finding_counts = _finding_changes(current, previous)

    comparable = [c for c in changes if c.delta is not None]
    mean_delta = _num(sum(c.delta for c in comparable) / len(comparable)) if comparable else None
    worst = max(comparable, key=lambda c: c.delta) if comparable else None
    best = min(comparable, key=lambda c: c.delta) if comparable else None

    facts = {
        "n_stations_now": len(now_by),
        "n_stations_prev": len(prev_by),
        "delta_threshold": DELTA_THRESHOLD,
        "n_worse": sum(1 for c in changes if c.direction == "worse"),
        "n_better": sum(1 for c in changes if c.direction == "better"),
        "n_same": sum(1 for c in changes if c.direction == "same"),
        "n_new": sum(1 for c in changes if c.direction == "new"),
        "n_dropped": sum(1 for c in changes if c.direction == "dropped"),
        "mean_delta": mean_delta,
        "worst_station": worst.station if worst else None,
        "worst_delta": worst.delta if worst else None,
        "best_station": best.station if best else None,
        "best_delta": best.delta if best else None,
        "max_aqi_now": _max_aqi(current, mask_indicative),
        "max_aqi_prev": _max_aqi(previous, mask_indicative),
        "n_category_transitions": len(transitions),
        "n_new_hotspots": len(new_hotspots),
        "n_resolved_hotspots": len(resolved_hotspots),
        "n_new_findings": finding_counts["new"],
        "n_resolved_findings": finding_counts["resolved"],
        "n_changed_findings": finding_counts["changed"],
        "n_persisting_findings": finding_counts["persisting"],
    }
    facts = {k: v for k, v in facts.items() if v is not None}
    facts["headline_facts"] = _headline_facts(facts)

    return DiffResult(
        has_previous=True,
        previous_brief_id=previous.brief_id,
        station_changes=changes,
        new_hotspots=new_hotspots,
        resolved_hotspots=resolved_hotspots,
        category_transitions=transitions,
        quality_changes=quality_changes,
        new_finding_titles=new_titles,
        resolved_finding_titles=resolved_titles,
        finding_changes=finding_changes,
        summary_facts=facts,
    )
