"""
S3 ingestion adapter.

Real mode: lists objects under `bucket/prefix` via boto3, downloads
JSON/CSV objects and parses them into SourceRecords.

Mock mode (default, no AWS creds needed): reads the same shapes from
mock_data/s3/*.json|*.csv so the whole pipeline runs standalone.

Each object becomes its OWN SourceBatch (named "bucket/key") rather than
all objects being merged into one — a bucket routinely holds files with
unrelated schemas (e.g. orders.json vs support_tickets.csv), and profiling
those as a single merged schema would produce misleading null/type stats.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ai_readiness_agent.config import S3Config
from ai_readiness_agent.ingestion.base import DataSourceAdapter, SourceBatch, SourceRecord

logger = logging.getLogger(__name__)


class S3Adapter(DataSourceAdapter):
    source_type = "s3"

    def __init__(self, config: S3Config):
        self.config = config

    def fetch(self, *, include_data: bool = True, include_security: bool = True) -> list[SourceBatch]:
        """`include_data` lists/downloads bucket objects; `include_security`
        runs the bucket-level security checks. Both default True for the
        combined pipeline; the webapp's standalone "Scan S3" / "Security
        scan" buttons set only one so each runs independently."""
        if self.config.use_mock:
            return self._fetch_mock()
        return self._fetch_real(include_data=include_data, include_security=include_security)

    # ------------------------------------------------------------------
    # Mock path
    # ------------------------------------------------------------------
    def _fetch_mock(self) -> list[SourceBatch]:
        mock_dir: Path = self.config.mock_dir
        if not mock_dir.exists():
            batch = SourceBatch(source_type=self.source_type, source_name=self.config.bucket)
            batch.errors.append(f"mock S3 directory not found: {mock_dir}")
            return [batch]

        batches: list[SourceBatch] = []
        for path in sorted(mock_dir.glob("**/*")):
            if not path.is_file():
                continue
            object_name = f"{self.config.bucket}/{path.name}"
            batch = SourceBatch(source_type=self.source_type, source_name=object_name)
            try:
                records = self._parse_object(path.name, path.read_bytes())
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                for rec in records:
                    rec.source_id = f"s3://{object_name}"
                    rec.last_modified = mtime
                batch.records.extend(records)
            except Exception as exc:  # noqa: BLE001 - want to keep profiling resilient
                batch.errors.append(f"failed to parse mock object {path.name}: {exc}")
            batches.append(batch)
        return batches or [SourceBatch(source_type=self.source_type, source_name=self.config.bucket)]

    # ------------------------------------------------------------------
    # Real AWS path
    # ------------------------------------------------------------------
    def _fetch_real(self, *, include_data: bool = True, include_security: bool = True) -> list[SourceBatch]:
        import boto3  # imported lazily so mock mode has zero AWS dependency

        batches: list[SourceBatch] = []
        client = boto3.client("s3", region_name=self.config.region)

        if include_security:
            security_batch = SourceBatch(
                source_type=self.source_type,
                source_name=f"{self.config.bucket} (security)",
            )
            security_batch.security_checks = self._check_bucket_security(client, security_batch.errors)
            batches.append(security_batch)

        if include_data:
            try:
                paginator = client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=self.config.bucket, Prefix=self.config.prefix):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        if not key.lower().endswith((".json", ".csv", ".jsonl")):
                            continue
                        object_name = f"{self.config.bucket}/{key}"
                        batch = SourceBatch(source_type=self.source_type, source_name=object_name)
                        try:
                            body = client.get_object(Bucket=self.config.bucket, Key=key)["Body"].read()
                            records = self._parse_object(key, body)
                            for rec in records:
                                rec.source_id = f"s3://{object_name}"
                                rec.last_modified = obj.get("LastModified")
                            batch.records.extend(records)
                        except Exception as exc:  # noqa: BLE001
                            batch.errors.append(f"failed to read s3://{object_name}: {exc}")
                        batches.append(batch)
            except Exception as exc:  # noqa: BLE001
                err_batch = SourceBatch(source_type=self.source_type, source_name=self.config.bucket)
                err_batch.errors.append(f"failed to list bucket {self.config.bucket}: {exc}")
                batches.append(err_batch)
        return batches or [SourceBatch(source_type=self.source_type, source_name=self.config.bucket)]

    # ------------------------------------------------------------------
    def _check_bucket_security(self, client, errors: list[str]) -> dict[str, bool]:
        """Bucket-level security posture, independent of any object content:
        public access block, default encryption, and versioning. Each check
        is independent — one missing/erroring config shouldn't hide the
        others, so failures are caught per-check rather than around the
        whole method."""
        checks: dict[str, bool] = {}

        try:
            pab = client.get_public_access_block(Bucket=self.config.bucket)
            config = pab.get("PublicAccessBlockConfiguration", {})
            checks["public_access_blocked"] = all(
                config.get(flag, False)
                for flag in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
            )
        except Exception as exc:  # noqa: BLE001
            checks["public_access_blocked"] = False
            errors.append(f"could not verify public access block on {self.config.bucket}: {exc}")

        try:
            client.get_bucket_encryption(Bucket=self.config.bucket)
            checks["encryption_enabled"] = True
        except Exception as exc:  # noqa: BLE001
            # AWS raises ServerSideEncryptionConfigurationNotFoundError (no
            # config at all) the same way as any other client error, so this
            # is the expected path for an unencrypted bucket, not just a
            # transport failure.
            checks["encryption_enabled"] = False
            errors.append(f"no default encryption configured on {self.config.bucket}: {exc}")

        try:
            versioning = client.get_bucket_versioning(Bucket=self.config.bucket)
            checks["versioning_enabled"] = versioning.get("Status") == "Enabled"
        except Exception as exc:  # noqa: BLE001
            checks["versioning_enabled"] = False
            errors.append(f"could not verify versioning on {self.config.bucket}: {exc}")

        return checks

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_object(name: str, raw: bytes) -> list[SourceRecord]:
        records: list[SourceRecord] = []
        if name.lower().endswith(".json"):
            data = json.loads(raw.decode("utf-8"))
            rows = data if isinstance(data, list) else [data]
            for row in rows:
                records.append(SourceRecord(fields=row, source_type="s3", source_id=name))
        elif name.lower().endswith(".jsonl"):
            for line in raw.decode("utf-8").splitlines():
                line = line.strip()
                if line:
                    records.append(SourceRecord(fields=json.loads(line), source_type="s3", source_id=name))
        elif name.lower().endswith(".csv"):
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
            for row in reader:
                records.append(SourceRecord(fields=dict(row), source_type="s3", source_id=name))
        return records
