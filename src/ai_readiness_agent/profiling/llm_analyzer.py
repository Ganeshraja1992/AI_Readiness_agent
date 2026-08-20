"""
LLM-powered content analysis for the Readiness Engine.

Augments the regex-based PII scanner (profiling/pii.py) with an actual
Claude call over a sample of the ingested text. A regex can catch
"looks like an SSN"; it can't judge whether a paragraph describes a
patient's diagnosis, whether a CSV column is placeholder/test data mixed
into real records, or how well a data source actually fits the AI use case
selected for this assessment. Those are judgment calls, so this module
hands a sample to Claude and asks for exactly that judgment, back as
structured JSON.

Off by default unless `LLMConfig.active` (an API key is configured) — the
rest of the pipeline runs standalone with no Anthropic account at all, and
this is purely additive on top of it.
"""
from __future__ import annotations

import json
import logging

from ai_readiness_agent.config import LLMConfig
from ai_readiness_agent.profiling.models import DataProfile, LLMContentAnalysis

logger = logging.getLogger(__name__)

MAX_SAMPLE_CHARS = 400

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "sensitive_data_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "e.g. ssn, credit_card, health_record, credentials, internal_only, other",
                    },
                    "excerpt": {
                        "type": "string",
                        "description": "Short excerpt or paraphrase (no more than 15 words) illustrating the finding",
                    },
                    "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
                    "explanation": {"type": "string"},
                },
                "required": ["kind", "excerpt", "severity", "explanation"],
                "additionalProperties": False,
            },
        },
        "quality_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["issue", "explanation"],
                "additionalProperties": False,
            },
        },
        "use_case_fit_score": {
            "type": "integer",
            "description": "0-100: how well this sampled data fits the stated AI use case",
        },
        "use_case_fit_notes": {"type": "string"},
    },
    "required": ["sensitive_data_findings", "quality_issues", "use_case_fit_score", "use_case_fit_notes"],
    "additionalProperties": False,
}


def _collect_samples(profile: DataProfile, max_samples: int) -> list[str]:
    samples: list[str] = []
    for source in profile.sources:
        for field in source.fields:
            for value in field.sample_values:
                if not value:
                    continue
                samples.append(value[:MAX_SAMPLE_CHARS])
                if len(samples) >= max_samples:
                    return samples
    return samples


def analyze(profile: DataProfile, use_case: str, config: LLMConfig) -> LLMContentAnalysis:
    """Run the optional Claude content analysis. Never raises — network,
    auth, and parsing failures degrade to `performed=False` with `error`
    set, so a missing/invalid API key never breaks the assessment."""
    if not config.active:
        return LLMContentAnalysis(performed=False)

    samples = _collect_samples(profile, config.max_samples)
    if not samples:
        return LLMContentAnalysis(performed=False)

    try:
        import anthropic
    except ImportError:
        return LLMContentAnalysis(performed=False, error="anthropic package not installed")

    prompt = (
        f"You are assessing a customer's data estate for AI readiness. The selected AI use case is: "
        f"{use_case!r}.\n\n"
        "Below is a sample of field values pulled from the customer's connected data sources "
        "(S3 objects, database rows, uploaded documents). Identify:\n"
        "1. Sensitive data (PII, credentials, health/financial records, anything that shouldn't "
        "leave the customer's environment unmasked) that a simple regex scan on structured fields "
        "might miss -- e.g. sensitive content embedded in free text.\n"
        "2. Data quality issues (inconsistent formats, garbled/truncated values, placeholder or "
        "test data mixed in with real records).\n"
        "3. How well this sampled data fits the stated use case, 0-100, with a short rationale.\n\n"
        "Sample values (one per line, some may be truncated):\n"
        + "\n".join(f"- {s}" for s in samples)
    )

    try:
        client = anthropic.Anthropic(api_key=config.api_key)
        response = client.messages.create(
            model=config.model,
            max_tokens=2048,
            output_config={"format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - network/API errors shouldn't crash the assessment
        logger.warning("LLM content analysis failed: %s", exc)
        return LLMContentAnalysis(performed=False, error=str(exc))

    if response.stop_reason == "refusal":
        return LLMContentAnalysis(performed=False, error="model declined to analyze this content")

    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        return LLMContentAnalysis(performed=False, error=f"could not parse model response: {exc}")

    return LLMContentAnalysis(
        performed=True,
        sensitive_data_findings=data.get("sensitive_data_findings", []),
        quality_issues=data.get("quality_issues", []),
        use_case_fit_score=data.get("use_case_fit_score"),
        use_case_fit_notes=data.get("use_case_fit_notes", ""),
    )
