"""
Compliance test: the payload that crosses the Secure Result Channel must
never contain raw record values — only aggregate stats, field/column
names, and human-readable summaries. This directly encodes the MVP spec's
"Never require raw S3 objects, database rows, or documents to be uploaded
to the control plane" / "No raw enterprise data in backend persistence"
requirements.
"""
from datetime import datetime, timezone

from ai_readiness_agent.engine.readiness_engine import assess
from ai_readiness_agent.ingestion.base import SourceBatch, SourceRecord
from ai_readiness_agent.profiling.profiler import build_data_profile

SECRET_SSN = "999-88-7777"
SECRET_EMAIL = "very-specific-person@example.com"
SECRET_NAME = "Zzyxx Uncommon Surname"


def _profile_with_secrets():
    now = datetime.now(timezone.utc)
    batch = SourceBatch(source_type="rds", source_name="customers")
    batch.records.append(
        SourceRecord(
            fields={"full_name": SECRET_NAME, "email": SECRET_EMAIL, "ssn": SECRET_SSN},
            source_type="rds",
            source_id="customers#1",
            last_modified=now,
        )
    )
    return build_data_profile("cust-1", [batch])


def test_full_result_contains_raw_values_but_payload_does_not():
    profile = _profile_with_secrets()
    result = assess(profile, use_case="customer_support_agent", environment_id="env-1")

    full_json = result.to_json()
    # Sanity check: the full local-audit result DOES retain raw sample
    # values (that's expected — it's the agent's own local audit trail and
    # never leaves the customer's environment).
    assert SECRET_SSN in full_json or SECRET_EMAIL in full_json or SECRET_NAME in full_json

    payload_json = result.to_control_plane_payload().to_json()
    assert SECRET_SSN not in payload_json
    assert SECRET_EMAIL not in payload_json
    assert SECRET_NAME not in payload_json
    # And the payload must not carry a data_profile field at all.
    assert "data_profile" not in payload_json
    assert "sample_values" not in payload_json
