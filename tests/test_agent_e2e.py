from ai_readiness_agent.agent import AIReadinessAgent
from ai_readiness_agent.config import AgentConfig, DocumentsConfig, RDSConfig, S3Config, SecureChannelConfig


def _config(tmp_path):
    return AgentConfig(
        customer_id="demo-customer",
        environment_id="demo-customer-aws",
        use_case="customer_support_agent",
        s3=S3Config(use_mock=True),
        rds=RDSConfig(use_mock=True),
        documents=DocumentsConfig(),
        channel=SecureChannelConfig(control_plane_url="", outbox_dir=tmp_path / "outbox"),
        local_audit_dir=tmp_path / "local_audit",
    )


def test_agent_runs_end_to_end_against_mock_data(tmp_path):
    agent = AIReadinessAgent(_config(tmp_path))
    result, receipt = agent.run(deliver=True)

    assert result.data_profile.total_records > 0
    assert 0 <= result.overall_score <= 100
    assert receipt is not None
    assert receipt.delivered is True
    assert receipt.transport == "outbox"

    # Local audit trail (full result, with Data Profile) was written locally.
    audit_files = list((tmp_path / "local_audit").glob("*.json"))
    assert len(audit_files) == 1

    # Only the minimal payload landed in the outbox.
    outbox_files = list((tmp_path / "outbox").glob("*.json"))
    assert len(outbox_files) == 1
    outbox_content = outbox_files[0].read_text()
    assert "data_profile" not in outbox_content
    assert "sample_values" not in outbox_content


def test_agent_run_accepts_control_plane_supplied_ids(tmp_path):
    agent = AIReadinessAgent(_config(tmp_path))
    result, _ = agent.run(
        deliver=False,
        use_case="fraud_detection_model",
        environment_id="env-from-backend",
        assessment_id="assessment-from-backend",
    )
    assert result.use_case == "fraud_detection_model"
    assert result.environment_id == "env-from-backend"
    assert result.assessment_id == "assessment-from-backend"
