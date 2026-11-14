# Opus review: Civic Air Quality Briefing Studio

Reviewer: Opus 5.5 acting as product/design reviewer and hackathon strategist. Read-only pass over PLAN.md, CONTRACT.md,
UI_BRIEF.md, `core/*`, `llm/*`, `app.py`, `ui/*`, the sample data and manifest, and the screenshots in
`/tmp/claude-1000/uipolish-shots/`. I also ran one in-memory pipeline probe (messy long-format, semicolon-delimited,
decimal-comma table; `save=False`, no LLM, nothing written to the project).

## Verdict in one paragraph

The engineering is stronger than most hackathon entries. Code computes every number and Gemini only writes. The
cleaner is solid: my probe of decimal commas, `--`, ppb→µg/m³, mg→µg, a negative value, `12/11/2026 8:00 AM` vs
ISO dates, and "Ward-12 Market" vs "ward 12 market" all came out right. Every finding carries facts, a formula and
evidence row IDs. **The demo currently undersells all of this.** Five problems would hurt on stage:

- The offline/replay path never replays (bug 1).
- First load fires about 7 serial Gemini calls (bug 2).
- A short table pasted by a judge gives "insufficient evidence" on every tab (bug 3).
- The honesty story contradicts itself on screen (bug 4).
- The verifier only reports problems. It never repairs them (bug 5).

Fix those, then build three demo moments: **"Prove it"** (sentence → number → raw cell, plus a red-team injection
the verifier catches), **an honest "What changed"** (an unreliable station can't silently become a "new hotspot";
show which actions changed), and **"Ask the briefing"** (a grounded, persona-aware assistant).

---

## 1. Correct interpretation, clause by clause

| Clause | Status | Evidence / what's wrong |
|---|---|---|
| GenAI as a core component | **OK, but the architecture hides it** | Gemini does P1–P5. In offline mode (every screenshot shows "AI: deterministic fallback"), the text is template output that reads like a debug dump: *"PM2.5 above the WHO 24-hour guideline at 8 of 8 stations: pollutant PM2.5, who 24h limit 15, n stations 8 [F4]"* (`llm/fallback.py` interpretation builder, visible in `desktop-dark-03-3-insights.png`). Judges must see Gemini's prose, never this. |
| Pasted or uploaded tables, messy formats/units/missing readings | **Strong cleaning, weak output on small inputs** | The `core/clean.py` + `core/ingest.py` probe passed. **But** `core/aqi.daily_aqi` only accepts a 24-hour AQI with 16 or more hours of data. My 8-row probe produced `station_aqi = None` for every station, only "gap" findings, the interpretation *"insufficient evidence for a daily AQI category"*, and an advisory category *"Data unavailable"*. A functional checker or judge who pastes a short messy table, the most likely test, gets an empty product. The 16-hour rule is correct CPCB practice, but the product must still say something useful (see bug 3). |
| Summarize trends and likely hotspots in plain language | **OK with Gemini, poor in fallback** | Trends are computed within the period. The diff compares across briefs. The two can disagree on screen: week 2 shows "Swargate: AQI trend up (+14.6 per day)" while the delta card shows Swargate −46 vs last brief. Nothing explains the difference. Label them "within this week" vs "vs last brief". |
| Prioritized actions **suited to who is asking** | **Under-delivered** | Persona changes only the action plan (`llm/stages.plan_actions`) and the note's action section. In the fallback (`desktop-dark-04-4-actions.png`), all 5 actions cite only `[F1]` and share one templated why-now: "the area is Very Poor today, PM2.5 is the dominant pollutant…". None names a station, ward or the peak window, apart from text baked into the library item. The `Action` schema (`llm/schemas.py`) has no `where` field. "Who is asking" should also change the *outputs the persona issues*: a school needs a parent note, an RWA a notice-board/WhatsApp text, a ward officer the internal note. Today only the ward-officer flavour gets a document. |
| Every finding traceable to the data | **Good backbone, buried UX** | Finding → facts → formula → evidence rows works. It sits behind a "Trace a finding" selectbox at the bottom of Insights. The `[F#]` chips only show a hover title. The headline feature takes 4 scrolls and a dropdown to reach. Evidence rows show `value_raw`/`value_std` but not the *cleaning step* applied, which is the most convincing proof of the messy-data handling. |
| Data gaps and unreliable readings clearly flagged | **Contradictory on screen** | Week 1 has 8 sentinels, 3 censored, a negative value, an impossible 2750 spike and a 10-hour Pashan outage. Yet the vitals row reads "100% usable" (99% rounded up), "0 stations not fully reliable", and every station card says "high confidence · reliable". The "2826 unit notes" chip is noise: every plain number carries UNIT_AMBIGUOUS by design, and it drowns out the 35 real flags. In week 2, **Nigdi goes reliable → unreliable, yet the What-changed tab lists Nigdi as a "New hotspot" with no caveat**, and its delta card shows "= −4.6" as if trustworthy (`desktop-dark-08-week2-what-changed.png`). That breaks the plan's own rule (PLAN §4: unreliable stations never headline a hotspot). |
| Compare each new brief with the previous one | **Data diff only, plus a bug** | `core/diff.py` compares station AQI, categories, top-3 membership and reliability labels. Missing: *brief* diff (headline, category, actions added/dropped: PLAN §6 promised "actions added/dropped") and a written "what changed" paragraph on the tab itself. Bug: `new_finding_titles` compares titles that contain numbers (`"Hotspot: Hadapsar at AQI 338"`), so every recurring hotspot and trend shows as "Findings new in this brief" (10 of 10 in the screenshot). Compare on `(type, stations, pollutant)` instead. |
| Internal note + citizen advisory, multiple languages, reading levels | **Present, nominal in places** | Structure is right (one P5 call per language × level). The weaknesses: (a) offline, Marathi/Kannada/etc. become English with a note; (b) the Hindi fallback source line is half English ("डेटा में कमी: Station Pashan has no readings between…"); (c) simple vs standard differ only by bullet count, and the care blocks are identical across levels; (d) nothing checks that translations keep the same numbers and place names (PLAN §5 lists back-translation, never built). |
| Extra care for children, older adults, sensitive people | **Present but city-wide and generic** | Three care blocks exist and stay separate. But `fallback.brief_snapshot` builds the whole city's advisory from the **worst station** (Hadapsar 305). Karve Road residents (124, Moderately Polluted) are told "Older adults should not go out" and "no outdoor school activity". That is over-alarming, and it is a credibility problem if a judge spots it. Care text ignores the data-specific fact the system already has: the **18:00–22:00 peak window**. "Walk before 10 am, not after 6 pm" is the single most useful thing for an older adult, and it is computed but never used. |
| "The assistant" | **Implied and missing** | The statement says "the assistant should compare…". Together with "suited to who is asking", that points to a conversational, persona-aware helper. Build a tightly grounded "Ask the briefing" (wow item 3). Keep it scoped. It should not replace the pipeline. |

---

## 2. The five most damaging weaknesses (fix in this order)

### Bug 1: the demo replay cache never hits (Wi-Fi fallback is broken)
`llm/stages.py` builds every cache key from `brief.model_dump()`, which includes `brief_id` and `created_at`.
`core/analytics.py:857-858` derives both from `now`. Every analysis therefore gets fresh keys, and
`demo_cache.load_run` can never find a previous run. `.demo_cache/` doesn't even exist in the repo. If Gemini or
Wi-Fi fails on stage, you get the robotic template text, not the last good Gemini run.
**Fix (15 min):** key on `brief.model_dump(exclude={"brief_id","created_at"})` (or `source_hash` + previous
`source_hash` + stage params + prompt version). Do the same for the `diff` payload, which contains
`previous_brief_id`. Then pre-warm the cache with one real Gemini run of week 1 → week 2 for all 3 personas,
English+Hindi × simple+standard, and commit `.demo_cache/`. Add `DEMO_MODE=1` to force replay.

### Bug 2: first render fires ~7 serial Gemini calls, with 3 models × 3 retries each
Streamlit runs every tab body on every rerun. After "Load sample", `insights_tab` calls P2, `actions_tab` calls
P3, `note_tab` calls P2+P3+P4, and `advisory_tab` calls P5 four times (2 languages × 2 levels). All of them run
serially behind skeletons, even while the user looks at the Data tab. `client.generate_json` has no request
timeout and retries up to 9 times with backoff. One slow model can stall the screen for minutes, and every
persona radio switch triggers more calls.
**Fix (30 min):** after `analyze()`, prefetch P2, P3 (current persona) and P5 in a `ThreadPoolExecutor`, then
P4 once P2+P3 are done. Add a per-call timeout (`http_options` / 20 s), set `MAX_ATTEMPTS=2`, and only walk to
the next model on 404/429. Generate advisories past the first 2 behind a "Generate" button.

### Bug 3: short or partial tables produce an empty product
See the probe in §1. Any table with fewer than 16 hours per station gets no AQI, no hotspot and an advisory
category of "Data unavailable".
**Fix (30 min):** add an **indicative AQI** to `core/aqi.py`: the sub-index of the mean of the available hours
(and the 8-hour max for O3/CO if present). Mark it `valid=False, indicative=True` and reason "based on N h,
CPCB needs 16 h". Have `analytics.build_brief` use it when no valid day exists, with confidence `low` and a
caveat. Hotspots and advisories then come out hedged. That turns a failure into a demonstration of honesty:
*"indicative only: 3 hours of data"*.

### Bug 4: the honesty story contradicts itself
- `core/diff._top_hotspots` ignores reliability, so unreliable Nigdi becomes a "New hotspot". Fix: carry
  reliability into the diff. Render as **"Nigdi: enters top 3, but its sensor is unreliable (30 h missing,
  stuck 9 h). Verify before acting."** with a warn chip. Grey out the delta card for that station.
- Week 1 reads as clean. Fix: show "99% usable" (floor, not round). Drop the "unit notes" chip from the vitals
  row, or reword it as "2826 readings had no unit, so the standard unit was assumed". Make coverage
  day-sensitive, so Pashan's 10-hour outage day (only 14 valid hours, which is below 16) shows as "use with
  caution" for that day.
- `new_finding_titles` bug (above).

### Bug 5: the verifier is a report, not a guard, and P6 is dead code
`stages.verify_draft` (P6) is never called from anywhere (grep: only defined in `llm/stages.py`). Code checks in
`core/verify.py` run only when the user opens an expander, and they never feed back into generation. The
`_CLAIM_HINTS` list is English-only, so uncited claims in Hindi or Marathi always pass. When `_clean_advisory`
rejects a Gemini advisory for a category mismatch, it silently switches to the template, and the UI shows no
reason. The pitch says "a verifier checks". Make that true and visible: generate → code check (+ P6 for P4/P5)
→ one repair call with the issues listed → re-check → show "✓ verified · 1 issue auto-repaired" with a
before/after diff.

Secondary, but quick:
- City-wide advisory from the worst station (§1). Write per-zone advisories, or at least "Hadapsar & Bhosari:
  Very Poor; elsewhere Moderately Polluted–Poor".
- 49 stale `.briefs/*.json` files and the "Saved briefs: 46" sidebar count look like debris. Clear them before
  the demo.
- Brief IDs like `B20261114T074718Z-ce5770` are shown three times on every screen. Show one short human label
  ("Week of 1 Nov").
- Station cards: every station is "worsening" with monotonic lines, which looks synthetic. Add day-to-day noise
  to the generator, or accept it and don't dwell on the trend chart.

---

## 3. Wow factor: ranked additions

| # | Addition | What the judge sees | Why it impresses | Effort | Risk | Clause |
|---|---|---|---|---|---|---|
| **1** | **"Prove it" trace drawer + red-team injection** | Click any `[F3]` chip in any tab (interpretation, action, note, advisory) and an `st.dialog` opens: the sentence → finding facts and formula → the evidence rows **as they appeared in the raw file** (`value_raw` "85 µg/m3", "-999", "1,250") → the cleaning step per row (`ppb ×1.88`, `SENTINEL → dropped`) → the flag. Next to it, a **"Try to fool it"** button injects a fake sentence ("Hadapsar AQI 412 caused by traffic [F9]") into the draft. The verifier turns red: unsupported number 412, unknown citation F9, cause stated as fact. | It shows grounding in action, and judges remember a demo where they watch the system catch a lie. | M (≈1–1.5 h). Use query-param links `?trace=F3` or a chip-click → `st.session_state` + `st.dialog`. The injection needs only `core/verify.check_text` plus a small regex for causal words. | Low. It is all code, no LLM. | Traceable; flagged readings; prompt elegance |
| **2** | **Honest "What changed" + action diff + narrative** | The top of tab 5 shows a 3-sentence Gemini paragraph with citations: "Since last week, Bhosari moved Poor → Very Poor [F2]. Nigdi's sensor failed, so its apparent hotspot cannot be confirmed [F9]." Below it, a two-column **plan diff** for the current persona: ➕ "Field-check Nigdi sensor" (new, triggered by F9), ➖ "Swargate junction traffic marshals" (dropped: Swargate improved 46 points). Nigdi carries an "unverified hotspot" badge. | It is the only clause most teams fake. Showing that *recommendations* changed, and why, is what an officer actually needs. | S–M (≈1 h). Store the last plan per persona in session; diff by `library_id`. Add a tiny P7 prompt or reuse P2 with a `MODE=change_summary`. | Low–med | Compare with previous; unreliable flagged; persona actions |
| **3** | **"Ask the briefing" grounded assistant** | A chat box above the tabs with persona-specific starter chips. *School*: "Can we hold sports day on Thursday?" *RWA*: "Should we run the DG set tonight?" *Officer*: "Where do I send the sprinkler truck at 7 pm?" Each answer cites `[F#]`, says "insufficient evidence" when appropriate, and refuses medical questions with a pointer to a doctor. The code verifier runs on every answer and shows a ✓ chip. | It fulfils "the assistant" and "who is asking" literally, and it is interactive, which panels love. | M (≈1.5 h). One prompt `p7_ask.md` (FINDINGS + DIFF + PLAN + persona spec + history of up to 4 turns), schema `{answer, cites, confidence, follow_ups[]}`, then `check_text` → one repair. | Med. Live LLM; cache the starter answers. | "Assistant"; who is asking; traceable |
| 4 | **Prompt cockpit tab** ("How this was made") | A pipeline diagram P1→P6 with, per stage: prompt file + version, schema name, temperature, model used, latency, tokens, cache hit/live/fallback, verifier result. Plus an **eval scoreboard** from the manifest: "planted faults detected X/N, citation validity, unsupported numbers, hedging on low-confidence findings" (fill with real numbers from the harness). | It scores "prompt quality / architectural elegance" directly and gives the pitch's architecture line something to point at. | S–M (≈45 min) if the stage wrapper records a `StageTrace` per call. | Low | Prompt quality |
| 5 | **Persona triptych** | One button, "Compare audiences": three columns (Ward officer / Housing society / School) with the top 3 actions each, *all citing the same F1/F2*, each column with its own "issue this" document (internal note / notice-board text / parent letter). | Shows "who is asking" in one glance instead of a radio toggle. | S (≈40 min). Three P3 calls in parallel (already cached individually); the RWA and school docs can be P5 with an `ISSUER` param. | Low | Who is asking |
| 6 | **Peak-window, per-zone care advisory** | The advisory says "Hadapsar, Bhosari: Very Poor. Walks before 10 am, not 6–10 pm [F7]" and "Karve Road: Moderately Polluted. Normal activity; sensitive people take it easy in the evening." Each care block uses a time window drawn from the data. | Data-specific, humane advice. It fixes the over-alarm credibility issue. | S–M (≈45 min). Pass `ZONES` (category per ward) + `PEAK_WINDOW` to P5; add `zones: list[{area, category, line}]` to the schema. | Low | Extra care; plain language |
| 7 | **Back-translation check + read-aloud** | Under the Hindi/Marathi card: "Back-translated ✓: numbers 305, 18:00–22:00 and places match", with one drift highlighted if any. A 🔊 button reads the simple-level advisory aloud using the browser's `speechSynthesis` (`hi-IN`). | Multilingual quality made visible. Audio for older adults or low-literacy readers is a strong emotional beat. | S (≈40 min) for back-translation (P8 + a number/place set compare). S for TTS via an `st.components.v1.html` snippet. | Med: Hindi voice availability depends on the demo laptop, so test it first. | Multiple languages; reading levels; extra care |
| 8 | **Photo of a printed log** (Gemini vision → table) | A judge photographs a printed sheet and it becomes a brief. | Pure wow. | L | High (venue Wi-Fi, OCR errors, time) | Messy data |

**Build first, given about 2 hours:**
0. **Non-negotiable fixes (≈45 min):** cache key + pre-warm (bug 1), parallel prefetch + timeout (bug 2),
   Nigdi caveat + title-diff fix (bug 4). Add indicative AQI (bug 3) only if a functional check is likely; it
   probably is, so do it if possible.
1. **"Prove it" drawer + red-team injection** (#1).
2. **Honest What-changed + plan diff** (#2).
3. **Ask the briefing**, scoped to starter chips plus free text (#3). If time runs short, drop it and ship #4
   (cockpit) instead: it is cheaper and directly targets the prompt-quality criterion.

---

## 4. Five-minute pitch + live demo script

Setup before going on stage: clear `.briefs/`, confirm a pre-warmed `.demo_cache/`, keep `GEMINI_API_KEY` set,
choose Audience = Ward officer, languages English+Hindi, levels simple+standard. Open the app on the start
screen with a second tab already at week 2 in case of trouble.

| Time | Beat | Exact clicks | Line |
|---|---|---|---|
| 0:00–0:30 | **Problem** | Start screen | "A ward officer in Pune gets a sensor spreadsheet every week. It's full of -999s, ppb vs µg/m³, a dead sensor. A school, a housing society and a ward office all need different things from it. We turn that sheet into a brief you can act on, where every claim is traceable." |
| 0:30–1:15 | **Messy in, clean out** | Click **Load sample week 1** → tab **1 · Data**. Open the mapping table and the cleaning log. | "Two title rows, three date formats, NO2 in ppb, CO in µg/m³ with thousands separators. Gemini proposes the column mapping. Code validates it, converts units and flags 35 bad readings. The model never does arithmetic." |
| 1:15–2:00 | **Insight with a footnote** | Tab **3 · Insights**: hero 305 Very Poor, read the Gemini headline, **click the `[F1]` chip** → Prove-it drawer. | "Every sentence ends in a finding ID. Click it: here's the number, the formula, and the exact rows in the original file, including the -999 we threw out and why." |
| 2:00–2:30 | **The wow moment** | In the drawer, click **Try to fool it**. | "Let's make the AI lie: AQI 412, caused by traffic. The verifier catches the unsupported number, the fake citation and the causal claim. Nothing reaches a citizen without passing this check." |
| 2:30–3:15 | **Who is asking** | Tab **4 · Actions** → toggle **Ward officer → School** (or the triptych). | "Same findings, different powers. The ward officer gets sweeping at the 6–10 pm peak. The school moves PE indoors and stops buses idling at the gate. Every action comes from a curated library and cites its trigger, so the model can't invent legal powers." |
| 3:15–4:00 | **What changed + honesty** | Click **Load sample week 2** → tab **5 · What changed**. | "One week later: Bhosari crossed into Very Poor. Look at Nigdi. It looks like a new hotspot, but its sensor failed, so we refuse to call it one and the top action becomes 'field-check the Nigdi sensor'. The plan diff shows which recommendations were added and dropped, and why." |
| 4:00–4:30 | **Citizen advisory** | Tab **7 · Citizen advisory**: Hindi simple card, 🔊 if built. (Optionally Ask: "Can the school hold sports day Thursday?") | "Hindi and English at a grade-5 reading level. Separate guidance for children, older adults and people with asthma, tied to the CPCB category and the evening peak. Numbers and place names are locked across languages." |
| 4:30–5:00 | **Architecture line** | Cockpit tab, or show `llm/prompts/` in the editor. | **"Code computes, Gemini explains, code verifies. Six single-purpose prompts, each with a JSON schema contract, a shared grounding clause and citations constrained to real finding IDs. Every output is checked and repaired before you see it. Scored against a dataset with N planted faults (use the real count from `data/sample_manifest.md`)."** |

**Fallback if Wi-Fi or Gemini fails:** set `DEMO_MODE=1` (or rely on the fixed cache replay; no visible
difference). The badge says "replayed Gemini run", which is honest. If the app itself won't load, play a 90-second
screen recording made after the fixes (record it as soon as bug 1 is fixed). Phone hotspot as a network backup.
Never demo the template-fallback text: in its current state it is the weakest thing in the app.

---

## 5. Prompt architecture: concrete upgrades

1. **Constrain citations at decode time (the strongest architecture point).** Build the response schema per
   call: `cites: list[Literal["F1",…,"Fn"]]` via `pydantic.create_model`, and do the same for
   `library_id: Literal[<persona library ids>, "suggested_new"]`. Gemini structured output honours enums, so
   invented citations become impossible rather than filtered later. `_clean_plan` / `_clean_interpretation`
   become a safety net. Pitch line: "the model literally cannot cite a finding that doesn't exist."
2. **Stop letting the model choose facts that code already knows.** `Advisory.category` is model-written and
   then rejected on mismatch (`_clean_advisory`), which wastes a whole call. Remove `category` and `language`
   from the schema and inject them in code. The same goes for `persona` in ActionPlan.
3. **One shared grounding contract.** P2–P5 each restate the contract with slight drift: P5 forbids pollutant
   codes in "simple", P2 says "no acronyms without expansion", and so on. Move it to
   `llm/prompts/_contract.md` and include it via a `{CONTRACT}` placeholder in `load_prompt`. Each prompt then
   keeps only its job-specific rules.
4. **Versioned front-matter per prompt.** Add `id`, `version`, `temperature`, `schema`, `inputs` and `changelog`
   in YAML front-matter to each `.md`. `load_prompt` parses it, `client.generate_json` reads its temperature
   from it, the version goes into the cache key and into each output (`generated_by: "p3_actioner@1.3"`), and
   the cockpit renders it.
5. **One few-shot pair per prompt, focused on the hard cases.** P2: a low-confidence finding written correctly
   ("cannot be confirmed from this data…") vs incorrectly. P3: a good action with a station, time window and
   citation vs a generic one. P5: a Hindi simple-level example using fixed CPCB Hindi category terms
   (`GLOSSARY`), so translations stay consistent between runs.
6. **Make persona a data spec, not code constants.** Move `PERSONA_BRIEFS` out of `llm/fallback.py` into
   `data/personas.yaml`: authority, limits, channels (notice board, WhatsApp, parent circular), decision horizon
   and vocabulary. Feed it to P3, P4, P5 (as `ISSUER`) and P7.
7. **Richer action contract.** Add `where: list[str]` (stations/wards from FINDINGS), `when` (the peak window
   finding), `trigger_finding` (must be a hotspot, pattern or gap finding, not just F1), and `expected_signal`
   ("PM2.5 at Hadapsar 18–22 h should fall in next week's brief"). That last field links neatly to the
   what-changed diff next week.
8. **Self-critique + repair loop as an explicit stage.** Pipeline: draft → `core/verify.check_object` + P6
   (for P4/P5 only) → if issues, one call to `p6b_repair.md` with `{DRAFT, ISSUES}` → re-verify → ship or fall
   back. Log the before/after and show "auto-repaired" in the UI. Make `_CLAIM_HINTS` language-aware, or rely
   on P6 for non-English text.
9. **Measurable reading levels.** Check the "simple" rules (≤12 words per sentence, no pollutant codes, ≤4
   items) in code after generation, and send failures through the repair loop. Show "simple ✓ avg 8.4
   words/sentence" on the card.
10. **Trim the context.** `_quality_json` sends the full `QualitySummary`, including the `cleaning_log`. Send
    stations, gaps and flag_counts only. Pass the snapshot fields P5 needs, not all findings. This means fewer
    tokens and less distraction, and the cache keys stay stable.
11. **Eval harness as a deliverable.** Add `tests/eval_prompts.py` (and a cockpit panel). Run over week 1, week
    2 and two adversarial inputs (an all-low-confidence table; a 6-row paste). Assert: citation validity,
    unsupported-number count = 0, hedge words present for every low-confidence finding cited, advisory
    category = code category, script check (Devanagari ratio > 0.6 for Hindi/Marathi), reading-level stats, and
    planted-fault recall against `data/sample_manifest.md`. Print a scoreboard and quote it in the pitch.
12. **Show the mapper earning its keep.** On the Data tab, show "Gemini mapping vs rule mapping: agree on 9/10
    columns; Gemini caught `NO2_ppb` header unit, rules didn't". Surface the mapper `issues` in the Quality tab.
    That makes P1 visible as AI doing real work on messy data, which is the clause judges look for.
