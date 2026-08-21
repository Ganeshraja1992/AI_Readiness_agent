"""
PII masking for RDS rows -- runs targeted, parameterized UPDATE statements
against the exact row (by primary key) and column captured by
profiling/models.py's PIIOccurrence.

Deliberately more conservative than pii_masking.py's S3 path: a live
UPDATE against a customer's real database has no automatic undo the way
S3's versioning gives S3 masking, so:
  - only rows with a detected primary key are ever masked (see
    rds_adapter.py's `pk` extraction, "id"/"ID" column only) -- no primary
    key, no UPDATE, full stop. Those occurrences are counted and excluded,
    never silently dropped.
  - the UPDATE is always scoped to exactly one row (WHERE id = :pk) and
    exactly one column, using REPLACE() to swap only the matched
    substring -- never a blanket column-wide or table-wide UPDATE.
  - matched/masked values are always bound parameters, never
    string-interpolated into SQL. Table and column names ARE interpolated
    (REPLACE/UPDATE can't parameterize identifiers), but they come from
    this table's own schema (rds_adapter.py's `result.keys()`), not from
    request input -- the same trust boundary rds_adapter.py's own
    `SELECT * FROM {table}` already relies on.
  - each row gets its own transaction, so one row's failure can't cascade
    into rejecting every later row in the same batch (some databases
    reject all further statements in a transaction after one errors).
"""
from __future__ import annotations

from dataclasses import dataclass

from ai_readiness_agent.profiling.models import DataProfile
from ai_readiness_agent.remediation.pii_masking import MASKABLE_KINDS, mask_value


@dataclass(frozen=True)
class RDSMaskPreviewItem:
    record_id: str  # "{database}.{table}#{pk}"
    database: str
    table: str
    pk: str
    field_name: str  # column name
    kind: str
    matched_value: str
    masked_value: str


def _parse_record_id(record_id: str) -> tuple[str, str, str]:
    """-> (database, table, pk). pk is "" if none could be parsed (no
    usable primary key was found for this row at ingest time)."""
    left, sep, pk = record_id.rpartition("#")
    if not sep or not pk:
        return "", "", ""
    database, dot, table = left.partition(".")
    if not dot:
        return "", "", ""
    return database, table, pk


def build_preview(profile: DataProfile) -> tuple[list[RDSMaskPreviewItem], int]:
    """Returns (maskable items, count of occurrences excluded for having no
    detectable primary key). Every distinct (row, column, kind, value) is
    deduped, same as the S3 preview."""
    seen: set[tuple[str, str, str, str]] = set()
    items: list[RDSMaskPreviewItem] = []
    excluded = 0
    for source in profile.sources:
        if source.source_type != "rds":
            continue
        for occ in source.pii_occurrences:
            if occ.kind not in MASKABLE_KINDS:
                continue
            database, table, pk = _parse_record_id(occ.record_id)
            if not pk:
                excluded += 1
                continue
            key = (occ.record_id, occ.field_name, occ.kind, occ.matched_value)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                RDSMaskPreviewItem(
                    record_id=occ.record_id,
                    database=database,
                    table=table,
                    pk=pk,
                    field_name=occ.field_name,
                    kind=occ.kind,
                    matched_value=occ.matched_value,
                    masked_value=mask_value(occ.kind, occ.matched_value),
                )
            )
    return items, excluded


def apply_rds_masking(items: list[RDSMaskPreviewItem], connector: dict) -> tuple[int, list[str]]:
    """Runs one parameterized UPDATE per item, each in its own transaction.
    Returns (rows successfully updated, errors) -- one row's failure
    doesn't stop the rest."""
    from sqlalchemy import create_engine, text

    from ai_readiness_agent.ingestion.rds_adapter import _CONNECT_TIMEOUT_KWARG, _DIALECT_DRIVERS

    engine_name = connector.get("engine", "postgresql")
    dialect = _DIALECT_DRIVERS.get(engine_name)
    if dialect is None:
        return 0, [f"unknown RDS engine {engine_name!r}"]

    if engine_name == "oracle":
        url = (
            f"{dialect}://{connector['username']}:{connector['password']}"
            f"@{connector['host']}:{connector['port']}/?service_name={connector['database']}"
        )
    else:
        url = (
            f"{dialect}://{connector['username']}:{connector['password']}"
            f"@{connector['host']}:{connector['port']}/{connector['database']}"
        )

    try:
        engine = create_engine(
            url, pool_pre_ping=True, connect_args={_CONNECT_TIMEOUT_KWARG[engine_name]: 10}
        )
    except Exception as exc:  # noqa: BLE001
        return 0, [f"connection failed: {exc}"]

    applied = 0
    errors: list[str] = []
    for item in items:
        try:
            with engine.begin() as conn:
                stmt = text(
                    f"UPDATE {item.table} SET {item.field_name} = "
                    f"REPLACE({item.field_name}, :old, :new) WHERE id = :pk"
                )
                conn.execute(stmt, {"old": item.matched_value, "new": item.masked_value, "pk": item.pk})
            applied += 1
        except Exception as exc:  # noqa: BLE001 - one row's failure shouldn't stop the rest
            errors.append(f"{item.table}#{item.pk}.{item.field_name}: {exc}")

    return applied, errors
