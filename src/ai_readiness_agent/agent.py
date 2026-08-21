"""
AI Readiness Agent — the orchestrator box at the top of the architecture
diagram. Wires together:

    Customer AWS (S3 / RDS / Documents)
        -> ingestion adapters
        -> Data Profile (profiler)
        -> Readiness Engine
        -> Assessment Result
        -> Secure Result Channel -> Control Plane (Member 3)

Per Member 3's Backend & Cloud MVP Spec: raw enterprise data must never
leave the customer's AWS environment, and the Control Plane receives only
the minimum assessment information required for orchestration and the
dashboard. This agent enforces that boundary structurally — the full
AssessmentResult (which embeds the Data Profile, including sample field
values) is only ever written to a local audit file; only
`AssessmentResult.to_control_plane_payload()` is handed to the Secure
Result Channel.
"""
from __future__ import annotations

import logging

from ai_readiness_agent import audit_store
from ai_readiness_agent.assessment.models import AssessmentResult
from ai_readiness_agent.channel.secure_channel import DeliveryReceipt, SecureResultChannel
from ai_readiness_agent.config import AgentConfig, load_config
from ai_readiness_agent.engine.readiness_engine import assess
from ai_readiness_agent.ingestion.base import SourceBatch
from ai_readiness_agent.ingestion.documents_adapter import DocumentsAdapter
from ai_readiness_agent.ingestion.rds_adapter import RDSAdapter
from ai_readiness_agent.ingestion.s3_adapter import S3Adapter
from ai_readiness_agent.profiling import bedrock_analyzer, llm_analyzer
from ai_readiness_agent.profiling.models import LLMContentAnalysis
from ai_readiness_agent.profiling.profiler import build_data_profile

logger = logging.getLogger(__name__)


class AIReadinessAgent:
    def __init__(self, config: AgentConfig | None = None):
        self.config = config or load_config()
        self.s3_adapter = S3Adapter(self.config.s3)
        self.rds_adapter = RDSAdapter(self.config.rds)
        self.documents_adapter = DocumentsAdapter(self.config.documents)
        self.channel = SecureResultChannel(self.config.channel)

    # ------------------------------------------------------------------
    def ingest(
        self,
        sources: set[str] | None = None,
        *,
        s3_include_data: bool = True,
        s3_include_security: bool = True,
    ) -> list[SourceBatch]:
        """`sources` restricts which adapters run (default: all three) — used
        by the webapp's standalone "Scan S3" / "Security scan" / "Upload
        documents" buttons so each can run independently of the others.
        `s3_include_data` / `s3_include_security` further split the S3
        adapter itself into its two independent functions."""
        if sources is None:
            sources = {"s3", "rds", "documents"}

        adapters: list[tuple] = []
        if "s3" in sources:
            adapters.append((lambda: self.s3_adapter.fetch(
                include_data=s3_include_data, include_security=s3_include_security
            ), "S3"))
        if "rds" in sources:
            adapters.append((self.rds_adapter.fetch, "RDS"))
        if "documents" in sources:
            adapters.append((self.documents_adapter.fetch, "Documents"))

        batches: list[SourceBatch] = []
        for fetch, label in adapters:
            logger.info("Ingesting from %s...", label)
            fetched = fetch()
            for batch in fetched:
                if batch.errors:
                    logger.warning("%s/%s ingestion had %d error(s): %s", label, batch.source_name, len(batch.errors), batch.errors)
                logger.info("%s/%s: %d record(s) ingested.", label, batch.source_name, len(batch.records))
                batches.append(batch)
        return batches

    # ------------------------------------------------------------------
    def run(
        self,
        deliver: bool = True,
        *,
        use_case: str | None = None,
        environment_id: str | None = None,
        assessment_id: str | None = None,
        sources: set[str] | None = None,
        s3_include_data: bool = True,
        s3_include_security: bool = True,
    ) -> tuple[AssessmentResult, DeliveryReceipt | None]:
        """Run the full pipeline once: ingest -> profile -> score -> deliver.

        `use_case`, `environment_id`, and `assessment_id` normally come from
        the Control Plane's "trigger scan" request (POST /assessment ->
        BACKEND-008). They default to this agent's local config so the
        pipeline is runnable standalone for local dev/demo.

        `sources` / `s3_include_data` / `s3_include_security` restrict which
        adapters run this pass — see `ingest()`.
        """
        batches = self.ingest(sources, s3_include_data=s3_include_data, s3_include_security=s3_include_security)
        data_profile = build_data_profile(self.config.customer_id, batches, comprehend_config=self.config.comprehend)
        logger.info(
            "Data profile built: %d record(s) across %d source(s).",
            data_profile.total_records,
            len(data_profile.sources),
        )

        resolved_use_case = use_case or self.config.use_case
        data_profile.llm_analysis = llm_analyzer.analyze(data_profile, resolved_use_case, self.config.llm)
        if not data_profile.llm_analysis.performed and self.config.bedrock.enabled:
            anthropic_error = data_profile.llm_analysis.error
            logger.info(
                "Anthropic content analysis unavailable (%s); falling back to Amazon Bedrock.",
                anthropic_error,
            )
            data_profile.llm_analysis = bedrock_analyzer.analyze(data_profile, resolved_use_case, self.config.bedrock)
            # Surface both attempts' errors -- otherwise a Bedrock failure
            # silently hides whatever happened with the Anthropic attempt,
            # making it impossible to tell from the result alone whether a
            # fixed Anthropic key actually worked.
            if not data_profile.llm_analysis.performed and anthropic_error:
                bedrock_error = data_profile.llm_analysis.error
                data_profile.llm_analysis = LLMContentAnalysis(
                    error=f"anthropic: {anthropic_error} | bedrock: {bedrock_error}"
                )
        if data_profile.llm_analysis.performed:
            logger.info(
                "LLM content analysis (%s): %d sensitive finding(s), %d quality issue(s).",
                data_profile.llm_analysis.engine,
                len(data_profile.llm_analysis.sensitive_data_findings),
                len(data_profile.llm_analysis.quality_issues),
            )

        result = assess(
            data_profile,
            use_case=resolved_use_case,
            environment_id=environment_id or self.config.environment_id,
            assessment_id=assessment_id,
        )
        logger.info(
            "Assessment complete: score=%.0f readiness_status=%s projected_score=%.0f",
            result.overall_score,
            result.readiness_level.value,
            result.projected_score,
        )

        audit_path = audit_store.write_audit(self.config, result)
        logger.info("Full assessment (with Data Profile) written to audit trail: %s", audit_path)

        receipt = None
        if deliver:
            payload = result.to_control_plane_payload()
            receipt = self.channel.send(payload)
            if receipt.delivered:
                logger.info("Minimal result payload delivered via %s -> %s", receipt.transport, receipt.location)
            else:
                logger.error("Failed to deliver assessment to Control Plane: %s", receipt.error)

        return result, receipt


def run_once(deliver: bool = True) -> tuple[AssessmentResult, DeliveryReceipt | None]:
    """Convenience module-level function, e.g. for a container entrypoint
    triggered by the Control Plane's scan request:

        def handle_scan_trigger(event, context):
            agent = AIReadinessAgent()
            result, receipt = agent.run(
                use_case=event["use_case"],
                environment_id=event["environment_id"],
                assessment_id=event["assessment_id"],
            )
            return {"statusCode": 200}
    """
    agent = AIReadinessAgent()
    return agent.run(deliver=deliver)
