"""
Document ingestion adapter.

Real mode: when `DocumentsConfig.bucket` and `.s3_prefix` are both set (the
webapp uploads documents there), lists and downloads objects under that
S3 prefix via boto3.

Local/mock mode (default, no AWS needed): scans a local directory —
`mock_data/documents` by default, or wherever `READINESS_DOCUMENTS_DIR`
points.

Either way, each file becomes a SourceRecord describing it: name,
extension, size, and — for text-like files — a lightweight content sample
used for PII heuristics downstream, including real PDF text extraction via
pypdf.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ai_readiness_agent.config import DocumentsConfig
from ai_readiness_agent.ingestion.base import DataSourceAdapter, SourceBatch, SourceRecord

TEXT_LIKE_EXTENSIONS = {".txt", ".csv", ".json", ".md", ".log"}
MAX_SAMPLE_BYTES = 20_000


class DocumentsAdapter(DataSourceAdapter):
    source_type = "document"

    def __init__(self, config: DocumentsConfig):
        self.config = config

    def fetch(self) -> list[SourceBatch]:
        if self.config.bucket and self.config.s3_prefix:
            return self._fetch_s3()
        return self._fetch_local()

    # ------------------------------------------------------------------
    # Local path
    # ------------------------------------------------------------------
    def _fetch_local(self) -> list[SourceBatch]:
        directory = self.config.directory
        # Use just the folder's own name, not the full local path — the
        # full path can reveal internal filesystem structure and this
        # label ends up in finding text that's part of the minimal payload
        # sent to the Control Plane.
        batch = SourceBatch(source_type=self.source_type, source_name=directory.name or "documents")
        if not directory.exists():
            batch.errors.append(f"documents directory not found: {directory}")
            return [batch]

        for path in sorted(directory.glob("**/*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
                fields = {
                    "file_name": path.name,
                    "extension": path.suffix.lower(),
                    "size_bytes": stat.st_size,
                    "text_sample": self._extract_text_sample(path.suffix.lower(), path.read_bytes()),
                }
                batch.records.append(
                    SourceRecord(
                        fields=fields,
                        source_type="document",
                        source_id=str(path),
                        last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                batch.errors.append(f"failed to read document {path}: {exc}")
        return [batch]

    # ------------------------------------------------------------------
    # Real AWS path
    # ------------------------------------------------------------------
    def _fetch_s3(self) -> list[SourceBatch]:
        import boto3  # imported lazily so local mode has zero AWS dependency

        batch = SourceBatch(source_type=self.source_type, source_name=self.config.bucket)
        client = boto3.client("s3", region_name=self.config.region)
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.config.bucket, Prefix=self.config.s3_prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith("/"):
                        continue
                    file_name = key.rsplit("/", 1)[-1]
                    suffix = ("." + file_name.rsplit(".", 1)[-1].lower()) if "." in file_name else ""
                    try:
                        body = client.get_object(Bucket=self.config.bucket, Key=key)["Body"].read()
                        fields = {
                            "file_name": file_name,
                            "extension": suffix,
                            "size_bytes": obj.get("Size", len(body)),
                            "text_sample": self._extract_text_sample(suffix, body),
                        }
                        batch.records.append(
                            SourceRecord(
                                fields=fields,
                                source_type="document",
                                source_id=f"s3://{self.config.bucket}/{key}",
                                last_modified=obj.get("LastModified"),
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        batch.errors.append(f"failed to read s3://{self.config.bucket}/{key}: {exc}")
        except Exception as exc:  # noqa: BLE001
            batch.errors.append(f"failed to list s3://{self.config.bucket}/{self.config.s3_prefix}: {exc}")
        return [batch]

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_text_sample(suffix: str, raw: bytes) -> str:
        if suffix == ".pdf":
            return DocumentsAdapter._extract_pdf_text_sample(raw)
        if suffix not in TEXT_LIKE_EXTENSIONS:
            # Extension point: plug python-docx / textract here for .docx/.doc
            # in a real deployment.
            return ""
        try:
            return raw.decode("utf-8", errors="ignore")[:MAX_SAMPLE_BYTES]
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _extract_pdf_text_sample(raw: bytes) -> str:
        import io

        from pypdf import PdfReader  # imported lazily so non-PDF paths have zero pypdf cost

        try:
            reader = PdfReader(io.BytesIO(raw))
            chunks: list[str] = []
            total = 0
            for page in reader.pages:
                text = page.extract_text() or ""
                chunks.append(text)
                total += len(text)
                if total >= MAX_SAMPLE_BYTES:
                    break
            return "".join(chunks)[:MAX_SAMPLE_BYTES]
        except Exception:  # noqa: BLE001 - e.g. encrypted/corrupt PDF; skip rather than fail ingestion
            return ""
