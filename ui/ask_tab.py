"""'Ask the briefing' tab: one grounded, persona-aware chat over one brief.

Every answer comes from `llm.ask.answer(...)`, which reads FINDINGS, QUALITY and DIFF only and
returns an `AskAnswer` whose citations can only be real finding IDs. This module never invents
text and never raises: a broken LLM layer degrades to a polite refusal, and the chat keeps
rendering. Conversation state lives in `st.session_state[f'{key_prefix}_history']`, keyed by
brief id, so loading a new brief starts a fresh conversation.

Presentation helpers come from `ui/components.py`; the citation chips are `components.cite_html`.
"""
from __future__ import annotations

from typing import Any, Optional

from ui import components as C

_DEFAULT_PERSONA = "ward_officer"
_MAX_STARTERS = 5
_MAX_FOLLOW_UPS = 3
_RENDER_GUARD = 8  # a click inside a rendered turn appends at most one turn; never loop forever

_GENERIC_STARTERS = (
    "Which station is worst this week?",
    "What changed since the last brief?",
    "Which numbers can I trust?",
    "What should go out today?",
)


# ------------------------------------------------------------------ public entry point
def render_ask_tab(st, brief: Any, diff: Any, persona: Any, key_prefix: str = "ask") -> None:
    """Render the Ask tab. Never raises: a failure becomes a visible, calm notice."""
    try:
        _render(st, brief, diff, persona, key_prefix)
    except Exception as exc:  # noqa: BLE001 - the chat must never take the page down
        try:
            st.warning(
                f"Ask is unavailable right now ({type(exc).__name__}). "
                "The other tabs are unaffected."
            )
        except Exception:  # noqa: BLE001 - not even the notice may raise
            pass


# ------------------------------------------------------------------ layout
def _render(st, brief: Any, diff: Any, persona: Any, key_prefix: str) -> None:
    persona_key = str(persona or _DEFAULT_PERSONA).strip().lower()
    st.markdown(
        C.section("Ask the briefing", "answers cite this brief's findings only"),
        unsafe_allow_html=True,
    )
    if brief is None:
        st.info(
            "Load a brief first — sample week 1 or your own table. "
            "Then ask about a station, the category, the peak hours or what changed."
        )
        return

    history = _history(st, key_prefix, _brief_key(brief))

    question = _ask_input(st, key_prefix)
    if question:
        _submit(st, history, question, brief, diff, persona_key, key_prefix)

    _controls(st, brief, diff, persona_key, history, key_prefix)

    rendered = 0
    while rendered < len(history) and rendered < _RENDER_GUARD:
        _render_turn(st, history[rendered], brief, diff, persona_key, key_prefix, rendered)
        rendered += 1
    if not history:
        st.caption(
            "No conversation yet. Pick a starter question above, or type your own — "
            "every answer names the findings it used."
        )


def _controls(st, brief: Any, diff: Any, persona: str, history: list[dict[str, Any]], key_prefix: str) -> None:
    """Starter-question buttons plus the clear button."""
    starters = _starters(brief, diff, persona)
    if starters:
        st.caption("Starter questions")
        columns = st.columns(2)
        for index, question in enumerate(starters):
            if columns[index % 2].button(question, key=f"{key_prefix}_starter_{index}"):
                _submit(st, history, question, brief, diff, persona, key_prefix)
    turns = sum(1 for turn in history if turn.get("role") == "assistant")
    label = f"Clear conversation ({turns} answer{'s' if turns != 1 else ''})"
    if st.button(label, key=f"{key_prefix}_clear"):
        history.clear()


def _render_turn(
    st,
    turn: dict[str, Any],
    brief: Any,
    diff: Any,
    persona: str,
    key_prefix: str,
    index: int,
) -> None:
    """One chat bubble: question, or answer with badges, sources and follow-ups."""
    if turn.get("role") == "user":
        with st.chat_message("user"):
            st.markdown(str(turn.get("text") or ""))
        return
    with st.chat_message("assistant", avatar="🌤"):
        badges = _badges(turn)
        if badges:
            st.markdown(badges, unsafe_allow_html=True)
        answer_html = C.cite_html(turn.get("text"), _findings(brief))
        st.markdown(f'<div class="pw-ask__answer">{answer_html}</div>', unsafe_allow_html=True)
        if not turn.get("answerable", True):
            st.info(
                "I can't answer that from this data. Ask about a station, the category, "
                "the peak hours or what changed since the last brief."
            )
        cited = _cited(turn, brief)
        if cited:
            with st.expander(f"Sources · {len(cited)} finding{'s' if len(cited) != 1 else ''}", expanded=False):
                for fid, finding in cited:
                    st.markdown(
                        f"**[{fid}]** {getattr(finding, 'title', '')} · "
                        f"confidence: {getattr(finding, 'confidence', '')}"
                    )
        follow_ups = [str(item) for item in (turn.get("follow_ups") or []) if str(item).strip()]
        if follow_ups:
            columns = st.columns(len(follow_ups[:_MAX_FOLLOW_UPS]))
            for position, question in enumerate(follow_ups[:_MAX_FOLLOW_UPS]):
                key = f"{key_prefix}_follow_{index}_{position}"
                if columns[position].button(question, key=key):
                    _submit(st, _history(st, key_prefix, _brief_key(brief)), question, brief, diff, persona, key_prefix)


def _badges(turn: dict[str, Any]) -> str:
    """Offline badge, verification badge, and the persona's own tally."""
    chips: list[str] = []
    if turn.get("used_fallback"):
        chips.append(C.chip("answered offline (template)", "warn"))
    verification = turn.get("verification") or {}
    if isinstance(verification, dict) and verification:
        remaining = verification.get("remaining")
        if remaining is None:
            remaining = verification.get("issues_found", 0)
        try:
            count = int(remaining)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            chips.append(C.chip(f"{count} issue{'s' if count != 1 else ''}", "warn", "⚠"))
        else:
            chips.append(C.chip("verified", "ok", "✓"))
    return f'<div class="pw-vitals">{"".join(chips)}</div>' if chips else ""


# ------------------------------------------------------------------ input
def _ask_input(st, key_prefix: str) -> Optional[str]:
    """`st.chat_input` when the runtime has it, else a text box plus a send button."""
    prompt = "Ask about this brief…"
    chat_input = getattr(st, "chat_input", None)
    if callable(chat_input):
        try:
            value = chat_input(prompt, key=f"{key_prefix}_chat")
            if value:
                return str(value)
            return None
        except TypeError:  # Streamlit build without a `key` argument
            try:
                value = chat_input(prompt)
                return str(value) if value else None
            except Exception:  # noqa: BLE001 - fall through to the text box
                pass
        except Exception:  # noqa: BLE001 - fall through to the text box
            pass
    text = st.text_input(prompt, key=f"{key_prefix}_text")
    if st.button("Ask", key=f"{key_prefix}_send") and str(text or "").strip():
        return str(text)
    return None


def _submit(st, history: list[dict[str, Any]], question: Any, brief: Any, diff: Any, persona: str, key_prefix: str) -> None:
    """Append the question and its answer to this brief's conversation."""
    text = " ".join(str(question or "").split())
    if not text:
        return
    history.append({"role": "user", "text": text})
    try:
        with st.spinner("Reading the findings…"):
            answer = _answer(text, brief, diff, persona)
    except Exception:  # noqa: BLE001 - a broken spinner/stage must not lose the conversation
        answer = _payload(
            "The answer could not be generated. Try another question, or open the Insights tab.",
            (),
            False,
            (),
            True,
            {},
        )
    history.append({"role": "assistant", **answer})


def _answer(question: str, brief: Any, diff: Any, persona: str) -> dict[str, Any]:
    """One `AskAnswer` as a plain dict. Missing or broken `llm.ask` degrades to a refusal."""
    try:
        from llm.ask import answer as ask_answer
    except Exception:  # noqa: BLE001 - the module may still be under construction
        return _payload(
            "The question answering stage is not available in this build, so I can't answer that "
            "from this data.",
            (),
            False,
            _starter_questions(brief, diff, persona),
            True,
            {},
        )
    try:
        result = ask_answer(question, brief, diff, persona)
    except Exception:  # noqa: BLE001 - never surface a stack trace in the chat
        return _payload(
            "That answer could not be generated. Try another question, or open the Insights tab.",
            (),
            False,
            (),
            True,
            {},
        )
    return _to_payload(result)


def _to_payload(result: Any) -> dict[str, Any]:
    """Normalise a pydantic AskAnswer (or a dict) into render-safe plain data."""
    if result is None:
        return _payload("insufficient evidence in this brief for that question.", (), False, (), True, {})
    data = result if isinstance(result, dict) else _dump(result)
    text = str(data.get("answer") or data.get("text") or "").strip()
    if not text:
        text = "insufficient evidence in this brief for that question."
    cites = tuple(str(cite).strip().strip("[] ") for cite in (data.get("cites") or []) if str(cite).strip())
    follow_ups = tuple(str(item) for item in (data.get("follow_ups") or []) if str(item).strip())
    verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
    return _payload(
        text,
        cites,
        bool(data.get("answerable", True)),
        follow_ups,
        bool(data.get("used_fallback", False)),
        verification or {},
    )


def _dump(result: Any) -> dict[str, Any]:
    try:
        return dict(result.model_dump())
    except Exception:  # noqa: BLE001 - a plain object with attributes still renders
        fields = ("answer", "cites", "answerable", "follow_ups", "used_fallback", "verification")
        return {name: getattr(result, name, None) for name in fields}


def _payload(
    text: str,
    cites: tuple[str, ...],
    answerable: bool,
    follow_ups: tuple[str, ...],
    used_fallback: bool,
    verification: dict[str, Any],
) -> dict[str, Any]:
    return {
        "text": text,
        "cites": tuple(cites),
        "answerable": answerable,
        "follow_ups": tuple(follow_ups[:_MAX_FOLLOW_UPS]),
        "used_fallback": used_fallback,
        "verification": dict(verification or {}),
    }


# ------------------------------------------------------------------ starters, findings, state
def _starters(brief: Any, diff: Any, persona: str) -> list[str]:
    questions = _starter_questions(brief, diff, persona)
    return (questions or list(_GENERIC_STARTERS))[:_MAX_STARTERS]


def _starter_questions(brief: Any, diff: Any, persona: str) -> list[str]:
    try:
        from llm.ask import starter_questions

        return [str(item) for item in (starter_questions(brief, diff, persona) or []) if str(item).strip()]
    except Exception:  # noqa: BLE001 - starters are a convenience, never a hard dependency
        return []


def _findings(brief: Any) -> list[Any]:
    return list(getattr(brief, "findings", None) or [])


def _cited(turn: dict[str, Any], brief: Any) -> list[tuple[str, Any]]:
    """Cited findings in answer order, unknown ids dropped."""
    index = {str(getattr(finding, "id", "")): finding for finding in _findings(brief)}
    seen: list[tuple[str, Any]] = []
    for raw in turn.get("cites") or ():
        fid = str(raw).strip().strip("[] ")
        finding = index.get(fid)
        if finding is not None and all(fid != kept for kept, _ in seen):
            seen.append((fid, finding))
    return seen


def _brief_key(brief: Any) -> str:
    return str(getattr(brief, "brief_id", "") or "unsaved-brief")


def _history(st, key_prefix: str, brief_key: str) -> list[dict[str, Any]]:
    """This brief's conversation; a new brief id starts a new, empty one."""
    state_key = f"{key_prefix}_history"
    store = st.session_state.get(state_key)
    if not isinstance(store, dict):
        store = {}
        st.session_state[state_key] = store
    history = store.get(brief_key)
    if not isinstance(history, list):
        history = []
        store[brief_key] = history
    return history
