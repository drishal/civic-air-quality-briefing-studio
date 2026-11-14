"""Pure HTML builders for the briefing studio.

Every function returns a string of HTML and never touches Streamlit, so the components are
cheap to unit test. All dynamic text goes through ``esc()``; only numbers the module formats
itself are inserted raw.
"""

from __future__ import annotations

import html
import math
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

# Unit notes, not data faults: they are shown apart from the flags that damage an AQI.
INFORMATIONAL_FLAGS = ("UNIT_AMBIGUOUS", "UNIT_CONVERTED")

_CITE_RE = re.compile(r"\[(F\d{1,3})\]")
_RELIABILITY_TONE = {"reliable": "ok", "use with caution": "warn", "unreliable": "danger"}
_CONFIDENCE_TONE = {"high": "ok", "medium": "warn", "low": "danger"}


# ------------------------------------------------------------------ primitives
def esc(value: Any) -> str:
    """HTML-escape any value; ``None`` becomes an empty string."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def num(value: Any, digits: int = 0, dash: str = "—") -> str:
    """Format a number with tabular grouping; ``None``/NaN/∞ become a dash."""
    if value is None:
        return dash
    try:
        number = float(value)
    except (TypeError, ValueError):
        return esc(value)
    if not math.isfinite(number):
        return dash
    return f"{number:,.{digits}f}"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a pydantic model or a plain dict."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _findings_map(findings: Any) -> dict[str, Any]:
    """Index findings by id; accepts a sequence of models/dicts or a mapping."""
    if not findings:
        return {}
    if isinstance(findings, Mapping):
        return dict(findings)
    return {str(_attr(f, "id", "")): f for f in findings if _attr(f, "id")}


def chip(label: Any, tone: str = "", icon: str = "") -> str:
    """Small pill. ``tone`` is one of ok/warn/danger/info/accent/hollow or ''."""
    classes = "pw-chip" + (f" pw-chip--{tone}" if tone else "")
    icon_html = f'<span aria-hidden="true">{esc(icon)}</span>' if icon else ""
    return f'<span class="{classes}">{icon_html}{esc(label)}</span>'


def dot_chip(label: Any, tone: str = "") -> str:
    """Pill with a leading colour dot: colour is never the only signal."""
    classes = "pw-chip" + (f" pw-chip--{tone}" if tone else "")
    return f'<span class="{classes}"><span class="pw-chip__dot" aria-hidden="true"></span>{esc(label)}</span>'


def cat_chip(
    category: Optional[str],
    label: Optional[str] = None,
    color: Optional[str] = None,
    emoji: str = "",
) -> str:
    """Solid CPCB category pill; the colour comes from the caller (core.aqi.category_color)."""
    if not category:
        return chip("no valid AQI", "hollow")
    fill = color or "#9aa0a6"
    ink = _text_on(fill)
    prefix = f'<span aria-hidden="true">{esc(emoji)}</span>' if emoji else ""
    return (
        f'<span class="pw-chip pw-chip--solid" style="--cat-color:{esc(fill)};background:{esc(fill)};'
        f'color:{ink}">{prefix}{esc(label or category)}</span>'
    )


def _text_on(hex_color: str) -> str:
    """Readable ink for a solid background (dark ink on light fills, white on dark)."""
    raw = str(hex_color or "").lstrip("#")
    try:
        red, green, blue = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return "#ffffff"
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "#111611" if luminance > 0.62 else "#ffffff"


def vitals(items: Sequence[tuple[str, str]]) -> str:
    """Chip row of session vitals: ``[(label, tone), ...]``."""
    if not items:
        return ""
    chips = "".join(chip(label, tone) for label, tone in items)
    return f'<div class="pw-vitals">{chips}</div>'


# ------------------------------------------------------------------ citations
def cite_html(text: Any, findings: Any = None, empty: str = "") -> str:
    """Escape text, then turn every ``[F#]`` into a chip whose tooltip names the finding."""
    if not text:
        return empty
    return _chipify(esc(text), _findings_map(findings))

def _chipify(safe: str, index: Mapping[str, Any]) -> str:
    """Replace [F#] in already-escaped text with citation chips."""

    def _replace(match: re.Match[str]) -> str:
        fid = match.group(1)
        finding = index.get(fid)
        if finding is None:
            return (
                f'<span class="pw-cite pw-cite--unknown" title="{esc(fid)} — not a finding in this brief">'
                f"[{esc(fid)}]</span>"
            )
        title = _attr(finding, "title", "")
        confidence = _attr(finding, "confidence", "")
        tooltip = f"{fid} · {title} (confidence: {confidence})"
        return f'<span class="pw-cite" title="{esc(tooltip)}">[{esc(fid)}]</span>'

    return _CITE_RE.sub(_replace, safe)


def markdown_html(text: Any, findings: Any = None) -> str:
    """Escape first, then render the small Markdown subset the note uses (headings, lists, bold, code).

    Generated text is never trusted as HTML: the only tags that reach the page are the ones this
    function adds.
    """
    if not text:
        return ""
    index = _findings_map(findings)
    blocks: list[str] = []
    items: list[str] = []

    def _flush() -> None:
        if items:
            blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
            items.clear()

    for raw in str(text).splitlines():
        safe = _chipify(esc(raw.rstrip()), index)
        plain = safe.strip()
        if not plain:
            _flush()
            continue
        if plain.startswith(("- ", "* ", "• ")):
            items.append(_inline(plain[2:].strip()))
            continue
        _flush()
        heading = re.match(r"^(#{1,6})\s+(.*)$", plain)
        if heading:
            level = min(len(heading.group(1)) + 2, 6)
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", plain):
            blocks.append("<hr/>")
        else:
            blocks.append(f"<p>{_inline(plain)}</p>")
    _flush()
    return "".join(blocks)


def _inline(safe: str) -> str:
    """Bold and inline code on already-escaped text (citations are already chips)."""
    bold = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", bold)



def cite_ids(ids: Iterable[str], findings: Any) -> list[str]:
    """Keep only the ids that exist in ``findings``, de-duplicated, order preserved."""
    index = _findings_map(findings)
    seen: list[str] = []
    for raw in ids or []:
        fid = str(raw).strip().strip("[] ")
        if fid and fid in index and fid not in seen:
            seen.append(fid)
    return seen


def sources_row(findings: Any, ids: Optional[Iterable[str]] = None, label: str = "Sources", limit: int = 8) -> str:
    """Collapsed-by-default caption listing the findings cited, with their evidence count."""
    index = _findings_map(findings)
    wanted = list(ids) if ids is not None else list(index.keys())
    chips: list[str] = []
    for raw in wanted[:limit]:
        fid = str(raw).strip().strip("[] ")
        finding = index.get(fid)
        if finding is None:
            continue
        title = _attr(finding, "title", "")
        confidence = _attr(finding, "confidence", "")
        evidence = _attr(finding, "evidence_row_ids", []) or []
        tooltip = f"{fid} · {title} · {confidence} confidence · {len(evidence)} evidence rows"
        chips.append(f'<span class="pw-cite" title="{esc(tooltip)}">{esc(fid)}</span>')
    if not chips:
        return ""
    return (
        f'<div class="pw-sources"><span class="pw-sources__label">{esc(label)}</span>'
        + "".join(chips)
        + "</div>"
    )


# ------------------------------------------------------------------ sparkline + ribbon
def sparkline(
    values: Sequence[Optional[float]],
    color: str = "var(--accent)",
    width: int = 240,
    height: int = 30,
    label: str = "daily AQI",
) -> str:
    """Inline SVG sparkline of daily AQI; gaps are drawn as gaps, not as zeroes."""
    points: list[tuple[float, float]] = []
    clean = [v for v in values if v is not None and _finite(v)]
    if len(clean) < 2:
        return ""
    low, high = min(clean), max(clean)
    span = high - low or 1.0
    pad = 3.0
    usable_w = max(width - 2 * pad, 1)
    usable_h = max(height - 2 * pad, 1)
    step = usable_w / max(len(values) - 1, 1)
    for i, value in enumerate(values):
        if value is None or not _finite(value):
            continue
        x = pad + i * step
        y = pad + usable_h - (float(value) - low) / span * usable_h
        points.append((round(x, 2), round(y, 2)))
    if len(points) < 2:
        return ""
    line = " ".join(f"{x},{y}" for x, y in points)
    area = f"{points[0][0]},{height - pad} {line} {points[-1][0]},{height - pad}"
    return (
        f'<svg class="pw-card__spark" viewBox="0 0 {int(width)} {int(height)}" preserveAspectRatio="none" '
        f'role="img" aria-label="{esc(label)}" style="color:{esc(color)}">'
        f'<polygon points="{area}" fill="currentColor" opacity=".12"/>'
        f'<polyline points="{line}" fill="none" stroke="currentColor" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        "</svg>"
    )


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def quality_ribbon(
    segments: Sequence[Mapping[str, Any]],
    compact: bool = False,
    legend: bool = True,
    height: Optional[int] = None,
) -> str:
    """Segmented data-quality strip. Each segment: ``{kind, weight, label}``.

    ``kind`` is one of valid / missing / flagged / stuck. Weights are normalised to 100%.
    """
    usable = [s for s in segments if _finite(s.get("weight")) and float(s["weight"]) > 0]
    total = sum(float(s["weight"]) for s in usable)
    if not usable or total <= 0:
        return ""
    classes = "pw-ribbon pw-ribbon--compact" if compact else "pw-ribbon"
    style = f' style="height:{int(height)}px"' if height else ""
    parts: list[str] = []
    for segment in usable:
        kind = str(segment.get("kind") or "valid")
        share = float(segment["weight"]) / total * 100.0
        label = segment.get("label") or kind
        parts.append(
            f'<span class="pw-ribbon__seg pw-ribbon__seg--{esc(kind)}" '
            f'style="flex:{share:.4f} 0 0" title="{esc(label)}"></span>'
        )
    strip = f'<div class="{classes}"{style}>{"".join(parts)}</div>'
    if compact or not legend:
        return strip
    totals: dict[str, float] = {}
    for segment in usable:
        kind = str(segment.get("kind") or "valid")
        totals[kind] = totals.get(kind, 0.0) + float(segment["weight"])
    names = {"valid": "valid", "missing": "no data", "flagged": "flagged", "stuck": "stuck"}
    legend_items = "".join(
        f'<span><i class="pw-ribbon__seg--{esc(kind)}"></i>'
        f"{esc(names.get(kind, kind))} {num(weight)} h ({weight / total:.0%})</span>"
        for kind, weight in sorted(totals.items(), key=lambda kv: -kv[1])
    )
    return strip + f'<div class="pw-ribbon-legend">{legend_items}</div>'


# ------------------------------------------------------------------ hero + cards
def aqi_hero(
    *,
    area: Any,
    aqi: Optional[float],
    category: Optional[str],
    color: Optional[str],
    dominant: Optional[str] = None,
    dominant_value: Optional[float] = None,
    dominant_unit: str = "",
    who_multiple: Optional[float] = None,
    confidence: Optional[str] = None,
    n_stations: Optional[int] = None,
    period: str = "",
    label: str = "area AQI",
    notes: Sequence[str] = (),
) -> str:
    """Headline AQI tile: the one display-size number on the page."""
    tint = color or "#9aa0a6"
    value = num(aqi)
    facts: list[str] = []
    if dominant:
        amount = ""
        if dominant_value is not None and _finite(dominant_value):
            amount = f" at <b>{num(dominant_value, 1)}</b> {esc(dominant_unit)}"
        facts.append(f"driven by <b>{esc(dominant)}</b>{amount}")
    if who_multiple is not None and _finite(who_multiple):
        facts.append(f"<b>{num(who_multiple, 1)}×</b> the WHO 24-hour guideline")
    chips: list[str] = [chip(note) for note in notes if str(note).strip()]
    if period:
        chips.append(chip(f"period {period}"))
    if n_stations:
        chips.append(chip(f"{n_stations} station{'s' if n_stations != 1 else ''}"))
    if confidence:
        chips.append(dot_chip(f"confidence: {confidence}", _CONFIDENCE_TONE.get(str(confidence), "warn")))
    return (
        f'<div class="pw-hero" style="--cat-color:{esc(tint)}">'
        '<div class="pw-hero__left">'
        f'<span class="pw-hero__value">{value}</span>'
        f'<div class="pw-hero__unit">{esc(label)}</div>'
        "</div>"
        '<div class="pw-hero__right">'
        f'<div class="pw-hero__cat">{esc(category or "no valid AQI")}</div>'
        f'<p class="pw-hero__facts">{" · ".join(facts) if facts else esc(area)}</p>'
        f'<div class="pw-hero__meta">{"".join(chips)}</div>'
        "</div>"
        "</div>"
    )


def trend_text(trend_per_day: Optional[float], unit: str = "AQI/day") -> tuple[str, str]:
    """Human trend phrase plus a tone: ('worsening +12 AQI/day', 'worse')."""
    if trend_per_day is None or not _finite(trend_per_day):
        return ("no trend", "")
    slope = float(trend_per_day)
    if abs(slope) < 1:
        return ("steady", "")
    direction = "worsening" if slope > 0 else "improving"
    sign = "+" if slope > 0 else "−"
    return (f"{direction} {sign}{abs(slope):.0f} {unit}", "worse" if slope > 0 else "better")


def station_card(
    *,
    station: Any,
    ward: Optional[str] = None,
    aqi: Optional[float] = None,
    category: Optional[str] = None,
    color: Optional[str] = None,
    reliability_label: Optional[str] = None,
    trend_per_day: Optional[float] = None,
    spark: str = "",
    dominant: Optional[str] = None,
    confidence: Optional[str] = None,
    ribbon: str = "",
    emoji: str = "",
) -> str:
    """One station tile: category edge, AQI, sparkline, reliability pill, trend in words."""
    phrase, trend_class = trend_text(trend_per_day)
    pills: list[str] = []
    if category:
        pills.append(cat_chip(category, category, color, emoji))
    if reliability_label:
        pills.append(dot_chip(reliability_label, _RELIABILITY_TONE.get(str(reliability_label), "warn")))
    if dominant:
        pills.append(chip(f"dominant {dominant}"))
    trend_html = (
        f'<span class="pw-card__trend pw-card__trend--{trend_class}">'
        f'{"▲" if trend_class == "worse" else "▼" if trend_class == "better" else "▬"} {esc(phrase)}</span>'
    )
    confidence_html = (
        f'<span class="pw-card__trend">{esc(confidence)} confidence</span>' if confidence else ""
    )
    return (
        f'<div class="pw-card" style="--cat-color:{esc(color or "#9aa0a6")}">'
        '<div class="pw-card__top">'
        f'<div><div class="pw-card__name">{esc(station)}</div>'
        f'<div class="pw-card__ward">{esc(ward or "ward not given")}</div></div>'
        f'<div class="pw-card__aqi">{num(aqi)}</div>'
        "</div>"
        f"{spark}"
        f"{ribbon}"
        f'<div class="pw-card__row">{trend_html}{confidence_html}</div>'
        f'<div class="pw-card__pills">{"".join(pills)}</div>'
        "</div>"
    )


def card_grid(cards: Sequence[str]) -> str:
    """Wrap station cards in the responsive grid; empty input returns ''."""
    body = "".join(c for c in cards if c)
    return f'<div class="pw-cards">{body}</div>' if body else ""


# ------------------------------------------------------------------ actions
def action_card(
    *,
    rank: Any,
    action: Any,
    effort: Optional[str] = None,
    owner: Optional[str] = None,
    time_window: Optional[str] = None,
    why_now_html: str = "",
    cites_html: str = "",
    data_gap_action: Optional[str] = None,
    library_id: Optional[str] = None,
) -> str:
    """Flat action card: rank badge, effort/owner/window chips, cited why-now, gap strip."""
    effort_key = str(effort or "").strip().lower()
    effort_tone = "ok" if effort_key.startswith("low") else "warn" if effort_key.startswith("medium") else "danger" if effort_key else ""
    meta: list[str] = []
    if effort:
        meta.append(chip(f"effort: {effort}", effort_tone))
    if owner:
        meta.append(chip(f"owner: {owner}"))
    if time_window:
        meta.append(chip(f"when: {time_window}"))
    gap = (
        f'<div class="pw-action__gap">⚠️ Verify first: {esc(data_gap_action)}</div>' if data_gap_action else ""
    )
    lib = f'<div class="pw-action__id">{esc(library_id)}</div>' if library_id else ""
    return (
        '<div class="pw-action">'
        f'<div class="pw-action__rank">{esc(rank)}</div>'
        f'<div class="pw-action__title">{esc(action)}</div>'
        f'<div class="pw-action__meta">{"".join(meta)}</div>'
        f'<p class="pw-action__why"><b>Why now:</b> {why_now_html}</p>'
        f"{cites_html}{gap}{lib}"
        "</div>"
    )


# ------------------------------------------------------------------ advisory
def advisory_card(
    *,
    headline: Any,
    category: Optional[str],
    color: Optional[str] = None,
    language: Optional[str] = None,
    level: Optional[str] = None,
    category_emoji: str = "",
    today_html: str = "",
    what_to_do: Sequence[str] = (),
    care: Optional[Mapping[str, Any]] = None,
    source_line: Optional[str] = None,
    cites_html: str = "",
    used_fallback: bool = False,
    print_class: bool = True,
) -> str:
    """Citizen advisory card: category-coloured header, care blocks, print-ready wrapper."""
    tint = color or "#9aa0a6"
    head_chips: list[str] = []
    if language or level:
        head_chips.append(chip(" · ".join(x for x in (str(language or ""), str(level or "")) if x)))
    if used_fallback:
        head_chips.append(chip("offline fallback text", "warn"))
    head_cat = cat_chip(category, category, color, category_emoji)
    steps = "".join(f"<li>{esc(item)}</li>" for item in what_to_do if str(item).strip())
    care_html = ""
    if care:
        blocks = [
            ("children", "🧒 Children", care.get("children")),
            ("older adults", "🧓 Older adults", care.get("older_adults")),
            ("sensitive groups", "🫁 Sensitive groups", care.get("sensitive_groups")),
        ]
        rendered = "".join(
            f'<div class="pw-care__block"><div class="pw-care__title">{esc(title)}</div>'
            f'<div class="pw-care__text">{esc(text)}</div></div>'
            for _, title, text in blocks
            if str(text or "").strip()
        )
        if rendered:
            care_html = f'<div class="pw-care">{rendered}</div>'
    source = f'<div class="pw-advisory__source">{esc(source_line)}</div>' if source_line else ""
    simple = " pw-advisory--simple" if str(level or "").lower() == "simple" else ""
    print_cls = " pw-print" if print_class else ""
    return (
        f'<div class="pw-advisory{simple}{print_cls}" style="--cat-color:{esc(tint)}">'
        f'<div class="pw-advisory__head">{head_cat}<h3 class="pw-advisory__headline">{esc(headline)}</h3>'
        f'{"".join(head_chips)}</div>'
        '<div class="pw-advisory__body">'
        f'<p class="pw-advisory__message">{today_html}</p>'
        + (f'<ul class="pw-advisory__list">{steps}</ul>' if steps else "")
        + care_html
        + f"{cites_html}{source}"
        '<div class="pw-advisory__care-note">Not medical advice. Set activity thresholds with your '
        "medical officer.</div>"
        "</div></div>"
    )


# ------------------------------------------------------------------ what changed
def delta_card(
    *,
    name: Any,
    delta: Optional[float],
    direction: str = "same",
    before: Optional[str] = None,
    after: Optional[str] = None,
    color_before: Optional[str] = None,
    color_after: Optional[str] = None,
) -> str:
    """One station delta: arrow + colour + signed number, never colour alone."""
    arrow = {"worse": "▲", "better": "▼", "new": "＋", "dropped": "－"}.get(direction, "＝")
    sign = "" if delta is None else ("+" if float(delta) > 0 else "−" if float(delta) < 0 else "±")
    amount = "" if delta is None else f"{sign}{num(abs(float(delta)), 1)}"
    transition = ""
    if before or after:
        left = cat_chip(before, before, color_before) if before else chip("n/a", "hollow")
        right = cat_chip(after, after, color_after) if after else chip("dropped", "hollow")
        transition = f'<span class="pw-transition">{left}<span class="pw-transition__arrow">→</span>{right}</span>'
    return (
        f'<div class="pw-delta pw-delta--{esc(direction)}">'
        f'<div><div class="pw-delta__name">{esc(name)}</div>{transition}</div>'
        f'<div class="pw-delta__num">{arrow} {amount or "—"}</div>'
        "</div>"
    )


def delta_grid(cards: Sequence[str]) -> str:
    body = "".join(c for c in cards if c)
    return f'<div class="pw-deltas">{body}</div>' if body else ""


def transition_pill(before: Optional[str], after: Optional[str], color_before: Optional[str], color_after: Optional[str]) -> str:
    """'Poor → Very Poor' pill pair. Falls back to plain text while a colour is missing."""
    left = cat_chip(before, before, color_before) if before else chip("n/a", "hollow")
    right = cat_chip(after, after, color_after) if after else chip("n/a", "hollow")
    return f'<span class="pw-transition">{left}<span class="pw-transition__arrow">→</span>{right}</span>'


# ------------------------------------------------------------------ empty / loading / start
def empty_state(title: Any, body: Any = "", next_action: Any = "", glyph: str = "◌") -> str:
    """Calm empty state that always names the next action."""
    next_html = f'<div class="pw-empty__next">→ {esc(next_action)}</div>' if next_action else ""
    body_html = f'<p class="pw-empty__body">{esc(body)}</p>' if body else ""
    return (
        '<div class="pw-empty">'
        f'<span class="pw-empty__glyph" aria-hidden="true">{esc(glyph)}</span>'
        f'<p class="pw-empty__title">{esc(title)}</p>{body_html}{next_html}'
        "</div>"
    )


def skeleton(lines: int = 3, label: str = "Working…") -> str:
    """Shimmer skeleton shown while a generative stage runs (static under reduced motion)."""
    count = max(1, min(int(lines), 6))
    bars = "".join('<div class="pw-skeleton__line"></div>' for _ in range(count))
    return (
        f'<div class="pw-skeleton" role="status" aria-label="{esc(label)}">{bars}'
        f'<div class="pw-skeleton__label">{esc(label)}</div></div>'
    )


def wash() -> str:
    """Slow 40 s accent wash for the start screen only."""
    return '<div class="pw-wash" aria-hidden="true"></div>'


def wordmark(title: str = "Air Briefing Studio", subtitle: str = "") -> str:
    sub = f'<div class="pw-wordmark__sub">{esc(subtitle)}</div>' if subtitle else ""
    return (
        '<div><div class="pw-wordmark"><span class="pw-wordmark__mark" aria-hidden="true"></span>'
        f"{esc(title)}</div>{sub}</div>"
    )


def start_card(step: Any, title: Any, body: Any) -> str:
    """One start-screen card: a step label, a title and one sentence."""
    return (
        '<div class="pw-start__card">'
        f'<div class="pw-start__step">{esc(step)}</div>'
        f'<div class="pw-start__title">{esc(title)}</div>'
        f'<p class="pw-start__body">{esc(body)}</p>'
        "</div>"
    )


def start_grid(cards: Sequence[str]) -> str:
    body = "".join(c for c in cards if c)
    return f'<div class="pw-start">{body}</div>' if body else ""


def section(title: Any, hint: Any = "") -> str:
    """Small section header with an optional inline hint."""
    hint_html = f'<span class="pw-section__hint">{esc(hint)}</span>' if hint else ""
    return f'<div class="pw-section"><span class="pw-section__title">{esc(title)}</span>{hint_html}</div>'


def small(text: Any) -> str:
    return f'<span class="pw-small">{esc(text)}</span>'
