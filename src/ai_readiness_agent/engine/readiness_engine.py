"""
Readiness Engine: consumes a DataProfile (+ the selected AI use case) and
produces the scored Assessment Result.

This is the "Readiness Engine" -> "Assessment Result" part of the
architecture diagram. Per Member 3's Backend & Cloud MVP Spec, scoring,
severity, and remediation business logic live here (and only here) — the
Control Plane invokes this engine but must not reinterpret or duplicate it.
"""
from __future__ import annotations

import uuid

from ai_readiness_agent.assessment.models import (
    AssessmentResult,
    AssessmentStatus,
    DimensionScore,
    Finding,
    ReadinessLevel,
    RemediationAction,
)
from ai_readiness_agent.engine import rules
from ai_readiness_agent.profiling.models import DataProfile

DEFAULT_USE_CASE = "general_ai_readiness"


def _completeness(profile: DataProfile, weight: float, findings: list[Finding]) -> DimensionScore:
    total_null = total_fields = 0
    for source in profile.sources:
        for field in source.fields:
            total_null += field.null_count
            total_fields += field.null_count + field.non_null_count
            if field.null_rate > rules.HIGH_NULL_RATE_THRESHOLD and (field.null_count + field.non_null_count) > 0:
                findings.append(
                    Finding(
                        severity="warning",
                        type="completeness",
                        title=f"Sparse field: {field.name}",
                        impact=f"Field '{field.name}' in {source.source_type}:{source.source_name} is "
                        f"{field.null_rate:.0%} null.",
                        source=f"{source.source_type}:{source.source_name}",
                    )
                )
    null_rate = (total_null / total_fields) if total_fields else 1.0
    score = max(0.0, (1 - null_rate) * 100)
    return DimensionScore(
        name="completeness",
        score=round(score, 1),
        weight=weight,
        summary=f"{null_rate:.1%} of field values are missing across {len(profile.sources)} source(s).",
    )


def _uniqueness(profile: DataProfile, weight: float, findings: list[Finding]) -> DimensionScore:
    total_dupes = total_records = 0
    for source in profile.sources:
        total_dupes += source.duplicate_record_count
        total_records += source.record_count
        if source.duplicate_rate > rules.HIGH_DUPLICATE_RATE_THRESHOLD:
            findings.append(
                Finding(
                    severity="warning",
                    type="uniqueness",
                    title=f"Duplicate records in {source.source_name}",
                    impact=f"{source.source_type}:{source.source_name} has a "
                    f"{source.duplicate_rate:.1%} duplicate record rate.",
                    source=f"{source.source_type}:{source.source_name}",
                )
            )
    dup_rate = (total_dupes / total_records) if total_records else 0.0
    score = max(0.0, (1 - dup_rate) * 100)
    return DimensionScore(
        name="uniqueness",
        score=round(score, 1),
        weight=weight,
        summary=f"{dup_rate:.1%} of records look like duplicates.",
    )


def _volume(profile: DataProfile, weight: float, findings: list[Finding]) -> DimensionScore:
    total_records = profile.total_records
    score = rules.bucket_score(total_records, rules.VOLUME_BUCKETS)
    if total_records == 0:
        findings.append(
            Finding(
                severity="critical",
                type="volume",
                title="No data ingested",
                impact="No records could be ingested from any source.",
            )
        )
    elif score < 65:
        findings.append(
            Finding(
                severity="info",
                type="volume",
                title="Low sample volume",
                impact=f"Only {total_records} records sampled across all sources; more data will improve "
                "model reliability.",
            )
        )
    return DimensionScore(
        name="volume",
        score=round(score, 1),
        weight=weight,
        summary=f"{total_records} total records sampled across {len(profile.sources)} source(s).",
    )


def _freshness(profile: DataProfile, weight: float, findings: list[Finding]) -> DimensionScore:
    weighted_days = []
    for source in profile.sources:
        days = source.freshness_days
        if days is not None and source.record_count:
            weighted_days.append((days, source.record_count))
            if days > rules.FRESHNESS_BUCKETS_DAYS[2][0]:  # > 90 days
                findings.append(
                    Finding(
                        severity="warning",
                        type="freshness",
                        title=f"Stale data in {source.source_name}",
                        impact=f"{source.source_type}:{source.source_name} newest record is {days:.0f} days old.",
                        source=f"{source.source_type}:{source.source_name}",
                    )
                )
    if not weighted_days:
        return DimensionScore(
            name="freshness",
            score=50.0,
            weight=weight,
            summary="No timestamps available to assess data freshness.",
        )
    total_weight = sum(w for _, w in weighted_days)
    avg_days = sum(d * w for d, w in weighted_days) / total_weight if total_weight else weighted_days[0][0]
    score = rules.bucket_score(avg_days, rules.FRESHNESS_BUCKETS_DAYS)
    return DimensionScore(
        name="freshness",
        score=round(score, 1),
        weight=weight,
        summary=f"Average newest-record age is {avg_days:.0f} day(s).",
    )


_CRITICAL_PII_KINDS = {
    "ssn",
    "credit_card",
    "SSN",
    "CREDIT_DEBIT_NUMBER",
    "BANK_ACCOUNT_NUMBER",
    "BANK_ROUTING",
    "PASSPORT_NUMBER",
    "PIN",
}


def _privacy_risk(profile: DataProfile, weight: float, findings: list[Finding]) -> DimensionScore:
    penalty = 0.0
    all_kinds: dict[str, int] = {}
    for source in profile.sources:
        for kind, count in source.pii_findings.items():
            all_kinds[kind] = all_kinds.get(kind, 0) + count
            multiplier = rules.PII_SEVERITY_MULTIPLIER.get(kind, 1.0)
            penalty += min(30.0, 5 + count) * multiplier
            # Comprehend's entity types are uppercase (see
            # profiling/comprehend_pii.py); the regex heuristic's are
            # lowercase — that's how we distinguish which scanner found it.
            scanner = "AWS Comprehend" if kind.isupper() else "heuristic scan"
            findings.append(
                Finding(
                    severity="critical" if kind in _CRITICAL_PII_KINDS else "warning",
                    type="privacy_risk",
                    title=f"Possible {kind} detected",
                    impact=f"Detected {count} possible {kind} value(s) in {source.source_type}:"
                    f"{source.source_name} ({scanner}). Ensure this is masked/tokenized before "
                    "training or handing off downstream.",
                    source=f"{source.source_type}:{source.source_name}",
                )
            )
    score = max(0.0, 100.0 - penalty)
    summary = (
        "No obvious PII detected."
        if not all_kinds
        else f"Possible PII detected: {', '.join(f'{k}={v}' for k, v in all_kinds.items())}."
    )
    return DimensionScore(
        name="privacy_risk",
        score=round(score, 1),
        weight=weight,
        summary=summary,
    )


_SECURITY_CHECK_LABELS = {
    "public_access_blocked": "S3 Block Public Access is not fully enabled",
    "encryption_enabled": "Default (server-side) encryption is not enabled",
    "versioning_enabled": "Bucket versioning is not enabled",
}
_SECURITY_CHECK_PENALTY = {
    "public_access_blocked": 50.0,  # public exposure is the most severe misconfiguration
    "encryption_enabled": 30.0,
    "versioning_enabled": 20.0,
}


def _security(profile: DataProfile, weight: float, findings: list[Finding]) -> DimensionScore:
    sources_with_checks = [s for s in profile.sources if s.security_checks]
    if not sources_with_checks:
        return DimensionScore(
            name="security",
            score=50.0,
            weight=weight,
            summary="No bucket security checks were performed (non-S3 source, or S3 running in mock mode).",
        )

    penalty = 0.0
    failed_checks: set[str] = set()
    for source in sources_with_checks:
        for check, passed in source.security_checks.items():
            if passed:
                continue
            failed_checks.add(check)
            penalty += _SECURITY_CHECK_PENALTY.get(check, 15.0)
            findings.append(
                Finding(
                    severity="critical" if check == "public_access_blocked" else "warning",
                    type="security",
                    title=_SECURITY_CHECK_LABELS.get(check, f"Failed check: {check}"),
                    impact=f"{source.source_type}:{source.source_name} does not have '{check}' enabled.",
                    source=f"{source.source_type}:{source.source_name}",
                )
            )
    score = max(0.0, 100.0 - penalty)
    summary = (
        "All bucket security checks passed (public access blocked, encryption enabled, versioning enabled)."
        if not failed_checks
        else f"Failed security check(s): {', '.join(sorted(failed_checks))}."
    )
    return DimensionScore(
        name="security",
        score=round(score, 1),
        weight=weight,
        summary=summary,
    )


_LLM_SEVERITY_PENALTY = {"critical": 25.0, "warning": 12.0, "info": 4.0}


def _ai_content_analysis(profile: DataProfile, weight: float, findings: list[Finding]) -> DimensionScore:
    analysis = profile.llm_analysis
    if not analysis or not analysis.performed:
        reason = f" ({analysis.error})" if analysis and analysis.error else ""
        return DimensionScore(
            name="ai_content_analysis",
            score=50.0,
            weight=weight,
            summary=(
                f"LLM content analysis was not performed{reason}. "
                "Set ANTHROPIC_API_KEY, or enable the Amazon Bedrock fallback, to use it."
            ),
        )

    penalty = 0.0
    for f in analysis.sensitive_data_findings:
        penalty += _LLM_SEVERITY_PENALTY.get(f.severity, 8.0)
        findings.append(
            Finding(
                severity=f.severity,
                type="ai_content_analysis",
                title=f"LLM flagged possible {f.kind}",
                impact=f'{f.explanation} (excerpt: "{f.excerpt}")',
            )
        )
    for q in analysis.quality_issues:
        penalty += 5.0
        findings.append(
            Finding(
                severity="info",
                type="ai_content_analysis",
                title=q.issue,
                impact=q.explanation,
            )
        )

    score = max(0.0, 100.0 - penalty)
    critical_count = sum(1 for f in analysis.sensitive_data_findings if f.severity == "critical")
    engine_label = {"anthropic": "Anthropic Claude", "bedrock": "Amazon Bedrock", "openrouter": "OpenRouter"}.get(
        analysis.engine, "LLM"
    )
    # State this dimension's own score explicitly, and clearly separate it
    # from use_case_fit_score below -- otherwise readers see two different
    # 0-100 numbers in the same sentence and reasonably assume the bar
    # isn't reflecting whichever one is quoted in the text.
    summary = (
        f"{engine_label} flagged {len(analysis.sensitive_data_findings)} sensitive-content finding(s) "
        f"({critical_count} critical) and {len(analysis.quality_issues)} quality issue(s) in sampled "
        f"content, scoring this dimension {score:.0f}/100."
    )
    if analysis.use_case_fit_score is not None:
        summary += (
            f" Separately (not part of this dimension's score), the LLM also judged how well this "
            f"sampled data fits the stated use case: {analysis.use_case_fit_score:.0f}/100 — "
            f"{analysis.use_case_fit_notes}"
        )
    return DimensionScore(
        name="ai_content_analysis",
        score=round(score, 1),
        weight=weight,
        summary=summary,
    )


def _ingestion_error_findings(profile: DataProfile) -> list[Finding]:
    out = []
    for source in profile.sources:
        for err in source.ingestion_errors:
            out.append(
                Finding(
                    severity="warning",
                    type="ingestion",
                    title=f"Ingestion error in {source.source_name}",
                    impact=err,
                    source=f"{source.source_type}:{source.source_name}",
                )
            )
    return out


# Rough effort heuristic per dimension, used only for the demo/MVP.
_EFFORT_BY_DIMENSION = {
    "completeness": "medium",
    "uniqueness": "medium",
    "volume": "high",
    "freshness": "low",
    "privacy_risk": "medium",
    "security": "low",
    "ai_content_analysis": "medium",
}

_ACTION_BY_DIMENSION = {
    "completeness": "Backfill or drop sparsely-populated fields before using this data for model training.",
    "uniqueness": "Deduplicate records (e.g. by natural key or fuzzy match) to avoid biasing model training.",
    "volume": "Increase the sample/history window so the Readiness Engine can assess a larger dataset.",
    "freshness": "Set up more frequent syncs so ingested data reflects recent activity.",
    "privacy_risk": "Mask, tokenize, or exclude detected PII fields before this data leaves the customer boundary.",
    "security": "Enable S3 Block Public Access, default (server-side) encryption, and versioning on this bucket.",
    "ai_content_analysis": "Review the LLM-flagged sensitive content and quality issues, then mask, clean, or exclude the affected records.",
}


def _remediation(dimension_scores: list[DimensionScore]) -> list[RemediationAction]:
    actions = []
    for d in dimension_scores:
        if d.score >= 85:
            continue
        gap = 100 - d.score
        projected_delta = round(gap * d.weight * 0.7, 1)  # assume remediation closes ~70% of the gap
        actions.append(
            RemediationAction(
                action=_ACTION_BY_DIMENSION.get(d.name, f"Improve {d.name}."),
                effort=_EFFORT_BY_DIMENSION.get(d.name, "medium"),
                projected_score_delta=projected_delta,
                dimension=d.name,
            )
        )
    return actions


def assess(
    profile: DataProfile,
    *,
    use_case: str = DEFAULT_USE_CASE,
    environment_id: str | None = None,
    assessment_id: str | None = None,
    status: AssessmentStatus = AssessmentStatus.COMPLETED,
) -> AssessmentResult:
    weights = rules.weights_for_use_case(use_case)
    findings: list[Finding] = []
    dimension_scores = [
        _completeness(profile, weights["completeness"], findings),
        _uniqueness(profile, weights["uniqueness"], findings),
        _volume(profile, weights["volume"], findings),
        _freshness(profile, weights["freshness"], findings),
        _privacy_risk(profile, weights["privacy_risk"], findings),
        _security(profile, weights["security"], findings),
        _ai_content_analysis(profile, weights["ai_content_analysis"], findings),
    ]
    findings.extend(_ingestion_error_findings(profile))

    overall = round(sum(d.score * d.weight for d in dimension_scores), 1)
    remediation = _remediation(dimension_scores)
    projected = min(100.0, round(overall + sum(r.projected_score_delta for r in remediation), 1))

    return AssessmentResult(
        customer_id=profile.customer_id,
        environment_id=environment_id or profile.customer_id,
        assessment_id=assessment_id or str(uuid.uuid4()),
        use_case=use_case,
        status=status,
        overall_score=overall,
        readiness_level=ReadinessLevel(rules.readiness_level_for(overall)),
        projected_score=projected,
        dimension_scores=dimension_scores,
        findings=findings,
        remediation=remediation,
        data_profile=profile,
    )
