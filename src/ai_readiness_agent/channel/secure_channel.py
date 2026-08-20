"""
Secure Result Channel: the handoff between the AI Readiness Agent and
Member 3's Control Plane.

Responsibilities:
  1. Take ONLY the minimal ControlPlanePayload (never the full
     AssessmentResult, which embeds the Data Profile) and serialize it to
     canonical JSON — enforcing, in code, the spec's "no raw enterprise
     data in backend persistence" / "never require raw S3 objects,
     database rows, or documents to be uploaded to the control plane"
     requirements.
  2. Sign it (HMAC-SHA256 with a shared secret) and tag it with this
     agent's ID so the Control Plane can verify integrity, authenticity,
     and reject arbitrary clients (stand-in for mTLS/SigV4 in a real AWS
     deployment — swap `_sign` for SigV4 signing if the Control Plane
     sits behind an AWS-native auth boundary, or for mTLS at the transport
     layer if it's a private VPC link).
  3. Transmit it — via HTTPS POST when CONTROL_PLANE_URL is configured, or
     by writing a signed envelope to a local outbox directory otherwise,
     so Member 3 can point their Control Plane's ingestion at that
     directory during integration testing.
  4. Retry transient failures with backoff.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ai_readiness_agent.assessment.models import ControlPlanePayload
from ai_readiness_agent.config import SecureChannelConfig

logger = logging.getLogger(__name__)


@dataclass
class DeliveryReceipt:
    delivered: bool
    transport: str  # "https" | "outbox"
    location: str  # URL or file path
    attempts: int
    error: str | None = None


class SecureResultChannel:
    def __init__(self, config: SecureChannelConfig):
        self.config = config

    # ------------------------------------------------------------------
    def _sign(self, payload: bytes) -> str:
        return hmac.new(self.config.shared_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    def _envelope(self, result: ControlPlanePayload) -> dict:
        payload = result.model_dump(mode="json")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = self._sign(raw)
        return {
            "payload": payload,
            "signature": signature,
            "signature_algorithm": "HMAC-SHA256",
            "agent_id": self.config.agent_id,
            "assessment_id": result.assessment_id,
        }

    # ------------------------------------------------------------------
    def send(self, result: ControlPlanePayload) -> DeliveryReceipt:
        """Send the minimal Control Plane payload. Callers must pass
        `AssessmentResult.to_control_plane_payload()`, never the full
        AssessmentResult — that keeps raw enterprise data out of this
        channel by construction rather than by convention."""
        envelope = self._envelope(result)
        if self.config.control_plane_url:
            return self._send_https(envelope)
        return self._send_outbox(envelope, result.assessment_id)

    # ------------------------------------------------------------------
    def _send_https(self, envelope: dict) -> DeliveryReceipt:
        import requests  # lazy import so outbox-only usage needs no network deps

        attempts = 0
        last_error = None
        while attempts < self.config.max_retries:
            attempts += 1
            try:
                resp = requests.post(
                    self.config.control_plane_url,
                    json=envelope,
                    timeout=self.config.timeout_seconds,
                    headers={
                        "Content-Type": "application/json",
                        "X-Signature": envelope["signature"],
                        "X-Agent-Id": self.config.agent_id,
                    },
                )
                if resp.status_code < 300:
                    return DeliveryReceipt(
                        delivered=True,
                        transport="https",
                        location=self.config.control_plane_url,
                        attempts=attempts,
                    )
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            if attempts < self.config.max_retries:
                time.sleep(min(2 ** attempts, 10))
        logger.warning("Failed to deliver assessment to Control Plane after %s attempts: %s", attempts, last_error)
        return DeliveryReceipt(
            delivered=False,
            transport="https",
            location=self.config.control_plane_url,
            attempts=attempts,
            error=last_error,
        )

    def _send_outbox(self, envelope: dict, assessment_id: str) -> DeliveryReceipt:
        outbox: Path = self.config.outbox_dir
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / f"{assessment_id}.json"
        try:
            path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
            return DeliveryReceipt(delivered=True, transport="outbox", location=str(path), attempts=1)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(delivered=False, transport="outbox", location=str(path), attempts=1, error=str(exc))

    # ------------------------------------------------------------------
    @staticmethod
    def verify(envelope: dict, shared_secret: str) -> bool:
        """Helper Member 3's Control Plane can use (or port to their own
        language) to verify a received envelope before trusting it."""
        raw = json.dumps(envelope["payload"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(shared_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, envelope.get("signature", ""))
