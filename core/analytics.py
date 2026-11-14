"""Deterministic analytics: tidy table + quality summary -> Brief.

All numbers are computed here. Every finding carries the citable numbers in
`facts`, a plain-language `formula`, at most 50 evidence row ids from the tidy
table, and a confidence derived from station reliability. Findings are ranked
and numbered F1..Fn in a fixed order (hotspot, exceedance, trend, pattern, gap,
anomaly).
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from itertools import pairwise
from typing import Any

import numpy as np
import pandas as pd

from core.aqi import (
    MIN_HOURS,
    WHO_24H,
    category,
    daily_aqi,
    pollutant_averages,
    who_multiple,
)
from core.models import (
    Brief,
    Confidence,
    Finding,
    QualitySummary,
    StationAQI,
    StationReliability,
)

MAX_EVIDENCE = 50
MAX_HOTSPOTS = 3
MAX_EXCEEDANCES = 5
MAX_TRENDS = 4
MAX_ANOMALIES = 4
MAX_GAP_STATIONS = 3
TREND_STABLE = 1.0  # AQI units per day; below this the trend is "stable"
TREND_MIN_DAYS = 3  # a slope over two days is noise, not a trend
TREND_MIN_SLOPE = 10.0  # AQI units per day; below this only the steepest stations are kept
DISAGREE_RATIO = 1.4  # co-located stations more than 40% apart
GAP_MIN_HOURS = 3  # keep in step with core.quality.GAP_MIN_HOURS
_GAP_FLAGS = ("MISSING", "SENTINEL", "NEGATIVE", "OUT_OF_RANGE")

_CONF_BY_LABEL: dict[str, Confidence] = {
    "reliable": "high",
    "use with caution": "medium",
    "unreliable": "low",
}
_CONF_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
_POLL_ORDER = ("PM2.5", "PM10", "NO2", "SO2", "O3", "CO", "NH3")

# Wording that hedges a possible cause; never a claim of cause.
_SIGNATURE = {
    "PM2.5": "combustion and dust sources such as traffic, waste burning or construction",
    "PM10": "dust and mechanical sources such as roads, sweeping and construction",
    "NO2": "traffic exhaust",
    "CO": "traffic exhaust and combustion",
    "SO2": "fuel or industrial combustion",
    "O3": "afternoon photochemical formation",
    "NH3": "waste or agricultural sources",
}

_DAILY_COLUMNS = [
    "station", "ward", "date", "aqi", "category", "dominant",
    "n_pollutants", "subindices", "valid", "reason",
]
_AVG_COLUMNS = ["station", "ward", "date", "pollutant", "avg", "n_hours", "valid"]


# ---------------------------------------------------------------- small helpers
def _num(value: Any, nd: int = 2) -> float | None:
    """Finite float rounded to `nd` places, else None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def _facts(raw: dict[str, Any]) -> dict[str, float | int | str | None]:
    """Drop Nones/NaN and coerce numpy scalars to plain Python values."""
    out: dict[str, float | int | str | None] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if isinstance(value, (bool, np.bool_)):
            out[key] = int(value)
        elif isinstance(value, (np.integer, int)):
            out[key] = int(value)
        elif isinstance(value, (np.floating, float)):
            f = _num(value)
            if f is not None:
                out[key] = f
        else:
            out[key] = str(value)
    return out


def _worst_conf(confs: list[Confidence]) -> Confidence:
    if not confs:
        return "low"
    return min(confs, key=lambda c: _CONF_RANK.get(c, 0))


def _confidence(label: str | None) -> Confidence:
    return _CONF_BY_LABEL.get(label or "unreliable", "low")


def _slope(values: Any) -> float | None:
    """Least-squares slope per valid day (x = order of the valid days)."""
    arr = np.asarray([float(v) for v in values if v is not None and not pd.isna(v)], dtype=float)
    if arr.size < 2:
        return None
    return _num(np.polyfit(np.arange(arr.size), arr, 1)[0], 2)


def _subindices(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, sub in value.items():
        f = _num(sub, 1)
        if f is not None:
            out[str(key)] = f
    return out

def _sample_ids(ids: list[str], limit: int = MAX_EVIDENCE) -> list[str]:
    """Keep `ids` order; when longer than `limit`, take evenly spaced entries."""
    if len(ids) <= limit:
        return ids
    if limit <= 1:
        return ids[:limit]
    step = (len(ids) - 1) / (limit - 1)
    return [ids[round(i * step)] for i in range(limit)]


def _evidence(df: pd.DataFrame | None, limit: int = MAX_EVIDENCE) -> list[str]:
    """Up to `limit` row ids, evenly sampled across the frame's own order."""
    if df is None or df.empty or "row_id" not in df.columns:
        return []
    return _sample_ids([str(r) for r in df["row_id"].dropna().tolist()], limit)


def _time_sorted(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "ts" not in df.columns:
        return df if df is not None else pd.DataFrame()
    return df.sort_values("ts", kind="mergesort")


def _station_holes(tidy: pd.DataFrame, station: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """(last hour with data, next hour with data) around every hole >= GAP_MIN_HOURS."""
    if tidy.empty or "ts" not in tidy.columns:
        return []
    sub = tidy[(tidy["station"] == station) & tidy["ts"].notna() & tidy["value_std"].notna()]
    if sub.empty:
        return []
    hours = sorted(set(sub["ts"].dt.floor("h").tolist()))
    holes: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for before, after in pairwise(hours):
        missing = int((after - before) / pd.Timedelta(hours=1)) - 1
        if missing >= GAP_MIN_HOURS:
            holes.append((before, after))
    return holes


def _hole_facts(holes: list[tuple[pd.Timestamp, pd.Timestamp]]) -> dict[str, Any]:
    """Widest hole as citable strings; empty when there is no hole."""
    if not holes:
        return {}
    before, after = max(holes, key=lambda h: (h[1] - h[0]))
    return {
        "gap_start": f"{before:%Y-%m-%d %H:%M}",
        "gap_end": f"{after:%Y-%m-%d %H:%M}",
        "hours_missing": int((after - before) / pd.Timedelta(hours=1)) - 1,
    }


def _gap_evidence(tidy: pd.DataFrame, station: str) -> pd.DataFrame:
    """Rows next to a station's holes plus the rows that carry gap flags.

    An outage is an absence of rows: only its two neighbours exist, so those are
    the evidence and the window itself is reported in `facts`.
    """
    if tidy.empty or "station" not in tidy.columns:
        return tidy
    parts: list[pd.DataFrame] = []
    if "flags" in tidy.columns:
        flagged = tidy[
            tidy["flags"].apply(lambda f: any(_has_flag(f, flag) for flag in _GAP_FLAGS))
            & (tidy["station"] == station)
        ]
        if not flagged.empty:
            parts.append(flagged)
    sub = tidy[(tidy["station"] == station) & tidy["ts"].notna() & tidy["value_std"].notna()].copy()
    if not sub.empty:
        sub["_hour"] = sub["ts"].dt.floor("h")
        picks: list[Any] = []
        for before, after in _station_holes(tidy, station):
            prior = sub.index[sub["_hour"] == before]
            following = sub.index[sub["_hour"] == after]
            if len(prior):
                picks.append(prior[0])
            if len(following):
                picks.append(following[0])
        if picks:
            parts.append(sub.loc[sorted(set(picks))].drop(columns="_hour"))
    if not parts:
        return tidy.iloc[0:0]
    return _time_sorted(pd.concat(parts))


def _trend_evidence(tidy: pd.DataFrame, station: str, days: pd.DataFrame) -> pd.DataFrame:
    """Valid readings of each valid day's dominant pollutant, in time order."""
    if tidy.empty or days is None or days.empty:
        return tidy.iloc[0:0]
    parts: list[pd.DataFrame] = []
    for _, day in days.sort_values("date").iterrows():
        dominant = day.get("dominant")
        if not dominant or pd.isna(dominant):
            continue
        rows = _station_pollutant_day(tidy, station, str(dominant), str(day.get("date")))
        if not rows.empty:
            parts.append(rows)
    if not parts:
        return _time_sorted(_station_day(tidy, station, str(days.iloc[-1].get("date"))))
    return _time_sorted(pd.concat(parts))


def _backfill_evidence(findings: list[Finding], tidy: pd.DataFrame) -> list[Finding]:
    """Safety net: no finding stays without evidence while the table has rows."""
    if tidy is None or tidy.empty:
        return findings
    fallback = _time_sorted(tidy[tidy["value_std"].notna()])
    if fallback.empty:
        fallback = tidy
    for finding in findings:
        if finding.evidence_row_ids or "station" not in tidy.columns:
            continue
        if finding.stations:
            rows = fallback[fallback["station"].isin(finding.stations)]
        else:
            rows = fallback
        # A finding about stations with no rows at all genuinely has no evidence.
        finding.evidence_row_ids = _evidence(_time_sorted(rows))
    return findings


def _empty_daily() -> pd.DataFrame:
    return pd.DataFrame(columns=_DAILY_COLUMNS)


def _empty_avgs() -> pd.DataFrame:
    return pd.DataFrame(columns=_AVG_COLUMNS)


def _valid_days(daily: pd.DataFrame) -> pd.DataFrame:
    if daily is None or daily.empty:
        return _empty_daily()
    mask = daily["valid"].astype(bool) & daily["aqi"].notna()
    return daily[mask]

def _indicative_days(daily: pd.DataFrame, station: str) -> pd.DataFrame:
    """Days whose AQI is indicative only (strict CPCB rule not met)."""
    if daily is None or daily.empty or "indicative" not in daily.columns:
        return daily.iloc[0:0] if daily is not None else _empty_daily()
    mask = (
        daily["indicative"].astype(bool)
        & daily["indicative_aqi"].notna()
        & (daily["station"] == station)
    )
    return daily[mask].sort_values("date")


def _day_mask(tidy: pd.DataFrame, date: str | None) -> pd.Series:
    if date is None or "ts" not in tidy.columns:
        return pd.Series(False, index=tidy.index)
    return tidy["ts"].dt.strftime("%Y-%m-%d") == str(date)


def _station_day(tidy: pd.DataFrame, station: str, date: str | None) -> pd.DataFrame:
    if tidy.empty:
        return tidy
    mask = (tidy["station"] == station) & _day_mask(tidy, date) & tidy["value_std"].notna()
    return tidy[mask]


def _station_pollutant_day(
    tidy: pd.DataFrame, station: str, pollutant: str, date: str | None
) -> pd.DataFrame:
    if tidy.empty:
        return tidy
    mask = (
        (tidy["station"] == station)
        & (tidy["pollutant"] == pollutant)
        & _day_mask(tidy, date)
        & tidy["value_std"].notna()
    )
    return tidy[mask]


def _has_flag(flags: Any, name: str) -> bool:
    return isinstance(flags, (list, tuple)) and name in flags


# ---------------------------------------------------------------- station table
def _ward_map(tidy: pd.DataFrame) -> dict[str, str | None]:
    wards: dict[str, str | None] = {}
    if tidy.empty or "ward" not in tidy.columns:
        return wards
    for station, group in tidy.groupby("station"):
        found = [w for w in group["ward"].tolist() if isinstance(w, str) and w]
        wards[str(station)] = found[0] if found else None
    return wards


def _station_table(
    tidy: pd.DataFrame,
    daily: pd.DataFrame,
    rel: dict[str, StationReliability],
) -> list[StationAQI]:
    wards = _ward_map(tidy)
    valid = _valid_days(daily)
    out: list[StationAQI] = []
    for station in sorted(str(s) for s in tidy["station"].dropna().unique()):
        info = rel.get(station)
        reliability = _num(info.reliability, 2) if info else 0.0
        confidence = _confidence(info.label if info else "unreliable")
        base = dict(
            station=station,
            ward=wards.get(station),
            reliability=reliability if reliability is not None else 0.0,
            confidence=confidence,
        )
        rows = valid[valid["station"] == station].sort_values("date") if not valid.empty else valid
        if not rows.empty:
            last = rows.iloc[-1].to_dict()
            aqi = _num(last.get("aqi"), 1)
            cat = last.get("category")
            if not cat and aqi is not None:
                cat = category(aqi)
            out.append(
                StationAQI(
                    **base,
                    aqi=aqi,
                    category=str(cat) if cat else None,
                    dominant=str(last["dominant"]) if last.get("dominant") else None,
                    subindices=_subindices(last.get("subindices")),
                    latest_date=str(last["date"]) if last.get("date") is not None else None,
                    n_valid_days=int(len(rows)),
                    mean_aqi_period=_num(rows["aqi"].mean(), 1),
                    trend_per_day=_slope(rows["aqi"].tolist()),
                )
            )
            continue
        # No official day: fall back to an indicative AQI, clearly marked and low confidence.
        ind = _indicative_days(daily, station)
        if ind.empty:
            out.append(StationAQI(**base))
            continue
        last = ind.iloc[-1].to_dict()
        ind_aqi = _num(last.get("indicative_aqi"), 1)
        ind_cat = last.get("indicative_category")
        if not ind_cat and ind_aqi is not None:
            ind_cat = category(ind_aqi)
        out.append(
            StationAQI(
                **{**base, "confidence": "low"},
                aqi=ind_aqi,
                category=str(ind_cat) if ind_cat else None,
                dominant=str(last["indicative_dominant"]) if last.get("indicative_dominant") else None,
                subindices=_subindices(last.get("indicative_subindices")),
                latest_date=str(last["date"]) if last.get("date") is not None else None,
                n_valid_days=0,
                mean_aqi_period=_num(ind["indicative_aqi"].mean(), 1),
                trend_per_day=_slope(ind["indicative_aqi"].tolist()) if len(ind) >= 2 else None,
                aqi_kind="indicative",
                indicative_reason=str(last.get("indicative_reason") or ""),
            )
        )
    out.sort(key=lambda s: (s.aqi is None, s.aqi_kind != "official", -(s.aqi or 0.0), s.station))
    return out


# ---------------------------------------------------------------- findings
def _hotspots(
    station_aqi: list[StationAQI],
    tidy: pd.DataFrame,
    rel: dict[str, StationReliability],
) -> list[Finding]:
    ranked = sorted(
        (s for s in station_aqi if s.aqi is not None),
        key=lambda s: (s.aqi_kind != "official", -s.aqi, s.station),
    )
    official = [s for s in ranked if s.aqi_kind == "official"]
    pool = official or ranked  # indicative values compete only when no station is official
    findings: list[Finding] = []
    for s in pool[:MAX_HOTSPOTS]:
        info = rel.get(s.station)
        label = info.label if info else "unreliable"
        indicative = s.aqi_kind == "indicative"
        confidence: Confidence = "low" if indicative else _confidence(label)
        caveats: list[str] = []
        if indicative:
            caveats.append(
                s.indicative_reason or "indicative only: the CPCB completeness rule is not met."
            )
        if label == "unreliable":
            caveats.append(
                f"{s.station} is unreliable (reliability {s.reliability:.2f}); "
                "treat this hotspot as provisional and verify the sensor first."
            )
        elif label == "use with caution":
            caveats.append(
                f"{s.station} data is usable with caution (reliability {s.reliability:.2f})."
            )
        if confidence != "high":
            caveats.append(
                f"unverified: sensor reliability {label} (reliability {s.reliability:.2f}); "
                "check the monitor before acting on this hotspot."
            )
        if s.n_valid_days < 2 and not indicative:
            caveats.append("Only one valid day of data, so the ranking can change.")
        facts = _facts(
            {
                "aqi": s.aqi,
                "category": s.category,
                "dominant": s.dominant,
                "ward": s.ward,
                "n_valid_days": s.n_valid_days,
                "reliability": s.reliability,
                "mean_aqi_period": s.mean_aqi_period,
                "trend_per_day": s.trend_per_day,
                "aqi_kind": s.aqi_kind,
                "verification_needed": "yes" if confidence != "high" else None,
                "indicative_reason": s.indicative_reason if indicative else None,
            }
        )
        findings.append(
            Finding(
                id="",
                type="hotspot",
                title=(
                    f"Hotspot (indicative): {s.station} at AQI {s.aqi:.0f}"
                    if indicative
                    else f"Hotspot: {s.station} at AQI {s.aqi:.0f}"
                ),
                facts=facts,
                formula=(
                    f"Latest valid 24-hour AQI for {s.station}: worst pollutant "
                    "sub-index of the day's 24-hour means; stations ranked by that AQI."
                    + (
                        " Below the CPCB completeness rule this is an indicative AQI, "
                        "computed from the readings present."
                        if indicative
                        else ""
                    )
                ),
                evidence_row_ids=_evidence(_station_day(tidy, s.station, s.latest_date)),
                stations=[s.station],
                confidence=confidence,
                caveats=caveats,
            )
        )
    return findings


def _exceedances(
    avgs: pd.DataFrame,
    tidy: pd.DataFrame,
    rel: dict[str, StationReliability],
) -> list[Finding]:
    if avgs is None or avgs.empty:
        return []
    findings: list[Finding] = []
    for pollutant in _POLL_ORDER:
        limit = WHO_24H.get(pollutant)
        if limit is None:
            continue
        sub = avgs[(avgs["pollutant"] == pollutant) & avgs["valid"].astype(bool)].copy()
        if sub.empty:
            continue
        latest = sub.sort_values("date").groupby("station", dropna=False).tail(1)
        latest = latest[latest["avg"].notna()]
        above = latest[latest["avg"] > limit]
        if above.empty:
            continue
        multiples: dict[str, float] = {}
        for _, row in above.iterrows():
            station = str(row["station"])
            m = who_multiple(pollutant, float(row["avg"]))
            multiples[station] = float(m) if m is not None else float(row["avg"]) / float(limit)
        max_station = max(multiples, key=lambda st: (multiples[st], st))
        row = above[above["station"] == max_station].iloc[0]
        labels = {
            st: (rel[st].label if st in rel else "unreliable") for st in multiples
        }
        caveats = [
            f"{st} data is rated {lab}; this multiple may be off."
            for st, lab in sorted(labels.items())
            if lab != "reliable"
        ]
        findings.append(
            Finding(
                id="",
                type="exceedance",
                title=(
                    f"{pollutant} above the WHO 24-hour guideline at "
                    f"{len(above)} of {len(latest)} stations"
                ),
                facts=_facts(
                    {
                        "pollutant": pollutant,
                        "who_24h_limit": float(limit),
                        "n_stations": int(len(latest)),
                        "n_exceeding": int(len(above)),
                        "max_multiple": multiples[max_station],
                        "max_avg": row["avg"],
                        "max_station": max_station,
                        "date": str(row["date"]),
                    }
                ),
                formula=(
                    "Daily mean (24-hour; 8-hour maximum for O3 and CO) divided by "
                    "the WHO 2021 24-hour guideline for the same pollutant."
                ),
                evidence_row_ids=_evidence(
                    _station_pollutant_day(tidy, max_station, pollutant, str(row["date"]))
                ),
                stations=sorted(multiples),
                confidence=_worst_conf([_confidence(lab) for lab in labels.values()]),
                caveats=caveats,
            )
        )
    findings.sort(key=lambda f: -(f.facts.get("max_multiple") or 0.0))
    return findings[:MAX_EXCEEDANCES]


def _trends(
    station_aqi: list[StationAQI], daily: pd.DataFrame, tidy: pd.DataFrame
) -> list[Finding]:
    valid = _valid_days(daily)
    ranked = [
        s
        for s in station_aqi
        if s.trend_per_day is not None and s.n_valid_days >= TREND_MIN_DAYS
    ]
    ranked.sort(key=lambda s: (-abs(s.trend_per_day or 0.0), s.station))
    # Report the steepest stations; weaker slopes only ride along in the same top list.
    steep = [s for s in ranked if abs(s.trend_per_day or 0.0) >= TREND_MIN_SLOPE]
    gentle = [s for s in ranked if abs(s.trend_per_day or 0.0) < TREND_MIN_SLOPE]
    candidates = (steep + gentle)[:MAX_TRENDS]
    findings: list[Finding] = []
    for s in candidates:
        slope = float(s.trend_per_day or 0.0)
        if slope >= TREND_STABLE:
            direction = "up"
        elif slope <= -TREND_STABLE:
            direction = "down"
        else:
            direction = "stable"
        rows = (
            valid[valid["station"] == s.station].sort_values("date")
            if not valid.empty
            else valid
        )
        evidence_rows = _trend_evidence(tidy, s.station, rows)
        dominants = sorted({str(d) for d in rows.get("dominant", []) if isinstance(d, str) and d})
        if not dominants and rows is not None and not rows.empty and rows.iloc[-1].get("dominant"):
            dominants = [str(rows.iloc[-1]["dominant"])]
        first = _num(rows.iloc[0]["aqi"], 1) if not rows.empty else None
        last = _num(rows.iloc[-1]["aqi"], 1) if not rows.empty else None
        caveats: list[str] = []
        if s.confidence != "high":
            caveats.append(
                "Few valid days and/or unreliable data limit this trend; "
                "read it as a direction, not a forecast."
            )
        caveats.append(
            f"The slope rests on {s.n_valid_days} valid day(s) at {slope:+.1f} AQI per day; "
            "a short period cannot separate a trend from day-to-day weather."
        )
        findings.append(
            Finding(
                id="",
                type="trend",
                title=f"{s.station}: AQI trend {direction} ({slope:+.1f} per day)",
                facts=_facts(
                    {
                        "station": s.station,
                        "direction": direction,
                        "slope_per_day": slope,
                        "first_aqi": first,
                        "last_aqi": last,
                        "delta_total": (last - first) if (first is not None and last is not None) else None,
                        "n_days": s.n_valid_days,
                        "n_valid_days": s.n_valid_days,
                        "dominant": ", ".join(dominants) if dominants else None,
                    }
                ),
                formula=(
                    "Least-squares slope of the station's valid daily AQI values "
                    "against the day order; |slope| below 1 AQI per day counts as stable."
                ),
                evidence_row_ids=_evidence(evidence_rows),
                stations=[s.station],
                confidence=s.confidence,
                caveats=caveats,
            )
        )
    return findings


def _patterns(
    tidy: pd.DataFrame,
    station_aqi: list[StationAQI],
    rel: dict[str, StationReliability],
) -> list[Finding]:
    if tidy.empty:
        return []
    valid = tidy[tidy["value_std"].notna() & tidy["ts"].notna()]
    if valid.empty:
        return []
    dominants = Counter(s.dominant for s in station_aqi if s.dominant)
    target = dominants.most_common(1)[0][0] if dominants else "PM2.5"
    sub = valid[valid["pollutant"] == target]
    if sub.empty:
        target = str(valid["pollutant"].iloc[0])
        sub = valid[valid["pollutant"] == target]
    hours = sub.assign(hour=sub["ts"].dt.hour)
    hourly = hours.groupby("hour")["value_std"].mean()
    if len(hourly) < 6:
        return []
    peak_hour = int(hourly.idxmax())
    trough_hour = int(hourly.idxmin())
    peak_mean = _num(hourly.max(), 1)
    trough_mean = _num(hourly.min(), 1)
    ratio = (
        _num(peak_mean / trough_mean, 2)
        if peak_mean is not None and trough_mean not in (None, 0.0)
        else None
    )
    window = {(peak_hour - 1) % 24, peak_hour, (peak_hour + 1) % 24}
    peak_rows = hours[hours["hour"].isin(window)]
    stations = sorted(str(st) for st in peak_rows["station"].dropna().unique())
    confidence = _worst_conf(
        [_confidence(rel[st].label if st in rel else "unreliable") for st in stations]
    )
    findings = [
        Finding(
            id="",
            type="pattern",
            title=f"{target} peaks around {peak_hour:02d}:00",
            facts=_facts(
                {
                    "pollutant": target,
                    "peak_hour": peak_hour,
                    "window_start": (peak_hour - 1) % 24,
                    "window_end": (peak_hour + 1) % 24,
                    "peak_mean": peak_mean,
                    "trough_hour": trough_hour,
                    "trough_mean": trough_mean,
                    "peak_to_trough": ratio,
                    "n_readings": int(len(peak_rows)),
                    "n_hours": int(len(hourly)),
                }
            ),
            formula=(
                "Mean of all valid readings of the dominant pollutant at each hour of "
                "day across the period; the peak window is the peak hour plus one hour "
                "on each side."
            ),
            evidence_row_ids=_evidence(peak_rows),
            stations=stations,
            confidence=confidence,
            caveats=[
                f"This time-of-day pattern is consistent with "
                f"{_SIGNATURE.get(target, 'sources this data cannot identify')}; "
                "it is not proof of a cause.",
                "The peak hour shifts with weather, traffic and day of week.",
            ],
        )
    ]
    return findings


def _no_readings_finding(tidy: pd.DataFrame) -> list[Finding]:
    """Unambiguous gap finding when nothing in the table is usable."""
    if (
        tidy is not None
        and not tidy.empty
        and "value_std" in tidy.columns
        and tidy["value_std"].notna().any()
    ):
        return []
    n_rows = 0 if tidy is None else int(len(tidy))
    caveat = (
        "No readings were loaded, so no air-quality claim can be made."
        if n_rows == 0
        else "Every reading in this table was empty, a sentinel code (NA, --, -999), "
        "negative, out of range or a duplicate, so no air-quality claim can be made."
    )
    return [
        Finding(
            id="",
            type="gap",
            title="No usable readings",
            facts=_facts({"n_readings": n_rows, "n_usable": 0}),
            formula=(
                "A reading is usable when its cell held a number in a stated or assumed "
                "standard unit and was not negative, out of range, censored away or duplicated."
            ),
            evidence_row_ids=_evidence(tidy),
            confidence="high",
            caveats=[caveat],
        )
    ]


def _distinct_hours(tidy: pd.DataFrame, station: str) -> int:
    """Distinct clock hours with at least one valid reading at a station."""
    if tidy.empty or "ts" not in tidy.columns or "value_std" not in tidy.columns:
        return 0
    rows = tidy[(tidy["station"] == station) & tidy["value_std"].notna() & tidy["ts"].notna()]
    if rows.empty:
        return 0
    return int(rows["ts"].dt.floor("h").nunique())


def _indicative_finding(station_aqi: list[StationAQI], tidy: pd.DataFrame) -> list[Finding]:
    """Say loudly that an AQI was computed below the CPCB completeness rule."""
    indicative = [s for s in station_aqi if s.aqi_kind == "indicative" and s.aqi is not None]
    if not indicative:
        return []
    stations = [s.station for s in indicative]
    hours = [_distinct_hours(tidy, station) for station in stations]
    min_hours = min(hours) if hours else 0
    rows = (
        tidy[(tidy["station"].isin(stations)) & tidy["value_std"].notna()]
        if not tidy.empty and "station" in tidy.columns
        else tidy.iloc[0:0]
    )
    caveats = [s.indicative_reason for s in indicative if s.indicative_reason]
    return [
        Finding(
            id="",
            type="gap",
            title=f"Indicative AQI only: {min_hours} hours of data, CPCB needs {MIN_HOURS}",
            facts=_facts(
                {
                    "n_stations": len(stations),
                    "n_hours": min_hours,
                    "min_hours_required": MIN_HOURS,
                    "min_pollutants_required": 3,
                }
            ),
            formula=(
                "A daily AQI is official only with at least 16 valid hours per pollutant, "
                "three pollutants and PM2.5 or PM10 among them. Below that the AQI uses the "
                "readings present and is reported as indicative."
            ),
            evidence_row_ids=_evidence(_time_sorted(rows)),
            stations=stations,
            confidence="high",
            caveats=caveats
            + ["Indicative AQI ranks stations only when no station has an official AQI."],
        )
    ]


def _gaps(
    quality: QualitySummary,
    tidy: pd.DataFrame,
    station_aqi: list[StationAQI],
) -> list[Finding]:
    findings: list[Finding] = []
    for info in quality.stations:
        if info.label != "unreliable":
            continue
        holes = _station_holes(tidy, info.station)
        facts = {
            "coverage": _num(info.coverage, 2),
            "flagged_share": _num(info.flagged_share, 2),
            "reliability": _num(info.reliability, 2),
            "n_readings": info.n_readings,
            "n_gaps": len(holes),
        }
        facts.update(_hole_facts(holes))
        caveats = list(info.notes[:3]) or ["Readings from this station cannot headline a hotspot."]
        if holes:
            caveats.append(
                "The gap window itself has no rows: the evidence lists the readings just "
                "before and just after it."
            )
        findings.append(
            Finding(
                id="",
                type="gap",
                title=f"Unreliable readings at {info.station}",
                facts=_facts(facts),
                formula=(
                    "Coverage = valid readings divided by expected hourly readings; "
                    "reliability = coverage x (1 - flagged share); below 0.5 is unreliable. "
                    "A gap is a run of missing hours between two hours that have data."
                ),
                evidence_row_ids=_evidence(_gap_evidence(tidy, info.station)),
                stations=[info.station],
                confidence="high",
                caveats=caveats,
            )
        )
    findings = findings[:MAX_GAP_STATIONS]
    if quality.gaps:
        stations = (
            [str(s) for s in tidy["station"].dropna().unique()]
            if not tidy.empty and "station" in tidy.columns
            else []
        )
        holes_by_station = {station: _station_holes(tidy, station) for station in stations}
        worst = sorted(
            (station for station in stations if holes_by_station[station]),
            key=lambda station: (-len(holes_by_station[station]), station),
        )[:MAX_GAP_STATIONS]
        parts = [_gap_evidence(tidy, station) for station in worst]
        parts = [part for part in parts if not part.empty]
        rows = _time_sorted(pd.concat(parts)) if parts else tidy.iloc[0:0]
        if rows.empty and not tidy.empty and "flags" in tidy.columns:
            # No hourly hole to point at: show the rows the quality flags mark unusable.
            rows = tidy[
                tidy["flags"].apply(lambda f: any(_has_flag(f, flag) for flag in _GAP_FLAGS))
            ]
            rows = tidy
        facts = {"n_gaps": len(quality.gaps), "n_stations_with_gaps": len(worst)}
        facts.update(_hole_facts([h for station in worst for h in holes_by_station[station]]))
        caveats = list(quality.gaps[:5])
        if worst:
            caveats.append(
                "A gap is missing rows, so it has no row id of its own; the evidence lists "
                "the readings next to the widest holes."
            )
        findings.append(
            Finding(
                id="",
                type="gap",
                title="Data gaps limit what this brief can say",
                facts=_facts(facts),
                formula=(
                    "Count of gap statements reported by the quality engine; the widest "
                    "hourly hole is reported as its first and last missing hour."
                ),
                evidence_row_ids=_evidence(rows),
                stations=[],  # city-wide: the station-level gaps have their own findings
                confidence="high",
                caveats=caveats,
            )
        )
    findings += _indicative_finding(station_aqi, tidy)
    no_readings = _no_readings_finding(tidy)
    if no_readings:
        return no_readings  # one clear finding beats a wall of "no data" notes
    return findings


def _anomalies(
    tidy: pd.DataFrame,
    station_aqi: list[StationAQI],
) -> list[Finding]:
    findings: list[Finding] = []
    if not tidy.empty and "flags" in tidy.columns:
        stuck = tidy[tidy["flags"].apply(lambda f: _has_flag(f, "STUCK"))]
        spike = tidy[tidy["flags"].apply(lambda f: _has_flag(f, "SPIKE"))]
        suspect = pd.concat([stuck, spike]) if (not stuck.empty or not spike.empty) else stuck
        for station in sorted(str(s) for s in suspect["station"].dropna().unique()):
            n_stuck = int((stuck["station"] == station).sum()) if not stuck.empty else 0
            n_spike = int((spike["station"] == station).sum()) if not spike.empty else 0
            if n_stuck + n_spike < 3:
                continue
            findings.append(
                Finding(
                    id="",
                    type="anomaly",
                    title=f"Suspect sensor readings at {station}",
                    facts=_facts(
                        {
                            "station": station,
                            "n_stuck": n_stuck,
                            "n_spike": n_spike,
                            "n_readings": int((tidy["station"] == station).sum()),
                        }
                    ),
                    formula=(
                        "STUCK = six or more identical consecutive readings; "
                        "SPIKE = a value far above the rolling median of the previous readings."
                    ),
                    evidence_row_ids=_evidence(suspect[suspect["station"] == station]),
                    stations=[station],
                    confidence="high",
                    caveats=["Flagged values may be sensor faults; check calibration before use."],
                )
            )
    findings = findings[:2]
    by_ward: dict[str, list[StationAQI]] = {}
    for s in station_aqi:
        if s.aqi is None or not s.ward:
            continue
        by_ward.setdefault(s.ward, []).append(s)
    for ward in sorted(by_ward):
        group = sorted(by_ward[ward], key=lambda s: (-s.aqi, s.station))
        if len(group) < 2:
            continue
        high, low = group[0], group[-1]
        if low.aqi is None or low.aqi <= 0:
            continue
        ratio = high.aqi / low.aqi
        if ratio < DISAGREE_RATIO:
            continue
        findings.append(
            Finding(
                id="",
                type="anomaly",
                title=f"Monitors in {ward} disagree",
                facts=_facts(
                    {
                        "ward": ward,
                        "ratio": ratio,
                        "max_aqi": high.aqi,
                        "min_aqi": low.aqi,
                        "n_stations": len(group),
                        "max_station": high.station,
                        "min_station": low.station,
                    }
                ),
                formula=(
                    "Latest valid AQI of the highest and lowest monitor in the same ward; "
                    "a ratio above 1.4 is reported as disagreement."
                ),
                evidence_row_ids=_evidence(
                    _time_sorted(
                        pd.concat(
                            [
                                _station_day(tidy, s.station, s.latest_date)
                                for s in (high, low)
                            ]
                        )
                    )
                ),
                stations=[high.station, low.station],
                confidence="medium",
                caveats=[
                    "Co-located monitors more than 40% apart; check siting and calibration "
                    "before ranking them."
                ],
            )
        )
    return findings[:MAX_ANOMALIES]


# ---------------------------------------------------------------- brief
def _rank(findings: list[Finding]) -> list[Finding]:
    for index, finding in enumerate(findings, start=1):
        finding.id = f"F{index}"
    return findings


def _period(tidy: pd.DataFrame) -> tuple[str | None, str | None]:
    if tidy.empty or "ts" not in tidy.columns:
        return None, None
    ts = tidy["ts"].dropna()
    if ts.empty:
        return None, None
    start = pd.Timestamp(ts.min()).strftime("%Y-%m-%d")
    end = pd.Timestamp(ts.max()).strftime("%Y-%m-%d")
    return start, end


def build_brief(
    tidy: pd.DataFrame,
    quality: QualitySummary,
    area: str,
    source_name: str,
    source_hash: str,
) -> Brief:
    """Compute AQI, trends, hotspots and patterns into a ranked Brief."""
    if tidy is None or tidy.empty:
        daily, avgs = _empty_daily(), _empty_avgs()
        tidy = pd.DataFrame(columns=["row_id", "station", "ward", "ts", "pollutant", "value_std", "flags"])
    else:
        daily = daily_aqi(tidy)
        avgs = pollutant_averages(tidy)

    rel: dict[str, StationReliability] = {s.station: s for s in quality.stations}
    station_aqi = _station_table(tidy, daily, rel)

    findings: list[Finding] = []
    findings += _hotspots(station_aqi, tidy, rel)
    findings += _exceedances(avgs, tidy, rel)
    findings += _trends(station_aqi, daily, tidy)
    findings += _patterns(tidy, station_aqi, rel)
    findings += _gaps(quality, tidy, station_aqi)
    findings += _anomalies(tidy, station_aqi)
    _backfill_evidence(findings, tidy)
    _rank(findings)

    now = datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="seconds")
    brief_id = f"B{now.strftime('%Y%m%dT%H%M%SZ')}-{source_hash[:6] or 'nohash'}"
    period_start, period_end = _period(tidy)
    return Brief(
        brief_id=brief_id,
        created_at=created_at,
        area=area,
        source_name=source_name,
        source_hash=source_hash,
        period_start=period_start,
        period_end=period_end,
        station_aqi=station_aqi,
        findings=findings,
        quality=quality,
    )
