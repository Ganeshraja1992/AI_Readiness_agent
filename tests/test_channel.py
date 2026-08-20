import json

from ai_readiness_agent.assessment.models import AssessmentStatus, ControlPlanePayload, ReadinessLevel
from ai_readiness_agent.channel.secure_channel import SecureResultChannel
from ai_readiness_agent.config import SecureChannelConfig


def _payload(assessment_id="assessment-1"):
    return ControlPlanePayload(
        assessment_id=assessment_id,
        environment_id="env-1",
        use_case="customer_support_agent",
        status=AssessmentStatus.COMPLETED,
        score=77,
        readiness_status=ReadinessLevel.READY,
        dimensions={"completeness": {"score": 96, "weight": 0.25, "summary": "ok"}},
        findings=[],
        remediation=[],
        projected_score=92,
    )


def test_outbox_delivery_writes_signed_envelope(tmp_path):
    config = SecureChannelConfig(control_plane_url="", shared_secret="test-secret", outbox_dir=tmp_path)
    channel = SecureResultChannel(config)
    receipt = channel.send(_payload("assessment-outbox"))

    assert receipt.delivered is True
    assert receipt.transport == "outbox"
    written = json.loads((tmp_path / "assessment-outbox.json").read_text())
    assert written["assessment_id"] == "assessment-outbox"
    assert "signature" in written
    assert SecureResultChannel.verify(written, "test-secret") is True
    assert SecureResultChannel.verify(written, "wrong-secret") is False


def test_envelope_carries_agent_id_for_authentication(tmp_path):
    config = SecureChannelConfig(
        control_plane_url="", shared_secret="s", agent_id="agent-xyz", outbox_dir=tmp_path
    )
    channel = SecureResultChannel(config)
    channel.send(_payload("assessment-agentid"))
    written = json.loads((tmp_path / "assessment-agentid.json").read_text())
    assert written["agent_id"] == "agent-xyz"


def test_tampered_payload_fails_verification(tmp_path):
    config = SecureChannelConfig(control_plane_url="", shared_secret="s", outbox_dir=tmp_path)
    channel = SecureResultChannel(config)
    channel.send(_payload("assessment-tamper"))
    written = json.loads((tmp_path / "assessment-tamper.json").read_text())
    written["payload"]["score"] = 999  # tamper after the fact
    assert SecureResultChannel.verify(written, "s") is False
