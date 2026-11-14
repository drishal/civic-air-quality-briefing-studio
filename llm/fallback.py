"""Deterministic, template-based versions of every LLM stage.

These run with no API key: they build the interpretation, action plan,
internal note and citizen advisory from findings and quality data alone.
Numbers and place names are never translated; every claim keeps its [F#] cite.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from core.models import CATEGORIES, Brief, DiffResult, Finding
from llm.schemas import Action, ActionPlan, Advisory, CareBlock, InternalNote, Interpretation, Point

PERSONAS = ("ward_officer", "housing_society", "school")
LIBRARY_PATH = Path(__file__).resolve().parent.parent / "data" / "action_library.yaml"
_MAX_ACTIONS = 5

_CONF_RANK = {"high": 0, "medium": 1, "low": 2}
_TYPE_ORDER = {"hotspot": 0, "exceedance": 1, "trend": 2, "pattern": 3, "anomaly": 4, "gap": 5}

# Persona parameters for the P3 prompt and the offline plan.
PERSONA_BRIEFS: dict[str, dict[str, str]] = {
    "ward_officer": {
        "authority": (
            "Municipal ward officer: road sweeping, water sprinkling, construction-site dust "
            "inspection, open-burning patrols, traffic coordination with the traffic police, "
            "public notices, escalation to the state pollution board."
        ),
        "limits": (
            "Cannot close schools, cannot order industrial shutdowns, cannot issue fines beyond "
            "municipal rules, cannot invoke GRAP outside Delhi-NCR. No new legal powers."
        ),
    },
    "housing_society": {
        "authority": (
            "Housing society committee (RWA): in-society burning and dust rules, common-area "
            "upkeep, DG-set scheduling, event timings, notice board and WhatsApp communication."
        ),
        "limits": (
            "No authority over public roads, traffic or neighbouring sites. Cannot give medical "
            "advice or tell residents to stop prescribed medicines."
        ),
    },
    "school": {
        "authority": (
            "School administration: assembly and sports scheduling, indoor alternatives, gate "
            "traffic and idling rules, parent communication, staff and student safety."
        ),
        "limits": (
            "No authority over traffic outside the gate, no medical decisions for students, no "
            "diagnosis. Emergency care goes to the school nurse or a doctor."
        ),
    },
}

# ------------------------------------------------------------------ small helpers


def fill_template(template: str, **values: Any) -> str:
    """Replace {NAME} placeholders; unknown placeholders are left as written."""
    return _PLACEHOLDER.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template
    )


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_CAUSE_WORDS = re.compile(r"\b(caused by|because of|due to|is responsible for)\b", re.IGNORECASE)


def _hedge(text: str) -> str:
    """Replace firm causal wording with 'consistent with'."""
    return _CAUSE_WORDS.sub("consistent with", text)


def _finite(value: Any) -> bool:
    """True for a real, finite number (not bool, not NaN)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(value: Any) -> str:
    """Format a number tightly, keeping one decimal only when it matters."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if float(value).is_integer():
        return f"{int(value)}"
    if abs(value) >= 10:
        return f"{value:.0f}"
    return f"{value:.1f}"


_KEY_LABELS = {
    "aqi": "AQI", "mean_aqi_period": "period mean AQI", "trend_per_day": "AQI change per day",
    "slope_per_day": "AQI change per day", "delta_total": "total AQI change", "first_aqi": "first AQI",
    "last_aqi": "latest AQI", "n_stuck": "stuck readings", "n_spike": "spikes", "n_readings": "readings",
    "n_gaps": "gaps", "n_stations_with_gaps": "stations with gaps", "hours_missing": "hours missing",
    "coverage": "coverage", "flagged_share": "flagged share", "reliability": "reliability",
    "max_avg": "highest average", "max_multiple": "times the WHO limit", "max_station": "worst station",
    "n_exceeding": "stations over the limit", "n_stations": "stations", "who_24h_limit": "WHO 24-hour limit",
    "peak_hour": "peak hour", "peak_mean": "peak average", "trough_hour": "lowest hour",
    "trough_mean": "lowest average", "peak_to_trough": "peak-to-low ratio", "n_hours": "hours",
    "n_days": "days", "n_valid_days": "valid days", "dominant": "dominant pollutant",
    "best_delta": "best change", "worst_delta": "worst change", "mean_delta": "average change",
    "max_aqi_now": "highest AQI now", "max_aqi_prev": "highest AQI before", "n_worse": "stations worse",
    "n_better": "stations better", "n_same": "stations unchanged", "n_new_hotspots": "new hotspots",
    "n_resolved_hotspots": "resolved hotspots", "n_category_transitions": "category changes",
    "worst_station": "worst-hit station", "best_station": "most improved station",
}
_QUIET_KEYS = {"aqi_kind", "verification_needed", "delta_threshold", "n_stations_now", "n_stations_prev"}


def _human_key(key: str) -> str:
    return _KEY_LABELS.get(key) or re.sub(r"^n_", "number of ", key).replace("_", " ").strip()


def aqi_category(aqi: float | None) -> str | None:
    """CPCB National AQI category for a numeric value."""
    if not _finite(aqi):
        return None
    value = float(aqi)
    if value <= 50:
        return "Good"
    if value <= 100:
        return "Satisfactory"
    if value <= 200:
        return "Moderately Polluted"
    if value <= 300:
        return "Poor"
    if value <= 400:
        return "Very Poor"
    return "Severe"


def _severity(category: str | None) -> int:
    """Index of a category in CATEGORIES, -1 when unknown."""
    try:
        return CATEGORIES.index(category or "")
    except ValueError:
        return -1


def _facts_phrase(facts: dict[str, Any], limit: int = 3) -> str:
    """Readable 'key value' pairs from a finding's facts, numbers only."""
    parts: list[str] = []
    for key, value in facts.items():
        if len(parts) >= limit:
            break
        if isinstance(value, bool) or value is None or key in _QUIET_KEYS:
            continue
        if isinstance(value, (int, float)):
            parts.append(f"{_human_key(key)} {_num(value)}")
        elif isinstance(value, str) and value.strip() and not re.fullmatch(r"[FC]\d+", value.strip()):
            parts.append(f"{_human_key(key)} {value.strip()}")
    return ", ".join(parts)


def _fact_matches(brief: Brief, value: float) -> bool:
    """True when a number appears in any finding fact (so it is safe to cite)."""
    for finding in brief.findings:
        for fact in finding.facts.values():
            if isinstance(fact, (int, float)) and not isinstance(fact, bool):
                if abs(float(fact) - float(value)) <= max(0.5, abs(float(value)) * 0.005):
                    return True
    return False


def _ordered_findings(brief: Brief) -> list[Finding]:
    """Findings by type priority, then confidence, then ID."""
    return sorted(
        brief.findings,
        key=lambda f: (_TYPE_ORDER.get(f.type, 9), _CONF_RANK.get(f.confidence, 2), f.id),
    )


def _hour_label(value: Any) -> str | None:
    """'19:00' from an hour int (19) or a written hour ('19' or '19:30')."""
    if _finite(value):
        return f"{int(value):02d}:00"
    text = str(value or "").strip()
    if re.fullmatch(r"\d{1,2}", text):
        return f"{int(text):02d}:00"
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        return text
    return None


def _peak_window(brief: Brief) -> str | None:
    """Peak time window from a pattern finding, if any ('19:00-21:00')."""
    for finding in brief.findings:
        if finding.type != "pattern":
            continue
        start = _hour_label(finding.facts.get("window_start"))
        end = _hour_label(finding.facts.get("window_end"))
        if start and end and start != end:
            return f"{start}-{end}"
        peak = _hour_label(finding.facts.get("peak_hour"))
        if peak:
            return peak
        for key, value in finding.facts.items():
            if isinstance(value, str) and re.search(r"\d{1,2}[:.]\d{2}", value):
                return value.strip()
        hours = re.findall(r"\d{1,2}[:.]\d{2}", finding.title)
        if len(hours) >= 2:
            return f"{hours[0].replace('.', ':')}-{hours[1].replace('.', ':')}"
    return None


def _signature_text(brief: Brief, dominant: str | None = None) -> str:
    """Signature words from every finding plus the dominant pollutant.

    Library triggers name descriptive signatures ("NO2 and CO moving together");
    the ranker matches those on shared words, so gather all the wording here.
    """
    chunks: list[str] = []
    for finding in brief.findings:
        chunks.append(finding.title)
        chunks.append(finding.type)
        for key, value in finding.facts.items():
            if isinstance(value, str):
                chunks.append(f"{key} {value}")
    chunks += brief.quality.gaps
    if dominant:
        chunks.append(f"{dominant} dominant")
    return " ".join(chunks).lower()




def brief_snapshot(brief: Brief, station: str | None = None) -> dict[str, Any]:
    """Worst station, category, dominant pollutant and quality notes for templates.

    With `station`, the snapshot describes that one station/ward instead of the worst one,
    so an advisory can be written for a single area. City scope also reports the best
    station and the category range across the city.
    """
    rows: list[tuple[int, float, Any, str | None]] = []
    for entry in brief.station_aqi:
        if not entry.station:
            continue
        aqi = float(entry.aqi) if _finite(entry.aqi) else None
        category = entry.category if entry.category in CATEGORIES else aqi_category(aqi)
        rows.append((_severity(category), aqi if aqi is not None else -1.0, entry, category))
    worst = max(rows, key=lambda row: (row[0], row[1])) if rows else None
    best = min(rows, key=lambda row: (row[0], row[1])) if rows else None

    focus = None
    if station:
        wanted = station.strip().lower()
        focus = next((row for row in rows if str(row[2].station).strip().lower() == wanted), None)
    chosen = focus if focus is not None else worst
    scope = "station" if station else "city"

    stations = [entry.station for entry in brief.station_aqi if entry.station]
    dominant = None
    if chosen is not None and chosen[2].dominant:
        dominant = chosen[2].dominant
    else:
        for entry in brief.station_aqi:
            if entry.dominant:
                dominant = entry.dominant
                break

    date = brief.period_end or (chosen[2].latest_date if chosen is not None else None)
    if not date:
        date = (brief.created_at or "")[:10] or "date not recorded"

    quality = brief.quality
    unreliable = [s.station for s in quality.stations if s.label == "unreliable"]
    caution = [s.station for s in quality.stations if s.label == "use with caution"]
    if focus is not None and scope == "station":
        name = str(focus[2].station)
        unreliable = [name] if name in unreliable else []
        caution = [name] if name in caution else []

    range_text = _range_text(best, worst)
    return {
        "area": brief.area or "this area",
        "scope": scope,
        "station": focus[2].station if focus is not None else None,
        "category": chosen[3] if chosen is not None else None,
        "aqi": float(chosen[2].aqi) if chosen is not None and _finite(chosen[2].aqi) else None,
        "dominant": dominant,
        "worst_station": worst[2].station if worst is not None else None,
        "worst_category": worst[3] if worst is not None else None,
        "best_station": best[2].station if best is not None else None,
        "best_category": best[3] if best is not None else None,
        "range_text": range_text,
        "stations": [chosen[2].station] if scope == "station" and focus is not None else stations[:8],
        "date": date,
        "severity": chosen[0] if chosen is not None else -1,
        "gaps": list(quality.gaps),
        "unreliable": unreliable,
        "caution": caution,
        "peak_window": _peak_window(brief),
        "signature": _signature_text(brief, dominant),
    }


def _range_text(
    best: tuple[int, float, Any, str | None] | None,
    worst: tuple[int, float, Any, str | None] | None,
) -> str:
    """'ranges from Moderately Polluted at Karve Road to Very Poor at Hadapsar'."""
    if best is None or worst is None or not best[3] or not worst[3]:
        return ""
    if best[2].station == worst[2].station:
        return f"is {worst[3]} at {worst[2].station}"
    if best[3] == worst[3]:
        return f"is {worst[3]} at every station (worst {worst[2].station}, best {best[2].station})"
    return f"ranges from {best[3]} at {best[2].station} to {worst[3]} at {worst[2].station}"


# ------------------------------------------------------------------ action library

DEFAULT_ACTION_LIBRARY: list[dict[str, Any]] = [
    # ---------------------------------------------------------- ward officer
    {
        "id": "fallback-wo-1",
        "persona": ["ward_officer"],
        "title": "Sweep and sprinkle water on hotspot road segments",
        "action": (
            "Run mechanised sweeping and water sprinkling on the road segments serving the worst "
            "station, before the morning and evening traffic peaks."
        ),
        "trigger": {"pollutants": ["PM10", "PM2.5"], "min_category": "Moderately Polluted"},
        "effort": "medium",
        "typical_owner": "ward officer, municipal sanitation",
        "notes": "Record the segments and timings so the next brief can check the effect.",
    },
    {
        "id": "fallback-wo-2",
        "persona": ["ward_officer"],
        "title": "Inspect construction sites for dust controls",
        "action": (
            "Inspect active construction and demolition sites near the hotspot for uncovered "
            "debris, missing barricades and dry cutting, and require dust-control measures."
        ),
        "trigger": {"pollutants": ["PM10"], "min_category": "Moderately Polluted"},
        "effort": "medium",
        "typical_owner": "ward officer, building permission cell",
        "notes": "Construction dust shows up as coarse PM10 rather than PM2.5.",
    },
    {
        "id": "fallback-wo-3",
        "persona": ["ward_officer"],
        "title": "Patrol for open waste and leaf burning at peak hours",
        "action": (
            "Send a patrol through the hotspot lanes during the evening peak to stop open waste "
            "and leaf burning, and clear the dumped waste that gets burnt."
        ),
        "trigger": {"pollutants": ["PM2.5"], "min_category": "Poor", "time_pattern": "evening peak"},
        "effort": "low",
        "typical_owner": "ward officer, sanitation supervisor",
        "notes": "Burning raises PM2.5 and CO together, so a PM2.5-CO signature supports this.",
    },
    {
        "id": "fallback-wo-4",
        "persona": ["ward_officer"],
        "title": "Manage traffic at the identified junction",
        "action": (
            "Post traffic management at the junction nearest the hotspot during peak hours: keep "
            "engines off while waiting, clear blocked lanes and stop illegal parking."
        ),
        "trigger": {"pollutants": ["NO2", "CO"], "min_category": "Moderately Polluted"},
        "effort": "medium",
        "typical_owner": "ward officer with traffic police",
        "notes": "Traffic exhaust shows as NO2 and CO together.",
    },
    {
        "id": "fallback-wo-5",
        "persona": ["ward_officer"],
        "title": "Verify the station readings before acting on them",
        "action": (
            "Ask the station operator to check the sensor, its inlet and its calibration, and log "
            "a field reading nearby. Publish actions only after the reading is confirmed."
        ),
        "trigger": {"pollutants": []},
        "effort": "low",
        "typical_owner": "ward officer, sensor operator",
        "notes": "Use when a station is flagged unreliable, stuck or spiking.",
    },
    {
        "id": "fallback-wo-6",
        "persona": ["ward_officer"],
        "title": "Put up public notice and signage at the hotspot",
        "action": (
            "Put the current category, the health advice and the helpline number on the notice "
            "board and on signage at the hotspot, in the local language."
        ),
        "trigger": {"pollutants": []},
        "effort": "low",
        "typical_owner": "ward officer, information cell",
        "notes": "Cheap, visible and useful even when the data is thin.",
    },
    {
        "id": "fallback-wo-7",
        "persona": ["ward_officer"],
        "title": "Escalate to the state pollution board",
        "action": (
            "Escalate to the state pollution control board with the findings, evidence rows and "
            "the multi-day trend, and ask for inspection of the nearby sources."
        ),
        "trigger": {"pollutants": ["PM2.5", "PM10"], "min_category": "Very Poor"},
        "effort": "medium",
        "typical_owner": "ward officer, municipal commissioner office",
        "notes": "Reserve for Very Poor or Severe readings that persist for several days.",
    },
    {
        "id": "fallback-wo-8",
        "persona": ["ward_officer"],
        "title": "Coordinate with schools and resident groups",
        "action": (
            "Send the category and the peak hours to schools, clinics and resident groups in the "
            "ward so they can shift outdoor activity and protect sensitive residents."
        ),
        "trigger": {"pollutants": [], "min_category": "Poor"},
        "effort": "low",
        "typical_owner": "ward officer, education and health liaison",
        "notes": "One message reaches many people quickly.",
    },
    # ---------------------------------------------------------- housing society
    {
        "id": "fallback-hs-1",
        "persona": ["housing_society"],
        "title": "Stop all burning inside the society",
        "action": (
            "Ban waste, leaf and garden burning in the society, and tell staff to hand over dry "
            "waste for collection instead of burning it."
        ),
        "trigger": {"pollutants": ["PM2.5"], "min_category": "Moderately Polluted"},
        "effort": "low",
        "typical_owner": "society committee, housekeeping",
        "notes": "Burning is the easiest local PM2.5 source to remove.",
    },
    {
        "id": "fallback-hs-2",
        "persona": ["housing_society"],
        "title": "Control dust from in-society construction",
        "action": (
            "Require covered debris, barricades and water spraying at any renovation or "
            "construction inside the society, and stop dry cutting of tiles and stone."
        ),
        "trigger": {"pollutants": ["PM10"], "min_category": "Moderately Polluted"},
        "effort": "medium",
        "typical_owner": "society committee, contractor",
        "notes": "One uncovered debris pile can raise local PM10 for the whole street.",
    },
    {
        "id": "fallback-hs-3",
        "persona": ["housing_society"],
        "title": "Water the paths and landscaped areas",
        "action": (
            "Water the internal roads, paths and bare landscaped patches in the morning so dust "
            "does not lift with traffic."
        ),
        "trigger": {"pollutants": ["PM10"], "min_category": "Moderately Polluted"},
        "effort": "low",
        "typical_owner": "society committee, gardener",
        "notes": "Use treated water where available; avoid wastage.",
    },
    {
        "id": "fallback-hs-4",
        "persona": ["housing_society"],
        "title": "Shift outdoor events and gym time away from peak hours",
        "action": (
            "Move society events, children's play and outdoor gym sessions out of the evening "
            "peak and into the cleanest morning hours."
        ),
        "trigger": {"pollutants": ["PM2.5"], "min_category": "Moderately Polluted", "time_pattern": "evening peak"},
        "effort": "low",
        "typical_owner": "society committee, event coordinators",
        "notes": "Timing changes cost nothing and cut exposure.",
    },
    {
        "id": "fallback-hs-5",
        "persona": ["housing_society"],
        "title": "Close windows at peak hours and run purifiers in common rooms",
        "action": (
            "Close the windows of flats and the clubhouse during peak hours, and run air "
            "purifiers in the clubhouse and any indoor play area."
        ),
        "trigger": {"pollutants": ["PM2.5"], "min_category": "Poor"},
        "effort": "low",
        "typical_owner": "society committee, facility staff",
        "notes": "Open windows during the peak let outdoor PM2.5 straight in.",
    },
    {
        "id": "fallback-hs-6",
        "persona": ["housing_society"],
        "title": "Check on older and sensitive residents",
        "action": (
            "Have volunteers call or visit older residents and anyone with asthma, heart or lung "
            "disease, share the advisory and help them arrange medicines if needed."
        ),
        "trigger": {"pollutants": [], "min_category": "Poor"},
        "effort": "low",
        "typical_owner": "society committee, volunteers",
        "notes": "Welfare contact, not medical advice.",
    },
    {
        "id": "fallback-hs-7",
        "persona": ["housing_society"],
        "title": "Put the advisory on the notice board and WhatsApp group",
        "action": (
            "Post the current category, the peak hours and the care steps for children, older "
            "adults and sensitive residents on the notice board and the society WhatsApp group."
        ),
        "trigger": {"pollutants": []},
        "effort": "low",
        "typical_owner": "society committee, communications",
        "notes": "Keep numbers and station names unchanged in every language.",
    },
    {
        "id": "fallback-hs-8",
        "persona": ["housing_society"],
        "title": "Cut diesel generator running hours",
        "action": (
            "Limit diesel generator use to power failures only, and schedule any test runs outside "
            "the evening peak."
        ),
        "trigger": {"pollutants": ["NO2", "CO"], "min_category": "Satisfactory"},
        "effort": "medium",
        "typical_owner": "society committee, electrical contractor",
        "notes": "Generators add NO2 and CO right where people live.",
    },
    # ---------------------------------------------------------- school
    {
        "id": "fallback-sc-1",
        "persona": ["school"],
        "title": "Move assembly and physical education indoors",
        "action": (
            "Hold assembly and physical education indoors while the category is Moderately "
            "Polluted or worse, and use the hall or classrooms for games."
        ),
        "trigger": {"pollutants": ["PM2.5", "PM10"], "min_category": "Moderately Polluted"},
        "effort": "low",
        "typical_owner": "principal, sports teachers",
        "notes": "Set the threshold in the school calendar so the decision is not argued daily.",
    },
    {
        "id": "fallback-sc-2",
        "persona": ["school"],
        "title": "Stop all outdoor activity at Poor or worse",
        "action": (
            "Cancel outdoor sports, morning assembly and field trips when the category reaches "
            "Poor or worse, and keep students in classrooms."
        ),
        "trigger": {"pollutants": ["PM2.5"], "min_category": "Poor"},
        "effort": "low",
        "typical_owner": "principal",
        "notes": "Matches the CPCB health-impact text for the Poor band.",
    },
    {
        "id": "fallback-sc-3",
        "persona": ["school"],
        "title": "Ban idling at the school gate",
        "action": (
            "Ask parents and bus drivers to switch off engines while waiting at the gate, and "
            "stagger the dismissal times so the queue clears faster."
        ),
        "trigger": {"pollutants": ["NO2", "CO"], "min_category": "Satisfactory"},
        "effort": "medium",
        "typical_owner": "principal, transport coordinator",
        "notes": "The gate queue is often the highest NO2 spot children breathe.",
    },
    {
        "id": "fallback-sc-4",
        "persona": ["school"],
        "title": "Manage classroom ventilation by the hour",
        "action": (
            "Open windows for fresh air in the clean morning hours, and close them during the "
            "evening peak or when smoke is visible outside."
        ),
        "trigger": {"pollutants": ["PM2.5"], "min_category": "Moderately Polluted"},
        "effort": "low",
        "typical_owner": "principal, class teachers",
        "notes": "Ventilation advice depends on the hour, not only the daily category.",
    },
    {
        "id": "fallback-sc-5",
        "persona": ["school"],
        "title": "Let sensitive students stay in",
        "action": (
            "Excuse students with asthma, heart or lung disease from outdoor activity without a "
            "medical certificate, and keep their inhalers with the class teacher or nurse."
        ),
        "trigger": {"pollutants": [], "min_category": "Poor"},
        "effort": "low",
        "typical_owner": "principal, school nurse",
        "notes": "Support access to prescribed medicine; do not advise on treatment.",
    },
    {
        "id": "fallback-sc-6",
        "persona": ["school"],
        "title": "Send a parent advisory in the local language",
        "action": (
            "Send the category, the school's activity rule and the care steps for children to "
            "parents in the local language, keeping numbers and station names unchanged."
        ),
        "trigger": {"pollutants": []},
        "effort": "low",
        "typical_owner": "principal, communications",
        "notes": "Parents need the rule, not the raw table.",
    },
    {
        "id": "fallback-sc-7",
        "persona": ["school"],
        "title": "Keep inhalers and first-aid ready",
        "action": (
            "Check that the sick room has salbutamol inhalers, spacers and the school's emergency "
            "contact list, and tell staff what to do if a student cannot breathe."
        ),
        "trigger": {"pollutants": [], "min_category": "Poor"},
        "effort": "low",
        "typical_owner": "school nurse, principal",
        "notes": "Emergency readiness is not a diagnosis.",
    },
]


def _personas_of(item: dict[str, Any]) -> list[str]:
    """Persona list from either key used by the library."""
    value = item.get("persona", item.get("personas", []))
    if isinstance(value, str):
        return [value.strip()]
    return [str(v).strip() for v in value or []]


def _normalise_item(raw: Any) -> dict[str, Any] | None:
    """Clean one library entry; None when it has no usable id or action."""
    if not isinstance(raw, dict):
        return None
    item_id = str(raw.get("id") or "").strip()
    action = str(raw.get("action") or raw.get("title") or "").strip()
    personas = [p for p in _personas_of(raw) if p]
    if not item_id or not action or not personas:
        return None
    trigger = raw.get("trigger") if isinstance(raw.get("trigger"), dict) else {}
    effort = str(raw.get("effort") or "medium").strip().lower()
    if effort not in ("low", "medium", "high"):
        effort = {"med": "medium", "medium-high": "high", "low-medium": "medium"}.get(effort, "medium")
    return {
        "id": item_id,
        "persona": personas,
        "title": str(raw.get("title") or action[:80]).strip(),
        "action": action,
        "trigger": {
            "pollutants": [str(p) for p in trigger.get("pollutants") or []],
            "min_category": trigger.get("min_category"),
            "time_pattern": trigger.get("time_pattern"),
            "signature": trigger.get("signature"),
        },
        "effort": effort,
        "typical_owner": str(raw.get("typical_owner") or "").strip(),
        "notes": str(raw.get("notes") or "").strip(),
    }


def load_action_library(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Curated actions from data/action_library.yaml, topped up with the built-in list.

    The YAML is written by another agent; when it is absent, short or unreadable the
    built-in list is used so every persona still has actions.
    """
    items: list[dict[str, Any]] = []
    target = Path(path) if path is not None else LIBRARY_PATH
    loaded = _read_yaml_library(target)
    if len(loaded) >= 6:
        items = loaded
    merged = list(items)
    known = {item["id"] for item in merged}
    for persona in PERSONAS:
        if sum(1 for item in merged if persona in item["persona"]) >= 3:
            continue
        for fallback_item in DEFAULT_ACTION_LIBRARY:
            if persona in fallback_item["persona"] and fallback_item["id"] not in known:
                merged.append(fallback_item)
                known.add(fallback_item["id"])
    return merged or list(DEFAULT_ACTION_LIBRARY)


def _read_yaml_library(path: Path) -> list[dict[str, Any]]:
    """Best-effort YAML read; [] when the file is absent or malformed."""
    try:
        if not path.is_file():
            return []
        import yaml  # local import: offline runs do not need it

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - any YAML/IO problem means "no library"
        return []
    if isinstance(data, dict):
        data = data.get("actions") or data.get("library") or []
    if not isinstance(data, list):
        return []
    items = [item for item in (_normalise_item(raw) for raw in data) if item]
    return items


def library_for_persona(persona: str) -> list[dict[str, Any]]:
    """Library entries that apply to one persona."""
    persona = (persona or "").strip().lower()
    return [item for item in load_action_library() if persona in item["persona"]]


def library_payload(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Public fields only, for prompt JSON."""
    keys = ("id", "title", "action", "trigger", "effort", "typical_owner", "notes")
    return [{key: item.get(key) for key in keys} for item in items]


# ------------------------------------------------------------------ ranking


_SIG_STOP = {
    "with", "from", "that", "this", "readings", "reading", "elevated", "elevation",
    "matching", "without", "moving", "together", "same", "many",
}


def _signature_tokens(text: str) -> set[str]:
    """Lowercase words long enough to carry meaning."""
    words = re.findall(r"[a-z0-9.]+", text.lower())
    return {word for word in words if len(word) >= 4 and word not in _SIG_STOP}


def _score_item(item: dict[str, Any], snapshot: dict[str, Any], brief: Brief) -> tuple[int, list[str]]:
    """Ranking score and the reasons that fired, for one library item."""
    trigger = item.get("trigger") or {}
    score = 0
    reasons: list[str] = []

    min_category = trigger.get("min_category")
    if min_category == "Good":
        score += 1  # "Good" means the trigger is not category-gated
    elif min_category and snapshot["severity"] >= _severity(min_category) >= 0:
        # The most demanding trigger that still fires fits today best.
        score += 3 + _severity(min_category)
        reasons.append(f"category at or above {min_category}")

    pollutants = [str(p).upper() for p in trigger.get("pollutants") or []]
    dominant = (snapshot["dominant"] or "").upper()
    if pollutants and dominant and dominant in pollutants:
        score += 2
        reasons.append(f"dominant pollutant is {dominant}")

    signature = str(trigger.get("signature") or "")
    hits = _signature_tokens(signature) & _signature_tokens(snapshot["signature"] or "")
    if len(hits) >= 2:
        score += 2
        reasons.append("signature match: " + ", ".join(sorted(hits)[:3]))
    elif hits:
        score += 1

    time_pattern = str(trigger.get("time_pattern") or "").lower()
    window = (snapshot["peak_window"] or "").lower()
    if time_pattern and window and ("peak" in time_pattern or time_pattern in window):
        score += 1
        reasons.append(f"peak window {snapshot['peak_window']}")

    low_trust = bool(snapshot["unreliable"] or snapshot["caution"])
    text = f"{item.get('title', '')} {item.get('action', '')}".lower()
    if low_trust and ("sensor" in text or "calibrat" in text or "verify" in text):
        score += 2  # a data-quality action is useful, but not above safety actions
        reasons.append("station data needs verification")

    if any(finding.type == "gap" for finding in brief.findings) and "gap" in text:
        score += 1
    return score, reasons


def rank_actions(brief: Brief, persona: str, limit: int = _MAX_ACTIONS) -> list[dict[str, Any]]:
    """Library actions for a persona, best fit first."""
    snapshot = brief_snapshot(brief)
    scored = []
    for index, item in enumerate(library_for_persona(persona)):
        score, reasons = _score_item(item, snapshot, brief)
        scored.append((score, index, item, reasons))
    scored.sort(key=lambda row: (-row[0], row[1]))
    ranked = []
    for rank, (score, _index, item, reasons) in enumerate(scored[:limit], start=1):
        entry = dict(item)
        entry["_rank"] = rank
        entry["_score"] = score
        entry["_reasons"] = reasons
        ranked.append(entry)
    return ranked


def _best_cite(brief: Brief, item: dict[str, Any]) -> str:
    """Finding ID that best supports one action ('' when there is none)."""
    if not brief.findings:
        return ""
    pollutants = [str(p).lower() for p in (item.get("trigger") or {}).get("pollutants") or []]
    ordered = _ordered_findings(brief)
    if pollutants:
        for finding in ordered:
            haystack = f"{finding.title} {_facts_phrase(finding.facts, 6)}".lower()
            if any(pollutant in haystack for pollutant in pollutants):
                return finding.id
    for finding in ordered:
        if finding.type in ("hotspot", "exceedance"):
            return finding.id
    return ordered[0].id

_CITE_PATTERN = re.compile(r"\[[A-Z]\d{1,3}\]")


def _with_cite(text: str, cite: str) -> str:
    """Cite every sentence of `text` that has no citation yet."""
    body = (text or "").strip()
    if not body or not cite:
        return body
    parts: list[str] = []
    for raw in re.split(r"(?<=[.!?।])\s+", body):
        sentence = raw.strip()
        if not sentence:
            continue
        if not _CITE_PATTERN.search(sentence):
            if sentence.endswith("?"):
                sentence = f"{sentence} [{cite}]"
            else:
                sentence = f"{sentence.rstrip('.').rstrip('।').rstrip()} [{cite}]."
        parts.append(sentence)
    return " ".join(parts)


def _station_cite(brief: Brief, station: str) -> str:
    """Finding ID that best supports a statement about one station ('' when none)."""
    matches = [finding for finding in brief.findings if station and station in finding.stations]
    for wanted in ("gap", "anomaly", "hotspot", "exceedance", "trend", "pattern"):
        for finding in matches:
            if finding.type == wanted:
                return finding.id
    if matches:
        return matches[0].id
    return _ordered_findings(brief)[0].id if brief.findings else ""


def _top_cite(brief: Brief) -> str:
    """Citation for a general statement: the first finding in rank order."""
    ordered = _ordered_findings(brief)
    return ordered[0].id if ordered else ""


def _cite_for_scope(brief: Brief, snapshot: dict[str, Any]) -> str:
    """Citation for the advisory: the focused station's hotspot, else its gap, else the top finding."""
    station = snapshot.get("station") or snapshot.get("worst_station") or ""
    if station:
        matches = [finding for finding in brief.findings if station in finding.stations]
        for wanted in ("hotspot", "gap", "anomaly", "exceedance"):
            for finding in matches:
                if finding.type == wanted:
                    return finding.id
        if matches:
            return matches[0].id
    return _top_cite(brief)
def _cite_for_text(brief: Brief, text: str) -> str:
    """Cite the finding that matches the statement: its gap or sensor finding first.

    A gap statement ("no readings", "only 14 valid hours") must cite a gap or anomaly finding,
    not the exceedance finding that happens to list every station.
    """
    lowered = (text or "").lower()
    wants_gap = any(
        cue in lowered
        for cue in ("gap", "no readings", "valid hour", "missing", "unreliable", "caution")
    )
    wants_sensor = any(cue in lowered for cue in ("flagged", "stuck", "spike", "suspect", "sensor"))
    preferred: tuple[str, ...] = ()
    if wants_gap:
        preferred = ("gap", "anomaly")
    elif wants_sensor:
        preferred = ("anomaly",)

    names = [
        str(entry.station or "").strip()
        for entry in brief.station_aqi
        if entry.station and str(entry.station).lower() in lowered
    ]
    if preferred:
        for wanted in preferred:
            for finding in brief.findings:
                if finding.type == wanted and (not names or any(n in finding.stations for n in names)):
                    return finding.id
    for name in names:
        return _station_cite(brief, name)
    if preferred:
        for wanted in preferred:
            for finding in brief.findings:
                if finding.type == wanted:
                    return finding.id
    return _top_cite(brief)


# ------------------------------------------------------------------ interpretation


def interpretation_fallback(brief: Brief, diff: DiffResult) -> Interpretation:
    """Plain-language interpretation built from findings, with citations."""
    snapshot = brief_snapshot(brief)
    findings = _ordered_findings(brief)

    if snapshot["category"]:
        headline = f"{snapshot['area']} is {snapshot['category']} today"
        if snapshot["aqi"] is not None and _fact_matches(brief, snapshot["aqi"]):
            headline += f" (AQI {_num(round(snapshot['aqi']))})"
        if snapshot["worst_station"]:
            headline += f", with {snapshot['worst_station']} the worst station"
    else:
        headline = "insufficient evidence for a daily AQI category in this data"
    if findings:
        headline += f" [{findings[0].id}]"
    headline = _hedge(headline.rstrip(".")) + "."

    trend_points = [
        _point(f) for f in findings if f.type in ("trend", "exceedance")
    ][:4]
    hotspot_points = [
        _point(f) for f in findings if f.type in ("hotspot", "pattern", "anomaly")
    ][:4]

    cannot_tell: list[str] = []
    if not diff.has_previous:
        cannot_tell.append(
            "No previous brief to compare against, so what changed since last time cannot be told."
        )
    for gap in snapshot["gaps"][:3]:
        cannot_tell.append(_with_cite(_hedge(gap.strip().rstrip(".")) + ".", _cite_for_text(brief, gap)))
    for station in snapshot["unreliable"][:2]:
        cannot_tell.append(
            _with_cite(
                f"Station {station} is unreliable, so its readings are shown but down-weighted.",
                _station_cite(brief, station),
            )
        )
    if not cannot_tell:
        cannot_tell.append(
            "Sensor readings show how polluted the air is, not what caused it or who is affected."
        )

    return Interpretation(
        headline=headline,
        trend_points=trend_points,
        hotspot_points=hotspot_points,
        cannot_tell=cannot_tell[:6],
        used_fallback=True,
    )


def _point(finding: Finding) -> Point:
    """One cited sentence from a finding; every sentence carries its own citation."""
    body = _hedge(finding.title.strip().rstrip("."))
    phrase = _facts_phrase(finding.facts)
    if phrase:
        body += f": {phrase}"
    text = f"{body} [{finding.id}]."
    if finding.confidence == "low":
        text += f" Low confidence, so verify this on site before acting [{finding.id}]."
    return Point(text=text, cites=[finding.id])

# ------------------------------------------------------------------ action plan


def plan_fallback(brief: Brief, diff: DiffResult, persona: str) -> ActionPlan:
    """Ranked actions from the library's trigger match, with citations."""
    persona = (persona or "ward_officer").strip().lower()
    snapshot = brief_snapshot(brief)
    ranked = rank_actions(brief, persona)
    low_trust = bool(snapshot["unreliable"] or snapshot["caution"])
    low_confidence = any(f.confidence == "low" for f in brief.findings)
    gap_action = ""
    if low_trust or low_confidence:
        station = (snapshot["unreliable"] or snapshot["caution"] or [""])[0]
        where = f" at {station}" if station else ""
        gap_action = f"Verify the reading{where} before publishing this action."

    actions: list[Action] = []
    for index, item in enumerate(ranked, start=1):
        cite = _best_cite(brief, item)
        why = _why_now(brief, snapshot, item)
        actions.append(
            Action(
                rank=index,
                action=_with_cite(item["action"], cite),
                why_now=why,
                cites=[cite] if cite else [],
                owner=item.get("typical_owner") or persona.replace("_", " "),
                effort=item.get("effort") or "medium",
                time_window=_time_window(item, snapshot),
                data_gap_action=gap_action if index == 1 else "",
                library_id=item["id"],
            )
        )

    if not actions:
        cite = brief.findings[0].id if brief.findings else ""
        actions = [
            Action(
                rank=1,
                action=(
                    "Check the monitoring station and the local sources before planning further "
                    "action; the library has no matching action for this category."
                ),
                why_now="insufficient evidence for a targeted action [%s]." % cite if cite else
                "insufficient evidence for a targeted action.",
                cites=[cite] if cite else [],
                owner=persona.replace("_", " "),
                effort="low",
                time_window="this week",
                data_gap_action="Verify the reading before publishing.",
                library_id="suggested_new",
            )
        ]
    return ActionPlan(persona=persona, actions=actions[:_MAX_ACTIONS], used_fallback=True)


def _why_now(brief: Brief, snapshot: dict[str, Any], item: dict[str, Any]) -> str:
    """Why this action fits today, with a citation."""
    bits: list[str] = []
    if snapshot["category"]:
        bits.append(f"the area is {snapshot['category']} today")
    dominant = snapshot["dominant"]
    if dominant:
        bits.append(f"{dominant} is the dominant pollutant")
    reasons = item.get("_reasons") or []
    # The dominant-pollutant reason would repeat the line above.
    if reasons and not str(reasons[0]).startswith("dominant pollutant"):
        bits.append(str(reasons[0]))
    text = ", ".join(bits) if bits else "the current readings support this action"
    cite = _best_cite(brief, item)
    return _hedge(text) + (f" [{cite}]" if cite else "")

def _time_window(item: dict[str, Any], snapshot: dict[str, Any]) -> str:
    """today / this week / this month, from the trigger and the category."""
    pattern = str((item.get("trigger") or {}).get("time_pattern") or "").lower()
    if "peak" in pattern or snapshot["severity"] >= _severity("Poor"):
        return "today"
    if item.get("effort") == "high" and snapshot["severity"] < _severity("Moderately Polluted"):
        return "this month"
    return "this week"


# ------------------------------------------------------------------ internal note


def internal_note_fallback(
    brief: Brief,
    diff: DiffResult,
    interp: Interpretation,
    plan: ActionPlan,
) -> InternalNote:
    """Fixed-section internal briefing note in Markdown, citations kept."""
    snapshot = brief_snapshot(brief)
    top = _top_cite(brief)
    lines: list[str] = ["## Situation"]
    situation = interp.headline.strip().rstrip(".")
    if snapshot["dominant"] and snapshot["dominant"].lower() not in situation.lower():
        situation += f". Dominant pollutant {snapshot['dominant']}" + (f" [{top}]" if top else "")
    lines.append(situation + ".")
    lines += ["", "## What changed since last brief"]
    if diff.has_previous:
        changes: list[str] = []
        if diff.summary_facts:
            changes.append(_with_cite(f"Summary facts: {_facts_phrase(diff.summary_facts, 5)}", top))
        changes += [_hedge(change) for change in diff.category_transitions[:4]]
        changes += [f"New hotspot: {station}" for station in diff.new_hotspots[:3]]
        changes += [f"No longer a hotspot: {station}" for station in diff.resolved_hotspots[:3]]
        changes += [_hedge(change) for change in diff.quality_changes[:3]]
        changes = [_with_cite(change, _cite_for_text(brief, change)) for change in changes]
        lines += [f"- {change}" for change in changes] or [
            "- No material change was recorded in the diff."
        ]
    else:
        lines.append(
            "- No previous brief was saved, so change over time cannot be stated. "
            "Load or save a brief to enable the comparison."
        )

    lines += ["", "## Hotspots"]
    hotspots = [f for f in _ordered_findings(brief) if f.type in ("hotspot", "exceedance", "pattern")][:4]
    if hotspots:
        for finding in hotspots:
            text = _hedge(finding.title.strip().rstrip("."))
            phrase = _facts_phrase(finding.facts, 2)
            if phrase:
                text += f" ({phrase})"
            text += f" [{finding.id}]"
            if finding.confidence != "high":
                text += f" — confidence {finding.confidence}"
            lines.append(f"- {text}")
    else:
        lines.append("- No hotspot finding in this brief.")

    lines += ["", "## Data caveats"]
    caveats = list(snapshot["gaps"][:3])
    caveats += [
        f"Station {station} is unreliable; its readings are down-weighted."
        for station in snapshot["unreliable"][:2]
    ]
    caveats += [
        f"Station {station} is usable with caution."
        for station in snapshot["caution"][:2]
    ]
    flag_counts = brief.quality.flag_counts or {}
    if flag_counts:
        top_flags = sorted(flag_counts.items(), key=lambda kv: -kv[1])[:3]
        caveats.append(
            "Flagged readings: " + ", ".join(f"{flag} {count}" for flag, count in top_flags)
        )
    lines += [
        f"- {_with_cite(_hedge(text.strip().rstrip('.')), _cite_for_text(brief, text))}"
        for text in caveats
    ] or [
        "- No data caveats were recorded."
    ]

    lines += ["", "## Actions by owner"]
    if plan.actions:
        for action in plan.actions:
            cite = f" [{action.cites[0]}]" if action.cites else ""
            lines.append(
                f"- {action.owner}: {action.action} ({action.time_window}, effort {action.effort}){cite}"
            )
    else:
        lines.append("- No actions were generated.")

    lines += ["", "## Open questions"]
    questions = [
        f"- {_with_cite(_hedge(item), _cite_for_text(brief, item))}" for item in interp.cannot_tell[:4]
    ]
    if snapshot["peak_window"]:
        pattern = next(
            (finding.id for finding in brief.findings if finding.type == "pattern"), top
        )
        questions.append(
            _with_cite(
                f"- Does source activity follow the peak window {snapshot['peak_window']}? Check on site.",
                pattern,
            )
        )
    questions.append("- Do the flagged stations reproduce their readings tomorrow?")
    lines += questions

    return InternalNote(markdown="\n".join(lines), used_fallback=True)


# ------------------------------------------------------------------ advisory

_ADVISORY_EN: dict[str, dict[str, Any]] = {
    "Good": {
        "headline": "Air quality is good today.",
        "message": "Today's AQI is {aqi}, category Good. The air is clean and normal outdoor activity is fine.",
        "do": [
            "Open windows and let fresh air in.",
            "Outdoor play, walking and exercise are fine.",
            "Keep checking the AQI through the day.",
        ],
        "children": "Children can play outside as usual.",
        "older_adults": "Older adults can take a normal walk; carry water.",
        "sensitive_groups": "People with asthma or heart and lung conditions can do normal activity; keep your medicine nearby.",
    },
    "Satisfactory": {
        "headline": "Air quality is satisfactory today.",
        "message": "Today's AQI is {aqi}, category Satisfactory. Most people can go outside.",
        "do": [
            "Outdoor activity is fine; keep long runs short.",
            "Rest if you feel breathless.",
            "Close windows for a while during the evening peak.",
        ],
        "children": "Outdoor play is fine; watch for coughing or eye irritation.",
        "older_adults": "Short walks are fine; avoid long outings in the evening.",
        "sensitive_groups": "People with asthma or heart and lung conditions should keep the reliever inhaler with them.",
    },
    "Moderately Polluted": {
        "headline": "Air quality is moderately polluted today.",
        "message": "Today's AQI is {aqi}, category Moderately Polluted. People sensitive to poor air should cut outdoor exertion.",
        "do": [
            "Do heavy outdoor work early in the morning.",
            "Avoid busy roads during the evening peak.",
            "Keep windows closed at peak hours.",
            "Wear an N95 mask if you must be outside for long.",
        ],
        "children": "Keep outdoor play short and avoid the evening peak.",
        "older_adults": "Skip long walks and rest if you feel tired or breathless.",
        "sensitive_groups": "People with asthma or heart and lung conditions should keep the reliever inhaler with them and avoid outdoor exercise.",
    },
    "Poor": {
        "headline": "Air quality is poor today.",
        "message": "Today's AQI is {aqi}, category Poor. Outdoor activity should be limited, especially for children and older adults.",
        "do": [
            "Move exercise and outdoor games indoors.",
            "Wear an N95 mask for any outdoor travel.",
            "Keep windows closed, especially in the evening.",
            "Run an air purifier if you have one.",
        ],
        "children": "Keep children indoors; move games and sports to an indoor room.",
        "older_adults": "Stay indoors and postpone outdoor walks.",
        "sensitive_groups": "People with asthma, heart or lung disease, and pregnant women should stay indoors, keep medicine and inhalers at hand, and contact a doctor if symptoms get worse.",
    },
    "Very Poor": {
        "headline": "Air quality is very poor today.",
        "message": "Today's AQI is {aqi}, category Very Poor. Avoid outdoor activity as far as possible.",
        "do": [
            "Stay indoors unless travel is necessary.",
            "Wear an N95 mask if you must go out, and keep it short.",
            "Keep windows and doors closed.",
            "Run an air purifier in the room you use most.",
            "Stop all outdoor exercise and sports.",
        ],
        "children": "Children should stay indoors; no outdoor school activity.",
        "older_adults": "Older adults should not go out; keep essential medicines within reach.",
        "sensitive_groups": "People with asthma, heart or lung disease, and pregnant women should stay indoors and see a doctor if breathing becomes difficult.",
    },
    "Severe": {
        "headline": "Air quality is severely polluted today.",
        "message": "Today's AQI is {aqi}, category Severe. This is an emergency level; stay indoors.",
        "do": [
            "Stay indoors and stop all outdoor activity.",
            "Keep windows and doors closed and seal gaps if smoke comes in.",
            "Wear an N95 mask if you must step out, and keep it brief.",
            "Call a doctor if you have chest pain, wheezing or severe breathlessness.",
        ],
        "children": "Children must stay indoors and away from windows and smoke.",
        "older_adults": "Older adults must stay indoors and avoid any exertion.",
        "sensitive_groups": "People with asthma, heart or lung disease, and pregnant women must stay indoors and contact a doctor at the first sign of breathing trouble.",
    },
    "Data unavailable": {
        "headline": "Not enough valid readings today.",
        "message": "Today's AQI could not be computed from the readings we received. Treat the air as uncertain.",
        "do": [
            "Limit outdoor activity until the category is known.",
            "Ask the ward office to check the station.",
            "Keep windows closed near busy roads.",
        ],
        "children": "Keep children's outdoor play short until the category is confirmed.",
        "older_adults": "Older adults should keep outdoor trips short until the data is confirmed.",
        "sensitive_groups": "People with asthma or heart and lung conditions should carry their medicine and avoid busy roads.",
    },
}

_ADVISORY_HI: dict[str, dict[str, Any]] = {
    "Good": {
        "headline": "आज हवा अच्छी है।",
        "message": "आज का सूचकांक {aqi} है और श्रेणी अच्छी है। हवा साफ़ है, बाहर की सामान्य गतिविधि ठीक है।",
        "do": [
            "खिड़कियाँ खोलकर घर में ताज़ी हवा आने दें।",
            "बाहर खेलना, टहलना और कसरत करना ठीक है।",
            "दिन भर सूचकांक देखते रहें।",
        ],
        "children": "बच्चे बाहर सामान्य रूप से खेल सकते हैं।",
        "older_adults": "बड़े-बुज़ुर्ग सामान्य सैर कर सकते हैं; पानी साथ रखें।",
        "sensitive_groups": "जिन्हें अस्थमा या दिल-फेफड़े की बीमारी है, वे सामान्य गतिविधि कर सकते हैं; दवा पास रखें।",
    },
    "Satisfactory": {
        "headline": "आज हवा संतोषजनक है।",
        "message": "आज का सूचकांक {aqi} है और श्रेणी संतोषजनक है। ज़्यादातर लोग बाहर जा सकते हैं।",
        "do": [
            "बाहरी गतिविधि ठीक है, लंबी दौड़ कम रखें।",
            "साँस फूलने पर आराम करें।",
            "शाम के व्यस्त समय में खिड़कियाँ थोड़ी देर बंद रखें।",
        ],
        "children": "बाहर खेलना ठीक है; खाँसी या आँखों में जलन देखें।",
        "older_adults": "छोटी सैर ठीक है; शाम को लंबी बाहरी गतिविधि टालें।",
        "sensitive_groups": "अस्थमा या दिल-फेफड़े की बीमारी वाले लोग इनहेलर साथ रखें।",
    },
    "Moderately Polluted": {
        "headline": "आज हवा मध्यम प्रदूषित है।",
        "message": "आज का सूचकांक {aqi} है और श्रेणी मध्यम प्रदूषित है। संवेदनशील लोग बाहर की मेहनत कम करें।",
        "do": [
            "भारी बाहरी काम सुबह जल्दी करें।",
            "शाम के व्यस्त समय में व्यस्त सड़कों से बचें।",
            "व्यस्त समय में खिड़कियाँ बंद रखें।",
            "ज़्यादा देर बाहर रहना हो तो N95 मास्क पहनें।",
        ],
        "children": "बच्चों का बाहरी खेल छोटा रखें और शाम के व्यस्त समय में बाहर न भेजें।",
        "older_adults": "लंबी सैर टालें और थकान या साँस फूलने पर आराम करें।",
        "sensitive_groups": "अस्थमा या दिल-फेफड़े की बीमारी वाले लोग इनहेलर साथ रखें और बाहरी कसरत से बचें।",
    },
    "Poor": {
        "headline": "आज हवा खराब है।",
        "message": "आज का सूचकांक {aqi} है और श्रेणी खराब है। बाहरी गतिविधि कम करें, खासकर बच्चों और बड़े-बुज़ुर्गों के लिए।",
        "do": [
            "कसरत और बाहरी खेल घर के अंदर करें।",
            "बाहर जाना हो तो N95 मास्क पहनें।",
            "खिड़कियाँ बंद रखें, खासकर शाम के समय।",
            "एयर प्यूरीफायर हो तो चलाएँ।",
        ],
        "children": "बच्चों को घर के अंदर रखें; खेल किसी अंदर के कमरे में कराएँ।",
        "older_adults": "घर के अंदर रहें और बाहर की सैर टालें।",
        "sensitive_groups": "अस्थमा, दिल या फेफड़े की बीमारी वाले लोग और गर्भवती महिलाएँ घर के अंदर रहें, दवा और इनहेलर पास रखें, और लक्षण बढ़ने पर डॉक्टर से मिलें।",
    },
    "Very Poor": {
        "headline": "आज हवा बहुत खराब है।",
        "message": "आज का सूचकांक {aqi} है और श्रेणी बहुत खराब है। बाहर की गतिविधि ज़्यादा से ज़्यादा कम करें।",
        "do": [
            "ज़रूरी न हो तो बाहर न जाएँ।",
            "बाहर जाना पड़े तो N95 मास्क पहनें और समय कम रखें।",
            "खिड़कियाँ और दरवाज़े बंद रखें।",
            "जिस कमरे में सबसे ज़्यादा रहते हैं, वहाँ एयर प्यूरीफायर चलाएँ।",
            "बाहरी कसरत और खेल पूरी तरह बंद रखें।",
        ],
        "children": "बच्चे घर के अंदर रहें; स्कूल की बाहरी गतिविधि न करें।",
        "older_adults": "बड़े-बुज़ुर्ग बाहर न जाएँ; ज़रूरी दवाएँ पास रखें।",
        "sensitive_groups": "अस्थमा, दिल या फेफड़े की बीमारी वाले लोग और गर्भवती महिलाएँ घर के अंदर रहें; साँस लेने में तकलीफ़ हो तो डॉक्टर से मिलें।",
    },
    "Severe": {
        "headline": "आज हवा गंभीर रूप से प्रदूषित है।",
        "message": "आज का सूचकांक {aqi} है और श्रेणी गंभीर है। यह आपात स्तर है; घर के अंदर रहें।",
        "do": [
            "घर के अंदर रहें और बाहर की सारी गतिविधि बंद रखें।",
            "खिड़कियाँ और दरवाज़े बंद रखें; धुआँ आए तो दरारें बंद करें।",
            "बाहर जाना ज़रूरी हो तो N95 मास्क पहनें और समय बहुत कम रखें।",
            "सीने में दर्द, घरघराहट या साँस लेने में भारी तकलीफ़ हो तो तुरंत डॉक्टर को बुलाएँ।",
        ],
        "children": "बच्चे घर के अंदर रहें, खिड़कियों और धुएँ से दूर।",
        "older_adults": "बड़े-बुज़ुर्ग घर के अंदर रहें और कोई मेहनत न करें।",
        "sensitive_groups": "अस्थमा, दिल या फेफड़े की बीमारी वाले लोग और गर्भवती महिलाएँ घर के अंदर रहें और साँस की तकलीफ़ के पहले संकेत पर डॉक्टर से मिलें।",
    },
    "Data unavailable": {
        "headline": "आज पर्याप्त वैध रीडिंग नहीं मिलीं।",
        "message": "मिली रीडिंग से आज का सूचकांक नहीं निकाला जा सका। हवा को अनिश्चित मानें।",
        "do": [
            "श्रेणी पता चलने तक बाहरी गतिविधि कम रखें।",
            "वार्ड कार्यालय से स्टेशन जाँचने को कहें।",
            "व्यस्त सड़कों के पास खिड़कियाँ बंद रखें।",
        ],
        "children": "श्रेणी पक्की होने तक बच्चों का बाहरी खेल छोटा रखें।",
        "older_adults": "डेटा पक्का होने तक बड़े-बुज़ुर्ग बाहर की यात्राएँ छोटी रखें।",
        "sensitive_groups": "अस्थमा या दिल-फेफड़े की बीमारी वाले लोग दवा साथ रखें और व्यस्त सड़कों से बचें।",
    },
}

_ADVISORY_TEMPLATES: dict[str, dict[str, dict[str, Any]]] = {
    "en": _ADVISORY_EN,
    "hi": _ADVISORY_HI,
}

_LANG_CODE = {
    "english": "en",
    "hindi": "hi",
    "marathi": "mr",
    "kannada": "kn",
    "tamil": "ta",
    "bengali": "bn",
    "telugu": "te",
    "gujarati": "gu",
}


def advisory_fallback(
    brief: Brief,
    language: str,
    reading_level: str,
    area_label: str,
    station: str | None = None,
) -> Advisory:
    """Citizen advisory from category templates, English or Hindi text.

    City scope speaks about the worst station and states the category range across the city.
    With `station`, it speaks about that one station/ward only. Care blocks use the evening
    peak window from a pattern finding when the data has one.
    """
    snapshot = brief_snapshot(brief, station)
    category = snapshot["category"] if snapshot["category"] in CATEGORIES else "Data unavailable"
    lang_code = _LANG_CODE.get((language or "English").strip().lower(), "en")
    supported = lang_code in _ADVISORY_TEMPLATES
    template_lang = lang_code if supported else "en"
    template = _ADVISORY_TEMPLATES[template_lang].get(category) or _ADVISORY_TEMPLATES[template_lang]["Data unavailable"]

    aqi_text = _num(round(snapshot["aqi"])) if snapshot["aqi"] is not None else "not available"
    level = (reading_level or "standard").strip().lower()
    cite = _cite_for_scope(brief, snapshot)
    message = _meaning_line(snapshot, category, aqi_text, template_lang, cite)
    if level != "simple":
        advice = _advice_line(template, aqi_text)
        if advice:
            message = f"{message} {advice}"
    actions = list(template["do"])
    if level == "simple":
        actions = actions[:3]
    elif level == "technical":
        detail = _technical_detail(brief, snapshot, template_lang)
        if detail:
            message = f"{message} {detail}"

    area = (area_label or snapshot["area"]).strip()
    headline = template["headline"]
    if snapshot["scope"] == "station" and snapshot["station"]:
        headline = f"{snapshot['station']}: {headline[0].lower()}{headline[1:]}"
    elif area and template_lang == "en" and category != "Data unavailable":
        headline = f"{area}: {headline[0].lower()}{headline[1:]}"

    source_line = _source_line(brief, snapshot, template_lang)
    if not supported:
        source_line += " Translation unavailable: English text shown."
    window = snapshot["peak_window"]

    return Advisory(
        language=language or "English",
        reading_level=level,
        headline=headline,
        category=category,
        today_message=message,
        what_to_do=actions,
        care=CareBlock(
            children=_care_with_peak(template["children"], window, template_lang),
            older_adults=_care_with_peak(template["older_adults"], window, template_lang),
            sensitive_groups=_care_with_peak(template["sensitive_groups"], window, template_lang),
        ),
        source_line=source_line,
        cites=[finding.id for finding in _ordered_findings(brief)[:3]],
        used_fallback=True,
    )


def _meaning_line(
    snapshot: dict[str, Any],
    category: str,
    aqi_text: str,
    lang: str,
    cite: str,
) -> str:
    """'What this means for you today' line, with the AQI number and the code category."""
    numbered = f"{aqi_text} [{cite}]" if cite else aqi_text
    station = snapshot.get("station")
    if lang == "hi":
        where = f" {station} पर" if station else ""
        line = f"आज आपके लिए इसका मतलब: सूचकांक {numbered}, श्रेणी {category}{where} है।"
        if snapshot["scope"] == "city" and snapshot.get("range_text"):
            line += (
                f" शहर में श्रेणी {snapshot.get('best_category')} ({snapshot.get('best_station')}) से "
                f"{snapshot.get('worst_category')} ({snapshot.get('worst_station')}) तक है।"
            )
        return line
    where = f" at {station}" if station else ""
    line = f"What this means for you today: AQI {numbered}, {category}{where}."
    if snapshot["scope"] == "city" and snapshot.get("range_text"):
        line += f" Across the city the category {snapshot['range_text']}."
    return line


def _advice_line(template: dict[str, Any], aqi_text: str) -> str:
    """Second sentence of the category template: the advice, without repeating the AQI."""
    parts = re.split(r"(?<=\.)\s+", fill_template(template["message"], aqi=aqi_text), maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _care_with_peak(text: str, window: str | None, lang: str) -> str:
    """Append the data's peak window to one care block, when a pattern finding exists."""
    body = (text or "").strip()
    if not window or not body:
        return body
    if lang == "hi":
        return f"{body} {window} के बीच बाहर जाना टालें।"
    if "-" in window:
        return f"{body} Avoid outdoor time between {window.replace('-', ' and ')}."
    return f"{body} Avoid outdoor time around {window}."


def _technical_detail(brief: Brief, snapshot: dict[str, Any], lang: str) -> str:
    """Pollutant and per-station numbers for the technical reading level."""
    parts: list[str] = []
    if snapshot["dominant"]:
        label = "Lead pollutant" if lang == "en" else "मुख्य प्रदूषक"
        parts.append(f"{label}: {snapshot['dominant']}.")
    rows = [
        (station.station, float(station.aqi))
        for station in brief.station_aqi
        if station.station and _finite(station.aqi)
    ]
    if rows:
        label = "Station AQI" if lang == "en" else "स्टेशन AQI"
        listing = ", ".join(f"{name} {_num(value)}" for name, value in rows[:5])
        parts.append(f"{label}: {listing}.")
    return " ".join(parts)


def _source_line(brief: Brief, snapshot: dict[str, Any], lang: str) -> str:
    """Date, stations used and the data-gap note, in the advisory language."""
    date = snapshot["date"]
    stations = ", ".join(snapshot["stations"]) or "station names not recorded"
    gaps = snapshot["gaps"]
    if lang == "hi":
        gap_note = (
            f"डेटा में कमी: {gaps[0].strip().rstrip('.')}।"
            if gaps
            else "डेटा में कोई बड़ी कमी दर्ज नहीं हुई।"
        )
        return (
            f"स्रोत: {date}। स्टेशन: {stations}। {gap_note} "
            "सूचकांक CPCB राष्ट्रीय AQI पैमाने पर है। यह सामान्य मार्गदर्शन है, चिकित्सा सलाह नहीं।"
        )
    gap_note = (
        f"Data gap: {gaps[0].strip().rstrip('.')}."
        if gaps
        else "No major data gaps were reported."
    )
    return (
        f"Source: {date}. Stations: {stations}. {gap_note} "
        "AQI uses the CPCB National AQI scale. General guidance, not medical advice."
    )
