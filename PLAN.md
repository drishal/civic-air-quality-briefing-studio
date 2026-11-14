# Civic Air Quality Briefing Studio — Build Plan

PromptWars in-person, warm-up challenge. Solo build, Gemini as the core.

## 0. What the event actually rewards (from promptwars.in)

- One-day, solo, in-person "vibe coding" event. Warm-up runs **10:30–11:30 (about 60 min)**, main challenge 11:30–2:30.
- Winners are judged on three things: **(1) how well the solution solves the problem, (2) quality of the AI prompts ("architectural elegance"), (3) a live demo to an expert panel.**
- Google Antigravity and other AI tools are encouraged.
- The slide's tip says the checks evaluate the application **as a whole**. Meaningful end-to-end workflows get credit. A feature that works in isolation does not.

Design consequences:

1. Ship a thin, working **end-to-end** slice before anything fancy. Every required feature on the slide should be visible in the demo, even if some are simple.
2. The prompt architecture is a scored deliverable. Keep prompts as separate versioned files with schemas, and explain them in the pitch.
3. One-click **sample data** (messy, week 1 + week 2) so the demo never depends on typing or on a stranger's file.
4. Keep an unscripted-input path working too: paste text or upload CSV/XLSX.

## 1. Requirement checklist (every clause on the slide)

| # | Slide requirement | How we satisfy it | Tier |
|---|---|---|---|
| R1 | Gen AI as the core component | Gemini does schema mapping, interpretation, plain-language narrative, persona actions, translation, self-check | P0 |
| R2 | Pasted **or** uploaded data tables | Text area + file upload (CSV/TSV/XLSX) | P0 |
| R3 | Messy data: inconsistent formats, units, missing readings | Rule-based cleaner + Gemini column/unit mapper + quality flags | P0 |
| R4 | Summarize trends and likely hotspots in plain language | Deterministic stats, Gemini narrates | P0 |
| R5 | Prioritized local actions **by audience** (ward officer / housing society / school) | Persona selector + curated action library + ranked output | P0 |
| R6 | Every finding traceable to data | Finding IDs (F1..) with row/station/time evidence, click to see source rows | P0 |
| R7 | Data gaps and unreliable readings clearly flagged | Quality engine, flags on rows/stations, a "Data Quality" panel, confidence labels | P0 |
| R8 | Compare each new brief with the previous one | Brief history store + diff view ("what changed") | P1 |
| R9 | Internal briefing note | Generated doc, export to Markdown/print | P0 |
| R10 | Citizen advisory in multiple languages | English + Hindi + one regional language, selectable | P0 (2 langs) / P1 (3+) |
| R11 | Different reading levels | Simple / standard / technical variants | P1 |
| R12 | Care messaging for children, older adults, sensitive groups | Separate care blocks per group, tied to the AQI category | P0 |

P0 = must exist by about T+45 min. P1 = T+60 or the main-challenge reuse. P2 is the stretch list in section 9.

## 2. Core design principle: the LLM interprets, code computes

The "traceable to data" and "unreliable readings flagged" requirements are the hard part. The failure mode to avoid is an LLM reading a CSV and inventing numbers. So:

- **Code** (pandas) parses, cleans, converts units, computes AQI, trends, rankings, and diffs. It outputs a **Findings JSON** where each finding has an ID, a number, a formula and its evidence (row IDs).
- **Gemini** never sees raw numbers it must compute with. It receives the Findings JSON (plus the quality report) and turns it into language, actions and translations. It must cite finding IDs.
- A **verifier** (code + optional Gemini check) rejects output that cites unknown IDs or contains numbers not present in the Findings JSON.

This gives the pitch line: "Every sentence has a footnote, and the footnotes are checked by code."

The one place Gemini touches raw data is **schema mapping** of messy headers and units (Stage 1). Even there it only emits a mapping, and code validates it and applies it.

## 3. Domain facts to encode (researched)

### India CPCB National AQI (use as the primary scale)

- Six categories: Good 0–50, Satisfactory 51–100, **Moderately Polluted** 101–200, Poor 201–300, Very Poor 301–400, Severe 401–500.
- Sub-index per pollutant by linear interpolation between breakpoints. **Overall AQI = the worst sub-index**; the pollutant that sets it is the "dominant pollutant".
- Valid AQI needs at least **3 pollutants, one of them PM2.5 or PM10**, and at least **16 hours** of data for a 24-h average.
- Averaging: 24 h for PM10, PM2.5, NO2, SO2, NH3, Pb; **8 h for O3 and CO**.

Breakpoints (µg/m³ unless noted; CO in mg/m³):

| Category | PM10 | PM2.5 | NO2 | O3 (8h) | CO (8h) | SO2 | NH3 |
|---|---|---|---|---|---|---|---|
| Good 0–50 | 0–50 | 0–30 | 0–40 | 0–50 | 0–1.0 | 0–40 | 0–200 |
| Satisfactory 51–100 | 51–100 | 31–60 | 41–80 | 51–100 | 1.1–2.0 | 41–80 | 201–400 |
| Moderate 101–200 | 101–250 | 61–90 | 81–180 | 101–168 | 2.1–10 | 81–380 | 401–800 |
| Poor 201–300 | 251–350 | 91–120 | 181–280 | 169–208 | 10–17 | 381–800 | 801–1200 |
| Very Poor 301–400 | 351–430 | 121–250 | 281–400 | 209–748 | 17–34 | 801–1600 | 1200–1800 |
| Severe 401–500 | 430+ | 250+ | 400+ | 748+ | 34+ | 1600+ | 1800+ |

Caveat: these came from a secondary (garbled-in-places) source. Cross-check against the CPCB document or the CPCB AQI PDF before relying on the Pb column (omit Pb in v1). Ranges are inclusive, so add a unit test for each boundary value.

### WHO 2021 guidelines (secondary lens, "multiple of WHO limit")

PM2.5 24-h 15 µg/m³ (annual 5), PM10 24-h 45, NO2 24-h 25, O3 8-h peak-season 60 / 8-h 100. Show "this reading is N× the WHO limit" as extra context.

### Unit conversion (25 °C, 1 atm; ppb × MW / 24.45 = µg/m³)

NO2 1 ppb = 1.88 µg/m³; O3 1 ppb = 1.96; SO2 1 ppb = 2.62; NH3 1 ppb = 0.70; CO 1 ppm = 1.146 mg/m³ (= 1146 µg/m³). PM is always µg/m³, so a ppb label on PM is a data error to flag.

### GRAP (Delhi-NCR only, conditional)

Staged emergency action plan by AQI band (revised Nov 2025; the sources I found disagree on stage labels and I could not confirm the current status as of today). **Do not hard-code GRAP stages.** Only mention GRAP if the user's city is Delhi-NCR, and say "check the latest CAQM order". This avoids a confident wrong claim.

### Likely messy-data patterns to handle (build test data from this list)

Sentinels `NA`, `N/A`, `--`, `-`, `NULL`, `ND`, `BDL`, `-999`, `9999`, blank; thousands separators and decimal commas ("1,250" vs "12,5"); values with units or symbols inside cells ("85 µg/m3", "<5", ">500"); mixed units for the same pollutant (µg/m³, ug/m3, mg/m³, ppb, ppm); mixed date formats (DD/MM/YYYY vs MM/DD, 12h/24h, IST vs UTC); wide vs long layout; duplicated headers and subtitle rows; duplicate rows; negative values; stuck sensors (same value for many hours); impossible spikes; a ward spelled three ways ("Ward 12", "ward-12", "W12"); station names with typos.

## 4. Architecture

Recommended stack (fastest path to a working demo, deployable to Cloud Run):

- **Python 3.12, Streamlit** (UI + upload + tabs + downloads), **pandas**, **pydantic v2**, **google-genai** SDK.
- Storage: local `./.briefs/*.json` (brief history). SQLite is optional.
- Alternative if you prefer a custom look: FastAPI + a single static HTML page. Streamlit wins on speed for a 60-minute slot.

Pipeline:

```
paste/upload ─► [1 Ingest+Map] ─► [2 Clean+Normalize] ─► [3 Quality engine] ─► [4 Analytics]
                  (Gemini maps       (code: sentinels,      (code: flags,          (code: AQI,
                   columns/units)     units, dates)          reliability)           trends, hotspots)
                                                                                         │
                       previous brief JSON ──────────────────────► [5 Diff engine] ◄────┤
                                                                                         ▼
                                           Findings JSON (F1..Fn, evidence row IDs)
                                                                                         │
                       [6 Gemini: Interpret] ─► [7 Gemini: Persona actions] ─► [8 Gemini: Documents]
                       (plain-language trends)   (ranked, cited, by persona)    (internal note + citizen
                                                                                  advisory × lang × level)
                                                                                         │
                                           [9 Verifier: IDs exist, numbers match, tone/care checks]
                                                                                         ▼
                                                 UI tabs + exports + save as "previous brief"
```

Suggested file layout:

```
app.py                  # Streamlit UI
core/ingest.py          # read paste/upload, detect delimiter, wide/long
core/clean.py           # sentinels, units, dates, dedupe
core/quality.py         # flags + station reliability score
core/aqi.py             # CPCB breakpoints, sub-index, averaging rules (unit-tested)
core/analytics.py       # trends, hotspots, patterns -> findings
core/diff.py            # brief vs previous brief
core/verify.py          # citation and number checks
llm/client.py           # Gemini wrapper: retries, JSON schema, temperature, model fallback
llm/schemas.py          # pydantic output schemas
llm/prompts/*.md        # versioned prompts (one file per stage)
data/action_library.yaml# curated interventions per persona
data/sample_week1.csv, sample_week2.csv   # deliberately messy
tests/test_aqi.py       # boundary tests
```

### Data model (the traceability backbone)

- `reading`: `row_id` (source file + row number), `station`, `ward`, `ts`, `pollutant`, `value_raw`, `unit_raw`, `value_std` (µg/m³ or mg/m³ for CO), `flags[]`.
- `finding`: `id` ("F3"), `type` (hotspot | trend | exceedance | gap | anomaly), `claim_facts` (numbers), `formula` (human-readable), `evidence` (list of row_ids or a station/time window), `confidence` (high/medium/low from the data quality), `caveats[]`.
- `brief`: timestamp, city/area, source hash, findings, per-station AQI table, quality summary. Persisted so the next brief can diff against it.

### Quality flags (code, deterministic)

`MISSING`, `SENTINEL`, `NEGATIVE`, `OUT_OF_RANGE` (physically implausible), `UNIT_AMBIGUOUS` (converted using a guess, so show the guess), `UNIT_MISMATCH` (ppb on PM), `STUCK` (≥6 identical consecutive readings), `SPIKE` (jump > k× rolling median), `DUPLICATE`, `PARTIAL_DAY` (<16 h, so no valid 24-h AQI), `LOW_COVERAGE` (station <75% complete), `TIMESTAMP_REPAIRED`.
Station reliability = coverage × (1 − flagged share). Unreliable stations are **shown but down-weighted** and never headline a hotspot at "high" confidence.

### Hotspot logic

Rank stations/wards by 24-h AQI (worst sub-index), then adjust for confidence. Also report: persistent (many hours above threshold), recurring time-of-day peaks (e.g. 7–10 pm), and a pollutant signature (PM2.5-dominant = combustion/dust/burning, NO2+CO = traffic, O3 = afternoon photochemical). The cause language must be hedged ("consistent with", never "caused by").

## 5. Prompt architecture (this is a scoring criterion — make it visible)

Principles, to say out loud in the pitch:

1. **One job per prompt.** Mapper, interpreter, actioner, writer, verifier, each with its own file, schema and test.
2. **Schema-first outputs.** Pydantic schema → JSON schema → Gemini structured output → `model_validate_json`. No free-text parsing.
3. **Grounding contract.** "Use only facts in `FINDINGS`. Cite as `[F#]`. If evidence is insufficient, output `insufficient_evidence` instead of guessing."
4. **Uncertainty is first-class.** Each claim carries `confidence`; low-confidence data produces hedged wording plus a "verify on site" action instead of a firm conclusion.
5. **Persona as parameters, not copy-paste.** One actioner prompt with `{persona, capabilities, constraints, authority}` plus the action library.
6. **Audience as parameters.** One writer prompt with `{language, reading_level, audience_group}`, with explicit readability rules per level.
7. **Low temperature (0.2)** for analysis and actions, a little higher (0.5) for citizen text. Pin the model ID in one place with a fallback.
8. **Self-check pass.** The verifier compares output to Findings JSON and lists contradictions.

Prompt stubs:

**P1 Mapper (system):**
> You map messy air-quality tables to a fixed schema. You receive the first 30 rows and the header. Return JSON only: for each source column, `role` (station | ward | timestamp | date | time | pollutant_value | pollutant_name | unit | lat | lon | ignore), the pollutant it measures and its unit if present, layout (`wide` or `long`), date format guess, sentinel tokens found, and a list of `issues`. Never invent columns. If unsure, set `confidence: low` and say why.

**P2 Interpreter:**
> You are an air-quality analyst writing for non-experts. Input: `FINDINGS` (JSON) and `QUALITY` (JSON). Write the trend summary and hotspot description in plain language. Every sentence ends with citations like [F2][F5]. Do not state any number that is not in FINDINGS. Do not state causes as facts; use "consistent with". If a finding has confidence low, say so in the sentence. Close with a "What we can't tell from this data" list drawn from QUALITY.

**P3 Actioner:**
> Persona: `{persona}` with authority `{authority}` and limits `{limits}`. Choose and rank up to 5 actions from `ACTION_LIBRARY` that fit the findings. For each: action, why now (cite findings), who does it, effort (low/med/high), expected benefit (qualitative), time window (today / this week), and a data-gap action if confidence is low. Do not invent legal powers. Do not recommend anything outside the library without marking it `suggested_new` with rationale.

**P4 Writer (internal note):** structure fixed (Situation / What changed since last brief / Hotspots / Data caveats / Actions by owner / Open questions), with finding citations kept.

**P5 Writer (citizen advisory):** parameters `{language, reading_level, groups}`. Rules: grade-4–6 for "simple" (short sentences, no jargon, one action per line); **no medical diagnosis**; separate care blocks for children, older adults, people sensitive to poor air (asthma, heart/lung conditions, pregnancy); lead with today's category and the single most important action; keep a fixed "AQI category → colour/emoji" mapping; keep numbers and place names untranslated and identical across languages; end with a source line (date, stations used, data-gap note).

**P6 Verifier:** inputs `FINDINGS`, `DRAFT`. Output: list of (sentence, problem) for unsupported numbers, uncited claims, certainty overreach, and category/advice mismatch (e.g. "safe to exercise outdoors" when AQI is Poor). Code runs the cheap checks first (ID existence, number matching with a regex); Gemini handles the rest.

Language note: translations come from Gemini directly in the writer prompt (not a separate MT step). Offer English, Hindi, and one regional language chosen by city (Marathi/Kannada/Tamil/Bengali/Telugu/Gujarati). Optionally add a back-translation check for one language as a P2 feature, to prove the meaning didn't drift.

### Action library (curated, to keep Gemini from inventing)

`data/action_library.yaml`, each item: `id`, `persona[]`, `trigger` (pollutant signature, AQI band, time pattern), `action`, `effort`, `typical_owner`, `notes`.

- **Ward officer / municipal:** targeted road sweeping and water sprinkling at hotspot segments; inspect construction sites for dust controls; step up waste-burning and open-burning patrols at the hotspot and its peak time; traffic management at identified junctions; check the sensor itself where data is unreliable (re-calibrate, field visit); public notice and signage; coordinate with schools and RWAs; escalate to the state pollution board when AQI is Very Poor/Severe for multiple days.
- **Housing society (RWA):** keep DG-set use to a minimum; no waste or leaf burning in the society; dust control on any in-society construction; watering paths and landscaping; shift outdoor events and gym time away from peak hours; common-area indoor air (close windows at peaks, air purifiers in clubhouse); check on elderly residents; notice-board and WhatsApp advisory.
- **School:** move PE/sports and assembly indoors above Satisfactory-to-Moderate thresholds (set explicit thresholds); skip outdoor activity when AQI is Poor or worse; bus and parent-vehicle idling ban at the gate; ventilation guidance (when to open windows); allow sensitive students to stay in; communicate with parents; inhaler/medication readiness.

Flag thresholds for activity advice as "suggested defaults" tied to the CPCB health-impact text, not as medical guidance, and add a disclaimer.

## 6. UI (single Streamlit page, left audience selector, tabs)

Sidebar: **Audience** (Ward officer | Housing society | School), **City/area**, **Languages** (multi-select), **Reading level**, "Load sample week 1/2", API key input if no env var.

Tabs:

1. **Data** — paste/upload, preview, detected schema (editable mapping), cleaning log.
2. **Quality** — station reliability table, flag counts, a "what we could not use" list. Flagged rows are highlighted.
3. **Insights** — AQI per station/ward (colour-coded by CPCB category), trend chart, hotspot cards with finding IDs; click a finding to show the evidence rows.
4. **Actions** — ranked actions for the selected audience, with citations and effort chips. Tab to switch persona and see the plan change.
5. **What changed** — vs the previous brief: AQI deltas, category transitions, new/resolved hotspots, data quality change, actions added/dropped.
6. **Briefing note** — internal note (copy/download Markdown).
7. **Citizen advisory** — language × reading-level grid, per-group care blocks, copy as WhatsApp-sized text.

Demo hook: show a flagged-bad reading, click its finding, and show that the AI's sentence changed to a hedged one because of it.

## 7. 60-minute execution plan (10:30–11:30)

| Time | Goal | Output |
|---|---|---|
| 0–5 | Setup: Gemini API key from AI Studio, `uv init`, install `streamlit pandas pydantic google-genai openpyxl pyyaml`; create repo + layout | App runs "hello" |
| 5–15 | `aqi.py` (breakpoints + tests) + `clean.py` (sentinels, units, dates) + generate/hand-write messy sample week 1 & 2 | Clean tidy table from sample |
| 15–25 | `quality.py` + `analytics.py` → Findings JSON with IDs/evidence | Printed findings |
| 25–40 | `llm/client.py` + prompts P2, P3, P5; render Insights, Actions, Advisory tabs | Real Gemini output with citations |
| 40–48 | `diff.py` + brief history + "What changed" tab | Week 2 compared to week 1 |
| 48–54 | Verifier (ID + number check), internal note (P4), second language | Verified text |
| 54–58 | Deploy: `gcloud run deploy --source .` (or `streamlit run` + tunnel if Cloud Run is slow), env var for key | Shareable URL |
| 58–60 | Pitch rehearsal using the sample files | 2-minute story |

Cut order if behind schedule (drop from the bottom): regional language → reading-level variants → Gemini mapper (use rules only) → P6 Gemini verifier (keep the code checks) → charts. Never cut: quality flags, citations, persona actions, previous-brief diff, bilingual advisory. Those are the slide's explicit requirements.

Vibe-coding tip: paste Section 8 below into Antigravity as the master prompt, then iterate module by module. Ask for tests on `aqi.py` first; everything else depends on it.

## 8. Master build prompt (paste into Antigravity)

> Build "Civic Air Quality Briefing Studio" as a Streamlit app (Python 3.12, pandas, pydantic v2, google-genai). Purpose: ward officers, housing societies and schools paste or upload messy air-quality tables and get plain-language trends, hotspots, prioritized local actions for their role, a comparison with the previous brief, an internal briefing note, and a multilingual citizen advisory.
>
> Hard rules: (1) Code computes all numbers (CPCB AQI sub-indices, worst-pollutant overall AQI, trends, hotspot ranking, diffs). Gemini only interprets and writes. (2) Every finding has an ID (F1…), a formula and evidence row IDs; every generated sentence must cite finding IDs; a verifier rejects unknown IDs and numbers not in the findings. (3) Detect and flag data problems (missing, sentinel values, negatives, impossible values, unit mismatch/ambiguity, stuck sensors, spikes, duplicates, partial days <16h, low-coverage stations) and show them; unreliable data lowers confidence and changes wording. (4) Gemini output uses structured JSON schemas via pydantic. Pin the model ID in one constant, with a fallback. (5) Prompts live in `llm/prompts/*.md`, one job per prompt.
>
> Modules: `core/ingest|clean|quality|aqi|analytics|diff|verify`, `llm/client|schemas`, `data/action_library.yaml`, sample messy data for two weeks, and `tests/test_aqi.py` covering every CPCB breakpoint boundary. UI tabs: Data, Quality, Insights, Actions (persona selector), What changed, Briefing note, Citizen advisory (languages: English, Hindi + one regional; reading levels simple/standard/technical; care blocks for children, older adults, sensitive groups). Persist each brief as JSON so the next upload diffs against it. Add a Dockerfile for Cloud Run. Start with `core/aqi.py` and its tests.

## 9. Stretch / differentiators (only after P0)

- Map view with station pins (folium/pydeck) if lat/lon is present.
- Back-translation check for the citizen advisory.
- Text-to-speech or voice-note script for older adults (Gemini TTS), if time.
- Image upload (photo of a printed sensor log) read via Gemini vision. This nicely fits "messy" data and the photo-of-a-slide spirit of the event.
- Anomaly explanations like "co-located stations disagree by >40%, check calibration".
- Export a one-page PDF for notice boards.
- Evaluation harness: a small file of known-answer cases (the messy sample with known planted faults) to prove detection rate. A good talking point for the "prompt quality" judges.

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Wrong AQI maths | Tests on every boundary; AQI logic in code only; breakpoints double-checked against CPCB doc |
| Gemini hallucinates numbers/causes | Grounding contract, code number-matcher, hedged-cause rule, verifier |
| Model ID changed or quota hit | Single constant + fallback list. Fetched from the model docs on 2026-11-14: stable `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`; check again at the venue. Retry with backoff |
| Wi-Fi slow at venue | Cache sample-run outputs as fixtures; "demo mode" replays a saved run if API fails |
| Structured output rejected (400) | Keep schemas shallow, few optionals/enums; validate with pydantic client-side; `minLength`/`maxLength` etc. are not enforced by the API |
| Translation quality | Fixed glossary (AQI categories, place names), keep numbers untranslated, optional back-translation |
| Health-advice liability | Tie advice to CPCB category text; "not medical advice" footer; no dosing/diagnosis |
| API key leakage | Env var / Streamlit secrets only; never in repo or the deployed image |
| Over-scoping | The cut order in section 7 |

## 10b. Whole-app resilience (from the full Pro-Tip)

The slide says functional checks evaluate the app as a whole, reward connected workflows, and that a well-rounded app survives one isolated broken flow. Evaluators will click through it "as intended", so:

1. **Zero-setup start.** No login, no API-key prompt for the evaluator. The key is a server-side env var. Opening the URL lands on a working screen with a "Load sample data" button.
2. **One connected happy path.** Data → Quality → Insights → Actions → What changed → Briefing note → Advisory. Each step's output feeds the next, and the UI says what to do next.
3. **Isolate failures.** Every Gemini stage is wrapped so one failure shows a clear message plus a deterministic fallback (rule-based summary, template advisory) instead of a crash. The tabs that don't need the LLM (Data, Quality, Insights numbers) always work.
4. **Demo mode.** If the API is down or rate-limited, replay a saved run for the sample data so every tab still has content.
5. **Handle bad input.** Empty paste, wrong delimiter, a file with no pollutant columns, a single row, all-missing data, an unknown language: each gives a friendly error, never a stack trace.
6. **First-brief case.** "What changed" must work when there is no previous brief (say so, and offer the sample week 1 as a baseline).
7. **Smoke test.** `tests/smoke.py` runs the sample through the whole pipeline (LLM mocked) and asserts every tab's data exists. Run it before every deploy.
8. **Stable deploy.** Deploy early (even at 45 min), redeploy after changes, and keep the URL working. Cloud Run min-instances=1 avoids cold starts during judging.

## 11. Pitch outline (5–7 min on stage; 2 min if asked early)

1. **Problem (30 s):** ward teams have sensor spreadsheets nobody can act on; a society, a school and a ward office need different actions from the same data.
2. **Demo (3 min):** upload the messy sheet, show cleaning and flags, show the hotspot with its evidence trail, switch persona and watch actions change, upload week 2 and show "what changed", then the Hindi advisory for older adults.
3. **How it works (1.5 min):** "Code computes, Gemini explains, a verifier checks." Show the prompt files and schemas.
4. **Trust (30 s):** flagged data lowers confidence and the wording hedges. Show a sentence before and after.
5. **Next (30 s):** live feeds (CPCB/OpenAQ), WhatsApp delivery, ward dashboards.

## 12. Environment notes for this machine

This repo has a `devenv.nix` with no Python yet. Declaratively, add to `devenv.nix`:

```nix
languages.python = { enable = true; uv.enable = true; };
```

and keep dependencies in `pyproject.toml` via `uv`. For a one-off test without touching the system: `nix shell nixpkgs#python312 nixpkgs#uv`. `gcloud` is not installed; for deployment either `nix shell nixpkgs#google-cloud-sdk` temporarily (add `pkgs.google-cloud-sdk` to the dotfiles or devenv `packages` for a permanent fix), or use the event's provided deploy path. Long runs (Streamlit server) go in a named tmux session.

## Sources

- Event rules and judging: https://promptwars.in/promptwars.html
- CPCB National AQI breakpoints: https://ies.gov.in/arthapedia/concept/national-air-quality-index (secondary; verify against CPCB)
- WHO 2021 guidelines: https://www.ncbi.nlm.nih.gov/books/NBK574591/ and https://apps.who.int/iris/handle/10665/345329
- GRAP revisions (Nov 2025) and CAQM orders: https://caqm.nic.in/
- Gemini models and structured output: https://aistudio.google.com/docs/models, https://aistudio.google.com/docs/structured-output
