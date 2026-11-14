#!/usr/bin/env python3
"""Generate the deliberately messy Pune sample data for the Civic Air Quality
Briefing Studio:

    data/sample_week1.csv      2026-11-01..03, 8 stations, hourly, wide layout
    data/sample_week2.csv      2026-11-08..10, same stations, second period
    data/sample_manifest.md    every planted fault with verified row ids R<n>

Deterministic: one seeded numpy generator with a fixed draw order, so a re-run
reproduces the files byte-for-byte. After writing, every planted fault is
verified by re-reading the CSVs with pandas; the manifest is written from those
verified row ids only. Run from the repo root:

    .venv/bin/python scripts/make_sample_data.py
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SEED = 20261101

STATIONS = ["Bhosari", "Hadapsar", "Karve Road", "Katraj", "Nigdi", "Pashan",
            "Shivajinagar", "Swargate"]
WARDS = {"Bhosari": "Ward 15", "Hadapsar": "Ward 12", "Karve Road": "Ward 5",
         "Katraj": "Ward 7", "Nigdi": "Ward 14", "Pashan": "Ward 2",
         "Shivajinagar": "Ward 3", "Swargate": "Ward 9"}
POLLUTANTS = ["PM2.5", "PM10", "NO2", "SO2", "O3", "CO"]
COL = {p: 4 + i for i, p in enumerate(POLLUTANTS)}     # cell column index in the CSV
NO2_UG_PER_PPB = 1.88
O3_UG_PER_PPB = 1.96
SPIKE_LIMIT = {"PM2.5": 1500, "PM10": 2500, "O3": 1000}  # > limit => OUT_OF_RANGE

# Multiplicative hour-of-day shapes, normalised so the 24 h mean equals the
# daily target. PM: morning (07-09) + evening (18-22) peaks. O3: midday peak.
# CO/NO2: traffic-shaped. SO2: nearly flat.
SHAPES = {
    "PM2.5": [0.85, 0.80, 0.76, 0.74, 0.75, 0.82, 1.00, 1.30, 1.48, 1.32, 1.10, 0.95,
              0.86, 0.80, 0.78, 0.80, 0.86, 1.02, 1.28, 1.47, 1.55, 1.48, 1.28, 1.02],
    "NO2": [0.70, 0.62, 0.55, 0.52, 0.55, 0.68, 0.95, 1.25, 1.35, 1.20, 1.05, 0.95,
            0.88, 0.85, 0.84, 0.86, 0.92, 1.05, 1.25, 1.38, 1.40, 1.30, 1.10, 0.85],
    "SO2": [0.90, 0.88, 0.86, 0.85, 0.86, 0.90, 0.98, 1.05, 1.10, 1.10, 1.08, 1.05,
            1.03, 1.02, 1.02, 1.04, 1.06, 1.08, 1.06, 1.04, 1.02, 1.00, 0.97, 0.93],
    "O3": [0.30, 0.25, 0.22, 0.20, 0.22, 0.30, 0.45, 0.62, 0.78, 0.92, 1.05, 1.15,
           1.25, 1.30, 1.32, 1.28, 1.15, 0.95, 0.75, 0.60, 0.50, 0.43, 0.38, 0.33],
    "CO": [0.75, 0.68, 0.62, 0.60, 0.62, 0.72, 0.98, 1.28, 1.38, 1.25, 1.08, 0.95,
           0.88, 0.85, 0.84, 0.86, 0.92, 1.08, 1.32, 1.48, 1.50, 1.40, 1.18, 0.90],
}
SHAPES["PM10"] = SHAPES["PM2.5"]
SHAPE_MEAN = {p: float(np.mean(v)) for p, v in SHAPES.items()}

POINT_KINDS = {"SENTINEL", "MISSING_BLANK", "CENSORED", "NEGATIVE", "SPIKE"}
DATE_RX = {"ddmm": r"^\d{2}/\d{2}/2026$", "iso": r"^2026-11-\d{2}$",
           "dash": r"^\d{2}-\d{2}-2026$"}
TIME_12H_RX = r"^\d{1,2}:\d{2} [AP]M$"


def hours_list(a: int, b: int) -> list[int]:
    """Inclusive hour range."""
    return list(range(a, b + 1))


# ------------------------------------------------------------------ week 1
W1_PLANTS = [
    dict(kind="SENTINEL", station="Nigdi", day=0, hours=[4], pollutant="SO2", text="NA"),
    dict(kind="SENTINEL", station="Swargate", day=0, hours=[13], pollutant="NO2", text="--"),
    dict(kind="SENTINEL", station="Shivajinagar", day=0, hours=[8], pollutant="CO", text="ND"),
    dict(kind="SENTINEL", station="Katraj", day=1, hours=[2], pollutant="O3", text="-999"),
    dict(kind="SENTINEL", station="Bhosari", day=1, hours=[21], pollutant="SO2", text="N/A"),
    dict(kind="SENTINEL", station="Karve Road", day=1, hours=[19], pollutant="SO2", text="-"),
    dict(kind="SENTINEL", station="Bhosari", day=2, hours=[6], pollutant="NO2", text="NULL"),
    dict(kind="SENTINEL", station="Pashan", day=2, hours=[17], pollutant="PM10", text="9999"),
    dict(kind="MISSING_BLANK", station="Karve Road", day=2, hours=[5], pollutant="O3", text=""),
    dict(kind="CENSORED", station="Katraj", day=1, hours=[6], pollutant="SO2", text="<5"),
    dict(kind="CENSORED", station="Pashan", day=0, hours=[10], pollutant="NO2", text="<5"),
    dict(kind="CENSORED", station="Karve Road", day=2, hours=[11], pollutant="SO2", text="<5"),
    dict(kind="STUCK", station="Swargate", day=1, hours=hours_list(10, 18),
         pollutant="O3", text="41.0"),
    dict(kind="SPIKE", station="Bhosari", day=0, hours=[19], pollutant="PM2.5", text="2750"),
    dict(kind="NEGATIVE", station="Nigdi", day=2, hours=[3], pollutant="PM10", text="-12.5"),
]
WEEK1 = dict(
    file="sample_week1.csv",
    title_lines=[
        "Pune Municipal Corporation - Continuous Ambient Air Quality Monitoring (CAAQMS)",
        "Hourly export for 01-03 Nov 2026, all stations; units as reported by each station",
    ],
    header=["Station Name", "ward", "Date", "Time", "PM2.5 (ug/m3)", "PM10",
            "NO2_ppb", "so2", "Ozone", "CO (mg/m3)"],
    dates={"ddmm": ["01/11/2026", "02/11/2026", "03/11/2026"],
           "iso": ["2026-11-01", "2026-11-02", "2026-11-03"],
           "dash": ["01-11-2026", "02-11-2026", "03-11-2026"]},
    pm25={"Bhosari": [100, 108, 118], "Hadapsar": [104, 112, 124],
          "Karve Road": [56, 61, 66], "Katraj": [73, 80, 87], "Nigdi": [82, 88, 94],
          "Pashan": [60, 65, 71], "Shivajinagar": [67, 72, 78], "Swargate": [90, 97, 104]},
    pm_ratio={"Bhosari": 2.10, "Hadapsar": 2.05, "Karve Road": 1.90, "Katraj": 2.00,
              "Nigdi": 1.95, "Pashan": 1.85, "Shivajinagar": 1.95, "Swargate": 2.00},
    base={"Bhosari": {"NO2": 38, "SO2": 28, "O3": 30, "CO": 1.7},
          "Hadapsar": {"NO2": 35, "SO2": 16, "O3": 34, "CO": 1.5},
          "Karve Road": {"NO2": 26, "SO2": 9, "O3": 38, "CO": 1.0},
          "Katraj": {"NO2": 30, "SO2": 12, "O3": 30, "CO": 1.2},
          "Nigdi": {"NO2": 33, "SO2": 14, "O3": 28, "CO": 1.1},
          "Pashan": {"NO2": 22, "SO2": 8, "O3": 42, "CO": 0.9},
          "Shivajinagar": {"NO2": 44, "SO2": 12, "O3": 32, "CO": 1.4},
          "Swargate": {"NO2": 48, "SO2": 18, "O3": 40, "CO": 1.6}},
    mult={s: 1.0 for s in STATIONS},
    # ward spelled 3 ways (cycles per day)
    ward_day={"Hadapsar": ["Ward 12", "ward-12", "W12"], "Pashan": ["Ward 2", "ward-2", "W2"]},
    # mixed date formats (some stations switch mid-file)
    date_style={"Hadapsar": ["ddmm", "ddmm", "iso"], "Katraj": "iso",
                "Nigdi": ["ddmm", "ddmm", "dash"]},
    # mixed time formats (two stations log 12-hour times)
    time_style={"Nigdi": "12h", "Karve Road": "12h"},
    plants=W1_PLANTS,
    gap=dict(station="Pashan", day=1, hours=hours_list(2, 11)),   # ~10 h outage
    extra_absent=[],
    duplicate=("Hadapsar", 0, 14),
)

# ------------------------------------------------------------------ week 2
W2_PLANTS = [
    dict(kind="SENTINEL", station="Nigdi", day=0, hours=hours_list(14, 18), pollutant="NO2", text="NA"),
    dict(kind="SENTINEL", station="Nigdi", day=0, hours=[18], pollutant="CO", text="--"),
    dict(kind="SENTINEL", station="Nigdi", day=1, hours=hours_list(0, 9), pollutant="SO2", text="NA"),
    dict(kind="SENTINEL", station="Nigdi", day=2, hours=[5, 6, 7, 8], pollutant="SO2", text="ND"),
    dict(kind="SENTINEL", station="Nigdi", day=2, hours=[5, 7], pollutant="NO2", text="NA"),
    dict(kind="SENTINEL", station="Swargate", day=0, hours=[21], pollutant="PM10", text="9999"),
    dict(kind="SENTINEL", station="Bhosari", day=2, hours=[3], pollutant="NO2", text="NULL"),
    dict(kind="SENTINEL", station="Karve Road", day=1, hours=[7], pollutant="O3", text="--"),
    dict(kind="SENTINEL", station="Pashan", day=0, hours=[13], pollutant="PM2.5", text="NA"),
    dict(kind="SENTINEL", station="Shivajinagar", day=1, hours=[10], pollutant="CO", text="-999"),
    dict(kind="SENTINEL", station="Hadapsar", day=2, hours=[1], pollutant="SO2", text="ND"),
    dict(kind="SENTINEL", station="Katraj", day=1, hours=[15], pollutant="O3", text="NA"),
    dict(kind="MISSING_BLANK", station="Nigdi", day=1, hours=hours_list(14, 17), pollutant="NO2", text=""),
    dict(kind="MISSING_BLANK", station="Nigdi", day=1, hours=hours_list(2, 7), pollutant="PM10", text=""),
    dict(kind="MISSING_BLANK", station="Nigdi", day=1, hours=hours_list(0, 4), pollutant="O3", text=""),
    dict(kind="MISSING_BLANK", station="Nigdi", day=1, hours=hours_list(19, 22), pollutant="CO", text=""),
    dict(kind="MISSING_BLANK", station="Nigdi", day=2, hours=[6], pollutant="CO", text=""),
    dict(kind="MISSING_BLANK", station="Karve Road", day=2, hours=[19], pollutant="SO2", text=""),
    dict(kind="CENSORED", station="Shivajinagar", day=2, hours=[9], pollutant="SO2", text="<5"),
    dict(kind="CENSORED", station="Karve Road", day=0, hours=[4], pollutant="NO2", text="<5"),
    dict(kind="CENSORED", station="Pashan", day=2, hours=[6], pollutant="O3", text="<5"),
    dict(kind="STUCK", station="Nigdi", day=1, hours=hours_list(9, 20), pollutant="PM2.5", text="96.0"),
    dict(kind="STUCK", station="Swargate", day=2, hours=hours_list(14, 22), pollutant="NO2", text="52.0"),
    dict(kind="SPIKE", station="Hadapsar", day=1, hours=[8], pollutant="PM10", text="3100"),
    dict(kind="SPIKE", station="Bhosari", day=0, hours=[14], pollutant="O3", text="1180"),
    dict(kind="NEGATIVE", station="Shivajinagar", day=0, hours=[2], pollutant="O3", text="-3.2 ppb"),
]
WEEK2 = dict(
    file="sample_week2.csv",
    title_lines=[],
    header=["Station", "Ward Name", "date", "time", "PM2.5", "PM10 (ug/m3)",
            "NO2", "SO2", "O3", "CO (mg/m3)"],
    dates={"ddmm": ["08/11/2026", "09/11/2026", "10/11/2026"],
           "iso": ["2026-11-08", "2026-11-09", "2026-11-10"],
           "dash": ["08-11-2026", "09-11-2026", "10-11-2026"]},
    pm25={"Bhosari": [132, 146, 158], "Hadapsar": [138, 152, 166],
          "Karve Road": [48, 52, 56], "Katraj": [64, 70, 74], "Nigdi": [88, 92, 95],
          "Pashan": [46, 50, 54], "Shivajinagar": [58, 63, 67], "Swargate": [80, 86, 90]},
    pm_ratio={"Bhosari": 2.10, "Hadapsar": 2.05, "Karve Road": 1.90, "Katraj": 2.00,
              "Nigdi": 1.95, "Pashan": 1.85, "Shivajinagar": 1.95, "Swargate": 2.00},
    base={"Bhosari": {"NO2": 38, "SO2": 28, "O3": 30, "CO": 1.7},
          "Hadapsar": {"NO2": 35, "SO2": 16, "O3": 34, "CO": 1.5},
          "Karve Road": {"NO2": 26, "SO2": 9, "O3": 38, "CO": 1.0},
          "Katraj": {"NO2": 30, "SO2": 12, "O3": 30, "CO": 1.2},
          "Nigdi": {"NO2": 33, "SO2": 14, "O3": 28, "CO": 1.1},
          "Pashan": {"NO2": 22, "SO2": 8, "O3": 42, "CO": 0.9},
          "Shivajinagar": {"NO2": 44, "SO2": 12, "O3": 32, "CO": 1.4},
          "Swargate": {"NO2": 48, "SO2": 18, "O3": 40, "CO": 1.6}},
    # hotspots worse, improved stations better (secondary pollutants follow the story)
    mult={"Bhosari": 1.30, "Hadapsar": 1.30, "Karve Road": 0.80, "Pashan": 0.80,
          "Swargate": 0.95, "Shivajinagar": 0.95, "Katraj": 0.95, "Nigdi": 1.0},
    ward_day={"Hadapsar": ["Ward 12", "W12", "ward-12"], "Nigdi": ["Ward 14", "ward-14", "W14"]},
    date_style={"Bhosari": "iso", "Hadapsar": ["iso", "iso", "ddmm"],
                "Nigdi": ["ddmm", "dash", "ddmm"]},
    time_style={"Shivajinagar": "12h", "Pashan": "12h"},
    plants=W2_PLANTS,
    gap=dict(station="Nigdi", day=0, hours=hours_list(3, 12)),    # ~10 h outage
    # Nigdi day 3 logger nearly dead: only hours 05-08 exist at all
    extra_absent=[("Nigdi", 2, h) for h in hours_list(0, 4) + hours_list(9, 23)],
    duplicate=("Katraj", 0, 16),
)


# ------------------------------------------------------------------ helpers
def ward_text(spec: dict, station: str, day: int) -> str:
    wd = spec["ward_day"].get(station)
    if wd is None:
        return WARDS[station]
    return wd[day] if isinstance(wd, list) else wd


def date_style_of(spec: dict, station: str, day: int) -> str:
    st = spec["date_style"].get(station, "ddmm")
    return st[day] if isinstance(st, list) else st


def date_text(spec: dict, station: str, day: int) -> str:
    return spec["dates"][date_style_of(spec, station, day)][day]


def time_text(spec: dict, station: str, hour: int) -> str:
    if spec["time_style"].get(station, "24h") == "12h":
        if hour == 0:
            return "12:00 AM"
        if hour < 12:
            return f"{hour}:00 AM"
        if hour == 12:
            return "12:00 PM"
        return f"{hour - 12}:00 PM"
    return f"{hour:02d}:00"


def cell_text(station: str, pollutant: str, v: float) -> str:
    """Format one value; unit-quirk stations stamp units into cells."""
    if station == "Shivajinagar" and pollutant in ("NO2", "O3"):
        k = NO2_UG_PER_PPB if pollutant == "NO2" else O3_UG_PER_PPB
        return f"{v / k:.1f} ppb"
    if station == "Katraj" and pollutant == "CO":
        return f"{v * 1000:,.0f} µg/m3"          # µg/m3 + thousands separators
    if pollutant == "SO2":
        return f"{v:.0f}"
    if pollutant == "CO":
        return f"{v:.2f}"
    return f"{v:.1f}"


def absent_set(spec: dict) -> set[tuple[str, int, int]]:
    out = set(spec["extra_absent"])
    g = spec["gap"]
    for h in g["hours"]:
        out.add((g["station"], g["day"], h))
    return out


def overrides_map(spec: dict) -> dict[tuple[str, int, int, int], str]:
    ov: dict[tuple[str, int, int, int], str] = {}
    for p in spec["plants"]:
        if p["kind"] in POINT_KINDS or p["kind"] == "STUCK":
            for h in p["hours"]:
                ov[(p["station"], p["day"], h, COL[p["pollutant"]])] = p["text"]
    return ov


def expected_rows(spec: dict) -> int:
    return 8 * 72 - len(absent_set(spec)) + (1 if spec["duplicate"] else 0)


def build_rows(spec: dict) -> list[dict]:
    """Assemble the data rows in file order: day -> station -> hour."""
    rng = np.random.default_rng(SEED)
    noise = {}
    for day in range(3):                                   # fixed draw order => deterministic
        for s in STATIONS:
            noise[(s, day)] = rng.uniform(0.88, 1.14, size=(24, 6))
    absent = absent_set(spec)
    ov = overrides_map(spec)
    rows: list[dict] = []
    for day in range(3):
        for s in STATIONS:
            wtxt = ward_text(spec, s, day)
            dtxt = date_text(spec, s, day)
            for h in range(24):
                if (s, day, h) in absent:
                    continue
                n = noise[(s, day)][h]
                pm25 = spec["pm25"][s][day] * SHAPES["PM2.5"][h] / SHAPE_MEAN["PM2.5"] * n[0]
                vals = {
                    "PM2.5": pm25,
                    "PM10": pm25 * spec["pm_ratio"][s] * n[1],
                    "NO2": spec["base"][s]["NO2"] * spec["mult"][s]
                           * SHAPES["NO2"][h] / SHAPE_MEAN["NO2"] * n[2],
                    "SO2": spec["base"][s]["SO2"] * spec["mult"][s]
                           * SHAPES["SO2"][h] / SHAPE_MEAN["SO2"] * n[3],
                    "O3": spec["base"][s]["O3"] * spec["mult"][s]
                          * SHAPES["O3"][h] / SHAPE_MEAN["O3"] * n[4],
                    "CO": spec["base"][s]["CO"] * spec["mult"][s]
                          * SHAPES["CO"][h] / SHAPE_MEAN["CO"] * n[5],
                }
                cells = [s, wtxt, dtxt, time_text(spec, s, h)]
                for p in POLLUTANTS:
                    text = cell_text(s, p, vals[p])
                    planted = ov.get((s, day, h, COL[p]))
                    cells.append(text if planted is None else planted)
                rows.append({"cells": cells, "key": (s, day, h)})
    dup = spec["duplicate"]
    if dup:
        idx = next(i for i, r in enumerate(rows) if r["key"] == dup)
        rows.insert(idx + 1, {"cells": list(rows[idx]["cells"]), "key": dup + ("dup",)})
    return rows


def write_csv(path: Path, spec: dict, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        for line in spec["title_lines"]:          # title/subtitle verbatim (no CSV quoting)
            f.write(line + "\n")
        w = csv.writer(f, lineterminator="\n")
        w.writerow(spec["header"])
        for r in rows:
            w.writerow(r["cells"])

# --------------------------------------------------------------- verification
def locate(df: pd.DataFrame, station: str, dtxt: str, ttxt: str) -> list[int]:
    """Positions in the re-read table (position 0 = header row, so position ==
    data row id R<n>)."""
    m = ((df[0] == station) & (df[2] == dtxt) & (df[3] == ttxt)).to_numpy()
    return [int(i) for i in np.flatnonzero(m)]


def ids_text(ids: list[int]) -> str:
    if len(ids) == 1:
        return f"R{ids[0]}"
    return f"R{ids[0]}-R{ids[-1]} ({len(ids)} rows)"


def verify_week(path: Path, spec: dict) -> list[dict]:
    """Re-read the written file with pandas; check every planted fault and
    return manifest entries carrying the VERIFIED row ids."""
    lines = path.read_text(encoding="utf-8").splitlines()
    n_title = len(spec["title_lines"])
    if n_title:
        assert len(spec["title_lines"]) == 2, "week1 must have exactly 2 title lines"
        assert lines[0].startswith("Pune Municipal Corporation"), lines[0]
        assert lines[1].startswith("Hourly export"), lines[1]
        pass
    assert lines[n_title] == ",".join(spec["header"]), "header row mismatch"
    df = pd.read_csv(path, header=None, skiprows=n_title, dtype=str, keep_default_na=False)
    assert df.shape == (expected_rows(spec) + 1, 10), (df.shape, expected_rows(spec))
    assert list(df.iloc[0]) == spec["header"]
    assert df.iat[1, 0] == "Bhosari" and df.iat[1, 3] == "00:00"

    absent = absent_set(spec)
    entries: list[dict] = []

    _ds, _dday = (spec["duplicate"][:2] if spec["duplicate"] else (None, None))

    def dup_extra(s: str, day: int | None = None) -> int:
        """1 for the duplicated row's own (station, day) bucket, else 0."""
        if spec["duplicate"] and s == _ds and (day is None or day == _dday):
            return 1
        return 0

    # -- point faults and stuck runs ----------------------------------------
    for p in spec["plants"]:
        kind = p["kind"]
        if kind not in POINT_KINDS and kind != "STUCK":
            continue
        s, day, pol = p["station"], p["day"], p["pollutant"]
        dtxt = date_text(spec, s, day)
        ids = []
        for h in p["hours"]:
            hits = locate(df, s, dtxt, time_text(spec, s, h))
            assert len(hits) == 1, (kind, s, day, h, hits)
            pos = hits[0]
            cell = df.iat[pos, COL[pol]]
            assert cell == p["text"], (kind, s, day, h, repr(cell), repr(p["text"]))
            ids.append(pos)
        if kind == "STUCK":
            assert ids == list(range(ids[0], ids[0] + len(ids))), (kind, ids)
        if kind == "STUCK":
            assert len(ids) >= 8, (kind, len(ids))
            detail = (f"{s} {pol} flat at {p['text']} from {p['hours'][0]:02d}:00 to "
                      f"{p['hours'][-1]:02d}:00 on {spec['dates']['iso'][day]} "
                      f"({len(ids)} identical readings) -> STUCK (values kept)")
        elif kind == "SPIKE":
            note = (f"physically impossible (> {SPIKE_LIMIT[pol]} µg/m3 for {pol}) "
                    "-> OUT_OF_RANGE, value NaN")
        else:
            note = {
                "SENTINEL": "sentinel token -> SENTINEL, value unusable",
                "MISSING_BLANK": "blank cell -> MISSING",
                "CENSORED": "censored value -> CENSORED",
                "NEGATIVE": "negative concentration -> NEGATIVE, value NaN",
            }[kind]
        if kind != "STUCK":
            detail = (f"{s} {pol} at {time_text(spec, s, p['hours'][0])} on "
                      f"{spec['dates']['iso'][day]}: cell = \"{p['text']}\" ({note})")
        entries.append(dict(kind=kind, station=s, pollutant=pol, ids=ids, detail=detail))

    # -- gap (absent rows) ---------------------------------------------------
    g = spec["gap"]
    s, day, hrs = g["station"], g["day"], g["hours"]
    dtxt = date_text(spec, s, day)
    for h in hrs:
        assert locate(df, s, dtxt, time_text(spec, s, h)) == [], (s, day, h)
    before = locate(df, s, dtxt, time_text(spec, s, hrs[0] - 1))
    after = locate(df, s, dtxt, time_text(spec, s, hrs[-1] + 1))
    assert len(before) == 1 and len(after) == 1, (before, after)
    present_day = 24 - len(hrs)
    entries.append(dict(
        kind="GAP", station=s, pollutant="(all)", ids=before + after,
        detail=(f"{s}: no rows exist for {hrs[0]:02d}:00-{hrs[-1]:02d}:00 on "
                f"{spec['dates']['iso'][day]} (~10 h outage; {present_day} valid hours that "
                f"day -> PARTIAL_DAY, no valid daily AQI). Last row before the gap "
                f"R{before[0]}, first row after R{after[0]}"),
        rows_text=f"absent rows; bounds R{before[0]} / R{after[0]}"))

    # -- duplicate row -------------------------------------------------------
    ds, dday, dhour = spec["duplicate"]
    hits = locate(df, ds, date_text(spec, ds, dday), time_text(spec, ds, dhour))
    assert len(hits) == 2 and hits[1] == hits[0] + 1, hits
    assert list(df.iloc[hits[0]]) == list(df.iloc[hits[1]]), "duplicate not byte-identical"
    entries.append(dict(
        kind="DUPLICATE", station=ds, pollutant="(all)", ids=hits,
        detail=(f"{ds} row for {spec['dates']['iso'][dday]} "
                f"{time_text(spec, ds, dhour)} written twice, byte-identical; the copy "
                f"R{hits[1]} should carry DUPLICATE. Row ids after this point are "
                f"shifted by +1 (the ids here are the final file ids)"),
        rows_text=f"R{hits[0]} (original) + R{hits[1]} (exact copy)"))

    # -- ward spelled 3 ways -------------------------------------------------
    for s in STATIONS:
        wd = spec["ward_day"].get(s)
        if wd is None:
            continue
        counts: dict[str, int] = {}
        for day in range(3):
            v = wd[day] if isinstance(wd, list) else wd
            n = 24 - sum(1 for h in range(24) if (s, day, h) in absent)
            counts[v] = counts.get(v, 0) + n + dup_extra(s, day)
        parts = []
        for variant, exp in counts.items():
            m = ((df[0] == s) & (df[1] == variant)).to_numpy()
            ids = [int(i) for i in np.flatnonzero(m)]
            assert len(ids) == exp, (s, variant, len(ids), exp)
            parts.append(f"'{variant}' x{exp} (first R{ids[0]})")
        entries.append(dict(
            kind="WARD_SPELLINGS", station=s, pollutant="(ward column)", ids=[],
            detail=(f"{s} ({WARDS[s]}) ward spelled {len(counts)} ways: "
                    + "; ".join(parts)),
            rows_text="; ".join(parts)))

    # -- mixed date formats --------------------------------------------------
    for s in STATIONS:
        st = spec["date_style"].get(s)
        if st is None:
            continue
        styles = st if isinstance(st, list) else [st] * 3
        counts: dict[str, int] = {}
        for day in range(3):
            counts[styles[day]] = counts.get(styles[day], 0) + \
                (24 - sum(1 for h in range(24) if (s, day, h) in absent)) + dup_extra(s, day)
        label = {"ddmm": "DD/MM/YYYY", "iso": "ISO YYYY-MM-DD", "dash": "DD-MM-YYYY"}
        parts = []
        for style, exp in counts.items():
            rx = DATE_RX[style]
            m = ((df[0] == s) & df[2].str.match(rx)).to_numpy()
            ids = [int(i) for i in np.flatnonzero(m)]
            assert len(ids) == exp, (s, style, len(ids), exp)
            parts.append(f"{label[style]} x{exp} (first R{ids[0]})")
        entries.append(dict(
            kind="DATE_FORMATS", station=s, pollutant="(date column)", ids=[],
            detail=f"{s} mixes date formats: " + "; ".join(parts),
            rows_text="; ".join(parts)))

    # -- 12-hour time format -------------------------------------------------
    for s in STATIONS:
        if spec["time_style"].get(s, "24h") != "12h":
            continue
        m = ((df[0] == s) & df[3].str.match(TIME_12H_RX)).to_numpy()
        ids = [int(i) for i in np.flatnonzero(m)]
        exp = 24 * 3 - sum(1 for (st_, d_, h_) in absent if st_ == s) + dup_extra(s)
        assert len(ids) == exp, (s, len(ids), exp)
        entries.append(dict(
            kind="TIME_12H", station=s, pollutant="(time column)", ids=[],
            detail=f"{s} writes all {exp} times as 12-hour clock (e.g. '{time_text(spec, s, 20)}'); "
                   f"other stations use 24-hour '20:00'",
            rows_text=f"{exp} rows (first R{ids[0]})"))

    # -- units inside cells: ppb at Shivajinagar -----------------------------
    # -- units inside cells: ppb at Shivajinagar -----------------------------
    _dup_station = spec["duplicate"][0] if spec["duplicate"] else None
    rx_ppb = r"^-?\d+\.\d ppb$"
    for coli, pol in ((6, "NO2"), (8, "O3")):
        m = df[coli].str.match(rx_ppb).to_numpy()
        stations_hitting = sorted(set(df.loc[m, 0]))
        assert stations_hitting == ["Shivajinagar"], (pol, stations_hitting)
        assert int(m.sum()) == 72 + (_dup_station == "Shivajinagar"), (pol, int(m.sum()))
    ids = [int(i) for i in np.flatnonzero(((df[0] == "Shivajinagar")).to_numpy())]
    entries.append(dict(
        kind="UNIT_PPB", station="Shivajinagar", pollutant="NO2, O3", ids=[],
        detail=("all 72 NO2 and 72 O3 cells carry ppb units (e.g. \"22.6 ppb\"); "
                "convert NO2 x1.88 and O3 x1.96 to µg/m3. Note: the week1 header "
                "label 'NO2_ppb' is correct only for this station - every other "
                "station's NO2 cells are plain µg/m3 numbers"),
        rows_text=f"144 cells across 72 rows (first R{ids[0]})"))

    # -- units inside cells: CO in µg/m3 with thousands separators at Katraj --
    rx_ug = r"^[\d,]+ µg/m3$"
    m = df[9].str.match(rx_ug).to_numpy()
    stations_hitting = sorted(set(df.loc[m, 0]))
    assert stations_hitting == ["Katraj"], stations_hitting
    assert int(m.sum()) == 72 + (_dup_station == "Katraj"), int(m.sum())
    assert df.loc[m, 9].str.contains(",").any(), "no thousand separator found"
    body = df.iloc[1:]
    plain_co = body.loc[body[0] != "Katraj", 9]
    co_ok = r"^$|^(-?\d+\.\d{2}|-999|9999|N/A|NA|NULL|ND|--|-)$"
    assert plain_co.str.match(co_ok).all(), "unexpected CO formatting elsewhere"
    ids = [int(i) for i in np.flatnonzero((df[0] == "Katraj").to_numpy())]
    entries.append(dict(
        kind="UNIT_UG_CO", station="Katraj", pollutant="CO", ids=[],
        detail=(f"every Katraj CO cell reports µg/m3 with thousands separators "
                f"(e.g. \"1,836 µg/m3\") - {int(m.sum())} cells = 72 unique rows + "
                f"{int(m.sum()) - 72} duplicate copy - while the header says CO (mg/m3) "
                "and every other station writes plain mg/m3 numbers; convert /1000 "
                "(UNIT_CONVERTED) and strip the thousands separator"),
        rows_text=f"{int(m.sum())} cells (first R{ids[0]})"))

    # -- Nigdi week2 must be genuinely unreliable ----------------------------
    if spec["file"] == "sample_week2.csv":
        nigdi_rows = int((df[0] == "Nigdi").sum())
        assert nigdi_rows == 42, nigdi_rows           # 14 + 24 + 4
        unusable = sum(len(p["hours"]) for p in spec["plants"]
                       if p["station"] == "Nigdi" and p["kind"] in
                       ("SENTINEL", "MISSING_BLANK"))
        valid_cells = nigdi_rows * 6 - unusable
        coverage = valid_cells / (72 * 6)
        assert coverage < 0.5, coverage
        print(f"  check: Nigdi week2 coverage ~{coverage:.2f} (<0.5 -> 'unreliable')")

    return entries


# ------------------------------------------------------------------ manifest
KIND_ORDER = ["SENTINEL", "MISSING_BLANK", "CENSORED", "NEGATIVE", "SPIKE",
              "STUCK", "GAP", "DUPLICATE", "WARD_SPELLINGS", "DATE_FORMATS",
              "TIME_12H", "UNIT_PPB", "UNIT_UG_CO", "TITLE_SUBTITLE"]
KIND_LABEL = {"MISSING_BLANK": "MISSING (blank cell)", "UNIT_PPB": "UNITS IN CELLS (ppb)",
              "UNIT_UG_CO": "UNITS IN CELLS (µg/m3 + thousands separators)"}


def entry_rows_text(entry: dict) -> str:
    if "rows_text" in entry:
        return entry["rows_text"]
    return ids_text(entry["ids"])


def machine_index(entries: list[dict]) -> list[str]:
    out = []
    for kind in KIND_ORDER:
        for e in entries:
            if e["kind"] != kind:
                continue
            if e["kind"] == "GAP":
                out.append(f"GAP ({e['station']} outage): {e['rows_text']}")
            else:
                short = e["station"] + ((" " + e["pollutant"]) if e["pollutant"] and
                                        e["pollutant"] != "(all)" else "")
                out.append(f"{kind} {short}: {entry_rows_text(e)}")
    return out


def write_manifest(verified: dict, specs: dict) -> None:
    w1, w2 = specs["week1"], specs["week2"]
    n1, n2 = verified["week1"]["n_rows"], verified["week2"]["n_rows"]
    e1, e2 = verified["week1"]["entries"], verified["week2"]["entries"]
    story = {
        "Bhosari": ("hotspot both weeks; Very Poor in week2"),
        "Hadapsar": ("hotspot; worsens week1 -> week2 (Poor -> Very Poor)"),
        "Karve Road": ("improves to Satisfactory in week2"),
        "Katraj": ("slightly better in week2"),
        "Nigdi": ("healthy in week1; failing in week2 (unreliable data)"),
        "Pashan": ("improves to Satisfactory in week2; outage day in week1"),
        "Shivajinagar": ("traffic station; slightly better in week2"),
        "Swargate": ("traffic station; slightly better in week2"),
    }
    lines: list[str] = []
    add = lines.append
    add("# Sample data manifest - Pune CAAQMS weeks 1 & 2")
    add("")
    add("Generated by `scripts/make_sample_data.py` (seed 20261101, deterministic; "
        "re-running reproduces the files byte-for-byte).")
    add("Regenerate with: `.venv/bin/python scripts/make_sample_data.py`")
    add("")
    add("## Row id convention")
    add("")
    add("- `R<n>` = 1-based index of the data row **after the header row**, in file order.")
    add("- `sample_week1.csv` has a 2-line title/subtitle ABOVE the header; those lines are "
        "not data rows and do not consume R ids. `sample_week2.csv` has no title lines.")
    add("- Row order: day -> station (alphabetical: Bhosari, Hadapsar, Karve Road, Katraj, "
        "Nigdi, Pashan, Shivajinagar, Swargate) -> hour 00-23.")
    add("- Outage hours are ABSENT rows (no R id exists for them). The duplicate row shifts "
        "every later R id by +1; **all ids below are the final file ids**, verified by "
        "re-reading the written files with pandas.")
    add("")
    add("## Files and layout")
    add("")
    add("| file | data rows | period | layout |")
    add("|---|---|---|---|")
    add(f"| data/sample_week1.csv | {n1} | 2026-11-01..03, hourly | wide (1 row = station-hour); "
        "2 title/subtitle lines above the header |")
    add(f"| data/sample_week2.csv | {n2} | 2026-11-08..10, hourly | wide (1 row = station-hour); "
        "no title lines |")
    add("")
    add(f"Counts: week1 {n1} = 8x72 - 10 (Pashan outage rows) + 1 (duplicate); "
        f"week2 {n2} = 8x72 - 10 (Nigdi day-1 outage) - 20 (Nigdi day-3 rows absent) + 1 (duplicate).")
    add("")
    add("Columns (both files, in order): station, ward, date, time, PM2.5, PM10, NO2, SO2, O3, CO. "
        "Headers are deliberately inconsistent between (and within) files; units in headers are "
        "unreliable by design - cell-embedded units are authoritative.")
    add("")
    add("## Stations and base story")
    add("")
    add("City: Pune. PM2.5 daily-mean targets (µg/m3, before noise):")
    add("")
    add("| Station | Ward | week1 (01/02/03 Nov) | week2 (08/09/10 Nov) | Role in the story |")
    add("|---|---|---|---|---|")
    for s in STATIONS:
        add(f"| {s} | {WARDS[s]} | {', '.join(str(x) for x in w1['pm25'][s])} | "
            f"{', '.join(str(x) for x in w2['pm25'][s])} | {story[s]} |")
    add("")
    add("- PM10 ~ 1.85-2.10 x PM2.5; NO2/SO2/O3/CO follow traffic/industrial/photochemical shapes "
        "(O3 peaks 12:00-16:00, PM peaks 07:00-09:00 and 18:00-22:00).")
    add("- Expected daily AQI span: week1 Moderately Polluted..Very Poor (Hadapsar 03 Nov ~ Very "
        "Poor); week2 adds Satisfactory at Karve Road/Pashan and Very Poor at the two hotspots. "
        "Daily means land within ~±5% of the targets.")
    add("- Units: standard is µg/m3 for all pollutants except CO (mg/m3). Shivajinagar reports "
        "NO2/O3 in ppb (cells stamped 'ppb'); Katraj reports CO in µg/m3 (cells stamped "
        "'µg/m3' with thousands separators). All other cells are plain numbers in the standard "
        "unit, so they carry the documented UNIT_AMBIGUOUS caveat under the 'missing unit -> "
        "assume standard' rule.")
    add("")
    add("## Planted faults - week1 (data/sample_week1.csv)")
    add("")
    add("| Fault | Station | Pollutant | Rows | Detail |")
    add("|---|---|---|---|---|")
    for kind in KIND_ORDER:
        for e in e1:
            if e["kind"] == kind:
                add(f"| {KIND_LABEL.get(kind, kind)} | {e['station']} | {e['pollutant']} | "
                    f"{entry_rows_text(e)} | {e['detail']} |")
    tl = w1["title_lines"]
    add(f"| TITLE + SUBTITLE | (file) | (rows above header) | lines 1-2 of the file | "
        f"\"{tl[0]}\" / \"{tl[1]}\"; the true header is line 3 |")
    add("| HEADER INCONSISTENT | (file) | (header row) | the row above R1 | "
        "`Station Name,ward,Date,Time,PM2.5 (ug/m3),PM10,NO2_ppb,so2,Ozone,CO (mg/m3)` - "
        "mixed case, 'NO2_ppb' suffix, unit only on PM2.5/CO |")
    add("")
    add("## Planted faults - week2 (data/sample_week2.csv)")
    add("")
    add("| Fault | Station | Pollutant | Rows | Detail |")
    add("|---|---|---|---|---|")
    for kind in KIND_ORDER:
        for e in e2:
            if e["kind"] == kind:
                add(f"| {KIND_LABEL.get(kind, kind)} | {e['station']} | {e['pollutant']} | "
                    f"{entry_rows_text(e)} | {e['detail']} |")
    add("| HEADER INCONSISTENT | (file) | (header row) | the row above R1 | "
        "`Station,Ward Name,date,time,PM2.5,PM10 (ug/m3),NO2,SO2,O3,CO (mg/m3)` - different "
        "style from week1; no title lines |")
    add("")
    add("## Row-id index (machine-readable)")
    add("")
    add("```")
    add("week1:")
    for line in machine_index(e1):
        add(f"  {line}")
    add("week2:")
    for line in machine_index(e2):
        add(f"  {line}")
    add("```")
    add("")
    add("## Notes for test authors")
    add("")
    add("| Planted cell | Expected flag / behaviour |")
    add("|---|---|")
    add("| `NA`, `N/A`, `--`, `-`, `NULL`, `ND`, `-999`, `9999` | SENTINEL (value unusable) |")
    add("| blank cell | MISSING |")
    add("| `-12.5` (PM10), `-3.2 ppb` (O3) | NEGATIVE, value NaN |")
    add("| `2750` (PM2.5), `3100` (PM10), `1180` (O3) | OUT_OF_RANGE, value NaN |")
    add("| `<5` | CENSORED |")
    add("| 9-12 identical consecutive readings | STUCK (values stay usable) |")
    add("| duplicate row copy | DUPLICATE on the later copy |")
    add("| `22.6 ppb` (Shivajinagar NO2/O3) | UNIT_CONVERTED (x1.88 / x1.96) |")
    add("| `1,836 µg/m3` (Katraj CO) | UNIT_CONVERTED (/1000) + thousands separator |")
    add("| Pashan 2026-11-02 | PARTIAL_DAY: only 14 valid hours -> no valid daily AQI |")
    add("| Nigdi week2 | LOW_COVERAGE / unreliable (coverage ~0.49; day 1 AQI valid but built on "
        "a stuck PM2.5, days 2-3 invalid) |")
    add("")
    add("- All dates have day <= 12, so DD/MM vs MM/DD is genuinely ambiguous; the contract rule "
        "(assume DD/MM) yields the correct dates.")
    add("- The duplicate rows are byte-identical copies (station, ward, date, time and every "
        "cell); detection is by repeated station+timestamp, not by value drift.")
    add("- The manifest is regenerated by the same script run that wrote the CSVs, so the ids "
        "here always match the files.")
    add("")
    path = DATA_DIR / "sample_manifest.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"manifest written: {path}")


# ----------------------------------------------------------------------- main
def main() -> None:
    specs = {"week1": WEEK1, "week2": WEEK2}
    verified = {}
    for name, spec in specs.items():
        rows = build_rows(spec)
        path = DATA_DIR / spec["file"]
        write_csv(path, spec, rows)
        entries = verify_week(path, spec)
        verified[name] = dict(spec=spec, path=path, entries=entries, n_rows=len(rows))
        print(f"[{name}] {path.name}: {len(rows)} data rows, "
              f"{len(entries)} planted faults verified against the written file")
    write_manifest(verified, specs)
    print("OK: all planted faults verified; manifest row ids match the files.")


if __name__ == "__main__":
    main()
