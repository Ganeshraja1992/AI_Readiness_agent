"""
RDS ingestion adapter.

Real mode: connects via SQLAlchemy (works against Postgres/MySQL RDS
instances given a proper connection string built from RDSConfig) and pulls a
sample of rows from the configured table.

Mock mode (default): reads mock_data/rds_customers.csv so the pipeline runs
with no database at all.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone

from ai_readiness_agent.config import RDSConfig
from ai_readiness_agent.ingestion.base import DataSourceAdapter, SourceBatch, SourceRecord

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_LIMIT = 5000


class RDSAdapter(DataSourceAdapter):
    source_type = "rds"

    def __init__(self, config: RDSConfig, sample_limit: int = DEFAULT_SAMPLE_LIMIT):
        self.config = config
        self.sample_limit = sample_limit

    def fetch(self) -> list[SourceBatch]:
        if self.config.use_mock:
            return [self._fetch_mock()]
        return [self._fetch_real()]

    # ------------------------------------------------------------------
    def _fetch_mock(self) -> SourceBatch:
        batch = SourceBatch(source_type=self.source_type, source_name=self.config.table)
        path = self.config.mock_csv
        if not path.exists():
            batch.errors.append(f"mock RDS csv not found: {path}")
            return batch

        now = datetime.now(timezone.utc)
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for i, row in enumerate(reader):
                if i >= self.sample_limit:
                    break
                batch.records.append(
                    SourceRecord(
                        fields=dict(row),
                        source_type="rds",
                        source_id=f"{self.config.database}.{self.config.table}#{row.get('id', i)}",
                        last_modified=now,
                    )
                )
        return batch

    # ------------------------------------------------------------------
    def _fetch_real(self) -> SourceBatch:
        from sqlalchemy import create_engine, text  # lazy import

        batch = SourceBatch(source_type=self.source_type, source_name=self.config.table)
        url = (
            f"postgresql+psycopg2://{self.config.username}:{self.config.password}"
            f"@{self.config.host}:{self.config.port}/{self.config.database}"
        )
        try:
            engine = create_engine(url, pool_pre_ping=True)
            with engine.connect() as conn:
                result = conn.execute(
                    text(f"SELECT * FROM {self.config.table} LIMIT :limit"),
                    {"limit": self.sample_limit},
                )
                columns = result.keys()
                now = datetime.now(timezone.utc)
                for row in result:
                    fields = dict(zip(columns, row))
                    pk = fields.get("id", "")
                    batch.records.append(
                        SourceRecord(
                            fields=fields,
                            source_type="rds",
                            source_id=f"{self.config.database}.{self.config.table}#{pk}",
                            last_modified=now,
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            batch.errors.append(f"failed to query {self.config.table}: {exc}")
        return batch
