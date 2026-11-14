"""Cheap deterministic verifier: citations must exist, numbers must be grounded.

A number in generated text is supported when it appears in a Finding's facts
(or in `extra_facts`) within a rounding tolerance of 0.5% or 0.5 absolute, or
when it is trivially safe (1-5, 8, 16, 24, years, dates, times, ordinals).

Text is checked in any supported script: Indic digits are normalised to ASCII
first, so "३०५" is checked as 305, and "।" ends a sentence like "." does.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from pydantic import BaseModel

from core.models import Finding

IssueKind = Literal["unknown_citation", "uncited_claim", "unsupported_number"]

#: Absolute numbers a sentence may use without a fact.
SAFE_NUMBERS = frozenset({0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 8.0, 16.0, 24.0})

_CITE = re.compile(r"\[([A-Za-z]\d{1,3})\]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?।])\s+|\n+")
#: Indic digit sets (Devanagari, Bengali, Kannada, Tamil, Telugu, Gujarati) -> ASCII.
_DIGITS = str.maketrans(
    "०१२३४५६७८९"
    "০১২৩৪৫৬৭৮৯"
    "೦೧೨೩೪೫೬೭೮೯"
    "௦௧௨௩௪௫௬௭௮௯"
    "౦౧౨౩౪౫౬౭౮౯"
    "૦૧૨૩૪૫૬૭૮૯",
    "0123456789" * 6,
)


def normalise_digits(text: str) -> str:
    """Rewrite Indic digits as ASCII so one number check covers every language."""
    return (text or "").translate(_DIGITS)

_BULLET = re.compile(r"^[-*•]\s+|^\d{1,2}[.)]\s+")
_HEADING = re.compile(r"^#{1,6}\s*")
_BRACKETS = re.compile(r"\[[^\]]*\]")
_DATES = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"          # 2026-11-03
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"   # 03/11/2026
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"  # 19:00
)
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:,\d{3})*(?:\.\d+)?)(?![A-Za-z0-9])")
_ORDINAL = ("st", "nd", "rd", "th")
_NUMERIC_STRING = re.compile(r"-?\d+(?:\.\d+)?")
_CLAIM_HINTS = (
    "dominant",
    "worst",
    "highest",
    "lowest",
    "increased",
    "decreased",
    "improved",
    "worsened",
    "hotspot",
    "above the",
    "below the",
    "consistent with",
    "unreliable",
    "trend",
    "peak",
)


@dataclass
class Issue:
    sentence: str
    kind: IssueKind
    detail: str = ""


def _numbers(value: Any, out: list[float]) -> None:
    """Collect every numeric value in `value`, recursing into containers."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.append(float(value))
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if _NUMERIC_STRING.fullmatch(text):
            out.append(float(text))
    elif isinstance(value, Mapping):
        for item in value.values():
            _numbers(item, out)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _numbers(item, out)


def _close(a: float, b: float) -> bool:
    if abs(a - b) <= 0.5:
        return True
    return b != 0.0 and abs(a - b) / abs(b) <= 0.005


def _supported(number: float, values: list[float]) -> bool:
    # Signed match first; then magnitude, so "falling by 2.5" matches a fact of -2.5.
    for value in values:
        if _close(number, value) or _close(abs(number), abs(value)):
            return True
    return False


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for raw in _SENTENCE_SPLIT.split(text or ""):
        sentence = raw.strip()
        if not sentence or _HEADING.match(sentence):
            continue
        sentence = _BULLET.sub("", sentence).strip()
        if sentence:
            out.append(sentence)
    return out


def _place_names(
    findings: list[Finding], extra_facts: Mapping[str, Any] | None
) -> list[str]:
    """Station, ward and other text names from the facts: never cited as numbers."""
    names: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, str) and len(value.strip()) >= 2:
            names.add(value.strip())
        elif isinstance(value, Mapping):
            for item in value.values():
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    for finding in findings:
        for name in finding.stations:
            collect(name)
        collect(finding.facts)
    if extra_facts:
        collect(dict(extra_facts))
    return sorted(names, key=len, reverse=True)


def _numbers_in(sentence: str, place_names: list[str] = ()) -> list[float]:
    """Numbers worth checking: no citations, dates, names, ordinals."""
    body = _BRACKETS.sub(" ", sentence)
    body = _DATES.sub(" ", body)
    for name in place_names:
        body = re.sub(re.escape(name), " ", body, flags=re.IGNORECASE)
    found: list[float] = []
    for match in _NUMBER.finditer(body):
        token = match.group(1)
        if body[match.end() : match.end() + 2].lower() in _ORDINAL:
            continue
        value = float(token.replace(",", ""))
        if token.replace(",", "").isdigit() and len(token.replace(",", "")) == 4:
            if 1900 <= value <= 2100:
                continue  # a year
        found.append(value)
    return found


def check_text(
    text: str,
    findings: list[Finding],
    extra_facts: Mapping[str, Any] | None = None,
    require_citations_for_numbers: bool = True,
) -> list[Issue]:
    """Report unknown citations, uncited claims and unsupported numbers.

    With `require_citations_for_numbers=False` a sentence that states a number
    without a [F#] citation is not reported as an uncited claim (the number is
    still checked against the facts).
    """
    text = normalise_digits(text)
    known = {f.id for f in findings}
    values: list[float] = []
    for finding in findings:
        _numbers(finding.facts, values)
    if extra_facts:
        _numbers(dict(extra_facts), values)
    names = _place_names(findings, extra_facts)

    issues: list[Issue] = []
    for sentence in _sentences(text):
        cited = _CITE.findall(sentence)
        has_cite = bool(cited)
        for citation in cited:
            if citation not in known:
                issues.append(
                    Issue(
                        sentence=sentence,
                        kind="unknown_citation",
                        detail=f"cites unknown finding id {citation}",
                    )
                )
        numbers = _numbers_in(sentence, names)
        for value in numbers:
            if value in SAFE_NUMBERS:
                continue
            if not _supported(value, values):
                issues.append(
                    Issue(
                        sentence=sentence,
                        kind="unsupported_number",
                        detail=f"number {value:g} is not in any finding fact",
                    )
                )
        if not has_cite and numbers and require_citations_for_numbers:
            issues.append(
                Issue(
                    sentence=sentence,
                    kind="uncited_claim",
                    detail="states a number without citing a finding",
                )
            )
        elif not has_cite and not numbers and not sentence.endswith(":") and any(
            hint in sentence.lower() for hint in _CLAIM_HINTS
        ):
            issues.append(
                Issue(
                    sentence=sentence,
                    kind="uncited_claim",
                    detail="claim without a finding citation",
                )
            )
    return _dedupe(issues)


def check_object(
    obj: Any,
    findings: list[Finding],
    extra_facts: Mapping[str, Any] | None = None,
) -> list[Issue]:
    """Apply the text checks to every string field (and `cites` list) of `obj`."""
    known = {f.id for f in findings}
    issues: list[Issue] = []

    def walk(value: Any) -> None:
        if isinstance(value, BaseModel):
            walk(value.model_dump())
        elif isinstance(value, Mapping):
            for key, item in value.items():
                if key == "cites" and isinstance(item, (list, tuple)):
                    for cite in item:
                        cite_id = str(cite).strip().strip("[]").strip()
                        if cite_id and cite_id not in known:
                            issues.append(
                                Issue(
                                    sentence=cite_id,
                                    kind="unknown_citation",
                                    detail=f"cites unknown finding id {cite_id}",
                                )
                            )
                else:
                    walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            issues.extend(check_text(value, findings, extra_facts))

    walk(obj)
    return _dedupe(issues)


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Issue] = []
    for issue in issues:
        key = (issue.sentence, issue.kind, issue.detail)
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out
