"""
LLM-powered content analysis for the Readiness Engine -- OpenRouter engine.

Asks the same question as llm_analyzer.py / bedrock_analyzer.py (same
sample data, same required output fields -- see _llm_shared.py) but through
OpenRouter's OpenAI-compatible chat completions API. This is for setups
that only have an OpenRouter key (e.g. a hackathon team issued a shared
OpenRouter key instead of its own Anthropic account or AWS Bedrock model
access) rather than a different judgment call from the other two engines.

Uses "JSON mode" (`response_format: json_object`) rather than a strict
json_schema/tool-use response, since OpenRouter can route a single key to
many different underlying models and not all of them support strict
structured-output enforcement -- the schema is instead spelled out in the
prompt text, and the response is parsed leniently.

agent.py tries this after the direct Anthropic call and before Bedrock --
see agent.py's try-Claude-then-OpenRouter-then-Bedrock chain.
"""
from __future__ import annotations

import json
import logging

from ai_readiness_agent.config import OpenRouterConfig
from ai_readiness_agent.profiling._llm_shared import RESPONSE_SCHEMA, build_prompt, collect_samples
from ai_readiness_agent.profiling.models import DataProfile, LLMContentAnalysis

logger = logging.getLogger(__name__)


def _schema_instructions() -> str:
    return (
        "\n\nRespond with ONLY a single JSON object (no markdown, no commentary) "
        "matching exactly this JSON Schema:\n" + json.dumps(RESPONSE_SCHEMA)
    )


def analyze(profile: DataProfile, use_case: str, config: OpenRouterConfig) -> LLMContentAnalysis:
    """Run the OpenRouter content analysis. Never raises -- network, auth,
    and parsing failures degrade to `performed=False` with `error` set."""
    if not config.active:
        return LLMContentAnalysis(performed=False)

    samples = collect_samples(profile, config.max_samples)
    if not samples:
        return LLMContentAnalysis(performed=False)

    try:
        import requests  # lazy import, consistent with the other engines
    except ImportError:
        return LLMContentAnalysis(performed=False, error="requests package not installed")

    prompt = build_prompt(use_case, samples) + _schema_instructions()

    try:
        response = requests.post(
            config.base_url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            json={
                "model": config.model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        response.raise_for_status()
        data_out = response.json()
    except Exception as exc:  # noqa: BLE001 - network/API errors shouldn't crash the assessment
        logger.warning("OpenRouter content analysis failed: %s", exc)
        return LLMContentAnalysis(performed=False, error=str(exc))

    choices = data_out.get("choices") or []
    if not choices:
        error = data_out.get("error", {}).get("message") or "model returned no choices"
        return LLMContentAnalysis(performed=False, error=error)

    choice = choices[0]
    if choice.get("finish_reason") == "content_filter":
        return LLMContentAnalysis(performed=False, error="model declined to analyze this content")

    text = (choice.get("message") or {}).get("content", "")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        return LLMContentAnalysis(performed=False, error=f"could not parse model response: {exc}")

    return LLMContentAnalysis(
        performed=True,
        engine="openrouter",
        sensitive_data_findings=data.get("sensitive_data_findings", []),
        quality_issues=data.get("quality_issues", []),
        use_case_fit_score=data.get("use_case_fit_score"),
        use_case_fit_notes=data.get("use_case_fit_notes", ""),
    )
