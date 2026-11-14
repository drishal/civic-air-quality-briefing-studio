"""What-changed cockpit: action plan diff, finding changes, plain summary, prompt cockpit.

Every number shown here comes from `DiffResult.summary_facts` / `headline_facts` and
`station_changes`; nothing is recalculated and nothing is invented. The prompt cockpit reads the
real prompt files from `llm/prompts/` so the architecture is visible on screen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pandas as pd

from core.aqi import category_color
from llm import stages
from ui import components as C

PROMPT_DIR = Path(__file__).resolve().parents[1] / "llm" / "prompts"

# (stage label, prompt file stem, schema the stage returns, who does the work)
STAGES: tuple[tuple[str, str, str, str], ...] = (
    ("P1 mapper", "p1_mapper", "ColumnMapping", "llm"),
    ("P2 interpreter", "p2_interpreter", "Interpretation", "llm"),
    ("P3 actioner", "p3_actioner", "ActionPlan", "llm"),
    ("P4 note", "p4_internal_note", "InternalNote", "llm"),
    ("P5 advisory", "p5_advisory", "Advisory", "llm"),
    ("P6 verifier", "p6_verifier", "VerifyReport", "llm"),
    ("P6b repair", "p6b_repair", "draft model (repair)", "llm"),
    ("P7 ask", "p7_ask", "AskAnswer", "llm"),
)

_STATUS_TONE = {"new": "accent", "resolved": "ok", "changed": "warn", "persisting": "hollow"}
_UNVERIFIED = "(unverified)"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a pydantic model or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _html(st: Any, markup: str) -> None:
    if markup:
        st.markdown(markup, unsafe_allow_html=True)


def _wrap(css_class: str, body: str, tone: str = "") -> str:
    style = f' style="--pw-tone:{tone}"' if tone else ""
    return f'<div class="{css_class}"{style}>{body}</div>'


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


# ------------------------------------------------------------------ 1. action diff
def _action_key(action: Any) -> str:
    """Identity of an action: its library_id, else its normalised text."""
    library_id = str(_attr(action, "library_id", "") or "").strip()
    if library_id and library_id != "suggested_new":
        return f"lib:{library_id}"
    text = " ".join(str(_attr(action, "action", "") or "").lower().split())
    return f"text:{text}"


def actions_diff(prev_plan: Any, cur_plan: Any) -> dict[str, list[Any]]:
    """Split two ActionPlans into added / dropped / kept, matching by library_id then action text.

    Returns the Action objects themselves, in plan order (rank order for the current plan).
    """
    prev_actions = list(_attr(prev_plan, "actions", []) or [])
    cur_actions = list(_attr(cur_plan, "actions", []) or [])
    prev_keys: set[str] = {_action_key(action) for action in prev_actions}
    cur_keys: set[str] = {_action_key(action) for action in cur_actions}
    return {
        "added": [a for a in cur_actions if _action_key(a) not in prev_keys],
        "dropped": [a for a in prev_actions if _action_key(a) not in cur_keys],
        "kept": [a for a in cur_actions if _action_key(a) in prev_keys],
    }


def _action_line(action: Any, sign: str, tone: str) -> str:
    """One chip: sign, action text, and its rank in the plan."""
    rank = _attr(action, "rank", None)
    text = str(_attr(action, "action", "") or "")
    label = f"{sign} {text}" + (f" (rank {rank})" if rank is not None else "")
    return C.chip(label, tone)



def render_actions_changed(st: Any, prev_plan: Any, cur_plan: Any, persona_label: str) -> None:
    """'Actions added / dropped for <persona>' as green/red chips carrying text and rank."""
    _html(st, C.section(f"Actions added / dropped for {persona_label}", "compared with the previous brief"))
    if cur_plan is None:
        _html(
            st,
            C.empty_state(
                "No action plan for this audience yet",
                "Generate the actions for this audience to compare it with the previous brief.",
                next_action="Open the Actions tab",
            ),
        )
        return
    if prev_plan is None:
        _html(
            st,
            C.empty_state(
                "No previous action plan to compare",
                f"This is the first plan saved for {persona_label}, so nothing was added or dropped yet.",
                next_action="Load a newer brief next week to see the plan diff",
            ),
        )
        return

    diff = actions_diff(prev_plan, cur_plan)
    added, dropped, kept = diff["added"], diff["dropped"], diff["kept"]
    if not added and not dropped:
        _html(st, C.chip("No change in the recommended actions", "hollow"))
    if added:
        _html(st, C.section("Added", _plural(len(added), "new action")))
        _html(st, _wrap("pw-chiprow", "".join(_action_line(a, "+", "ok") for a in added)))
    if dropped:
        _html(st, C.section("Dropped", _plural(len(dropped), "action")))
        _html(st, _wrap("pw-chiprow", "".join(_action_line(a, "−", "danger") for a in dropped)))
    if kept:
        _html(st, C.small(f"Kept: {_plural(len(kept), 'action')} carried over unchanged."))


# ------------------------------------------------------------------ 2. finding changes
def _hotspot_chip(entry: str) -> str:
    """Hotspot chip; an '(unverified)' entry gets an extra amber reliability caveat."""
    name = entry.replace(_UNVERIFIED, "").strip()
    html = C.chip(name, "info")
    if _UNVERIFIED in entry:
        html += " " + C.chip("unverified: sensor reliability low", "warn", "!")
    return html


def _transition_pill(line: str) -> str:
    """'Anand Vihar: Poor -> Very Poor' as two category chips with an arrow."""
    station, _, rest = line.partition(":")
    before, _, after = rest.partition("->")
    before, after = before.strip(), after.strip()
    if not (before and after):
        return C.chip(line, "hollow")
    pill = C.transition_pill(before, after, category_color(before), category_color(after))
    return f'{C.esc(station.strip())} {pill}'


def render_finding_changes(st: Any, diff: Any) -> None:
    """Finding status changes, hotspot entries/exits, category moves and data-quality moves."""
    if diff is None or not getattr(diff, "has_previous", False):
        _html(st, C.section("What changed in the findings", ""))
        _html(
            st,
            C.empty_state(
                "No previous brief to compare",
                "This is the baseline brief: findings, hotspots and data quality are shown as they are.",
                next_action="Load the next week's table to see what changed",
            ),
        )
        return

    changes = list(getattr(diff, "finding_changes", []) or [])
    if changes:
        _html(st, C.section("Findings", _plural(len(changes), "finding")))
        rows = []
        for change in changes:
            status = str(_attr(change, "status", "") or "")
            title = _attr(change, "title_now", None) or _attr(change, "title_prev", None) or _attr(change, "key", "")
            row = C.chip(status or "unknown", _STATUS_TONE.get(status, "hollow")) + " " + C.esc(title)
            previous = _attr(change, "title_prev", None)
            if status == "changed" and previous:
                row += " " + C.small(f"was: {previous}")
            rows.append(row)
        _html(st, "".join(f'<div class="pw-diff__row">{row}</div>' for row in rows))

    new_hotspots = list(getattr(diff, "new_hotspots", []) or [])
    resolved_hotspots = list(getattr(diff, "resolved_hotspots", []) or [])
    if new_hotspots or resolved_hotspots:
        _html(st, C.section("Hotspots", "top-3 entries and exits"))
        if new_hotspots:
            _html(st, C.small("New in the top 3"))
            _html(st, _wrap("pw-chiprow", " ".join(_hotspot_chip(e) for e in new_hotspots)))
        if resolved_hotspots:
            _html(st, C.small("Left the top 3"))
            _html(st, _wrap("pw-chiprow", " ".join(C.chip(e, "ok") for e in resolved_hotspots)))

    transitions = list(getattr(diff, "category_transitions", []) or [])
    if transitions:
        _html(st, C.section("Category moves", _plural(len(transitions), "station")))
        _html(st, "".join(f'<div class="pw-transition-line">{_transition_pill(t)}</div>' for t in transitions))

    quality_changes = list(getattr(diff, "quality_changes", []) or [])
    if quality_changes:
        _html(st, C.section("Data-quality changes", ""))
        _html(st, "".join(f'<div class="pw-transition-line">{C.esc(q)}</div>' for q in quality_changes))


# ------------------------------------------------------------------ 3. plain summary
def _facts(diff: Any) -> dict[str, Any]:
    """The citable numbers, top-level headline_facts winning over the copy inside summary_facts."""
    summary = getattr(diff, "summary_facts", None) or {}
    facts: dict[str, Any] = dict(summary) if isinstance(summary, dict) else {}
    headline = getattr(diff, "headline_facts", None) or facts.get("headline_facts") or {}
    if isinstance(headline, dict):
        facts.update(headline)
    return facts


def _fmt(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.0f}" if number.is_integer() else f"{number:.1f}"


def change_summary_text(diff: Any) -> str:
    """Two to three plain sentences built only from facts the diff already computed."""
    if diff is None:
        return "No comparison is available yet."
    facts = _facts(diff)
    sentences: list[str] = []

    if not getattr(diff, "has_previous", False):
        parts = []
        if isinstance(facts.get("n_stations"), (int, float)):
            parts.append(f"{_plural(int(facts['n_stations']), 'station')} read")
        if isinstance(facts.get("n_findings"), (int, float)):
            parts.append(_plural(int(facts["n_findings"]), "finding"))
        if isinstance(facts.get("max_aqi"), (int, float)) and facts.get("worst_station"):
            parts.append(f"the worst is {facts['worst_station']} at AQI {_fmt(facts['max_aqi'])}")
        body = f"This is the first brief for this area: {', '.join(parts)}." if parts else "This is the first brief for this area."
        return f"{body} There is no earlier brief to compare against."

    worse, better = facts.get("n_worse"), facts.get("n_better")
    if isinstance(worse, (int, float)) or isinstance(better, (int, float)):
        moves = []
        if worse:
            moves.append(f"{_plural(int(worse), 'station')} got worse")
        if better:
            moves.append(f"{_plural(int(better), 'station')} got better")
        if moves:
            sentences.append(f"Since the last brief, {' and '.join(moves)}.")

    transitions = list(getattr(diff, "category_transitions", []) or [])
    if transitions:
        station, _, rest = transitions[0].partition(":")
        before, _, after = rest.partition("->")
        if before.strip() and after.strip():
            sentences.append(f"{station.strip()} moved from {before.strip()} to {after.strip()}.")
    elif facts.get("worst_station") and facts.get("worst_delta") is not None:
        sentences.append(
            f"{facts['worst_station']} moved by {_fmt(facts['worst_delta'])} AQI points, the largest change."
        )

    new_hotspots = list(getattr(diff, "new_hotspots", []) or [])
    resolved_hotspots = list(getattr(diff, "resolved_hotspots", []) or [])
    if new_hotspots:
        entry = new_hotspots[0]
        name = entry.replace(_UNVERIFIED, "").strip()
        if _UNVERIFIED in entry:
            sentences.append(f"{name} is a new hotspot but its sensor is unreliable.")
        else:
            sentences.append(f"{name} is a new hotspot.")
    elif resolved_hotspots:
        sentences.append(f"{resolved_hotspots[0]} left the hotspot list.")
    elif facts.get("n_new_findings"):
        sentences.append(f"{_plural(int(facts['n_new_findings']), 'finding')} appeared and {_plural(int(facts.get('n_resolved_findings') or 0), 'finding')} cleared.")

    if not sentences:
        return "No station moved enough to report against the previous brief."
    return " ".join(sentences[:3])


def render_change_summary(st: Any, diff: Any) -> None:
    """The deterministic two-to-three sentence 'what changed' paragraph."""
    _html(st, C.section("What changed, in plain words", "computed from the diff, no model involved"))
    st.markdown(change_summary_text(diff))


# ------------------------------------------------------------------ 4. prompt cockpit
_PIPELINE = (
    ("Raw table", "upload / paste", "code"),
    ("Clean & quality", "units, sentinels, flags", "code"),
    ("Findings F1..Fn", "formula + evidence rows", "code"),
    ("Gemini explain", "interpret · act · write", "llm"),
    ("Verifier", "cite + number check, repair", "code"),
    ("UI", "every claim cites a finding", "code"),
)


def pipeline_diagram() -> str:
    """Code-vs-LLM pipeline as HTML/CSS boxes, one tone per kind of work."""
    boxes = []
    for index, (title, subtitle, kind) in enumerate(_PIPELINE):
        tone = "var(--accent)" if kind == "llm" else "var(--fg-3)"
        background = "color-mix(in oklab, var(--accent) 12%, var(--surface))" if kind == "llm" else "var(--surface)"
        boxes.append(
            f'<div style="flex:1 1 120px;min-width:120px;padding:.55rem .7rem;border:1px solid '
            f'color-mix(in oklab, {tone} 40%, transparent);border-radius:10px;background:{background}">'
            f'<div style="font-weight:600;font-size:.84rem">{C.esc(title)}</div>'
            f'<div style="font-size:.75rem;color:var(--fg-3)">{C.esc(subtitle)}</div>'
            f'<div style="margin-top:.25rem;font-size:.7rem;letter-spacing:.04em;text-transform:uppercase;'
            f'color:{tone}">{C.esc("LLM" if kind == "llm" else "code")}</div></div>'
        )
        if index < len(_PIPELINE) - 1:
            boxes.append('<div style="align-self:center;color:var(--fg-3)">→</div>')
    body = "".join(boxes)
    return (
        f'<div style="display:flex;flex-wrap:wrap;gap:.4rem;align-items:stretch;margin:.3rem 0 .9rem">{body}</div>'
    )


def _prompt_text(stem: str) -> str:
    path = PROMPT_DIR / f"{stem}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _stage_result(results: Optional[dict], stem: str, label: str) -> Any:
    if not results:
        return None
    for key in (stem, label, f"{stem}.md"):
        if key in results:
            return results[key]
    return None


def cockpit_rows(results: Optional[dict] = None) -> list[dict[str, Any]]:
    """One row per prompt stage: version, model-vs-fallback, schema, verification counts."""
    versions = getattr(stages, "PROMPT_VERSIONS", {}) or {}
    rows: list[dict[str, Any]] = []
    for label, stem, schema, kind in STAGES:
        result = _stage_result(results, stem, label)
        used_fallback = getattr(result, "used_fallback", None)
        if used_fallback is None and isinstance(result, dict):
            used_fallback = result.get("used_fallback")
        if used_fallback is None:
            status = "n/a"
        else:
            status = "deterministic fallback" if used_fallback else "model"
        verification = getattr(result, "verification", None)
        if verification is None and isinstance(result, dict):
            verification = result.get("verification")
        verification = verification if isinstance(verification, dict) else {}
        rows.append(
            {
                "stage": label,
                "prompt": f"{stem}.md",
                "prompt_version": versions.get(stem, "n/a"),
                "model": status,
                "schema": schema,
                "issues_found": verification.get("issues_found", "n/a"),
                "repaired": verification.get("repaired", "n/a"),
                "remaining": verification.get("remaining", "n/a"),
            }
        )
    return rows


def render_prompt_cockpit(st: Any, brief: Any, diff: Any, results: Optional[dict] = None) -> None:
    """Prompt cockpit panel: pipeline diagram, stage table, and every prompt file behind an expander."""
    _html(st, C.section("Prompt cockpit", "code computes · Gemini explains · code verifies"))
    _html(st, pipeline_diagram())
    if brief is not None:
        area = getattr(brief, "area", "") or getattr(brief, "area_name", "")
        _html(st, C.small(f"Brief {area} · {getattr(brief, 'brief_id', '')} · {getattr(brief, 'created_at', '')}"))
    _html(st, C.chip("no arithmetic in the model", "ok") + " " + C.chip("every sentence cites a finding", "ok"))

    rows = cockpit_rows(results)
    st.dataframe(pd.DataFrame(rows), hide_index=True)

    for (label, stem, schema, kind), row in zip(STAGES, rows):
        version = row["prompt_version"]
        with st.expander(f"{label} · {stem}.md · {version}", expanded=False):
            _html(st, C.vitals([(f"schema: {schema}", "hollow"), (f"prompt_version: {version}", "hollow"),
                                (f"lanes: {kind}", "hollow"), (f"status: {row['model']}", "hollow")]))
            text = _prompt_text(stem)
            if text:
                st.code(text, language="markdown")
            else:
                _html(st, C.chip(f"{stem}.md not found", "warn"))

    contract = _prompt_text("grounding_contract")
    if contract:
        with st.expander("Shared grounding contract · grounding_contract.md", expanded=False):
            st.code(contract, language="markdown")
