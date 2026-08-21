"""
Shared prompt + response schema for the two content-analysis engines
(llm_analyzer.py's direct Anthropic API call, bedrock_analyzer.py's Amazon
Bedrock fallback) -- kept in one place so a fallback from one to the other
is a genuine apples-to-apples retry, not a different judgment call.
"""
from __future__ import annotations

from ai_readiness_agent.profiling.models import DataProfile

MAX_SAMPLE_CHARS = 400

RESPONSE_SCHEMA = {
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


def collect_samples(profile: DataProfile, max_samples: int) -> list[str]:
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


def build_prompt(use_case: str, samples: list[str]) -> str:
    return (
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
