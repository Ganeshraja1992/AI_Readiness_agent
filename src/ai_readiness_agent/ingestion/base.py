"""
Common contract every ingestion adapter (S3, RDS, Documents, ...future
sources) implements. The Data Profile / Readiness Engine only ever talk to
this shape, so adding a new source later (Snowflake, GCS, SharePoint, ...)
never touches downstream code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass
class SourceRecord:
    """One ingested "row" (a DB row, an S3 object's parsed rows, a document)."""

    fields: dict[str, Any]
    source_type: str  # "s3" | "rds" | "document"
    source_id: str  # bucket/key, table/pk, file path, etc.
    last_modified: datetime | None = None


@dataclass
class SourceBatch:
    """Everything pulled from one adapter for one profiling run."""

    source_type: str
    source_name: str
    records: list[SourceRecord] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    errors: list[str] = field(default_factory=list)
    # Bucket/table-level security posture (e.g. S3 public access block,
    # encryption, versioning) — not per-record, so it lives on the batch
    # rather than on individual SourceRecords. Empty for sources that don't
    # report this (RDS, documents, or S3 in mock mode).
    security_checks: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return len(self.records) == 0


class DataSourceAdapter(ABC):
    """Base class for all ingestion adapters.

    fetch() returns a LIST of batches rather than one, because a single
    "source" (e.g. an S3 bucket) commonly holds multiple objects with
    unrelated schemas (orders.json vs support_tickets.csv). Profiling those
    together as one schema would produce misleading null/type stats, so
    each adapter groups records into one batch per homogeneous
    object/table before handing them to the profiler.
    """

    source_type: str

    @abstractmethod
    def fetch(self) -> list[SourceBatch]:
        """Pull a representative sample of records from the source, grouped
        into one batch per homogeneous object/table."""
        raise NotImplementedError

    @staticmethod
    def _safe(records: Iterable[SourceRecord]) -> list[SourceRecord]:
        return list(records)
