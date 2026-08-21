"""
RDS ingestion adapter.

Real mode: connects via SQLAlchemy and pulls a sample of rows from the
configured table. Covers every engine Amazon RDS actually offers:
PostgreSQL, MySQL, MariaDB, Oracle, and SQL Server -- given a proper
connection string built from RDSConfig.

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

# RDSConfig.engine -> SQLAlchemy dialect+driver. Using the wrong one against
# a real server doesn't fail fast -- the client sends the wrong wire
# protocol and the connection just hangs until something times out.
_DIALECT_DRIVERS = {
    "postgresql": "postgresql+psycopg2",
    "mysql": "mysql+pymysql",
    "mariadb": "mysql+pymysql",  # MariaDB speaks the MySQL wire protocol
    "oracle": "oracle+oracledb",
    "mssql": "mssql+pymssql",
}

# Each DBAPI names its connection-establishment timeout differently.
_CONNECT_TIMEOUT_KWARG = {
    "postgresql": "connect_timeout",
    "mysql": "connect_timeout",
    "mariadb": "connect_timeout",
    "oracle": "tcp_connect_timeout",
    "mssql": "login_timeout",
}


def _select_sql(engine: str, table: str) -> str:
    """"Give me at most N rows" is spelled differently per dialect --
    LIMIT isn't SQL-standard and neither Oracle nor SQL Server support it."""
    if engine == "oracle":
        return f"SELECT * FROM {table} FETCH FIRST :limit ROWS ONLY"
    if engine == "mssql":
        return f"SELECT TOP (:limit) * FROM {table}"
    return f"SELECT * FROM {table} LIMIT :limit"


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
        dialect = _DIALECT_DRIVERS.get(self.config.engine)
        if dialect is None:
            batch.errors.append(
                f"unknown RDS engine {self.config.engine!r}; expected one of {sorted(_DIALECT_DRIVERS)}"
            )
            return batch
        if self.config.engine == "oracle":
            # Oracle distinguishes SIDs from service names; putting the
            # database in the URL path gets parsed as a SID (and RDS/PDB
            # databases are service names, not SIDs) -- "DPY-6003: SID ...
            # is not registered with the listener" even though the service
            # exists. service_name as a query param is what actually works.
            url = (
                f"{dialect}://{self.config.username}:{self.config.password}"
                f"@{self.config.host}:{self.config.port}/?service_name={self.config.database}"
            )
        else:
            url = (
                f"{dialect}://{self.config.username}:{self.config.password}"
                f"@{self.config.host}:{self.config.port}/{self.config.database}"
            )
        try:
            # A misconfigured security group / wrong host+port hangs the TCP
            # handshake rather than failing fast -- cap it well under
            # gunicorn's worker timeout so the request comes back with a
            # real error instead of an empty/dropped connection.
            timeout_kwarg = _CONNECT_TIMEOUT_KWARG[self.config.engine]
            engine = create_engine(url, pool_pre_ping=True, connect_args={timeout_kwarg: 10})
            with engine.connect() as conn:
                result = conn.execute(
                    text(_select_sql(self.config.engine, self.config.table)),
                    {"limit": self.sample_limit},
                )
                columns = result.keys()
                now = datetime.now(timezone.utc)
                for row in result:
                    fields = dict(zip(columns, row))
                    # Oracle folds unquoted column names to uppercase.
                    pk = fields.get("id") or fields.get("ID") or ""
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
