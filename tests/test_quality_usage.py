"""Honest quality numbers: usable share, informational flags, and reliability labels.

The headline "usable %" must count a fault-flagged reading as unusable while unit
notes stay counted but informational. Reliability must not call a station with a
stuck sensor or a dead day "reliable".
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.ingest import RawTable
from core.pipeline import run_pipeline
from core.quality import (
    INFO_FLAGS,
    USABILITY_FLAGS,
    flag_readings,
    quality_summary,
    station_reliability,
)

DATA = Path(__file__).resolve().parents[1] / "data"
WEEK1 = DATA / "sample_week1.csv"
WEEK2 = DATA / "sample_week2.csv"
needs_samples = pytest.mark.skipif(
    not (WEEK1.exists() and WEEK2.exists()), reason="sample data has not been generated"
)

TIDY_COLUMNS = ["row_id", "station", "ward", "ts", "pollutant", "value_raw", "unit_raw", "value_std", "flags"]


def raw_table(n_rows: int) -> RawTable:
    frame = pd.DataFrame({"_src_row": list(range(1, n_rows + 1)), "cell": ["x"] * n_rows})
    return RawTable(df=frame, source_name="inline.csv", notes=[])


def tidy_with_flags(entries: list[tuple[str, float | None, list[str]]]) -> pd.DataFrame:
    """Tidy table from (row_id, value_std, flags) entries, one station and pollutant."""
    records = []
    for index, (row_id, value, flags) in enumerate(entries):
        records.append(
            {
                "row_id": row_id,
                "station": "S1",
                "ward": "Ward 1",
                "ts": pd.Timestamp("2026-11-03") + pd.Timedelta(hours=index),
                "pollutant": "PM2.5",
                "value_raw": None if value is None else str(value),
                "unit_raw": None,
                "value_std": value,
                "flags": list(flags),
            }
        )
    return pd.DataFrame(records, columns=TIDY_COLUMNS)


# ---------------------------------------------------------------- usable share
def test_usable_share_counts_faults_and_ignores_unit_notes():
    tidy = tidy_with_flags(
        [(f"R{i}", 50.0, ["UNIT_AMBIGUOUS"]) for i in range(1, 6)]
        + [
            ("R6", 50.0, ["UNIT_CONVERTED"]),
            ("R7", None, ["SENTINEL"]),
            ("R8", 51.0, ["STUCK"]),
            ("R9", 52.0, ["DUPLICATE"]),
            ("R10", None, ["NEGATIVE"]),
        ]
    )
    summary = quality_summary(raw_table(len(tidy)), tidy, [])

    assert summary.n_readings == 10
    # Six rows carry no fault flag (five unit notes and one conversion); the four fault
    # rows are unusable: sentinel, stuck, duplicate and negative.
    assert summary.n_usable == 6
    assert summary.usable_share == pytest.approx(0.6)
    assert summary.info_flag_counts == {"UNIT_AMBIGUOUS": 5, "UNIT_CONVERTED": 1}
    # Backward compatibility: flag_counts stays the combined dictionary.
    assert summary.flag_counts["UNIT_AMBIGUOUS"] == 5
    assert summary.flag_counts["SENTINEL"] == 1


def test_unit_notes_alone_never_lower_the_usable_share():
    tidy = tidy_with_flags(
        [(f"R{i}", 40.0 + i, ["UNIT_AMBIGUOUS", "UNIT_CONVERTED"]) for i in range(1, 9)]
    )
    summary = quality_summary(raw_table(len(tidy)), tidy, [])
    assert summary.usable_share == 1.0
    assert summary.n_usable == summary.n_readings
    assert sum(summary.info_flag_counts.values()) == 2 * summary.n_readings


def test_flag_sets_keep_informational_flags_apart():
    assert set(INFO_FLAGS) == {"UNIT_AMBIGUOUS", "UNIT_CONVERTED"}
    assert not (set(INFO_FLAGS) & set(USABILITY_FLAGS))
    for flag in ("MISSING", "SENTINEL", "NEGATIVE", "OUT_OF_RANGE", "STUCK", "SPIKE", "DUPLICATE"):
        assert flag in USABILITY_FLAGS


def test_empty_table_has_no_usable_readings():
    summary = quality_summary(raw_table(0), pd.DataFrame(columns=TIDY_COLUMNS), [])
    assert summary.n_readings == 0
    assert summary.n_usable == 0
    assert summary.usable_share == 0.0


# ---------------------------------------------------------------- reliability
def test_a_stuck_sensor_is_never_labelled_reliable():
    values = [50.0] * 6 + [float(60 + hour) for hour in range(18)]
    tidy = tidy_with_flags([(f"R{i + 1}", value, []) for i, value in enumerate(values)])
    flagged = flag_readings(tidy)
    assert any("STUCK" in flags for flags in flagged["flags"])
    reliability = station_reliability(flagged)[0]
    assert reliability.label != "reliable"
    assert reliability.reliability <= 0.7
    assert any("sensor fault" in note for note in reliability.notes)


def test_a_dead_day_on_an_otherwise_full_record_is_use_with_caution():
    rows = []
    row_id = 0
    for hours in (24, 14, 24):
        for hour in range(hours):
            row_id += 1
            rows.append((f"R{row_id}", 60.0 + (hour % 5), []))
    tidy = tidy_with_flags(rows)
    stamps = (
        [pd.Timestamp("2026-11-01") + pd.Timedelta(hours=hour) for hour in range(24)]
        + [pd.Timestamp("2026-11-02") + pd.Timedelta(hours=hour) for hour in range(14)]
        + [pd.Timestamp("2026-11-03") + pd.Timedelta(hours=hour) for hour in range(24)]
    )
    tidy["ts"] = stamps
    reliability = station_reliability(tidy)[0]
    assert reliability.label == "use with caution"
    assert reliability.reliability < 0.75
    assert any("fewer than 16 valid hours" in note for note in reliability.notes)


def test_a_full_clean_record_is_reliable():
    stamps = [
        pd.Timestamp("2026-11-01") + pd.Timedelta(hours=hour)
        for hour in range(24)
    ]
    stamps += [stamp + pd.Timedelta(days=1) for stamp in stamps]
    tidy = tidy_with_flags([(f"R{i + 1}", 50.0 + (i % 7), []) for i in range(len(stamps))])
    tidy["ts"] = stamps
    reliability = station_reliability(tidy)[0]
    assert reliability.label == "reliable"
    assert reliability.reliability > 0.9


# ---------------------------------------------------------------- sample files
def sample_summary(path: Path):
    from core import clean, ingest

    raw = ingest.read_input(file_bytes=path.read_bytes(), filename=path.name)
    tidy, log = clean.apply_mapping(raw, ingest.heuristic_mapping(raw))
    flagged = flag_readings(tidy)
    return quality_summary(raw, flagged, log), flagged


@needs_samples
def test_week1_usable_share_is_below_one_hundred_percent():
    summary, _ = sample_summary(WEEK1)
    assert summary.n_usable < summary.n_readings
    assert 0.97 <= summary.usable_share < 1.0
    assert summary.info_flag_counts["UNIT_AMBIGUOUS"] > 1000
    # The informational notes never enter the unusable count.
    assert summary.flag_counts["UNIT_AMBIGUOUS"] > summary.n_readings - summary.n_usable


@needs_samples
def test_week1_planted_faults_show_in_the_reliability_labels():
    summary, _ = sample_summary(WEEK1)
    labels = {station.station: station.label for station in summary.stations}
    assert labels["Pashan"] == "use with caution"  # ~10 h outage leaves one 14-hour day
    assert labels["Nigdi"] == "use with caution"  # stuck PM2.5 run
    assert labels["Swargate"] == "use with caution"  # stuck O3 run
    assert labels["Hadapsar"] == "reliable"
    assert "reliable" in labels.values()


@needs_samples
def test_week2_nigdi_is_unreliable_and_swargate_carries_its_stuck_sensor():
    summary, _ = sample_summary(WEEK2)
    stations = {station.station: station for station in summary.stations}
    assert stations["Nigdi"].label == "unreliable"
    assert stations["Nigdi"].coverage < 0.7
    assert stations["Swargate"].label == "use with caution"
    assert 0.95 <= summary.usable_share < 1.0


@needs_samples
def test_pipeline_reports_the_usable_share_without_a_llm(tmp_path):
    path = WEEK1
    result = run_pipeline(
        file_bytes=path.read_bytes(),
        filename=path.name,
        use_llm_mapper=False,
        save=False,
        store_dir=str(tmp_path / "briefs"),
    )
    assert result.quality.n_usable < result.quality.n_readings
    assert result.quality.usable_share < 1.0
    assert result.brief.quality.usable_share == result.quality.usable_share
    assert any("readings are usable" in note for note in result.notes)
