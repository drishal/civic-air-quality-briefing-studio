"""Data-quality flags, station reliability and the quality summary.

`flag_readings` adds STUCK and SPIKE flags to the tidy table. `station_reliability`
scores each station as coverage x (1 - flagged share), then lowers the score for a day
that died while other days worked and caps it when a stuck/spiking sensor is present.
`quality_summary` writes the plain-language gap statements and the honest usable share
the analyst and the brief need.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.ingest import RawTable
from core.models import FLAGS, QualitySummary, StationReliability

STUCK_RUN = 6            # identical consecutive readings that count as a stuck sensor
SPIKE_WINDOW = 6         # readings in the rolling median window
SPIKE_MULTIPLE = 5.0     # spike when value > 5 x rolling median
PM_SPIKE_OFFSET = 150.0  # ... or median + 150 µg/m³ for particulate matter
MIN_SPIKE_HISTORY = 3    # need this many earlier valid readings before judging a spike
SPIKE_MAX_GAP_HOURS = 2  # a longer hole resets the window: old readings are no baseline
PARTIAL_DAY_HOURS = 16   # fewer valid hours than this is a partial day
GAP_MIN_HOURS = 3        # shorter holes are noise, not gaps
# Flags that make a reading unusable or its pattern untrustworthy. UNIT_AMBIGUOUS and
# UNIT_CONVERTED are unit notes (FLAGS calls UNIT_CONVERTED informational): they are
# reported in the notes and the cleaning log, not multiplied into the reliability score.
QUALITY_FLAGS = frozenset(
    {
        "MISSING", "SENTINEL", "NEGATIVE", "OUT_OF_RANGE", "UNIT_MISMATCH", "CENSORED",
        "STUCK", "SPIKE", "DUPLICATE", "TIMESTAMP_UNPARSEABLE",
    }
)
# Flags that make a reading unusable for the headline "usable %": a missing / sentinel /
# rejected / duplicated value, or a pattern fault (stuck, spike) that voids the reading.
# CENSORED keeps the stated limit and unit notes are informational, so neither counts.
USABILITY_FLAGS = frozenset(
    {
        "MISSING", "SENTINEL", "NEGATIVE", "OUT_OF_RANGE", "UNIT_MISMATCH",
        "STUCK", "SPIKE", "DUPLICATE", "TIMESTAMP_UNPARSEABLE",
    }
)
INFO_FLAGS = ("UNIT_AMBIGUOUS", "UNIT_CONVERTED")  # informational: counted, never "unusable"

# Reliability tuning. A day that cannot produce a daily AQI (too few hours) while other
# days at the same station can is evidence of a sensor failure, not just sparse data, so
# it lowers the score. A stuck or spiking sensor caps the score below "reliable".
FAULT_DAY_WEIGHT = 0.5
PATTERN_FAULT_CAP = 0.7
PATTERN_FAULT_MIN_SPIKES = 3
MAX_GAP_LINES = 40


def _add(flags: list[str], flag: str) -> None:
    if flag in FLAGS and flag not in flags:
        flags.append(flag)


def _usable(value: object) -> bool:
    try:
        return value is not None and np.isfinite(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _same(a: object, b: object) -> bool:
    if not (_usable(a) and _usable(b)):
        return False
    return float(a) == float(b)  # type: ignore[arg-type]


def _as_flag_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(flag) for flag in value]
    return []


def flag_readings(tidy: pd.DataFrame) -> pd.DataFrame:
    """Add STUCK and SPIKE flags; existing flags are kept."""
    frame = tidy.copy(deep=True)
    if frame.empty:
        if "flags" not in frame.columns:
            frame["flags"] = pd.Series(dtype=object)
        return frame
    if "flags" not in frame.columns:
        frame["flags"] = pd.Series([[] for _ in range(len(frame))], index=frame.index, dtype=object)
    else:
        frame["flags"] = pd.Series(
            [_as_flag_list(value) for value in frame["flags"].tolist()], index=frame.index, dtype=object
        )
    for column in ("ts", "value_std", "pollutant", "station"):
        if column not in frame.columns:
            return frame

    frame = frame.reset_index(drop=True)
    for (_, _), group in frame.groupby(["station", "pollutant"], sort=False):
        ordered = group[group["ts"].notna()].sort_values("ts", kind="mergesort").index.tolist()
        if not ordered:
            continue
        values = [frame.at[i, "value_std"] for i in ordered]
        _mark_stuck(frame, ordered, values)
        _mark_spikes(
            frame, ordered, values, str(group["pollutant"].iloc[0]), [frame.at[i, "ts"] for i in ordered]
        )
    return frame


def _mark_stuck(frame: pd.DataFrame, rows: list[int], values: list[object]) -> None:
    start = 0
    for position in range(1, len(values) + 1):
        continues = position < len(values) and _same(values[position], values[start])
        if continues:
            continue
        if position - start >= STUCK_RUN and _usable(values[start]):
            for k in range(start, position):
                _add(frame.at[rows[k], "flags"], "STUCK")
        start = position


def _mark_spikes(
    frame: pd.DataFrame, rows: list[int], values: list[object], pollutant: str, times: list[object]
) -> None:
    window: list[float] = []
    previous = None
    for position, row in enumerate(rows):
        stamp = times[position] if position < len(times) else None
        if (
            previous is not None
            and stamp is not None
            and pd.notna(stamp)
            and pd.Timestamp(stamp) - pd.Timestamp(previous) > pd.Timedelta(hours=SPIKE_MAX_GAP_HOURS)
        ):
            window.clear()  # readings before an outage are no baseline for the first one back
        value = values[position]
        if _usable(value):
            if len(window) >= MIN_SPIKE_HISTORY:
                median = float(np.median(window))
                if median > 0:  # a zero median would flag every morning rise
                    threshold = SPIKE_MULTIPLE * median
                    if pollutant in ("PM2.5", "PM10"):
                        threshold = max(threshold, median + PM_SPIKE_OFFSET)
                    if float(value) > threshold:  # type: ignore[arg-type]
                        _add(frame.at[row, "flags"], "SPIKE")
            window.append(float(value))  # type: ignore[arg-type]
            if len(window) > SPIKE_WINDOW:
                window.pop(0)
        if stamp is not None and pd.notna(stamp):
            previous = stamp


def _label(reliability: float) -> str:
    if reliability >= 0.75:
        return "reliable"
    if reliability >= 0.5:
        return "use with caution"
    return "unreliable"


def _valid_hours_per_day(group: pd.DataFrame) -> pd.Series:
    """Distinct hours with a valid value, per calendar day of one station."""
    if "ts" not in group.columns or "value_std" not in group.columns:
        return pd.Series(dtype="int64")
    usable = group[group["value_std"].notna() & group["ts"].notna()]
    if usable.empty:
        return pd.Series(dtype="int64")
    days = usable["ts"].dt.date
    return usable.groupby(days)["ts"].nunique()


def station_reliability(tidy: pd.DataFrame) -> list[StationReliability]:
    """Coverage x (1 - flagged share) for every station present in the tidy table."""
    if tidy.empty or "station" not in tidy.columns:
        return []
    results: list[StationReliability] = []
    for station, group in tidy.groupby("station", sort=True):
        n_total = len(group)
        n_valid = int(group["value_std"].notna().sum()) if "value_std" in group else 0
        pollutants = sorted({str(p) for p in group["pollutant"].dropna().unique()}) if "pollutant" in group else []
        stamps = group["ts"].dropna() if "ts" in group else pd.Series(dtype="datetime64[ns]")
        span_hours = 1
        if len(stamps):
            try:
                span_hours = int((stamps.max() - stamps.min()).total_seconds() // 3600) + 1
            except (TypeError, ValueError):
                span_hours = 1
        expected = max(span_hours, 1) * max(len(pollutants), 1)
        coverage = min(n_valid / expected, 1.0) if expected else 0.0
        flags_per_row = group["flags"].tolist() if "flags" in group else []
        flag_lists = [_as_flag_list(flags) for flags in flags_per_row]
        flagged = sum(1 for flags in flag_lists if any(flag in QUALITY_FLAGS for flag in flags))
        flagged_share = flagged / n_total if n_total else 0.0
        reliability = coverage * (1.0 - flagged_share)

        # A day that cannot produce a daily AQI while other days at this station can is a
        # sensor outage, so it costs more than the missing hours already counted.
        day_hours = _valid_hours_per_day(group)
        fault_days = 0
        if len(day_hours) >= 2 and int(day_hours.max()) >= PARTIAL_DAY_HOURS:
            fault_days = int((day_hours < PARTIAL_DAY_HOURS).sum())
            if fault_days:
                reliability *= 1.0 - FAULT_DAY_WEIGHT * (fault_days / len(day_hours))

        # A stuck or spiking sensor cannot be called reliable, however good the coverage.
        n_stuck = sum(1 for flags in flag_lists if "STUCK" in flags)
        n_spike = sum(1 for flags in flag_lists if "SPIKE" in flags)
        pattern_fault = n_stuck >= STUCK_RUN or n_spike >= PATTERN_FAULT_MIN_SPIKES
        cap_applied = pattern_fault and reliability >= PATTERN_FAULT_CAP
        if cap_applied:
            reliability = PATTERN_FAULT_CAP
        notes: list[str] = []
        if n_valid == 0:
            notes.append("No usable readings at all.")
        elif coverage < 0.75:
            notes.append(f"Only {coverage:.0%} of the expected hourly readings are present.")
        if span_hours < 24:
            notes.append(f"Only {span_hours} hour(s) of data, so a daily average cannot be trusted.")
        counts: dict[str, int] = {}
        for flags in flags_per_row:
            for flag in _as_flag_list(flags):
                counts[flag] = counts.get(flag, 0) + 1
        for flag, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]:
            notes.append(f"{count} reading(s) flagged {flag}.")
        if fault_days:
            notes.append(
                f"{fault_days} of {len(day_hours)} days had fewer than {PARTIAL_DAY_HOURS} "
                "valid hours, so no daily AQI could be computed for it."
            )
        if cap_applied:
            notes.append(
                f"{n_stuck} stuck and {n_spike} spike reading(s): a sensor fault caps this "
                f"station at {PATTERN_FAULT_CAP:.2f} (use with caution)."
            )
        if counts.get("UNIT_AMBIGUOUS", 0) > n_total * 0.1:
            notes.append("Units were missing for a large share of readings, so treat values as approximate.")
        if not notes:
            notes.append("Full hourly coverage with no quality flags.")

        results.append(
            StationReliability(
                station=str(station),
                n_readings=n_total,
                coverage=round(coverage, 4),
                flagged_share=round(flagged_share, 4),
                reliability=round(reliability, 4),
                label=_label(reliability),  # type: ignore[arg-type]
                notes=notes,
            )
        )
    return results


def _runs(times: list[pd.Timestamp]) -> list[list[pd.Timestamp]]:
    """Group consecutive hourly timestamps."""
    runs: list[list[pd.Timestamp]] = []
    current: list[pd.Timestamp] = []
    for stamp in times:
        if current and stamp - current[-1] != pd.Timedelta(hours=1):
            runs.append(current)
            current = []
        current.append(stamp)
    if current:
        runs.append(current)
    return runs


def _gap_statements(station: str, missing: list[pd.Timestamp]) -> list[str]:
    statements: list[str] = []
    for run in _runs(missing):
        if len(run) < GAP_MIN_HOURS:
            continue
        first = run[0]
        last = run[-1] + pd.Timedelta(hours=1)
        if first.date() == last.date():
            statements.append(
                f"Station {station} has no readings between {first:%H:%M} and {last:%H:%M} on {first:%Y-%m-%d}."
            )
        else:
            statements.append(
                f"Station {station} has no readings between {first:%Y-%m-%d %H:%M} and {last:%Y-%m-%d %H:%M}."
            )
    return statements


def _statements(tidy: pd.DataFrame) -> list[str]:
    """Plain-language gaps: outages, missing pollutants and partial days."""
    if tidy.empty or "station" not in tidy.columns:
        return ["No readings were loaded."]
    hourly = tidy[tidy["ts"].notna()] if "ts" in tidy.columns else tidy.iloc[0:0]
    if hourly.empty:
        return ["No reading carries a usable timestamp."]
    usable_rows = hourly[hourly["value_std"].notna()] if "value_std" in hourly else hourly.iloc[0:0]
    everywhere = {str(p) for p in usable_rows["pollutant"].dropna().unique()}
    statements: list[str] = []
    partial: list[str] = []
    groups = []
    all_hours: list[pd.Timestamp] = []
    for station, group in hourly.groupby("station", sort=True):
        usable = group["value_std"].notna() if "value_std" in group else pd.Series(False, index=group.index)
        observed = sorted({stamp.floor("h") for stamp in group.loc[usable, "ts"]})
        groups.append((str(station), group, usable, observed))
        all_hours.extend(observed)
    if not all_hours:
        return ["No station has a usable reading to line up on a clock."]
    # Judge every station over the file's own period, so a station that stops early
    # (or starts late) is reported instead of hidden by its own short span.
    start, end = min(all_hours), max(all_hours)
    for station, group, usable, observed in groups:
        if not observed:
            statements.append(f"Station {station} has no usable readings.")
            continue
        expected = pd.date_range(start, end, freq="h")
        seen = set(observed)
        missing = [stamp for stamp in expected if stamp not in seen]
        statements.extend(_gap_statements(station, missing))

        have = {str(p) for p in group.loc[usable, "pollutant"].dropna().unique()}
        absent = sorted(everywhere - have)
        if absent:
            statements.append(f"Station {station} has no {' or '.join(absent)} readings.")

        valid = group.loc[usable]
        per_day = valid.assign(day=valid["ts"].dt.date).groupby("day")["ts"].nunique()
        for day, hours in per_day.items():
            if hours < PARTIAL_DAY_HOURS:
                partial.append(f"Station {station} has only {int(hours)} valid hour(s) on {day} (partial day).")
    statements.extend(partial[:20])
    if len(statements) > MAX_GAP_LINES:
        extra = len(statements) - MAX_GAP_LINES
        statements = statements[:MAX_GAP_LINES] + [f"... and {extra} more coverage note(s)."]
    return statements


def _count_usable(tidy: pd.DataFrame) -> int:
    """Readings that are neither unusable nor pattern-faulted (unit notes do not count)."""
    if tidy is None or tidy.empty or "value_std" not in tidy.columns:
        return 0
    usable = tidy["value_std"].notna()
    if "flags" in tidy.columns:
        blocked = tidy["flags"].apply(
            lambda flags: any(flag in USABILITY_FLAGS for flag in _as_flag_list(flags))
        )
        usable = usable & ~blocked
    return int(usable.sum())


def quality_summary(
    raw: RawTable, tidy: pd.DataFrame, cleaning_log: list[str]
) -> QualitySummary:
    """Assemble the quality report for a cleaned table."""
    n_rows_in = int(len(raw.df)) if raw is not None and raw.df is not None else 0
    n_readings = int(len(tidy)) if tidy is not None else 0
    flag_counts: dict[str, int] = {}
    if tidy is not None and "flags" in tidy.columns:
        for flags in tidy["flags"].tolist():
            for flag in _as_flag_list(flags):
                flag_counts[flag] = flag_counts.get(flag, 0) + 1
    flag_counts = dict(sorted(flag_counts.items(), key=lambda item: (-item[1], item[0])))
    info_flag_counts = {flag: flag_counts[flag] for flag in INFO_FLAGS if flag in flag_counts}
    n_usable = _count_usable(tidy)
    usable_share = (n_usable / n_readings) if n_readings else 0.0
    stations = station_reliability(tidy) if tidy is not None else []
    gaps = _statements(tidy) if tidy is not None else ["No readings were loaded."]
    return QualitySummary(
        n_rows_in=n_rows_in,
        n_readings=n_readings,
        flag_counts=flag_counts,
        info_flag_counts=info_flag_counts,
        n_usable=n_usable,
        usable_share=round(usable_share, 4),
        stations=stations,
        gaps=gaps,
        cleaning_log=list(cleaning_log or []),
    )


__all__ = [
    "INFO_FLAGS",
    "QUALITY_FLAGS",
    "USABILITY_FLAGS",
    "flag_readings",
    "quality_summary",
    "station_reliability",
]
