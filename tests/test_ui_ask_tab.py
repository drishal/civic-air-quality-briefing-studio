"""AppTest for `ui.ask_tab`: the Ask tab renders offline and a starter question is answered.

The pipeline builds a real brief from `data/sample_week1.csv`; the LLM layer is forced offline, so
every answer must come from `llm.ask`'s deterministic, cited fallback.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEEK1 = ROOT / "data" / "sample_week1.csv"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No key and no network: the ask stage must answer from the brief offline."""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    try:
        from llm import client

        monkeypatch.setattr(client, "llm_available", lambda: False, raising=False)
        from llm import stages

        if hasattr(stages, "llm_available"):
            monkeypatch.setattr(stages, "llm_available", lambda: False, raising=False)
    except Exception:
        pass
    yield


def _script(csv_path: str = "") -> None:
    """Streamlit script under test: build a brief from the CSV, then render only the Ask tab."""
    import streamlit as st

    from core import pipeline
    from ui.ask_tab import render_ask_tab

    if "brief" not in st.session_state:
        data = open(csv_path, "rb").read()
        result = pipeline.run_pipeline(
            file_bytes=data,
            filename="sample_week1.csv",
            area="Pune",
            use_llm_mapper=False,
            save=False,
        )
        st.session_state["brief"] = result.brief
        st.session_state["diff"] = result.diff
    brief = st.session_state["brief"]
    render_ask_tab(st, brief, st.session_state["diff"], "ward_officer")


def _app():
    from streamlit.testing.v1 import AppTest

    if not WEEK1.exists():
        pytest.skip(f"{WEEK1.name} is not part of this build yet")
    return AppTest.from_function(_script, default_timeout=240, kwargs={"csv_path": str(WEEK1)})


def _markdown(app) -> str:
    return "\n".join(str(element.value) for element in app.markdown)


def test_ask_tab_renders_and_answers_a_starter_question():
    app = _app()
    app.run()
    assert not app.exception, f"first load raised: {app.exception}"

    brief = app.session_state["brief"]
    diff = app.session_state["diff"]

    from llm.ask import starter_questions

    starters = starter_questions(brief, diff, "ward_officer")
    assert starters, "the persona must get starter questions"
    labels = [str(button.label) for button in app.button]
    for question in starters:
        assert question in labels, f"starter question missing from the tab: {question!r} (have {labels})"
    assert "pw-ask__answer" not in _markdown(app), "no answer should be rendered before a question"

    target = starters[0]
    clicked = False
    for button in app.button:
        if str(button.label) == target:
            button.click().run()
            clicked = True
            break
    assert clicked, f"starter button {target!r} not clickable"
    assert not app.exception, f"answering a starter question raised: {app.exception}"

    history = app.session_state["ask_history"][brief.brief_id]
    assert history and history[0]["role"] == "user"
    assert history[0]["text"] == target
    assert len(history) == 2 and history[1]["role"] == "assistant"

    answer_markdown = _markdown(app)
    assert "[F" in answer_markdown, f"the answer carries no citation chip:\n{answer_markdown}"
    assert "pw-cite" in answer_markdown, "citations must render through the shared chip helper"
    expander_labels = [str(element.label) for element in app.expander]
    assert any("Sources" in label for label in expander_labels), f"cited findings need a Sources block: {expander_labels}"
    for finding in brief.findings:
        if finding.id in history[1]["cites"]:
            assert finding.title in answer_markdown, f"{finding.id} missing from the Sources list"


def test_ask_tab_clear_button_empties_the_conversation():
    app = _app()
    app.run()
    brief = app.session_state["brief"]

    from llm.ask import starter_questions

    target = starter_questions(brief, app.session_state["diff"], "ward_officer")[0]
    next(button for button in app.button if str(button.label) == target).click().run()
    assert app.session_state["ask_history"][brief.brief_id]

    clear = next(button for button in app.button if "Clear conversation" in str(button.label))
    clear.click().run()
    assert not app.exception
    assert app.session_state["ask_history"][brief.brief_id] == []


def test_ask_tab_empty_state_without_a_brief():
    from streamlit.testing.v1 import AppTest

    def script() -> None:
        import streamlit as st

        from ui.ask_tab import render_ask_tab

        render_ask_tab(st, None, None, "school")

    app = AppTest.from_function(script, default_timeout=60)
    app.run()
    assert not app.exception, f"empty state raised: {app.exception}"
    assert any("Load a brief first" in str(element.value) for element in app.info)
