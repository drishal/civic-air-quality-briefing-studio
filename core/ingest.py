"""Ingest messy air-quality tables: paste text, CSV/TSV/semicolon files or XLSX.

`read_input` turns bytes or text into a RawTable (all cells are strings, plus one
`_src_row` integer per data row). `heuristic_mapping` guesses which column is what
without an LLM; `clean.apply_mapping` applies that mapping. Nothing here raises on a
bad cell: bad cells are recorded in `notes` / the cleaning log and survive as strings.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

import pandas as pd

from core.models import ColumnMapping, ColumnRole

_SUBSCRIPT = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
_DELIMITERS = (",", "\t", ";", "|")
_XLSX_MAGIC = b"PK\x03\x04"

# Header cells that hint at a table header (not a title/subtitle row).
_HEADER_HINTS = re.compile(
    r"(station|site|location|monitor|observ|ward|zone|date|time|stamp|hour|"
    r"pm\s*[-_. ]?2[.,]?5|pm\s*[-_. ]?10|no2|so2|ozone|\bo3\b|\bco\b|nh3|ammonia|"
    r"param|pollutant|species|value|conc|unit|lat|lon|reading|aqi)",
    re.IGNORECASE,
)
_NUMERIC_ISH = re.compile(r"^[+-]?[\d.,]+$")

# Unit tokens, longest / most specific first.
_UNIT_TOKEN = re.compile(
    r"(?:µg|μg|ug|mg|ng)\s*[/·]\s*m\s*\^?\s*[3³]"
    r"|(?:ug|mg|ng)m\s*-?\s*[3³]"
    r"|(?<![0-9a-z])p?pp[bmv]+(?![0-9a-z])"
    r"|nmol\s*/\s*mol",
    re.IGNORECASE,
)

# Pollutant patterns, checked in this order. `\b` keeps "CO" out of "CO2"/"conc".
_POLLUTANT_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PM2.5", re.compile(r"pm\s*[-_. ]?\s*2\s*[.,]\s*5(?![0-9a-z])|pm25(?![0-9a-z])|fine\s+particulate")),
    ("PM10", re.compile(r"pm\s*[-_. ]?\s*10(?![0-9a-z])|pm10(?![0-9a-z])|coarse\s+particulate")),
    ("NO2", re.compile(r"\bno\s*[-_ ]?\s*2(?![0-9a-z])|nitrogen\s+dioxide")),
    ("SO2", re.compile(r"\bso\s*[-_ ]?\s*2(?![0-9a-z])|sulph?ur\s+dioxide")),
    ("O3", re.compile(r"\bo\s*[-_ ]?\s*3(?![0-9a-z])|\bozone\b")),
    ("NH3", re.compile(r"\bnh\s*[-_ ]?\s*3(?![0-9a-z])|ammonia")),
    ("CO", re.compile(r"\bco(?![0-9a-z])|carbon\s+monoxide")),
)

_UNIT_ALIASES = {
    "ug/m3": "ug/m3", "µg/m³": "ug/m3", "μg/m³": "ug/m3", "ug/m^3": "ug/m3",
    "µg/m3": "ug/m3", "ugm3": "ug/m3", "ugm-3": "ug/m3", "ug/m-3": "ug/m3",
    "micrograms/m3": "ug/m3", "microgram/m3": "ug/m3",
    "mg/m3": "mg/m3", "mg/m^3": "mg/m3", "mgm3": "mg/m3", "mgm-3": "mg/m3",
    "milligrams/m3": "mg/m3", "milligram/m3": "mg/m3",
    "ng/m3": "ng/m3", "ng/m^3": "ng/m3", "ngm3": "ng/m3",
    "ppb": "ppb", "ppbv": "ppb", "ppb(v)": "ppb", "nmol/mol": "ppb",
    "ppm": "ppm", "ppmv": "ppm", "ppm(v)": "ppm",
}


@dataclass
class RawTable:
    """Tables as read from the source: strings only, one `_src_row` per data row."""

    df: pd.DataFrame
    source_name: str
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers
def _mode(values: list[int]) -> tuple[int, int]:
    """Most common value and its count."""
    if not values:
        return 0, 0
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    best = max(counts, key=lambda k: (counts[k], k))
    return best, counts[best]


def _split_line(line: str, delimiter: str | None) -> list[str]:
    """Split one line; `None` means whitespace-aligned columns."""
    if delimiter is None:
        return [c.strip() for c in re.split(r"\s{2,}", line.strip()) if c.strip() != ""]
    try:
        return next(csv.reader([line], delimiter=delimiter))
    except (csv.Error, StopIteration):
        return line.split(delimiter)


def _detect_delimiter(lines: list[str]) -> tuple[str | None, str]:
    """Pick the delimiter that gives the most consistent column count."""
    sample = lines[:40]
    best: tuple[float, str | None] | None = None
    for delim in _DELIMITERS:
        counts = [len(_split_line(line, delim)) for line in sample]
        modal, freq = _mode(counts)
        if modal < 2 or freq < 0.5:
            continue
        score = freq * 10 + min(modal, 30) / 100
        if best is None or score > best[0]:
            best = (score, delim)
    if best is not None:
        name = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}[best[1]]  # type: ignore[index]
        return best[1], f"Detected {name}-separated text."
    counts = [len(_split_line(line, None)) for line in sample]
    modal, freq = _mode(counts)
    if modal >= 2 and freq >= 0.6:
        return None, "Detected whitespace-aligned columns."
    return ("," if any("," in line for line in sample) else None), "Delimiter was unclear; guessed comma."


def _decode(data: bytes, notes: list[str]) -> str:
    """Decode file bytes, trying the usual encodings."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            notes.append("Decoded the file as UTF-16.")
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            if enc != "utf-8-sig":
                notes.append(f"Decoded the file as {enc} (not UTF-8).")
            return text
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _is_xlsx(file_bytes: bytes | None, filename: str | None) -> bool:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm", ".xltx", ".xls")):
        return True
    return bool(file_bytes) and bytes(file_bytes[:4]) == _XLSX_MAGIC


def _rows_from_xlsx(data: bytes, notes: list[str]) -> list[list[str]]:
    """The sheet with the most data, as rows of strings (cover sheets are ignored)."""
    try:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
    except Exception as exc:  # noqa: BLE001 - any Excel parse error becomes a friendly message
        raise ValueError(
            "This Excel file could not be opened. Save it as .xlsx or CSV and try again "
            f"(details: {type(exc).__name__})."
        ) from exc
    best_rows: list[list[str]] | None = None
    best_name = ""
    best_score = 0
    for sheet_name, frame in sheets.items():
        rows: list[list[str]] = []
        for record in frame.itertuples(index=False, name=None):
            rows.append(["" if c is None or (isinstance(c, float) and pd.isna(c)) else str(c) for c in record])
        score = sum(1 for row in rows for c in row if c.strip())
        if score > best_score:
            best_rows, best_name, best_score = rows, sheet_name, score
    if best_rows is None or best_score < 2:
        raise ValueError("The Excel file has no sheet with a table in it.")
    notes.append(f"Read sheet '{best_name}' from the Excel file.")
    return best_rows


def _looks_numeric(cell: str) -> bool:
    return bool(_NUMERIC_ISH.fullmatch(cell.strip()))


def _detect_header(rows: list[list[str]]) -> int:
    """Index of the true header row (skips title/subtitle rows above it)."""
    limit = min(len(rows), 12)
    best_index, best_score = None, -1
    for i in range(limit):
        cells = [c.strip() for c in rows[i] if c.strip()]
        if len(cells) < 2:
            continue
        hits = sum(1 for c in cells if _HEADER_HINTS.search(c))
        numeric = sum(1 for c in cells if _looks_numeric(c))
        if numeric > len(cells) * 0.34:
            continue
        score = hits * 10 + min(len(cells), 12)
        if hits == 0:
            score = 1 + min(len(cells), 12)
        if score > best_score:
            best_index, best_score = i, score
    if best_index is not None:
        return best_index
    counts = [len([c for c in row if c.strip()]) for row in rows[:limit]]
    modal, _ = _mode(counts)
    for i, count in enumerate(counts):
        if count == modal and modal >= 2:
            return i
    return 0


def _unique_headers(header: list[str]) -> list[str]:
    """Header cells as unique, non-empty column names."""
    out: list[str] = []
    counts: dict[str, int] = {}
    for i, cell in enumerate(header):
        name = str(cell).strip().strip('"').strip()
        if not name:
            name = f"col_{i + 1}"
        if name in counts:
            counts[name] += 1
            out.append(f"{name}_{counts[name]}")
        else:
            counts[name] = 1
            out.append(name)
    return out


def _norm_cell(cell: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(cell).lower())


def _is_repeated_header(row: list[str], header_keys: set[str]) -> bool:
    """True when a data row is just the header written again."""
    cells = [c.strip() for c in row if c.strip()]
    if not cells or any(_looks_numeric(c) for c in cells):
        return False
    keys = [_norm_cell(c) for c in cells]
    hits = sum(1 for k in keys if k and k in header_keys)
    return hits >= max(2, len(header_keys) // 2)


# --------------------------------------------------------------------------- read_input
def read_input(
    text: str | None = None,
    file_bytes: bytes | None = None,
    filename: str | None = None,
) -> RawTable:
    """Read pasted text or an uploaded CSV/TSV/semicolon/XLSX file into a RawTable.

    Raises ValueError with a friendly message when the input is empty or has no
    usable table (no header plus at least one data row).
    """
    notes: list[str] = []
    source_name = filename.strip() if isinstance(filename, str) and filename.strip() else "pasted text"
    if file_bytes is not None and not isinstance(file_bytes, (bytes, bytearray)):
        raise ValueError("The uploaded file could not be read (expected raw bytes).")
    if text is not None and not isinstance(text, str):
        raise ValueError("The pasted text could not be read (expected text).")
    if text is None and file_bytes is None:
        raise ValueError("Nothing to read: paste a table or upload a CSV, TSV or Excel file.")

    if text is not None:
        raw_text = text
    else:
        assert file_bytes is not None
        if _is_xlsx(file_bytes, filename):
            rows = _rows_from_xlsx(bytes(file_bytes), notes)
            return _table_from_rows(rows, source_name, notes)
        raw_text = _decode(bytes(file_bytes), notes)

    raw_text = raw_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line for line in raw_text.split("\n") if line.strip()]
    if not lines:
        raise ValueError("The input is empty. Paste a table or upload a file with a header row and data.")
    delimiter, note = _detect_delimiter(lines)
    notes.append(note)
    rows = [_split_line(line, delimiter) for line in lines]
    return _table_from_rows(rows, source_name, notes)


def _table_from_rows(rows: list[list[str]], source_name: str, notes: list[str]) -> RawTable:
    """Build a RawTable from raw string rows (skipping title rows, blank and repeat rows)."""
    if len(rows) < 2:
        raise ValueError("The table needs a header row and at least one data row.")
    header_index = _detect_header(rows)
    if header_index >= len(rows) - 1:
        raise ValueError("Found a header row but no data rows below it.")
    header = [str(c) for c in rows[header_index]]
    columns = _unique_headers(header)
    width = len(columns)
    if width < 2:
        raise ValueError(
            "Could not find a table header with two or more columns. "
            "Supported: CSV, TSV, semicolon text or Excel (.xlsx)."
        )
    if header_index > 0:
        notes.append(f"Skipped {header_index} title row(s) above the header.")

    header_keys = {_norm_cell(c) for c in columns if _norm_cell(c)}
    data: list[list[str]] = []
    src_rows: list[int] = []
    ragged = 0
    repeated = 0
    blanks = 0
    for row in rows[header_index + 1:]:
        cells = [str(c).strip() for c in row]
        if not any(cells):
            blanks += 1
            continue
        if _is_repeated_header(cells, header_keys):
            repeated += 1
            continue
        if len(cells) != width:
            ragged += 1
            cells = (cells + [""] * width)[:width]
        data.append(cells)
        src_rows.append(len(data))

    if not data:
        raise ValueError("No data rows were found below the header row.")
    if blanks:
        notes.append(f"Dropped {blanks} blank row(s).")
    if repeated:
        notes.append(f"Dropped {repeated} repeated header row(s) inside the data.")
    if ragged:
        notes.append(f"{ragged} row(s) had a different number of columns; padded or trimmed to {width}.")

    frame = pd.DataFrame(data, columns=columns, dtype=object)
    frame["_src_row"] = src_rows
    return RawTable(df=frame, source_name=source_name, notes=notes)


# --------------------------------------------------------------------------- names / units
def _clean_text(value: str) -> str:
    text = str(value).translate(_SUBSCRIPT).lower()
    text = text.replace("µ", "u").replace("μ", "u").replace("³", "3")
    text = text.replace("²", "2").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def canonical_pollutant(name: str | None) -> str | None:
    """Map a free-form pollutant name ("pm25", "Ozone", "NO2_ppb") to POLLUTANTS."""
    if name is None:
        return None
    text = _clean_text(name)
    if not text:
        return None
    for pollutant, pattern in _POLLUTANT_RES:
        if pattern.search(text):
            return pollutant
    return None


def canonical_unit(unit: str | None) -> str | None:
    """Map a free-form unit ("µg/m³", "ugm-3", "PPB") to a canonical unit key."""
    if unit is None:
        return None
    text = str(unit).strip().lower().replace("μ", "µ").replace("³", "3").replace(" ", "")
    text = text.replace("^", "").replace("·", "/")
    if text in _UNIT_ALIASES:
        return _UNIT_ALIASES[text]
    norm = text.replace("µ", "u")
    if norm in _UNIT_ALIASES:
        return _UNIT_ALIASES[norm]
    for alias, key in _UNIT_ALIASES.items():
        if norm == alias.replace(" ", ""):
            return key
    return None


def unit_in_header(header: str) -> str | None:
    """The unit written inside a column header, e.g. "PM2.5 (ug/m3)" -> "ug/m3"."""
    match = _UNIT_TOKEN.search(str(header))
    return canonical_unit(match.group(0)) if match else None


# --------------------------------------------------------------------------- heuristic_mapping
def _is_header_role(cleaned: str) -> tuple[str, str | None, str | None]:
    """Classify one header cell as (role, pollutant, unit)."""
    if re.search(r"\b(lat|latitude)\b", cleaned):
        return "lat", None, None
    if re.search(r"\b(lon|lng|long|longitude)\b", cleaned):
        return "lon", None, None
    if re.search(r"\b(ward|zone)\b|ward\s*(no|number)?", cleaned):
        return "ward", None, None
    if re.search(
        r"(time\s*stamp|datetime|date\s*(and|&|/)?\s*time|date_time|timestamp|\bts\b|\bobserved?\b)",
        cleaned,
    ):
        return "timestamp", None, None
    if re.search(r"station|site|location|monitor(ing|ed)?\b", cleaned):
        return "station", None, None
    if re.search(r"^\s*(date|day)\s*$|\b(date|day)\b", cleaned) and not re.search(r"update|valid", cleaned):
        return "date", None, None
    if re.search(r"\b(time|hour|hh)\b", cleaned):
        return "time", None, None
    if re.search(r"(parameter|param$|pollutant|species|measure\b|component)", cleaned):
        return "pollutant_name", None, None
    if re.search(r"(unit|uom)", cleaned):
        return "unit", None, None
    pollutant = canonical_pollutant(cleaned)
    if pollutant:
        return "pollutant_value", pollutant, unit_in_header(cleaned)
    if re.search(r"(value|conc|conc\.|reading|mean|average|obs)", cleaned):
        return "value", None, None
    return "ignore", None, None


def _date_format_hint(raw: RawTable, column: str) -> str | None:
    """Guess the date order from a sample of timestamp cells."""
    values = [str(v).strip() for v in raw.df[column].head(60) if str(v).strip()]
    day_first = month_first = 0
    has_time = False
    for value in values:
        if re.search(r"\d{1,2}:\d{2}", value):
            has_time = True
        match = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", value)
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(2))
        if first > 12:
            day_first += 1
        elif second > 12:
            month_first += 1
    if day_first and month_first:
        order = "DD/MM (mixed with MM/DD in the file)"
    elif month_first:
        order = "MM/DD"
    else:
        order = "DD/MM"
    return order + (" HH:mm" if has_time else "")


def heuristic_mapping(raw: RawTable) -> ColumnMapping:
    """Rule-based column mapping (the fallback when the LLM is unavailable)."""
    notes: list[str] = []
    df = raw.df
    chosen: dict[str, tuple[str, str | None, str | None]] = {}
    pollutant_columns: list[tuple[str, str, str | None]] = []
    for column in df.columns:
        if column == "_src_row":
            continue
        role, pollutant, unit = _is_header_role(_clean_text(column))
        if role == "pollutant_value" and pollutant:
            pollutant_columns.append((column, pollutant, unit))
            continue
        if role in ("station", "ward", "timestamp", "date", "time", "pollutant_name", "value", "unit", "lat", "lon"):
            if role not in chosen:
                chosen[role] = (column, pollutant, unit)

    roles: list[ColumnRole] = []
    layout: str = "long" if ("pollutant_name" in chosen and "value" in chosen) else "wide"
    if layout == "long":
        for role in ("station", "ward", "timestamp", "date", "time", "pollutant_name", "value", "unit", "lat", "lon"):
            if role in chosen:
                column, _, _ = chosen[role]
                roles.append(ColumnRole(column=column, role=role))  # type: ignore[arg-type]
    else:
        for role in ("station", "ward", "timestamp", "date", "time", "unit", "lat", "lon"):
            if role in chosen:
                column, _, _ = chosen[role]
                roles.append(ColumnRole(column=column, role=role))  # type: ignore[arg-type]
        for column, pollutant, unit in pollutant_columns:
            roles.append(ColumnRole(column=column, role="pollutant_value", pollutant=pollutant, unit=unit))

    if "timestamp" in chosen:
        hint = _date_format_hint(raw, chosen["timestamp"][0])
    elif "date" in chosen:
        hint = _date_format_hint(raw, chosen["date"][0])
    else:
        hint = None

    sentinels: list[str] = []
    seen: set[str] = set()
    sample = df.head(400)
    for column in df.columns:
        if column == "_src_row":
            continue
        for value in sample[column]:
            token = str(value).strip()
            if not token or len(token) > 6:
                continue
            key = token.upper()
            if key in {"NA", "N/A", "--", "-", "ND", "BDL", "-999", "9999", "NULL", "NONE"} and key not in seen:
                seen.add(key)
                sentinels.append(token)

    if "station" not in chosen:
        notes.append("No station column found; readings will be labelled 'Unknown station'.")
    if "timestamp" not in chosen and "date" not in chosen:
        notes.append("No timestamp or date column found; times cannot be placed.")
    if not pollutant_columns and layout == "wide":
        notes.append("No pollutant columns recognised; check the headers.")
    if layout == "long" and "unit" not in chosen:
        notes.append("Long layout without a unit column; units will be assumed.")
    if layout == "wide" and pollutant_columns:
        no_unit = [p for _, p, u in pollutant_columns if not u]
        if no_unit:
            notes.append(f"No unit in the header for {', '.join(sorted(set(no_unit)))}; assuming the standard unit.")

    return ColumnMapping(
        layout=layout,  # type: ignore[arg-type]
        columns=roles,
        date_format_hint=hint,
        sentinel_tokens=sentinels,
        issues=notes,
        source="heuristic",
    )


__all__ = [
    "RawTable",
    "canonical_pollutant",
    "canonical_unit",
    "heuristic_mapping",
    "read_input",
    "unit_in_header",
]
