"""
LLM-powered content analysis for the Readiness Engine -- Amazon Bedrock
fallback engine.

Asks the exact same question as llm_analyzer.py (same prompt, same
response schema — see _llm_shared.py) but through Amazon Bedrock's
InvokeModel API instead of a direct Anthropic API call, so it
authenticates with the same AWS credentials already used for
S3/RDS/DynamoDB/Comprehend and bills through AWS rather than a separate
Anthropic account. agent.py calls this only when the direct Anthropic
call didn't work (no key, billing issue, network error, etc.) — see
agent.py's try-Claude-then-Bedrock chain.

Uses InvokeModel (not Bedrock's newer, model-agnostic Converse API)
specifically because Anthropic models on Bedrock accept the native
Anthropic Messages API request/response shape directly under
InvokeModel -- so this reuses the same tool-use-for-structured-output
approach as llm_analyzer.py almost verbatim, and only needs the
long-established bedrock:InvokeModel IAM action (some IAM policy editors'
visual/dropdown pickers don't yet recognize the newer bedrock:Converse
action name, even though it's valid).

Requires bedrock:InvokeModel IAM permission, and the model must have
"model access" granted in the Bedrock console for this account/region
before first use -- unlike Comprehend, Bedrock model access isn't on by
default even with a correct IAM policy.
"""
from __future__ import annotations

import json
import logging

from ai_readiness_agent.config import BedrockConfig
from ai_readiness_agent.profiling._llm_shared import RESPONSE_SCHEMA, build_prompt, collect_samples
from ai_readiness_agent.profiling.models import DataProfile, LLMContentAnalysis

logger = logging.getLogger(__name__)

_TOOL_NAME = "report_content_analysis"
_ANTHROPIC_BEDROCK_VERSION = "bedrock-2023-05-31"
_REFUSAL_STOP_REASONS = {"refusal"}


def analyze(profile: DataProfile, use_case: str, config: BedrockConfig) -> LLMContentAnalysis:
    """Run the Bedrock content analysis. Never raises -- auth, access, and
    parsing failures degrade to `performed=False` with `error` set."""
    if not config.enabled:
        return LLMContentAnalysis(performed=False)

    samples = collect_samples(profile, config.max_samples)
    if not samples:
        return LLMContentAnalysis(performed=False)

    prompt = build_prompt(use_case, samples)

    request_body = {
        "anthropic_version": _ANTHROPIC_BEDROCK_VERSION,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [
            {
                "name": _TOOL_NAME,
                "description": "Report the content analysis findings.",
                "input_schema": RESPONSE_SCHEMA,
            }
        ],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
    }

    try:
        import boto3  # imported lazily so the Anthropic-only path has zero extra cost

        client = boto3.client("bedrock-runtime", region_name=config.region)
        response = client.invoke_model(
            modelId=config.model_id,
            body=json.dumps(request_body),
        )
        data_out = json.loads(response["body"].read())
    except Exception as exc:  # noqa: BLE001 - network/API/permission errors shouldn't crash the assessment
        logger.warning("Bedrock content analysis failed: %s", exc)
        return LLMContentAnalysis(performed=False, error=str(exc))

    if data_out.get("stop_reason") in _REFUSAL_STOP_REASONS:
        return LLMContentAnalysis(performed=False, error="model declined to analyze this content")

    tool_use = next(
        (block for block in data_out.get("content", []) if block.get("type") == "tool_use"), None
    )
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
