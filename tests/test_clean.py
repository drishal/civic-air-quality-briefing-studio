"""Tests for ingest, clean and quality with small inline messy tables."""
from __future__ import annotations

import io
import pathlib

import pandas as pd
import pytest

from core.clean import apply_mapping, canonical_ward, convert_value, parse_timestamp, parse_value_cell
from core.ingest import heuristic_mapping, read_input
from core.quality import flag_readings, quality_summary, station_reliability


# --------------------------------------------------------------------------- helpers
def pipeline(text: str | None = None, **kwargs):
    raw = read_input(text=text, **kwargs)
    mapping = heuristic_mapping(raw)
    tidy, log = apply_mapping(raw, mapping)
    return tidy, log, raw, mapping


def rows(tidy: pd.DataFrame, pollutant: str) -> pd.DataFrame:
    return tidy[tidy["pollutant"] == pollutant].sort_values("ts").reset_index(drop=True)


def values(tidy: pd.DataFrame, pollutant: str) -> list[float]:
    return list(rows(tidy, pollutant)["value_std"])


def row_flags(tidy: pd.DataFrame, pollutant: str) -> list[list[str]]:
    return [list(f) for f in rows(tidy, pollutant)["flags"]]


def series_csv(
    values_list: list[object],
    pollutant: str = "PM2.5",
    unit: str = "µg/m³",
    station: str = "S1",
    ward: str = "Ward 1",
    start: str = "2026-11-03 00:00",
    header: str | None = None,
) -> str:
    """A wide one-station hourly table with the given values."""
    lines = [header or f"Station,Ward,Timestamp,{pollutant} ({unit})"]
    stamp = pd.Timestamp(start)
    for offset, value in enumerate(values_list):
        lines.append(f"{station},{ward},{stamp + pd.Timedelta(hours=offset):%Y-%m-%d %H:%M},{value}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- sentinels
def test_sentinel_codes_become_missing() -> None:
    tidy, log, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (µg/m³),PM10 (µg/m³)
S1,Ward 1,2026-11-03 07:00,NA,45
S1,Ward 1,2026-11-03 08:00,--,55
S1,Ward 1,2026-11-03 09:00,-999,9999
S1,Ward 1,2026-11-03 10:00,ND,BDL
S1,Ward 1,2026-11-03 11:00,,50
"""
    )
    pm25 = row_flags(tidy, "PM2.5")
    assert [f for f in pm25[0]] == ["SENTINEL"]
    assert pm25[4] == ["MISSING"]
    assert all(pd.isna(v) for v in values(tidy, "PM2.5"))
    assert values(tidy, "PM10") == [45.0, 55.0, 50.0, float("nan"), float("nan")] or sorted(
        [v for v in values(tidy, "PM10") if not pd.isna(v)]
    ) == [45.0, 50.0, 55.0]
    assert any("sentinel" in line for line in log)


def test_sentinels_inside_cells_with_units() -> None:
    tidy, _, _, _ = pipeline(
        """Station,Ward,Date,PM2.5
S1,Ward 1,2026-11-03 07:00,NA µg/m3
S1,Ward 1,2026-11-03 08:00,12 µg/m3
"""
    )
    assert pd.isna(values(tidy, "PM2.5")[0])
    assert values(tidy, "PM2.5")[1] == 12.0


# --------------------------------------------------------------------------- numbers
def test_decimal_comma_thousands_separator_and_ambiguity() -> None:
    tidy, log, _, _ = pipeline(
        '''Station,Ward,Date,PM10 (µg/m³),CO (mg/m³)
S1,Ward 1,2026-11-03 07:00,"1,250",1.2
S1,Ward 1,2026-11-03 08:00,"12,5","0,8"
S1,Ward 1,2026-11-03 09:00,"2,400","1,250"
'''
    )
    assert values(tidy, "PM10") == [1250.0, 12.5, 2400.0]
    # "1,250" as CO would be 1250 mg/m³ (impossible); read it as 1.25 instead.
    co = values(tidy, "CO")
    assert co[0] == 1.2
    assert co[2] == 1.25
    assert any("thousands" in line for line in log)


def test_parse_value_cell_reads_separators_both_ways() -> None:
    assert parse_value_cell("1,250").value == 1250.0
    assert parse_value_cell("1,250").alt == 1.25
    assert parse_value_cell("12,5").value == 12.5
    assert parse_value_cell("0,8").value == 0.8
    assert parse_value_cell("1.250,5").value == 1250.5
    assert parse_value_cell("1,250.5").value == 1250.5
    assert parse_value_cell("  85  ").value == 85.0
    assert parse_value_cell("").kind == "blank"
    assert parse_value_cell("bdl").kind == "sentinel"
    assert parse_value_cell("traffic").kind == "bad"


# --------------------------------------------------------------------------- units
def test_units_inside_cells_are_stripped() -> None:
    tidy, log, _, _ = pipeline(
        """Station,Ward,Date,PM2.5
S1,Ward 1,2026-11-03 07:00,85 µg/m³
S1,Ward 1,2026-11-03 08:00,85 ug/m3
S1,Ward 1,2026-11-03 09:00, 90 (est)
"""
    )
    assert values(tidy, "PM2.5") == [85.0, 85.0, 90.0]
    assert list(rows(tidy, "PM2.5")["unit_raw"])[:2] == ["µg/m³", "ug/m3"]


def test_ppb_and_ppm_convert_to_standard_units() -> None:
    tidy, _, _, mapping = pipeline(
        """Station,Ward,Date,NO2 (ppb),O3 (ppb),SO2 (ppb),NH3 (ppb),CO (ppm)
S1,Ward 1,2026-11-03 07:00,40,10,5,100,1.5
"""
    )
    assert mapping.layout == "wide"
    assert values(tidy, "NO2") == [pytest.approx(75.2)]
    assert values(tidy, "O3") == [pytest.approx(19.6)]
    assert values(tidy, "SO2") == [pytest.approx(13.1)]
    assert values(tidy, "NH3") == [pytest.approx(70.0)]
    assert values(tidy, "CO") == [pytest.approx(1.719)]  # 1.5 ppm x 1.146 mg/m³
    assert all("UNIT_CONVERTED" in f for f in row_flags(tidy, "NO2"))


def test_mg_per_m3_and_ug_per_m3_are_normalised() -> None:
    tidy, _, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (mg/m³),CO (µg/m³)
S1,Ward 1,2026-11-03 07:00,0.085,1200
"""
    )
    assert values(tidy, "PM2.5") == [pytest.approx(85.0)]
    assert values(tidy, "CO") == [pytest.approx(1.2)]


def test_ppb_on_particulate_matter_is_a_unit_mismatch() -> None:
    tidy, log, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (ppb)
S1,Ward 1,2026-11-03 07:00,35
"""
    )
    assert pd.isna(values(tidy, "PM2.5")[0])
    assert "UNIT_MISMATCH" in row_flags(tidy, "PM2.5")[0]
    assert any("particulate" in line for line in log)


def test_missing_unit_is_assumed_and_flagged() -> None:
    tidy, log, _, _ = pipeline(
        """Station,Ward,Date,PM2.5
S1,Ward 1,2026-11-03 07:00,85
"""
    )
    assert values(tidy, "PM2.5") == [85.0]
    assert "UNIT_AMBIGUOUS" in row_flags(tidy, "PM2.5")[0]
    assert any("no clear unit" in line for line in log)
    assert convert_value(5.0, None, "CO")[1] == ["UNIT_AMBIGUOUS"]


def test_long_layout_units_come_from_the_unit_column() -> None:
    tidy, _, _, mapping = pipeline(
        """Station,Ward,Timestamp,Parameter,Value,Unit
S1,Ward 1,2026-11-03 07:00,PM2.5,85,ug/m3
S1,Ward 1,2026-11-03 07:00,no2,40,ppb
S1,Ward 1,2026-11-03 07:00,CO,2.0,mg/m3
S1,Ward 1,2026-11-03 07:00,lead,1.0,ug/m3
"""
    )
    assert mapping.layout == "long"
    assert set(tidy["pollutant"]) == {"PM2.5", "NO2", "CO"}
    assert values(tidy, "NO2") == [pytest.approx(75.2)]
    assert values(tidy, "CO") == [pytest.approx(2.0)]


def test_per_row_unit_column_and_time_only_timestamp() -> None:
    tidy, _, _, mapping = pipeline(
        """Station,Ward,Reading Time,PM2.5,NO2,Unit
S1,Ward 1,2026-11-03 07:00,85,40,ug/m3
"""
    )
    assert mapping.layout == "wide"
    assert not tidy["ts"].isna().any()
    assert values(tidy, "PM2.5") == [85.0]
    assert values(tidy, "NO2") == [40.0]
    assert "UNIT_AMBIGUOUS" not in row_flags(tidy, "NO2")[0]


# --------------------------------------------------------------------------- censored / range
def test_censored_values_keep_the_stated_limit() -> None:
    tidy, _, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (µg/m³),PM10 (µg/m³)
S1,Ward 1,2026-11-03 07:00,<5,>500
"""
    )
    assert values(tidy, "PM2.5") == [5.0]
    assert values(tidy, "PM10") == [500.0]
    assert "CENSORED" in row_flags(tidy, "PM2.5")[0]
    assert "CENSORED" in row_flags(tidy, "PM10")[0]


def test_negative_and_impossible_values_are_rejected() -> None:
    tidy, log, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (µg/m³),PM10 (µg/m³),NO2 (µg/m³)
S1,Ward 1,2026-11-03 07:00,-4,3000,20
S1,Ward 1,2026-11-03 08:00,20,50,2500
"""
    )
    pm25 = values(tidy, "PM2.5")
    pm10 = values(tidy, "PM10")
    no2 = values(tidy, "NO2")
    assert pd.isna(pm25[0]) and "NEGATIVE" in row_flags(tidy, "PM2.5")[0]
    assert pd.isna(pm10[0]) and "OUT_OF_RANGE" in row_flags(tidy, "PM10")[0]
    assert pd.isna(no2[1]) and "OUT_OF_RANGE" in row_flags(tidy, "NO2")[1]
    assert pm25[1] == 20.0 and pm10[1] == 50.0
    assert any("negative" in line for line in log)


# --------------------------------------------------------------------------- timestamps
def test_mixed_date_formats_with_dd_mm_inference() -> None:
    tidy, log, _, _ = pipeline(
        """Station,Ward,Timestamp,PM2.5 (µg/m³)
S1,Ward 1,03/11/2026 07:00,10
S1,Ward 1,2026-11-03 08:00,11
S1,Ward 1,04/11/2026 19:00,12
S1,Ward 1,13/11/2026 07:00,13
S1,Ward 1,03 Nov 2026 09:00,14
"""
    )
    stamps = rows(tidy, "PM2.5")["ts"].tolist()
    assert not any(pd.isna(stamp) for stamp in stamps)
    days = {(stamp.day, stamp.month) for stamp in stamps}
    assert (3, 11) in days and (4, 11) in days and (13, 11) in days
    assert any("DD/MM" in line for line in log)


def test_month_first_dates_are_detected() -> None:
    tidy, log, _, _ = pipeline(
        """Station,Ward,Timestamp,PM2.5 (µg/m³)
S1,Ward 1,11/03/2026 07:00,10
S1,Ward 1,11/25/2026 08:00,11
"""
    )
    stamps = rows(tidy, "PM2.5")["ts"].tolist()
    assert stamps[0].month == 11 and stamps[0].day == 3
    assert stamps[1].month == 11 and stamps[1].day == 25
    assert any("MM/DD" in line for line in log)


def test_twelve_and_twenty_four_hour_times_agree() -> None:
    tidy, _, _, _ = pipeline(
        """Station,Ward,Timestamp,PM2.5 (µg/m³)
S1,Ward 1,2026-11-03 07:00 PM,20
S1,Ward 1,2026-11-03 19:00,21
S1,Ward 1,2026-11-03 12:00 AM,22
"""
    )
    stamps = rows(tidy, "PM2.5")["ts"].tolist()
    hours = sorted(stamp.hour for stamp in stamps)
    assert hours == [0, 19, 19]


def test_unparseable_timestamp_does_not_crash() -> None:
    tidy, _, _, _ = pipeline(
        """Station,Ward,Timestamp,PM2.5 (µg/m³)
S1,Ward 1,last tuesday,10
S1,Ward 1,2026-11-03 07:00,11
"""
    )
    assert tidy["ts"].isna().sum() == 1
    unparsed = tidy[tidy["ts"].isna()].iloc[0]
    assert "TIMESTAMP_UNPARSEABLE" in unparsed["flags"]


def test_parse_timestamp_handles_timezone_labels_and_24_00() -> None:
    stamp, _ = parse_timestamp("2026-11-03 07:00 IST")
    assert stamp is not None and stamp.hour == 7
    stamp, repaired = parse_timestamp("2026-11-03 24:00")
    assert stamp is not None and stamp.day == 4 and repaired


# --------------------------------------------------------------------------- names and layouts
def test_ward_spelled_three_ways_becomes_one_name() -> None:
    tidy, _, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (µg/m³)
S1,Ward 12,2026-11-03 07:00,10
S1,ward-12,2026-11-03 08:00,11
S1,W12,2026-11-03 09:00,12
S1,Ward No. 12,2026-11-03 10:00,13
"""
    )
    assert list(tidy["ward"].unique()) == ["Ward 12"]
    assert canonical_ward("W12") == canonical_ward("ward-12") == "Ward 12"
    assert canonical_ward("kothrud") == "Kothrud"


def test_station_typos_are_merged_but_numbered_sites_are_not() -> None:
    tidy, log, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (µg/m³)
Shivajinagar,Ward 1,2026-11-03 07:00,10
Shivajinagar,Ward 1,2026-11-03 08:00,10
Shivajinagr,Ward 1,2026-11-03 09:00,10
SITE 1,Ward 2,2026-11-03 07:00,10
SITE 2,Ward 2,2026-11-03 08:00,10
"""
    )
    stations = set(tidy["station"])
    assert "Shivajinagar" in stations
    assert "Shivajinagr" not in stations
    assert {"Site 1", "Site 2"} <= stations
    assert any("Merged station name" in line for line in log)


def test_subtitle_row_above_the_header_is_skipped() -> None:
    raw = read_input(
        text="""Pune air quality monitoring - November 2026
Station,Ward,Date,PM2.5 (µg/m³)
S1,Ward 1,2026-11-03 07:00,10
"""
    )
    assert list(raw.df.columns) == ["Station", "Ward", "Date", "PM2.5 (µg/m³)", "_src_row"]
    assert len(raw.df) == 1
    assert any("title row" in note for note in raw.notes)

    tidy, _, _, mapping = pipeline(
        """Pune air quality monitoring - November 2026
Station,Ward,Date,PM2.5 (µg/m³)
S1,Ward 1,2026-11-03 07:00,10
"""
    )
    assert mapping.layout == "wide"
    assert values(tidy, "PM2.5") == [10.0]


def test_repeated_header_row_inside_data_is_dropped() -> None:
    raw = read_input(
        text="""Station,Ward,Date,PM2.5 (µg/m³)
S1,Ward 1,2026-11-03 07:00,10
Station,Ward,Date,PM2.5 (µg/m³)
S1,Ward 1,2026-11-03 08:00,11
"""
    )
    assert len(raw.df) == 2
    assert list(raw.df["_src_row"]) == [1, 2]
    assert any("repeated header" in note for note in raw.notes)
    tidy, _, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (µg/m³)
S1,Ward 1,2026-11-03 07:00,10
Station,Ward,Date,PM2.5 (µg/m³)
S1,Ward 1,2026-11-03 08:00,11
"""
    )
    assert values(tidy, "PM2.5") == [10.0, 11.0]
    assert list(rows(tidy, "PM2.5")["row_id"]) == ["R1", "R2"]


def test_duplicate_rows_are_flagged() -> None:
    tidy, log, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (µg/m³)
S1,Ward 1,2026-11-03 07:00,10
S1,Ward 1,2026-11-03 07:00,10
"""
    )
    pm = rows(tidy, "PM2.5")
    assert len(pm) == 2
    flagged = [f for f in pm["flags"] if "DUPLICATE" in f]
    assert len(flagged) == 1
    assert any("duplicate" in line for line in log)


def test_tsv_semicolon_pipe_and_whitespace_files() -> None:
    tsv = "Station\tWard\tDate\tPM2.5 (µg/m³)\nS1\tWard 1\t2026-11-03 07:00\t10\n"
    semicolon = "Station;Ward;Date;PM2.5 (µg/m³)\nS1;Ward 1;03/11/2026 07:00;12,5\n"
    pipe = "Station|Ward|Date|PM2.5 (µg/m³)\nS1|Ward 1|2026-11-03 07:00|10\n"
    spaced = "Station   Ward     Date              PM2.5 (µg/m³)\nS1        Ward 1   2026-11-03 07:00  10\n"
    assert values(pipeline(tsv)[0], "PM2.5") == [10.0]
    assert values(pipeline(semicolon)[0], "PM2.5") == [12.5]
    assert values(pipeline(pipe)[0], "PM2.5") == [10.0]
    assert values(pipeline(spaced)[0], "PM2.5") == [10.0]


def test_bytes_input_and_bom_are_handled() -> None:
    text = "Station,Ward,Date,PM2.5 (µg/m³)\nS1,Ward 1,2026-11-03 07:00,10\n"
    raw = read_input(file_bytes=text.encode("utf-8-sig"), filename="week.csv")
    assert raw.source_name == "week.csv"
    assert list(raw.df.columns)[0] == "Station"
    tidy, _, _, _ = pipeline(None, file_bytes=text.encode("utf-8-sig"), filename="week.csv")
    assert values(tidy, "PM2.5") == [10.0]


def test_xlsx_first_sheet_with_data_is_read() -> None:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame({"note": ["empty"]}).iloc[0:0].to_excel(writer, sheet_name="cover", index=False, header=True)
        pd.DataFrame(
            {
                "Station": ["S1", "S1"],
                "Ward": ["Ward 1", "Ward 1"],
                "Date": ["2026-11-03 07:00", "2026-11-03 08:00"],
                "PM2.5": [10, 11],
            }
        ).to_excel(writer, sheet_name="data", index=False)
    data = buffer.getvalue()
    raw = read_input(file_bytes=data, filename="week.xlsx")
    assert "PM2.5" in raw.df.columns
    tidy, _, _, _ = pipeline(None, file_bytes=data, filename="week.xlsx")
    assert values(tidy, "PM2.5") == [10.0, 11.0]


# --------------------------------------------------------------------------- unusable input
def test_empty_input_raises_friendly_errors() -> None:
    with pytest.raises(ValueError, match="Nothing to read"):
        read_input()
    with pytest.raises(ValueError, match="empty"):
        read_input(text="   \n\n  ")
    with pytest.raises(ValueError, match="data row"):
        read_input(text="Station,Ward,Date,PM2.5")
    with pytest.raises(ValueError, match="no usable readings"):
        pipeline("Station,Ward,Timestamp,Parameter,Value\nS1,Ward 1,2026-11-03 07:00,lead,1.0")


def test_table_without_pollutants_raises() -> None:
    raw = read_input(text="Station,Ward,Date,AQI\nS1,Ward 1,2026-11-03 07:00,120\n")
    mapping = heuristic_mapping(raw)
    with pytest.raises(ValueError, match="pollutant"):
        apply_mapping(raw, mapping)


def test_bad_cells_never_crash_a_row() -> None:
    tidy, log, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (µg/m³),PM10 (µg/m³)
S1,Ward 1,2026-11-03 07:00,not a number,20
S1,Ward 1,2026-11-03 08:00,???,25
S1,Ward 1,2026-11-03 09:00,12345678901234567890,30
"""
    )
    assert len(rows(tidy, "PM2.5")) == 3
    assert values(tidy, "PM2.5")[0] is not None and pd.isna(values(tidy, "PM2.5")[0])
    assert pd.isna(values(tidy, "PM2.5")[1])
    assert any("no readable number" in line for line in log)


# --------------------------------------------------------------------------- quality
def test_stuck_sensor_is_flagged() -> None:
    tidy, _, _, _ = pipeline(series_csv([20, 20, 20, 20, 20, 20, 20, 20, 25]))
    flagged = flag_readings(tidy)
    stuck = [bool({"STUCK"} & set(f)) for f in flagged["flags"]]
    assert sum(stuck) == 8
    assert not any("STUCK" in f for f in flagged["flags"].tolist()[-1:])


def test_spike_is_flagged_against_the_rolling_median() -> None:
    tidy, _, _, _ = pipeline(series_csv([10, 12, 11, 13, 12, 10, 900, 11]))
    flagged = flag_readings(tidy)
    flags = flagged["flags"].tolist()
    assert "SPIKE" in flags[6]
    assert not any("SPIKE" in f for f in flags[:6])
    assert not any("SPIKE" in f for f in flags[7:])


def test_flag_readings_keeps_existing_flags_and_input_unchanged() -> None:
    tidy, _, _, _ = pipeline(
        """Station,Ward,Date,PM2.5 (µg/m³)
S1,Ward 1,2026-11-03 07:00,NA
S1,Ward 1,2026-11-03 08:00,10
"""
    )
    before = [list(f) for f in tidy["flags"]]
    flagged = flag_readings(tidy)
    assert [list(f) for f in tidy["flags"]] == before
    assert "SENTINEL" in flagged["flags"].tolist()[0]


def test_reliability_labels_follow_coverage_and_flags() -> None:
    lines = ["Station,Ward,Timestamp,PM2.5 (µg/m³)"]
    start = pd.Timestamp("2026-11-03 00:00")
    for hour in range(24):
        lines.append(f"Good,Ward 1,{start + pd.Timedelta(hours=hour):%Y-%m-%d %H:%M},{10 + hour % 5}")
    for hour in range(0, 24, 2):  # only every second hour: 50% coverage
        lines.append(f"Patchy,Ward 1,{start + pd.Timedelta(hours=hour):%Y-%m-%d %H:%M},{20 + hour % 7}")
    tidy, _, _, _ = pipeline("\n".join(lines))
    reliability = {item.station: item for item in station_reliability(flag_readings(tidy))}
    assert reliability["Good"].coverage == pytest.approx(1.0)
    assert reliability["Good"].label == "reliable"
    # Coverage counts over the station's own observed span, so an every-other-hour
    # series from 00:00 to 22:00 is 12 of 23 hours.
    assert 0.4 < reliability["Patchy"].coverage < 0.6
    assert reliability["Patchy"].label == "use with caution"
    assert reliability["Patchy"].notes


def test_quality_summary_reports_gaps_missing_pollutants_and_flags() -> None:
    lines = ["Station,Ward,Timestamp,PM2.5 (µg/m³),NO2 (µg/m³)"]
    start = pd.Timestamp("2026-11-03 00:00")
    for hour in list(range(0, 2)) + list(range(9, 24)):
        lines.append(f"S1,Ward 1,{start + pd.Timedelta(hours=hour):%Y-%m-%d %H:%M},{10 + hour % 5},30")
    for hour in range(0, 24):
        lines.append(f"S2,Ward 2,{start + pd.Timedelta(hours=hour):%Y-%m-%d %H:%M},{15 + hour % 6},")
    tidy, log, raw, _ = pipeline("\n".join(lines))
    flagged = flag_readings(tidy)
    summary = quality_summary(raw, flagged, log)
    assert summary.n_rows_in == raw.df.shape[0]
    assert summary.n_readings == len(flagged)
    assert summary.flag_counts.get("MISSING", 0) >= 24  # S2 never reports NO2
    assert any("no readings between 02:00 and 09:00" in gap for gap in summary.gaps)
    assert any("has no NO2" in gap for gap in summary.gaps)
    assert {station.station for station in summary.stations} == {"S1", "S2"}
    assert summary.cleaning_log


def test_quality_summary_on_empty_tidy_table() -> None:
    raw = read_input(text="Station,Ward,Date,PM2.5 (µg/m³)\nS1,Ward 1,2026-11-03 07:00,10\n")
    empty = pd.DataFrame(columns=["row_id", "station", "ward", "ts", "pollutant", "value_raw", "unit_raw", "value_std", "flags"])
    summary = quality_summary(raw, empty, [])
    assert summary.n_readings == 0
    assert summary.stations == []
    assert summary.gaps


# --------------------------------------------------------------------------- sample files
DATA = pathlib.Path(__file__).resolve().parents[1] / "data"
WEEK1 = DATA / "sample_week1.csv"
WEEK2 = DATA / "sample_week2.csv"
needs_samples = pytest.mark.skipif(
    not (WEEK1.exists() and WEEK2.exists()), reason="sample data has not been generated"
)


def sample(name: str):
    path = DATA / name
    raw = read_input(file_bytes=path.read_bytes(), filename=name)
    tidy, log = apply_mapping(raw, heuristic_mapping(raw))
    return flag_readings(tidy), log, raw


def flag_at(tidy: pd.DataFrame, row_id: str, pollutant: str) -> list[str]:
    found = tidy[(tidy["row_id"] == row_id) & (tidy["pollutant"] == pollutant)]
    assert len(found) == 1, f"{row_id}/{pollutant} should be exactly one reading"
    return list(found["flags"].iloc[0])


@needs_samples
def test_sample_week1_planted_faults_are_detected() -> None:
    tidy, log, raw = sample("sample_week1.csv")
    assert len(raw.df) == 567
    assert raw.notes and any("title row" in note for note in raw.notes)
    assert tidy["station"].nunique() == 8
    assert not tidy["ts"].isna().any()  # DD/MM, ISO, DD-MM and 12-hour times all parse
    assert sorted(tidy["pollutant"].unique()) == ["CO", "NO2", "O3", "PM10", "PM2.5", "SO2"]

    expected = {
        ("R102", "SO2"): "SENTINEL",
        ("R183", "NO2"): "SENTINEL",
        ("R154", "CO"): "SENTINEL",
        ("R268", "O3"): "SENTINEL",
        ("R215", "SO2"): "SENTINEL",
        ("R261", "SO2"): "SENTINEL",
        ("R382", "NO2"): "SENTINEL",
        ("R513", "PM10"): "SENTINEL",
        ("R429", "O3"): "MISSING",
        ("R272", "SO2"): "CENSORED",
        ("R132", "NO2"): "CENSORED",
        ("R435", "SO2"): "CENSORED",
        ("R475", "PM10"): "NEGATIVE",
        ("R20", "PM2.5"): "OUT_OF_RANGE",
        ("R40", "PM2.5"): "DUPLICATE",
        ("R146", "NO2"): "UNIT_CONVERTED",   # Shivajinagar writes ppb inside the cell
        ("R74", "CO"): "UNIT_CONVERTED",     # Katraj writes "1,836 µg/m3"
    }
    for (row_id, pollutant), flag in expected.items():
        assert flag in flag_at(tidy, row_id, pollutant), f"{row_id} {pollutant} should be {flag}"

    # Katraj writes CO in µg/m³ with a thousands separator, so it converts to mg/m³.
    katraj_co = tidy[(tidy["row_id"] == "R74") & (tidy["pollutant"] == "CO")].iloc[0]
    assert katraj_co["unit_raw"] == "µg/m3"
    thousands = str(katraj_co["value_raw"]).split()[0].replace(",", "")
    assert katraj_co["value_std"] == pytest.approx(float(thousands) / 1000)
    # Shivajinagar writes NO2 in ppb inside the cell: 1 ppb = 1.88 µg/m³ at 25 °C.
    shiv_no2 = tidy[(tidy["row_id"] == "R146") & (tidy["pollutant"] == "NO2")].iloc[0]
    assert "ppb" in str(shiv_no2["value_raw"])
    assert shiv_no2["value_std"] == pytest.approx(float(str(shiv_no2["value_raw"]).split()[0]) * 1.88)

    # A 9-hour stuck sensor, one duplicate row across all six pollutants.
    stuck = tidy[tidy["flags"].apply(lambda flags: "STUCK" in flags)]
    swargate_o3 = stuck[(stuck["station"] == "Swargate") & (stuck["pollutant"] == "O3")]
    assert len(swargate_o3) == 9
    duplicates = tidy[tidy["flags"].apply(lambda flags: "DUPLICATE" in flags)]
    assert len(duplicates) == 6

    # Ward spellings collapse per station.
    assert tidy.groupby("station")["ward"].nunique().max() == 1
    assert set(tidy[tidy["station"] == "Hadapsar"]["ward"]) == {"Ward 12"}

    summary = quality_summary(raw, tidy, log)
    assert any("Pashan" in gap and "no readings between" in gap for gap in summary.gaps)
    assert any("partial day" in gap for gap in summary.gaps)
    assert summary.flag_counts["SENTINEL"] == 8


@needs_samples
def test_sample_week2_planted_faults_are_detected() -> None:
    tidy, log, raw = sample("sample_week2.csv")
    assert len(raw.df) == 547
    assert not tidy["ts"].isna().any()

    expected = {
        ("R102", "NO2"): "SENTINEL",
        ("R181", "PM10"): "SENTINEL",
        ("R138", "O3"): "NEGATIVE",
        ("R216", "PM10"): "OUT_OF_RANGE",
        ("R15", "O3"): "OUT_OF_RANGE",
        ("R294", "NO2"): "MISSING",
        ("R90", "CO"): "DUPLICATE",
        ("R509", "SO2"): "CENSORED",
        ("R289", "PM2.5"): "STUCK",
        ("R538", "NO2"): "STUCK",
        ("R73", "CO"): "UNIT_CONVERTED",
    }
    for (row_id, pollutant), flag in expected.items():
        assert flag in flag_at(tidy, row_id, pollutant), f"{row_id} {pollutant} should be {flag}"

    summary = quality_summary(raw, tidy, log)
    stations = {station.station: station for station in summary.stations}
    assert stations["Nigdi"].label == "unreliable"  # two outages and a run of blank cells
    assert stations["Nigdi"].coverage < 0.7
    assert any("Nigdi" in gap and "no readings between" in gap for gap in summary.gaps)
    assert len(tidy[tidy["flags"].apply(lambda flags: "DUPLICATE" in flags)]) == 6
