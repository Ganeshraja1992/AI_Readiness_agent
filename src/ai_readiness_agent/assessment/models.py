"""
Pydantic models for the Assessment Result.

Two shapes live here, deliberately kept separate:

  * AssessmentResult — the FULL internal result, including the DataProfile
    (which can contain sample field values pulled from S3/RDS/documents).
    This never leaves the customer's AWS environment: the agent may write
    it to a local audit log, but it is not what crosses the Secure Result
    Channel.

  * ControlPlanePayload — the "minimal result payload" that actually gets
    signed and sent to Member 3's Control Plane. Its shape matches the
    Assessment Result Contract in Member 3's Backend & Cloud MVP Spec
    (section 9) exactly: assessment_id, use_case, status, score,
    readiness_status, dimensions, findings, remediation, projected_score.
    It intentionally carries no raw record data, only aggregate stats,
    field/column names, and human-readable summaries — per the spec's
    "Never require raw S3 objects, database rows, or documents to be
    uploaded to the control plane" / "No raw enterprise data in backend
    persistence" requirements.

AssessmentResult.to_control_plane_payload() converts one to the other.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from ai_readiness_agent.profiling.models import DataProfile


class ReadinessLevel(str, Enum):
    """Values match the `readiness_status` strings in Member 3's contract."""

    NOT_READY = "NOT_READY"
    NEEDS_WORK = "NEEDS_WORK"
    READY = "READY"
    AI_READY = "AI_READY"


class AssessmentStatus(str, Enum):
    """Mirrors the backend's assessment state machine (spec section 6);
    the agent only ever reports COMPLETED or FAILED for its own step."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DimensionScore(BaseModel):
    name: str
    score: float  # 0-100
    weight: float  # 0-1
    summary: str


class Finding(BaseModel):
    finding_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    severity: str  # "info" | "warning" | "critical"
    type: str  # dimension/category this finding belongs to, e.g. "privacy_risk"
    title: str
    impact: str
    source: str | None = None  # e.g. "s3:orders.json" — kept internally, dropped in payload


class RemediationAction(BaseModel):
    action_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action: str
    effort: str  # "low" | "medium" | "high"
    projected_score_delta: float
    dimension: str = ""  # which DimensionScore this came from -- lets the UI attach an "Apply fix" action


class AssessmentResult(BaseModel):
    customer_id: str
    environment_id: str
    assessment_id: str
    use_case: str
    status: AssessmentStatus = AssessmentStatus.COMPLETED
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    overall_score: float
    readiness_level: ReadinessLevel
    projected_score: float
    dimension_scores: list[DimensionScore]
    findings: list[Finding]
    remediation: list[RemediationAction]
    data_profile: DataProfile  # local-only detail; NEVER sent as-is over the wire

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    def to_control_plane_payload(self) -> "ControlPlanePayload":
        return ControlPlanePayload(
            assessment_id=self.assessment_id,
            environment_id=self.environment_id,
            use_case=self.use_case,
            status=self.status,
            score=round(self.overall_score),
            readiness_status=self.readiness_level,
            dimensions={
                d.name: {"score": round(d.score), "weight": d.weight, "summary": d.summary}
                for d in self.dimension_scores
            },
            findings=[
                {
                    "finding_id": f.finding_id,
                    "severity": f.severity,
                    "type": f.type,
                    "title": f.title,
                    "impact": f.impact,
                }
                for f in self.findings
            ],
            remediation=[
                {
                    "action_id": r.action_id,
                    "action": r.action,
                    "effort": r.effort,
                    "projected_score_delta": r.projected_score_delta,
                }
                for r in self.remediation
            ],
            projected_score=round(self.projected_score),
        )


class ControlPlanePayload(BaseModel):
    """Exact shape of the "minimal result payload" Member 3's backend
    expects (Backend & Cloud MVP Spec, section 9 / BACKEND-009..013)."""

    assessment_id: str
    environment_id: str
    use_case: str
    status: AssessmentStatus
    score: int
    readiness_status: ReadinessLevel
    dimensions: dict
    findings: list[dict]
    remediation: list[dict]
    projected_score: int

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)
