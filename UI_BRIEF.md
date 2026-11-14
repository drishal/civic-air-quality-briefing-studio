# UI polish brief (distilled from webui/.todo/design-notes.md and DESIGN.md)

Decision: **keep Streamlit** (fast, one Python deploy, resilient for the "whole app" evaluation). Do NOT migrate to
React/Tailwind/Arc UI: Arc UI is React + shadcn CLI, so installing it means a second frontend stack and build step
for a 3-hour event. We **steal Arc UI's patterns** (sliding-indicator tabs, toast stack, stat cards, sources/citation
chips from its AI-chat block) and the design-notes craft rules, implemented as injected CSS + small HTML helpers.
Streamlit has no CSP restriction on `st.markdown(unsafe_allow_html=True)` / `<style>`, so inline CSS is fine here.

## Principles carried over from awui DESIGN.md
- Quiet, instrument-like calm. Nested hierarchy, not a wall of cards. No ornamental numbers.
- Semantic colours only: CPCB category colours for AQI (Good green ... Severe maroon), danger/warn/ok for flags.
- Tokens first: define everything as CSS custom properties on `:root` and a dark variant under
  `@media (prefers-color-scheme: dark)` and Streamlit's theme; derive hairlines/chips with `color-mix`.
- Motion makes state legible: 100-180 ms, `translate`/`opacity`/`clip-path` only, never animate width/height/top/left,
  no perpetual bounce. Respect `prefers-reduced-motion` (turn all motion off).
- Accessibility: contrast >= 4.5:1, real labels, focus rings, colour never the only signal (always add text/icon).

## What to build (all in `ui/theme.py` + `ui/components.py`, wired into app.py)
1. **Surface and type** (design-notes 2.1-2.7): three elevation tokens (--elev-0/1/2) with a lit top edge; squircle
   corners via `@supports (corner-shape: squircle)` fallback to radius; 2-3% page grain overlay; tint neutrals from the
   accent via `color-mix(in oklab, var(--accent) 3%, var(--bg))`; warm light paper (#fbfaf8); fonts: Geist (UI) and a
   mono (Commit Mono / JetBrains Mono) loaded from Google Fonts with system fallbacks; tabular numerals utility for
   every number; one display size (28-32px/650) for the page title and the headline AQI; `text-wrap: pretty/balance`.
2. **AQI hero + station cards**: big headline AQI tile for the selected area (category colour bar, dominant pollutant,
   WHO multiple, confidence chip), per-station cards with category colour edge, tiny sparkline of daily AQI (inline SVG),
   reliability pill, trend arrow with text ("worsening +12 AQI/day").
3. **Citation chips** (design-notes 4.12 Sources/Inline citation): render every `[F#]` in generated text as a small
   chip with a hover title showing the finding title + confidence; plus a collapsed "Sources" row under each AI block
   listing cited findings with the evidence row count. Helper: `cite_html(text, findings) -> str`.
4. **Data-quality ribbon** (design-notes 4.1 turn ribbon idea, adapted): a 6px segmented strip per station across the
   observed time span: valid / missing / flagged / stuck segments coloured by kind, hover title with label and
   duration. Pure HTML/CSS (flex-basis %), no canvas. Show in the Quality tab, and a compact version on station cards.
5. **Vitals chips** (4.2): `n readings`, `% usable`, `n flags`, `n stations unreliable`, `AI: Gemini | fallback` in a
   chip row at the top. Animate digits with a CSS `@property` counter (no JS) or leave static if it risks breaking.
6. **What-changed view**: delta chips with arrows and colours (worse = warn/danger, better = ok), category transition
   pills "Poor -> Very Poor", new/resolved hotspot badges.
7. **Action cards**: rank badge, effort chip (low/medium/high), owner, time window, why-now with citation chips, a warn
   strip for data_gap_action. Keep them flat (elev-1), not nested cards.
8. **Advisory cards**: category-coloured header, large readable text (min 18px for the "simple" level), three care
   blocks (children / older adults / sensitive groups) with icons and text labels, source line in muted text.
   Print stylesheet (`@media print`) that outputs the advisory as a clean one-pager.
9. **Sliding tab indicator** (Arc UI Tabs pattern): style Streamlit's tabs so the active underline slides (CSS only,
   `st.tabs` DOM selectors; degrade gracefully if selectors change).
10. **Empty states and skeletons**: calm illustrated-text empty states with the next action; shimmer skeleton while an
    LLM call runs (disabled under reduced motion).
11. **Start screen moment** (2.8): the first screen (no data) gets the wordmark, a slow 40s accent wash at 6-8%,
    and two big "Load sample week 1/2" cards. The rest of the app stays restrained.
12. **Responsive**: works at 390px width (stack columns, horizontal-scroll tables, 16px gutters), no horizontal page scroll.

## Do not
Add glass everywhere; use Lenis/smooth scroll; scale text for popovers; animate layout properties; ship pure-gray dark
mode; add a JS framework; add network-loaded assets other than Google Fonts (and everything must still render if fonts fail).

## Deliverables
`ui/__init__.py`, `ui/theme.py` (CSS string + `inject()`), `ui/components.py` (pure functions returning HTML strings,
HTML-escaped inputs only, unit-tested), `tests/test_ui_components.py`, and app.py edits to use them. Verify with
Streamlit AppTest plus real screenshots (desktop 1280 and phone 390, light and dark) using Playwright or the preview
tools; look at them and iterate.
