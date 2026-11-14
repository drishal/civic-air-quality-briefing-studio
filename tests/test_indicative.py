"""Short or partial tables: the indicative AQI path and the "no usable readings" brief.

A pasted table with fewer than 16 hours per station must still produce an AQI,
clearly marked indicative, with low confidence and a caveat. The strict CPCB
validity rule stays intact for full tables.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from core import analytics
from core.aqi import AQI_COLUMNS, daily_aqi
from core.diff import diff_briefs
from core.models import QualitySummary, StationAQI, StationReliability
from core.pipeline import run_pipeline

SHORT_TABLE = """Station,Ward,Timestamp,PM2.5 (ug/m3),NO2
Hadapsar,Ward 12,2026-11-03 08:00,180,40
Hadapsar,Ward 12,2026-11-03 09:00,190,42
Hadapsar,Ward 12,2026-11-03 10:00,200,45
Hadapsar,Ward 12,2026-11-03 11:00,210,44
Hadapsar,Ward 12,2026-11-03 12:00,205,43
Hadapsar,Ward 12,2026-11-03 13:00,198,41
Hadapsar,Ward 12,2026-11-03 14:00,220,46
Hadapsar,Ward 12,2026-11-03 15:00,215,47
"""

TIDY_COLUMNS = [
    "row_id", "station", "ward", "ts", "pollutant",
    "value_raw", "unit_raw", "value_std", "flags",
]
DAY = "2026-11-03"


def short_brief(tmp_path) -> object:
    return run_pipeline(
        text=SHORT_TABLE,
        area="Pune",
        use_llm_mapper=False,
        save=False,
        store_dir=str(tmp_path / "briefs"),
    ).brief


def make_tidy(rows: list[tuple]) -> pd.DataFrame:
    """Tidy table from (station, ts, pollutant, value) tuples."""
    records = []
    for index, (station, ts, pollutant, value) in enumerate(rows, start=1):
        records.append(
            {
                "row_id": f"R{index}",
                "station": station,
                "ward": "Ward 1",
                "ts": pd.Timestamp(ts) if ts is not None else pd.NaT,
                "pollutant": pollutant,
                "value_raw": None if value is None else str(value),
                "unit_raw": None,
                "value_std": None if value is None else float(value),
                "flags": [],
            }
        )
    return pd.DataFrame(records, columns=TIDY_COLUMNS)


def one_day(station: str, pollutant: str, values: list[float], start_hour: int = 0) -> list[tuple]:
    return [
        (station, f"{DAY} {start_hour + hour:02d}:00", pollutant, value)
        for hour, value in enumerate(values)
    ]


def full_day(station: str) -> list[tuple]:
    return (
        one_day(station, "PM2.5", [150.0] * 24)
        + one_day(station, "PM10", [180.0] * 24)
        + one_day(station, "NO2", [60.0] * 24)
    )


def station_rel(station: str, label: str = "reliable") -> StationReliability:
    scores = {"reliable": 0.92, "use with caution": 0.6, "unreliable": 0.2}
    return StationReliability(
        station=station,
        n_readings=100,
        coverage=0.95 if label == "reliable" else 0.4,
        flagged_share=0.0 if label == "reliable" else 0.3,
        reliability=scores[label],
        label=label,
    )


def quality_for(stations: list[str], labels: dict[str, str] | None = None) -> QualitySummary:
    labels = labels or {}
    return QualitySummary(
        n_rows_in=100,
        n_readings=100 * len(stations),
        stations=[station_rel(name, labels.get(name, "reliable")) for name in stations],
    )


# ---------------------------------------------------------------- indicative AQI
def test_eight_row_table_gets_an_indicative_aqi(tmp_path):
    brief = short_brief(tmp_path)

    assert len(brief.station_aqi) == 1
    station = brief.station_aqi[0]
    assert station.station == "Hadapsar"
    assert station.aqi is not None and not math.isnan(station.aqi)
    assert station.aqi_kind == "indicative"
    assert station.n_valid_days == 0
    assert station.confidence == "low"
    assert "8 hours of data" in station.indicative_reason
    assert "CPCB needs 16" in station.indicative_reason
    assert station.category  # a category is still shown

    hotspot = next(f for f in brief.findings if f.type == "hotspot")
    assert hotspot.stations == ["Hadapsar"]
    assert hotspot.confidence == "low"
    assert hotspot.facts["aqi_kind"] == "indicative"
    assert hotspot.facts["verification_needed"] == "yes"
    assert any("indicative only" in caveat for caveat in hotspot.caveats)
    assert any("unverified" in caveat for caveat in hotspot.caveats)

    gap = next(f for f in brief.findings if f.type == "gap" and "Indicative AQI only" in f.title)
    assert gap.facts["n_hours"] == 8
    assert gap.facts["min_hours_required"] == 16
    assert "CPCB needs 16" in gap.title
    assert gap.evidence_row_ids


def test_one_station_long_record_is_indicative_for_too_few_pollutants():
    # 24 hours of PM2.5 only: the 16-hour rule passes, the three-pollutant rule does not.
    tidy = make_tidy(one_day("S1", "PM2.5", [120.0] * 24))
    brief = analytics.build_brief(
        tidy, quality_for(["S1"]), "Pune", "one.csv", "hash"
    )
    station = brief.station_aqi[0]
    assert station.aqi_kind == "indicative"
    assert station.aqi == pytest.approx(300.0, abs=0.5)
    assert "1 pollutant" in station.indicative_reason
    assert station.confidence == "low"


def test_strict_valid_day_is_still_official_and_has_no_indicative_value():
    tidy = make_tidy(full_day("S1"))
    daily = daily_aqi(tidy)
    row = daily.iloc[0]
    assert bool(row["valid"]) is True
    assert bool(row["indicative"]) is False
    assert row["indicative_reason"] == ""
    assert row["category"] != ""

    brief = analytics.build_brief(tidy, quality_for(["S1"]), "Pune", "full.csv", "hash")
    station = brief.station_aqi[0]
    assert station.aqi_kind == "official"
    assert station.indicative_reason == ""
    assert station.n_valid_days == 1


def test_indicative_columns_are_part_of_the_daily_aqi_contract():
    assert "indicative_aqi" in AQI_COLUMNS
    assert "indicative_reason" in AQI_COLUMNS
    assert "indicative" in AQI_COLUMNS


def test_indicative_requires_a_particulate_reading():
    # NO2 only: no PM reading, so not even an indicative AQI is allowed.
    tidy = make_tidy(one_day("S1", "NO2", [80.0] * 8))
    row = daily_aqi(tidy).iloc[0]
    assert bool(row["indicative"]) is False
    assert math.isnan(row["indicative_aqi"])
    brief = analytics.build_brief(tidy, quality_for(["S1"]), "Pune", "no2.csv", "hash")
    assert brief.station_aqi[0].aqi is None


# ---------------------------------------------------------------- ranking rules
def test_indicative_aqi_never_outranks_an_official_hotspot():
    rows = full_day("Official")
    rows += one_day("Short", "PM2.5", [400.0] * 6)  # indicative AQI 500, higher than Official
    tidy = make_tidy(rows)
    brief = analytics.build_brief(tidy, quality_for(["Official", "Short"]), "Pune", "mix.csv", "h")

    aqi_by_station = {s.station: s for s in brief.station_aqi}
    assert aqi_by_station["Official"].aqi_kind == "official"
    assert aqi_by_station["Short"].aqi_kind == "indicative"
    assert aqi_by_station["Short"].aqi > aqi_by_station["Official"].aqi

    hotspots = [f for f in brief.findings if f.type == "hotspot"]
    assert [f.stations[0] for f in hotspots] == ["Official"]


def test_all_indicative_briefs_still_rank_a_hotspot():
    tidy = make_tidy(one_day("A", "PM2.5", [200.0] * 6) + one_day("B", "PM2.5", [60.0] * 6))
    brief = analytics.build_brief(tidy, quality_for(["A", "B"]), "Pune", "all.csv", "h")
    hotspots = [f for f in brief.findings if f.type == "hotspot"]
    assert [f.stations[0] for f in hotspots] == ["A", "B"]
    assert all(f.confidence == "low" for f in hotspots)


# ---------------------------------------------------------------- diff behaviour
def test_diff_against_no_previous_works_for_a_short_table(tmp_path):
    result = run_pipeline(
        text=SHORT_TABLE, use_llm_mapper=False, save=False, store_dir=str(tmp_path / "b")
    ).diff
    assert result.has_previous is False
    assert result.summary_facts["n_stations"] == 1
    assert "headline_facts" in result.summary_facts


def test_diff_marks_a_low_confidence_new_hotspot_unverified():
    def make_brief(name: str, stations: list[StationAQI]) -> object:
        return analytics.build_brief(
            make_tidy([]), quality_for([s.station for s in stations]), "Pune", name, name
        ).model_copy(update={"station_aqi": stations})

    previous = make_brief(
        "prev",
        [
            StationAQI(station="A", aqi=300.0, category="Very Poor", confidence="high"),
            StationAQI(station="B", aqi=250.0, category="Poor", confidence="high"),
            StationAQI(station="C", aqi=200.0, category="Poor", confidence="high"),
        ],
    )
    current = make_brief(
        "now",
        [
            StationAQI(station="A", aqi=310.0, category="Very Poor", confidence="high"),
            StationAQI(station="B", aqi=255.0, category="Poor", confidence="high"),
            StationAQI(station="C", aqi=205.0, category="Poor", confidence="high"),
            StationAQI(station="D", aqi=290.0, category="Poor", confidence="low"),
        ],
    )
    result = diff_briefs(current, previous)
    assert result.new_hotspots == ["D (unverified)"]


# ---------------------------------------------------------------- no usable data
def test_all_nan_table_gives_a_no_usable_readings_gap():
    tidy = make_tidy(
        [
            ("S1", f"{DAY} {hour:02d}:00", "PM2.5", None)
            for hour in range(6)
        ]
        + [("S1", f"{DAY} {hour:02d}:00", "PM10", None) for hour in range(6)]
    )
    tidy["flags"] = [["SENTINEL"]] * len(tidy)
    brief = analytics.build_brief(tidy, quality_for(["S1"]), "Pune", "nan.csv", "h")

    assert [f.title for f in brief.findings] == ["No usable readings"]
    finding = brief.findings[0]
    assert finding.type == "gap"
    assert finding.facts["n_usable"] == 0
    assert finding.caveats
    assert brief.station_aqi

    # Never raises, and the diff still renders.
    result = diff_briefs(brief, None)
    assert result.has_previous is False


def test_empty_tidy_brief_has_the_gap_finding_and_no_exception():
    brief = analytics.build_brief(pd.DataFrame(), quality_for([]), "Pune", "empty.csv", "h")
    assert [f.title for f in brief.findings] == ["No usable readings"]
    assert brief.findings[0].facts["n_readings"] == 0


def test_pipeline_note_reports_the_honest_usable_share(tmp_path):
    result = run_pipeline(
        text=SHORT_TABLE, use_llm_mapper=False, save=False, store_dir=str(tmp_path / "b")
    )
    note = next(note for note in result.notes if "readings are usable" in note)
    assert "16 of 16" in note  # 8 rows x 2 pollutants, nothing faulted


SAMPLE_WEEK1 = Path(__file__).resolve().parents[1] / "data" / "sample_week1.csv"


@pytest.mark.skipif(not SAMPLE_WEEK1.exists(), reason="sample_week1.csv is not present")
def test_full_sample_table_stays_official(tmp_path):
    path = SAMPLE_WEEK1
    result = run_pipeline(
        file_bytes=path.read_bytes(),
        filename=path.name,
        use_llm_mapper=False,
        save=False,
        store_dir=str(tmp_path / "briefs"),
    )
    assert result.brief.station_aqi
    assert all(s.aqi_kind == "official" for s in result.brief.station_aqi)
    assert not any("Indicative AQI only" in f.title for f in result.brief.findings)
