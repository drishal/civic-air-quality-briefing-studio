"""End-to-end smoke test: numeric pipeline -> every LLM stage offline -> the Streamlit UI.

Run explicitly with:  .venv/bin/python -m pytest tests/smoke.py -q

No network and no API key: `llm.client.llm_available` is patched to False and the key env vars are
deleted, so every generative stage must return its deterministic fallback and the app must still
render every tab.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEEK1 = ROOT / "data" / "sample_week1.csv"
WEEK2 = ROOT / "data" / "sample_week2.csv"


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """No key, no network, no stale demo cache, and a throwaway working directory."""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    try:
        from llm import client

        monkeypatch.setattr(client, "llm_available", lambda: False, raising=False)
        from llm import stages

        if hasattr(stages, "llm_available"):
            monkeypatch.setattr(stages, "llm_available", lambda: False, raising=False)
    except Exception:
        pass
    yield


def _require_sample(path: Path) -> bytes:
    if not path.exists():
        pytest.skip(f"{path.name} is not part of this build yet")
    return path.read_bytes()


@pytest.fixture
def results(tmp_path):
    """Week 1 as the baseline, week 2 diffed against it — the demo happy path."""
    from core import pipeline

    store = str(tmp_path / ".briefs")
    week1 = _require_sample(WEEK1)
    week2 = _require_sample(WEEK2)
    first = pipeline.run_pipeline(
        file_bytes=week1,
        filename=WEEK1.name,
        area="Pune",
        use_llm_mapper=False,
        store_dir=store,
        save=False,
        previous=None,
    )
    second = pipeline.run_pipeline(
        file_bytes=week2,
        filename=WEEK2.name,
        area="Pune",
        use_llm_mapper=False,
        store_dir=store,
        save=False,
        previous=first.brief,
    )
    return first, second


def test_pipeline_feeds_every_tab(results):
    from core.models import CATEGORIES

    first, second = results

    # Tab 1 - Data
    assert len(first.raw.df) > 0
    assert first.mapping.source in {"llm", "heuristic"}
    assert first.mapping.columns
    for column in ("row_id", "station", "pollutant", "value_raw", "unit_raw", "value_std", "flags", "ts"):
        assert column in first.tidy.columns, f"tidy table is missing {column}"
    assert len(first.tidy) > 0

    # Tab 2 - Quality
    assert first.quality.stations, "no station reliability was computed"
    assert first.quality.n_rows_in > 0 and first.quality.n_readings > 0
    assert isinstance(first.quality.flag_counts, dict)
    assert first.quality.cleaning_log or first.notes

    # Tab 3 - Insights
    assert first.brief.station_aqi, "no station AQI"
    assert any(station.aqi is not None for station in first.brief.station_aqi)
    assert first.brief.findings, "no findings"
    for category in [s.category for s in first.brief.station_aqi if s.category]:
        assert category in CATEGORIES
    for finding in first.brief.findings:
        assert finding.id.startswith("F")
        assert finding.formula
        assert finding.confidence in {"high", "medium", "low"}
        assert len(finding.evidence_row_ids) <= 50
        for row_id in finding.evidence_row_ids:
            assert row_id in set(first.tidy["row_id"].astype(str))

    # Tab 5 - What changed
    assert first.diff.has_previous is False, "the first brief must not diff against anything"
    assert second.diff.has_previous is True
    assert second.diff.previous_brief_id == first.brief.brief_id
    assert second.diff.station_changes, "week 2 produced no station deltas"
    assert isinstance(second.diff.summary_facts, dict)


def test_all_llm_stages_offline(results):
    from core import verify
    from core.models import CATEGORIES
    from llm import stages
    from core.models import ColumnMapping
    from llm.schemas import ActionPlan, Advisory, InternalNote, Interpretation

    first, _ = results
    brief, diff_result = first.brief, first.diff
    finding_ids = {finding.id for finding in brief.findings}

    mapping = stages.map_columns(first.raw)
    assert mapping is None or isinstance(mapping, ColumnMapping)

    interpretation = stages.interpret(brief, diff_result)
    assert isinstance(interpretation, Interpretation)
    assert interpretation.headline
    assert interpretation.used_fallback is True, "offline run must be marked as a fallback"
    assert isinstance(interpretation.cannot_tell, list)

    plans = {}
    for persona in ("ward_officer", "housing_society", "school"):
        plan = stages.plan_actions(brief, diff_result, persona)
        assert isinstance(plan, ActionPlan), f"{persona}: wrong plan type"
        assert plan.persona == persona
        assert plan.actions, f"{persona}: no actions"
        assert plan.used_fallback is True
        ranks = [action.rank for action in plan.actions]
        assert ranks == sorted(ranks), f"{persona}: ranks are not ordered"
        for action in plan.actions:
            assert action.action and action.why_now
            assert action.effort and action.owner and action.time_window
            assert not set(action.cites) - finding_ids, f"{persona}: cites an unknown finding"
        plans[persona] = plan

    note = stages.write_internal_note(brief, diff_result, interpretation, plans["ward_officer"])
    assert isinstance(note, InternalNote)
    assert note.markdown.strip()
    assert note.used_fallback is True

    for language, level in (("English", "simple"), ("Hindi", "standard"), ("Marathi", "technical")):
        advisory = stages.write_advisory(brief, language, level, "Pune")
        assert isinstance(advisory, Advisory), f"{language}/{level}: wrong advisory type"
        assert advisory.language == language
        assert advisory.reading_level == level
        assert advisory.headline and advisory.today_message
        assert advisory.what_to_do, f"{language}/{level}: no action list"
        assert advisory.care.children and advisory.care.older_adults and advisory.care.sensitive_groups
        assert advisory.source_line
        assert advisory.category in CATEGORIES
        assert advisory.used_fallback is True

    for obj in (interpretation, note, plans["school"]):
        issues = verify.check_object(obj, brief.findings)
        assert isinstance(issues, list)


def test_bad_input_is_a_friendly_value_error(tmp_path):
    from core import pipeline

    with pytest.raises(ValueError):
        pipeline.run_pipeline(text="", store_dir=str(tmp_path / ".briefs"), save=False)


def _button(elements, label: str):
    for element in elements:
        if label.lower() in (element.label or "").lower():
            return element
    available = [element.label for element in elements]
    raise AssertionError(f"button {label!r} not found; available: {available}")


def test_app_loads_and_samples_run():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=240)
    app.run()
    assert not app.exception, f"first load raised: {app.exception}"
    assert app.session_state["result"] is None

    _button(app.button, "Load sample week 1").click().run()
    assert not app.exception, f"sample week 1 raised: {app.exception}"
    assert app.session_state["error"] is None
    result = app.session_state["result"]
    assert result is not None and result.brief.findings

    _button(app.sidebar.button, "Load sample week 2").click().run()
    assert not app.exception, f"sample week 2 raised: {app.exception}"
    result = app.session_state["result"]
    assert result is not None
    assert result.diff.has_previous is True, "week 2 must diff against week 1"
