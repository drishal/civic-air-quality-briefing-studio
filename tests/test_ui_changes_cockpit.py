"""Tests for ui.changes_cockpit: action diff, finding changes, plain summary, prompt cockpit.

The UI is exercised through Streamlit's AppTest on the real two-week sample data, so the
assertions cover what a judge actually sees.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core.models import Brief, DiffResult
from core.pipeline import run_pipeline
from llm import schemas
from ui import changes_cockpit as CC

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PROMPTS = ROOT / "llm" / "prompts"


def _plan(*rows: tuple[int, str, str]) -> schemas.ActionPlan:
    """ActionPlan from (rank, action text, library_id) triples."""
    return schemas.ActionPlan(
        persona="ward_officer",
        actions=[
            schemas.Action(
                rank=rank,
                action=text,
                why_now="because [F1]",
                cites=["F1"],
                owner="ward office",
                effort="low",
                time_window="today",
                data_gap_action="",
                library_id=library_id,
            )
            for rank, text, library_id in rows
        ],
    )


@pytest.fixture(scope="module")
def weeks() -> tuple[Brief, Brief, DiffResult]:
    """Week 1, week 2 (diffed against week 1) built offline from the sample CSVs."""
    week1 = run_pipeline(
        file_bytes=(DATA / "sample_week1.csv").read_bytes(),
        filename="sample_week1.csv",
        area="Pune",
        use_llm_mapper=False,
        save=False,
    )
    week2 = run_pipeline(
        file_bytes=(DATA / "sample_week2.csv").read_bytes(),
        filename="sample_week2.csv",
        area="Pune",
        use_llm_mapper=False,
        save=False,
        previous=week1.brief,
    )
    assert week2.diff.has_previous, "week 2 must be diffed against week 1"
    return week1.brief, week2.brief, week2.diff


# ------------------------------------------------------------------ action diff
def test_actions_diff_matches_by_library_id():
    prev = _plan((1, "Field-check the Nigdi sensor", "sensor_check"), (2, "Sweep roads at peak", "road_sweep"))
    cur = _plan((1, "Sweep roads at peak", "road_sweep"), (2, "Water the market yard", "water_spray"))
    diff = CC.actions_diff(prev, cur)
    assert [a.action for a in diff["added"]] == ["Water the market yard"]
    assert [a.action for a in diff["dropped"]] == ["Field-check the Nigdi sensor"]
    assert [a.action for a in diff["kept"]] == ["Sweep roads at peak"]


def test_actions_diff_falls_back_to_action_text():
    prev = _plan((1, "Issue a public notice", "suggested_new"))
    cur = _plan((1, "issue  a public NOTICE", "suggested_new"))
    diff = CC.actions_diff(prev, cur)
    assert diff["added"] == [] and diff["dropped"] == []
    assert len(diff["kept"]) == 1


def test_actions_diff_handles_empty_and_missing_plans():
    assert CC.actions_diff(None, None) == {"added": [], "dropped": [], "kept": []}
    only_now = CC.actions_diff(None, _plan((1, "Do a thing", "x")))
    assert len(only_now["added"]) == 1 and only_now["dropped"] == []


def _actions_script(prev_plan, cur_plan, persona_label) -> None:
    """Script body for AppTest: from_function re-execs the source, so it imports its own names."""
    import streamlit as st

    from ui import changes_cockpit as cockpit

    cockpit.render_actions_changed(st, prev_plan, cur_plan, persona_label)


def test_actions_diff_render_without_previous_plan_shows_friendly_message():
    at = AppTest.from_function(
        _actions_script, default_timeout=60, args=(None, _plan((1, "Sweep the road", "road_sweep")), "Ward officer")
    ).run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Actions added / dropped for Ward officer" in text
    assert "No previous action plan to compare" in text


# ------------------------------------------------------------------ finding changes
def test_finding_changes_render_on_real_diff(weeks):
    _week1, _week2, diff = weeks

    at = AppTest.from_function(_findings_script, default_timeout=60, args=(diff,)).run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Hotspots" in text
    if any("(unverified)" in entry for entry in diff.new_hotspots):
        assert "unverified: sensor reliability low" in text
    if diff.category_transitions:
        assert "pw-transition" in text


def test_finding_changes_baseline_message_without_previous():
    at = AppTest.from_function(_baseline_changes, default_timeout=60).run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "No previous brief to compare" in text


def _findings_script(diff) -> None:
    import streamlit as st

    from ui import changes_cockpit as cockpit

    cockpit.render_finding_changes(st, diff)


def _summary_script(diff) -> None:
    import streamlit as st

    from ui import changes_cockpit as cockpit

    cockpit.render_change_summary(st, diff)


def _cockpit_script(brief, diff) -> None:
    import streamlit as st

    from ui import changes_cockpit as cockpit

    cockpit.render_prompt_cockpit(st, brief, diff)


def _baseline_changes() -> None:
    import streamlit as st

    from core.models import DiffResult as Result
    from ui import changes_cockpit as cockpit

    cockpit.render_finding_changes(st, Result(has_previous=False))


# ------------------------------------------------------------------ summary text
def test_change_summary_is_deterministic_and_uses_only_diff_facts(weeks):
    _week1, _week2, diff = weeks
    first = CC.change_summary_text(diff)
    assert first == CC.change_summary_text(diff)
    sentences = [s for s in first.split(". ") if s.strip()]
    assert 1 <= len(sentences) <= 3
    for entry in diff.category_transitions:
        station = entry.split(":")[0].strip()
        if entry == diff.category_transitions[0]:
            assert station in first
    assert "unverified" in first.lower() or "hotspot" in first.lower() or "moved" in first.lower()


def test_change_summary_baseline_and_no_change():
    baseline = CC.change_summary_text(DiffResult(has_previous=False, summary_facts={"n_stations": 8, "n_findings": 10}))
    assert "first brief" in baseline
    assert "8 stations" in baseline and "10 findings" in baseline
    quiet = CC.change_summary_text(DiffResult(has_previous=True, summary_facts={"n_worse": 0, "n_better": 0}))
    assert quiet == "No station moved enough to report against the previous brief."


def test_change_summary_render_shows_the_text(weeks):
    _week1, _week2, diff = weeks
    expected = CC.change_summary_text(diff)

    at = AppTest.from_function(_summary_script, default_timeout=60, args=(diff,)).run()
    assert not at.exception
    assert any(expected == m.value for m in at.markdown)


# ------------------------------------------------------------------ prompt cockpit
def test_cockpit_rows_read_versions_and_verification():
    class Result:
        used_fallback = False
        verification = {"issues_found": 3, "repaired": 2, "remaining": 1}

    rows = CC.cockpit_rows({"p3_actioner": Result(), "p5_advisory": {"used_fallback": True}})
    by_stage = {row["stage"]: row for row in rows}
    assert by_stage["P3 actioner"]["prompt_version"] == "p3.v1"
    assert by_stage["P3 actioner"]["model"] == "model"
    assert by_stage["P3 actioner"]["issues_found"] == 3
    assert by_stage["P3 actioner"]["remaining"] == 1
    assert by_stage["P5 advisory"]["model"] == "deterministic fallback"
    assert by_stage["P5 advisory"]["issues_found"] == "n/a"
    assert by_stage["P1 mapper"]["model"] == "n/a"


def test_prompt_files_exist_for_every_stage():
    for _label, stem, _schema, _kind in CC.STAGES:
        assert (PROMPTS / f"{stem}.md").is_file(), stem


def test_prompt_cockpit_renders_table_diagram_and_prompt_text(weeks):
    week1, _week2, diff = weeks

    at = AppTest.from_function(_cockpit_script, default_timeout=120, args=(week1, diff)).run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Prompt cockpit" in text
    assert "Gemini explain" in text and ">LLM<" in text
    assert "Fresh table" not in text

    table = at.dataframe[0].value
    assert len(table) == len(CC.STAGES)
    assert set(table["stage"]) == {label for label, _stem, _schema, _kind in CC.STAGES}
    assert "p3.v1" in set(table["prompt_version"])
    assert "n/a" in set(table["model"])

    labels = " ".join(exp.label for exp in at.expander)
    assert "P2 interpreter" in labels and "p2.v1" in labels
    prompt_texts = " ".join(block.value for block in at.code)
    assert "prompt_version: p3.v1" in prompt_texts
    assert "AUTHORITY" in prompt_texts or "FINDINGS" in prompt_texts


def test_all_panels_render_together(weeks):
    week1, _week2, diff = weeks
    prev_plan = _plan((1, "Field-check the Nigdi sensor", "sensor_check"))
    cur_plan = _plan((1, "Water the market yard", "water_spray"))

    at = AppTest.from_function(
        _all_panels, default_timeout=180, args=(week1, diff, prev_plan, cur_plan)
    ).run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "What changed, in plain words" in text
    assert "Actions added / dropped for Ward officer" in text
    assert "Water the market yard" in text
    assert "Field-check the Nigdi sensor" in text
    assert "Prompt cockpit" in text


def _all_panels(brief, diff, prev_plan, cur_plan) -> None:
    import streamlit as st

    from ui import changes_cockpit as cockpit

    cockpit.render_change_summary(st, diff)
    cockpit.render_finding_changes(st, diff)
    cockpit.render_actions_changed(st, prev_plan, cur_plan, "Ward officer")
    cockpit.render_prompt_cockpit(st, brief, diff, {"p3_actioner": cur_plan})
