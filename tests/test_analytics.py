"""Analytics tests on small synthetic tidy tables.

`core/aqi.py` is owned by another agent. These tests use the real module when it
is importable and otherwise install a temporary, test-only stub in
`sys.modules` (never written to disk) so analytics can be tested in isolation.
"""
from __future__ import annotations

import math
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from core.models import QualitySummary, StationReliability

# ---------------------------------------------------------------- AQI availability
_REQUIRED = ("subindex", "category", "WHO_24H", "who_multiple", "pollutant_averages", "daily_aqi")
_COLUMNS = [
    "station", "ward", "date", "aqi", "category", "dominant",
    "n_pollutants", "subindices", "valid", "reason",
]


def _make_aqi_stub() -> types.ModuleType:
    """Minimal CPCB AQI implementation, only for tests."""
    module = types.ModuleType("core.aqi")
    module.BREAKPOINTS = {
        "PM2.5": [(0, 30, 0, 50), (30, 60, 51, 100), (60, 90, 101, 200), (90, 120, 201, 300), (120, 250, 301, 400), (250, 500, 401, 500)],
        "PM10": [(0, 50, 0, 50), (50, 100, 51, 100), (100, 250, 101, 200), (250, 350, 201, 300), (350, 430, 301, 400), (430, 600, 401, 500)],
        "NO2": [(0, 40, 0, 50), (40, 80, 51, 100), (80, 180, 101, 200), (180, 280, 201, 300), (280, 400, 301, 400), (400, 600, 401, 500)],
        "SO2": [(0, 40, 0, 50), (40, 80, 51, 100), (80, 380, 101, 200), (380, 800, 201, 300), (800, 1600, 301, 400), (1600, 2000, 401, 500)],
        "O3": [(0, 50, 0, 50), (50, 100, 51, 100), (100, 168, 101, 200), (168, 208, 201, 300), (208, 748, 301, 400), (748, 1000, 401, 500)],
        "CO": [(0, 1, 0, 50), (1, 2, 51, 100), (2, 10, 101, 200), (10, 17, 201, 300), (17, 34, 301, 400), (34, 50, 401, 500)],
        "NH3": [(0, 200, 0, 50), (200, 400, 51, 100), (400, 800, 101, 200), (800, 1200, 201, 300), (1200, 1800, 301, 400), (1800, 2500, 401, 500)],
    }
    module.WHO_24H = {"PM2.5": 15, "PM10": 45, "NO2": 25, "SO2": 40, "O3": 100, "CO": 4}

    def subindex(pollutant, conc):
        brackets = module.BREAKPOINTS.get(pollutant)
        if brackets is None or conc is None or not math.isfinite(conc) or conc < 0:
            return None
        for lo, hi, aqi_lo, aqi_hi in brackets:
            if lo <= conc <= hi:
                if hi == lo:
                    return float(aqi_hi)
                return aqi_lo + (aqi_hi - aqi_lo) * (conc - lo) / (hi - lo)
        return 500.0

    def category(aqi):
        if aqi is None or not math.isfinite(aqi):
            return "Unknown"
        for limit, name in ((50, "Good"), (100, "Satisfactory"), (200, "Moderately Polluted"),
                            (300, "Poor"), (400, "Very Poor")):
            if aqi <= limit:
                return name
        return "Severe"

    def who_multiple(pollutant, conc):
        limit = module.WHO_24H.get(pollutant)
        if limit is None or conc is None or not math.isfinite(conc):
            return None
        return conc / limit

    def pollutant_averages(tidy):
        columns = ["station", "ward", "date", "pollutant", "avg", "n_hours", "valid"]
        if tidy is None or tidy.empty:
            return pd.DataFrame(columns=columns)
        df = tidy[tidy["value_std"].notna() & tidy["ts"].notna()].copy()
        if df.empty:
            return pd.DataFrame(columns=columns)
        df["date"] = df["ts"].dt.strftime("%Y-%m-%d")
        df["hour"] = df["ts"].dt.hour
        grouped = (
            df.groupby(["station", "ward", "date", "pollutant"], dropna=False)
            .agg(avg=("value_std", "mean"), n_hours=("hour", "nunique"))
            .reset_index()
        )
        grouped["valid"] = grouped["n_hours"] >= 16
        return grouped

    def daily_aqi(tidy):
        averages = pollutant_averages(tidy)
        rows = []
        if averages.empty:
            return pd.DataFrame(columns=_COLUMNS)
        for (station, date), group in averages.groupby(["station", "date"], dropna=False):
            subs = {}
            for _, row in group[group["valid"]].iterrows():
                value = subindex(row["pollutant"], float(row["avg"]))
                if value is not None:
                    subs[row["pollutant"]] = value
            ward = next((w for w in group["ward"].tolist() if isinstance(w, str)), None)
            if len(subs) < 3 or not ({"PM2.5", "PM10"} & set(subs)):
                rows.append({"station": station, "ward": ward, "date": str(date), "aqi": float("nan"),
                             "category": None, "dominant": None, "n_pollutants": len(subs),
                             "subindices": subs, "valid": False,
                             "reason": "needs 3 valid pollutants including PM2.5 or PM10"})
                continue
            dominant = max(subs, key=lambda name: subs[name])
            rows.append({"station": station, "ward": ward, "date": str(date), "aqi": subs[dominant],
                         "category": category(subs[dominant]), "dominant": dominant,
                         "n_pollutants": len(subs), "subindices": subs, "valid": True, "reason": ""})
        return pd.DataFrame(rows, columns=_COLUMNS)

    module.subindex = subindex
    module.category = category
    module.who_multiple = who_multiple
    module.pollutant_averages = pollutant_averages
    module.daily_aqi = daily_aqi
    return module


def _ensure_aqi() -> None:
    """Use the real module; only substitute the stub when core/aqi.py is absent."""
    try:
        import core.aqi as real
    except ModuleNotFoundError:
        stub = _make_aqi_stub()
        sys.modules["core.aqi"] = stub
        import core

        core.aqi = stub
        return
    missing = [name for name in _REQUIRED if not hasattr(real, name)]
    if missing:
        raise RuntimeError(f"core.aqi exists but is missing {missing}")


_ensure_aqi()

from core import analytics  # noqa: E402

# ---------------------------------------------------------------- builders
POLLS = ("PM2.5", "PM10", "NO2")


def _cell(spec, hour: int, day_index: int) -> float:
    if callable(spec):
        return float(spec(hour, day_index))
    return float(spec)


def make_tidy(
    levels: dict[str, dict[str, object]],
    days: tuple[str, ...] = ("2026-11-01",),
    wards: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Tidy table; values may be scalars or callables `(hour, day_index) -> value`."""
    wards = wards or {}
    rows = []
    counter = 0
    for day_index, day in enumerate(days):
        start = pd.Timestamp(day)
        for hour in range(24):
            ts = start + pd.Timedelta(hours=hour)
            for station in sorted(levels):
                for pollutant in POLLS:
                    if pollutant not in levels[station]:
                        continue
                    value = _cell(levels[station][pollutant], hour, day_index)
                    counter += 1
                    rows.append(
                        {
                            "row_id": f"R{counter}",
                            "station": station,
                            "ward": wards.get(station),
                            "ts": ts,
                            "pollutant": pollutant,
                            "value_raw": f"{value:g}",
                            "unit_raw": "µg/m³",
                            "value_std": value,
                            "flags": [],
                        }
                    )
    return pd.DataFrame(rows)


def make_quality(
    stations: list[str],
    labels: dict[str, str] | None = None,
    gaps: list[str] | None = None,
) -> QualitySummary:
    labels = labels or {}
    reliability = {"reliable": 0.92, "use with caution": 0.6, "unreliable": 0.2}
    rels = []
    for station in stations:
        label = labels.get(station, "reliable")
        rels.append(
            StationReliability(
                station=station,
                n_readings=72,
                coverage=0.95 if label == "reliable" else 0.4,
                flagged_share=0.0 if label == "reliable" else 0.3,
                reliability=reliability[label],
                label=label,
                notes=[f"{station} has a 10 hour outage"] if label != "reliable" else [],
            )
        )
    return QualitySummary(
        n_rows_in=1000,
        n_readings=72 * len(stations),
        flag_counts={},
        stations=rels,
        gaps=list(gaps or []),
        cleaning_log=[],
    )


def find(findings: list, kind: str, station: str | None = None) -> list:
    """Findings of a type, optionally touching a station."""
    return [
        f
        for f in findings
        if f.type == kind and (station is None or station in f.stations)
    ]


def three_stations() -> dict[str, dict[str, object]]:
    return {
        "A": {"PM2.5": 150.0, "PM10": 200.0, "NO2": 60.0},
        "B": {"PM2.5": 70.0, "PM10": 150.0, "NO2": 50.0},
        "C": {"PM2.5": 35.0, "PM10": 80.0, "NO2": 30.0},
    }


WARD = {"A": "Ward 1", "B": "Ward 2", "C": "Ward 3"}


# ---------------------------------------------------------------- tests
def test_hotspot_ranking_is_ordered_and_cited():
    tidy = make_tidy(three_stations(), wards=WARD)
    quality = make_quality(["A", "B", "C"])
    brief = analytics.build_brief(tidy, quality, "Pune", "sample.csv", "abc123")

    assert brief.area == "Pune"
    assert brief.source_hash == "abc123"
    assert [s.station for s in brief.station_aqi] == ["A", "B", "C"]
    assert [s.aqi for s in brief.station_aqi] == sorted(
        [s.aqi for s in brief.station_aqi], reverse=True
    )

    hotspots = find(brief.findings, "hotspot")
    assert [f.stations[0] for f in hotspots] == ["A", "B", "C"]
    assert hotspots[0].id == "F1"
    assert hotspots[0].confidence == "high"
    assert hotspots[0].facts["aqi"] == brief.station_aqi[0].aqi
    assert "aqi" in hotspots[0].facts and "reliability" in hotspots[0].facts
    assert hotspots[0].formula

    row_ids = set(tidy["row_id"])
    for finding in brief.findings:
        assert finding.id.startswith("F")
        assert len(finding.evidence_row_ids) <= 50
        assert set(finding.evidence_row_ids) <= row_ids
    assert [f.id for f in brief.findings] == [f"F{i}" for i in range(1, len(brief.findings) + 1)]


def test_unreliable_station_never_high_confidence_and_gets_caveat():
    tidy = make_tidy(three_stations(), wards=WARD)
    quality = make_quality(["A", "B", "C"], labels={"A": "unreliable"})
    brief = analytics.build_brief(tidy, quality, "Pune", "sample.csv", "abc123")

    station_a = next(s for s in brief.station_aqi if s.station == "A")
    assert station_a.confidence == "low"
    hotspot_a = find(brief.findings, "hotspot", "A")[0]
    assert hotspot_a.confidence == "low"
    assert hotspot_a.caveats
    assert any("unreliable" in caveat for caveat in hotspot_a.caveats)
    # The unreliability is reported as a finding too.
    gap_a = [f for f in find(brief.findings, "gap") if f.stations == ["A"]]
    assert gap_a and gap_a[0].facts["reliability"] == 0.2
    # No pollution finding built from A's data may headline at high confidence.
    for kind in ("hotspot", "exceedance", "trend", "pattern"):
        for finding in find(brief.findings, kind, "A"):
            assert finding.confidence != "high"


def test_trend_direction_up_and_down():
    days = ("2026-11-01", "2026-11-02", "2026-11-03")
    levels = {
        "A": {"PM2.5": lambda hour, day: (60.0, 90.0, 120.0)[day], "PM10": 100.0, "NO2": 40.0},
        "B": {"PM2.5": lambda hour, day: (120.0, 90.0, 60.0)[day], "PM10": 100.0, "NO2": 40.0},
    }
    tidy = make_tidy(levels, days=days, wards={"A": "Ward 1", "B": "Ward 2"})
    brief = analytics.build_brief(tidy, make_quality(["A", "B"]), "Pune", "week.csv", "h1")

    station_a = next(s for s in brief.station_aqi if s.station == "A")
    assert station_a.n_valid_days == 3
    assert station_a.mean_aqi_period is not None
    assert station_a.trend_per_day > 1.0

    trend_a = find(brief.findings, "trend", "A")[0]
    assert trend_a.facts["direction"] == "up"
    assert trend_a.facts["slope_per_day"] > 1.0
    assert trend_a.facts["first_aqi"] < trend_a.facts["last_aqi"]
    trend_b = find(brief.findings, "trend", "B")[0]
    assert trend_b.facts["direction"] == "down"
    assert trend_b.facts["slope_per_day"] < -1.0


def test_exceedance_reports_who_multiples():
    levels = {
        "A": {"PM2.5": 30.0, "PM10": 50.0, "NO2": 20.0},
        "B": {"PM2.5": 30.0, "PM10": 50.0, "NO2": 20.0},
    }
    tidy = make_tidy(levels, wards={"A": "Ward 1", "B": "Ward 2"})
    brief = analytics.build_brief(tidy, make_quality(["A", "B"]), "Pune", "w.csv", "h2")

    exceed = find(brief.findings, "exceedance")
    pm25 = [f for f in exceed if f.facts.get("pollutant") == "PM2.5"][0]
    assert pm25.facts["who_24h_limit"] == 15.0
    assert pm25.facts["max_multiple"] == 2.0
    assert pm25.facts["n_exceeding"] == 2
    assert pm25.facts["n_stations"] == 2
    assert pm25.facts["max_station"] in {"A", "B"}
    assert pm25.evidence_row_ids
    pm10 = [f for f in exceed if f.facts.get("pollutant") == "PM10"][0]
    assert pm10.facts["max_multiple"] == round(50 / 45, 2)
    # NO2 at 20 is below the WHO limit of 25: no finding.
    assert not [f for f in exceed if f.facts.get("pollutant") == "NO2"]


def test_hour_of_day_pattern_finds_evening_peak():
    evening = lambda hour, day: 120.0 if 19 <= hour <= 21 else 20.0  # noqa: E731
    levels = {
        "A": {"PM2.5": evening, "PM10": 30.0, "NO2": 20.0},
        "B": {"PM2.5": evening, "PM10": 30.0, "NO2": 20.0},
    }
    tidy = make_tidy(levels, wards={"A": "Ward 1", "B": "Ward 2"})
    brief = analytics.build_brief(tidy, make_quality(["A", "B"]), "Pune", "w.csv", "h3")

    pattern = find(brief.findings, "pattern")[0]
    assert pattern.facts["pollutant"] == "PM2.5"
    assert pattern.facts["peak_hour"] in {19, 20, 21}
    assert pattern.facts["window_start"] in {18, 19, 20}
    assert pattern.facts["peak_mean"] > pattern.facts["trough_mean"]
    assert pattern.facts["peak_to_trough"] is not None
    assert pattern.evidence_row_ids
    assert any("consistent with" in caveat for caveat in pattern.caveats)


def test_findings_are_deterministic_and_complete():
    tidy = make_tidy(three_stations(), days=("2026-11-01", "2026-11-02"), wards=WARD)
    quality = make_quality(["A", "B", "C"], gaps=["Station C has no readings between 02:00 and 09:00"])
    first = analytics.build_brief(tidy, quality, "Pune", "w.csv", "h4")
    second = analytics.build_brief(tidy, quality, "Pune", "w.csv", "h4")

    assert [f.model_dump() for f in first.findings] == [f.model_dump() for f in second.findings]
    assert first.findings
    for finding in first.findings:
        assert finding.type in {"hotspot", "trend", "exceedance", "gap", "anomaly", "pattern"}
        assert finding.confidence in {"high", "medium", "low"}
        assert finding.facts
        assert finding.formula
        assert len(finding.evidence_row_ids) <= 50
    gap_titles = [f.title for f in find(first.findings, "gap")]
    assert "Data gaps limit what this brief can say" in gap_titles


def test_anomaly_flags_stuck_and_spike_rows():
    tidy = make_tidy({"A": {"PM2.5": 50.0, "PM10": 90.0, "NO2": 30.0}}, wards={"A": "Ward 1"})
    for index in tidy.index[:8]:
        tidy.at[index, "flags"] = ["STUCK"]
    brief = analytics.build_brief(tidy, make_quality(["A"]), "Pune", "w.csv", "h5")

    anomaly = find(brief.findings, "anomaly", "A")[0]
    assert anomaly.facts["n_stuck"] == 8
    assert anomaly.facts["n_spike"] == 0
    assert anomaly.evidence_row_ids


def test_empty_tidy_builds_a_brief_with_an_explicit_no_readings_gap():
    brief = analytics.build_brief(pd.DataFrame(), make_quality([]), "Pune", "empty.csv", "h6")
    # An empty table must never raise, and must say plainly that nothing was usable.
    assert [f.title for f in brief.findings] == ["No usable readings"]
    assert brief.findings[0].type == "gap"
    assert brief.findings[0].facts["n_usable"] == 0
    assert brief.findings[0].caveats
    assert brief.station_aqi == []
    assert brief.period_start is None

# ---------------------------------------------------------------- traceability
DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def make_traceable_case() -> tuple:
    """Three rising days per station, an outage at C and a few flagged readings."""
    days = ("2026-11-01", "2026-11-02", "2026-11-03")
    levels = {
        "A": {"PM2.5": lambda hour, day: 120.0 + 30.0 * day, "PM10": 200.0, "NO2": 60.0},
        "B": {"PM2.5": lambda hour, day: 80.0 + 20.0 * day, "PM10": 150.0, "NO2": 50.0},
        "C": {"PM2.5": lambda hour, day: 40.0 + 15.0 * day, "PM10": 90.0, "NO2": 30.0},
    }
    tidy = make_tidy(levels, days=days, wards=WARD)
    outage = (
        (tidy["station"] == "C")
        & (tidy["ts"].dt.strftime("%Y-%m-%d") == days[1])
        & (tidy["ts"].dt.hour >= 3)
        & (tidy["ts"].dt.hour < 9)
    )
    tidy = tidy[~outage].reset_index(drop=True)
    tidy.loc[tidy.index[:2], "flags"] = [["MISSING"]]
    tidy.loc[tidy.index[2:4], "flags"] = [["SENTINEL"]]
    quality = make_quality(
        ["A", "B", "C"],
        labels={"C": "unreliable"},
        gaps=["Station C has no readings between 03:00 and 09:00 on 2026-11-02."],
    )
    return tidy, quality


def test_every_finding_carries_traceable_evidence():
    tidy, quality = make_traceable_case()
    brief = analytics.build_brief(tidy, quality, "Pune", "w.csv", "h7")
    row_ids = set(tidy["row_id"])

    assert brief.findings
    for finding in brief.findings:
        assert finding.evidence_row_ids, f"{finding.id} {finding.title} has no evidence"
        assert len(finding.evidence_row_ids) <= 50
        assert set(finding.evidence_row_ids) <= row_ids
        assert len(set(finding.evidence_row_ids)) == len(finding.evidence_row_ids)

    trend = next(f for f in brief.findings if f.type == "trend" and "A" in f.stations)
    assert trend.evidence_row_ids
    assert "slope_per_day" in trend.facts and "n_days" in trend.facts
    assert trend.facts["n_days"] >= 3
    assert trend.evidence_row_ids

    # The outage is rows that do not exist: its neighbours are the evidence and
    # the window itself is stated in facts.
    gap_c = next(f for f in find(brief.findings, "gap") if f.stations == ["C"])
    assert gap_c.evidence_row_ids
    assert gap_c.facts["hours_missing"] == 6
    assert gap_c.facts["gap_start"].startswith("2026-11-02 02:00")
    assert gap_c.facts["gap_end"].startswith("2026-11-02 09:00")
    assert any("no rows" in caveat for caveat in gap_c.caveats)

    overall = [f for f in find(brief.findings, "gap") if not f.stations]
    assert not overall or overall[0].evidence_row_ids
    summary_gap = next(f for f in find(brief.findings, "gap") if f.title.startswith("Data gaps"))
    assert summary_gap.evidence_row_ids
    assert {"gap_start", "gap_end", "hours_missing"} <= set(summary_gap.facts)


def test_trend_needs_three_valid_days():
    days = ("2026-11-01", "2026-11-02")
    levels = {
        "A": {"PM2.5": lambda hour, day: 60.0 + 60.0 * day, "PM10": 100.0, "NO2": 40.0},
        "B": {"PM2.5": lambda hour, day: 40.0 + 60.0 * day, "PM10": 100.0, "NO2": 40.0},
    }
    tidy = make_tidy(levels, days=days, wards=WARD)
    brief = analytics.build_brief(tidy, make_quality(["A", "B"]), "Pune", "w.csv", "h8")
    assert find(brief.findings, "trend") == []
    # Station statistics still carry the slope.
    assert next(s for s in brief.station_aqi if s.station == "A").trend_per_day is not None


def rising(rise: float):
    """PM2.5 value that climbs by `rise` per day."""
    return lambda hour, day: 60.0 + rise * day


def test_trend_findings_are_capped_at_the_steepest_four():
    days = ("2026-11-01", "2026-11-02", "2026-11-03")
    rises = {"A": 40.0, "B": 30.0, "C": 20.0, "D": 15.0, "E": 2.0}
    levels = {
        station: {
            "PM2.5": rising(rises[station]),
            "PM10": 100.0,
            "NO2": 40.0,
        }
        for station in rises
    }
    tidy = make_tidy(levels, days=days, wards={s: f"Ward {i + 1}" for i, s in enumerate(rises)})
    brief = analytics.build_brief(tidy, make_quality(sorted(rises)), "Pune", "w.csv", "h9")
    trends = find(brief.findings, "trend")
    assert len(trends) <= 4
    assert [f.facts["slope_per_day"] for f in trends] == sorted(
        (f.facts["slope_per_day"] for f in trends), reverse=True
    )
    assert all(f.evidence_row_ids for f in trends)


@pytest.mark.skipif(
    not (DATA_DIR / "sample_week1.csv").exists(), reason="sample_week1.csv is not present"
)
def test_real_sample_brief_evidence_is_traceable():
    from core import clean, ingest, quality

    path = DATA_DIR / "sample_week1.csv"
    raw = ingest.read_input(filename=path.name, file_bytes=path.read_bytes())
    mapping = ingest.heuristic_mapping(raw)
    tidy, log = clean.apply_mapping(raw, mapping)
    tidy = quality.flag_readings(tidy)
    summary = quality.quality_summary(raw, tidy, log)
    brief = analytics.build_brief(tidy, summary, "Pune", path.name, "samplehash")

    row_ids = set(tidy["row_id"])
    assert brief.findings
    for finding in brief.findings:
        assert finding.evidence_row_ids, f"{finding.id} {finding.title} has no evidence"
        assert set(finding.evidence_row_ids) <= row_ids
        assert len(finding.evidence_row_ids) <= 50
