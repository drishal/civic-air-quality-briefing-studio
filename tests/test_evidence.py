"""The "Prove it" evidence drawer: original rows plus the cleaning step per row."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core import analytics, clean, ingest, quality
from core.evidence import EVIDENCE_COLUMNS, MAX_ROWS, cleaning_step, evidence_rows
from core.models import Finding

DATA = Path(__file__).resolve().parents[1] / "data"
WEEK1 = DATA / "sample_week1.csv"
needs_sample = pytest.mark.skipif(not WEEK1.exists(), reason="sample_week1.csv is not present")

TIDY_COLUMNS = ["row_id", "station", "ward", "ts", "pollutant", "value_raw", "unit_raw", "value_std", "flags"]


def make_tidy(rows: list[dict]) -> pd.DataFrame:
    base = {
        "row_id": "R1",
        "station": "S1",
        "ward": "Ward 1",
        "ts": pd.Timestamp("2026-11-03 08:00"),
        "pollutant": "PM2.5",
        "value_raw": "10",
        "unit_raw": None,
        "value_std": 10.0,
        "flags": [],
    }
    records = [{**base, **row} for row in rows]
    return pd.DataFrame(records, columns=TIDY_COLUMNS)


def finding_for(*row_ids: str, stations: list[str] | None = None) -> Finding:
    return Finding(
        id="F1",
        type="hotspot",
        title="Hotspot: S1",
        formula="x",
        evidence_row_ids=list(row_ids),
        stations=stations or (["S1"] if not row_ids else []),
    )


# ---------------------------------------------------------------- cleaning step
def test_sentinel_step_names_the_token():
    assert cleaning_step("NA", None, None, ["SENTINEL"]) == "sentinel NA -> missing"
    assert cleaning_step("-999", None, None, ["SENTINEL"]) == "sentinel -999 -> missing"


def test_blank_and_unreadable_cells():
    assert cleaning_step(None, None, None, ["MISSING"]) == "blank cell -> missing"
    assert "unreadable cell" in cleaning_step("n/a?", None, None, ["MISSING"])


def test_unit_conversion_step_names_the_factor():
    assert cleaning_step("22.6 ppb", "ppb", 42.5, ["UNIT_CONVERTED"], "NO2") == "ppb x1.88 -> µg/m³"
    assert cleaning_step("7.5 ppb", "ppb", 14.7, ["UNIT_CONVERTED"], "O3") == "ppb x1.96 -> µg/m³"
    assert (
        cleaning_step("1,836 µg/m3", "µg/m3", 1.836, ["UNIT_CONVERTED"], "CO")
        == "µg/m³ /1000 -> mg/m³; thousands separator removed"
    )


def test_fault_steps_are_plain_words():
    assert "impossible value" in cleaning_step("2750", "ug/m3", None, ["OUT_OF_RANGE"], "PM2.5")
    assert "negative" in cleaning_step("-12.5", None, None, ["NEGATIVE"], "PM10")
    assert cleaning_step("41.0", None, 41.0, ["STUCK"], "O3") == "stuck sensor: identical run flagged"
    assert cleaning_step("10", None, 10.0, ["SPIKE"], "PM2.5") == "spike above the rolling median flagged"
    assert cleaning_step("1,25", None, 1.25, []) == "decimal comma read as a decimal point"


def test_untouched_row_says_kept_as_is():
    assert cleaning_step("45.8", "ug/m3", 45.8, [], "PM2.5") == "kept as-is"
    assert cleaning_step(None, None, None, []) == "no value"


# ---------------------------------------------------------------- evidence frame
def test_evidence_rows_uses_the_finding_row_ids_in_file_order():
    tidy = make_tidy(
        [
            {"row_id": "R9", "pollutant": "PM2.5", "value_raw": "2750", "value_std": None, "flags": ["OUT_OF_RANGE"]},
            {"row_id": "R2", "pollutant": "PM2.5", "value_raw": "80", "value_std": 80.0},
        ]
    )
    frame = evidence_rows(tidy, finding_for("R9", "R2"))
    assert list(frame.columns) == EVIDENCE_COLUMNS
    assert list(frame["row_id"]) == ["R2", "R9"]
    assert frame["cleaning_step"].iloc[0] == "kept as-is"
    assert "impossible value" in frame["cleaning_step"].iloc[1]
    assert frame["value_raw"].tolist() == ["80", "2750"]


def test_evidence_rows_falls_back_to_the_finding_stations():
    tidy = make_tidy(
        [
            {"row_id": "R1", "station": "S1"},
            {"row_id": "R2", "station": "S2"},
            {"row_id": "R3", "station": "S1", "value_std": None, "flags": ["SENTINEL"], "value_raw": "NA"},
        ]
    )
    frame = evidence_rows(tidy, finding_for(stations=["S1"]))
    assert list(frame["row_id"]) == ["R1"]  # unusable rows are not evidence
    assert frame["cleaning_step"].tolist() == ["kept as-is"]


def test_evidence_rows_is_capped_and_never_returns_more_than_fifty():
    tidy = make_tidy([{"row_id": f"R{i}", "value_std": float(i)} for i in range(1, 121)])
    frame = evidence_rows(tidy, Finding(id="F1", type="gap", title="x", formula="x",
                                        evidence_row_ids=[f"R{i}" for i in range(1, 121)]))
    assert len(frame) == MAX_ROWS
    assert list(frame["row_id"])[:2] == ["R1", "R2"]


def test_evidence_rows_with_no_matching_rows_is_an_empty_frame():
    tidy = make_tidy([{"row_id": "R1"}])
    # Unknown ids with a station that is present fall back to that station's rows.
    assert list(evidence_rows(tidy, finding_for("R999", stations=["S1"]))["row_id"]) == ["R1"]
    empty = evidence_rows(tidy, finding_for("R999", stations=["S9"]))
    assert len(empty) == 0
    assert list(empty.columns) == EVIDENCE_COLUMNS
    assert evidence_rows(tidy.iloc[0:0], finding_for("R1")).empty


# ---------------------------------------------------------------- sample file
@needs_sample
def test_sample_week1_rows_show_their_cleaning_steps():
    raw = ingest.read_input(file_bytes=WEEK1.read_bytes(), filename=WEEK1.name)
    tidy, _log = clean.apply_mapping(raw, ingest.heuristic_mapping(raw))
    tidy = quality.flag_readings(tidy)

    # R20 PM2.5 is the impossible 2750, R40 PM2.5 the duplicate copy, R146 NO2 the ppb cell,
    # R102 SO2 the "NA" sentinel, R362 O3 one of the stuck readings.
    frame = evidence_rows(
        tidy,
        finding_for("R20", "R40", "R146", "R102", "R362"),
    )
    steps = {
        (row["row_id"], row["pollutant"]): row["cleaning_step"]
        for _, row in frame.iterrows()
    }
    assert "impossible value 2750" in steps[("R20", "PM2.5")]
    assert steps[("R40", "PM2.5")] == "exact duplicate of an earlier row"
    assert steps[("R146", "NO2")] == "ppb x1.88 -> µg/m³"
    assert steps[("R102", "SO2")] == "sentinel NA -> missing"
    assert "stuck sensor" in steps[("R362", "O3")]
    assert frame["value_raw"].notna().sum() > 0
    assert len(frame) <= MAX_ROWS


@needs_sample
def test_every_finding_in_the_sample_brief_has_usable_evidence_rows():
    raw = ingest.read_input(file_bytes=WEEK1.read_bytes(), filename=WEEK1.name)
    tidy, log = clean.apply_mapping(raw, ingest.heuristic_mapping(raw))
    tidy = quality.flag_readings(tidy)
    summary = quality.quality_summary(raw, tidy, log)
    brief = analytics.build_brief(tidy, summary, "Pune", WEEK1.name, "sample")

    assert brief.findings
    for finding in brief.findings:
        frame = evidence_rows(tidy, finding)
        assert len(frame) <= MAX_ROWS
        assert list(frame.columns) == EVIDENCE_COLUMNS
        assert frame["cleaning_step"].astype(str).str.len().gt(0).all()
        if finding.evidence_row_ids:
            assert set(frame["row_id"]).issubset(set(finding.evidence_row_ids))
