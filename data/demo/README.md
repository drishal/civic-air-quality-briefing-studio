# Demo files

Upload these in the **Data** tab (or paste the `.txt`). All are fictional Pune data, deliberately messy.
The main pair is one level up: `data/sample_week1.csv` then `data/sample_week2.csv` (what-changed demo).

| File | Shows | Expect |
|---|---|---|
| `sample_week1.xlsx` | Excel upload, two title rows above the header, mixed units and date formats | Same result as `sample_week1.csv`: Hadapsar Very Poor (305), Nigdi flagged unreliable |
| `week3_long_layout.csv` | **Long layout** (one row per reading), unit in a column, ISO timestamps, 15-17 Nov | Upload after week 2 for a third comparison: Bhosari and Hadapsar Very Poor |
| `quick_paste_school.txt` | **Paste** a short table (12 hours, one sensor, gaps `""`, `NA`, `--`) | An **indicative** low-confidence AQI instead of an official one, with the gaps flagged |
| `ward_export_semicolon.csv` | Semicolon separator, **decimal commas**, Windows-1252 encoding, 3 stations | Parsed cleanly, Hadapsar Very Poor (338) |

Suggested full demo order: `sample_week1.csv` -> `sample_week2.csv` (What changed) -> `week3_long_layout.csv`
(another change) -> paste `quick_paste_school.txt` (short-table honesty) -> `sample_week1.xlsx` (Excel).

Regenerate with `python scripts/make_demo_extras.py` (deterministic).
