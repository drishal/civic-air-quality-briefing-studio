"""Tests for the CPCB AQI module: breakpoints, categories, averaging, daily AQI."""
from __future__ import annotations

import math
import re

import pandas as pd
import pytest

from core.aqi import (
    AQI_COLUMNS,
    AVG_COLUMNS,
    BREAKPOINTS,
    MIN_HOURS,
    WHO_24H,
    category,
    category_color,
    category_emoji,
    daily_aqi,
    pollutant_averages,
    subindex,
    who_multiple,
)
from core.models import CATEGORIES, POLLUTANTS

DAY = "2026-11-03"
TIDY_COLUMNS = [
    "row_id", "station", "ward", "ts", "pollutant",
    "value_raw", "unit_raw", "value_std", "flags",
]

# First (Good) band of the PLAN.md section 3 table: (conc_lo, conc_hi).
GOOD_BAND = {
    "PM10": (0, 50),
    "PM2.5": (0, 30),
    "NO2": (0, 40),
    "O3": (0, 50),
    "CO": (0, 1.0),
    "SO2": (0, 40),
    "NH3": (0, 200),
}

# Concentration upper bounds exactly as printed in PLAN.md section 3 (top band open).
PRINTED_UPPER = {
    "PM2.5": [30, 60, 90, 120, 250],
    "PM10": [50, 100, 250, 350, 430],
    "NO2": [40, 80, 180, 280, 400],
    "SO2": [40, 80, 380, 800, 1600],
    "O3": [50, 100, 168, 208, 748],
    "CO": [1.0, 2.0, 10.0, 17.0, 34.0],
    "NH3": [200, 400, 800, 1200, 1800],
}


def make_tidy(rows: list[tuple]) -> pd.DataFrame:
    """Build a tidy table from (station, ts, pollutant, value) tuples."""
    records = []
    for i, (station, ts, pollutant, value) in enumerate(rows, start=1):
        records.append(
            {
                "row_id": f"R{i}",
                "station": station,
                "ward": "Ward 1",
                "ts": pd.Timestamp(ts),
                "pollutant": pollutant,
                "value_raw": None if value is None else str(value),
                "unit_raw": None,
                "value_std": None if value is None else float(value),
                "flags": [],
            }
        )
    return pd.DataFrame(records, columns=TIDY_COLUMNS)


def day_rows(station: str, pollutant: str, values: list, start_hour: int = 0) -> list[tuple]:
    """One reading per hour starting at start_hour."""
    return [
        (station, f"{DAY} {start_hour + h:02d}:00", pollutant, v)
        for h, v in enumerate(values)
    ]


BANDS = [
    (pollutant, index, band)
    for pollutant, bands in BREAKPOINTS.items()
    for index, band in enumerate(bands)
]


# --------------------------------------------------------------- breakpoints
def test_breakpoints_cover_every_pollutant_and_match_the_printed_table():
    assert set(BREAKPOINTS) == set(POLLUTANTS)          # Pb omitted
    assert "Pb" not in BREAKPOINTS
    for pollutant, (lo, hi) in GOOD_BAND.items():
        bands = BREAKPOINTS[pollutant]
        assert len(bands) == 6
        assert bands[0] == (lo, hi, 0, 50)
        assert bands[-1][2:] == (401, 500)
        assert math.isinf(bands[-1][1])
        # printed upper bounds, and each band starts where the previous ends
        assert [b[1] for b in bands[:-1]] == PRINTED_UPPER[pollutant]
        for previous, current in zip(bands, bands[1:]):
            assert current[0] == previous[1]
        for band in bands:
            assert band[2] < band[3]


@pytest.mark.parametrize("pollutant,index,band", BANDS, ids=str)
def test_band_upper_boundary(pollutant, index, band):
    lo, hi, ilo, ihi = band
    if math.isinf(hi):
        assert subindex(pollutant, lo * 2) == 500.0     # top band is capped
    else:
        assert subindex(pollutant, hi) == pytest.approx(ihi)


@pytest.mark.parametrize("pollutant,index,band", BANDS, ids=str)
def test_band_lower_boundary_belongs_to_the_lower_band(pollutant, index, band):
    lo = band[0]
    if index == 0:
        assert subindex(pollutant, lo) == 0.0
    else:
        assert subindex(pollutant, lo) == pytest.approx(BREAKPOINTS[pollutant][index - 1][3])


@pytest.mark.parametrize("pollutant,index,band", BANDS, ids=str)
def test_interpolation_inside_every_band(pollutant, index, band):
    lo, hi, ilo, ihi = band
    if math.isinf(hi):
        pytest.skip("top band is capped, covered by the cap test")
    mid = (lo + hi) / 2
    expected = ilo + (ihi - ilo) / (hi - lo) * (mid - lo)
    assert subindex(pollutant, mid) == pytest.approx(expected)


@pytest.mark.parametrize("pollutant", POLLUTANTS)
def test_values_in_the_gap_between_printed_bands_are_interpolated(pollutant):
    first_lo, first_hi, _, first_aqi_hi = BREAKPOINTS[pollutant][0]
    second = BREAKPOINTS[pollutant][1]
    gap_value = first_hi + (0.05 if first_hi <= 1 else 0.5)
    assert second[0] == first_hi
    result = subindex(pollutant, gap_value)
    assert result is not None
    assert result > first_aqi_hi
    assert result < second[3]


def test_pm25_gap_value_maps_into_the_satisfactory_band():
    # 30 is Good (50); 30.5 falls between the printed bands 30 and 31.
    assert subindex("PM2.5", 30) == pytest.approx(50.0)
    assert subindex("PM2.5", 30.5) == pytest.approx(51 + (100 - 51) / (60 - 30) * 0.5)
    assert subindex("PM2.5", 31) == pytest.approx(51 + (100 - 51) / (60 - 30) * 1)


@pytest.mark.parametrize("pollutant", POLLUTANTS)
def test_top_band_is_capped_at_500(pollutant):
    top_lo = BREAKPOINTS[pollutant][-1][0]
    assert subindex(pollutant, top_lo * 10 + 5) == 500.0
    assert subindex(pollutant, 1e9) == 500.0


def test_each_printed_boundary_hits_its_category_maximum():
    assert subindex("PM10", 50) == pytest.approx(50)
    assert subindex("PM2.5", 60) == pytest.approx(100)
    assert subindex("NO2", 180) == pytest.approx(200)
    assert subindex("SO2", 800) == pytest.approx(300)
    assert subindex("O3", 748) == pytest.approx(400)
    assert subindex("CO", 34) == pytest.approx(400)
    assert subindex("NH3", 1800) == pytest.approx(400)


def test_invalid_concentrations_return_none():
    assert subindex("PM2.5", -0.1) is None
    assert subindex("PM2.5", float("nan")) is None
    assert subindex("PM2.5", float("inf")) is None
    assert subindex("PM2.5", None) is None
    assert subindex("PM2.5", "abc") is None
    assert subindex("Pb", 1.0) is None          # Pb is out of scope in v1
    assert subindex("pm2.5", 30) is None        # canonical names only


# ----------------------------------------------------------------- categories
@pytest.mark.parametrize(
    "aqi,expected",
    [
        (0, "Good"),
        (25, "Good"),
        (50, "Good"),
        (50.1, "Satisfactory"),
        (51, "Satisfactory"),
        (100, "Satisfactory"),
        (101, "Moderately Polluted"),
        (200, "Moderately Polluted"),
        (201, "Poor"),
        (300, "Poor"),
        (301, "Very Poor"),
        (400, "Very Poor"),
        (401, "Severe"),
        (500, "Severe"),
        (900, "Severe"),
    ],
)
def test_category_edges(aqi, expected):
    assert category(aqi) == expected
    assert category(aqi) in CATEGORIES


def test_category_without_aqi_is_empty():
    assert category(float("nan")) == ""
    assert category(None) == ""


def test_every_category_has_a_distinct_colour_and_emoji():
    colors = [category_color(cat) for cat in CATEGORIES]
    emojis = [category_emoji(cat) for cat in CATEGORIES]
    assert all(re.fullmatch(r"#[0-9A-Fa-f]{6}", c) for c in colors)
    assert len(set(colors)) == len(CATEGORIES)
    assert len(set(emojis)) == len(CATEGORIES)
    assert all(emojis)


# ------------------------------------------------------------------- WHO lens
def test_who_24h_values():
    assert WHO_24H["PM2.5"] == 15
    assert WHO_24H["PM10"] == 45
    assert WHO_24H["NO2"] == 25
    assert WHO_24H["SO2"] == 40
    assert WHO_24H["O3"] == 100
    assert WHO_24H["CO"] == 4
    assert "NH3" not in WHO_24H


def test_who_multiple():
    assert who_multiple("PM2.5", 30) == pytest.approx(2.0)
    assert who_multiple("CO", 2) == pytest.approx(0.5)
    assert who_multiple("NH3", 100) is None
    assert who_multiple("PM2.5", float("nan")) is None
    assert who_multiple("PM2.5", -5) is None


# -------------------------------------------------------------- averaging
def test_24h_mean_and_columns():
    values = [float(v) for v in range(20, 44)]
    tidy = make_tidy(day_rows("S1", "PM2.5", values))
    avg = pollutant_averages(tidy)
    assert list(avg.columns) == AVG_COLUMNS
    assert len(avg) == 1
    row = avg.iloc[0]
    assert row["station"] == "S1"
    assert row["ward"] == "Ward 1"
    assert row["date"] == DAY
    assert row["pollutant"] == "PM2.5"
    assert row["n_hours"] == 24
    assert bool(row["valid"]) is True
    assert row["avg"] == pytest.approx(sum(values) / 24)


@pytest.mark.parametrize("n,valid", [(24, True), (16, True), (15, False), (6, False)])
def test_sixteen_hour_rule(n, valid):
    tidy = make_tidy(day_rows("S1", "NO2", [10.0] * n))
    row = pollutant_averages(tidy).iloc[0]
    assert row["n_hours"] == n
    assert bool(row["valid"]) is valid
    assert MIN_HOURS == 16


def test_several_readings_in_one_hour_are_averaged_first():
    rows = day_rows("S1", "PM2.5", [10.0] * 16)
    rows.append(("S1", f"{DAY} 08:30", "PM2.5", 30.0))
    row = pollutant_averages(make_tidy(rows)).iloc[0]
    assert row["n_hours"] == 16
    assert row["avg"] == pytest.approx((15 * 10 + 20) / 16)


@pytest.mark.parametrize("pollutant", ["O3", "CO"])
def test_o3_and_co_use_the_max_rolling_8h_mean(pollutant):
    values = [20.0] * 24
    for hour in range(12, 16):
        values[hour] = 200.0
    row = pollutant_averages(make_tidy(day_rows("S1", pollutant, values))).iloc[0]
    assert row["n_hours"] == 24
    # window 08:00-15:00 holds four low and four high hours
    assert row["avg"] == pytest.approx((4 * 20 + 4 * 200) / 8)


@pytest.mark.parametrize("pollutant", ["O3", "CO"])
def test_partial_rolling_window_uses_six_hours(pollutant):
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    tidy = make_tidy(day_rows("S1", pollutant, values, start_hour=8))
    row = pollutant_averages(tidy).iloc[0]
    assert row["n_hours"] == 6
    assert bool(row["valid"]) is False
    assert row["avg"] == pytest.approx(sum(values) / 6)


@pytest.mark.parametrize("pollutant", ["O3", "CO"])
def test_rolling_mean_needs_six_hours(pollutant):
    row = pollutant_averages(make_tidy(day_rows("S1", pollutant, [10.0] * 5))).iloc[0]
    assert row["n_hours"] == 5
    assert math.isnan(row["avg"])
    assert bool(row["valid"]) is False


def test_rolling_window_spans_a_real_clock_gap():
    # 8 valid hours, a gap, then 7 more: only the last window reaches 6 valid hours.
    rows = day_rows("S1", "O3", [10.0] * 8)
    rows += day_rows("S1", "O3", [30.0] * 7, start_hour=14)
    row = pollutant_averages(make_tidy(rows)).iloc[0]
    assert row["n_hours"] == 15
    assert bool(row["valid"]) is False
    assert row["avg"] == pytest.approx(30.0)


def test_nat_and_unusable_rows_are_dropped():
    rows = day_rows("S1", "PM2.5", [10.0] * 18)
    rows.append(("S1", pd.NaT, "PM2.5", 500.0))
    rows.append(("S1", f"{DAY} 20:00", "PM2.5", None))
    row = pollutant_averages(make_tidy(rows)).iloc[0]
    assert row["n_hours"] == 18
    assert row["avg"] == pytest.approx(10.0)


def test_averages_empty_input():
    empty = pd.DataFrame(columns=TIDY_COLUMNS)
    assert list(pollutant_averages(empty).columns) == AVG_COLUMNS
    assert pollutant_averages(empty).empty
    assert pollutant_averages(pd.DataFrame()).empty


def test_averages_one_row_per_pollutant_and_day():
    rows = day_rows("S1", "PM2.5", [100.0] * 24) + day_rows("S1", "NO2", [50.0] * 24)
    rows += day_rows("S1", "NO2", [60.0] * 24, start_hour=0)  # same station/pollutant key
    rows += [("S1", f"2026-11-04 {h:02d}:00", "PM2.5", 100.0) for h in range(24)]
    avg = pollutant_averages(make_tidy(rows))
    keys = list(zip(avg["date"], avg["pollutant"]))
    assert keys == [(DAY, "NO2"), (DAY, "PM2.5"), ("2026-11-04", "PM2.5")]


# ---------------------------------------------------------------- daily AQI
def valid_day_rows(station: str = "S1") -> list[tuple]:
    return (
        day_rows(station, "PM2.5", [120.0] * 24)
        + day_rows(station, "PM10", [60.0] * 24)
        + day_rows(station, "NO2", [80.0] * 24)
    )


def test_daily_aqi_is_the_worst_subindex():
    result = daily_aqi(make_tidy(valid_day_rows()))
    assert list(result.columns) == AQI_COLUMNS
    row = result.iloc[0]
    assert row["aqi"] == pytest.approx(300.0)
    assert row["dominant"] == "PM2.5"
    assert row["category"] == "Poor"
    assert row["n_pollutants"] == 3
    assert bool(row["valid"]) is True
    assert row["reason"] == ""
    assert row["subindices"] == pytest.approx({"PM2.5": 300.0, "PM10": 60.8, "NO2": 100.0})


def test_daily_aqi_needs_three_pollutants():
    rows = day_rows("S1", "PM2.5", [120.0] * 24) + day_rows("S1", "NO2", [80.0] * 24)
    row = daily_aqi(make_tidy(rows)).iloc[0]
    assert row["n_pollutants"] == 2
    assert bool(row["valid"]) is False
    assert row["reason"] == "only 2 pollutants valid"
    assert row["aqi"] == pytest.approx(300.0)   # still reported, just not valid


def test_daily_aqi_needs_pm25_or_pm10():
    rows = (
        day_rows("S1", "NO2", [80.0] * 24)
        + day_rows("S1", "SO2", [80.0] * 24)
        + day_rows("S1", "O3", [60.0] * 24)
    )
    row = daily_aqi(make_tidy(rows)).iloc[0]
    assert row["n_pollutants"] == 3
    assert bool(row["valid"]) is False
    assert row["reason"] == "no PM2.5/PM10"


def test_daily_aqi_drops_pollutants_under_sixteen_hours():
    rows = (
        day_rows("S1", "PM2.5", [120.0] * 11)
        + day_rows("S1", "PM10", [60.0] * 24)
        + day_rows("S1", "NO2", [80.0] * 24)
    )
    row = daily_aqi(make_tidy(rows)).iloc[0]
    assert bool(row["valid"]) is False
    assert row["n_pollutants"] == 2
    assert "PM2.5" not in row["subindices"]
    assert "only 2 pollutants valid" in row["reason"]
    assert "PM2.5 has 11 valid hours (<16)" in row["reason"]


def test_daily_aqi_accepts_seventeen_hours():
    rows = (
        day_rows("S1", "PM2.5", [120.0] * 17)
        + day_rows("S1", "PM10", [60.0] * 24)
        + day_rows("S1", "NO2", [80.0] * 24)
    )
    row = daily_aqi(make_tidy(rows)).iloc[0]
    assert bool(row["valid"]) is True
    assert row["reason"] == ""
    assert row["aqi"] == pytest.approx(300.0)


def test_daily_aqi_returns_the_row_for_an_unusable_day():
    tidy = make_tidy(
        [("S1", f"{DAY} {h:02d}:00", "PM2.5", None) for h in range(24)]
        + [("S1", f"{DAY} {h:02d}:00", "PM10", -5.0) for h in range(24)]
    )
    result = daily_aqi(tidy)
    assert len(result) == 1
    row = result.iloc[0]
    assert math.isnan(row["aqi"])
    assert row["category"] == ""
    assert row["dominant"] is None
    assert row["n_pollutants"] == 0
    assert row["subindices"] == {}
    assert bool(row["valid"]) is False
    assert "no valid pollutants" in row["reason"]
    assert "no PM2.5/PM10" in row["reason"]


def test_daily_aqi_one_row_per_station_and_date():
    rows = valid_day_rows("S1")
    rows += [("S2", f"2026-11-04 {h:02d}:00", "PM2.5", 120.0) for h in range(24)]
    rows += [("S2", f"2026-11-04 {h:02d}:00", "PM10", 60.0) for h in range(24)]
    result = daily_aqi(make_tidy(rows))
    assert list(zip(result["station"], result["date"])) == [
        ("S1", DAY),
        ("S2", "2026-11-04"),
    ]
    assert list(result["ward"]) == ["Ward 1", "Ward 1"]
    assert bool(result.iloc[1]["valid"]) is False


def test_daily_aqi_drops_nat_rows():
    rows = valid_day_rows("S1") + [("S2", pd.NaT, "PM2.5", 120.0)]
    result = daily_aqi(make_tidy(rows))
    assert list(result["station"]) == ["S1"]


def test_daily_aqi_empty_input():
    result = daily_aqi(pd.DataFrame(columns=TIDY_COLUMNS))
    assert list(result.columns) == AQI_COLUMNS
    assert result.empty
    assert daily_aqi(pd.DataFrame()).empty


def test_reduced_tidy_frame_without_ward_column_still_works():
    tidy = make_tidy(valid_day_rows()).drop(columns=["ward", "flags", "value_raw"])
    result = daily_aqi(tidy)
    assert len(result) == 1
    assert result.iloc[0]["ward"] is None
    assert bool(result.iloc[0]["valid"]) is True
    assert len(pollutant_averages(tidy)) == 3


def test_daily_aqi_dominant_tie_breaks_on_pollutant_order():
    # PM2.5 30 -> 50 and NH3 200 -> 50: PM2.5 comes first in POLLUTANTS
    rows = day_rows("S1", "PM2.5", [30.0] * 24) + day_rows("S1", "PM10", [10.0] * 24)
    rows += day_rows("S1", "NH3", [200.0] * 24)
    row = daily_aqi(make_tidy(rows)).iloc[0]
    assert row["subindices"]["PM2.5"] == pytest.approx(50.0)
    assert row["subindices"]["NH3"] == pytest.approx(50.0)
    assert row["dominant"] == "PM2.5"
    assert row["category"] == "Good"
    assert bool(row["valid"]) is True


def test_daily_aqi_uses_each_pollutants_own_averaging():
    # O3 400 for four midday hours: peak 8h mean 200 -> sub-index 301 + 99/540*... above 301
    values = [20.0] * 24
    for hour in range(10, 14):
        values[hour] = 400.0
    rows = day_rows("S1", "O3", values) + day_rows("S1", "PM2.5", [10.0] * 24)
    rows += day_rows("S1", "NO2", [10.0] * 24)
    row = daily_aqi(make_tidy(rows)).iloc[0]
    peak = (4 * 20 + 4 * 400) / 8
    expected = 301 + (400 - 301) / (748 - 208) * (peak - 208)
    assert row["subindices"]["O3"] == pytest.approx(round(expected, 1))
    assert row["dominant"] == "O3"
