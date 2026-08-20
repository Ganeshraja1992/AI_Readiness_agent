"""Pydantic models describing a Data Profile."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class FieldProfile(BaseModel):
    name: str
    inferred_type: str  # "string" | "numeric" | "boolean" | "date" | "empty"
    null_count: int = 0
    non_null_count: int = 0
    null_rate: float = 0.0
    distinct_count: int = 0
    sample_values: list[str] = Field(default_factory=list)


class SourceProfile(BaseModel):
    source_type: str
    source_name: str
    record_count: int = 0
    duplicate_record_count: int = 0
    duplicate_rate: float = 0.0
    fields: list[FieldProfile] = Field(default_factory=list)
    pii_findings: dict[str, int] = Field(default_factory=dict)  # kind -> count
    oldest_record_at: datetime | None = None
    newest_record_at: datetime | None = None
    ingestion_errors: list[str] = Field(default_factory=list)
    security_checks: dict[str, bool] = Field(default_factory=dict)  # check name -> passed

    @property
    def has_data(self) -> bool:
        return self.record_count > 0

    @property
    def freshness_days(self) -> float | None:
        if not self.newest_record_at:
            return None
        newest = self.newest_record_at
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - newest
        return max(delta.total_seconds() / 86400.0, 0.0)


class LLMSensitiveFinding(BaseModel):
    kind: str
    excerpt: str
    severity: str  # "info" | "warning" | "critical"
    explanation: str


class LLMQualityIssue(BaseModel):
    issue: str
    explanation: str


class LLMContentAnalysis(BaseModel):
    """Result of the optional Claude-powered deeper content analysis —
    supplements the regex-based PII scan with judgment calls a regex can't
    make (sensitive content in free text, data-quality issues, fit against
    the selected AI use case)."""

    performed: bool = False
    sensitive_data_findings: list[LLMSensitiveFinding] = Field(default_factory=list)
    quality_issues: list[LLMQualityIssue] = Field(default_factory=list)
    use_case_fit_score: float | None = None
    use_case_fit_notes: str = ""
    error: str | None = None


class DataProfile(BaseModel):
    customer_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sources: list[SourceProfile] = Field(default_factory=list)
    llm_analysis: LLMContentAnalysis | None = None

    @property
    def total_records(self) -> int:
        return sum(s.record_count for s in self.sources)

    @property
    def total_pii_findings(self) -> int:
        return sum(sum(s.pii_findings.values()) for s in self.sources)
