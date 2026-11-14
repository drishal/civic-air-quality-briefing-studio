"""Visual polish for the briefing studio: one stylesheet, injected once per rerun.

Design tokens first: every colour, radius and shadow is a CSS custom property, so the dark
variant only redefines tokens. Streamlit's own chrome is restyled through stable ``data-testid``
hooks and degrades to the default look when a hook is missing.

Light/dark follows the theme Streamlit actually renders (read from ``st.config``), not the OS media
query: ``.streamlit/config.toml`` sets a theme option, which pins Streamlit's base to light, so an
OS-dark browser still gets light chrome and the two must not disagree. The media query stays as the
fallback for a build with no theme options at all, where Streamlit follows the browser.
No JavaScript, no network assets other than the two Google Fonts families.
"""

from __future__ import annotations

from typing import Optional

import streamlit as st

FONT_IMPORT = (
    "https://fonts.googleapis.com/css2"
    "?family=Geist:wght@400;500;600;650;700"
    "&family=JetBrains+Mono:wght@400;600"
    "&display=swap"
)

_SHARED_TOKENS = """
  color-scheme: light dark;
  --font-sans: "Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --radius: 12px;
  --radius-lg: 16px;
  --gutter: 20px;
  --ease: cubic-bezier(.2, .7, .2, 1);
"""

LIGHT_TOKENS = """
  --accent: #0b6e4f;
  --accent-ink: #084d38;
  --on-accent: #ffffff;

  --bg: #fbfaf8;
  --surface: #ffffff;
  --surface-2: #f6f5f2;
  --fg: #171a18;
  --fg-2: #5b605c;
  --fg-3: #6f746f;
  --hairline: color-mix(in oklab, var(--fg) 11%, transparent);
  --hairline-strong: color-mix(in oklab, var(--fg) 20%, transparent);
  --tint: color-mix(in oklab, var(--accent) 3%, var(--bg));

  --ok: #0c7a4d;
  --warn: #8a5a00;
  --danger: #a8201a;
  --info: #1f5fbf;
  --ok-surface: color-mix(in oklab, var(--ok) 12%, var(--surface));
  --warn-surface: color-mix(in oklab, var(--warn) 14%, var(--surface));
  --danger-surface: color-mix(in oklab, var(--danger) 12%, var(--surface));
  --info-surface: color-mix(in oklab, var(--info) 10%, var(--surface));

  --elev-0: none;
  --elev-1: inset 0 1px 0 rgba(255, 255, 255, .9),
            0 1px 2px rgba(23, 26, 24, .06),
            0 10px 24px -16px rgba(23, 26, 24, .28);
  --elev-2: inset 0 1px 0 rgba(255, 255, 255, .95),
            0 2px 4px rgba(23, 26, 24, .07),
            0 22px 44px -24px rgba(23, 26, 24, .32);
"""

DARK_TOKENS = """
  --accent: #5ad3a5;
  --accent-ink: #9ff0cd;
  --on-accent: #06231a;
  --bg: #101312;
  --surface: #171b19;
  --surface-2: #1d2220;
  --fg: #eef2ef;
  --fg-2: #b4bbb6;
  --fg-3: #949c97;
  --hairline: color-mix(in oklab, var(--fg) 15%, transparent);
  --hairline-strong: color-mix(in oklab, var(--fg) 26%, transparent);
  --tint: color-mix(in oklab, var(--accent) 4%, var(--bg));
  --ok: #59d3a0;
  --warn: #e5ab4d;
  --danger: #ff8a80;
  --info: #8ab4ff;
  --ok-surface: color-mix(in oklab, var(--ok) 16%, var(--surface));
  --warn-surface: color-mix(in oklab, var(--warn) 16%, var(--surface));
  --danger-surface: color-mix(in oklab, var(--danger) 16%, var(--surface));
  --info-surface: color-mix(in oklab, var(--info) 16%, var(--surface));
  --elev-1: inset 0 1px 0 rgba(255, 255, 255, .045),
            0 1px 2px rgba(0, 0, 0, .5),
            0 12px 28px -18px rgba(0, 0, 0, .8);
  --elev-2: inset 0 1px 0 rgba(255, 255, 255, .06),
            0 2px 6px rgba(0, 0, 0, .55),
            0 26px 50px -26px rgba(0, 0, 0, .9);
"""

CSS = (
    """
<style>
@import url('"""
    + FONT_IMPORT
    + """');

/* ------------------------------------------------------------------ tokens */
:root {"""
    + _SHARED_TOKENS
    + LIGHT_TOKENS
    + """}
@media (prefers-color-scheme: dark) {
  :root {"""
    + DARK_TOKENS
    + """  }
}

/* ------------------------------------------------------------------ page shell */
html, body, [data-testid="stAppViewContainer"] {
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-sans);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
[data-testid="stAppViewContainer"] {
  background-image:
    radial-gradient(120% 80% at 100% 0%, color-mix(in oklab, var(--accent) 5%, transparent), transparent 60%),
    radial-gradient(90% 70% at 0% 0%, color-mix(in oklab, var(--accent) 3%, transparent), transparent 55%);
  background-attachment: fixed;
}
/* 2.5% paper grain, fixed so it never scrolls with content */
[data-testid="stAppViewContainer"]::after {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 3;
  opacity: .025;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3' stitchTiles='stitch'/></filter><rect width='160' height='160' filter='url(%23n)'/></svg>");
}
[data-testid="stHeader"] { background: transparent; }
[data-testid="stMainBlockContainer"] {
  max-width: 1180px;
  padding-top: 2.4rem;
  padding-bottom: 4rem;
}
[data-testid="stSidebar"] {
  background: var(--tint);
  border-right: 1px solid var(--hairline);
}
[data-testid="stSidebarUserContent"] { padding-top: .4rem; }
[data-testid="stSidebar"] hr, hr { border-color: var(--hairline); }
/* sample-data buttons: full width, never truncated */
[data-testid="stSidebar"] .stButton > button { width: 100%; }

h1, h2, h3, h4, h5, [data-testid="stHeading"] {
  font-family: var(--font-sans);
  letter-spacing: -.017em;
  color: var(--fg);
  text-wrap: balance;
}
[data-testid="stHeading"] h1, [data-testid="stMarkdownContainer"] h1 { font-size: 1.9rem; font-weight: 650; }
[data-testid="stHeading"] h2, [data-testid="stMarkdownContainer"] h2 { font-size: 1.42rem; font-weight: 630; }
[data-testid="stHeading"] h3, [data-testid="stMarkdownContainer"] h3 { font-size: 1.16rem; font-weight: 600; }
[data-testid="stMarkdownContainer"] h4 { font-size: 1.02rem; font-weight: 600; }
p, li, label, [data-testid="stMarkdownContainer"] { text-wrap: pretty; }
a { color: var(--accent-ink); }
code, pre, [data-testid="stCode"] { font-family: var(--font-mono); }

/* --------------------------------------------------------------- widgets */
.stButton > button, .stDownloadButton > button,
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-primary"] {
  border-radius: 10px;
  border: 1px solid var(--hairline-strong);
  font-family: var(--font-sans);
  font-weight: 550;
  transition: transform 120ms var(--ease), box-shadow 120ms var(--ease), background 120ms var(--ease);
}
[data-testid="stBaseButton-secondary"]:hover, [data-testid="stBaseButton-primary"]:hover {
  transform: translateY(-1px);
  box-shadow: var(--elev-1);
}
[data-testid="stBaseButton-primary"] {
  background: var(--accent);
  border-color: color-mix(in oklab, var(--accent) 70%, black);
  color: var(--on-accent);
}
[data-testid="stBaseButton-primary"]:hover { background: color-mix(in oklab, var(--accent) 88%, black); }
.stButton > button:focus-visible,
[data-testid="stBaseButton-secondary"]:focus-visible,
[data-testid="stBaseButton-primary"]:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

[data-baseweb="select"] > div, [data-baseweb="input"] > div,
[data-testid="stTextInputRootElement"], [data-testid="stTextAreaRootElement"],
[data-testid="stMultiSelect"] [data-baseweb="select"] > div {
  border-radius: 10px;
  border-color: var(--hairline-strong);
}
[data-testid="stFileUploaderDropzone"] {
  border: 1px dashed var(--hairline-strong);
  border-radius: var(--radius);
  background: transparent;
}
[data-testid="stExpander"] details, [data-testid="stExpander"] {
  border-radius: var(--radius);
  border-color: var(--hairline);
  background: color-mix(in oklab, var(--surface) 60%, transparent);
}
[data-testid="stAlertContainer"] {
  border-radius: var(--radius);
  border: 1px solid var(--hairline);
  box-shadow: var(--elev-0);
}
[data-testid="stDataFrame"], [data-testid="stTable"] {
  border: 1px solid var(--hairline);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--surface);
}
[data-testid="stMetric"] {
  background: transparent;
  border-left: 2px solid color-mix(in oklab, var(--accent) 45%, transparent);
  padding-left: 12px;
}
[data-testid="stMetricValue"] {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  letter-spacing: -.02em;
  font-size: 1.55rem;
}
[data-testid="stMetricValue"] [data-testid="stMarkdownContainer"] { overflow: visible; }
[data-testid="stMetricValue"] p { margin: 0; line-height: 1.35; }
[data-testid="stMetricLabel"] { color: var(--fg-2); }
[data-testid="stMetricLabel"] p { margin: 0; }

/* ------------------------------------------- native widgets follow the tokens (so the in-app toggle works) */
[data-testid="stApp"], [data-testid="stSidebar"], [data-testid="stSidebarContent"] { color: var(--fg); }
[data-testid="stSidebar"] { background: var(--surface-2); }
[data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li, [data-testid="stText"] { color: var(--fg); }
[data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label { color: var(--fg-2); }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { color: var(--fg-3); }
[data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3 { color: var(--fg); }
.stButton > button[data-testid="stBaseButton-secondary"], .stDownloadButton > button,
[data-testid="stBaseButton-secondary"] {
  background: var(--surface);
  color: var(--fg);
}
[data-testid="stBaseButton-secondary"]:hover { background: var(--surface-2); border-color: var(--accent); }
[data-baseweb="select"] > div, [data-baseweb="input"], [data-baseweb="input"] > div, [data-baseweb="textarea"],
[data-testid="stTextInputRootElement"], [data-testid="stTextAreaRootElement"] {
  background: var(--surface);
  color: var(--fg);
}
[data-baseweb="select"] *, [data-baseweb="input"] input, [data-baseweb="textarea"] textarea,
[data-testid="stTextArea"] textarea, [data-testid="stTextInput"] input { color: var(--fg); }
[data-baseweb="input"] input, [data-baseweb="textarea"] textarea, textarea, input { background: transparent; }
[data-baseweb="popover"] [data-baseweb="menu"], [data-baseweb="popover"] ul {
  background: var(--surface);
  color: var(--fg);
}
[data-baseweb="select"] > div > div, [data-testid="stMultiSelect"] [data-baseweb="select"] > div { background: var(--surface); }
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primary"] p,
[data-testid="stBaseButton-primary"] * { color: var(--on-accent); }
/* tooltips: always a high-contrast inverted chip */
[data-testid="stTooltipContent"], [data-testid="stTooltipContent"] *,
[data-testid="stTooltipContent"] [data-testid="stMarkdownContainer"] p { background: var(--fg) !important; color: var(--bg) !important; }
/* select / input internals carry their own theme colours: force the tokens all the way down */
[data-baseweb="select"] div, [data-baseweb="input"] div, [data-baseweb="textarea"] div,
[data-testid="stSelectbox"] div, [data-testid="stMultiSelect"] div { background-color: var(--surface) !important; color: var(--fg) !important; }
[data-testid="stSelectbox"] [data-testid="stWidgetLabel"], [data-testid="stSelectbox"] [data-testid="stWidgetLabel"] div,
[data-testid="stMultiSelect"] [data-testid="stWidgetLabel"], [data-testid="stMultiSelect"] [data-testid="stWidgetLabel"] div {
  background-color: transparent !important; color: var(--fg-2) !important;
}
[data-testid="stSelectbox"] *:not(span), [data-testid="stSelectbox"] input,
[data-testid="stMultiSelect"] input { color: var(--fg) !important; -webkit-text-fill-color: var(--fg); opacity: 1; }
[data-testid="stSelectbox"] [data-testid="stWidgetLabel"] *, [data-testid="stMultiSelect"] [data-testid="stWidgetLabel"] * { color: var(--fg-2) !important; -webkit-text-fill-color: var(--fg-2); }
[data-testid="stSelectbox"] svg, [data-testid="stMultiSelect"] svg { fill: var(--fg-2) !important; color: var(--fg-2) !important; opacity: 1 !important; }
[data-testid="stMultiSelect"] span, [data-testid="stMultiSelect"] span svg { color: #fff !important; fill: #fff !important; }
[data-testid="stTooltipHoverTarget"] svg, [data-testid="stWidgetLabel"] svg { color: var(--fg-3); fill: var(--fg-3); }

/* theme toggle: a small round icon button pinned to the top right */
.st-key-theme_toggle { position: fixed; top: .55rem; right: 3.4rem; z-index: 1000002; width: auto; }
.st-key-theme_toggle button {
  width: 2.25rem; height: 2.25rem; min-height: 0; padding: 0; border-radius: 999px;
  background: var(--surface); color: var(--fg); border: 1px solid var(--hairline-strong);
  box-shadow: var(--elev-1); font-size: 1.15rem;
}
.st-key-theme_toggle button:hover { border-color: var(--accent); color: var(--accent-ink); }
.st-key-theme_toggle button * { color: inherit; }
[data-baseweb="popover"] li { color: var(--fg); }
[data-baseweb="popover"] li:hover, [data-baseweb="popover"] li[aria-selected="true"] { background: var(--surface-2); }
[data-testid="stFileUploaderDropzone"] *, [data-testid="stFileUploaderDropzoneInstructions"] * { color: var(--fg-2); }
[data-testid="stFileUploaderDropzone"] button { background: var(--surface); color: var(--fg); }
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary * { color: var(--fg); }
[data-testid="stAlertContainer"] * { color: inherit; }
[data-testid="stDataFrame"] { color-scheme: inherit; }

/* --------------------------------------------------------------- tabs (react-aria in 1.67, graceful otherwise) */
[data-testid="stTabs"] [role="tablist"] {
  gap: 2px;
  border-bottom: 1px solid var(--hairline);
  overflow-x: auto;
  scrollbar-width: none;
}
[data-testid="stTabs"] [role="tablist"]::-webkit-scrollbar { display: none; }
[data-testid="stTab"] {
  position: relative;
  flex: 0 0 auto;
  white-space: nowrap;
  border-radius: 8px 8px 0 0;
  padding: .35rem .6rem;
  color: var(--fg-2);
  font-weight: 500;
  font-size: .92rem;
  transition: color 140ms var(--ease), background 140ms var(--ease);
}
[data-testid="stTab"] p, [data-testid="stTab"] [data-testid="stMarkdownContainer"] { white-space: nowrap; }
[data-testid="stTab"]:hover { color: var(--fg); background: color-mix(in oklab, var(--fg) 5%, transparent); }
[data-testid="stTab"][data-selected="true"] { color: var(--fg); font-weight: 600; }
[data-testid="stTab"] .react-aria-SelectionIndicator,
[data-testid="stTab"] [class*="SelectionIndicator"] {
  position: absolute;
  left: 6px;
  right: 6px;
  bottom: -1px;
  height: 2px;
  border-radius: 2px;
  background: var(--accent);
  transform-origin: left center;
  animation: pw-tab-slide 180ms var(--ease);
}
[data-testid="stTab"][data-selected="true"]:not(:has([class*="SelectionIndicator"]))::after {
  content: "";
  position: absolute;
  left: 6px;
  right: 6px;
  bottom: -1px;
  height: 2px;
  border-radius: 2px;
  background: var(--accent);
  animation: pw-tab-slide 180ms var(--ease);
}
[data-testid="stTabPanel"] { padding-top: 1.1rem; }
@keyframes pw-tab-slide {
  from { transform: scaleX(.35); opacity: 0; }
  to { transform: scaleX(1); opacity: 1; }
}

/* --------------------------------------------------------------- chips, badges */
.pw-chip {
  display: inline-flex;
  align-items: center;
  gap: .35em;
  padding: .22rem .6rem;
  border-radius: 999px;
  font-size: .78rem;
  font-weight: 560;
  line-height: 1.35;
  border: 1px solid var(--hairline-strong);
  background: var(--surface);
  color: var(--fg-2);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.pw-chip--solid { color: var(--chip-ink, #fff); border-color: transparent; }
.pw-chip--ok { background: var(--ok-surface); color: var(--ok); border-color: color-mix(in oklab, var(--ok) 40%, transparent); }
.pw-chip--warn { background: var(--warn-surface); color: var(--warn); border-color: color-mix(in oklab, var(--warn) 40%, transparent); }
.pw-chip--danger { background: var(--danger-surface); color: var(--danger); border-color: color-mix(in oklab, var(--danger) 40%, transparent); }
.pw-chip--info { background: var(--info-surface); color: var(--info); border-color: color-mix(in oklab, var(--info) 36%, transparent); }
.pw-chip--accent { background: color-mix(in oklab, var(--accent) 14%, var(--surface)); color: var(--accent-ink); border-color: color-mix(in oklab, var(--accent) 34%, transparent); }
.pw-chip--hollow { background: transparent; border-style: dashed; color: var(--fg-3); }
.pw-chip__dot { width: .48em; height: .48em; border-radius: 50%; background: currentColor; }

.pw-vitals { display: flex; flex-wrap: wrap; gap: .4rem; margin: .2rem 0 .9rem; }

/* citation chips + sources row */
.pw-cite {
  display: inline-block;
  font-family: var(--font-mono);
  font-size: .74em;
  font-weight: 600;
  line-height: 1;
  padding: .18em .38em .16em;
  margin: 0 .12em;
  border-radius: 5px;
  color: var(--accent-ink);
  background: color-mix(in oklab, var(--accent) 16%, transparent);
  border: 1px solid color-mix(in oklab, var(--accent) 38%, transparent);
  cursor: help;
  vertical-align: baseline;
  transition: background 120ms var(--ease);
}
.pw-cite:hover { background: color-mix(in oklab, var(--accent) 30%, transparent); }
.pw-cite--unknown { color: var(--warn); background: var(--warn-surface); border-color: color-mix(in oklab, var(--warn) 45%, transparent); border-style: dashed; }
.pw-sources {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: .35rem;
  margin: .55rem 0 0;
  padding-top: .5rem;
  border-top: 1px dashed var(--hairline);
  font-size: .78rem;
  color: var(--fg-3);
}
.pw-sources__label { letter-spacing: .08em; text-transform: uppercase; font-size: .68rem; font-weight: 600; }

/* --------------------------------------------------------------- AQI hero */
.pw-hero {
  position: relative;
  display: grid;
  grid-template-columns: minmax(128px, 168px) 1fr;
  gap: 1.4rem;
  align-items: center;
  padding: 1.25rem 1.4rem 1.35rem;
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--elev-2);
  border: 1px solid var(--hairline);
  overflow: hidden;
  margin-bottom: 1.1rem;
}
.pw-hero::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 6px;
  background: var(--cat-color, var(--accent));
}
.pw-hero__value {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 2rem;
  line-height: 1;
  font-weight: 650;
  letter-spacing: -.04em;
  color: color-mix(in oklab, var(--cat-color, var(--fg)) 68%, var(--fg));
  display: block;
}
.pw-hero__unit { font-size: .72rem; color: var(--fg-3); letter-spacing: .08em; text-transform: uppercase; margin-top: .35rem; }
.pw-hero__cat { font-size: 1.24rem; font-weight: 650; letter-spacing: -.02em; color: color-mix(in oklab, var(--cat-color, var(--fg)) 62%, var(--fg)); }
.pw-hero__facts { color: var(--fg-2); font-size: .86rem; margin: .3rem 0 0; line-height: 1.5; }
.pw-hero__facts b { color: var(--fg); font-variant-numeric: tabular-nums; }
.pw-hero__meta { display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .65rem; }

/* --------------------------------------------------------------- cards */
.pw-cards { display: grid; gap: .7rem; grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); margin: .6rem 0 1rem; }
.pw-card {
  position: relative;
  padding: .8rem .9rem .75rem;
  border-radius: var(--radius);
  background: var(--surface);
  border: 1px solid var(--hairline);
  box-shadow: var(--elev-1);
  overflow: hidden;
}
.pw-card::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 4px;
  background: var(--cat-color, var(--hairline-strong));
}
.pw-card__top { display: flex; justify-content: space-between; align-items: baseline; gap: .5rem; }
.pw-card__name { font-weight: 600; font-size: .95rem; letter-spacing: -.01em; }
.pw-card__ward { color: var(--fg-3); font-size: .76rem; }
.pw-card__aqi {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-size: 1.65rem;
  font-weight: 650;
  letter-spacing: -.03em;
  color: color-mix(in oklab, var(--cat-color, var(--fg)) 68%, var(--fg));
  line-height: 1.1;
}
.pw-card__row { display: flex; align-items: center; justify-content: space-between; gap: .5rem; margin-top: .3rem; }
.pw-card__trend { font-size: .74rem; color: var(--fg-2); font-variant-numeric: tabular-nums; }
.pw-card__trend--worse { color: var(--danger); }
.pw-card__trend--better { color: var(--ok); }
.pw-card__spark { display: block; margin: .55rem 0 .1rem; width: 100%; height: 30px; }
.pw-card__pills { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .5rem; }

/* --------------------------------------------------------------- ribbon */
.pw-ribbon { display: flex; width: 100%; height: 7px; border-radius: 999px; overflow: hidden; background: var(--surface-2); margin: .35rem 0; }
.pw-ribbon--compact { height: 4px; margin: .35rem 0 0; border-radius: 3px; }
.pw-ribbon__seg { height: 100%; min-width: 2px; transition: filter 120ms var(--ease); }
.pw-ribbon__seg:hover { filter: brightness(1.25); }
.pw-ribbon__seg--valid { background: color-mix(in oklab, var(--ok) 62%, var(--surface-2)); }
.pw-ribbon__seg--missing { background: repeating-linear-gradient(135deg, var(--hairline-strong) 0 3px, transparent 3px 6px); background-color: var(--surface-2); }
.pw-ribbon__seg--flagged { background: color-mix(in oklab, var(--warn) 78%, var(--surface-2)); }
.pw-ribbon__seg--stuck { background: color-mix(in oklab, var(--danger) 72%, var(--surface-2)); }
.pw-ribbon-legend { display: flex; flex-wrap: wrap; gap: .8rem; font-size: .74rem; color: var(--fg-3); margin-top: .4rem; }
.pw-ribbon-legend span { display: inline-flex; align-items: center; gap: .3rem; }
.pw-ribbon-legend i { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }
.pw-ribbon-row { display: grid; grid-template-columns: minmax(96px, 150px) 1fr auto; align-items: center; gap: .6rem; margin: .3rem 0; }
.pw-ribbon-row__name { font-size: .82rem; font-weight: 550; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pw-ribbon-row__bar { display: block; }
.pw-ribbon-row__pct { font-size: .74rem; color: var(--fg-3); font-variant-numeric: tabular-nums; white-space: nowrap; }

/* --------------------------------------------------------------- action cards */
.pw-action {
  position: relative;
  padding: .85rem 1rem .8rem 3rem;
  border-radius: var(--radius);
  background: var(--surface);
  border: 1px solid var(--hairline);
  box-shadow: var(--elev-1);
  margin-bottom: .6rem;
}
.pw-action__rank {
  position: absolute;
  left: .8rem;
  top: .8rem;
  width: 1.6rem;
  height: 1.6rem;
  border-radius: 8px;
  display: grid;
  place-items: center;
  font-family: var(--font-mono);
  font-size: .82rem;
  font-weight: 600;
  color: var(--accent-ink);
  background: color-mix(in oklab, var(--accent) 14%, var(--surface));
  border: 1px solid color-mix(in oklab, var(--accent) 30%, transparent);
}
.pw-action__title { font-weight: 600; font-size: 1rem; margin: 0 0 .35rem; letter-spacing: -.01em; }
.pw-action__meta { display: flex; flex-wrap: wrap; gap: .3rem; margin-bottom: .45rem; }
.pw-action__meta .pw-chip { white-space: normal; overflow-wrap: anywhere; text-align: left; }
.pw-action__why { color: var(--fg-2); font-size: .88rem; margin: 0; }
.pw-action__why b { color: var(--fg); }
.pw-action__gap {
  margin-top: .5rem;
  padding: .45rem .6rem;
  border-radius: 9px;
  background: var(--warn-surface);
  border: 1px solid color-mix(in oklab, var(--warn) 32%, transparent);
  color: var(--warn);
  font-size: .8rem;
}
.pw-action__id { color: var(--fg-3); font-size: .7rem; font-family: var(--font-mono); margin-top: .35rem; }

/* --------------------------------------------------------------- advisory cards */
.pw-advisory {
  position: relative;
  border-radius: var(--radius-lg);
  background: var(--surface);
  border: 1px solid var(--hairline);
  box-shadow: var(--elev-1);
  overflow: hidden;
  margin-bottom: .8rem;
}
.pw-advisory__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: .5rem;
  padding: .7rem 1rem;
  background: color-mix(in oklab, var(--cat-color, var(--accent)) 16%, var(--surface));
  border-bottom: 1px solid color-mix(in oklab, var(--cat-color, var(--accent)) 30%, transparent);
}
.pw-advisory__headline { font-size: 1.1rem; font-weight: 640; letter-spacing: -.015em; margin: 0; flex: 1 1 14rem; }
.pw-advisory__body { padding: .9rem 1rem 1rem; }
.pw-advisory__message { font-size: 1.02rem; line-height: 1.55; margin: 0 0 .7rem; }
.pw-advisory--simple .pw-advisory__message { font-size: 1.15rem; }
.pw-advisory__list { margin: 0 0 .8rem; padding-left: 1.05rem; font-size: .95rem; line-height: 1.55; }
.pw-advisory__list li { margin-bottom: .2rem; }
.pw-care { display: grid; gap: .5rem; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.pw-care__block {
  padding: .55rem .65rem;
  border-radius: 10px;
  background: var(--surface-2);
  border: 1px solid var(--hairline);
}
.pw-care__title { display: flex; align-items: center; gap: .35rem; font-size: .72rem; font-weight: 650; letter-spacing: .06em; text-transform: uppercase; color: var(--fg-2); margin-bottom: .25rem; }
.pw-care__text { font-size: .9rem; line-height: 1.5; }
.pw-advisory__source { margin-top: .75rem; padding-top: .55rem; border-top: 1px dashed var(--hairline); font-size: .76rem; color: var(--fg-3); }
.pw-advisory__care-note { font-size: .72rem; color: var(--fg-3); margin-top: .45rem; }

/* --------------------------------------------------------------- what changed */
.pw-deltas { display: grid; gap: .5rem; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); margin: .5rem 0 .9rem; }
.pw-delta {
  display: flex; align-items: center; justify-content: space-between; gap: .6rem;
  padding: .55rem .7rem; border-radius: 10px;
  background: var(--surface); border: 1px solid var(--hairline); box-shadow: var(--elev-1);
}
.pw-delta__name { font-size: .86rem; font-weight: 550; }
.pw-delta__num { font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: .92rem; font-weight: 600; }
.pw-delta--worse { border-left: 3px solid var(--danger); }
.pw-delta--better { border-left: 3px solid var(--ok); }
.pw-delta--same { border-left: 3px solid var(--hairline-strong); }
.pw-delta--new { border-left: 3px solid var(--warn); }
.pw-delta--dropped { border-left: 3px solid var(--info); }
.pw-delta--worse .pw-delta__num { color: var(--danger); }
.pw-delta--better .pw-delta__num { color: var(--ok); }
.pw-delta--new .pw-delta__num { color: var(--warn); }
.pw-transition { display: inline-flex; align-items: center; gap: .4rem; flex-wrap: wrap; font-size: .85rem; margin-top: .15rem; }
.pw-transition__arrow { color: var(--fg-3); }
.pw-transition-line { font-size: .86rem; margin: .25rem 0; color: var(--fg-2); }

/* --------------------------------------------------------------- start screen */
.pw-wordmark { display: flex; align-items: center; gap: .55rem; font-weight: 650; letter-spacing: -.03em; font-size: 1.45rem; }
.pw-wordmark__mark {
  width: 1.55rem; height: 1.55rem; border-radius: 8px;
  background: linear-gradient(135deg, var(--accent), color-mix(in oklab, var(--accent) 45%, #f5c518));
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .5);
}
.pw-wordmark__sub { color: var(--fg-3); font-size: .78rem; letter-spacing: .02em; font-weight: 450; margin-top: .2rem; }
.pw-start { display: grid; gap: .8rem; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); margin: .8rem 0 1rem; }
.pw-start__card {
  position: relative; padding: 1rem 1.1rem 1.05rem;
  border-radius: var(--radius-lg);
  background: var(--surface);
  border: 1px solid var(--hairline);
  box-shadow: var(--elev-2);
  transition: transform 140ms var(--ease), box-shadow 140ms var(--ease);
}
.pw-start__card:hover { transform: translateY(-2px); box-shadow: var(--elev-2), 0 0 0 1px var(--accent); }
.pw-start__step { font-family: var(--font-mono); font-size: .72rem; letter-spacing: .1em; color: var(--accent-ink); text-transform: uppercase; }
.pw-start__title { font-size: 1.02rem; font-weight: 620; margin: .2rem 0 .3rem; }
.pw-start__body { font-size: .85rem; color: var(--fg-2); line-height: 1.5; margin: 0; }
.pw-wash {
  position: fixed; inset: -20%; z-index: 0; pointer-events: none;
  background:
    radial-gradient(38% 42% at 28% 32%, color-mix(in oklab, var(--accent) 8%, transparent), transparent 70%),
    radial-gradient(32% 40% at 72% 62%, color-mix(in oklab, var(--accent) 6%, transparent), transparent 72%);
  animation: pw-wash 40s ease-in-out infinite alternate;
}
@keyframes pw-wash {
  from { transform: translate3d(-3%, -2%, 0) scale(1.02); }
  to { transform: translate3d(4%, 3%, 0) scale(1.09); }
}

/* --------------------------------------------------------------- empty states, skeletons */
.pw-empty {
  padding: 1.4rem 1.3rem;
  border-radius: var(--radius-lg);
  border: 1px dashed var(--hairline-strong);
  background: color-mix(in oklab, var(--surface) 55%, transparent);
  max-width: 46rem;
}
.pw-empty__title { font-size: 1.02rem; font-weight: 620; margin: 0 0 .25rem; }
.pw-empty__body { color: var(--fg-2); font-size: .9rem; line-height: 1.55; margin: 0; }
.pw-empty__next { display: inline-flex; align-items: center; gap: .4rem; margin-top: .7rem; font-size: .84rem; color: var(--accent-ink); font-weight: 550; }
.pw-empty__glyph { font-size: 1.3rem; margin-bottom: .35rem; display: block; }

.pw-skeleton { display: grid; gap: .5rem; margin: .6rem 0; }
.pw-skeleton__line {
  height: 10px; border-radius: 999px;
  background: linear-gradient(90deg, var(--surface-2) 0%, color-mix(in oklab, var(--fg) 10%, var(--surface-2)) 50%, var(--surface-2) 100%);
  background-size: 220% 100%;
  animation: pw-shimmer 1.35s linear infinite;
  opacity: .85;
}
.pw-skeleton__line:nth-child(2) { width: 86%; }
.pw-skeleton__line:nth-child(3) { width: 62%; }
.pw-skeleton__label { font-size: .78rem; color: var(--fg-3); }
@keyframes pw-shimmer {
  from { background-position: 120% 0; }
  to { background-position: -120% 0; }
}

.pw-small { font-size: .8rem; color: var(--fg-3); }
.pw-num { font-variant-numeric: tabular-nums; font-family: var(--font-mono); }
.pw-section { display: flex; align-items: baseline; justify-content: space-between; gap: .6rem; margin: 1.1rem 0 .35rem; }
.pw-section__title { font-size: .95rem; font-weight: 620; letter-spacing: -.01em; }
.pw-section__hint { font-size: .76rem; color: var(--fg-3); }

/* prose blocks used by app.py */
.pw-lead { font-size: 1.06rem; line-height: 1.55; margin: .2rem 0 .6rem; }
.pw-note { max-width: 52rem; }
.pw-note h3, .pw-note h4, .pw-note h5 { margin: .9rem 0 .3rem; }
.pw-note p { margin: .35rem 0; line-height: 1.6; }
.pw-note li { margin: .2rem 0; line-height: 1.55; }
.pw-finding { padding: .45rem 0; border-bottom: 1px dashed var(--hairline); font-size: .9rem; }

/* --------------------------------------------------------------- responsive (390px) */
/* one display size: page title and the headline AQI */
.pw-title { font-size: 1.85rem; font-weight: 650; letter-spacing: -.03em; line-height: 1.15; margin: 0 0 .15rem; text-wrap: balance; }
@media (max-width: 900px) {
  .pw-hero { grid-template-columns: 1fr; }
  .pw-hero__value { font-size: 2rem; }
}
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"] { padding-left: 16px; padding-right: 16px; padding-top: 1.2rem; }
  .pw-title { font-size: 1.6rem; }
  .pw-cards, .pw-start, .pw-deltas, .pw-care { grid-template-columns: 1fr; }
  .pw-hero { padding: .9rem 1rem 1rem; }
  .pw-advisory__headline { font-size: 1rem; }
  .pw-action { padding: .8rem .85rem .75rem 2.6rem; }
  .pw-action__rank { left: .6rem; width: 1.4rem; height: 1.4rem; }
  [data-testid="stTabs"] [role="tablist"] { font-size: .85rem; }
  [data-testid="stSidebar"] { min-width: 0; }
  .pw-advisory__message, .pw-advisory--simple .pw-advisory__message { font-size: 1rem; }
  .pw-ribbon-row { grid-template-columns: 1fr auto; grid-template-areas: "name pct" "bar bar"; }
  .pw-ribbon-row__name { grid-area: name; }
  .pw-ribbon-row__pct { grid-area: pct; }
  .pw-ribbon-row__bar { grid-area: bar; }
  .pw-section { flex-direction: column; align-items: flex-start; gap: .1rem; }
  .pw-section__hint { font-size: .74rem; }
}
@media (max-width: 420px) {
  .pw-chip { font-size: .74rem; }
  .pw-card__aqi { font-size: 1.45rem; }
}

/* --------------------------------------------------------------- squircle corners where supported */
@supports (corner-shape: squircle) {
  .pw-hero, .pw-card, .pw-action, .pw-advisory, .pw-empty, .pw-start__card, .pw-delta { corner-shape: squircle; }
}

/* --------------------------------------------------------------- print: advisory one-pager */
@media print {
  @page { margin: 14mm; }
  html, body, [data-testid="stAppViewContainer"] { background: #fff !important; }
  .pw-wash, [data-testid="stAppViewContainer"]::after { display: none !important; }
  /* With advisories on the page, print them and nothing else: every element that neither is,
     sits inside, nor contains an advisory collapses, so the sheet starts at the first card. */
  body:has(.pw-print) *:not(.pw-print):not(.pw-print *):not(:has(.pw-print)):not(html):not(body) {
    display: none !important;
  }
  .pw-print { break-inside: avoid; margin: 0 0 8mm; box-shadow: none !important; border: 1px solid #8a8a8a !important; }
  .pw-advisory__head { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .001ms !important;
    scroll-behavior: auto !important;
  }
  .pw-wash { animation: none !important; }
}
</style>
"""
)


def _theme_option_values() -> dict[str, object]:
    """Configured theme options; empty when the runtime cannot be read (bare mode, tests)."""
    try:
        options = st.config.get_config_options()
    except Exception:
        return {}
    return {name: getattr(option, "value", None) for name, option in options.items()}


def detect_theme(values: Optional[dict] = None) -> Optional[str]:
    """The theme Streamlit is actually rendering, or None when it follows the browser.

    Streamlit pins the base to light as soon as any other ``theme.*`` option is set, which is the
    case for this app (``primaryColor``). When nothing is configured it follows the browser, and
    the media query in ``CSS`` is the correct signal.
    """
    values = _theme_option_values() if values is None else values
    base = values.get("theme.base")
    if base in ("light", "dark"):
        return str(base)
    if any(value is not None for name, value in values.items() if name.startswith("theme.")):
        return "light"
    return None


def tokens_css(scheme: Optional[str]) -> str:  # noqa: D401
    """An explicit token block that wins over the media-query fallback (same specificity, later)."""
    tokens = {"light": LIGHT_TOKENS, "dark": DARK_TOKENS}.get(scheme or "")
    if not tokens:
        return ""
    return f"<style>:root {{ color-scheme: {scheme};{tokens}}}</style>"


MODE_KEY = "theme_mode"  # session_state: "light" | "dark" once the user has picked one


def current_scheme() -> str:
    """The scheme in effect: the user's pick, else what Streamlit renders (light when unknown)."""
    try:
        picked = st.session_state.get(MODE_KEY)
    except Exception:
        picked = None
    return picked if picked in ("light", "dark") else (detect_theme() or "light")


def toggle_scheme() -> None:
    """Button callback: flip light/dark for this session."""
    st.session_state[MODE_KEY] = "light" if current_scheme() == "dark" else "dark"


def theme_toggle(key: str = "theme_toggle") -> None:
    """Icon button (top right) that switches between the light and dark theme."""
    dark = current_scheme() == "dark"
    st.button(
        ":material/light_mode:" if dark else ":material/dark_mode:", key=key, on_click=toggle_scheme,
        help="Switch to the light theme" if dark else "Switch to the dark theme",
    )


def inject() -> None:
    """Add the stylesheet to the page. Safe to call on every rerun."""
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(tokens_css(current_scheme()), unsafe_allow_html=True)
