"""
LLM-powered content analysis for the Readiness Engine -- direct Anthropic
API engine.

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
this is purely additive on top of it. If this fails for any reason
(including a billing/credit issue), agent.py falls back to
bedrock_analyzer.py, which asks the same question of the same model
family via Amazon Bedrock instead.
"""
from __future__ import annotations

import json
import logging

from ai_readiness_agent.config import LLMConfig
from ai_readiness_agent.profiling._llm_shared import RESPONSE_SCHEMA, build_prompt, collect_samples
from ai_readiness_agent.profiling.models import DataProfile, LLMContentAnalysis

logger = logging.getLogger(__name__)


def analyze(profile: DataProfile, use_case: str, config: LLMConfig) -> LLMContentAnalysis:
    """Run the optional Claude content analysis. Never raises — network,
    auth, and parsing failures degrade to `performed=False` with `error`
    set, so a missing/invalid API key never breaks the assessment."""
    if not config.active:
        return LLMContentAnalysis(performed=False)

    samples = collect_samples(profile, config.max_samples)
    if not samples:
        return LLMContentAnalysis(performed=False)

    try:
        import anthropic
    except ImportError:
        return LLMContentAnalysis(performed=False, error="anthropic package not installed")

    prompt = build_prompt(use_case, samples)

    try:
        client = anthropic.Anthropic(api_key=config.api_key)
        response = client.messages.create(
            model=config.model,
            max_tokens=2048,
            output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
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
        engine="anthropic",
        sensitive_data_findings=data.get("sensitive_data_findings", []),
        quality_issues=data.get("quality_issues", []),
        use_case_fit_score=data.get("use_case_fit_score"),
        use_case_fit_notes=data.get("use_case_fit_notes", ""),
    )
