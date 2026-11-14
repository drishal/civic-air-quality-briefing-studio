"""Apply a column mapping to a raw table and produce the tidy reading table.

The tidy table is the contract in CONTRACT.md: one row per reading with
`row_id`, `station`, `ward`, `ts`, `pollutant`, `value_raw`, `unit_raw`,
`value_std` (µg/m³, mg/m³ for CO) and `flags`. Bad cells never raise: they are
flagged and logged. A table with nothing usable raises ValueError.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from core.ingest import RawTable, canonical_pollutant, canonical_unit
from core.models import FLAGS, POLLUTANTS, ColumnMapping, ColumnRole

# Physically implausible concentrations, in the standard unit (µg/m³, CO in mg/m³).
MAX_VALUE: dict[str, float] = {
    "PM2.5": 1500.0,
    "PM10": 2500.0,
    "NO2": 2000.0,
    "SO2": 2000.0,
    "O3": 1000.0,
    "CO": 100.0,
    "NH3": 5000.0,
}

# ppb -> µg/m³ at 25 °C, 1 atm (CO is handled separately: 1 ppm = 1.146 mg/m³).
PPB_TO_UG: dict[str, float] = {"NO2": 1.88, "O3": 1.96, "SO2": 2.62, "NH3": 0.70}
PPM_TO_MG_CO = 1.146
PPB_TO_MG_CO = PPM_TO_MG_CO / 1000.0

SENTINEL_TOKENS: frozenset[str] = frozenset(
    {
        "", "na", "n/a", "n.a", "n.a.", "nan", "null", "none", "nil", "--", "-", "---",
        "nd", "n.d.", "n.d", "bdl", "b.d.l", "b.d.l.", "bdl*", "?", "??", "missing",
        "not available", "no data", "#n/a", "*", "**", "-999", "-9999", "-99999",
        "9999", "99999", "999999", "-9999.0", "-999.0", "9999.0",
    }
)
SENTINEL_NUMBERS: frozenset[float] = frozenset({-999.0, -9999.0, -99999.0, 9_999.0, 99_999.0, 999_999.0})

_UNIT_TOKEN = re.compile(
    r"(?:µg|μg|ug|mg|ng)\s*[/·]\s*m\s*\^?\s*[3³]"
    r"|(?:ug|mg|ng)\s*m\s*-?\s*[3³]"
    r"|(?<![0-9a-z])p?pp[bmv]+(?![0-9a-z])"
    r"|nmol\s*/\s*mol",
    re.IGNORECASE,
)
_CENSOR = re.compile(r"^([<>]=?)\s*(.+)$")

_TS_COMMON = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d",
    "%Y%m%d %H:%M", "%Y%m%d%H%M", "%Y%m%d",
    "%d %b %Y %H:%M:%S", "%d %b %Y %H:%M", "%d %b %Y %I:%M %p", "%d %b %Y",
    "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d %B %Y %H:%M", "%d %B %Y",
    "%b %d, %Y %H:%M", "%b %d, %Y %I:%M %p", "%b %d %Y %H:%M", "%B %d, %Y %H:%M",
)
_TS_DAY_FIRST = (
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M %p",
    "%d/%m/%Y %I:%M%p", "%d/%m/%Y", "%d/%m/%y %H:%M", "%d/%m/%y %I:%M %p", "%d/%m/%y",
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%d-%m-%y %H:%M",
)
_TS_MONTH_FIRST = (
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %I:%M%p", "%m/%d/%Y", "%m/%d/%y %H:%M", "%m/%d/%y %I:%M %p", "%m/%d/%y",
)

_WARD_NUMBER = re.compile(
    r"^(?:w|wd|wrd|ward|ward\s*no\.?|ward\s*number|ward\s*#)\s*[-_.#:\s]*\s*(\d+)\s*([a-zA-Z])?$",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]")


# --------------------------------------------------------------------------- value cells
@dataclass
class ParsedValue:
    """One raw cell, split into number, unit and censoring marker."""

    value: float | None = None
    alt: float | None = None          # second reading when the separators are ambiguous
    unit: str | None = None           # canonical unit key, e.g. "ug/m3"
    unit_text: str | None = None      # the unit exactly as written in the cell
    censored: str | None = None
    kind: str = "ok"                  # ok | blank | sentinel | bad


def _numbers(text: str) -> tuple[float | None, float | None] | None:
    """Read one number that may use '.' or ',' as decimal or thousands separator."""
    sign = 1.0
    if text[:1] in "+-−":
        if text[0] in "-−":
            sign = -1.0
        text = text[1:]
    exponent = ""
    match = re.search(r"[eE][+-]?\d+$", text)
    if match:
        exponent = match.group(0)
        text = text[: match.start()]
    if not text or not re.fullmatch(r"[\d.,]+", text):
        return None

    def finish(digits: str) -> float | None:
        try:
            return sign * float(digits + exponent)
        except ValueError:
            return None

    has_comma = "," in text
    has_dot = "." in text
    if has_comma and has_dot:
        if text.rfind(",") > text.rfind("."):     # 1.250,5 -> decimal comma
            return finish(text.replace(".", "").replace(",", ".")), None
        return finish(text.replace(",", "")), None  # 1,250.5 -> thousands comma
    if not has_comma and not has_dot:
        return finish(text), None
    sep = "," if has_comma else "."
    parts = text.split(sep)
    if len(parts) > 2:                              # 1,250,000 -> thousands
        return finish("".join(parts)), None
    head, tail = parts
    if len(tail) == 3 and 1 <= len(head) <= 3 and not (head.startswith("0") and len(head) > 1):
        thousands = finish(head + tail)
        decimal = finish(head + "." + tail)
        if sep == ",":
            return thousands, decimal
        return decimal, thousands
    return finish(head + "." + tail), None


def parse_value_cell(cell: object) -> ParsedValue:
    """Read a value cell: "85 µg/m3", "<5", "1,250", "NA", "  "."""
    text = "" if cell is None else str(cell).strip()
    if not text:
        return ParsedValue(kind="blank")
    text = text.replace("\u00a0", " ").replace("\u2212", "-")

    unit = None
    unit_text = None
    match = _UNIT_TOKEN.search(text)
    if match:
        unit_text = match.group(0).strip()
        unit = canonical_unit(unit_text)
        text = (text[: match.start()] + " " + text[match.end():]).strip()

    censored = None
    match = _CENSOR.match(text)
    if match:
        censored = match.group(1)
        text = match.group(2).strip()

    text = re.sub(r"\([^)]*\)", " ", text).strip().strip("*").strip()
    lowered = text.lower()
    if lowered in SENTINEL_TOKENS or lowered.replace(" ", "") in SENTINEL_TOKENS:
        return ParsedValue(None, None, unit, unit_text, censored, "sentinel")
    if not text:
        return ParsedValue(kind="blank")

    numbers = _numbers(text)
    if numbers is None:
        # Trailing notes like "85 est" or "~85": take the first number-looking token.
        loose = re.search(r"[+-]?\d[\d.,]*(?:[eE][+-]?\d+)?", text)
        numbers = _numbers(loose.group(0)) if loose else None
    if numbers is None:
        return ParsedValue(None, None, unit, unit_text, censored, "bad")
    value, alt = numbers
    if value is not None and value in SENTINEL_NUMBERS:
        return ParsedValue(None, None, unit, unit_text, censored, "sentinel")
    if censored:
        return ParsedValue(value, alt, unit, unit_text, censored, "ok")
    return ParsedValue(value, alt, unit, unit_text, None, "ok")


def _std_unit_key(pollutant: str) -> str:
    return "mg/m3" if pollutant == "CO" else "ug/m3"


def convert_value(
    value: float | None, unit: str | None, pollutant: str
) -> tuple[float | None, list[str]]:
    """Convert a reading to the standard unit; return (value, flags)."""
    flags: list[str] = []
    if value is None:
        return None, flags
    key = canonical_unit(unit) if unit else None
    if key is None:
        key = _std_unit_key(pollutant)
        flags.append("UNIT_AMBIGUOUS")
    converted = value
    if key == "ng/m3":
        converted /= 1000.0
        key = "ug/m3"
        flags.append("UNIT_CONVERTED")
    if key == "ppb":
        if pollutant in ("PM2.5", "PM10"):
            return None, ["UNIT_MISMATCH"]
        if pollutant == "CO":
            converted *= PPB_TO_MG_CO
            key = "mg/m3"
        else:
            converted *= PPB_TO_UG[pollutant]
            key = "ug/m3"
        flags.append("UNIT_CONVERTED")
    elif key == "ppm":
        if pollutant in ("PM2.5", "PM10"):
            return None, ["UNIT_MISMATCH"]
        if pollutant == "CO":
            converted *= PPM_TO_MG_CO
            key = "mg/m3"
        else:
            converted *= PPB_TO_UG[pollutant] * 1000.0
            key = "ug/m3"
        flags.append("UNIT_CONVERTED")
    elif key == "mg/m3" and pollutant != "CO":
        converted *= 1000.0
        key = "ug/m3"
        flags.append("UNIT_CONVERTED")
    elif key == "ug/m3" and pollutant == "CO":
        converted /= 1000.0
        key = "mg/m3"
        flags.append("UNIT_CONVERTED")
    return converted, flags


def _range_flags(value: float | None, pollutant: str) -> list[str]:
    """NEGATIVE / OUT_OF_RANGE flags for a value already in the standard unit."""
    if value is None or not np.isfinite(value):
        return []
    if value < 0:
        return ["NEGATIVE"]
    if value > MAX_VALUE[pollutant]:
        return ["OUT_OF_RANGE"]
    return []


# --------------------------------------------------------------------------- timestamps
def _clean_timestamp_text(value: object) -> tuple[str, bool]:
    """Strip timezone labels, normalise 12 h markers and repair '24:00'."""
    text = "" if value is None else str(value).strip()
    repaired = False
    if not text:
        return "", False
    text = text.replace("\u00a0", " ").replace("\u2212", "-")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\b(IST|UTC|GMT|LOCAL)\b", " ", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"(?<=\d)(Z|[+-]\d{2}:?\d{2})$", "", text).strip()
    text = re.sub(r"\b([ap])\.?\s*m\.?", lambda m: m.group(1).upper() + "M", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)([AP]M)\b", r" \1", text)
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d", text):
        text = text.replace("T", " ", 1)
    if "24:00" in text:
        text = text.replace("24:00", "00:00")
        try:
            base = _parse_timestamp_text(text, day_first=True)
            if base is not None:
                text = (base + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
                repaired = True
        except (ValueError, OverflowError):
            repaired = False
    return text, repaired


def _parse_timestamp_text(text: str, day_first: bool) -> datetime | None:
    """Parse one timestamp string with the known formats."""
    if not text:
        return None
    day_fmt = _TS_DAY_FIRST if day_first else _TS_MONTH_FIRST
    other_fmt = _TS_MONTH_FIRST if day_first else _TS_DAY_FIRST
    for fmt in _TS_COMMON + day_fmt + other_fmt:
        try:
            return datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
    try:
        result = pd.to_datetime(text, dayfirst=day_first, errors="coerce")
    except (ValueError, TypeError, OverflowError):
        return None
    if result is None or result is pd.NaT or (isinstance(result, float) and np.isnan(result)):
        return None
    if hasattr(result, "to_pydatetime"):
        try:
            return result.to_pydatetime()
        except (ValueError, OverflowError):
            return None
    return None


def _infer_day_first(values: list[str]) -> tuple[bool, str]:
    """DD/MM vs MM/DD: any first component > 12 decides; else assume DD/MM."""
    day_votes = month_votes = 0
    for value in values:
        match = re.match(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", str(value))
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(2))
        if first > 12:
            day_votes += 1
        elif second > 12:
            month_votes += 1
    if day_votes and month_votes:
        return True, "The file mixes DD/MM and MM/DD dates; read ambiguous dates as DD/MM."
    if month_votes and not day_votes:
        return False, "Dates look like MM/DD/YYYY (a month above 12 appears in the second position)."
    return True, "Dates read as DD/MM/YYYY (no day above 12 disagrees)."


def parse_timestamp(value: object, day_first: bool = True) -> tuple[pd.Timestamp | None, bool]:
    """Parse one timestamp; returns (timestamp, was_repaired). Never raises."""
    try:
        text, repaired = _clean_timestamp_text(value)
        parsed = _parse_timestamp_text(text, day_first)
    except Exception:  # noqa: BLE001 - a weird cell must not stop the run
        return None, False
    if parsed is None:
        return None, repaired
    try:
        return pd.Timestamp(parsed), repaired
    except (ValueError, OverflowError):
        return None, repaired


# --------------------------------------------------------------------------- names
def _tidy_name(value: object) -> str:
    text = "" if value is None else str(value).strip()
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" .,;:|\"'")
    if not text:
        return ""
    if text.isupper() or text.islower():
        text = text.title()
    return text


def canonical_ward(value: object) -> str | None:
    """Canonical ward name: "ward-12", "W12", "Ward No. 12" -> "Ward 12"."""
    text = re.sub(r"\s+", " ", "" if value is None else str(value).strip())
    if not text:
        return None
    match = _WARD_NUMBER.match(text)
    if match:
        return f"Ward {match.group(1)}{(match.group(2) or '').upper()}"
    return _tidy_name(text) or None


def _name_key(value: str) -> str:
    return _NON_ALNUM.sub("", value.lower())


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def canonical_station_map(values: list[str], log: list[str]) -> dict[str, str]:
    """Map every station spelling to one canonical name (fixes typos and case)."""
    counts: dict[str, int] = {}
    raw_to_tidy: dict[str, str] = {}
    for value in values:
        raw = "" if value is None else str(value)
        tidy = _tidy_name(raw) or "Unknown station"
        raw_to_tidy[raw] = tidy
        counts[tidy] = counts.get(tidy, 0) + 1

    ordered = sorted(counts, key=lambda name: (-counts[name], name))
    groups: list[tuple[str, str, str]] = []          # (canonical, normalized key, digits)
    mapping: dict[str, str] = {}
    for name in ordered:
        key = _name_key(name)
        digits = _digits(name)
        target = None
        for canonical, other_key, other_digits in groups:
            if key == other_key and digits == other_digits:
                target = canonical
                break
            if len(key) < 5 or len(other_key) < 5 or digits != other_digits:
                continue
            if _similar(key, other_key):
                target = canonical
                break
        if target is None:
            groups.append((name, key, digits))
            target = name
        elif target != name:
            log.append(f"Merged station name '{name}' into '{target}' ({counts[name]} reading(s)).")
        mapping[name] = target
    return {raw: mapping[tidy] for raw, tidy in raw_to_tidy.items()}


def _similar(a: str, b: str) -> bool:
    """Cheap typo test: a shared prefix/suffix or a one/two character difference."""
    import difflib

    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.88


# --------------------------------------------------------------------------- mapping helpers
def _pick(mapping: ColumnMapping, role: str, present: set[str]) -> ColumnRole | None:
    for column in mapping.columns:
        if column.role == role and column.column in present:
            return column
    return None


def _add(flags: list[str], new: str) -> None:
    if new in FLAGS and new not in flags:
        flags.append(new)


# --------------------------------------------------------------------------- apply_mapping
def apply_mapping(raw: RawTable, mapping: ColumnMapping) -> tuple[pd.DataFrame, list[str]]:
    """Turn a RawTable plus a ColumnMapping into the tidy reading table.

    Returns (tidy DataFrame, cleaning log). Raises ValueError if nothing usable
    can be extracted.
    """
    log: list[str] = list(raw.notes)
    frame = raw.df
    if frame is None or len(frame) == 0:
        raise ValueError("There are no rows to clean. Upload a file with a header row and data.")
    present = {str(c) for c in frame.columns}
    for column in mapping.columns:
        if column.column not in present:
            log.append(f"The mapping refers to column '{column.column}', which is not in the file; skipped.")

    station_role = _pick(mapping, "station", present)
    ward_role = _pick(mapping, "ward", present)
    timestamp_role = _pick(mapping, "timestamp", present)
    date_role = _pick(mapping, "date", present)
    time_role = _pick(mapping, "time", present)
    pollutant_name_role = _pick(mapping, "pollutant_name", present)
    value_role = _pick(mapping, "value", present)
    unit_role = _pick(mapping, "unit", present)
    lat_role = _pick(mapping, "lat", present)
    lon_role = _pick(mapping, "lon", present)
    wide_columns = [
        column for column in mapping.columns
        if column.role == "pollutant_value" and column.column in present and column.pollutant in POLLUTANTS
    ]

    layout = "long" if (pollutant_name_role and value_role) else "wide"
    if layout == "wide" and not wide_columns:
        raise ValueError(
            "No pollutant columns could be identified. Check that headers name a pollutant "
            "(for example PM2.5, PM10, NO2, SO2, O3, CO, NH3), or use a long layout with "
            "pollutant name, value and unit columns."
        )

    def column_values(role: ColumnRole | None) -> list[str]:
        if role is None:
            return [""] * len(frame)
        return ["" if v is None else str(v) for v in frame[role.column].tolist()]

    station_raw = column_values(station_role)
    ward_raw = column_values(ward_role)
    lat_raw = column_values(lat_role)
    lon_raw = column_values(lon_role)
    src_rows = [int(v) for v in frame["_src_row"].tolist()]

    if timestamp_role is not None:
        stamp_raw = column_values(timestamp_role)
    elif date_role is not None:
        date_values = column_values(date_role)
        time_values = column_values(time_role)
        stamp_raw = [
            (f"{d} {t}".strip() if t.strip() and ":" not in d else d)
            for d, t in zip(date_values, time_values)
        ]
    elif time_role is not None:
        # A single "Time" column that actually holds full timestamps.
        stamp_raw = column_values(time_role)
    else:
        stamp_raw = [""] * len(frame)
        log.append("No timestamp column: every reading is kept without a time.")

    day_first, date_note = _infer_day_first(stamp_raw)
    log.append(date_note)
    stamp_cache: dict[str, tuple[pd.Timestamp | None, bool]] = {}
    stamps: list[pd.Timestamp | None] = []
    repaired = 0
    for value in stamp_raw:
        if value not in stamp_cache:
            stamp_cache[value] = parse_timestamp(value, day_first)
        parsed, was_repaired = stamp_cache[value]
        stamps.append(parsed)
        repaired += 1 if was_repaired else 0

    station_map = canonical_station_map(station_raw, log)
    ward_map: dict[str, str | None] = {}
    for value in ward_raw:
        if value not in ward_map:
            ward_map[value] = canonical_ward(value)

    def floats(values: list[str]) -> list[float]:
        out: list[float] = []
        for value in values:
            try:
                out.append(float(value))
            except (TypeError, ValueError):
                out.append(float("nan"))
        return out

    lats, lons = floats(lat_raw), floats(lon_raw)

    counters = {
        "rows_in": len(frame),
        "readings": 0,
        "blank": 0,
        "sentinel": 0,
        "bad": 0,
        "censored": 0,
        "negative": 0,
        "out_of_range": 0,
        "unit_ambiguous": 0,
        "unit_mismatch": 0,
        "unit_converted": 0,
        "unparsed_ts": 0,
        "duplicates": 0,
        "decimal_fix": 0,
        "skipped_unknown_pollutant": 0,
        "crashed_cells": 0,
    }

    records: list[dict[str, object]] = []
    value_cache: dict[str, ParsedValue] = {}

    def cached_parse(cell: str) -> ParsedValue:
        if cell not in value_cache:
            value_cache[cell] = parse_value_cell(cell)
        return value_cache[cell]

    def make_record(
        index: int,
        pollutant: str,
        cell: str,
        header_unit: str | None,
        row_unit: str | None,
    ) -> None:
        station = station_map.get(station_raw[index], "Unknown station") or "Unknown station"
        ward = ward_map.get(ward_raw[index])
        flags: list[str] = []
        unit_raw = row_unit or header_unit
        value_raw = cell.strip() or None
        value_std = float("nan")
        try:
            parsed = cached_parse(cell)
            unit_raw = parsed.unit_text or unit_raw
            if parsed.kind == "blank":
                _add(flags, "MISSING")
                counters["blank"] += 1
            elif parsed.kind == "sentinel":
                _add(flags, "SENTINEL")
                counters["sentinel"] += 1
            elif parsed.kind == "bad":
                _add(flags, "MISSING")
                counters["bad"] += 1
            else:
                effective_unit = parsed.unit or row_unit or header_unit
                converted, unit_flags = convert_value(parsed.value, effective_unit, pollutant)
                for flag in unit_flags:
                    _add(flags, flag)
                    if flag == "UNIT_AMBIGUOUS":
                        counters["unit_ambiguous"] += 1
                    elif flag == "UNIT_MISMATCH":
                        counters["unit_mismatch"] += 1
                    elif flag == "UNIT_CONVERTED":
                        counters["unit_converted"] += 1
                if parsed.alt is not None and converted is not None:
                    alternative, _ = convert_value(parsed.alt, effective_unit, pollutant)
                    if (
                        alternative is not None
                        and _range_flags(converted, pollutant)
                        and not _range_flags(alternative, pollutant)
                    ):
                        converted = alternative
                        counters["decimal_fix"] += 1
                if parsed.censored:
                    _add(flags, "CENSORED")
                    counters["censored"] += 1
                for flag in _range_flags(converted, pollutant):
                    _add(flags, flag)
                    counters["negative" if flag == "NEGATIVE" else "out_of_range"] += 1
                    converted = None
                if converted is not None:
                    value_std = float(converted)
        except Exception:  # noqa: BLE001 - one bad cell must not kill the run
            counters["crashed_cells"] += 1
            _add(flags, "MISSING")
        records.append(
            {
                "row_id": f"R{src_rows[index]}",
                "station": station,
                "ward": ward,
                "ts": stamps[index],
                "pollutant": pollutant,
                "value_raw": value_raw,
                "unit_raw": unit_raw,
                "value_std": value_std,
                "flags": flags,
                "lat": lats[index],
                "lon": lons[index],
            }
        )
        counters["readings"] += 1

    if layout == "wide":
        wide_series = {column.column: column_values(column) for column in wide_columns}
        row_units = column_values(unit_role)
        header_units: dict[str, str | None] = {}
        for column in wide_columns:
            cells = wide_series[column.column]
            states_units = any(cached_parse(cell).unit_text for cell in cells if cell.strip())
            if states_units:
                # Some cells state their own unit, so the header cannot be trusted for the
                # plain cells (headers are the least reliable part of these files).
                header_units[column.column] = None
                log.append(
                    f"Column '{column.column}' mixes cells that state a unit with plain numbers; "
                    "the header unit is ignored and plain cells are read in the standard unit."
                )
            else:
                header_units[column.column] = column.unit
        for i in range(len(frame)):
            for column in wide_columns:
                make_record(
                    i,
                    column.pollutant or "",
                    wide_series[column.column][i],
                    header_units[column.column],
                    row_units[i] or None,
                )
    else:
        assert pollutant_name_role is not None and value_role is not None
        name_values = column_values(pollutant_name_role)
        cell_values = column_values(value_role)
        unit_values = column_values(unit_role)
        unknown: set[str] = set()
        for i in range(len(frame)):
            pollutant = canonical_pollutant(name_values[i])
            if pollutant is None:
                counters["skipped_unknown_pollutant"] += 1
                if name_values[i].strip() and len(unknown) < 6:
                    unknown.add(name_values[i].strip())
                continue
            make_record(i, pollutant, cell_values[i], None, unit_values[i] or None)
        if unknown:
            log.append(f"Skipped rows with unrecognised pollutant names: {', '.join(sorted(unknown))}.")

    if not records:
        raise ValueError(
            "The table was read but no usable readings were found. Check that the pollutant "
            "columns hold numbers and that each row has a station and a time."
        )

    tidy = pd.DataFrame.from_records(records)
    tidy["ts"] = pd.to_datetime(tidy["ts"], errors="coerce").astype("datetime64[ns]")
    tidy["value_std"] = pd.to_numeric(tidy["value_std"], errors="coerce").astype(float)

    # Duplicate station+time+pollutant rows keep the first and flag the rest.
    seen: dict[tuple[str, object, str], str | None] = {}
    for i in range(len(tidy)):
        if pd.isna(tidy.at[i, "ts"]):
            continue
        key = (str(tidy.at[i, "station"]), tidy.at[i, "ts"], str(tidy.at[i, "pollutant"]))
        raw_value = tidy.at[i, "value_raw"]
        if key in seen and seen[key] == raw_value:
            _add(tidy.at[i, "flags"], "DUPLICATE")
            counters["duplicates"] += 1
        else:
            seen.setdefault(key, raw_value)

    bad_ts = int(tidy["ts"].isna().sum())
    if bad_ts:
        counters["unparsed_ts"] = bad_ts
        tidy.loc[tidy["ts"].isna(), "flags"] = tidy.loc[tidy["ts"].isna(), "flags"].apply(
            lambda flags: flags if "TIMESTAMP_UNPARSEABLE" in flags else flags + ["TIMESTAMP_UNPARSEABLE"]
        )
    if repaired:
        log.append(f"Repaired {repaired} timestamp(s) written with an hour of 24:00.")

    tidy = tidy.sort_values(["station", "pollutant", "ts"], na_position="last").reset_index(drop=True)
    columns = ["row_id", "station", "ward", "ts", "pollutant", "value_raw", "unit_raw", "value_std", "flags"]
    if tidy["lat"].notna().any():  # optional coordinates, only when the file has them
        columns += ["lat", "lon"]
    tidy = tidy[columns]
    log.extend(_summarise(counters, mapping, layout))
    return tidy, log




def _summarise(counters: dict[str, int], mapping: ColumnMapping, layout: str) -> list[str]:
    """Friendly one-line notes for the cleaning log."""
    log: list[str] = []
    log.append(
        f"Read {counters['readings']} reading(s) from {counters['rows_in']} data row(s) "
        f"({layout} layout)."
    )
    if counters["sentinel"]:
        log.append(f"{counters['sentinel']} sentinel code(s) (NA, --, -999, ND, BDL …) treated as missing.")
    if counters["blank"]:
        log.append(f"{counters['blank']} empty cell(s) treated as missing.")
    if counters["bad"]:
        log.append(f"{counters['bad']} cell(s) held no readable number and were treated as missing.")
    if counters["censored"]:
        log.append(f"{counters['censored']} censored value(s) like '<5' kept as the stated limit and flagged CENSORED.")
    if counters["unit_converted"]:
        log.append(f"{counters['unit_converted']} value(s) converted to the standard unit (µg/m³, CO in mg/m³).")
    if counters["unit_ambiguous"]:
        log.append(f"{counters['unit_ambiguous']} value(s) had no clear unit; the standard unit was assumed.")
    if counters["unit_mismatch"]:
        log.append(f"{counters['unit_mismatch']} value(s) used a gas unit (ppb/ppm) for particulate matter and were dropped.")
    if counters["negative"]:
        log.append(f"{counters['negative']} negative value(s) rejected.")
    if counters["out_of_range"]:
        log.append(f"{counters['out_of_range']} value(s) above the plausible maximum rejected.")
    if counters["decimal_fix"]:
        log.append(f"{counters['decimal_fix']} value(s) read with a thousands separator instead, because the other reading was impossible.")
    if counters["unparsed_ts"]:
        log.append(f"{counters['unparsed_ts']} timestamp(s) could not be read and are flagged TIMESTAMP_UNPARSEABLE.")
    if counters["duplicates"]:
        log.append(f"{counters['duplicates']} duplicate row(s) kept but flagged DUPLICATE.")
    if counters["skipped_unknown_pollutant"]:
        log.append(f"{counters['skipped_unknown_pollutant']} row(s) named a pollutant this app does not track; skipped.")
    if counters["crashed_cells"]:
        log.append(f"{counters['crashed_cells']} cell(s) hit an unexpected parsing problem and were kept as missing.")
    for issue in mapping.issues:
        if issue not in log:
            log.append(issue)
    return log


__all__ = [
    "MAX_VALUE",
    "PPB_TO_UG",
    "SENTINEL_TOKENS",
    "apply_mapping",
    "canonical_station_map",
    "canonical_ward",
    "convert_value",
    "parse_timestamp",
    "parse_value_cell",
]
