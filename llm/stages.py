"""Gemini stages P1-P6 (P7, Ask the briefing, lives in llm.ask).

Every function returns a valid object and never raises: a failed call, a bad answer or a
missing key falls back to the deterministic templates in llm.fallback.

Two ideas hold the layer together:

* **Closed schemas.** Each call builds its output schema with the real finding IDs (and action
  library IDs) as `Literal` enums, so the model cannot cite something that does not exist.
  If Gemini rejects the schema (HTTP 400), the call is retried once with the loose schema and
  post-validated here.
* **Generate -> check -> repair.** After every generative stage, `core.verify.check_object`
  plus a language-neutral digit-citation rule run on the result. If there are issues, ONE
  repair call (p6b_repair) gets the draft and the issue list, then the checks run again. What
  is still wrong is dropped (or the stage falls back), and the counts are recorded on the
  result in `verification`.
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence, TypeVar

from pydantic import BaseModel

from core import verify as code_verify
from core.models import CATEGORIES, POLLUTANTS, Brief, ColumnMapping, ColumnRole, DiffResult
from core.verify import Issue
from llm.schemas import (
    ActionPlan,
    Advisory,
    CareBlock,
    InternalNote,
    Interpretation,
    Point,
    VerifyIssue,
    VerifyReport,
    make_action_plan_schema,
    make_advisory_schema,
    make_interpretation_schema,
)

from . import client, demo_cache, fallback
from .fallback import fill_template

if TYPE_CHECKING:  # core/ingest.py is owned by another agent and may land later
    from core.ingest import RawTable

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
CONTRACT_PROMPT = "grounding_contract"
REPAIR_PROMPT = "p6b_repair"
MAX_ACTIONS = 5
MAX_VERIFY_ISSUES = 8
# PLAN.md section 5: low temperature for analysis, a little higher for citizen text.
TEMP_ANALYSIS = 0.2
TEMP_ADVISORY = 0.5
#: Drafts at least this long get the P6 model verifier once the cheap checks pass.
P6_MIN_CHARS = 900

#: Prompt files and their version headers. Bump the version AND the file header together;
#: the cache key includes the version, so a prompt change never replays an old answer.
PROMPT_VERSIONS: dict[str, str] = {
    "grounding_contract": "contract.v1",
    "p1_mapper": "p1.v1",
    "p2_interpreter": "p2.v1",
    "p3_actioner": "p3.v1",
    "p4_internal_note": "p4.v1",
    "p5_advisory": "p5.v2",
    "p6_verifier": "p6.v1",
    "p6b_repair": "p6b.v1",
    "p7_ask": "p7.v1",
}

T = TypeVar("T", bound=BaseModel)

_VERSION_LINE = re.compile(r"^prompt_version:\s*(\S+)\s*$", re.MULTILINE)
_CITE_PATTERN = re.compile(r"\[([A-Za-z]\d{1,3})\]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?।])\s+|\n+")
_STANDALONE_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)*(?![A-Za-z0-9])")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.\-]{2,60}")
_CATEGORY_PATTERN = re.compile(
    "|".join(re.escape(category) for category in sorted(CATEGORIES, key=len, reverse=True)),
    re.IGNORECASE,
)
_AQI_LABELS = re.compile(r"(?i)\b(aqi|index|सूचकांक)\b[^0-9]{0,4}(\d{2,4}(?:\.\d+)?)")
_NON_LATIN_RANGES = (
    (0x0900, 0x097F),  # Devanagari: Hindi, Marathi
    (0x0980, 0x09FF),  # Bengali
    (0x0A00, 0x0A7F),  # Gurmukhi
    (0x0A80, 0x0AFF),  # Gujarati
    (0x0B80, 0x0BFF),  # Tamil
    (0x0C00, 0x0C7F),  # Telugu
    (0x0C80, 0x0CFF),  # Kannada
)

_ALIASES = {
    "PM25": "PM2.5",
    "PARTICULATE25": "PM2.5",
    "PM10": "PM10",
    "NO2": "NO2",
    "NITROGENDIOXIDE": "NO2",
    "SO2": "SO2",
    "SULPHURDIOXIDE": "SO2",
    "SULFURDIOXIDE": "SO2",
    "O3": "O3",
    "OZONE": "O3",
    "CO": "CO",
    "CARBONMONOXIDE": "CO",
    "NH3": "NH3",
    "AMMONIA": "NH3",
}

#: Advice that is unsafe from Poor upwards, or over-alarming at Satisfactory or better.
_UNSAFE_WHEN_BAD = (
    "safe to exercise",
    "safe to go for a run",
    "safe to run",
    "go for a run",
    "go for a long run",
    "run outdoors",
    "run outside",
    "exercise outdoors",
    "no need for a mask",
    "no mask needed",
    "air is clean",
    "outdoor activity is fine",
    "normal outdoor activity is fine",
    "children can play outside",
    "safe to go outside",
    "बाहर की गतिविधि ठीक है",
    "बाहर खेलना ठीक है",
)
_UNSAFE_WHEN_GOOD = (
    "stay indoors",
    "do not go out",
    "avoid all outdoor",
    "cancel outdoor",
    "no outdoor school activity",
    "घर के अंदर रहें",
)


# ------------------------------------------------------------------ prompt plumbing


def _prompt_text(name: str) -> str:
    """Raw prompt file text."""
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def prompt_version(name: str) -> str:
    """Version header of one prompt file, e.g. 'p2.v1'."""
    found = _VERSION_LINE.search(_prompt_text(name))
    if not found:
        raise ValueError(f"prompt {name!r} needs a 'prompt_version: pN.vM' header line")
    return found.group(1)


@lru_cache(maxsize=None)
def grounding_contract() -> str:
    """The one shared grounding contract, inlined into every stage prompt."""
    return _VERSION_LINE.sub("", _prompt_text(CONTRACT_PROMPT), count=1).strip()


@lru_cache(maxsize=None)
def load_prompt(name: str) -> tuple[str, str]:
    """(system, user template) from llm/prompts/<name>.md, contract already inlined."""
    text = _VERSION_LINE.sub("", _prompt_text(name), count=1)
    system_at = re.search(r"^##\s*SYSTEM\s*$", text, re.MULTILINE)
    user_at = re.search(r"^##\s*USER\s*$", text, re.MULTILINE)
    if not system_at or not user_at or user_at.start() < system_at.end():
        raise ValueError(f"prompt {name!r} needs a SYSTEM section then a USER section")
    system = text[system_at.end() : user_at.start()].strip()
    user = text[user_at.end() :].strip()
    if not system or not user:
        raise ValueError(f"prompt {name!r} has an empty SYSTEM or USER section")
    system = system.replace("{CONTRACT}", grounding_contract())
    return system, user


def _json(value: Any) -> str:
    """Compact JSON for a prompt placeholder."""
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _findings_json(brief: Brief) -> str:
    return _json([finding.model_dump(mode="json") for finding in brief.findings])


def _quality_json(brief: Brief) -> str:
    return _json(brief.quality.model_dump(mode="json"))


def _diff_json(diff: DiffResult) -> str:
    return _json(diff.model_dump(mode="json"))


def _period(brief: Brief) -> str:
    if brief.period_start and brief.period_end:
        return f"{brief.period_start} to {brief.period_end}"
    return brief.period_end or brief.period_start or "period not recorded"


def _cite_ids(values: list[str] | None) -> list[str]:
    """Normalise ['[F2]', 'F5'] to ['F2', 'F5']."""
    ids: list[str] = []
    for value in values or []:
        match = re.search(r"[A-Z]\d+", str(value).upper())
        if match and match.group(0) not in ids:
            ids.append(match.group(0))
    return ids


def _brief_payload(brief: Brief) -> dict[str, Any]:
    """Data fingerprint of a brief for a cache key: no brief_id, no created_at."""
    return {
        "area": brief.area,
        "source_hash": brief.source_hash,
        "period": [brief.period_start, brief.period_end],
        "stations": [station.model_dump(mode="json") for station in brief.station_aqi],
        "findings": [finding.model_dump(mode="json") for finding in brief.findings],
        "quality": brief.quality.model_dump(mode="json"),
    }


def _diff_payload(diff: DiffResult) -> dict[str, Any]:
    """Diff fingerprint for a cache key: no previous_brief_id."""
    return diff.model_dump(mode="json", exclude={"previous_brief_id"})


def _cache_key(stage: str, prompt_name: str, schema_name: str, *parts: Any) -> str:
    """Key = stage + prompt version + schema name + stable data fingerprint."""
    return demo_cache.run_key(stage, prompt_version(prompt_name), schema_name, list(parts))



def stage_key(stage: str, prompt_name: str, schema_name: str, *parts: Any) -> str:
    """Public cache-key helper: the same key a stage uses for the same inputs."""
    return _cache_key(stage, prompt_name, schema_name, *parts)


def brief_fingerprint(brief: Brief) -> dict[str, Any]:
    """Data fingerprint of a brief (no brief_id, no created_at), for cache keys."""
    return _brief_payload(brief)


def diff_fingerprint(diff: DiffResult) -> dict[str, Any]:
    """Data fingerprint of a diff (no previous_brief_id), for cache keys."""
    return _diff_payload(diff)

def _live_or_cached(cache_key: str, schema: type[T], live: Callable[[], T]) -> T | None:
    """Real call (recorded in the demo cache) or a cached replay; None if neither."""
    if client.llm_available():
        try:
            result = live()
            if isinstance(result, schema):
                demo_cache.save_run(cache_key, result)
                return result
        except Exception:  # noqa: BLE001 - any failure means "use the fallback"
            pass
    try:
        cached = demo_cache.load_run(cache_key)
        if cached is not None:
            return schema.model_validate(cached)
    except Exception:  # noqa: BLE001 - corrupt cache entry
        pass
    return None


def _schema_rejected(exc: BaseException) -> bool:
    """True when Gemini answered 400, its usual 'I cannot use this JSON schema' error."""
    return client.status_of(exc) == 400


def _structured(
    prompt: str,
    system: str,
    strict: type[BaseModel] | None,
    loose: type[BaseModel],
    temperature: float,
) -> BaseModel:
    """Strict schema first; on a 400 retry once with the loose schema, then post-validate."""
    if strict is not None:
        try:
            return client.generate_json(prompt, system, strict, temperature=temperature)
        except Exception as exc:  # noqa: BLE001 - only a rejected schema is retried
            if not _schema_rejected(exc):
                raise
    return client.generate_json(prompt, system, loose, temperature=temperature)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(value: Any) -> str:
    """Same number formatting as the fallbacks, for prompt and cache stability."""
    if not _finite(value):
        return "not available"
    if float(value).is_integer():
        return f"{int(value)}"
    return f"{value:.1f}" if abs(value) < 10 else f"{value:.0f}"


def _finding_ids(brief: Brief) -> list[str]:
    return [finding.id for finding in brief.findings]


# ------------------------------------------------------------------ verification helpers


def _strings(obj: Any) -> list[str]:
    """Every string value in a pydantic model, dict or list."""
    if isinstance(obj, BaseModel):
        return _strings(obj.model_dump())
    if isinstance(obj, dict):
        return [text for value in obj.values() for text in _strings(value)]
    if isinstance(obj, (list, tuple)):
        return [text for value in obj for text in _strings(value)]
    if isinstance(obj, str):
        return [obj]
    return []


def _is_non_english(text: str) -> bool:
    """True when most letters are outside Latin, i.e. a translated advisory."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    foreign = sum(1 for ch in letters if ord(ch) > 0x7F)
    return foreign / len(letters) > 0.3


def _labelled_line(sentence: str) -> bool:
    """'Station AQI: Hadapsar 338' style lines are number listings, not claims."""
    head = sentence.split(":", 1)[0]
    return ":" in sentence and len(head) <= 40


def _digit_cite_issues(text: str) -> list[Issue]:
    """Non-English text: a sentence with a digit needs a [F#] or a labelled numbers line.

    `core.verify` only knows English claim words, so uncited numbers in Hindi or Marathi pass
    it. This is the language-neutral half of the same rule.
    """
    if not _is_non_english(text):
        return []
    issues: list[Issue] = []
    for raw in _SENTENCE_SPLIT.split(text):
        sentence = raw.strip()
        if not sentence or not _STANDALONE_NUMBER.search(sentence):
            continue
        if _CITE_PATTERN.search(sentence) or _labelled_line(sentence):
            continue
        issues.append(
            Issue(
                sentence=sentence,
                kind="uncited_claim",
                detail="a number in non-English text needs a [F#] citation",
            )
        )
    return issues


def _dedupe(issues: Sequence[Issue]) -> list[Issue]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Issue] = []
    for issue in issues:
        key = (issue.sentence, issue.kind, issue.detail)
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


def _extra_facts(brief: Brief, diff: DiffResult | None = None) -> dict[str, Any]:
    """Numbers a draft may cite besides finding facts.

    The diff summary, the reading counts, the flag counts and the numbers inside the plain
    quality-gap statements ("only 14 valid hours") are all grounded input, so they are citable.
    """
    facts: dict[str, Any] = dict(diff.summary_facts) if diff is not None else {}
    facts["n_readings"] = brief.quality.n_readings
    facts["n_rows_in"] = brief.quality.n_rows_in
    for flag, count in (brief.quality.flag_counts or {}).items():
        facts[f"flag_{flag}"] = count
    for index, gap in enumerate(brief.quality.gaps or []):
        for offset, number in enumerate(re.findall(r"\d+(?:\.\d+)?", gap or "")):
            facts[f"gap_{index}_{offset}"] = float(number)
    return facts


def _keep_issue(issue: Issue) -> bool:
    """Identifiers (library ids) cannot be claims; a question cannot state a number."""
    body = issue.sentence.strip()
    if not body:
        return False
    if _IDENTIFIER.fullmatch(body):
        return False
    if body.endswith("?") and issue.kind != "unknown_citation":
        return False
    return True


def verification_issues(
    obj: Any,
    brief: Brief,
    diff: DiffResult | None = None,
) -> list[Issue]:
    """Cheap code checks for one stage result: citations, numbers, non-English digits."""
    found = [
        *code_verify.check_object(obj, brief.findings, _extra_facts(brief, diff)),
        *[issue for text in _strings(obj) for issue in _digit_cite_issues(text)],
    ]
    return _dedupe([issue for issue in found if _keep_issue(issue)])


def category_contradictions(text: str, category: str | None) -> list[str]:
    """Phrases in `text` that contradict the CPCB category (empty when the advice fits)."""
    if not category or category not in CATEGORIES:
        return []
    severity = CATEGORIES.index(category)
    lowered = (text or "").lower()
    pool = _UNSAFE_WHEN_BAD if severity >= CATEGORIES.index("Poor") else _UNSAFE_WHEN_GOOD
    if CATEGORIES.index("Poor") > severity > CATEGORIES.index("Satisfactory"):
        return []  # the mid band: neither "safe to run" nor "stay indoors" is a hard error
    return [phrase for phrase in pool if phrase in lowered]


def _hits(text: str, offending: set[str]) -> bool:
    """True when `text` contains one of the offending sentences (or the reverse)."""
    body = " ".join((text or "").lower().split())
    if not body:
        return False
    return any(item and (item in body or body in item) for item in offending)


def _offenders(issues: Sequence[Issue]) -> set[str]:
    return {issue.sentence.strip().lower() for issue in issues if issue.sentence.strip()}


def _strip_unknown_cites(text: str, known: set[str]) -> str:
    """Remove citation markers such as [F99] that name no finding."""

    def swap(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1).upper() in known else ""

    cleaned = _CITE_PATTERN.sub(swap, text or "")
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def _drop_interpretation(obj: Interpretation, offending: set[str]) -> Interpretation:
    return obj.model_copy(
        update={
            "trend_points": [point for point in obj.trend_points if not _hits(point.text, offending)],
            "hotspot_points": [point for point in obj.hotspot_points if not _hits(point.text, offending)],
            "cannot_tell": [item for item in obj.cannot_tell if not _hits(item, offending)],
        }
    )


def _drop_plan(obj: ActionPlan, offending: set[str]) -> ActionPlan:
    kept = [
        action
        for action in obj.actions
        if not (
            _hits(action.action, offending)
            or _hits(action.why_now, offending)
            or _hits(action.data_gap_action, offending)
        )
    ]
    return obj.model_copy(
        update={"actions": [action.model_copy(update={"rank": index}) for index, action in enumerate(kept, 1)]}
    )


def _drop_note(obj: InternalNote, offending: set[str]) -> InternalNote:
    lines = [
        line
        for line in obj.markdown.splitlines()
        if not _hits(line.lstrip("-* ").strip(), offending)
    ]
    return obj.model_copy(update={"markdown": "\n".join(lines)})


def _drop_advisory(obj: Advisory, offending: set[str]) -> Advisory:
    return obj.model_copy(
        update={"what_to_do": [item for item in obj.what_to_do if not _hits(item, offending)]}
    )


def _repair(
    obj: T,
    brief: Brief,
    issues: Sequence[Issue],
    *,
    stage: str,
    strict: type[BaseModel] | None,
    to_public: Callable[[BaseModel], T | None],
    clean: Callable[[T], T | None],
    category: str | None,
) -> T | None:
    """One p6b repair call: the draft plus the issue list, same schema back."""
    if not issues:
        return None
    draft = obj.model_dump(mode="json")
    issue_payload = [
        {"sentence": issue.sentence, "problem": f"{issue.kind}: {issue.detail}".strip(": ")}
        for issue in issues[:MAX_VERIFY_ISSUES]
    ]
    key = _cache_key(
        f"repair-{stage}",
        REPAIR_PROMPT,
        stage,
        {"draft": draft, "issues": issue_payload},
    )

    def live() -> T:
        system, user = load_prompt(REPAIR_PROMPT)
        prompt = fill_template(
            user,
            STAGE=stage,
            CATEGORY=category or "insufficient evidence",
            ISSUES=_json(issue_payload),
            DRAFT=_json(draft),
            FINDINGS=_findings_json(brief),
            QUALITY=_quality_json(brief),
        )
        raw = _structured(prompt, system, strict, obj.__class__, TEMP_ANALYSIS)
        fixed = to_public(raw)
        if fixed is None:
            raise ValueError("repair answer was not usable")
        return fixed

    result = _live_or_cached(key, obj.__class__, live)
    if result is None:
        return None
    return clean(result)


def finish_stage(
    obj: T,
    brief: Brief,
    *,
    stage: str,
    strict: type[BaseModel] | None,
    to_public: Callable[[BaseModel], T | None],
    clean: Callable[[T], T | None],
    drop: Callable[[T, set[str]], T],
    diff: DiffResult | None = None,
    category: str | None = None,
    text_of: Callable[[T], str] | None = None,
) -> T:
    """Check -> one repair -> check -> drop/fall back, and record what happened.

    Works for any stage: `to_public` converts a schema answer into the public model,
    `clean` post-validates it, `drop` removes the sentences that are still wrong.
    """
    issues = verification_issues(obj, brief, diff)
    found = len(issues)
    repaired = 0
    remaining = issues

    if issues:
        fixed = _repair(
            obj,
            brief,
            issues,
            stage=stage,
            strict=strict,
            to_public=to_public,
            clean=clean,
            category=category,
        )
        if fixed is not None:
            left = verification_issues(fixed, brief, diff)
            repaired = max(0, found - len(left))
            if len(left) <= len(remaining):
                obj, remaining = fixed, left
    if remaining:
        offending = _offenders(remaining)
        candidate = clean(drop(obj, offending)) or obj
        left = verification_issues(candidate, brief, diff)
        if len(left) < len(remaining):
            repaired += len(remaining) - len(left)
            obj, remaining = candidate, left

    p6_used = False
    p6_issues: list[VerifyIssue] = []
    if not remaining and text_of is not None:
        text = text_of(obj)
        if len(text) >= P6_MIN_CHARS:
            p6_used = True
            p6_issues = verify_draft(text, brief)
            if p6_issues:
                as_issues = [
                    Issue(sentence=issue.sentence, kind="uncited_claim", detail=issue.problem)
                    for issue in p6_issues
                ]
                fixed = _repair(
                    obj,
                    brief,
                    as_issues,
                    stage=stage,
                    strict=strict,
                    to_public=to_public,
                    clean=clean,
                    category=category,
                )
                if fixed is not None:
                    obj = fixed
                remaining = verification_issues(obj, brief, diff)
                repaired = max(repaired, found + len(p6_issues) - len(remaining))

    details = [f"{issue.kind}: {issue.detail}" for issue in remaining][:5]
    verification = {
        "issues_found": found + len(p6_issues),
        "repaired": repaired,
        "remaining": len(remaining),
        "p6_used": p6_used,
        "details": details,
    }
    return obj.model_copy(update={"verification": verification})


def record_verification(
    obj: T,
    brief: Brief,
    *,
    diff: DiffResult | None = None,
    category: str | None = None,
) -> T:
    """Record the cheap-check result on a deterministic fallback (never repaired offline)."""
    issues = verification_issues(obj, brief, diff)
    contradictions = category_contradictions(" ".join(_strings(obj)), category)
    return obj.model_copy(
        update={
            "verification": {
                "issues_found": len(issues),
                "repaired": 0,
                "remaining": len(issues),
                "p6_used": False,
                "fallback": True,
                "contradictions": contradictions[:5],
                "details": [f"{issue.kind}: {issue.detail}" for issue in issues][:5],
            }
        }
    )


# ------------------------------------------------------------------ P1 mapper


def map_columns(raw: RawTable) -> ColumnMapping | None:
    """Map messy columns to the fixed schema. None on any failure."""
    try:
        columns = [str(column) for column in raw.df.columns]
        sample = [
            {str(key): _cell(value) for key, value in row.items()}
            for row in raw.df.head(30).to_dict(orient="records")
        ]
        cache_key = _cache_key("map", "p1_mapper", "ColumnMapping", {"columns": columns, "sample": sample})

        def live() -> ColumnMapping:
            system, user = load_prompt("p1_mapper")
            prompt = fill_template(
                user,
                COLUMNS=_json(columns),
                POLLUTANT_LIST=_json(list(POLLUTANTS)),
                SAMPLE_ROWS=_json(sample),
            )
            return client.generate_json(prompt, system, ColumnMapping, temperature=0.0)

        result = _live_or_cached(cache_key, ColumnMapping, live)
        return _clean_mapping(result, columns) if result is not None else None
    except Exception:  # noqa: BLE001 - caller falls back to the heuristic mapper
        return None


def _cell(value: Any) -> str:
    """One sample cell as short text."""
    if value is None:
        return ""
    text = str(value)
    return "" if text.lower() in ("nan", "nat", "none") else text[:60]


def _canonical_pollutant(name: str | None) -> str | None:
    """Canonical pollutant name, or None when it is not one of POLLUTANTS."""
    if not name:
        return None
    key = re.sub(r"[^A-Z0-9.]", "", str(name).upper())
    key = key.replace(".", "")
    return _ALIASES.get(key)


def _clean_mapping(mapping: ColumnMapping, columns: list[str]) -> ColumnMapping | None:
    """Drop unknown columns and non-canonical pollutants; None when unusable."""
    available = set(columns)
    kept: list[ColumnRole] = []
    seen: set[str] = set()
    for role in mapping.columns:
        if role.column not in available or role.column in seen:
            continue
        seen.add(role.column)
        if role.role in ("pollutant_value", "pollutant_name"):
            canonical = _canonical_pollutant(role.pollutant)
            if role.role == "pollutant_value" and canonical is None:
                continue
            if canonical and canonical != role.pollutant:
                role = role.model_copy(update={"pollutant": canonical})
        kept.append(role)
    if not any(role.role in ("pollutant_value", "value") for role in kept):
        return None
    if not any(role.role in ("station", "timestamp", "date", "time", "pollutant_name") for role in kept):
        return None
    return mapping.model_copy(update={"columns": kept, "source": "llm"})


# ------------------------------------------------------------------ P2 interpreter


def interpret(brief: Brief, diff: DiffResult) -> Interpretation:
    """Plain-language interpretation of the findings."""
    schema_name = "Interpretation"
    try:
        key = _cache_key(
            "interpret", "p2_interpreter", schema_name, _brief_payload(brief), _diff_payload(diff)
        )

        def live() -> Interpretation:
            system, user = load_prompt("p2_interpreter")
            prompt = fill_template(
                user,
                AREA=brief.area or "this area",
                PERIOD=_period(brief),
                FINDINGS=_findings_json(brief),
                QUALITY=_quality_json(brief),
                DIFF=_diff_json(diff),
            )
            raw = _structured(
                prompt, system, make_interpretation_schema(_finding_ids(brief)), Interpretation, TEMP_ANALYSIS
            )
            return Interpretation.model_validate(raw.model_dump())

        result = _live_or_cached(key, Interpretation, live)
        cleaned = _clean_interpretation(result, brief) if result is not None else None
        if cleaned is not None:
            return finish_stage(
                cleaned,
                brief,
                stage="interpretation",
                strict=make_interpretation_schema(_finding_ids(brief)),
                to_public=lambda raw: Interpretation.model_validate(raw.model_dump()),
                clean=lambda obj: _clean_interpretation(obj, brief),
                drop=_drop_interpretation,
                diff=diff,
            )
    except Exception:  # noqa: BLE001 - fall through to the template
        pass
    try:
        fallback_result = fallback.interpretation_fallback(brief, diff)
    except Exception:  # noqa: BLE001 - last resort, still valid
        fallback_result = Interpretation(
            headline="insufficient evidence to summarise this brief.",
            trend_points=[],
            hotspot_points=[],
            cannot_tell=["No findings were available."],
            used_fallback=True,
        )
    return record_verification(fallback_result, brief, diff=diff)


def _clean_interpretation(result: Interpretation, brief: Brief) -> Interpretation | None:
    """Keep only known citations; None when the answer is unusable."""
    known = {finding.id for finding in brief.findings}

    def clean_point(point: Point) -> Point:
        return point.model_copy(
            update={
                "text": _strip_unknown_cites(point.text, known),
                "cites": [c for c in _cite_ids(point.cites) if c in known],
            }
        )

    headline = _strip_unknown_cites(result.headline.strip(), known)
    if not headline:
        return None
    trend_points = [clean_point(p) for p in result.trend_points if p.text.strip()][:5]
    hotspot_points = [clean_point(p) for p in result.hotspot_points if p.text.strip()][:5]
    if not trend_points and not hotspot_points and brief.findings:
        return None
    cannot_tell = [
        _strip_unknown_cites(item, known) for item in result.cannot_tell if item.strip()
    ][:6]
    return result.model_copy(
        update={
            "headline": headline,
            "trend_points": trend_points,
            "hotspot_points": hotspot_points,
            "cannot_tell": cannot_tell,
            "used_fallback": False,
        }
    )


# ------------------------------------------------------------------ P3 actioner


def plan_actions(brief: Brief, diff: DiffResult, persona: str) -> ActionPlan:
    """Ranked, cited actions for one persona, from the curated library."""
    persona_key = (persona or "ward_officer").strip().lower()
    schema_name = "ActionPlan"
    try:
        spec = fallback.PERSONA_BRIEFS.get(
            persona_key,
            {"authority": persona_key.replace("_", " "), "limits": "No powers beyond this role."},
        )
        items = fallback.library_for_persona(persona_key)
        library_ids = [item["id"] for item in items]
        snapshot = fallback.brief_snapshot(brief)
        key = _cache_key(
            "actions",
            "p3_actioner",
            schema_name,
            _brief_payload(brief),
            _diff_payload(diff),
            {"persona": persona_key},
        )

        def live() -> ActionPlan:
            system, user = load_prompt("p3_actioner")
            prompt = fill_template(
                user,
                PERSONA=persona_key,
                AUTHORITY=spec["authority"],
                LIMITS=spec["limits"],
                CATEGORY=snapshot["category"] or "insufficient evidence",
                PERIOD=_period(brief),
                FINDINGS=_findings_json(brief),
                QUALITY=_quality_json(brief),
                DIFF=_diff_json(diff),
                ACTION_LIBRARY=_json(fallback.library_payload(items)),
            )
            raw = _structured(
                prompt,
                system,
                make_action_plan_schema(_finding_ids(brief), library_ids),
                ActionPlan,
                TEMP_ANALYSIS,
            )
            return ActionPlan.model_validate({**raw.model_dump(), "persona": persona_key})

        result = _live_or_cached(key, ActionPlan, live)
        cleaned = (
            _clean_plan(result, brief, persona_key, set(library_ids)) if result is not None else None
        )
        if cleaned is not None:
            return finish_stage(
                cleaned,
                brief,
                stage="action_plan",
                strict=make_action_plan_schema(_finding_ids(brief), library_ids),
                to_public=lambda raw: ActionPlan.model_validate({**raw.model_dump(), "persona": persona_key}),
                clean=lambda obj: _clean_plan(obj, brief, persona_key, set(library_ids)),
                drop=_drop_plan,
                diff=diff,
            )
    except Exception:  # noqa: BLE001 - fall through to the template
        pass
    try:
        plan = fallback.plan_fallback(brief, diff, persona_key)
    except Exception:  # noqa: BLE001 - last resort, still valid
        plan = ActionPlan(persona=persona_key, actions=[], used_fallback=True)
    return record_verification(plan, brief, diff=diff)


def _clean_plan(
    plan: ActionPlan,
    brief: Brief,
    persona: str,
    library_ids: set[str],
) -> ActionPlan | None:
    """Drop unknown citations and library IDs, renumber ranks; None when empty."""
    known = {finding.id for finding in brief.findings}
    actions = []
    for raw in plan.actions:
        cites = [c for c in _cite_ids(raw.cites) if c in known]
        if raw.cites and not cites:
            continue  # every citation was invented
        if not raw.action.strip():
            continue
        library_id = raw.library_id if raw.library_id in library_ids else "suggested_new"
        actions.append(
            raw.model_copy(
                update={
                    "action": _strip_unknown_cites(raw.action, known),
                    "why_now": _strip_unknown_cites(raw.why_now, known),
                    "data_gap_action": _strip_unknown_cites(raw.data_gap_action, known),
                    "cites": cites,
                    "library_id": library_id,
                    "rank": len(actions) + 1,
                }
            )
        )
        if len(actions) >= MAX_ACTIONS:
            break
    if not actions:
        return None
    return plan.model_copy(update={"persona": persona, "actions": actions, "used_fallback": False})


# ------------------------------------------------------------------ P4 internal note


def write_internal_note(
    brief: Brief,
    diff: DiffResult,
    interp: Interpretation,
    plan: ActionPlan,
) -> InternalNote:
    """Internal briefing note in Markdown with fixed sections."""
    schema_name = "InternalNote"
    try:
        key = _cache_key(
            "note",
            "p4_internal_note",
            schema_name,
            _brief_payload(brief),
            _diff_payload(diff),
            interp.model_dump(mode="json", exclude={"used_fallback", "verification"}),
            plan.model_dump(mode="json", exclude={"used_fallback", "verification"}),
        )

        def live() -> InternalNote:
            system, user = load_prompt("p4_internal_note")
            prompt = fill_template(
                user,
                AREA=brief.area or "this area",
                PERIOD=_period(brief),
                FINDINGS=_findings_json(brief),
                QUALITY=_quality_json(brief),
                DIFF=_diff_json(diff),
                INTERPRETATION=_json(interp.model_dump(mode="json")),
                ACTION_PLAN=_json(plan.model_dump(mode="json")),
            )
            raw = _structured(prompt, system, None, InternalNote, TEMP_ANALYSIS)
            return InternalNote.model_validate(raw.model_dump())

        result = _live_or_cached(key, InternalNote, live)
        if result is not None and result.markdown.strip():
            return finish_stage(
                result.model_copy(update={"used_fallback": False}),
                brief,
                stage="internal_note",
                strict=None,
                to_public=lambda raw: _note_from_raw(raw),
                clean=lambda obj: _clean_note(obj, {finding.id for finding in brief.findings}),
                drop=_drop_note,
                diff=diff,
                text_of=lambda obj: obj.markdown,
            )
    except Exception:  # noqa: BLE001 - fall through to the template
        pass
    try:
        note = fallback.internal_note_fallback(brief, diff, interp, plan)
    except Exception:  # noqa: BLE001 - last resort, still valid
        note = InternalNote(
            markdown="## Situation\ninsufficient evidence to build the note.",
            used_fallback=True,
        )
    return record_verification(note, brief, diff=diff)


def _note_from_raw(raw: BaseModel) -> InternalNote | None:
    data = raw.model_dump()
    markdown = str(data.get("markdown") or "").strip()
    if not markdown:
        return None
    return InternalNote(markdown=markdown, used_fallback=False)


def _clean_note(note: InternalNote, known: set[str] | None = None) -> InternalNote | None:
    markdown = note.markdown.strip()
    if not markdown:
        return None
    if known:
        markdown = _strip_unknown_cites(markdown, known)
    return note.model_copy(update={"markdown": markdown, "used_fallback": False})


# ------------------------------------------------------------------ P5 advisory


def write_advisory(
    brief: Brief,
    language: str,
    reading_level: str,
    area_label: str,
    station: str | None = None,
) -> Advisory:
    """Citizen advisory for one language and reading level.

    City scope (station None) speaks about the worst station and states the category range
    across the city. With `station`, it speaks about that one station/ward only.
    """
    schema_name = "Advisory"
    snapshot = fallback.brief_snapshot(brief, station)
    category = snapshot["category"] if snapshot["category"] in CATEGORIES else None
    try:
        key = _cache_key(
            "advisory",
            "p5_advisory",
            schema_name,
            _brief_payload(brief),
            {
                "language": language,
                "reading_level": reading_level,
                "area": area_label,
                "station": station,
            },
        )

        def live() -> Advisory:
            system, user = load_prompt("p5_advisory")
            prompt = fill_template(
                user,
                LANGUAGE=language,
                READING_LEVEL=reading_level,
                SCOPE=snapshot["scope"],
                STATION=snapshot["station"] or "not recorded",
                AREA=area_label or brief.area or "this area",
                DATE=snapshot["date"],
                CATEGORY=snapshot["category"] or "insufficient evidence",
                AQI=_num(snapshot["aqi"]),
                DOMINANT=snapshot["dominant"] or "not identified",
                STATIONS=", ".join(snapshot["stations"]) or "not recorded",
                RANGE=snapshot["range_text"] or "not recorded",
                PEAK_WINDOW=snapshot["peak_window"] or "not identified",
                GAP_NOTE=snapshot["gaps"][0] if snapshot["gaps"] else "no major data gaps reported",
                FINDINGS=_findings_json(brief),
                QUALITY=_quality_json(brief),
                GROUPS="children, older adults, people sensitive to poor air",
            )
            raw = _structured(
                prompt, system, make_advisory_schema(_finding_ids(brief)), Advisory, TEMP_ADVISORY
            )
            return _advisory_from_raw(raw, brief, language, reading_level, area_label, station)

        result = _live_or_cached(key, Advisory, live)
        cleaned = (
            _stabilise_advisory(result, brief, language, reading_level, area_label, station)
            if result is not None
            else None
        )
        if cleaned is not None:
            return finish_stage(
                cleaned,
                brief,
                stage="advisory",
                strict=make_advisory_schema(_finding_ids(brief)),
                to_public=lambda raw: _advisory_from_raw(raw, brief, language, reading_level, area_label, station),
                clean=lambda obj: _stabilise_advisory(obj, brief, language, reading_level, area_label, station),
                drop=_drop_advisory,
                diff=None,
                category=category,
                text_of=lambda obj: " ".join(_strings(obj)),
            )
    except Exception:  # noqa: BLE001 - fall through to the template
        pass
    try:
        advisory = fallback.advisory_fallback(brief, language, reading_level, area_label, station)
    except Exception:  # noqa: BLE001 - last resort, still valid
        advisory = Advisory(
            language=language,
            reading_level=reading_level,
            headline="insufficient evidence for today's category.",
            category="insufficient evidence",
            today_message="The data was not sufficient for advice today.",
            what_to_do=["Limit outdoor activity until the category is known."],
            care=CareBlock(
                children="Keep children's outdoor play short.",
                older_adults="Keep outdoor trips short.",
                sensitive_groups="Keep prescribed medicine at hand; contact a doctor if breathing is difficult.",
            ),
            source_line="Source: data unavailable. General guidance, not medical advice.",
            cites=[],
            used_fallback=True,
        )
    return record_verification(advisory, brief, category=category)


def _advisory_from_raw(
    raw: BaseModel,
    brief: Brief,
    language: str,
    reading_level: str,
    area_label: str,
    station: str | None,
) -> Advisory:
    """Turn a strict or loose advisory answer into the public model with code-owned fields."""
    data = raw.model_dump()
    care = data.get("care") if isinstance(data.get("care"), dict) else {}
    return Advisory(
        language=data.get("language") or language,
        reading_level=data.get("reading_level") or reading_level,
        headline=str(data.get("headline") or ""),
        category=str(data.get("category") or ""),
        today_message=str(data.get("today_message") or ""),
        what_to_do=[str(item) for item in data.get("what_to_do") or [] if str(item).strip()],
        care=CareBlock(
            children=str(care.get("children") or ""),
            older_adults=str(care.get("older_adults") or ""),
            sensitive_groups=str(care.get("sensitive_groups") or ""),
        ),
        source_line=str(data.get("source_line") or ""),
        cites=[str(cite) for cite in data.get("cites") or []],
        used_fallback=False,
    )


def _stabilise_advisory(
    result: Advisory,
    brief: Brief,
    language: str,
    reading_level: str,
    area_label: str,
    station: str | None,
) -> Advisory | None:
    """Force category, AQI number and scope from the Brief; repair contradictory advice.

    The model writes prose only: `category`, the headline category word and the AQI number are
    code values, and a sentence that contradicts the category is dropped or replaced.
    """
    code = fallback.advisory_fallback(brief, language, reading_level, area_label, station)
    category = code.category
    snapshot = fallback.brief_snapshot(brief, station)
    known = {finding.id for finding in brief.findings}

    headline = _force_category_word(_strip_unknown_cites(result.headline.strip(), known), category)
    if headline and category.lower() not in headline.lower():
        headline = f"{headline.rstrip('.')} ({category})."
    if not headline:
        headline = code.headline

    message = _force_category_word(_strip_unknown_cites(result.today_message.strip(), known), category)
    message = _force_aqi(message, snapshot["aqi"])
    if not message or category_contradictions(message, category):
        message = code.today_message
    actions = [
        _strip_unknown_cites(item.strip(), known) for item in result.what_to_do if item.strip()
    ]
    actions = [item for item in actions if not category_contradictions(item, category)][:5]
    if not actions:
        actions = code.what_to_do[:5]
    care = result.care
    care_data = {}
    for field in ("children", "older_adults", "sensitive_groups"):
        text = _strip_unknown_cites(str(getattr(care, field) or "").strip(), known)
        if not text or category_contradictions(text, category):
            text = str(getattr(code.care, field))
        care_data[field] = text

    source_line = _strip_unknown_cites(result.source_line.strip(), known) or code.source_line
    cites = [c for c in _cite_ids(result.cites) if c in known] or list(code.cites)
    return result.model_copy(
        update={
            "language": language,
            "reading_level": reading_level,
            "headline": headline,
            "category": category,
            "today_message": message,
            "what_to_do": actions,
            "care": CareBlock(**care_data),
            "source_line": source_line,
            "cites": cites[:5],
            "used_fallback": False,
        }
    )


def _force_category_word(text: str, category: str) -> str:
    """Replace any other CPCB category word with the code category (single pass)."""
    if not text:
        return text

    def swap(match: re.Match[str]) -> str:
        return match.group(0) if match.group(0).lower() == category.lower() else category

    return _CATEGORY_PATTERN.sub(swap, text)


def _force_aqi(text: str, aqi: float | None) -> str:
    """Put the code AQI number in the text: replace a wrong one, else add it."""
    if not _finite(aqi):
        return text
    value = _num(round(float(aqi)))
    body = text or ""
    match = _AQI_LABELS.search(body)
    if match:
        found = match.group(2)
        if abs(float(found) - float(round(float(aqi)))) > 0.5:
            return body[: match.start(2)] + value + body[match.end(2) :]
        return body
    if re.search(rf"(?<!\d){re.escape(value)}(?!\d)", body):
        return body
    return f"AQI {value}. {body}".strip()




# ------------------------------------------------------------------ P6 verifier


def verify_draft(text: str, brief: Brief) -> list[VerifyIssue]:
    """Optional Gemini check of a draft; [] when unavailable or clean."""
    try:
        if not isinstance(text, str) or not text.strip():
            return []
        system, user = load_prompt("p6_verifier")
        snapshot = fallback.brief_snapshot(brief)
        prompt = fill_template(
            user,
            CATEGORY=snapshot["category"] or "insufficient evidence",
            ALLOWED_NUMBERS=_json(_allowed_numbers(brief)),
            FINDINGS=_findings_json(brief),
            DRAFT=text[:12000],
        )
        report = client.generate_json(prompt, system, VerifyReport, temperature=0.0)
        return [issue for issue in report.issues if issue.sentence.strip()][:MAX_VERIFY_ISSUES]
    except Exception:  # noqa: BLE001 - the code checks still run outside this stage
        return []


def _allowed_numbers(brief: Brief) -> list[float]:
    """Every number a draft may legitimately quote."""
    values: list[float] = []
    for finding in brief.findings:
        values += [
            float(value)
            for value in finding.facts.values()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
    values += [float(station.aqi) for station in brief.station_aqi if _finite(station.aqi)]
    return sorted({round(value, 2) for value in values})
