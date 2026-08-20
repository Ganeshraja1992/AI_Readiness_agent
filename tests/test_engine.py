from datetime import datetime, timedelta, timezone

from ai_readiness_agent.engine.readiness_engine import assess
from ai_readiness_agent.ingestion.base import SourceBatch, SourceRecord
from ai_readiness_agent.profiling.profiler import build_data_profile


def _clean_profile(customer_id="cust-1"):
    now = datetime.now(timezone.utc)
    batch = SourceBatch(source_type="rds", source_name="customers")
    for i in range(50):
        batch.records.append(
            SourceRecord(
                fields={"id": str(i), "plan": "pro", "signup_date": "2026-01-01"},
                source_type="rds",
                source_id=f"customers#{i}",
                last_modified=now,
            )
        )
    return build_data_profile(customer_id, [batch])


def _dirty_profile(customer_id="cust-2"):
    stale = datetime.now(timezone.utc) - timedelta(days=400)
    batch = SourceBatch(source_type="s3", source_name="dump.json")
    rows = [
        {"email": "a@example.com", "ssn": None},
        {"email": "a@example.com", "ssn": None},  # duplicate
        {"email": None, "ssn": "123-45-6789"},
    ]
    for i, row in enumerate(rows):
        batch.records.append(
            SourceRecord(fields=row, source_type="s3", source_id=f"dump.json#{i}", last_modified=stale)
        )
    return build_data_profile(customer_id, [batch])


def test_clean_dense_profile_scores_high_on_completeness_and_uniqueness():
    profile = _clean_profile()
    result = assess(profile, use_case="general_ai_readiness", environment_id="env-1")
    dims = {d.name: d for d in result.dimension_scores}
    assert dims["completeness"].score == 100.0
    assert dims["uniqueness"].score == 100.0
    assert result.overall_score > 0


def test_dirty_stale_profile_flags_privacy_and_freshness():
    profile = _dirty_profile()
    result = assess(profile, use_case="general_ai_readiness", environment_id="env-1")
    dims = {d.name: d for d in result.dimension_scores}
    assert dims["privacy_risk"].score < 100.0
    assert dims["freshness"].score < 100.0
    assert dims["uniqueness"].score < 100.0
    finding_types = {f.type for f in result.findings}
    assert "privacy_risk" in finding_types


def test_use_case_changes_weighting():
    profile = _dirty_profile()
    support = assess(profile, use_case="customer_support_agent", environment_id="env-1")
    forecasting = assess(profile, use_case="sales_forecasting_model", environment_id="env-1")
    support_privacy_weight = next(d.weight for d in support.dimension_scores if d.name == "privacy_risk")
    forecasting_privacy_weight = next(d.weight for d in forecasting.dimension_scores if d.name == "privacy_risk")
    assert support_privacy_weight > forecasting_privacy_weight
    # Same underlying data, different use case -> can produce a different overall score.
    assert support.overall_score != forecasting.overall_score


def test_readiness_level_thresholds_are_monotonic():
    profile = _clean_profile()
    result = assess(profile, use_case="general_ai_readiness", environment_id="env-1")
    assert result.readiness_level.value in {"NOT_READY", "NEEDS_WORK", "READY", "AI_READY"}
    assert result.projected_score >= result.overall_score
    assert result.projected_score <= 100.0


def test_assessment_id_and_env_threaded_through():
    profile = _clean_profile()
    result = assess(profile, use_case="x", environment_id="env-42", assessment_id="assessment-001")
    assert result.assessment_id == "assessment-001"
    assert result.environment_id == "env-42"
    assert result.use_case == "x"
