"""Shared data contracts. Every module imports types from here. DO NOT change field names
without telling the integrator; other modules depend on them."""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

POLLUTANTS = ("PM2.5", "PM10", "NO2", "SO2", "O3", "CO", "NH3")
# Standard unit after cleaning: ug/m3 for all pollutants except CO which is mg/m3.
STD_UNIT = {p: "µg/m³" for p in POLLUTANTS}
STD_UNIT["CO"] = "mg/m³"

CATEGORIES = ("Good", "Satisfactory", "Moderately Polluted", "Poor", "Very Poor", "Severe")

# Quality flags attached to individual readings (list[str] in the tidy table).
FLAGS = (
    "MISSING",          # empty / NA
    "SENTINEL",         # -999, 9999, "--", ND, BDL ... treated as missing
    "NEGATIVE",         # negative concentration, value_std set to NaN
    "OUT_OF_RANGE",     # physically implausible (e.g. PM2.5 > 1000), value_std set to NaN
    "UNIT_AMBIGUOUS",   # unit missing/unclear, converted using a guess
    "UNIT_MISMATCH",    # e.g. ppb given for PM
    "UNIT_CONVERTED",   # informational: ppb->ug/m3, mg->ug etc.
    "CENSORED",         # "<5" or ">500" style values
    "STUCK",            # >=6 identical consecutive readings
    "SPIKE",            # jump far above rolling median
    "DUPLICATE",        # duplicate station+ts+pollutant
    "TIMESTAMP_REPAIRED",
    "TIMESTAMP_UNPARSEABLE",
)

Confidence = Literal["high", "medium", "low"]


# ---------------------------------------------------------------- ingest / mapping
class ColumnRole(BaseModel):
    column: str
    role: Literal[
        "station", "ward", "timestamp", "date", "time",
        "pollutant_value",   # wide layout: the column holds values of ONE pollutant (see `pollutant`)
        "pollutant_name",    # long layout: column holding the pollutant name per row
        "value",             # long layout: column holding the numeric value
        "unit",              # long layout (or per-row unit): column holding the unit
        "lat", "lon", "ignore",
    ]
    pollutant: Optional[str] = None   # canonical name from POLLUTANTS (for pollutant_value)
    unit: Optional[str] = None        # unit as written in header, e.g. "ppb", "ug/m3"
    confidence: Confidence = "high"


class ColumnMapping(BaseModel):
    layout: Literal["wide", "long"]
    columns: list[ColumnRole]
    date_format_hint: Optional[str] = None      # e.g. "DD/MM/YYYY HH:mm"
    sentinel_tokens: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    source: Literal["llm", "heuristic"] = "heuristic"


# ---------------------------------------------------------------- quality
class StationReliability(BaseModel):
    station: str
    n_readings: int
    coverage: float          # 0..1 share of expected hourly readings actually valid
    flagged_share: float     # 0..1 share of readings carrying a data-quality flag
    reliability: float       # 0..1 = coverage x (1 - flagged share), lowered for a dead day, capped by a sensor fault
    label: Literal["reliable", "use with caution", "unreliable"]
    notes: list[str] = Field(default_factory=list)


class QualitySummary(BaseModel):
    n_rows_in: int
    n_readings: int
    flag_counts: dict[str, int] = Field(default_factory=dict)      # combined, incl. informational flags
    info_flag_counts: dict[str, int] = Field(default_factory=dict)  # UNIT_AMBIGUOUS / UNIT_CONVERTED only
    n_usable: int = 0              # readings with no missing/sentinel/negative/…/stuck/spike/duplicate flag
    usable_share: float = 1.0      # n_usable / n_readings; 0.0 when there are no readings
    stations: list[StationReliability] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)          # human-readable gap statements
    cleaning_log: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- findings / brief
class Finding(BaseModel):
    id: str                                   # "F1", "F2", ...
    type: Literal["hotspot", "trend", "exceedance", "gap", "anomaly", "pattern"]
    title: str                                # short human title
    facts: dict[str, Union[float, int, str, None]] = Field(default_factory=dict)  # ALL numbers the text may cite
    formula: str                              # how it was computed, plain words
    evidence_row_ids: list[str] = Field(default_factory=list)   # row_ids in the tidy table (cap at 50)
    stations: list[str] = Field(default_factory=list)
    confidence: Confidence = "high"
    caveats: list[str] = Field(default_factory=list)


class StationAQI(BaseModel):
    station: str
    ward: Optional[str] = None
    aqi: Optional[float] = None               # latest valid daily AQI; None if no valid day
    category: Optional[str] = None            # one of CATEGORIES
    dominant: Optional[str] = None            # pollutant setting the AQI
    subindices: dict[str, float] = Field(default_factory=dict)
    latest_date: Optional[str] = None         # YYYY-MM-DD
    n_valid_days: int = 0
    reliability: float = 0.0
    confidence: Confidence = "low"
    mean_aqi_period: Optional[float] = None
    trend_per_day: Optional[float] = None     # AQI units/day, linear slope over valid days
    aqi_kind: Literal["official", "indicative"] = "official"  # indicative = below the CPCB completeness rule
    indicative_reason: str = ""               # why this AQI is only indicative ("" for official)

class Brief(BaseModel):
    brief_id: str
    created_at: str                           # ISO timestamp
    area: str
    source_name: str
    source_hash: str
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    station_aqi: list[StationAQI] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    quality: QualitySummary


# ---------------------------------------------------------------- diff
class StationChange(BaseModel):
    station: str
    aqi_prev: Optional[float]
    aqi_now: Optional[float]
    delta: Optional[float]
    category_prev: Optional[str]
    category_now: Optional[str]
    direction: Literal["worse", "better", "same", "new", "dropped", "unknown"]


class DiffResult(BaseModel):
    has_previous: bool
    previous_brief_id: Optional[str] = None
    station_changes: list[StationChange] = Field(default_factory=list)
    new_hotspots: list[str] = Field(default_factory=list)
    resolved_hotspots: list[str] = Field(default_factory=list)
    category_transitions: list[str] = Field(default_factory=list)   # e.g. "Anand Vihar: Poor -> Very Poor"
    quality_changes: list[str] = Field(default_factory=list)
    new_finding_titles: list[str] = Field(default_factory=list)
    resolved_finding_titles: list[str] = Field(default_factory=list)
    finding_changes: list[dict[str, Any]] = Field(default_factory=list)  # {key, status, title_now, title_prev}
    summary_facts: dict[str, Any] = Field(default_factory=dict)  # citable numbers; "headline_facts" holds the writer's set


# ---------------------------------------------------------------- personas
Persona = Literal["ward_officer", "housing_society", "school"]
ReadingLevel = Literal["simple", "standard", "technical"]
