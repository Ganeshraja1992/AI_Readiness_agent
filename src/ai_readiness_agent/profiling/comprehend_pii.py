"""
Real AWS PII detection via Amazon Comprehend's DetectPiiEntities API.

Runs alongside the fast, zero-cost regex heuristic in pii.py (see
profiler.py) rather than replacing it — Comprehend adds NLP-based detection
(names, addresses, financial/government IDs, etc.) that regex can't catch,
at the cost of a live AWS API call. It's opt-in via
`ComprehendConfig.enabled` (off by default, on for the webapp) and fails
soft on any error so a PII-scan hiccup never breaks ingestion.
"""
from __future__ import annotations

import logging

from ai_readiness_agent.config import ComprehendConfig

logger = logging.getLogger(__name__)


def scan_text(text: str, config: ComprehendConfig) -> dict[str, int]:
    """Return counts of each PII entity TYPE Comprehend detects in `text`
    (e.g. "SSN", "EMAIL", "NAME", "ADDRESS", "CREDIT_DEBIT_NUMBER" — see
    https://docs.aws.amazon.com/comprehend/latest/dg/how-pii.html for the
    full taxonomy). Truncates to `config.max_chars_per_call` since this is a
    sampled-content scan, the same tradeoff the LLM content analyzer makes."""
    if not config.enabled or not text:
        return {}
    try:
        import boto3

        client = boto3.client("comprehend", region_name=config.region)
        sample = text[: config.max_chars_per_call]
        resp = client.detect_pii_entities(Text=sample, LanguageCode=config.language_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Comprehend PII scan failed: %s", exc)
        return {}

    counts: dict[str, int] = {}
    for entity in resp.get("Entities", []):
        kind = entity.get("Type", "UNKNOWN")
        counts[kind] = counts.get(kind, 0) + 1
    return counts
