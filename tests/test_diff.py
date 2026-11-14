"""Diff tests: brief vs previous brief, and the first-brief case."""
from __future__ import annotations

from core.diff import diff_briefs
from core.store import list_briefs, load_latest, save_brief
from core.models import (
    Brief,
    Finding,
    QualitySummary,
    StationAQI,
    StationReliability,
)


def station(name: str, aqi: float | None, category: str | None, ward: str | None = None) -> StationAQI:
    return StationAQI(station=name, ward=ward, aqi=aqi, category=category, confidence="high", reliability=0.9)


def reliability(name: str, label: str = "reliable") -> StationReliability:
    scores = {"reliable": 0.9, "use with caution": 0.6, "unreliable": 0.2}
    return StationReliability(
        station=name,
        n_readings=72,
        coverage=0.95,
        flagged_share=0.1,
        reliability=scores[label],
        label=label,
    )


def make_brief(
    brief_id: str,
    created_at: str,
    stations: list[StationAQI],
    labels: dict[str, str] | None = None,
    gaps: list[str] | None = None,
    findings: list[Finding] | None = None,
) -> Brief:
    labels = labels or {}
    names = [s.station for s in stations]
    return Brief(
        brief_id=brief_id,
        created_at=created_at,
        area="Pune",
        source_name="week.csv",
        source_hash="hash",
        station_aqi=stations,
        findings=findings or [],
        quality=QualitySummary(
            n_rows_in=100,
            n_readings=72 * len(names),
            stations=[reliability(name, labels.get(name, "reliable")) for name in names],
            gaps=list(gaps or []),
        ),
    )


def change_for(result, name: str):
    return next(c for c in result.station_changes if c.station == name)


def test_first_brief_without_previous():
    current = make_brief(
        "B2",
        "2026-11-03T10:00:00+00:00",
        [station("A", 210.0, "Poor"), station("B", 80.0, "Satisfactory")],
        findings=[Finding(id="F1", type="hotspot", title="Hotspot: A", formula="x")],
    )
    result = diff_briefs(current, None)

    assert result.has_previous is False
    assert result.previous_brief_id is None
    assert result.station_changes == []
    assert result.summary_facts["n_stations"] == 2
    assert result.summary_facts["n_findings"] == 1
    assert result.summary_facts["max_aqi"] == 210.0
    assert result.summary_facts["worst_station"] == "A"
    assert result.new_hotspots == []


def test_diff_classifies_changes_and_hotspots():
    previous = make_brief(
        "B1",
        "2026-11-02T10:00:00+00:00",
        [
            station("A", 180.0, "Moderately Polluted"),
            station("B", 100.0, "Satisfactory"),
            station("C", 50.0, "Good"),
            station("E", 200.0, "Moderately Polluted"),
        ],
        labels={"B": "reliable"},
        gaps=["gap one"],
        findings=[
            Finding(id="F1", type="hotspot", title="Hotspot: E", formula="x"),
            Finding(id="F2", type="trend", title="A: AQI trend up", formula="x"),
        ],
    )
    current = make_brief(
        "B2",
        "2026-11-03T10:00:00+00:00",
        [
            station("A", 230.0, "Poor"),
            station("B", 95.0, "Satisfactory"),
            station("D", 300.0, "Very Poor"),
            station("E", 150.0, "Moderately Polluted"),
        ],
        labels={"B": "use with caution"},
        gaps=["gap one", "gap two"],
        findings=[
            Finding(id="F1", type="hotspot", title="Hotspot: D", formula="x"),
        ],
    )

    result = diff_briefs(current, previous)

    assert result.has_previous is True
    assert result.previous_brief_id == "B1"

    assert change_for(result, "A").direction == "worse"
    assert change_for(result, "A").delta == 50.0
    assert change_for(result, "B").direction == "same"
    assert change_for(result, "B").delta == -5.0
    assert change_for(result, "E").direction == "better"
    assert change_for(result, "E").delta == -50.0
    assert change_for(result, "D").direction == "new"
    assert change_for(result, "D").aqi_prev is None
    assert change_for(result, "C").direction == "dropped"

    assert result.new_hotspots == ["D"]
    assert result.resolved_hotspots == ["B"]
    assert result.category_transitions == ["A: Moderately Polluted -> Poor"]
    assert "B reliability: reliable -> use with caution" in result.quality_changes
    assert "Data gap notes: 1 -> 2" in result.quality_changes
    assert result.new_finding_titles == ["Hotspot: D"]

    facts = result.summary_facts
    assert facts["n_worse"] == 1
    assert facts["n_better"] == 1
    assert facts["n_same"] == 1
    assert facts["n_new"] == 1
    assert facts["n_dropped"] == 1
    assert facts["mean_delta"] == round((50.0 - 5.0 - 50.0) / 3, 1)
    assert facts["worst_station"] == "A"
    assert facts["worst_delta"] == 50.0
    assert facts["best_station"] == "E"
    assert facts["max_aqi_now"] == 300.0
    assert facts["n_new_findings"] == 1
    assert facts["n_new_hotspots"] == 1
    assert facts["n_resolved_hotspots"] == 1


def test_diff_handles_missing_aqi_and_identical_briefs():
    previous = make_brief(
        "B1", "2026-11-02T10:00:00+00:00", [station("A", 120.0, "Moderately Polluted")]
    )
    current = make_brief(
        "B2", "2026-11-03T10:00:00+00:00", [station("A", None, None)]
    )
    result = diff_briefs(current, previous)
    assert change_for(result, "A").direction == "unknown"
    assert change_for(result, "A").delta is None

    stable = make_brief(
        "B3", "2026-11-03T10:00:00+00:00", [station("A", 140.0, "Moderately Polluted")]
    )
    same = diff_briefs(stable, stable)
    assert same.has_previous is True
    assert same.station_changes[0].direction == "same"
    assert same.station_changes[0].delta == 0.0
    assert same.new_hotspots == []
    assert same.resolved_hotspots == []


def test_station_changes_are_sorted_worst_first():
    previous = make_brief(
        "B1",
        "2026-11-02T10:00:00+00:00",
        [station("A", 100.0, "Satisfactory"), station("B", 100.0, "Satisfactory")],
    )
    current = make_brief(
        "B2",
        "2026-11-03T10:00:00+00:00",
        [station("A", 200.0, "Poor"), station("B", 85.0, "Satisfactory")],
    )
    result = diff_briefs(current, previous)
    assert [c.station for c in result.station_changes] == ["A", "B"]


def test_store_saves_lists_and_loads_latest(tmp_path):
    older = make_brief("B1", "2026-11-01T10:00:00+00:00", [station("A", 100.0, "Satisfactory")])
    newer = make_brief("B2", "2026-11-02T10:00:00+00:00", [station("A", 140.0, "Moderately Polluted")])
    path_old = save_brief(older, tmp_path)
    path_new = save_brief(newer, tmp_path)
    assert path_old.exists() and path_new.exists() and path_old != path_new

    assert [b.brief_id for b in list_briefs(tmp_path)] == ["B2", "B1"]
    assert load_latest(tmp_path).brief_id == "B2"
    assert load_latest(tmp_path, exclude_id="B2").brief_id == "B1"

    # Re-saving the same brief is idempotent.
    save_brief(newer, tmp_path)
    assert len(list_briefs(tmp_path)) == 2

    # A second brief with the same created_at still gets its own file.
    twin = make_brief("B3", "2026-11-02T10:00:00+00:00", [station("A", 90.0, "Satisfactory")])
    save_brief(twin, tmp_path)
    assert sorted(b.brief_id for b in list_briefs(tmp_path)) == ["B1", "B2", "B3"]


def test_store_skips_corrupt_and_foreign_files(tmp_path):
    brief = make_brief("B1", "2026-11-01T10:00:00+00:00", [station("A", 100.0, "Satisfactory")])
    save_brief(brief, tmp_path)
    (tmp_path / "broken.json").write_text("{not json at all", encoding="utf-8")
    (tmp_path / "other.json").write_text('{"hello": "world"}', encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

    loaded = list_briefs(tmp_path)
    assert [b.brief_id for b in loaded] == ["B1"]
    assert load_latest(tmp_path).brief_id == "B1"


def test_store_missing_directory_returns_nothing(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert load_latest(missing) is None
    assert list_briefs(missing) == []


# ---------------------------------------------------------------- finding identity
def test_recurring_findings_are_not_listed_as_new():
    """Titles carry changing numbers; the diff compares type + stations + pollutant."""
    previous = make_brief(
        "B1",
        "2026-11-02T10:00:00+00:00",
        [station("A", 200.0, "Poor"), station("B", 150.0, "Moderately Polluted")],
        findings=[
            Finding(id="F1", type="hotspot", title="Hotspot: A at AQI 200", formula="x", stations=["A"]),
            Finding(id="F2", type="trend", title="A: AQI trend up (+12.0 per day)", formula="x", stations=["A"]),
            Finding(
                id="F3",
                type="exceedance",
                title="PM2.5 above the WHO 24-hour guideline at 2 of 2 stations",
                formula="x",
                stations=["A", "B"],
                facts={"pollutant": "PM2.5"},
            ),
        ],
    )
    current = make_brief(
        "B2",
        "2026-11-03T10:00:00+00:00",
        [station("A", 240.0, "Poor"), station("B", 150.0, "Moderately Polluted"), station("C", 90.0, "Satisfactory")],
        findings=[
            Finding(id="F1", type="hotspot", title="Hotspot: A at AQI 240", formula="x", stations=["A"]),
            Finding(id="F2", type="trend", title="A: AQI trend up (+12.0 per day)", formula="x", stations=["A"]),
            Finding(
                id="F3",
                type="exceedance",
                title="PM2.5 above the WHO 24-hour guideline at 1 of 3 stations",
                formula="x",
                stations=["A"],
                facts={"pollutant": "PM2.5"},
            ),
            Finding(id="F4", type="gap", title="Unreliable readings at C", formula="x", stations=["C"]),
        ],
    )

    result = diff_briefs(current, previous)

    # Only the genuinely new finding is new; the hotspot and exceedance changed their numbers.
    assert result.new_finding_titles == ["Unreliable readings at C"]
    assert result.resolved_finding_titles == []
    statuses = {change["key"]: change["status"] for change in result.finding_changes}
    assert statuses["hotspot|A|"] == "changed"
    assert statuses["trend|A|"] == "persisting"
    assert statuses["exceedance|A|PM2.5"] == "changed"
    assert statuses["gap|C|"] == "new"
    assert result.summary_facts["n_new_findings"] == 1
    assert result.summary_facts["n_changed_findings"] == 2
    assert result.summary_facts["n_persisting_findings"] == 1


def test_resolved_findings_are_reported():
    previous = make_brief(
        "B1",
        "2026-11-02T10:00:00+00:00",
        [station("A", 200.0, "Poor")],
        findings=[
            Finding(id="F1", type="anomaly", title="Suspect sensor readings at A", formula="x", stations=["A"]),
        ],
    )
    current = make_brief("B2", "2026-11-03T10:00:00+00:00", [station("A", 210.0, "Poor")])
    result = diff_briefs(current, previous)
    assert result.resolved_finding_titles == ["Suspect sensor readings at A"]
    assert result.new_finding_titles == []
    assert [change["status"] for change in result.finding_changes] == ["resolved"]


def test_a_low_confidence_new_hotspot_is_labelled_unverified():
    def aqi(name: str, value: float, confidence: str = "high") -> StationAQI:
        return StationAQI(station=name, aqi=value, category="Poor", confidence=confidence, reliability=0.9)

    previous = make_brief(
        "B1",
        "2026-11-02T10:00:00+00:00",
        [aqi("A", 300.0), aqi("B", 250.0), aqi("C", 200.0)],
        labels={"A": "reliable", "B": "reliable", "C": "reliable"},
    )
    current = make_brief(
        "B2",
        "2026-11-03T10:00:00+00:00",
        [aqi("A", 310.0), aqi("B", 255.0), aqi("C", 205.0), aqi("D", 290.0, "low")],
        labels={"A": "reliable", "B": "reliable", "C": "reliable", "D": "unreliable"},
    )
    result = diff_briefs(current, previous)
    assert result.new_hotspots == ["D (unverified)"]
    assert result.summary_facts["n_new_hotspots"] == 1


def test_indicative_aqi_is_not_compared_with_an_official_one():
    previous = make_brief(
        "B1",
        "2026-11-02T10:00:00+00:00",
        [station("A", 200.0, "Poor")],
    )
    current = make_brief(
        "B2",
        "2026-11-03T10:00:00+00:00",
        [StationAQI(station="A", aqi=400.0, category="Very Poor", aqi_kind="indicative", confidence="low")],
    )
    change = change_for(diff_briefs(current, previous), "A")
    assert change.direction == "unknown"
    assert change.delta is None


def test_two_indicative_briefs_are_compared():
    previous = make_brief(
        "B1",
        "2026-11-02T10:00:00+00:00",
        [StationAQI(station="A", aqi=200.0, category="Poor", aqi_kind="indicative", confidence="low")],
    )
    current = make_brief(
        "B2",
        "2026-11-03T10:00:00+00:00",
        [StationAQI(station="A", aqi=240.0, category="Poor", aqi_kind="indicative", confidence="low")],
    )
    change = change_for(diff_briefs(current, previous), "A")
    assert change.delta == 40.0
    assert change.direction == "worse"


def test_headline_facts_match_station_changes():
    previous = make_brief(
        "B1",
        "2026-11-02T10:00:00+00:00",
        [station("A", 100.0, "Satisfactory"), station("B", 200.0, "Moderately Polluted")],
    )
    current = make_brief(
        "B2",
        "2026-11-03T10:00:00+00:00",
        [station("A", 160.0, "Moderately Polluted"), station("B", 150.0, "Moderately Polluted")],
    )
    result = diff_briefs(current, previous)
    facts = result.summary_facts
    headline = facts["headline_facts"]

    assert headline["n_worse"] == sum(1 for c in result.station_changes if c.direction == "worse")
    assert headline["n_better"] == sum(1 for c in result.station_changes if c.direction == "better")
    assert headline["worst_station"] == "A"
    assert headline["worst_delta"] == 60.0
    assert headline["best_station"] == "B"
    assert headline["best_delta"] == -50.0
    assert headline["max_aqi_now"] == 160.0
    assert set(headline) <= set(facts)
