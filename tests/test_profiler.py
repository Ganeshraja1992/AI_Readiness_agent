from datetime import datetime, timezone

from ai_readiness_agent.ingestion.base import SourceBatch, SourceRecord
from ai_readiness_agent.profiling.profiler import build_data_profile, profile_source


def _batch(records: list[dict], source_name="test-source") -> SourceBatch:
    b = SourceBatch(source_type="s3", source_name=source_name)
    now = datetime.now(timezone.utc)
    for r in records:
        b.records.append(SourceRecord(fields=r, source_type="s3", source_id=source_name, last_modified=now))
    return b


def test_profile_source_counts_nulls_and_types():
    batch = _batch(
        [
            {"a": "1", "b": "x", "c": None},
            {"a": "2", "b": "", "c": "z"},
            {"a": "3", "b": "y", "c": "w"},
        ]
    )
    profile = profile_source(batch)
    assert profile.record_count == 3
    fields = {f.name: f for f in profile.fields}
    assert fields["a"].inferred_type == "numeric"
    assert fields["a"].null_rate == 0.0
    assert fields["c"].null_count == 1


def test_profile_source_detects_duplicates():
    batch = _batch([{"a": "1"}, {"a": "1"}, {"a": "2"}])
    profile = profile_source(batch)
    assert profile.duplicate_record_count == 1
    assert abs(profile.duplicate_rate - (1 / 3)) < 1e-9


def test_profile_source_detects_pii():
    batch = _batch([{"note": "contact me at test@example.com"}, {"note": "no pii here"}])
    profile = profile_source(batch)
    assert profile.pii_findings.get("email") == 1


def test_empty_batch_has_no_data():
    batch = SourceBatch(source_type="rds", source_name="empty")
    profile = profile_source(batch)
    assert profile.has_data is False
    assert profile.record_count == 0


def test_build_data_profile_aggregates_sources():
    batches = [_batch([{"a": "1"}], "one"), _batch([{"a": "1"}, {"a": "2"}], "two")]
    dp = build_data_profile("cust-1", batches)
    assert dp.total_records == 3
    assert len(dp.sources) == 2
