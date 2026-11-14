# Civic Air Quality Briefing Studio

Turns messy air-quality tables (pasted or uploaded) into plain-language trends and hotspots, prioritized
local actions for a ward officer / housing society / school, a "what changed since last brief" comparison,
an internal briefing note, and a multilingual citizen advisory with extra care for children, older adults
and sensitive groups. Every finding points back to the source rows; data gaps and unreliable readings are flagged.

**How it works:** code computes every number (CPCB AQI, trends, hotspots, diffs, data-quality flags);
Gemini only interprets and writes, must cite finding ids (`[F3]`), and a verifier checks the citations and
numbers. With no API key the app still works in an offline fallback mode.

## Run it (needs only Python 3.11+)

### Windows
1. Install Python 3.11 or newer from <https://www.python.org/downloads/> and tick **Add python.exe to PATH**.
2. Double-click **`run.bat`** (or in a terminal: `python run.py`).
3. The first start creates a local `.venv` and installs dependencies (1-2 minutes, internet needed once).
   Your browser opens at <http://localhost:8501>.

### macOS / Linux
```
./start.sh            # or: python3 run.py
```

Options: `python run.py --port 8700`, `--no-browser`, `--host 0.0.0.0` (share on your network),
`--reinstall`, `--check` (self-check), `--test` (run the test suite).

### Gemini key (optional, recommended)
Copy `.env.example` to `.env` and set `GEMINI_API_KEY=...` (get a key at <https://aistudio.google.com/apikey>).
Or set the environment variable: `set GEMINI_API_KEY=...` (cmd), `$env:GEMINI_API_KEY="..."` (PowerShell).
Without a key the app shows an "offline fallback" badge and uses built-in templates (English and Hindi advisories).
The key stays on your machine and is never shown in the UI.

### Try it
Click **Load sample week 1**, explore the tabs, then **Load sample week 2** to see **What changed**. The sample
files (`data/sample_week1.csv`, `data/sample_week2.csv`) are deliberately messy; `data/sample_manifest.md`
lists every planted fault.

## Using your own data
Paste a table or upload CSV / TSV / XLSX (Excel "CSV UTF-8" and legacy encodings are fine). Wide or long layouts,
unit-in-header or unit-in-cell, `NA` / `--` / `-999` sentinels, decimal commas and mixed date formats are handled
and every cleaning step is logged. Short tables give an *indicative* (low-confidence) AQI instead of nothing.

## Project layout
```
app.py            Streamlit UI            core/   parsing, cleaning, quality, AQI, findings, diff, verify
ui/               theme + HTML components llm/    Gemini client, prompts (llm/prompts), stages, fallbacks
data/             sample data, actions    tests/  pytest suite       run.py / run.bat / start.sh  launchers
```
Prompts live in `llm/prompts/*.md` (one job each, versioned). `python -m llm.eval` scores the offline pipeline
against the planted faults.

## Deploy
- **Anywhere with Python:** `python run.py --host 0.0.0.0 --port 8080 --no-browser`.
- **Docker / Cloud Run:** a `Dockerfile` is included (`docker build -t air-briefing . && docker run -p 8080:8080 -e GEMINI_API_KEY=... air-briefing`).
- **Pre-warm the demo cache** (replays saved Gemini runs if the network fails): `python scripts/prewarm_cache.py` with a key set.

## Troubleshooting
| Problem | Fix |
|---|---|
| `python` not found (Windows) | Reinstall Python and tick "Add python.exe to PATH", or use `py -3 run.py` |
| pip install fails | Check internet/proxy: `.venv\Scripts\python -m pip install --proxy http://host:port -r requirements.txt` |
| Port busy | The launcher picks the next free port and prints it; or `--port 8700` |
| Windows Firewall prompt | Choose "Private networks" (only needed with `--host 0.0.0.0`) |
| Odd characters in the console | The launcher sets UTF-8 mode; use Windows Terminal rather than old cmd if needed |
| Want a clean reinstall | Delete the `.venv` folder and run again |

## Development
`python run.py --test` (installs dev deps, runs pytest). NixOS: `devenv shell` provides everything
(`./start.sh` uses it automatically; set `AIR_PLAIN=1` to bypass).
