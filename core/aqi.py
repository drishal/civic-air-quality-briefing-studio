"""CPCB National AQI: breakpoints, sub-indices, averages and daily AQI.

The breakpoint table follows PLAN.md section 3 (Pb omitted). The printed table
leaves one-unit gaps between bands (PM2.5 Good 0-30, Satisfactory 31-60), so the
bands are stored as contiguous ranges: a band's lower bound is the previous
band's upper bound. That keeps a concentration inside a gap (e.g. 30.5) on the
scale instead of leaving it undefined. AQI numbers stay exactly as printed
(0-50, 51-100, 101-200, ...).
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from core.models import POLLUTANTS

_INF = math.inf

# pollutant -> [(conc_lo, conc_hi, aqi_lo, aqi_hi), ...] ascending, contiguous.
# Units: ug/m3, except CO in mg/m3. The top band has conc_hi = inf and its
# sub-index is capped at 500.
BREAKPOINTS: dict[str, list[tuple[float, float, float, float]]] = {
    "PM2.5": [
        (0, 30, 0, 50),
        (30, 60, 51, 100),
        (60, 90, 101, 200),
        (90, 120, 201, 300),
        (120, 250, 301, 400),
        (250, _INF, 401, 500),
    ],
    "PM10": [
        (0, 50, 0, 50),
        (50, 100, 51, 100),
        (100, 250, 101, 200),
        (250, 350, 201, 300),
        (350, 430, 301, 400),
        (430, _INF, 401, 500),
    ],
    "NO2": [
        (0, 40, 0, 50),
        (40, 80, 51, 100),
        (80, 180, 101, 200),
        (180, 280, 201, 300),
        (280, 400, 301, 400),
        (400, _INF, 401, 500),
    ],
    "SO2": [
        (0, 40, 0, 50),
        (40, 80, 51, 100),
        (80, 380, 101, 200),
        (380, 800, 201, 300),
        (800, 1600, 301, 400),
        (1600, _INF, 401, 500),
    ],
    "O3": [
        (0, 50, 0, 50),
        (50, 100, 51, 100),
        (100, 168, 101, 200),
        (168, 208, 201, 300),
        (208, 748, 301, 400),
        (748, _INF, 401, 500),
    ],
    "CO": [
        (0, 1.0, 0, 50),
        (1.0, 2.0, 51, 100),
        (2.0, 10.0, 101, 200),
        (10.0, 17.0, 201, 300),
        (17.0, 34.0, 301, 400),
        (34.0, _INF, 401, 500),
    ],
    "NH3": [
        (0, 200, 0, 50),
        (200, 400, 51, 100),
        (400, 800, 101, 200),
        (800, 1200, 201, 300),
        (1200, 1800, 301, 400),
        (1800, _INF, 401, 500),
    ],
}

# AQI category upper bounds, inclusive. Anything above 500 is Severe.
_CATEGORY_MAX: tuple[tuple[float, str], ...] = (
    (50, "Good"),
    (100, "Satisfactory"),
    (200, "Moderately Polluted"),
    (300, "Poor"),
    (400, "Very Poor"),
    (500, "Severe"),
)

_CATEGORY_COLOR = {
    "Good": "#00A651",
    "Satisfactory": "#8CC63F",
    "Moderately Polluted": "#F5C518",
    "Poor": "#F26522",
    "Very Poor": "#D0021B",
    "Severe": "#7A0026",
}

_CATEGORY_EMOJI = {
    "Good": "😊",
    "Satisfactory": "🙂",
    "Moderately Polluted": "😐",
    "Poor": "😷",
    "Very Poor": "😨",
    "Severe": "🚨",
}

# WHO 2021 24-hour guideline values (ug/m3; CO in mg/m3).
WHO_24H: dict[str, float] = {
    "PM2.5": 15,
    "PM10": 45,
    "NO2": 25,
    "SO2": 40,
    "O3": 100,
    "CO": 4,
}

MIN_HOURS = 16          # hours needed for a valid daily average
ROLLING_WINDOW = 8      # hours in an O3 / CO rolling mean
ROLLING_MIN_PERIODS = 6  # partial window floor for O3 / CO
ROLLING_POLLUTANTS = ("O3", "CO")

AVG_COLUMNS = ["station", "ward", "date", "pollutant", "avg", "n_hours", "valid"]
AQI_COLUMNS = [
    "station", "ward", "date", "aqi", "category", "dominant",
    "n_pollutants", "subindices", "valid", "reason",
    # Indicative AQI: computed when the strict CPCB rule fails but a reading exists.
    "indicative", "indicative_aqi", "indicative_category", "indicative_dominant",
    "indicative_subindices", "indicative_reason",
]


def _as_float(value: object) -> Optional[float]:
    """Return value as a float, or None when it is not a usable number."""
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def subindex(pollutant: str, conc: float) -> Optional[float]:
    """CPCB sub-index for one pollutant concentration.

    Linear interpolation inside the bracket holding `conc`:
    Ip = (Ihi - Ilo) / (BPhi - BPlo) * (C - BPlo) + Ilo.
    Returns 500 for anything in the top bracket, None for negative/NaN input
    or an unknown pollutant.
    """
    bands = BREAKPOINTS.get(pollutant)
    if bands is None:
        return None
    c = _as_float(conc)
    if c is None or c < 0:
        return None
    for lo, hi, ilo, ihi in bands:
        if c <= hi:
            if math.isinf(hi):
                return float(ihi)  # top bracket: capped at 500
            return ilo + (ihi - ilo) / (hi - lo) * (c - lo)
    return 500.0  # unreachable: the top bracket has no upper bound


def category(aqi: float) -> str:
    """CPCB category name for an AQI value; '' when there is no AQI."""
    value = _as_float(aqi)
    if value is None:
        return ""
    for upper, name in _CATEGORY_MAX:
        if value <= upper:
            return name
    return "Severe"


def category_color(cat: str) -> str:
    """Hex colour for a category name."""
    return _CATEGORY_COLOR.get(cat, "#808080")


def category_emoji(cat: str) -> str:
    """Emoji marker for a category name."""
    return _CATEGORY_EMOJI.get(cat, "❔")


def who_multiple(pollutant: str, conc: float) -> Optional[float]:
    """How many times the WHO 24-hour guideline a concentration is (None if unknown)."""
    limit = WHO_24H.get(pollutant)
    value = _as_float(conc)
    if limit is None or value is None or value < 0:
        return None
    return value / limit


def _normalise(tidy: pd.DataFrame) -> pd.DataFrame:
    """Copy of the tidy table with parsed ts, date, hour and numeric value_std."""
    df = tidy.copy()
    if "ward" not in df.columns:
        df["ward"] = None
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["value_std"] = pd.to_numeric(df["value_std"], errors="coerce")
    df = df[df["pollutant"].isin(POLLUTANTS)]
    df = df.dropna(subset=["ts"])
    df["date"] = df["ts"].dt.strftime("%Y-%m-%d")
    df["hour"] = df["ts"].dt.hour
    return df


def _has_columns(tidy: Optional[pd.DataFrame]) -> bool:
    """True when the frame has rows and the columns this module needs."""
    return (
        isinstance(tidy, pd.DataFrame)
        and len(tidy) > 0
        and {"ts", "station", "pollutant", "value_std"}.issubset(tidy.columns)
    )


def _station_wards(df: pd.DataFrame) -> dict[str, str]:
    """Most common ward per station (empty when no ward is known)."""
    known = df.dropna(subset=["ward"])
    if known.empty:
        return {}
    return known.groupby("station")["ward"].agg(lambda s: s.mode().iloc[0]).to_dict()


def _daily_mean(hours: pd.Series) -> float:
    """24-hour mean of the hourly values that are present."""
    return float(hours.mean())


def _max_rolling_8h(hours: pd.Series) -> float:
    """Highest 8-hour rolling mean within the day (6 valid hours minimum)."""
    day = hours.reindex(range(24))
    rolling = day.rolling(ROLLING_WINDOW, min_periods=ROLLING_MIN_PERIODS).mean()
    if not rolling.notna().any():
        return float("nan")
    return float(rolling.max())


def pollutant_averages(tidy: pd.DataFrame) -> pd.DataFrame:
    """Per station, day and pollutant: the 24-hour average (O3, CO: peak 8-hour mean).

    Readings are binned to hourly means first, so several readings in one clock
    hour count once. `n_hours` is the number of distinct valid hours and `valid`
    is n_hours >= 16.
    """
    if not _has_columns(tidy):
        return pd.DataFrame(columns=AVG_COLUMNS)
    df = _normalise(tidy)  # type: ignore[arg-type]
    df = df.dropna(subset=["value_std"])
    if df.empty:
        return pd.DataFrame(columns=AVG_COLUMNS)
    wards = _station_wards(df)
    hourly = (
        df.groupby(["station", "date", "pollutant", "hour"], as_index=False)
        .agg(value=("value_std", "mean"))
    )
    rows: list[dict[str, object]] = []
    for (station, date, pollutant), grp in hourly.groupby(
        ["station", "date", "pollutant"], sort=True
    ):
        hours = grp.set_index("hour")["value"].sort_index()
        n_hours = int(hours.index.nunique())
        if pollutant in ROLLING_POLLUTANTS:
            avg = _max_rolling_8h(hours)
        else:
            avg = _daily_mean(hours)
        rows.append(
            {
                "station": station,
                "ward": wards.get(station),
                "date": date,
                "pollutant": pollutant,
                "avg": avg,
                "n_hours": n_hours,
                "valid": n_hours >= MIN_HOURS,
            }
        )
    out = pd.DataFrame(rows, columns=AVG_COLUMNS)
    return out.sort_values(["station", "date"], kind="stable").reset_index(drop=True)


def _day_reason(n_valid: int, has_pm: bool, notes: list[str]) -> str:
    """Plain-language reason why a day has no valid AQI."""
    parts: list[str] = []
    if n_valid == 0:
        parts.append("no valid pollutants")
    elif n_valid < 3:
        plural = "pollutant" if n_valid == 1 else "pollutants"
        parts.append(f"only {n_valid} {plural} valid")
    if not has_pm:
        parts.append("no PM2.5/PM10")
    parts.extend(notes)
    parts.extend(notes)
    return "; ".join(parts)


def _indicative_reason(min_hours: int, n_pollutants: int) -> str:
    """Why an AQI is indicative only: the CPCB completeness rule is not met."""
    details: list[str] = []
    if min_hours < MIN_HOURS:
        hours = "hour" if min_hours == 1 else "hours"
        details.append(f"{min_hours} {hours} of data, CPCB needs {MIN_HOURS}")
    if n_pollutants < 3:
        plural = "pollutant" if n_pollutants == 1 else "pollutants"
        details.append(f"{n_pollutants} {plural}, CPCB needs 3")
    if not details:
        details.append("the CPCB completeness rule is not met")
    return "indicative only: " + "; ".join(details)


def daily_aqi(tidy: pd.DataFrame) -> pd.DataFrame:
    """One daily AQI row per station and date.

    AQI is the worst sub-index; the pollutant setting it is the dominant one. A
    day is valid when at least three pollutants are valid and PM2.5 or PM10 is
    among them; invalid days are still returned, with `reason` explaining why.

    Every row also carries an indicative AQI (`indicative_*` columns): the same
    worst-sub-index arithmetic applied to whatever valid readings exist, with no
    16-hour rule and no three-pollutant rule. It is set only when the strict AQI
    is not valid and at least one PM reading exists, and it is never a substitute
    for the official value.
    """
    if not _has_columns(tidy):
        return pd.DataFrame(columns=AQI_COLUMNS)
    df = _normalise(tidy)  # type: ignore[arg-type]
    if df.empty:
        return pd.DataFrame(columns=AQI_COLUMNS)
    wards = _station_wards(df)
    keys = df[["station", "date"]].drop_duplicates().sort_values(["station", "date"])

    averages = pollutant_averages(tidy)
    grouped: dict[tuple[str, str], pd.DataFrame] = {}
    if not averages.empty:
        grouped = {
            (str(station), str(date)): grp
            for (station, date), grp in averages.groupby(["station", "date"], sort=True)
        }

    # Plain hourly mean per station-day-pollutant: the fallback when an O3/CO day is
    # too short for even one 8-hour window, so the indicative AQI can still be computed.
    hourly = df.dropna(subset=["value_std"]).groupby(
        ["station", "date", "pollutant", "hour"], as_index=False
    )["value_std"].mean()
    plain_mean = hourly.groupby(["station", "date", "pollutant"])["value_std"].mean().to_dict()

    rows: list[dict[str, object]] = []
    for station, date in keys.itertuples(index=False):
        station = str(station)
        date = str(date)
        subindices: dict[str, float] = {}
        notes: list[str] = []
        indicative: dict[str, float] = {}
        indicative_hours: list[int] = []
        grp = grouped.get((station, date))
        if grp is not None:
            hours_by_pollutant = dict(zip(grp["pollutant"], grp["n_hours"]))
            avg_by_pollutant = dict(zip(grp["pollutant"], grp["avg"]))
            for pollutant in POLLUTANTS:
                if pollutant not in avg_by_pollutant:
                    continue
                n_hours = int(hours_by_pollutant[pollutant])
                average = _as_float(avg_by_pollutant[pollutant])
                if n_hours < MIN_HOURS:
                    notes.append(f"{pollutant} has {n_hours} valid hours (<{MIN_HOURS})")
                elif average is not None:
                    value = subindex(pollutant, average)
                    if value is not None:
                        subindices[pollutant] = round(float(value), 1)
                # Indicative: no 16-hour gate. O3/CO without an 8-hour window fall
                # back to the plain mean of whatever hours exist.
                if average is None:
                    average = _as_float(plain_mean.get((station, date, pollutant)))
                if average is None:
                    continue
                value = subindex(pollutant, average)
                if value is None:
                    continue
                indicative[pollutant] = round(float(value), 1)
                indicative_hours.append(n_hours)

        n_pollutants = len(subindices)
        has_pm = "PM2.5" in subindices or "PM10" in subindices
        valid = n_pollutants >= 3 and has_pm
        if subindices:
            dominant = max(
                subindices,
                key=lambda p: (subindices[p], -POLLUTANTS.index(p)),
            )
            aqi = subindices[dominant]
            cat = category(aqi)
        else:
            dominant = None
            aqi = float("nan")
            cat = ""

        ind_valid = bool(indicative) and ("PM2.5" in indicative or "PM10" in indicative)
        if valid or not ind_valid:
            ind_aqi: object = float("nan")
            ind_cat = ""
            ind_dominant = None
            ind_subindices: dict[str, float] = {}
            ind_reason = ""
        else:
            ind_dominant = max(
                indicative,
                key=lambda p: (indicative[p], -POLLUTANTS.index(p)),
            )
            ind_aqi = indicative[ind_dominant]
            ind_cat = category(float(ind_aqi))
            ind_subindices = indicative
            ind_reason = _indicative_reason(
                min(indicative_hours) if indicative_hours else 0, len(indicative)
            )
        rows.append(
            {
                "station": station,
                "ward": wards.get(station),
                "date": date,
                "aqi": aqi,
                "category": cat,
                "dominant": dominant,
                "n_pollutants": n_pollutants,
                "subindices": subindices,
                "valid": valid,
                "reason": "" if valid else _day_reason(n_pollutants, has_pm, notes),
                "indicative": ind_valid and not valid,
                "indicative_aqi": ind_aqi,
                "indicative_category": ind_cat,
                "indicative_dominant": ind_dominant,
                "indicative_subindices": ind_subindices,
                "indicative_reason": ind_reason,
            }
        )
    out = pd.DataFrame(rows, columns=AQI_COLUMNS)
    return out.sort_values(["station", "date"], kind="stable").reset_index(drop=True)
