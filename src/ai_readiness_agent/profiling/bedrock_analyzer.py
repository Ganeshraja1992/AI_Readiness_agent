"""
LLM-powered content analysis for the Readiness Engine -- Amazon Bedrock
fallback engine.

Asks the exact same question as llm_analyzer.py (same prompt, same
response schema — see _llm_shared.py) but through Amazon Bedrock's
Converse API instead of a direct Anthropic API call, so it authenticates
with the same AWS credentials already used for S3/RDS/DynamoDB/Comprehend
and bills through AWS rather than a separate Anthropic account. agent.py
calls this only when the direct Anthropic call didn't work (no key, billing
issue, network error, etc.) — see agent.py's try-Claude-then-Bedrock chain.

Structured JSON output isn't a first-class Converse API feature the way it
is on the Anthropic API directly, so this gets it via tool use instead:
the response schema becomes a "tool" the model is forced to call, and its
arguments are the structured result.

Requires bedrock:InvokeModel (or Converse) IAM permission, and the model
must have "model access" granted in the Bedrock console for this
account/region before first use -- unlike Comprehend, Bedrock model access
isn't on by default even with a correct IAM policy.
"""
from __future__ import annotations

import logging

from ai_readiness_agent.config import BedrockConfig
from ai_readiness_agent.profiling._llm_shared import RESPONSE_SCHEMA, build_prompt, collect_samples
from ai_readiness_agent.profiling.models import DataProfile, LLMContentAnalysis

logger = logging.getLogger(__name__)

_TOOL_NAME = "report_content_analysis"
_REFUSAL_STOP_REASONS = {"content_filtered", "guardrail_intervened"}


def analyze(profile: DataProfile, use_case: str, config: BedrockConfig) -> LLMContentAnalysis:
    """Run the Bedrock content analysis. Never raises -- auth, access, and
    parsing failures degrade to `performed=False` with `error` set."""
    if not config.enabled:
        return LLMContentAnalysis(performed=False)

    samples = collect_samples(profile, config.max_samples)
    if not samples:
        return LLMContentAnalysis(performed=False)

    prompt = build_prompt(use_case, samples)

    try:
        import boto3  # imported lazily so the Anthropic-only path has zero extra cost

        client = boto3.client("bedrock-runtime", region_name=config.region)
        response = client.converse(
            modelId=config.model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            toolConfig={
                "tools": [
                    {
                        "toolSpec": {
                            "name": _TOOL_NAME,
                            "description": "Report the content analysis findings.",
                            "inputSchema": {"json": RESPONSE_SCHEMA},
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": _TOOL_NAME}},
            },
        )
    except Exception as exc:  # noqa: BLE001 - network/API/permission errors shouldn't crash the assessment
        logger.warning("Bedrock content analysis failed: %s", exc)
        return LLMContentAnalysis(performed=False, error=str(exc))

    if response.get("stopReason") in _REFUSAL_STOP_REASONS:
        return LLMContentAnalysis(performed=False, error="model declined to analyze this content")

    content_blocks = response.get("output", {}).get("message", {}).get("content", [])
    tool_use = next((block["toolUse"] for block in content_blocks if "toolUse" in block), None)
    if not tool_use:
        return LLMContentAnalysis(performed=False, error="model did not return structured output")

    data = tool_use.get("input", {})
    return LLMContentAnalysis(
        performed=True,
        engine="bedrock",
        sensitive_data_findings=data.get("sensitive_data_findings", []),
        quality_issues=data.get("quality_issues", []),
        use_case_fit_score=data.get("use_case_fit_score"),
        use_case_fit_notes=data.get("use_case_fit_notes", ""),
    )
