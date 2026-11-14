"""Unit tests for the pure HTML component builders. No Streamlit, no network."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from core.models import Finding
from ui import components as c
from ui import theme

ROOT = Path(__file__).resolve().parents[1]


def _finding(fid: str = "F1", title: str = "Hotspot: Shivajinagar", confidence: str = "high") -> Finding:
    return Finding(
        id=fid,
        type="hotspot",
        title=title,
        facts={"aqi": 214.0},
        formula="max station AQI",
        evidence_row_ids=["R12", "R44"],
        confidence=confidence,
    )


# ------------------------------------------------------------------ escaping
def test_esc_escapes_tags_and_quotes():
    assert c.esc("<script>alert('x')</script>") == "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;"
    assert c.esc(None) == ""
    assert c.esc('a "b"') == "a &quot;b&quot;"


def test_no_unescaped_dynamic_text_reaches_html():
    evil = '<img src=x onerror="alert(1)">'
    assert "<img" not in c.chip(evil)
    assert "<img" not in c.empty_state(evil, evil, evil)
    assert "<img" not in c.action_card(rank=1, action=evil, owner=evil, why_now_html="ok")
    assert "<img" not in c.station_card(station=evil, ward=evil)
    assert "<img" not in c.start_card(evil, evil, evil)
    assert "<img" not in c.section(evil, evil)


def test_num_formats_and_dashes():
    assert c.num(1234.567, 1) == "1,234.6"
    assert c.num(None) == "—"
    assert c.num(float("nan")) == "—"
    assert c.num(float("inf")) == "—"
    assert c.num("12") == "12"


# ------------------------------------------------------------------ citations
def test_cite_chips_carry_title_and_confidence():
    html = c.cite_html("Shivajinagar is worst [F1].", [_finding()])
    assert '<span class="pw-cite" title="F1 · Hotspot: Shivajinagar (confidence: high)">[F1]</span>' in html
    assert html.startswith("Shivajinagar is worst ")


def test_unknown_citation_is_marked_not_dropped():
    html = c.cite_html("Made up [F9].", [_finding("F1")])
    assert "pw-cite--unknown" in html
    assert "[F9]" in html


def test_cite_html_escapes_before_chipping():
    html = c.cite_html('<b>x</b> [F1]', [_finding()])
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "pw-cite" in html


def test_sources_row_lists_cited_findings_with_evidence_count():
    findings = [_finding("F1"), _finding("F2", "Spike at Bhosari", "medium")]
    html = c.sources_row(findings, ["F1", "F2", "F7"])
    assert "pw-sources" in html
    assert html.count("pw-cite") == 2
    assert "2 evidence rows" in html
    assert "Spike at Bhosari" in html
    assert "F7" not in html
    assert c.sources_row([], ["F1"]) == ""


def test_cite_ids_filters_and_dedupes():
    assert c.cite_ids(["F1", "[F2]", "F1", "F9"], [_finding("F1"), _finding("F2")]) == ["F1", "F2"]


# ------------------------------------------------------------------ sparkline
def test_sparkline_needs_two_points():
    assert c.sparkline([None, None]) == ""
    assert c.sparkline([12.0]) == ""
    html = c.sparkline([10, 40, None, 80])
    assert "<svg" in html and "polyline" in html
    assert "None" not in html


def test_sparkline_is_flat_safe_and_escapes_label():
    html = c.sparkline([50, 50, 50], label='a "chart"')
    assert html.count(",") > 0
    assert "&quot;chart&quot;" in html
    assert 'preserveAspectRatio="none"' in html


# ------------------------------------------------------------------ ribbon
def test_ribbon_shares_normalise_to_100():
    html = c.quality_ribbon(
        [
            {"kind": "valid", "weight": 60, "label": "valid 60 h"},
            {"kind": "missing", "weight": 30, "label": "no data 30 h"},
            {"kind": "flagged", "weight": 10, "label": "flagged 10 h"},
        ]
    )
    shares = [float(m) for m in re.findall(r"flex:([\d.]+) 0 0", html)]
    assert len(shares) == 3
    assert abs(sum(shares) - 100) < 0.01
    assert 'title="valid 60 h"' in html
    assert "pw-ribbon-legend" in html


def test_ribbon_compact_has_no_legend_and_skips_zero_weights():
    html = c.quality_ribbon(
        [{"kind": "valid", "weight": 1, "label": "valid"}, {"kind": "stuck", "weight": 0, "label": "stuck"}],
        compact=True,
    )
    assert "pw-ribbon--compact" in html
    assert "pw-ribbon-legend" not in html
    assert "stuck" not in html
    assert c.quality_ribbon([]) == ""


# ------------------------------------------------------------------ hero + cards
def test_hero_shows_value_category_and_facts():
    html = c.aqi_hero(
        area="Pune",
        aqi=214,
        category="Poor",
        color="#F26522",
        dominant="PM2.5",
        dominant_value=88.4,
        dominant_unit="µg/m³",
        who_multiple=5.9,
        confidence="medium",
        n_stations=7,
        period="2026-11-01 → 2026-11-03",
    )
    assert "--cat-color:#F26522" in html
    assert ">214<" in html
    assert "Poor" in html
    assert "PM2.5" in html
    assert "5.9×" in html
    assert "7 stations" in html
    assert "confidence: medium" in html


def test_hero_shows_extra_note_chips():
    html = c.aqi_hero(
        area="Pune", aqi=338, category="Very Poor", color="#D0021B",
        notes=["area mean 196 · Moderately Polluted"], period="2026-11-08 → 2026-11-10",
    )
    assert "area mean 196 · Moderately Polluted" in html
    assert "period 2026-11-08 → 2026-11-10" in html


def test_hero_without_data_says_so():
    html = c.aqi_hero(area="Pune", aqi=None, category=None, color=None, period="")
    assert "no valid AQI" in html
    assert ">—<" in html


def test_station_card_has_edge_spark_and_trend_words():
    spark = c.sparkline([120, 160, 200])
    html = c.station_card(
        station="Shivajinagar",
        ward="Ward 8",
        aqi=214.4,
        category="Poor",
        color="#F26522",
        reliability_label="use with caution",
        trend_per_day=12.4,
        spark=spark,
        dominant="PM2.5",
        confidence="medium",
    )
    assert '--cat-color:#F26522' in html
    assert "Shivajinagar" in html
    assert "Ward 8" in html
    assert ">214<" in html
    assert "<svg" in html
    assert "worsening +12 AQI/day" in html
    assert "use with caution" in html
    assert "medium confidence" in html


def test_trend_text_directions():
    assert c.trend_text(12.4) == ("worsening +12 AQI/day", "worse")
    assert c.trend_text(-3.2) == ("improving −3 AQI/day", "better")
    assert c.trend_text(0.4) == ("steady", "")
    assert c.trend_text(None) == ("no trend", "")


# ------------------------------------------------------------------ actions + advisory
def test_action_card_fields_and_gap_strip():
    html = c.action_card(
        rank=2,
        action="Send water sprinklers to the transport hub",
        effort="medium",
        owner="Ward officer",
        time_window="today",
        why_now_html='PM10 is high <span class="pw-cite">[F2]</span>',
        data_gap_action="Verify the Bhosari sensor first.",
        library_id="wo_sprinkle_hotspot",
    )
    assert "pw-action__rank" in html and ">2<" in html
    assert "effort: medium" in html
    assert "owner: Ward officer" in html
    assert "when: today" in html
    assert "Verify the Bhosari sensor first." in html
    assert "wo_sprinkle_hotspot" in html


def test_advisory_card_has_print_wrapper_and_care_blocks():
    html = c.advisory_card(
        headline="Air is unhealthy today",
        category="Poor",
        color="#F26522",
        language="English",
        level="simple",
        today_html="Keep windows closed <span class=&quot;pw-cite&quot;>[F1]</span>",
        what_to_do=["Avoid outdoor running", "Wear a mask outdoors"],
        care={"children": "No outdoor play.", "older_adults": "Stay indoors.", "sensitive_groups": ""},
        source_line="2026-11-01 · 7 stations",
        used_fallback=True,
    )
    assert "pw-print" in html
    assert "pw-advisory--simple" in html
    assert "--cat-color:#F26522" in html
    assert "No outdoor play." in html
    assert "sensitive groups" not in html.lower()
    assert "offline fallback text" in html
    assert "Not medical advice" in html


# ------------------------------------------------------------------ what changed
def test_delta_card_arrow_signed_number_and_transition():
    html = c.delta_card(
        name="Shivajinagar",
        delta=34.0,
        direction="worse",
        before="Poor",
        after="Very Poor",
        color_before="#F26522",
        color_after="#D0021B",
    )
    assert "pw-delta--worse" in html
    assert "▲" in html and "+34.0" in html
    assert "Poor" in html and "Very Poor" in html

    dropped = c.delta_card(name="Bhosari", delta=-12.0, direction="better")
    assert "pw-delta--better" in dropped
    assert "▼" in dropped and "−12.0" in dropped


def test_transition_pill_without_colours_degrades():
    html = c.transition_pill("Poor", "Severe", None, None)
    assert "pw-transition" in html
    assert "Poor" in html and "Severe" in html


# ------------------------------------------------------------------ empty / loading / start
def test_empty_state_names_the_next_action():
    html = c.empty_state("No data yet", "Load a table.", "Load sample week 1")
    assert "No data yet" in html
    assert "→ Load sample week 1" in html


def test_skeleton_counts_lines_and_labels():
    html = c.skeleton(4, "Interpreting the findings…")
    assert html.count("pw-skeleton__line") == 4
    assert 'aria-label="Interpreting the findings…"' in html
    assert c.skeleton(0).count("pw-skeleton__line") == 1
    assert c.skeleton(99).count("pw-skeleton__line") == 6


def test_start_screen_pieces():
    html = c.wordmark("Air Briefing Studio", "Pune")
    assert "pw-wordmark__mark" in html
    assert "Air Briefing Studio" in html
    grid = c.start_grid([c.start_card("Step 1", "Load data", "Click a sample.")])
    assert "pw-start__card" in grid
    assert "pw-wash" in c.wash()


# ------------------------------------------------------------------ theme + purity
def test_theme_defines_required_tokens():
    css = theme.CSS
    for token in (
        "--elev-0", "--elev-1", "--elev-2", "--font-sans", "--font-mono",
        "--bg", "--fg", "--hairline", "--accent",
    ):
        assert token in css, token
    assert "prefers-color-scheme: dark" in css
    assert "prefers-reduced-motion" in css
    assert "@media print" in css
    assert "font-variant-numeric: tabular-nums" in css
    assert "corner-shape: squircle" in css
    assert "fonts.googleapis.com" in css
    assert "data-testid=\\\"stTab\\\"" not in css  # selectors stay real CSS, not Python escapes
    assert '[data-testid="stTab"]' in css
    assert "max-width: 640px" in css


def test_theme_inject_uses_unsafe_html():
    source = inspect.getsource(theme.inject)
    assert "unsafe_allow_html=True" in source


def test_components_module_has_no_streamlit_calls():
    source = (ROOT / "ui" / "components.py").read_text()
    assert "streamlit" not in source
    assert "import st" not in source


def test_detect_theme_reads_streamlit_config():
    assert theme.detect_theme({"theme.base": "dark", "theme.primaryColor": "#0B6E4F"}) == "dark"
    assert theme.detect_theme({"theme.base": "light"}) == "light"
    # any other theme option pins Streamlit's base to light
    assert theme.detect_theme({"theme.base": None, "theme.primaryColor": "#0B6E4F"}) == "light"
    # nothing configured: Streamlit follows the browser, so the media query decides
    assert theme.detect_theme({"theme.base": None, "theme.primaryColor": None}) is None
    assert theme.detect_theme({}) is None


def test_tokens_css_overrides_only_for_a_known_scheme():
    assert theme.tokens_css(None) == ""
    assert theme.tokens_css("weird") == ""
    dark = theme.tokens_css("dark")
    assert dark.startswith("<style>:root {") and "--bg: #101312" in dark
    light = theme.tokens_css("light")
    assert "--bg: #fbfaf8" in light
