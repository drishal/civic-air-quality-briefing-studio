"""End-to-end data pipeline: raw input -> tidy table -> quality -> brief -> diff.

LLM text generation lives in llm.stages and is invoked by the UI per tab, so a failure there never
blocks the numeric pipeline.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core import analytics, clean, diff as diff_mod, ingest, quality, store
from core.models import Brief, ColumnMapping, DiffResult, QualitySummary


@dataclass
class PipelineResult:
    raw: ingest.RawTable
    mapping: ColumnMapping
    tidy: pd.DataFrame
    quality: QualitySummary
    brief: Brief
    previous: Optional[Brief]
    diff: DiffResult
    notes: list[str] = field(default_factory=list)


def run_pipeline(
    text: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    filename: Optional[str] = None,
    area: str = "My area",
    use_llm_mapper: bool = True,
    store_dir: str = ".briefs",
    save: bool = True,
    previous: Optional[Brief] = None,
) -> PipelineResult:
    """Run everything except the generative text stages. Raises ValueError for unusable input."""
    notes: list[str] = []
    raw = ingest.read_input(text=text, file_bytes=file_bytes, filename=filename)
    notes.extend(raw.notes)

    mapping: Optional[ColumnMapping] = None
    if use_llm_mapper:
        try:
            from llm import stages  # local import: keeps pipeline usable without the LLM layer

            mapping = stages.map_columns(raw)
        except Exception as exc:  # never let the mapper break the flow
            notes.append(f"AI column mapper unavailable ({type(exc).__name__}); used rule-based mapping.")
    if mapping is None:
        mapping = ingest.heuristic_mapping(raw)
        notes.append("Columns mapped with rule-based fallback.")
    else:
        notes.append("Columns mapped by Gemini and validated by code.")

    tidy, log = clean.apply_mapping(raw, mapping)
    tidy = quality.flag_readings(tidy)
    qsum = quality.quality_summary(raw, tidy, log)
    if qsum.n_readings:
        # Honest usability: fault-flagged readings do not count, unit notes are reported apart.
        notes.append(
            f"{qsum.n_usable} of {qsum.n_readings} readings are usable "
            f"({qsum.usable_share:.0%}); {sum(qsum.info_flag_counts.values())} carry a unit note only."
        )

    src_hash = hashlib.sha256(
        (text.encode() if text is not None else (file_bytes or b""))
    ).hexdigest()[:12]
    brief = analytics.build_brief(tidy, qsum, area=area, source_name=raw.source_name, source_hash=src_hash)
    brief.brief_id = brief.brief_id or uuid.uuid4().hex[:8]
    brief.created_at = brief.created_at or datetime.now(timezone.utc).isoformat()

    prev = previous if previous is not None else store.load_latest(store_dir, exclude_id=brief.brief_id)
    d = diff_mod.diff_briefs(brief, prev)
    if save:
        try:
            store.save_brief(brief, store_dir)
        except Exception as exc:
            notes.append(f"Could not save brief history ({exc}).")
    return PipelineResult(raw, mapping, tidy, qsum, brief, prev, d, notes)
