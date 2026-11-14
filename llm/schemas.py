"""Pydantic output schemas for Gemini stages. Keep SHALLOW (few optionals/enums, no deep nesting):
Gemini structured output rejects very complex schemas, and minLength/maxLength are not enforced.

The `make_*` factories build the SAME shape with the real finding and library IDs as
`Literal` enums, so a model cannot cite a finding that does not exist. The static classes
are the loose schemas: they accept anything and the stage code post-validates them. A stage
tries the strict schema first and falls back to the loose one if Gemini rejects the schema
(HTTP 400).
"""
from __future__ import annotations

from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field, create_model


class Point(BaseModel):
    text: str = Field(description="One plain-language sentence. Must end with citations like [F2][F5].")
    cites: list[str] = Field(description="Finding IDs used, e.g. ['F2','F5']")


class Interpretation(BaseModel):
    headline: str = Field(description="One-sentence headline, plain language, with citations.")
    trend_points: list[Point]
    hotspot_points: list[Point]
    cannot_tell: list[str] = Field(description="What the data cannot tell us (gaps, unreliable stations).")
    used_fallback: bool = False
    verification: dict[str, Any] = Field(default_factory=dict)


class Action(BaseModel):
    rank: int
    action: str
    why_now: str = Field(description="Reason with citations like [F3].")
    cites: list[str]
    owner: str
    effort: str = Field(description="low | medium | high")
    time_window: str = Field(description="today | this week | this month")
    data_gap_action: str = Field(description="If the evidence is weak, what to verify first; else empty string.")
    library_id: str = Field(description="id from the action library, or 'suggested_new'.")


class ActionPlan(BaseModel):
    persona: str
    actions: list[Action]
    used_fallback: bool = False
    verification: dict[str, Any] = Field(default_factory=dict)


class CareBlock(BaseModel):
    children: str
    older_adults: str
    sensitive_groups: str = Field(description="People with asthma, heart/lung disease, pregnancy.")


class Advisory(BaseModel):
    language: str
    reading_level: str
    headline: str
    category: str
    today_message: str
    what_to_do: list[str]
    care: CareBlock
    source_line: str = Field(description="Date, stations used, data gap note.")
    cites: list[str]
    used_fallback: bool = False
    verification: dict[str, Any] = Field(default_factory=dict)


class InternalNote(BaseModel):
    markdown: str = Field(description="Internal briefing note in Markdown with sections Situation / What changed / Hotspots / Data caveats / Actions by owner / Open questions; citations [F#] kept.")
    used_fallback: bool = False
    verification: dict[str, Any] = Field(default_factory=dict)


class VerifyIssue(BaseModel):
    sentence: str
    problem: str


class VerifyReport(BaseModel):
    issues: list[VerifyIssue]


class AskAnswer(BaseModel):
    """One grounded answer to a question about the brief."""

    answer: str = Field(description="Two to four sentences, every sentence cited like [F2].")
    cites: list[str] = Field(description="Finding IDs used, e.g. ['F2','F5'].")
    answerable: bool = Field(description="False when the question is out of scope or the findings do not cover it.")
    follow_ups: list[str] = Field(default_factory=list, description="Up to 3 good next questions.")
    used_fallback: bool = False
    verification: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ strict schema factories


def _id_type(ids: Sequence[str]) -> Any:
    """Literal of the real IDs, or plain str when the brief has no findings."""
    unique = list(dict.fromkeys(str(i) for i in ids if str(i).strip()))
    return Literal[tuple(unique)] if unique else str  # type: ignore[valid-type]


def make_point_schema(finding_ids: Sequence[str]) -> type[BaseModel]:
    """Point with `cites` limited to the real finding IDs."""
    return create_model(
        "PointOut",
        text=(str, Field(description="One plain-language sentence ending with citations like [F2].")),
        cites=(
            list[_id_type(finding_ids)],  # type: ignore[valid-type]
            Field(description="Finding IDs used in this sentence."),
        ),
    )


def make_interpretation_schema(finding_ids: Sequence[str]) -> type[BaseModel]:
    """Interpretation whose citations can only be real finding IDs."""
    point = make_point_schema(finding_ids)
    return create_model(
        "InterpretationOut",
        headline=(str, Field(description="One-sentence headline with citations.")),
        trend_points=(list[point], Field(default_factory=list)),  # type: ignore[valid-type]
        hotspot_points=(list[point], Field(default_factory=list)),  # type: ignore[valid-type]
        cannot_tell=(list[str], Field(default_factory=list)),
    )


def make_action_plan_schema(finding_ids: Sequence[str], library_ids: Sequence[str]) -> type[BaseModel]:
    """ActionPlan whose citations and `library_id` are closed sets."""
    library = list(dict.fromkeys([str(i) for i in library_ids if str(i).strip()])) + ["suggested_new"]
    action = create_model(
        "ActionOut",
        rank=(int, Field(default=1)),
        action=(str, Field(description="One concrete action from the library.")),
        why_now=(str, Field(default="", description="Reason with a citation like [F3].")),
        cites=(list[_id_type(finding_ids)], Field(default_factory=list)),  # type: ignore[valid-type]
        owner=(str, Field(default="", description="Who carries the action out.")),
        effort=(str, Field(default="medium", description="low | medium | high")),
        time_window=(str, Field(default="this week", description="today | this week | this month")),
        data_gap_action=(str, Field(default="", description="What to verify first, else empty.")),
        library_id=(Literal[tuple(library)], Field(default="suggested_new")),  # type: ignore[valid-type]
    )
    return create_model(
        "ActionPlanOut",
        actions=(list[action], Field(default_factory=list)),  # type: ignore[valid-type]
    )


def make_advisory_schema(finding_ids: Sequence[str]) -> type[BaseModel]:
    """Advisory prose only: `language`, `reading_level`, `category` and the AQI are set in code."""
    care = create_model(
        "CareOut",
        children=(str, Field(description="Advice for children.")),
        older_adults=(str, Field(description="Advice for older adults.")),
        sensitive_groups=(str, Field(description="Advice for people with asthma, heart or lung disease, pregnancy.")),
    )
    return create_model(
        "AdvisoryOut",
        headline=(str, Field(description="One line naming today's category.")),
        today_message=(str, Field(description="What this means for you today, with a citation.")),
        what_to_do=(list[str], Field(default_factory=list)),  # type: ignore[valid-type]
        care=(care, Field(description="Three separate care blocks.")),
        source_line=(str, Field(default="", description="Date, stations used, data-gap note.")),
        cites=(list[_id_type(finding_ids)], Field(default_factory=list)),  # type: ignore[valid-type]
    )


def make_ask_schema(finding_ids: Sequence[str]) -> type[BaseModel]:
    """AskAnswer whose citations can only be real finding IDs."""
    return create_model(
        "AskAnswerOut",
        answer=(str, Field(description="Two to four sentences, every sentence cited like [F2].")),
        cites=(list[_id_type(finding_ids)], Field(default_factory=list)),  # type: ignore[valid-type]
        answerable=(bool, Field(default=True, description="False when out of scope or not covered by findings.")),
        follow_ups=(list[str], Field(default_factory=list)),  # type: ignore[valid-type]
    )
