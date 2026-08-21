"""
PII masking for real data -- rewrites the exact values captured by
profiling/models.py's PIIOccurrence, in place at the source.

Only regex-detected occurrences are masked (see PIIOccurrence's docstring
for why Comprehend's findings aren't locatable the same way). Two backends:
S3 (this module) and RDS (rds_masking.py, kept deliberately separate given
how much higher-risk a live UPDATE against production data is compared to
rewriting an S3 object that already has versioning enabled as a safety net).

Always preview before apply: build_preview() is a pure, read-only
computation over the already-completed assessment's DataProfile (no new
AWS calls); only apply_s3_masking() actually touches AWS, and only after
the caller has shown the preview and gotten explicit confirmation.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass

from ai_readiness_agent.profiling.models import DataProfile

# Occurrence kinds the regex scanner produces -- Comprehend's uppercase
# entity types are intentionally excluded (see PIIOccurrence docstring).
MASKABLE_KINDS = {"ssn", "email", "phone", "credit_card"}


@dataclass(frozen=True)
class MaskPreviewItem:
    source_type: str
    record_id: str  # e.g. "s3://bucket/key.json"
    field_name: str
    kind: str
    matched_value: str
    masked_value: str


def mask_value(kind: str, matched: str) -> str:
    """Redacts a matched PII substring, keeping just enough shape/context
    to stay useful in a preview (e.g. last 4 digits) without exposing the
    original value."""
    if kind == "ssn":
        digits = re.sub(r"\D", "", matched)
        return f"***-**-{digits[-4:]}" if len(digits) >= 4 else "***-**-****"
    if kind == "credit_card":
        digits = re.sub(r"\D", "", matched)
        return f"**** **** **** {digits[-4:]}" if len(digits) >= 4 else "**** **** **** ****"
    if kind == "email":
        local, _, domain = matched.partition("@")
        if not domain:
            return "***@***"
        visible = local[0] if local else "*"
        return f"{visible}***@{domain}"
    if kind == "phone":
        digits = re.sub(r"\D", "", matched)
        return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***-***-****"
    return "***REDACTED***"


def build_preview(profile: DataProfile, source_type: str) -> list[MaskPreviewItem]:
    """Every distinct (record, field, kind, value) maskable occurrence for
    the given source type, deduped -- a duplicate value appearing twice in
    the same field/record only needs to be listed (and masked) once."""
    seen: set[tuple[str, str, str, str]] = set()
    items: list[MaskPreviewItem] = []
    for source in profile.sources:
        if source.source_type != source_type:
            continue
        for occ in source.pii_occurrences:
            if occ.kind not in MASKABLE_KINDS:
                continue
            key = (occ.record_id, occ.field_name, occ.kind, occ.matched_value)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                MaskPreviewItem(
                    source_type=occ.source_type,
                    record_id=occ.record_id,
                    field_name=occ.field_name,
                    kind=occ.kind,
                    matched_value=occ.matched_value,
                    masked_value=mask_value(occ.kind, occ.matched_value),
                )
            )
    return items


def _parse_bucket_key(record_id: str) -> tuple[str, str]:
    without_scheme = record_id[len("s3://"):] if record_id.startswith("s3://") else record_id
    bucket, _, key = without_scheme.partition("/")
    return bucket, key


def _mask_row(row: dict, items: list[MaskPreviewItem]) -> bool:
    changed = False
    for item in items:
        value = row.get(item.field_name)
        if isinstance(value, str) and item.matched_value in value:
            row[item.field_name] = value.replace(item.matched_value, item.masked_value)
            changed = True
    return changed


def _mask_one_s3_object(client, bucket: str, key: str, items: list[MaskPreviewItem]) -> None:
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    lower = key.lower()

    if lower.endswith(".json"):
        data = json.loads(body.decode("utf-8"))
        was_list = isinstance(data, list)
        rows = data if was_list else [data]
        for row in rows:
            _mask_row(row, items)
        new_body = json.dumps(rows if was_list else rows[0], indent=2).encode("utf-8")

    elif lower.endswith(".jsonl"):
        new_lines = []
        for line in body.decode("utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                new_lines.append(line)
                continue
            row = json.loads(stripped)
            _mask_row(row, items)
            new_lines.append(json.dumps(row))
        new_body = "\n".join(new_lines).encode("utf-8")

    elif lower.endswith(".csv"):
        reader = csv.DictReader(io.StringIO(body.decode("utf-8")))
        fieldnames = reader.fieldnames or []
        rows = list(reader)
        for row in rows:
            _mask_row(row, items)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        new_body = buf.getvalue().encode("utf-8")

    else:
        raise ValueError(f"unsupported object type for masking: {key}")

    client.put_object(Bucket=bucket, Key=key, Body=new_body)


def apply_s3_masking(items: list[MaskPreviewItem], region: str) -> tuple[list[str], list[str]]:
    """Re-fetches, rewrites, and PUTs back every S3 object referenced by
    `items` (grouped so each object is only fetched/written once even if
    it has multiple maskable occurrences). Returns (objects applied ok,
    errors) -- one object's failure doesn't stop the others."""
    import boto3  # imported lazily so previewing has zero AWS dependency

    client = boto3.client("s3", region_name=region)

    by_object: dict[tuple[str, str], list[MaskPreviewItem]] = {}
    for item in items:
        bucket, key = _parse_bucket_key(item.record_id)
        by_object.setdefault((bucket, key), []).append(item)

    applied: list[str] = []
    errors: list[str] = []
    for (bucket, key), object_items in by_object.items():
        try:
            _mask_one_s3_object(client, bucket, key, object_items)
            applied.append(f"{bucket}/{key}")
        except Exception as exc:  # noqa: BLE001 - report every failure, don't let one abort the rest
            errors.append(f"{bucket}/{key}: {exc}")
    return applied, errors
