#!/usr/bin/env python3
"""Extra demo files in other shapes, derived deterministically from data/sample_week2.csv.

    data/demo/sample_week1.xlsx          Excel version of week 1 (title rows kept)
    data/demo/week3_long_layout.csv      long layout: one row per reading, unit in a column, ISO timestamps
    data/demo/quick_paste_school.txt     small tab-separated table to paste (gives an indicative AQI)
    data/demo/ward_export_semicolon.csv  semicolon-separated, decimal commas, Windows-1252 encoding

Run from the repo root: .venv/bin/python scripts/make_demo_extras.py
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA, OUT = ROOT / "data", ROOT / "data" / "demo"
OUT.mkdir(exist_ok=True)


def read(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA / name, skiprows=2, dtype=str)


# 1. Excel copy of week 1, keeping the two title rows above the header like the CSV.
w1_lines = (DATA / "sample_week1.csv").read_text(encoding="utf-8").splitlines()
w1 = read("sample_week1.csv")
with pd.ExcelWriter(OUT / "sample_week1.xlsx") as xw:
    pd.DataFrame([[w1_lines[0]], [w1_lines[1]]]).to_excel(xw, index=False, header=False, startrow=0, sheet_name="Hourly")
    w1.to_excel(xw, index=False, startrow=2, sheet_name="Hourly")

# 2. Long layout for a third week: shift week 2 by 7 days, one row per reading, unit in its own column.
w2 = pd.read_csv(DATA / "sample_week2.csv", dtype=str)
station_col, date_col, time_col = w2.columns[0], w2.columns[2], w2.columns[3]
stamp = w2[date_col].str.strip() + " " + w2[time_col].str.strip()
iso = w2[date_col].str.strip().str.match(r"^\d{4}-")
ts = pd.Series(pd.NaT, index=w2.index, dtype="datetime64[ns]")
ts[iso] = pd.to_datetime(stamp[iso], errors="coerce")                                  # 2026-11-08 00:00
ts[~iso] = pd.to_datetime(stamp[~iso], format="mixed", dayfirst=True, errors="coerce")  # 08/11/2026 12:00 AM
ts = ts + pd.Timedelta(days=7)
NAMES = {"PM2.5": "PM2.5", "PM10": "PM10", "NO2": "NO2", "SO2": "SO2", "O3": "O3", "CO": "CO"}
UNITS = {"CO": "mg/m3"}
rows = []
for col in w2.columns[4:]:
    name = NAMES[col.split(" ")[0].upper()]
    for i, v in w2[col].items():
        if pd.isna(ts[i]):
            continue
        rows.append((w2.at[i, station_col], ts[i].strftime("%Y-%m-%dT%H:%M"), name, v, UNITS.get(name, "ug/m3")))
pd.DataFrame(rows, columns=["station", "timestamp", "pollutant", "value", "unit"]).to_csv(OUT / "week3_long_layout.csv", index=False)

# 3. Tiny paste-able table: one school-zone sensor, 12 hours (too short for an official CPCB AQI, so it shows an indicative one), a few gaps. Tab separated.
small = w1[w1[w1.columns[0]] == "Karve Road"].head(12).copy()
small.iloc[:, 0] = "School zone sensor"
small.iloc[3, 4] = ""
small.iloc[7, 5] = "NA"
small.iloc[11, 6] = "--"
small.to_csv(OUT / "quick_paste_school.txt", sep="\t", index=False)

# 4. Semicolon + decimal comma + cp1252 (typical of a regional Excel export).
eu = w2[w2[station_col].isin(["Pashan", "Katraj", "Hadapsar"])].copy()
for c in eu.columns[4:]:
    eu[c] = eu[c].str.replace(".", ",", regex=False)
eu.to_csv(OUT / "ward_export_semicolon.csv", sep=";", index=False, encoding="cp1252")
print("wrote", *sorted(p.name for p in OUT.iterdir()))
