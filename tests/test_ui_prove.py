"""Streamlit AppTest coverage for the "Prove it" block and the "Try to fool it" panel."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sample_week1.csv"


@lru_cache(maxsize=1)
def _result():
    """One real pipeline run over the sample week (no LLM, nothing saved)."""
    from core.pipeline import run_pipeline

    return run_pipeline(
        file_bytes=SAMPLE.read_bytes(),
        filename=SAMPLE.name,
        area="Pune",
        use_llm_mapper=False,
        save=False,
    )


def _prove_script(result):
    import streamlit as st

    from ui.prove import render_fool_it, render_prove_it_selector

    render_prove_it_selector(st, result.brief, result.tidy, key_prefix="prove")
    render_fool_it(st, result.brief, key_prefix="fool")


def _empty_script():
    import streamlit as st

    from ui.prove import render_fool_it, render_prove_it, render_prove_it_selector

    render_prove_it(st, None, None, None)
    render_prove_it_selector(st, None, None)
    render_fool_it(st, None)


def _press(at, label):
    button = next(b for b in at.button if b.label == label)
    return button.click().run()


def test_prove_and_fool_render_on_real_brief():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_prove_script, kwargs={"result": _result()}, default_timeout=120).run()
    assert not at.exception, f"prove/fool raised: {at.exception}"

    # Selector lists every finding and defaults to the first.
    assert len(at.selectbox) == 1
    findings = _result().brief.findings
    labels = [f"{f.id} · {f.title}" for f in findings]
    assert at.selectbox[0].options == labels  # options are the formatted labels
    assert at.selectbox[0].value == findings[0].id

    # Facts table + evidence table.
    assert len(at.dataframe) >= 2

    markup = " ".join(m.value for m in at.markdown)
    assert "confidence: high" in markup
    assert "How it is computed" in markup

    captions = " ".join(c.value for c in at.caption)
    assert "source rows back this finding" in captions

    # The fool-it box is prefilled with the real worst station and a fake citation.
    assert len(at.text_input) == 1
    claim = at.text_input[0].value
    assert claim.startswith("Hadapsar") and "[F99]" in claim and "412" in claim


def test_fool_it_catches_the_unsupported_claim():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_prove_script, kwargs={"result": _result()}, default_timeout=120).run()
    at = _press(at, "Check this claim")
    assert not at.exception, f"check raised: {at.exception}"

    markup = " ".join(m.value for m in at.markdown)
    assert "unsupported_number" in markup
    assert "unknown_citation" in markup and "F99" in markup
    assert "All claims supported" not in markup


def test_fool_it_accepts_a_supported_preset():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_prove_script, kwargs={"result": _result()}, default_timeout=120).run()
    supported = at.button(key="fool_preset_ok")
    assert not supported.disabled
    third_finding_id = _result().brief.findings[0].id
    at = _press(at, "Supported example")
    assert not at.exception, f"preset raised: {at.exception}"
    assert f"[{third_finding_id}]" in at.text_input[0].value

    at = _press(at, "Check this claim")
    assert not at.exception, f"check raised: {at.exception}"
    markup = " ".join(m.value for m in at.markdown)
    assert "All claims supported" in markup, markup


def test_selector_deep_links_from_session_focus():
    from streamlit.testing.v1 import AppTest

    result = _result()
    third = result.brief.findings[2].id

    def script(result, fid):
        import streamlit as st

        from ui.prove import render_prove_it_selector

        st.session_state["prove_focus"] = fid
        render_prove_it_selector(st, result.brief, result.tidy)

    at = AppTest.from_function(script, kwargs={"result": result, "fid": third}, default_timeout=120).run()
    assert not at.exception, f"focus raised: {at.exception}"
    title = result.brief.findings[2].title
    assert at.selectbox[0].value == third
    assert f"{third} · {title}" in " ".join(m.value for m in at.markdown)


def test_everything_degrades_without_a_brief():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_empty_script, default_timeout=120).run()
    assert not at.exception, f"empty state raised: {at.exception}"
    assert not at.selectbox
    assert at.info, "expected a friendly st.info instead of an exception"
    assert at.button(key="fool_preset_ok").disabled
