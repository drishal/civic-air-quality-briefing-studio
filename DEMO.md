# Demo checklist (5 minutes)

Source: OPUS_REVIEW.md section 4, trimmed to what the app does. Fault count: the two sample files carry about
65 planted faults (see `data/sample_manifest.md`; run `python -m llm.eval` for the live detection score).

## Before going on stage
- [ ] `GEMINI_API_KEY` in `.env` (offline fallback works but is the weakest thing to show).
- [ ] `python scripts/prewarm_cache.py` once with the key, so a dead network replays real Gemini runs.
- [ ] Clear `.briefs/` (sidebar "Clear history"), start with `./start.sh` or `run.bat`.
- [ ] Audience = Ward officer, languages English + Hindi, levels simple + standard.
- [ ] Second browser tab at the app, a screen recording as the last-resort backup, phone hotspot ready.

## Script
| Time | Beat | Click | Say |
|---|---|---|---|
| 0:00 | Problem | start screen | A ward officer gets a sensor spreadsheet nobody can act on. A school, a housing society and a ward office need different things from it. |
| 0:30 | Messy in, clean out | **Load sample week 1** > Data tab: mapping + cleaning log | Two title rows, three date formats, NO2 in ppb, CO in µg/m³ with thousands separators. Gemini proposes the mapping, code validates, converts and flags. The model never does arithmetic. |
| 1:15 | Insight with a footnote | Insights tab, click a `[F#]` chip / Prove it | Every sentence ends in a finding id. Here are the number, the formula and the original rows, including the -999 we dropped and why. |
| 2:00 | Wow: fool it | Try to fool the verifier | Make the AI lie: "AQI 412, caused by traffic [F99]". The verifier catches the fake citation and the unsupported number. |
| 2:30 | Who is asking | Actions tab, switch Ward officer > School | Same findings, different powers. Actions come from a curated library, cited to findings, so no invented legal powers. |
| 3:15 | What changed + honesty | **Load sample week 2** > What changed | Bhosari crossed into Very Poor. Nigdi looks like a new hotspot, but its sensor failed, so it is marked unverified and the action is to field-check it. |
| 4:00 | Citizen advisory | Advisory tab, Hindi simple | Grade-5 reading level, separate care for children, older adults and sensitive groups, tied to the CPCB category. Numbers and place names stay identical across languages. |
| 4:30 | Ask + architecture | Ask tab: "Can I trust Nigdi's numbers?"; Prompt cockpit | Code computes, Gemini explains, code verifies. Single-purpose prompts with JSON schemas, citations restricted to real finding ids, outputs checked and repaired. |

## If something breaks
- Gemini or Wi-Fi down: the app replays cached runs (badge says replayed) or shows template text; keep going, the numbers are unaffected.
- App will not start: `python run.py --check`, then `python run.py --reinstall`.
- Last resort: the screen recording.
