"""
Data Profiler: turns raw SourceBatches (from the ingestion adapters) into a
structured DataProfile the Readiness Engine can score.

This is the "Data Profile" box in the architecture diagram.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime

from ai_readiness_agent.config import ComprehendConfig
from ai_readiness_agent.ingestion.base import SourceBatch, SourceRecord
from ai_readiness_agent.profiling import comprehend_pii
from ai_readiness_agent.profiling.models import DataProfile, FieldProfile, SourceProfile
from ai_readiness_agent.profiling.pii import merge_findings, scan_text

_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?")
MAX_SAMPLE_VALUES = 3
# Amazon Comprehend PII scanning only makes sense on S3 objects / uploaded
# documents (free-text-ish content) — not on the structured RDS rows.
_COMPREHEND_SOURCE_TYPES = {"s3", "document"}


def _infer_type(values: list) -> str:
    non_empty = [v for v in values if v not in (None, "")]
    if not non_empty:
        return "empty"
    if all(isinstance(v, bool) for v in non_empty):
        return "boolean"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_empty):
        return "numeric"
    str_vals = [str(v) for v in non_empty]
    if all(_NUMERIC_RE.match(v) for v in str_vals):
        return "numeric"
    if all(_DATE_RE.match(v) for v in str_vals):
        return "date"
    return "string"


def _record_fingerprint(record: SourceRecord) -> str:
    try:
        return json.dumps(record.fields, sort_keys=True, default=str)
    except TypeError:
        return str(sorted(record.fields.items()))


def _build_comprehend_sample(batch: SourceBatch, max_chars: int) -> str:
    """Concatenate a sample of each record's text (the document adapter's
    text_sample, or stringified field values for S3 records) up to
    max_chars — one Comprehend call per source is enough for a readiness
    signal without scanning every record."""
    chunks: list[str] = []
    total = 0
    for r in batch.records:
        sample = r.fields.get("text_sample")
        piece = sample if isinstance(sample, str) and sample else " ".join(
            str(v) for v in r.fields.values() if isinstance(v, (str, int, float))
        )
        if not piece:
            continue
        chunks.append(piece)
        total += len(piece)
        if total >= max_chars:
            break
    return "\n".join(chunks)[:max_chars]


def profile_source(batch: SourceBatch, comprehend_config: ComprehendConfig | None = None) -> SourceProfile:
    profile = SourceProfile(
        source_type=batch.source_type,
        source_name=batch.source_name,
        record_count=len(batch.records),
        ingestion_errors=list(batch.errors),
        security_checks=dict(batch.security_checks),
    )
    if not batch.records:
        return profile

    # --- duplicates -----------------------------------------------------
    fingerprints = Counter(_record_fingerprint(r) for r in batch.records)
    profile.duplicate_record_count = sum(c - 1 for c in fingerprints.values() if c > 1)
    profile.duplicate_rate = profile.duplicate_record_count / profile.record_count

    # --- per-field stats --------------------------------------------------
    field_names: list[str] = []
    for r in batch.records:
        for k in r.fields.keys():
            if k not in field_names:
                field_names.append(k)

    pii_findings_total: dict[str, int] = {}
    for name in field_names:
        raw_values = [r.fields.get(name) for r in batch.records]
        non_null = [v for v in raw_values if v not in (None, "")]
        fp = FieldProfile(
            name=name,
            inferred_type=_infer_type(raw_values),
            null_count=len(raw_values) - len(non_null),
            non_null_count=len(non_null),
            null_rate=(len(raw_values) - len(non_null)) / len(raw_values) if raw_values else 0.0,
            distinct_count=len({str(v) for v in non_null}),
            sample_values=[str(v) for v in non_null[:MAX_SAMPLE_VALUES]],
        )
        profile.fields.append(fp)

        # PII scan on string-ish fields
        for v in non_null:
            if isinstance(v, str):
                findings = scan_text(v)
                if findings:
                    pii_findings_total = merge_findings(pii_findings_total, findings)

    # documents adapter stashes a larger text sample separately
    for r in batch.records:
        sample = r.fields.get("text_sample")
        if isinstance(sample, str) and sample:
            findings = scan_text(sample)
            if findings:
                pii_findings_total = merge_findings(pii_findings_total, findings)

    # real AWS PII detection (Amazon Comprehend), S3/document sources only
    if comprehend_config and comprehend_config.enabled and batch.source_type in _COMPREHEND_SOURCE_TYPES:
        sample_text = _build_comprehend_sample(batch, comprehend_config.max_chars_per_call)
        comprehend_findings = comprehend_pii.scan_text(sample_text, comprehend_config)
        if comprehend_findings:
            pii_findings_total = merge_findings(pii_findings_total, comprehend_findings)

    profile.pii_findings = pii_findings_total

    # --- freshness ----------------------------------------------------
    timestamps: list[datetime] = [r.last_modified for r in batch.records if r.last_modified]
    if timestamps:
        profile.oldest_record_at = min(timestamps)
        profile.newest_record_at = max(timestamps)

    return profile


def build_data_profile(
    customer_id: str, batches: list[SourceBatch], comprehend_config: ComprehendConfig | None = None
) -> DataProfile:
    profile = DataProfile(customer_id=customer_id)
    for batch in batches:
        profile.sources.append(profile_source(batch, comprehend_config))
    return profile
