"""Ask the briefing: one grounded answer to one question about a brief.

P7 answers from FINDINGS, QUALITY and DIFF only, with citations constrained to the real
finding IDs, and the answer goes through the same check -> repair loop as every other stage.
Offline, a keyword matcher builds an answer from the same data, so the feature works with no
API key: it refuses medical and out-of-scope questions and says "insufficient evidence" when
the findings do not cover the question.
"""
from __future__ import annotations

import re
from typing import Any

from core.models import Brief, DiffResult
from llm.schemas import AskAnswer, make_ask_schema

from . import fallback, stages

_INTENT_WORDS = {
    "trust": ("trust", "reliable", "reliability", "accurate", "sensor", "calibrat", "believe", "valid"),
    "change": ("change", "changed", "since", "last week", "worse", "better", "improve", "compare", "difference"),
    "outdoor": ("outdoor", "outside", "play", "sport", "sports", "run", "walk", "assembly", "pe", "exercise"),
    "cause": ("why", "cause", "caused", "source", "coming from", "reason", "responsible"),
    "time": ("when", "time", "hour", "evening", "morning", "peak", "night", "today", "tonight"),
}
_MEDICAL_WORDS = (
    "medicine",
    "medicines",
    "medication",
    "dose",
    "dosage",
    "inhaler",
    "diagnos",
    "diagnosis",
    "cure",
    "treat",
    "treatment",
    "symptom",
    "prescription",
    "is it safe for my asthma",
    "hospital",
)
_OUT_OF_SCOPE_WORDS = (
    "weather",
    "forecast",
    "rain",
    "temperature",
    "next month",
    "next year",
    "tomorrow",
    "election",
    "politics",
    "price",
    "cost",
    "budget",
    "stock",
    "traffic count",
    "vehicle count",
    "population",
)
_MAX_ANSWER_FINDINGS = 3


def answer(question: str, brief: Brief, diff: DiffResult, persona: str) -> AskAnswer:
    """One cited answer to one question, never raising."""
    persona_key = (persona or "ward_officer").strip().lower()
    text = " ".join((question or "").split())
    if not text:
        return stages.record_verification(
            AskAnswer(
                answer="insufficient evidence: no question was asked.",
                cites=[],
                answerable=False,
                follow_ups=starter_questions(brief, diff, persona_key)[:3],
                used_fallback=True,
            ),
            brief,
            diff=diff,
        )

    schema_name = "AskAnswer"
    try:
        key = stages.stage_key(
            "ask",
            "p7_ask",
            schema_name,
            stages.brief_fingerprint(brief),
            stages.diff_fingerprint(diff),
            {"question": text.lower(), "persona": persona_key},
        )

        def live() -> AskAnswer:
            system, user = stages.load_prompt("p7_ask")
            spec = fallback.PERSONA_BRIEFS.get(persona_key, {"authority": "", "limits": ""})
            snapshot = fallback.brief_snapshot(brief)
            prompt = fallback.fill_template(
                user,
                PERSONA=persona_key,
                AUTHORITY=spec["authority"],
                LIMITS=spec["limits"],
                CATEGORY=snapshot["category"] or "insufficient evidence",
                PEAK_WINDOW=snapshot["peak_window"] or "not identified",
                QUESTION=text,
                FINDINGS=stages._findings_json(brief),
                QUALITY=stages._quality_json(brief),
                DIFF=stages._diff_json(diff),
            )
            raw = stages._structured(
                prompt,
                system,
                make_ask_schema(stages._finding_ids(brief)),
                AskAnswer,
                stages.TEMP_ANALYSIS,
            )
            return AskAnswer.model_validate(raw.model_dump())

        result = stages._live_or_cached(key, AskAnswer, live)
        cleaned = _clean_answer(result, brief) if result is not None else None
        if cleaned is not None:
            return stages.finish_stage(
                cleaned,
                brief,
                stage="ask",
                strict=make_ask_schema(stages._finding_ids(brief)),
                to_public=lambda raw: AskAnswer.model_validate(raw.model_dump()),
                clean=lambda obj: _clean_answer(obj, brief),
                drop=lambda obj, offending: _drop_answer(obj, offending),
                diff=diff,
                text_of=lambda obj: obj.answer,
            )
    except Exception:  # noqa: BLE001 - fall through to the keyword answer
        pass
    return stages.record_verification(
        _fallback_answer(text, brief, diff, persona_key), brief, diff=diff
    )


def starter_questions(brief: Brief, diff: DiffResult, persona: str) -> list[str]:
    """Four or five questions this brief can actually answer, for one persona."""
    persona_key = (persona or "ward_officer").strip().lower()
    snapshot = fallback.brief_snapshot(brief)
    worst = snapshot["worst_station"] or "the worst station"
    risky = (snapshot["unreliable"] or snapshot["caution"] or [None])[0]
    peak = snapshot["peak_window"]
    dominant = snapshot["dominant"]
    questions: list[str] = []

    if persona_key == "school":
        questions.append(f"Is it safe to hold outdoor sports at our school this week?")
        questions.append(f"Should we move assembly indoors at {worst}?")
        if risky:
            questions.append(f"Can I trust {risky}'s numbers?")
        if peak:
            questions.append(f"Which hours are worst for outdoor play?")
        questions.append("What should we do about vehicles idling at the gate?")
    elif persona_key == "housing_society":
        questions.append("Can we hold the society event outdoors this evening?")
        questions.append("Should we run the diesel generator tonight?")
        if risky:
            questions.append(f"Can I trust {risky}'s numbers?")
        if dominant:
            questions.append(f"What is driving {dominant} in our area?")
        questions.append("Which residents need a welfare check this week?")
    else:
        questions.append(f"Why is {worst} the highest station this week?")
        if risky:
            questions.append(f"Can I trust {risky}'s numbers?")
        if peak:
            questions.append(f"Is the evening peak traffic or burning?")
        questions.append("What changed since the last brief?")
        questions.append("Which action should go out today?")
    if diff.has_previous and len(questions) < 5:
        questions.append("What changed since the last brief?")
    return questions[:5]


# ------------------------------------------------------------------ cleanup


def _clean_answer(result: AskAnswer, brief: Brief) -> AskAnswer | None:
    """Keep only known citations and a usable answer; None when unusable."""
    known = {finding.id for finding in brief.findings}
    body = stages._strip_unknown_cites(result.answer.strip(), known)
    if not body:
        return None
    cites = [cite for cite in stages._cite_ids(result.cites) if cite in known]
    if result.answerable and not cites:
        return None  # an answerable answer must trace to a finding
    follow_ups = [
        stages._strip_unknown_cites(item, known)
        for item in result.follow_ups
        if item.strip()
    ][:3]
    return result.model_copy(
        update={"answer": body, "cites": cites, "follow_ups": follow_ups, "used_fallback": False}
    )


def _drop_answer(obj: AskAnswer, offending: set[str]) -> AskAnswer:
    """A wrong sentence in an answer cannot be dropped alone, so refuse safely."""
    if not stages._hits(obj.answer, offending):
        return obj
    return obj.model_copy(
        update={
            "answer": "insufficient evidence in this brief for that question.",
            "cites": [],
            "answerable": False,
            "follow_ups": [item for item in obj.follow_ups if not stages._hits(item, offending)][:3],
        }
    )


# ------------------------------------------------------------------ offline answer


def _fallback_answer(question: str, brief: Brief, diff: DiffResult, persona: str) -> AskAnswer:
    """Keyword match of the question against findings, stations, quality and the diff."""
    lowered = question.lower()
    follow_ups = starter_questions(brief, diff, persona)[:3]
    snapshot = fallback.brief_snapshot(brief)

    if _matches(lowered, _MEDICAL_WORDS):
        return AskAnswer(
            answer=(
                "A doctor must answer health questions for you or your family; this briefing cannot "
                "diagnose or advise on medicine. The data-based guidance is general: follow the care "
                f"steps for the {snapshot['category'] or 'current'} category and contact a doctor if "
                "breathing becomes difficult."
            ),
            cites=[],
            answerable=False,
            follow_ups=follow_ups,
            used_fallback=True,
        )
    if _matches(lowered, _OUT_OF_SCOPE_WORDS):
        return AskAnswer(
            answer=(
                "insufficient evidence: this briefing has no forecast, traffic counts or price data, "
                "so it cannot answer that. Ask about the stations, the category or what changed since "
                "the last brief."
            ),
            cites=[],
            answerable=False,
            follow_ups=follow_ups,
            used_fallback=True,
        )

    findings = _ranked_findings(question, brief)
    if not findings:
        return AskAnswer(
            answer="insufficient evidence in this brief for that question.",
            cites=[],
            answerable=False,
            follow_ups=follow_ups,
            used_fallback=True,
        )

    sentences = [_sentence(finding) for finding in findings[:_MAX_ANSWER_FINDINGS]]
    if _matches(lowered, _INTENT_WORDS["outdoor"]):
        guidance = _outdoor_sentence(brief, persona, snapshot)
        if guidance:
            sentences.insert(0, guidance)
    if _matches(lowered, _INTENT_WORDS["change"]) and diff.has_previous:
        sentences.insert(0, _change_sentence(diff, brief))
    return AskAnswer(
        answer=" ".join(sentence for sentence in sentences if sentence),
        cites=_unique_cites(sentences),
        answerable=True,
        follow_ups=follow_ups,
        used_fallback=True,
    )


def _ranked_findings(question: str, brief: Brief) -> list[Any]:
    """Findings that best answer the question, best first."""
    lowered = question.lower()
    tokens = {token for token in re.findall(r"[a-z]{4,}", lowered)}
    scored: list[tuple[int, int, Any]] = []
    for finding in brief.findings:
        score = 0
        text = f"{finding.title} {finding.type} {' '.join(finding.stations)}".lower()
        if any(station.lower() in lowered for station in finding.stations):
            score += 4
        score += sum(1 for token in tokens if token in text)
        if _matches(lowered, _INTENT_WORDS["trust"]) and finding.type in ("gap", "anomaly"):
            score += 5
        if _matches(lowered, _INTENT_WORDS["time"]) and finding.type == "pattern":
            score += 4
        if _matches(lowered, _INTENT_WORDS["cause"]) and finding.type in ("pattern", "exceedance"):
            score += 3
        if _matches(lowered, _INTENT_WORDS["outdoor"]) and finding.type in ("hotspot", "gap", "anomaly"):
            score += 2
        if finding.type == "hotspot":
            score += 1
        scored.append((score, -int(re.sub(r"\D", "", finding.id) or 0), finding))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    best = [finding for score, _order, finding in scored if score > 0]
    if not best and brief.findings:
        best = [fallback._ordered_findings(brief)[0]]
    return best


def _sentence(finding: Any) -> str:
    """One cited sentence from a finding, using the shared fallback wording."""
    return fallback._point(finding).text


_OUTDOOR_ADVICE = {
    "Good": "Outdoor activity is fine",
    "Satisfactory": "Outdoor activity is fine for most people",
    "Moderately Polluted": "Keep outdoor activity short and away from busy roads",
    "Poor": "Move outdoor games and sport indoors",
    "Very Poor": "Stop outdoor activity and keep children indoors",
    "Severe": "Stay indoors and stop all outdoor activity",
}
_PERSONA_PLACE = {
    "school": " at school",
    "housing_society": " in the society",
    "ward_officer": " in the ward",
}


def _outdoor_sentence(brief: Brief, persona: str, snapshot: dict[str, Any]) -> str:
    """Category-based outdoor guidance for an activity question, with a finding citation."""
    advice = _OUTDOOR_ADVICE.get(snapshot["category"] or "")
    cite = fallback._top_cite(brief)
    if not advice or not cite:
        return ""
    text = f"{advice}{_PERSONA_PLACE.get(persona, '')}"
    if snapshot["peak_window"]:
        text += f"; the worst hours are {snapshot['peak_window']}"
    return f"{text} [{cite}]."


def _change_sentence(diff: DiffResult, brief: Brief) -> str:
    """One cited sentence about what changed since the last brief."""
    cite = fallback._top_cite(brief)
    facts = diff.summary_facts or {}
    worse = facts.get("n_worse")
    better = facts.get("n_better")
    if worse is None and better is None:
        return ""
    body = f"Since the last brief, {worse} station(s) got worse and {better} improved"
    if diff.category_transitions:
        body += f"; {diff.category_transitions[0]}"
    return f"{body} [{cite}]." if cite else f"{body}."


def _unique_cites(sentences: list[str]) -> list[str]:
    cites: list[str] = []
    for sentence in sentences:
        for cite in re.findall(r"\[([A-Z]\d{1,3})\]", sentence):
            if cite not in cites:
                cites.append(cite)
    return cites


def _matches(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)
