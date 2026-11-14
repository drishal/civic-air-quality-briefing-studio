"""The "Prove it" block and the "Try to fool it" red-team panel.

Two demonstration moments from OPUS_REVIEW §3, kept in one module so the whole grounding story
lives in code a judge can read:

* :func:`render_prove_it` — one finding as sentence -> numbers -> formula -> raw source rows,
  with the cleaning step per row (``core.evidence.evidence_rows``).
* :func:`render_prove_it_selector` — a finding picker that deep-links from
  ``st.session_state[f"{key_prefix}_focus"]``.
* :func:`render_fool_it` — paste a false claim, run the deterministic verifier
  (``core.verify.check_text``) and watch it turn red.

Every builder degrades to a friendly ``st.info`` instead of raising when the brief, the findings
or the tidy table are missing. HTML is built from ``ui.components`` helpers, which escape input.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

import pandas as pd

from core.evidence import EVIDENCE_COLUMNS, evidence_rows
from core.models import Brief, Finding
from core.verify import Issue, check_text
from ui import components as C

_CONFIDENCE_TONE = {"high": "ok", "medium": "warn", "low": "danger"}
_INFORMATIONAL_FLAGS = set(C.INFORMATIONAL_FLAGS)


# ------------------------------------------------------------------ small helpers
def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Field from a pydantic model or a plain dict."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _stretch(st: Any) -> dict[str, Any]:
    """Width kwarg for the installed Streamlit (1.49+ wants ``width='stretch'``)."""
    try:
        parts = [int(p) for p in re.findall(r"\d+", str(getattr(st, "__version__", "")))[:2]]
        if len(parts) == 2 and tuple(parts) >= (1, 49):
            return {"width": "stretch"}
    except Exception:
        pass
    return {"use_container_width": True}


def _html(st: Any, markup: str) -> None:
    """Only HTML produced by ui.components, which escapes every input string."""
    if markup:
        st.markdown(markup, unsafe_allow_html=True)


def _findings(brief: Any) -> list[Any]:
    return [f for f in (_get(brief, "findings", []) or []) if _get(f, "id")]


def _fact_text(value: Any) -> str:
    """A fact exactly as computed: no rounding of the number a judge is about to check."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _is_flagged(flags: Any) -> bool:
    """True when a reading carries a data fault (unit notes alone do not count)."""
    marks = [str(m) for m in flags] if isinstance(flags, (list, tuple)) else []
    return any(mark not in _INFORMATIONAL_FLAGS for mark in marks)


def _evidence_frame(rows: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Display frame for the evidence table plus the number of fault-flagged rows."""
    shown = rows.copy()
    for column in EVIDENCE_COLUMNS:
        if column not in shown.columns:
            shown[column] = None
    shown = shown[list(EVIDENCE_COLUMNS)]
    if "flags" in shown.columns:
        shown["flagged"] = shown["flags"].apply(lambda f: "flagged" if _is_flagged(f) else "")
        shown["flags"] = shown["flags"].apply(
            lambda f: ", ".join(str(m) for m in f) if isinstance(f, (list, tuple)) else ""
        )
    else:
        shown["flagged"] = ""
    if "ts" in shown.columns:
        shown["ts"] = shown["ts"].astype(str)
    n_flagged = int((shown["flagged"] == "flagged").sum())
    return shown, n_flagged


def _styled(frame: pd.DataFrame) -> Any:
    """Tint the flagged rows; colour is never the only signal (the 'flagged' column says so)."""
    try:
        return frame.style.apply(
            lambda row: [
                "background-color: rgba(168, 32, 26, 0.14);" if row.get("flagged") == "flagged" else ""
                for _ in row
            ],
            axis=1,
        )
    except Exception:  # a styler failure must not hide the table
        return frame


# ------------------------------------------------------------------ prove it
def render_prove_it(st: Any, finding: Optional[Finding], brief: Optional[Brief], tidy: Any) -> None:
    """One finding: numbers, formula, caveats and the raw rows behind it."""
    if finding is None:
        st.info("Pick a finding to prove it. No finding selected.")
        return

    facts = dict(_get(finding, "facts", {}) or {})
    fid = str(_get(finding, "id", ""))
    title = str(_get(finding, "title", ""))
    confidence = str(_get(finding, "confidence", "low"))
    caveats = [str(c) for c in (_get(finding, "caveats", []) or [])]

    with st.container(border=True):
        _html(
            st,
            '<div class="pw-finding">'
            f"<b>{C.esc(fid)} · {C.esc(title)}</b><br>"
            f"{C.chip(str(_get(finding, 'type', '')))} "
            f"{C.dot_chip('confidence: ' + confidence, _CONFIDENCE_TONE.get(confidence, 'warn'))}"
            "</div>",
        )

        rows = [{"fact": str(key), "value": _fact_text(value)} for key, value in facts.items()]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, **_stretch(st))
        else:
            st.caption("This finding records no numbers.")

        if _get(finding, "formula"):
            _html(st, f'<p class="pw-small">How it is computed — {C.esc(_get(finding, "formula"))}</p>')

        if caveats:
            _html(
                st,
                '<div class="pw-vitals">'
                + "".join(C.chip(caveat, "warn", "⚠") for caveat in caveats)
                + "</div>",
            )

        evidence = evidence_rows(tidy, finding) if tidy is not None else None
        recorded = len(list(_get(finding, "evidence_row_ids", []) or []))
        if evidence is None or evidence.empty:
            st.info(
                "The source rows for this finding are not in this run's table "
                "(they may belong to an earlier brief)."
            )
            total = recorded
        else:
            shown, n_flagged = _evidence_frame(evidence)
            with st.expander(f"Source rows ({len(shown)})", expanded=True):
                st.dataframe(_styled(shown), hide_index=True, **_stretch(st))
                st.caption(
                    "value_raw and unit_raw are as the file stated them; cleaning_step is what the "
                    f"pipeline did to each cell. {n_flagged} of {len(shown)} rows carry a data fault."
                )
            total = len(shown)

        if total:
            st.caption(f"{total} source rows back this finding")
        else:
            st.caption("No source rows were recorded for this finding.")


def render_prove_it_selector(
    st: Any,
    brief: Optional[Brief],
    tidy: Any,
    key_prefix: str = "prove",
) -> None:
    """Selectbox of finding ids that renders :func:`render_prove_it` for the chosen one."""
    findings = _findings(brief)
    if not findings:
        st.info("No findings to prove yet. Load a table first; the proof needs at least one finding.")
        return

    ids = [str(_get(f, "id")) for f in findings]
    titles = {str(_get(f, "id")): str(_get(f, "title", "")) for f in findings}
    widget_key = f"{key_prefix}_select"

    focus = st.session_state.get(f"{key_prefix}_focus")
    if focus in ids:
        st.session_state[widget_key] = focus
    if st.session_state.get(widget_key) not in ids:
        st.session_state[widget_key] = ids[0]

    chosen = st.selectbox(
        "Prove a finding",
        ids,
        key=widget_key,
        format_func=lambda fid: f"{fid} · {titles.get(fid, '')}",
    )
    finding = next((f for f in findings if str(_get(f, "id")) == chosen), None)
    render_prove_it(st, finding, brief, tidy)


# ------------------------------------------------------------------ fool it
def _worst_station(brief: Optional[Brief]) -> str:
    """Name of the worst station in the brief, else the first station a finding names."""
    stations = [s for s in (_get(brief, "station_aqi", []) or []) if _get(s, "aqi") is not None]
    if stations:
        worst = max(stations, key=lambda s: float(_get(s, "aqi")))
        name = str(_get(worst, "station", "") or "")
        if name:
            return name
    for finding in _findings(brief):
        names = [str(n) for n in (_get(finding, "stations", []) or []) if str(n).strip()]
        if names:
            return names[0]
    return "Hadapsar"


def _supported_sentence(findings: Sequence[Any]) -> str:
    """A sentence built only from the top finding's facts, so the verifier passes it."""
    pools = [
        [f for f in findings if _get(f, "type") == "hotspot" and _get(f, "confidence") == "high"],
        [f for f in findings if _get(f, "type") == "hotspot"],
        list(findings),
    ]
    for pool in pools:
        for finding in pool:
            facts = dict(_get(finding, "facts", {}) or {})
            aqi = facts.get("aqi")
            if not isinstance(aqi, (int, float)):
                continue
            station = (_get(finding, "stations", []) or ["The area"])[0]
            category = facts.get("category")
            tail = f" ({category})" if category else ""
            return (
                f"{station} recorded the highest 24-hour AQI in this brief at {aqi:.0f}{tail} "
                f"[{_get(finding, 'id')}]."
            )
    first = findings[0]
    return f"{_get(first, 'title')} [{_get(first, 'id')}]."


def _unsupported_sentence(station: str) -> str:
    """The red-team injection: a plausible number, an invented citation, a cause as fact."""
    return f"{station} AQI is 412 and it was caused by traffic. [F99]"


def _extra_facts(brief: Optional[Brief]) -> dict[str, Any]:
    """Brief-level numbers a draft may cite without a finding."""
    return {
        "n_findings": len(_findings(brief)),
        "n_stations": len(list(_get(brief, "station_aqi", []) or [])),
        "area": str(_get(brief, "area", "") or ""),
    }


def _load_claim(key: str, value: str) -> None:
    """Button callback: runs before the text box is instantiated, so the value sticks."""
    import streamlit as st

    st.session_state[key] = value


def render_fool_it(st: Any, brief: Optional[Brief], key_prefix: str = "fool") -> None:
    """Text box + verifier: type a false claim, press the button, see the issues."""
    findings = _findings(brief)
    station = _worst_station(brief)
    unsupported = _unsupported_sentence(station)
    supported = _supported_sentence(findings) if findings else ""
    text_key = f"{key_prefix}_text"
    check_key = f"{key_prefix}_check"

    with st.container(border=True):
        _html(
            st,
            C.section("Try to fool the verifier", "no LLM: citations and every number are checked in code"),
        )
        st.caption(
            "Every number must appear in a finding's facts, every [F#] must exist, and a claim that "
            "states a number needs a citation. Edit the claim, or load a preset."
        )

        if text_key not in st.session_state:
            st.session_state[text_key] = unsupported
        st.text_input("Claim to check", key=text_key)

        columns = st.columns(2)
        with columns[0]:
            st.button(
                "Supported example",
                key=f"{key_prefix}_preset_ok",
                disabled=not supported,
                on_click=_load_claim,
                args=(text_key, supported),
            )
        with columns[1]:
            st.button(
                "Unsupported example",
                key=f"{key_prefix}_preset_bad",
                on_click=_load_claim,
                args=(text_key, unsupported),
            )

        if supported:
            st.caption(f"Supported preset: {supported}")

        if not st.button("Check this claim", key=check_key):
            return

        text = str(st.session_state.get(text_key, ""))
        if not text.strip():
            st.info("Nothing to check yet. Type a claim first.")
            return
        if not findings:
            st.info(
                "No findings in this brief, so every number is unsupported. "
                "Load a table to compute findings first."
            )
            return
        try:
            issues: list[Issue] = check_text(text, findings, extra_facts=_extra_facts(brief))
        except Exception as exc:  # the demo must never die on a check
            st.warning(f"The verifier could not run: {type(exc).__name__}: {exc}")
            return

        if not issues:
            _html(st, C.chip("All claims supported", "ok", "✓"))
            return

        _html(st, C.section(f"{len(issues)} issue(s) caught", "nothing reaches a citizen without this check"))
        _html(
            st,
            '<div class="pw-vitals">'
            + "".join(
                C.chip(f"{issue.kind}: {issue.detail or 'unsupported'}", "danger", "✕")
                for issue in issues
            )
            + "</div>",
        )
        for sentence in dict.fromkeys(issue.sentence for issue in issues if issue.sentence):
            st.caption(f"Sentence: {sentence}")


__all__ = [
    "render_fool_it",
    "render_prove_it",
    "render_prove_it_selector",
]
