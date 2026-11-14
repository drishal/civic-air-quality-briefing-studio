"""Row-level evidence for the "Prove it" drawer.

`evidence_rows` returns the readings behind a finding exactly as they appeared in
the source file (`value_raw`, `unit_raw`) next to the cleaned value, plus a
`cleaning_step` sentence per row saying what cleaning did to it. That last column
is derived from the row's flags and from `value_raw` vs `value_std`; the tidy
table does not keep the original file, so this is the honest maximum. Date-format
inference (DD/MM vs MM/DD) happens before rows reach the tidy table and stays in the
cleaning log, so it cannot be attributed to a single row here.
"""
from __future__ import annotations

import re

import pandas as pd

from core.clean import MAX_VALUE, PPB_TO_MG_CO, PPB_TO_UG, PPM_TO_MG_CO
from core.models import STD_UNIT, Finding

MAX_ROWS = 50
EVIDENCE_COLUMNS = [
    "row_id",
    "station",
    "ts",
    "pollutant",
    "value_raw",
    "unit_raw",
    "value_std",
    "flags",
    "cleaning_step",
]
_THOUSANDS = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")

def _as_flags(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(flag) for flag in value]
    return []


def _unit_conversion(pollutant: str, unit_raw: object) -> str:
    """Readable unit-conversion step from the unit the source cell stated."""
    unit = str(unit_raw or "").lower().replace("³", "3").replace("μ", "u").replace("µ", "u")
    target = STD_UNIT.get(pollutant, "µg/m³")
    if "ppb" in unit:
        if pollutant == "CO":
            return f"ppb x{PPB_TO_MG_CO:.4g} -> {target}"
        factor = PPB_TO_UG.get(pollutant)
        if factor is not None:
            return f"ppb x{factor:g} -> {target}"
        return f"ppb -> {target}"
    if "ppm" in unit:
        if pollutant == "CO":
            return f"ppm x{PPM_TO_MG_CO:g} -> {target}"
        return f"ppm -> {target}"
    if "ug/m3" in unit and pollutant == "CO":
        return f"µg/m³ /1000 -> {target}"
    if "mg/m3" in unit and pollutant != "CO":
        return f"mg/m³ x1000 -> {target}"
    if "ng/m3" in unit:
        return f"ng/m³ /1000 -> {target}"
    return f"unit converted -> {target}"

def cleaning_step(
    value_raw: object,
    unit_raw: object,
    value_std: object,
    flags: object,
    pollutant: str = "",
) -> str:
    """One sentence: what cleaning did to this reading."""
    marks = _as_flags(flags)
    raw = "" if value_raw is None or pd.isna(value_raw) else str(value_raw).strip()
    steps: list[str] = []

    if "SENTINEL" in marks:
        steps.append(f"sentinel {raw or 'code'} -> missing")
    if "MISSING" in marks:
        steps.append("blank cell -> missing" if not raw else f"unreadable cell '{raw}' -> missing")
    if "NEGATIVE" in marks:
        steps.append(f"negative {raw or 'value'} -> rejected")
    if "OUT_OF_RANGE" in marks:
        limit = MAX_VALUE.get(pollutant)
        cap = f" (above {limit:g})" if limit else ""
        steps.append(f"impossible value {raw or 'value'}{cap} -> rejected")
    if "UNIT_MISMATCH" in marks:
        steps.append("gas unit used for particulate matter -> dropped")
    if "CENSORED" in marks:
        steps.append(f"censored '{raw}' kept as the stated limit")
    if "UNIT_CONVERTED" in marks:
        steps.append(_unit_conversion(pollutant, unit_raw))
    if "UNIT_AMBIGUOUS" in marks:
        steps.append("no unit stated -> standard unit assumed")
    if raw and "," in raw:
        if _THOUSANDS.fullmatch(raw.split()[0]):
            steps.append("thousands separator removed")
        else:
            steps.append("decimal comma read as a decimal point")
    if "TIMESTAMP_REPAIRED" in marks:
        steps.append("timestamp repaired (24:00 -> next day)")
    if "TIMESTAMP_UNPARSEABLE" in marks:
        steps.append("timestamp unreadable")
    if "DUPLICATE" in marks:
        steps.append("exact duplicate of an earlier row")
    if "STUCK" in marks:
        steps.append("stuck sensor: identical run flagged")
    if "SPIKE" in marks:
        steps.append("spike above the rolling median flagged")

    if not steps:
        if not marks:
            has_value = value_std is not None and not pd.isna(value_std)
            return "kept as-is" if has_value else "no value"
        return ", ".join(mark.lower() for mark in marks if mark != "CENSORED")
    return "; ".join(steps)


def _row_order(rows: pd.DataFrame) -> pd.DataFrame:
    """Original file order: row_id R<n> sorts numerically."""
    numbers = rows["row_id"].astype(str).str.extract(r"(\d+)", expand=False)
    ordered = rows.assign(_order=pd.to_numeric(numbers, errors="coerce"))
    return ordered.sort_values("_order", na_position="last", kind="stable").drop(columns="_order")


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=EVIDENCE_COLUMNS)


def evidence_rows(tidy: pd.DataFrame, finding: Finding) -> pd.DataFrame:
    """Rows behind a finding as they appeared in the source file, at most MAX_ROWS."""
    if tidy is None or tidy.empty or "row_id" not in tidy.columns:
        return _empty()

    selected = tidy.iloc[0:0]
    ids = [str(row_id) for row_id in (finding.evidence_row_ids or [])]
    if ids:
        selected = tidy[tidy["row_id"].astype(str).isin(ids)]
    if selected.empty and finding.stations and "station" in tidy.columns:
        # Fallback for a finding without row ids: the usable rows of its stations.
        usable = tidy["value_std"].notna() if "value_std" in tidy.columns else pd.Series(True, index=tidy.index)
        selected = tidy[(tidy["station"].isin(finding.stations)) & usable]
    if selected.empty:
        return _empty()

    selected = _row_order(selected).head(MAX_ROWS).copy()
    steps = [
        cleaning_step(
            row.get("value_raw"),
            row.get("unit_raw"),
            row.get("value_std"),
            row.get("flags"),
            str(row.get("pollutant") or ""),
        )
        for _, row in selected.iterrows()
    ]
    selected["cleaning_step"] = steps
    if "flags" in selected.columns:
        selected["flags"] = selected["flags"].apply(_as_flags)
    for column in EVIDENCE_COLUMNS:
        if column not in selected.columns:
            selected[column] = None
    return selected[EVIDENCE_COLUMNS].reset_index(drop=True)


__all__ = ["EVIDENCE_COLUMNS", "MAX_ROWS", "cleaning_step", "evidence_rows"]
