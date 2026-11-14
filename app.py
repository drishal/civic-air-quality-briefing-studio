"""Civic Air Quality Briefing Studio — single-page Streamlit UI (seven tabs).

Code computes every number (core.pipeline); Gemini only narrates (llm.stages). Each generated
object is cached in session_state under (brief_id, persona/language/level) so switching tabs,
personas, languages or reading levels never calls the API twice for the same thing. No stage may
break a tab: every LLM call sits behind llm_stage() and falls back to numbers-only rendering.

Presentation lives in ui/theme.py (tokens + CSS) and ui/components.py (pure HTML builders).
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Civic Air Quality Briefing Studio",
    page_icon="🌤️",
    layout="wide",
    initial_sidebar_state="auto",
)


# ---------------------------------------------------------------- environment bootstrap
def _bootstrap_api_key() -> None:
    """Copy a key from Streamlit secrets into the environment once. The key is never rendered."""
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return
    try:
        secrets = st.secrets
    except Exception:  # no secrets.toml / not inside a Streamlit runtime
        return
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        try:
            value = secrets[name]
        except Exception:
            continue
        if value:
            os.environ[name] = str(value)
            return


_bootstrap_api_key()

from core import aqi as aqi_mod  # noqa: E402
from core import diff as diff_mod  # noqa: E402
from core import pipeline as pipeline_mod  # noqa: E402
from core import store as store_mod  # noqa: E402
from core import verify as verify_mod  # noqa: E402
from core.models import CATEGORIES, FLAGS  # noqa: E402
from ui import components as C  # noqa: E402
from ui import theme  # noqa: E402

try:  # the numeric tabs must keep working even if the LLM layer is broken
    from llm import client as llm_client  # noqa: E402
    from llm import stages as llm_stages  # noqa: E402
except Exception:  # pragma: no cover - defensive
    llm_client = None
    llm_stages = None

try:
    import plotly.graph_objects as go  # noqa: E402

    HAS_PLOTLY = True
except Exception:  # pragma: no cover - plotly is a declared dependency
    go = None
    HAS_PLOTLY = False


# ---------------------------------------------------------------- constants
APP_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = APP_DIR / "data"
BRIEF_DIR = ".briefs"

AREA_DEFAULT = "Pune"
LANGUAGES = ["English", "Hindi", "Marathi", "Kannada", "Tamil", "Bengali", "Telugu", "Gujarati"]
READING_LEVELS = ["simple", "standard", "technical"]
PERSONAS = {"Ward officer": "ward_officer", "Housing society": "housing_society", "School": "school"}
PERSONA_LABEL = {v: k for k, v in PERSONAS.items()}

# Unit notes (UNIT_AMBIGUOUS / UNIT_CONVERTED) are informational: they never lower reliability,
# so they are listed apart from the flags that mark a reading as unusable or untrustworthy.
REAL_FLAGS = frozenset(FLAGS) - frozenset(C.INFORMATIONAL_FLAGS)
FLAG_MEANING = {
    "MISSING": "empty or NA cell",
    "SENTINEL": "placeholder like -999 / ND",
    "NEGATIVE": "negative concentration",
    "OUT_OF_RANGE": "physically implausible",
    "UNIT_MISMATCH": "wrong unit for the pollutant",
    "CENSORED": "“<5” / “>500” style value",
    "STUCK": "6+ identical readings in a row",
    "SPIKE": "jump far above the rolling median",
    "DUPLICATE": "repeated station+time+pollutant",
    "TIMESTAMP_REPAIRED": "timestamp inferred",
    "TIMESTAMP_UNPARSEABLE": "timestamp unreadable",
    "UNIT_AMBIGUOUS": "unit missing, standard assumed",
    "UNIT_CONVERTED": "converted to standard unit",
}
RIBBON_LABEL = {"valid": "valid", "missing": "no data", "flagged": "flagged", "stuck": "stuck"}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
DANGER_HEX = "#a8201a"  # plotly needs a literal colour, not a CSS variable

# Columns shown when a table of individual readings is rendered (Quality flags, evidence rows).
TIDY_COLUMNS = ("row_id", "station", "ts", "pollutant", "value_raw", "unit_raw", "value_std", "flags")


def _stretch() -> dict[str, Any]:
    """Width kwarg that works on old and new Streamlit (use_container_width -> width='stretch')."""
    try:
        parts = [int(p) for p in re.findall(r"\d+", st.__version__)[:2]]
        if len(parts) == 2 and tuple(parts) >= (1, 49):
            return {"width": "stretch"}
    except Exception:
        pass
    return {"use_container_width": True}


STRETCH = _stretch()


# ---------------------------------------------------------------- small state helpers
def _get(key: str, default: Any = None) -> Any:
    return st.session_state[key] if key in st.session_state else default


def _set(key: str, value: Any) -> None:
    st.session_state[key] = value


def _html(markup: str) -> None:
    """Render one component string."""
    if markup:
        st.markdown(markup, unsafe_allow_html=True)


def init_state() -> None:
    defaults = {
        "result": None,
        "history": [],
        "error": None,
        "llm_cache": {},
        "daily_cache": {},
        "ribbon_cache": {},
        "pollutant_cache": {},
        "loaded_label": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# ---------------------------------------------------------------- formatting helpers
def _show(v: Any, nd: int = 0, dash: str = "—") -> str:
    """Format a number defensively; None/NaN become a dash."""
    if v is None:
        return dash
    try:
        if pd.isna(v):
            return dash
    except Exception:
        pass
    try:
        return f"{float(v):,.{nd}f}"
    except Exception:
        return str(v)


def _safe_color(category: Optional[str]) -> str:
    if not category:
        return "#9aa0a6"
    try:
        return aqi_mod.category_color(category)
    except Exception:
        return "#9aa0a6"


def _safe_emoji(category: Optional[str]) -> str:
    if not category:
        return ""
    try:
        return aqi_mod.category_emoji(category)
    except Exception:
        return ""


def cited_ids(*texts: Any) -> list[str]:
    """Finding ids cited in a set of generated strings, in order of appearance."""
    found: list[str] = []
    for text in texts:
        if isinstance(text, (list, tuple)):
            found.extend(cited_ids(*text))
            continue
        found.extend(re.findall(r"\[(F\d{1,3})\]", str(text or "")))
    return found


def _plain(line: Any) -> str:
    """Strip a leading bullet the text already carries, so generated list lines render once."""
    return re.sub(r"^\s*[-*•]\s+", "", str(line or ""))


def _fail(message: str) -> bool:
    """Show a friendly error where the action happened, and remember it for the session."""
    _set("error", message)
    st.error(message)
    return False


# ---------------------------------------------------------------- LLM plumbing
def gemini_available() -> bool:
    if llm_client is None:
        return False
    try:
        return bool(llm_client.llm_available())
    except Exception:
        return False


def llm_stage(spinner: str, cache_key: tuple, fn: Callable[[], Any], lines: int = 3) -> Any:
    """Run one LLM stage, cached in session_state. Returns None if the stage is unavailable."""
    if llm_stages is None:
        return None
    cache: dict = _get("llm_cache", {})
    if cache_key in cache:
        return cache[cache_key]
    slot = st.empty()
    with slot.container():
        _html(C.skeleton(lines, spinner))
    try:
        obj = fn()
    except Exception:  # stages are supposed to swallow errors; guard anyway
        obj = None
    slot.empty()
    if obj is not None:
        cache[cache_key] = obj
        _set("llm_cache", cache)
    return obj


def fallback_badge(obj: Any) -> None:
    """Visible marker when a stage returned its deterministic template instead of Gemini text."""
    if getattr(obj, "used_fallback", False):
        _html(C.chip("offline fallback text", "warn"))


def stage_unavailable(what: str) -> None:
    st.warning(f"{what} is unavailable right now. The numbers on this page are unaffected.")


def get_interp(brief, diff_result):
    return llm_stage(
        "Interpreting the findings…",
        ("interpret", brief.brief_id, diff_result.previous_brief_id if diff_result else None),
        lambda: llm_stages.interpret(brief, diff_result),
    )


def get_plan(brief, diff_result, persona: str):
    return llm_stage(
        f"Drafting actions for {PERSONA_LABEL.get(persona, persona)}…",
        ("plan", brief.brief_id, persona, diff_result.previous_brief_id if diff_result else None),
        lambda: llm_stages.plan_actions(brief, diff_result, persona),
    )


def get_note(brief, diff_result, interp, plan):
    return llm_stage(
        "Writing the internal briefing note…",
        ("note", brief.brief_id, getattr(plan, "persona", ""), diff_result.previous_brief_id if diff_result else None),
        lambda: llm_stages.write_internal_note(brief, diff_result, interp, plan),
        lines=5,
    )


def get_advisory(brief, language: str, level: str, area: str):
    return llm_stage(
        f"Writing the {language} advisory ({level})…",
        ("advisory", brief.brief_id, language, level, area),
        lambda: llm_stages.write_advisory(brief, language, level, area),
    )


# ---------------------------------------------------------------- verification panel
def verification_expander(obj: Any, findings: list, extra_facts: dict | None = None, label: str | None = None) -> None:
    label = label or "🔎 Verification — citations and numbers checked by code"
    with st.expander(label):
        if obj is None:
            st.caption("Nothing generated yet, so nothing to verify.")
            return
        try:
            try:
                issues = verify_mod.check_object(obj, findings, extra_facts or {})
            except TypeError:
                issues = verify_mod.check_object(obj, findings)
        except Exception as exc:
            st.caption(f"Verification is unavailable ({type(exc).__name__}).")
            return
        issues = list(issues or [])
        if not issues:
            st.success("✅ No issues found. Every citation points to a real finding and every number is traceable.")
            return
        _html(C.chip(f"{len(issues)} issue(s)", "warn"))
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "sentence": getattr(i, "sentence", ""),
                        "problem": getattr(i, "kind", ""),
                        "detail": getattr(i, "detail", ""),
                    }
                    for i in issues
                ]
            ),
            hide_index=True,
            **STRETCH,
        )


# ---------------------------------------------------------------- data loading
def _source_hash(text: Optional[str], file_bytes: Optional[bytes]) -> str:
    payload = text.encode() if text is not None else (file_bytes or b"")
    return hashlib.sha256(payload).hexdigest()[:12]


def read_sample(week: int) -> Optional[tuple[bytes, str]]:
    path = SAMPLE_DIR / f"sample_week{week}.csv"
    if not path.exists():
        return None
    try:
        return path.read_bytes(), path.name
    except Exception:
        return None


def analyze(
    *,
    text: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
    area: str = AREA_DEFAULT,
    label: Optional[str] = None,
) -> bool:
    """Run the numeric pipeline on new input and chain the diff against this session's last brief."""
    source = _source_hash(text, file_bytes)
    previous = None
    for brief in reversed(list(_get("history", []))):
        if brief.source_hash != source:  # never diff a dataset against itself
            previous = brief
            break
    try:
        with st.spinner("Reading, cleaning and scoring the data…"):
            result = pipeline_mod.run_pipeline(
                text=text,
                file_bytes=file_bytes,
                filename=filename,
                area=(area or "").strip() or AREA_DEFAULT,
                use_llm_mapper=True,
                store_dir=BRIEF_DIR,
                save=True,
                previous=previous,
            )
    except ValueError as exc:
        return _fail(f"Could not read that table: {exc}")
    except Exception as exc:  # wrong sheet, unexpected parser error... keep the UI alive
        return _fail(
            f"Could not process that table ({type(exc).__name__}). Try a CSV with one header row, "
            "or use the sample data."
        )

    if not isinstance(result.tidy, pd.DataFrame) or result.tidy.empty:
        return _fail("That table had no readable data rows. Check the header row and try again.")
    values = result.tidy["value_std"] if "value_std" in result.tidy.columns else None
    if values is not None and values.notna().sum() == 0:
        return _fail("No numeric readings survived cleaning. Check that the pollutant columns hold numbers.")

    # Own deterministic diff (pipeline may have loaded a stale brief from disk).
    try:
        result.previous = previous
        result.diff = diff_mod.diff_briefs(result.brief, previous)
    except Exception:
        pass

    _set("result", result)
    _set("error", None)
    history = list(_get("history", [])) + [result.brief]
    _set("history", history[-6:])
    _set("llm_cache", {})
    _set("daily_cache", {})
    _set("ribbon_cache", {})
    _set("pollutant_cache", {})
    _set("loaded_label", label or filename or "pasted table")
    return True


def load_sample(week: int, area: str) -> bool:
    payload = read_sample(week)
    if payload is None:
        return _fail(
            f"The sample file data/sample_week{week}.csv is missing from this build. Paste or upload data instead."
        )
    data, name = payload
    return analyze(file_bytes=data, filename=name, area=area, label=name)


def clear_history() -> None:
    """Wipe the saved briefs on disk and the session analysis (demo reset)."""
    try:
        for path in Path(BRIEF_DIR).glob("*.json"):
            try:
                path.unlink()
            except Exception:
                pass
    except Exception:
        pass
    for key in ("result", "history", "error", "llm_cache", "daily_cache", "ribbon_cache", "pollutant_cache", "loaded_label"):
        _set(key, [] if key == "history" else {} if isinstance(_get(key), dict) else None)


def daily_aqi_cached(brief, tidy: pd.DataFrame) -> pd.DataFrame:
    """Daily AQI per station, cached per brief; returns an empty frame when unavailable."""
    cache: dict = _get("daily_cache", {})
    if brief.brief_id in cache:
        return cache[brief.brief_id]
    try:
        frame = aqi_mod.daily_aqi(tidy)
        if not isinstance(frame, pd.DataFrame):
            frame = pd.DataFrame()
    except Exception:
        frame = pd.DataFrame()
    cache[brief.brief_id] = frame
    _set("daily_cache", cache)
    return frame


def pollutant_means(result) -> dict[str, float]:
    """Period-average concentration per pollutant, cached per brief (24 h / 8 h rules applied)."""
    cache: dict = _get("pollutant_cache", {})
    key = result.brief.brief_id
    if key in cache:
        return cache[key]
    means: dict[str, float] = {}
    try:
        averages = aqi_mod.pollutant_averages(result.tidy)
        if isinstance(averages, pd.DataFrame) and not averages.empty and {"pollutant", "avg"} <= set(averages.columns):
            grouped = averages.dropna(subset=["avg"]).groupby("pollutant")["avg"].mean()
            means = {str(name): float(value) for name, value in grouped.items()}
    except Exception:
        means = {}
    cache[key] = means
    _set("pollutant_cache", cache)
    return means


def area_headline(result) -> Optional[dict[str, Any]]:
    """Headline tile for the selected area: the worst station, with the area mean beside it.

    The worst station is what the briefing note and the advisory speak about ("Pune is Very Poor
    today"), so the hero shows the same number instead of a mean that reads as a contradiction.
    """
    brief = result.brief
    scored = [s for s in brief.station_aqi if s.aqi is not None]
    if not scored:
        return None
    worst = max(scored, key=lambda s: float(s.aqi))
    average = sum(float(s.aqi) for s in scored) / len(scored)
    try:
        mean_category = aqi_mod.category(average)
    except Exception:
        mean_category = None
    dominant = worst.dominant or next((s.dominant for s in scored if s.dominant), None)
    concentration = pollutant_means(result).get(dominant) if dominant else None
    multiple = None
    if dominant and concentration is not None:
        try:
            multiple = aqi_mod.who_multiple(dominant, concentration)
        except Exception:
            multiple = None
    ranked = [s.confidence for s in scored if s.confidence]
    confidence = max(ranked, key=lambda c: CONFIDENCE_RANK.get(c, 3)) if ranked else None
    period = ""
    if brief.period_start or brief.period_end:
        period = f"{brief.period_start or '?'} → {brief.period_end or '?'}"
    return {
        "aqi": worst.aqi,
        "category": worst.category,
        "color": _safe_color(worst.category),
        "dominant": dominant,
        "value": concentration,
        "unit": _unit_for(dominant),
        "multiple": multiple,
        "confidence": confidence,
        "n_stations": len(scored),
        "period": period,
        "station": worst.station,
        "mean": average,
        "mean_label": f"area mean {average:.0f}"
        + (f" · {mean_category}" if mean_category else ""),
    }


def _unit_for(pollutant: Optional[str]) -> str:
    units = {"CO": "mg/m³"}
    return units.get(pollutant or "", "µg/m³")


# ---------------------------------------------------------------- data-quality ribbons
def ribbon_segments(result) -> dict[str, list[dict]]:
    """Per-station segment runs of valid / missing / flagged / stuck hours, cached per brief."""
    cache: dict = _get("ribbon_cache", {})
    key = result.brief.brief_id
    if key in cache:
        return cache[key]
    try:
        segments = _build_ribbons(result.tidy)
    except Exception:
        segments = {}
    cache[key] = segments
    _set("ribbon_cache", cache)
    return segments


def _build_ribbons(tidy: pd.DataFrame) -> dict[str, list[dict]]:
    if not isinstance(tidy, pd.DataFrame) or "ts" not in tidy.columns:
        return {}
    work = tidy.dropna(subset=["ts"]).copy()
    if work.empty:
        return {}
    work["_hour"] = work["ts"].dt.floor("h")
    work["_valid"] = work["value_std"].notna() if "value_std" in work.columns else False

    def _flags(value: Any) -> set:
        return set(value) if isinstance(value, (list, tuple)) else set()

    work["_flags"] = work["flags"].apply(_flags) if "flags" in work.columns else [set() for _ in range(len(work))]
    work["_stuck"] = work["_flags"].apply(lambda s: "STUCK" in s)
    work["_bad"] = work["_flags"].apply(lambda s: bool(s & REAL_FLAGS))

    segments: dict[str, list[dict]] = {}
    for station, group in work.groupby("station", sort=True):
        hours = (
            group.groupby("_hour")
            .agg(valid=("_valid", "any"), stuck=("_stuck", "any"), bad=("_bad", "any"))
            .sort_index()
        )
        runs: list[dict] = []
        previous_hour = None
        for hour, row in hours.iterrows():
            if row["stuck"]:
                kind = "stuck"
            elif not row["valid"]:
                kind = "missing"
            elif row["bad"]:
                kind = "flagged"
            else:
                kind = "valid"
            if runs and runs[-1]["kind"] == kind and previous_hour is not None and hour - previous_hour <= pd.Timedelta(hours=1):
                runs[-1]["weight"] += 1
                runs[-1]["end"] = hour
            else:
                runs.append({"kind": kind, "weight": 1, "start": hour, "end": hour})
            previous_hour = hour
        for run in runs:
            span = f"{run['start']:%d %b %H:%M}"
            if run["weight"] > 1:
                span += f" → {run['end']:%d %b %H:%M}"
            run["label"] = f"{RIBBON_LABEL.get(run['kind'], run['kind'])} · {run['weight']} h · {span}"
        segments[str(station)] = runs
    return segments


def station_spark(daily: pd.DataFrame, station: str) -> str:
    """Inline SVG sparkline of a station's daily AQI, coloured by its latest category."""
    if daily is None or daily.empty or not {"station", "aqi"} <= set(daily.columns):
        return ""
    frame = daily[daily["station"].astype(str) == str(station)].copy()
    if "valid" in frame.columns:
        frame = frame[frame["valid"].astype(bool)]
    if "date" in frame.columns:
        frame = frame.sort_values("date")
    values = [float(v) if pd.notna(v) else None for v in frame["aqi"].tolist()]
    colour = _safe_color(str(frame["category"].iloc[-1])) if not frame.empty and "category" in frame else "var(--accent)"
    return C.sparkline(values, colour, width=240, height=30, label=f"daily AQI at {station}")


# ---------------------------------------------------------------- sidebar
def sidebar() -> tuple[str, str, list[str], list[str]]:
    with st.sidebar:
        _html(C.wordmark("Air Briefing Studio", "Messy sensor tables → traceable, cited action briefs."))
        if gemini_available():
            _html(C.dot_chip("Gemini connected", "ok"))
            st.caption("Narrative, actions and translations come from Gemini.")
        else:
            _html(C.dot_chip("offline fallback", "warn"))
            st.caption("No Gemini key on the server — every tab still works with template text.")
        st.divider()

        audience = st.selectbox("Audience", list(PERSONAS), key="audience_select")
        area = st.text_input("City / area", value=AREA_DEFAULT, key="area_input")
        languages = st.multiselect(
            "Languages", LANGUAGES, default=["English", "Hindi"], key="language_select",
            help="Citizen advisories are written per language × reading level.",
        )
        levels = st.multiselect(
            "Reading level", READING_LEVELS, default=["simple", "standard"], key="level_select",
        )
        if not languages:
            st.caption("Pick at least one language for the advisory tab.")
        if not levels:
            st.caption("Pick at least one reading level for the advisory tab.")

        st.divider()
        st.markdown("**Sample data**")
        col1, col2 = st.columns(2)
        if col1.button("Load sample week 1", key="sb_load_week1", **STRETCH):
            if load_sample(1, area):
                st.rerun()
        if col2.button("Load sample week 2", key="sb_load_week2", **STRETCH):
            if load_sample(2, area):
                st.rerun()
        st.caption("Load week 1, then week 2, to unlock the “What changed” diff.")

        st.divider()
        try:
            saved = len(store_mod.list_briefs(BRIEF_DIR))
        except Exception:
            saved = 0
        st.caption(f"Saved briefs in `{BRIEF_DIR}`: {saved}")
        if st.button("Clear history", key="clear_history", **STRETCH):
            clear_history()
        st.caption("Clears the loaded analysis and the saved brief files.")

    return PERSONAS[audience], area, list(languages), list(levels)


# ---------------------------------------------------------------- page-level blocks
def vitals_row(result, persona: str) -> str:
    """Chip row: readings, usable share, real flags, unreliable stations, AI source."""
    brief = result.brief
    quality = result.quality
    usable = 0.0
    try:
        if "value_std" in result.tidy.columns and len(result.tidy):
            usable = float(result.tidy["value_std"].notna().mean())
    except Exception:
        usable = 0.0
    real_flags = sum(count for flag, count in (quality.flag_counts or {}).items() if flag in REAL_FLAGS)
    unit_notes = sum(count for flag, count in (quality.flag_counts or {}).items() if flag in C.INFORMATIONAL_FLAGS)
    shaky = sum(1 for s in quality.stations if s.label != "reliable")
    items: list[tuple[str, str]] = [
        (f"{_show(quality.n_readings)} readings", ""),
        (f"{usable:.0%} usable", "ok" if usable >= 0.9 else "warn"),
        (f"{_show(real_flags)} quality flags", "warn" if real_flags else "ok"),
        (f"{unit_notes} unit notes", ""),
        (f"{shaky} station{'s' if shaky != 1 else ''} not fully reliable", "warn" if shaky else "ok"),
        (f"AI: Gemini" if gemini_available() else "AI: deterministic fallback", "info" if gemini_available() else ""),
        (f"audience: {PERSONA_LABEL.get(persona, persona)}", ""),
    ]
    return C.vitals(items)


def start_screen(area: str) -> None:
    """First screen: wordmark, slow accent wash, the three-step path and the two sample cards."""
    _html(C.wash())
    _html(C.wordmark("Air Briefing Studio", "Civic air-quality briefings from messy sensor tables"))
    st.markdown("#### One connected path: data → quality → insights → actions → what changed → note → advisory")
    st.caption(
        "Every sentence carries a finding ID (`[F1]`) and every finding links back to the exact rows it came from."
    )
    _html(
        C.start_grid(
            [
                C.start_card("Step 1", "Load data", "Click a sample week below, or paste/upload your own table."),
                C.start_card("Step 2", "Read the brief", "Check the quality flags, then the hotspots with their evidence trail. Switch audience to change the actions."),
                C.start_card("Step 3", "Ship it", "Download the briefing note as Markdown, the citizen advisory as WhatsApp text, and diff against the next week."),
            ]
        )
    )
    _html(C.section("Two clicks to a full demo", "sample data is messy on purpose"))
    col1, col2 = st.columns(2)
    if col1.button("Load sample week 1", key="main_load_week1", type="primary", **STRETCH):
        if load_sample(1, area):
            st.rerun()
    if col2.button("Load sample week 2", key="main_load_week2", **STRETCH):
        if load_sample(2, area):
            st.rerun()
    st.caption("No login and no key required: the Gemini key is a server-side environment variable.")
    with st.expander("…or paste / upload your own table", expanded=False):
        data_inputs(area)


def station_cards(result, daily: pd.DataFrame) -> str:
    """Station tile grid: category edge, AQI, sparkline, ribbon, reliability pill, trend words."""
    brief = result.brief
    reliability = {s.station: s for s in result.quality.stations}
    ribbons = ribbon_segments(result)
    cards: list[str] = []
    for station in sorted(brief.station_aqi, key=lambda s: (s.aqi is None, -(s.aqi or 0))):
        rel = reliability.get(station.station)
        cards.append(
            C.station_card(
                station=station.station,
                ward=station.ward,
                aqi=station.aqi,
                category=station.category,
                color=_safe_color(station.category),
                reliability_label=rel.label if rel else None,
                trend_per_day=station.trend_per_day,
                spark=station_spark(daily, station.station),
                dominant=station.dominant,
                confidence=station.confidence,
                ribbon=C.quality_ribbon(ribbons.get(station.station) or [], compact=True),
                emoji=_safe_emoji(station.category),
            )
        )
    return C.card_grid(cards)




# ---------------------------------------------------------------- tab 1: data
def data_inputs(area: str) -> None:
    """The paste/upload widgets, shared by the start screen and the Data tab."""
    text = st.text_area(
        "Paste rows (CSV, TSV or whitespace-aligned)",
        height=150,
        key="paste_text",
        placeholder="station,ward,timestamp,PM2.5,PM10,NO2,SO2\nShivajinagar,Ward 8,01/11/2026 07:00,NA,142,38,12",
    )
    upload = st.file_uploader(
        "…or upload a file (CSV, TSV, TXT, XLSX)",
        type=["csv", "tsv", "txt", "xlsx"],
        key="upload_file",
        help="First sheet with data is used; title rows above the true header are skipped.",
    )
    if st.button("Analyze", type="primary", key="analyze_btn"):
        if upload is not None:
            if analyze(file_bytes=upload.getvalue(), filename=upload.name, area=area, label=upload.name):
                st.rerun()
        elif text and text.strip():
            if analyze(text=text, area=area, label="pasted table"):
                st.rerun()
        else:
            st.error("Nothing to analyze yet — paste some rows or upload a file first.")


def data_tab(area: str) -> None:
    _html(C.section("Input and mapping", "paste a table, or upload a file"))
    st.caption("Messy headers, mixed units, mixed dates, sentinel values and subtitle rows are handled.")

    data_inputs(area)

    result = _get("result")
    if result is None:
        _html(
            C.empty_state(
                "No data loaded yet",
                "Use the sample buttons on the left, paste rows above, or upload a CSV/TSV/XLSX file.",
                "Press Analyze, or click Load sample week 1",
            )
        )
        return
    brief = result.brief
    st.success(
        f"Analyzed **{result.raw.source_name}** for **{brief.area}** · "
        f"{brief.period_start or '?'} → {brief.period_end or '?'} · brief `{brief.brief_id}`"
    )

    with st.expander("Raw preview (as read from the file)", expanded=False):
        st.caption(f"{len(result.raw.df)} row(s), {len(result.raw.df.columns)} column(s) · source: {result.raw.source_name}")
        st.dataframe(result.raw.df.head(100), hide_index=True, **STRETCH)

    _html(C.section("Detected column mapping", "Gemini or rules, always validated by code"))
    st.caption(
        f"Mapped by **{'Gemini' if result.mapping.source == 'llm' else 'rules (heuristic)'}** and validated by code"
    )
    mapping_rows = [
        {
            "source column": role.column,
            "role": role.role,
            "pollutant": role.pollutant or "—",
            "unit in header": role.unit or "—",
            "confidence": role.confidence,
        }
        for role in result.mapping.columns
    ]
    if mapping_rows:
        st.dataframe(pd.DataFrame(mapping_rows), hide_index=True, **STRETCH)
    else:
        st.caption("No columns were mapped.")
    if result.mapping.date_format_hint:
        st.caption(f"Date format hint: `{result.mapping.date_format_hint}`")
    if result.mapping.issues:
        st.warning("Mapper notes: " + " · ".join(result.mapping.issues))

    with st.expander(f"Cleaning log ({len(result.quality.cleaning_log)} entries)", expanded=False):
        entries = list(dict.fromkeys([*result.raw.notes, *result.notes, *result.quality.cleaning_log]))
        for entry in entries[:60]:
            st.markdown(f"- {entry}")
        if len(entries) > 60:
            st.caption(f"…and {len(entries) - 60} more.")
        if not entries:
            st.caption("No cleaning actions were needed.")

    with st.expander("Cleaned tidy table (first 60 rows)", expanded=False):
        tidy = result.tidy.copy()
        if "flags" in tidy.columns:
            tidy["flags"] = tidy["flags"].apply(lambda f: ", ".join(f) if isinstance(f, (list, tuple)) else "")
        st.dataframe(tidy.head(60), hide_index=True, **STRETCH)
        st.caption(f"{len(result.tidy)} tidy readings · next: check Quality →")


# ---------------------------------------------------------------- tab 2: quality
def quality_tab() -> None:
    _html(C.section("Quality of the data", "coverage, flags, reliability"))
    result = _get("result")
    if result is None:
        _html(C.empty_state("No data yet", "Load the sample week or your own table in the Data tab first.", "Load sample week 1"))
        return
    quality = result.quality
    tidy = result.tidy

    left, right, third = st.columns(3)
    left.metric("Raw rows", _show(quality.n_rows_in))
    right.metric("Tidy readings", _show(quality.n_readings))
    third.metric("Stations", _show(len(quality.stations)))

    real_counts = {flag: count for flag, count in (quality.flag_counts or {}).items() if flag in REAL_FLAGS}
    unit_counts = {flag: count for flag, count in (quality.flag_counts or {}).items() if flag in C.INFORMATIONAL_FLAGS}

    _html(C.section("Data-quality flags", "readings that were dropped, repaired or distrusted"))
    if real_counts:
        flags = pd.DataFrame(sorted(real_counts.items(), key=lambda kv: kv[1], reverse=True), columns=["flag", "readings"])
        if HAS_PLOTLY:
            fig = go.Figure(
                go.Bar(
                    x=flags["readings"],
                    y=flags["flag"],
                    orientation="h",
                    marker_color=DANGER_HEX,
                    text=flags["readings"],
                    textposition="outside",
                    customdata=[[FLAG_MEANING.get(flag, "")] for flag in flags["flag"]],
                    hovertemplate="%{y}: %{x} reading(s)<br>%{customdata[0]}<extra></extra>",
                )
            )
            fig.update_layout(
                template="streamlit",
                height=max(200, 34 * len(flags) + 80),
                margin=dict(l=10, r=42, t=10, b=10),
                xaxis_title="readings flagged",
                yaxis=dict(autorange="reversed"),
            )
            fig.update_traces(cliponaxis=False)
            st.plotly_chart(fig, theme="streamlit", **STRETCH)
        else:
            st.dataframe(flags, hide_index=True, **STRETCH)
    else:
        st.caption("No reading carries a data-quality flag.")

    _html(C.section("Unit notes", "informational — these readings are still used"))
    if unit_counts:
        items = [(f"{flag}: {_show(count)} reading(s)", "") for flag, count in sorted(unit_counts.items(), key=lambda kv: -kv[1])]
        _html(C.vitals(items))
        if "UNIT_AMBIGUOUS" in unit_counts:
            st.caption(
                "UNIT_AMBIGUOUS means the header carried no unit, so the standard unit was assumed. "
                "These notes are deliberately kept out of the chart above and out of the reliability score."
            )
    else:
        st.caption("Every reading arrived with a usable unit.")

    _html(C.section("Station reliability", "reliability = coverage × (1 − flagged share)"))
    st.caption("🟢 reliable ≥ 0.75 · 🟡 use with caution 0.5–0.75 · 🔴 unreliable < 0.5")
    if quality.stations:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "station": s.station,
                        "label": s.label,
                        "reliability": round(s.reliability, 2),
                        "coverage": round(s.coverage, 2),
                        "flagged share": round(s.flagged_share, 2),
                        "readings": s.n_readings,
                        "notes": "; ".join(s.notes[:3]) if s.notes else "",
                    }
                    for s in sorted(quality.stations, key=lambda s: s.reliability)
                ]
            ),
            hide_index=True,
            column_config={
                "reliability": st.column_config.ProgressColumn("reliability", min_value=0.0, max_value=1.0, format="%.2f"),
                "coverage": st.column_config.ProgressColumn("coverage", min_value=0.0, max_value=1.0, format="%.2f"),
            },
            **STRETCH,
        )
    else:
        st.caption("No stations detected.")

    _html(C.section("Hour-by-hour ribbon", "hover a segment for its duration"))
    _ribbon_legend_and_rows(result)

    _html(C.section("Gaps and what we could not use", ""))
    if quality.gaps:
        for gap in quality.gaps[:40]:
            st.markdown(f"- {gap}")
    else:
        st.caption("No significant gaps were detected.")

    _html(C.section("Flagged rows", "individual readings behind the counts above"))
    tidy_flags = tidy.copy()
    if "flags" in tidy_flags.columns:
        mask = tidy_flags["flags"].apply(
            lambda f: isinstance(f, (list, tuple)) and any(flag in REAL_FLAGS for flag in f)
        )
        flagged = tidy_flags[mask].copy()
        flagged["flags"] = flagged["flags"].apply(lambda f: ", ".join(f))
        if flagged.empty:
            st.caption("No individual reading carries a real data-quality flag.")
        else:
            columns = [c for c in TIDY_COLUMNS if c in flagged.columns]
            st.dataframe(flagged[columns].head(200), hide_index=True, **STRETCH)
            if len(flagged) > 200:
                st.caption(f"Showing the first 200 of {len(flagged)} flagged rows.")
    else:
        st.caption("The tidy table carries no flags column.")

    st.caption("Next: the Insights tab turns these readings into hotspots with an evidence trail →")


def _ribbon_legend_and_rows(result) -> None:
    """Per-station quality ribbons plus the shared legend."""
    ribbons = ribbon_segments(result)
    if not ribbons:
        st.caption("No hourly timestamps, so the quality ribbon cannot be drawn.")
        return
    rows: list[str] = []
    totals: dict[str, float] = {}
    for station, segments in sorted(ribbons.items()):
        for segment in segments:
            totals[segment["kind"]] = totals.get(segment["kind"], 0.0) + segment["weight"]
        total = sum(s["weight"] for s in segments) or 1
        valid = sum(s["weight"] for s in segments if s["kind"] == "valid")
        rows.append(
            '<div class="pw-ribbon-row">'
            f'<span class="pw-ribbon-row__name">{C.esc(station)}</span>'
            f'<span class="pw-ribbon-row__bar">{C.quality_ribbon(segments, compact=True)}</span>'
            f'<span class="pw-ribbon-row__pct">{valid / total:.0%} valid</span>'
            "</div>"
        )
    grand = sum(totals.values()) or 1
    legend = "".join(
        f'<span><i class="pw-ribbon__seg--{C.esc(kind)}"></i>'
        f"{RIBBON_LABEL.get(kind, kind)} {_show(totals[kind])} h ({totals[kind] / grand:.0%})</span>"
        for kind, _ in sorted(totals.items(), key=lambda kv: -kv[1])
    )
    _html("".join(rows) + f'<div class="pw-ribbon-legend">{legend}</div>')


# ---------------------------------------------------------------- tab 3: insights
def insights_tab() -> None:
    _html(C.section("AQI, hotspots and evidence", "every claim points at source rows"))
    result = _get("result")
    if result is None:
        _html(C.empty_state("No data yet", "Load the sample week or your own table in the Data tab first.", "Load sample week 1"))
        return
    brief = result.brief
    diff_result = result.diff
    findings = brief.findings
    daily = daily_aqi_cached(brief, result.tidy)

    headline = area_headline(result)
    if headline:
        _html(
            C.aqi_hero(
                area=brief.area,
                aqi=headline["aqi"],
                category=headline["category"],
                color=headline["color"],
                dominant=headline["dominant"],
                dominant_value=headline["value"],
                dominant_unit=headline["unit"],
                who_multiple=headline["multiple"],
                confidence=headline["confidence"],
                n_stations=headline["n_stations"],
                period=headline["period"],
                label=f"worst station · {headline['station']}",
                notes=[headline["mean_label"]],
            )
        )
    else:
        _html(C.empty_state("No station could be scored", "Check the Quality tab: an AQI day needs 3 pollutants including PM2.5 or PM10.", "Open the Quality tab"))

    _html(C.section("Stations", "latest valid day · click through the findings below"))
    cards = station_cards(result, daily)
    if cards:
        _html(cards)
    else:
        st.caption("No station could be scored from this table.")

    if brief.station_aqi:
        with st.expander("Station table (all numbers)", expanded=False):
            rows = []
            for station in sorted(brief.station_aqi, key=lambda s: (s.aqi is None, -(s.aqi or 0))):
                rows.append(
                    {
                        "station": station.station,
                        "ward": station.ward or "—",
                        "AQI": round(station.aqi) if station.aqi is not None else None,
                        "category": station.category or "no valid day",
                        "dominant": station.dominant or "—",
                        "confidence": station.confidence,
                        "mean period AQI": round(station.mean_aqi_period, 1) if station.mean_aqi_period is not None else None,
                        "trend / day": round(station.trend_per_day, 1) if station.trend_per_day is not None else None,
                        "valid days": station.n_valid_days,
                        "latest date": station.latest_date or "—",
                    }
                )
            st.dataframe(pd.DataFrame(rows), hide_index=True, **STRETCH)

    if HAS_PLOTLY:
        valid = [s for s in brief.station_aqi if s.aqi is not None and s.category]
        if valid:
            _html(C.section("Latest AQI by station", "coloured by CPCB category"))
            fig = go.Figure()
            for category in CATEGORIES:
                subset = [s for s in valid if s.category == category]
                if not subset:
                    continue
                fig.add_bar(
                    x=[s.station for s in subset],
                    y=[s.aqi for s in subset],
                    name=category,
                    marker_color=_safe_color(category),
                    text=[f"{s.aqi:.0f}" for s in subset],
                    textposition="outside",
                )
            fig.update_layout(
                template="streamlit",
                barmode="stack",
                height=380,
                margin=dict(l=10, r=10, t=30, b=10),
                yaxis_title="AQI (latest valid day)",
                legend=dict(orientation="h", y=-0.25),
            )
            # Category bands as quiet dotted rules: the bar colours and the legend carry the names,
            # so no annotation competes with a bar on a 390px-wide screen.
            for threshold in (50, 100, 200, 300, 400):
                fig.add_hline(y=threshold, line_dash="dot", line_color="rgba(128,128,128,.45)")
            st.plotly_chart(fig, theme="streamlit", **STRETCH)
        else:
            st.caption("No valid daily AQI to chart. Check the Quality tab for the reason.")

        if not daily.empty and {"date", "aqi", "station"}.issubset(daily.columns):
            daily_valid = daily[daily["aqi"].notna()]
            if "valid" in daily_valid.columns:
                daily_valid = daily_valid[daily_valid["valid"]]
            if not daily_valid.empty:
                _html(C.section("Daily AQI trend per station", ""))
                trend = go.Figure()
                for station in sorted(daily_valid["station"].astype(str).unique()):
                    subset = daily_valid[daily_valid["station"].astype(str) == station]
                    trend.add_scatter(x=subset["date"].astype(str), y=subset["aqi"], mode="lines+markers", name=station)
                trend.update_layout(
                    template="streamlit",
                    height=420,
                    margin=dict(l=10, r=10, t=30, b=70),
                    yaxis_title="AQI",
                    xaxis_title="date",
                    legend=dict(orientation="h", y=-0.32),
                )
                st.plotly_chart(trend, theme="streamlit", **STRETCH)

    _html(C.section("Plain-language interpretation", "every sentence ends in a finding id"))
    interp = get_interp(brief, diff_result)
    if interp is None:
        stage_unavailable("The written interpretation")
    else:
        fallback_badge(interp)
        headline_text = getattr(interp, "headline", "")
        _html(f'<p class="pw-lead">{C.cite_html(headline_text, findings)}</p>')
        if getattr(interp, "trend_points", None):
            st.markdown("**Trends**")
            for point in interp.trend_points:
                _html(f"- {C.cite_html(point.text, findings)}")
        if getattr(interp, "hotspot_points", None):
            st.markdown("**Hotspots**")
            for point in interp.hotspot_points:
                _html(f"- {C.cite_html(point.text, findings)}")
        if getattr(interp, "cannot_tell", None):
            st.markdown("**What this data cannot tell us**")
            for item in interp.cannot_tell[:10]:
                st.markdown(f"- {item}")
        point_texts: list[Any] = [headline_text]
        point_texts += [p.text for p in (getattr(interp, "trend_points", None) or [])]
        point_texts += [p.text for p in (getattr(interp, "hotspot_points", None) or [])]
        _html(C.sources_row(findings, cited_ids(point_texts)))

    _html(C.section("Findings", "each claim has an id and its source rows"))
    if not findings:
        st.caption("No findings were computed for this table.")
        return

    with st.expander(f"All {len(findings)} findings", expanded=False):
        for finding in findings:
            _html(
                f'<div class="pw-finding"><b>{C.esc(finding.id)} · {C.esc(finding.title)}</b><br>'
                f"{C.chip(finding.type)} {C.dot_chip(f'confidence: {finding.confidence}', {'high': 'ok', 'medium': 'warn', 'low': 'danger'}.get(finding.confidence, 'warn'))} "
                f"{C.small(', '.join(finding.stations[:4]))}</div>"
            )

    finding_ids = [f.id for f in findings]
    titles = {f.id: f.title for f in findings}
    chosen = st.selectbox(
        "Trace a finding",
        finding_ids,
        format_func=lambda fid: f"{fid} · {titles.get(fid, '')}",
        key=f"finding_select_{brief.brief_id}",
    )
    finding = next((f for f in findings if f.id == chosen), None)
    if finding is not None:
        _html(
            C.section(f"{finding.id} · {finding.title}", f"{finding.type} · {finding.confidence} confidence")
        )
        meta = [C.chip(finding.type)]
        meta.append(C.dot_chip(f"confidence: {finding.confidence}", {"high": "ok", "medium": "warn", "low": "danger"}.get(finding.confidence, "warn")))
        if finding.stations:
            meta.append(C.chip("stations: " + ", ".join(finding.stations[:5])))
        _html("".join(meta))
        fact_rows = [
            {"fact": key, "value": _show(value, 1) if isinstance(value, (int, float)) else str(value)}
            for key, value in finding.facts.items()
        ]
        if fact_rows:
            st.dataframe(pd.DataFrame(fact_rows), hide_index=True, **STRETCH)
        _html(f'<p class="pw-small">How it is computed — {C.esc(finding.formula or "n/a")}</p>')
        if finding.caveats:
            st.markdown("**Caveats**")
            for caveat in finding.caveats:
                st.markdown(f"- {caveat}")
        if finding.evidence_row_ids:
            st.markdown(f"**Evidence rows** ({len(finding.evidence_row_ids)} row IDs)")
            evidence = result.tidy[result.tidy["row_id"].astype(str).isin([str(r) for r in finding.evidence_row_ids])]
            if evidence.empty:
                st.caption("The evidence rows are not present in this tidy table (they may belong to another run).")
            else:
                columns = [c for c in TIDY_COLUMNS if c in evidence.columns]
                shown = evidence[columns].head(50).copy()
                if "flags" in shown.columns:
                    shown["flags"] = shown["flags"].apply(lambda f: ", ".join(f) if isinstance(f, (list, tuple)) else "")
                st.dataframe(shown, hide_index=True, **STRETCH)
                st.caption(f"{len(evidence)} matching reading(s); showing up to 50.")
        else:
            st.caption("No row-level evidence was recorded for this finding.")

    verification_expander(interp, findings, extra_facts=diff_result.summary_facts if diff_result else None)
    st.caption("Next: the Actions tab turns the findings into a ranked plan for your audience →")


# ---------------------------------------------------------------- tab 4: actions
def actions_tab(persona: str) -> None:
    _html(C.section("Ranked, cited and owned", "switch audience to change the plan"))
    result = _get("result")
    if result is None:
        _html(C.empty_state("No data yet", "Load the sample week or your own table in the Data tab first.", "Load sample week 1"))
        return
    brief = result.brief
    diff_result = result.diff
    findings = brief.findings

    options = [PERSONA_LABEL[persona]] + [label for label, key in PERSONAS.items() if key != persona]
    preview = st.radio("Plan for", options, horizontal=True, key=f"persona_preview_{persona}")
    preview_key = PERSONAS[preview]
    if preview_key != persona:
        st.caption(f"Previewing **{preview}** — the sidebar audience stays {PERSONA_LABEL[persona]}.")

    plan = get_plan(brief, diff_result, preview_key)
    if plan is None:
        stage_unavailable(f"The action plan for {preview}")
        return
    fallback_badge(plan)
    if not plan.actions:
        st.info("No action in the curated library matches these findings. The data may be too thin to act on — check the Quality tab.")
        return

    _html(C.section(f"{len(plan.actions)} ranked action(s)", "from the curated library, each citing the findings that triggered it"))
    for action in plan.actions:
        ids = cited_ids(action.why_now, action.cites)
        _html(
            C.action_card(
                rank=action.rank,
                action=action.action,
                effort=action.effort,
                owner=action.owner,
                time_window=action.time_window,
                why_now_html=C.cite_html(action.why_now, findings),
                cites_html=C.sources_row(findings, ids or action.cites),
                data_gap_action=action.data_gap_action,
                library_id=action.library_id,
            )
        )

    st.caption("Next: the What changed tab compares this brief with the previous one →")


# ---------------------------------------------------------------- tab 5: what changed
def what_changed_tab(area: str) -> None:
    _html(C.section("This brief against the previous one", "deltas, transitions, hotspots"))
    result = _get("result")
    if result is None:
        _html(C.empty_state("No data yet", "Load the sample week or your own table in the Data tab first.", "Load sample week 1"))
        return
    brief = result.brief
    diff_result = result.diff

    if not diff_result.has_previous:
        st.info(
            "**This is the baseline brief.** There is no earlier brief yet, so there is nothing to compare against. "
            "Load the sample **week 1** first, then **week 2**, and this tab fills in."
        )
        baseline_facts = diff_result.summary_facts or {}
        b1, b2, b3 = st.columns(3)
        b1.metric("Stations scored", _show(baseline_facts.get("n_stations")))
        b2.metric("Findings recorded", _show(baseline_facts.get("n_findings")))
        worst_aqi = baseline_facts.get("max_aqi")
        worst_name = baseline_facts.get("worst_station") or ""
        b3.metric("Worst AQI this brief", f"{_show(worst_aqi)}" + (f" · {worst_name}" if worst_name else ""))
        if st.button("Use sample week 1 as the previous brief", key="baseline_week1"):
            payload = read_sample(1)
            if payload is None:
                st.warning("data/sample_week1.csv is missing from this build.")
            else:
                data, name = payload
                try:
                    with st.spinner("Building the baseline from sample week 1…"):
                        baseline = pipeline_mod.run_pipeline(
                            file_bytes=data,
                            filename=name,
                            area=(area or "").strip() or AREA_DEFAULT,
                            use_llm_mapper=False,
                            store_dir=BRIEF_DIR,
                            save=True,
                            previous=None,
                        )
                    result.previous = baseline.brief
                    result.diff = diff_mod.diff_briefs(brief, baseline.brief)
                    diff_result = result.diff
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not build the baseline ({type(exc).__name__}). Use “Load sample week 1” in the sidebar instead.")
                    return
        if not diff_result.has_previous:
            return

    st.caption(
        f"Compared with brief `{diff_result.previous_brief_id or 'previous'}` · "
        f"current brief `{brief.brief_id}`"
    )
    facts = diff_result.summary_facts or {}
    chips: list[tuple[str, str]] = []
    if "n_worse" in facts:
        chips.append((f"{_show(facts['n_worse'])} station(s) worse", "danger" if facts["n_worse"] else "ok"))
    if "n_better" in facts:
        chips.append((f"{_show(facts['n_better'])} station(s) better", "ok" if facts["n_better"] else ""))
    if facts.get("mean_delta") is not None:
        mean_delta = float(facts["mean_delta"])
        chips.append((f"mean AQI change {mean_delta:+.1f}", "danger" if mean_delta > 0 else "ok"))
    if facts.get("worst_station"):
        chips.append((f"worst: {facts['worst_station']} {_show(facts.get('worst_delta'), 1)}", "warn"))
    if facts.get("max_aqi_now") is not None:
        chips.append((f"worst AQI now {_show(facts['max_aqi_now'])}", ""))
    _html(C.vitals(chips))

    if diff_result.station_changes:
        _html(C.section("Station deltas", "worse = red, better = green, arrow plus sign always shown"))
        _html(
            C.delta_grid(
                [
                    C.delta_card(
                        name=change.station,
                        delta=change.delta,
                        direction=change.direction,
                        before=change.category_prev,
                        after=change.category_now,
                        color_before=_safe_color(change.category_prev),
                        color_after=_safe_color(change.category_now),
                    )
                    for change in diff_result.station_changes
                ]
            )
        )
    else:
        st.caption("No station-level changes were computed.")

    col1, col2 = st.columns(2)
    with col1:
        _html(C.section("Category transitions", ""))
        if diff_result.category_transitions:
            for line in diff_result.category_transitions:
                _html(f'<div class="pw-transition-line">▬ {C.esc(_plain(line))}</div>')
        else:
            st.caption("No station changed CPCB category.")
        _html(C.section("New hotspots", ""))
        if diff_result.new_hotspots:
            _html("".join(C.chip(station, "warn") for station in diff_result.new_hotspots))
        else:
            st.caption("None.")
    with col2:
        _html(C.section("Resolved hotspots", ""))
        if diff_result.resolved_hotspots:
            _html("".join(C.chip(station, "ok") for station in diff_result.resolved_hotspots))
        else:
            st.caption("None.")
        _html(C.section("Data-quality changes", ""))
        if diff_result.quality_changes:
            for line in diff_result.quality_changes:
                st.markdown(f"- {_plain(line)}")
        else:
            st.caption("No station reliability or gap changes.")

    if diff_result.new_finding_titles:
        _html(C.section("Findings new in this brief", ""))
        for title in diff_result.new_finding_titles:
            _html(f'- {C.esc(title)}')

    st.caption("Next: export the Briefing note and the Citizen advisory →")


# ---------------------------------------------------------------- tab 6: briefing note
def note_tab(persona: str) -> None:
    _html(C.section("Internal briefing note", "exportable, citations kept"))
    result = _get("result")
    if result is None:
        _html(C.empty_state("No data yet", "Load the sample week or your own table in the Data tab first.", "Load sample week 1"))
        return
    brief = result.brief
    diff_result = result.diff
    interp = get_interp(brief, diff_result)
    plan = get_plan(brief, diff_result, persona)
    note = get_note(brief, diff_result, interp, plan)

    if note is None:
        stage_unavailable("The internal briefing note")
        return
    fallback_badge(note)
    st.caption(f"Audience: {PERSONA_LABEL[persona]} · area: {brief.area} · brief `{brief.brief_id}`")
    body = note.markdown or "_No note text was generated._"
    _html(f'<div class="pw-note">{C.markdown_html(body, brief.findings)}</div>')
    _html(C.sources_row(brief.findings, cited_ids(body)))
    st.download_button(
        "⬇️ Download briefing note (.md)",
        data=body.encode("utf-8"),
        file_name=f"brief_{brief.brief_id}.md",
        mime="text/markdown",
        key="download_note",
    )
    verification_expander(note, brief.findings, extra_facts=diff_result.summary_facts if diff_result else None)
    st.caption("Next: the Citizen advisory tab writes the public version in each selected language →")


# ---------------------------------------------------------------- tab 7: citizen advisory
def whatsapp_text(advisory) -> str:
    care = getattr(advisory, "care", None)
    lines = [f"*{advisory.headline}*", "", f"AQI category: {advisory.category}", "", advisory.today_message, ""]
    if advisory.what_to_do:
        lines.append("What to do:")
        lines.extend(f"• {item}" for item in advisory.what_to_do)
        lines.append("")
    if care is not None:
        lines.extend(
            [
                f"Children: {care.children}",
                f"Older adults: {care.older_adults}",
                f"Sensitive groups: {care.sensitive_groups}",
                "",
            ]
        )
    lines.append(advisory.source_line)
    lines.append(f"[{advisory.language} · {advisory.reading_level}] · not medical advice")
    return "\n".join(lines)


def advisory_tab(area: str, languages: list[str], levels: list[str]) -> None:
    _html(C.section("Citizen advisory", "one card per language × reading level · print-ready"))
    result = _get("result")
    if result is None:
        _html(C.empty_state("No data yet", "Load the sample week or your own table in the Data tab first.", "Load sample week 1"))
        return
    brief = result.brief
    if not languages or not levels:
        st.info("Pick at least one **language** and one **reading level** in the sidebar to generate advisories.")
        return

    combos = [(language, level) for language in languages for level in levels]
    st.caption(f"{len(combos)} advisory variant(s) · {len(languages)} language(s) × {len(levels)} reading level(s) · generated once per brief and cached.")
    _html(
        C.vitals(
            [
                ("print: browser Print gives a one-pager", ""),
                ("tone: CPCB category text, no diagnosis", ""),
                ("not medical advice", "warn"),
            ]
        )
    )

    generated: list[tuple[str, str, Any]] = []
    for language, level in combos:
        advisory = get_advisory(brief, language, level, area or brief.area)
        if advisory is None:
            st.warning(f"Could not generate the {language} / {level} advisory right now.")
            continue
        generated.append((language, level, advisory))
        care = getattr(advisory, "care", None)
        _html(
            C.advisory_card(
                headline=advisory.headline,
                category=advisory.category,
                color=_safe_color(advisory.category),
                category_emoji=_safe_emoji(advisory.category),
                language=advisory.language,
                level=advisory.reading_level,
                today_html=C.cite_html(advisory.today_message, brief.findings, empty=C.esc(advisory.today_message)),
                what_to_do=advisory.what_to_do,
                care={
                    "children": getattr(care, "children", "") if care else "",
                    "older_adults": getattr(care, "older_adults", "") if care else "",
                    "sensitive_groups": getattr(care, "sensitive_groups", "") if care else "",
                },
                source_line=advisory.source_line,
                cites_html=C.sources_row(brief.findings, cited_ids(advisory.today_message, advisory.what_to_do, advisory.cites)),
                used_fallback=getattr(advisory, "used_fallback", False),
            )
        )
        with st.expander("📋 Copy as WhatsApp text"):
            st.code(whatsapp_text(advisory), language="text")

    if generated:
        bundle = "\n\n" + ("\n\n" + "-" * 60 + "\n\n").join(
            f"# {advisory.language} · {advisory.reading_level}\n\n{whatsapp_text(advisory)}" for _, _, advisory in generated
        )
        st.download_button(
            "⬇️ Download all advisories (.txt)",
            data=bundle.encode("utf-8"),
            file_name=f"advisories_{brief.brief_id}.txt",
            mime="text/plain",
            key="download_advisories",
        )
        with st.expander("🔎 Verification — citations and numbers checked by code"):
            total = 0
            details = []
            for language, level, advisory in generated:
                try:
                    try:
                        issues = verify_mod.check_object(advisory, brief.findings, result.diff.summary_facts or {})
                    except TypeError:
                        issues = verify_mod.check_object(advisory, brief.findings)
                except Exception as exc:
                    st.caption(f"Verification is unavailable ({type(exc).__name__}).")
                    issues = []
                issues = list(issues or [])
                total += len(issues)
                for issue in issues:
                    details.append(
                        {
                            "variant": f"{language} / {level}",
                            "sentence": getattr(issue, "sentence", ""),
                            "problem": getattr(issue, "kind", ""),
                            "detail": getattr(issue, "detail", ""),
                        }
                    )
            if total == 0:
                st.success("✅ No issues found across every generated advisory.")
            else:
                _html(C.chip(f"{total} issue(s)", "warn"))
                st.dataframe(pd.DataFrame(details), hide_index=True, **STRETCH)


# ---------------------------------------------------------------- main
# ---------------------------------------------------------------- extra tabs (feature modules, all optional)
def _feature(module: str):
    """Import a ui feature module lazily; the app keeps working if one is missing or broken."""
    try:
        import importlib

        return importlib.import_module(f"ui.{module}")
    except Exception:
        return None


def _guarded(label: str, fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception as exc:  # one broken panel must never take the page down
        st.warning(f"{label} is unavailable right now ({type(exc).__name__}). The rest of the app is unaffected.")


def what_changed_extras(persona: str) -> None:
    result = _get("result")
    mod = _feature("changes_cockpit")
    if result is None or mod is None:
        return
    diff_result = result.diff
    _html(C.section("In plain words", "what changed since the previous brief"))
    _guarded("Change summary", lambda: mod.render_change_summary(st, diff_result))
    _html(C.section("Findings and hotspots", "new, resolved, unverified"))
    _guarded("Finding changes", lambda: mod.render_finding_changes(st, diff_result))
    if diff_result.has_previous and result.previous is not None and llm_stages is not None:
        _html(C.section("Recommended actions", f"added and dropped for {PERSONA_LABEL.get(persona, persona)}"))
        cur = get_plan(result.brief, diff_result, persona)
        prev_brief = result.previous
        prev = llm_stage(
            "Rebuilding last brief's plan…",
            ("plan_prev", prev_brief.brief_id, persona),
            lambda: llm_stages.plan_actions(prev_brief, diff_mod.diff_briefs(prev_brief, None), persona),
        )
        _guarded(
            "Action changes",
            lambda: mod.render_actions_changed(st, prev, cur, PERSONA_LABEL.get(persona, persona)),
        )


def ask_briefing_tab(persona: str) -> None:
    result = _get("result")
    mod = _feature("ask_tab")
    if mod is None:
        st.info("The Ask tab is not available in this build.")
        return
    _guarded("Ask the briefing", lambda: mod.render_ask_tab(st, result.brief, result.diff, persona))


def prove_it_tab() -> None:
    result = _get("result")
    mod = _feature("prove")
    if mod is None:
        st.info("The Prove-it panel is not available in this build.")
        return
    _html(C.section("Prove it", "click through from a claim to the original rows"))
    _guarded("Prove it", lambda: mod.render_prove_it_selector(st, result.brief, result.tidy))
    _html(C.section("Try to fool the verifier", "unsupported numbers and fake citations get caught"))
    _guarded("Verifier demo", lambda: mod.render_fool_it(st, result.brief))


def prompt_cockpit_tab(persona: str) -> None:
    result = _get("result")
    mod = _feature("changes_cockpit")
    if mod is None:
        st.info("The prompt cockpit is not available in this build.")
        return
    cache = _get("llm_cache", {}) or {}
    results = {str(k[0]): v for k, v in cache.items() if isinstance(k, tuple) and k and not str(k[0]).startswith("plan_prev")}
    _guarded("Prompt cockpit", lambda: mod.render_prompt_cockpit(st, result.brief, result.diff, results))


def main() -> None:
    init_state()
    theme.inject()

    persona, area, languages, levels = sidebar()

    result = _get("result")
    if result is None:
        start_screen(area)
    else:
        _html(f'<div class="pw-title">{C.esc(result.brief.area)} · air quality briefing</div>')
        _html(vitals_row(result, persona))
        st.caption(
            f"Loaded **{_get('loaded_label') or result.raw.source_name}** · "
            f"{result.brief.period_start or '?'} → {result.brief.period_end or '?'} · "
            f"brief `{result.brief.brief_id}` · audience **{PERSONA_LABEL[persona]}**"
        )

        tabs = st.tabs(
            [
                "1 · Data",
                "2 · Quality",
                "3 · Insights",
                "4 · Actions",
                "5 · What changed",
                "6 · Briefing note",
                "7 · Citizen advisory",
                "8 · Ask the briefing",
                "9 · Prove it",
                "10 · Prompt cockpit",
            ]
        )
        with tabs[0]:
            data_tab(area)
        with tabs[1]:
            quality_tab()
        with tabs[2]:
            insights_tab()
        with tabs[3]:
            actions_tab(persona)
        with tabs[4]:
            what_changed_tab(area)
            what_changed_extras(persona)
        with tabs[5]:
            note_tab(persona)
        with tabs[6]:
            advisory_tab(area, languages, levels)
        with tabs[7]:
            ask_briefing_tab(persona)
        with tabs[8]:
            prove_it_tab()
        with tabs[9]:
            prompt_cockpit_tab(persona)
    st.divider()
    st.caption(
        "Code computes every number (CPCB sub-indices, trends, diffs); Gemini only writes, and every "
        "sentence is checked against the findings. No medical advice."
    )


main()
