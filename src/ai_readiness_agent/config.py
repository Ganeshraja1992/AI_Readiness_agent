"""
Central configuration for the AI Readiness Agent.

Values are read from environment variables first (so this drops straight into
a container / Lambda / ECS task with real AWS creds and a real Control Plane
endpoint) and fall back to sane local-mock defaults so the whole pipeline runs
out of the box with no AWS account at all.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MOCK_DATA_DIR = REPO_ROOT / "mock_data"


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class S3Config:
    bucket: str = os.environ.get("READINESS_S3_BUCKET", "customer-data-bucket")
    prefix: str = os.environ.get("READINESS_S3_PREFIX", "")
    region: str = os.environ.get("AWS_REGION", "us-east-1")
    # When True (default unless AWS creds are present) reads from mock_data/s3
    # instead of calling real S3.
    use_mock: bool = _bool_env("READINESS_USE_MOCK_AWS", True)
    mock_dir: Path = field(default_factory=lambda: DEFAULT_MOCK_DATA_DIR / "s3")


@dataclass
class RDSConfig:
    # "postgresql", "mysql", "mariadb", "oracle", or "mssql" -- every engine
    # Amazon RDS offers. Picks the SQLAlchemy dialect/driver in
    # rds_adapter.py. Pointing the wrong dialect at a real server causes the
    # connection to hang on a protocol mismatch rather than fail fast.
    engine: str = os.environ.get("READINESS_RDS_ENGINE", "postgresql")
    host: str = os.environ.get("READINESS_RDS_HOST", "customer-db.cluster.rds.amazonaws.com")
    port: int = int(os.environ.get("READINESS_RDS_PORT", "5432"))
    database: str = os.environ.get("READINESS_RDS_DATABASE", "app")
    table: str = os.environ.get("READINESS_RDS_TABLE", "customers")
    username: str = os.environ.get("READINESS_RDS_USER", "")
    password: str = os.environ.get("READINESS_RDS_PASSWORD", "")
    use_mock: bool = _bool_env("READINESS_USE_MOCK_AWS", True)
    mock_csv: Path = field(default_factory=lambda: DEFAULT_MOCK_DATA_DIR / "rds_customers.csv")


@dataclass
class DocumentsConfig:
    # Local fallback: a folder on disk (used by the CLI / tests / mock_data).
    directory: Path = field(
        default_factory=lambda: Path(
            os.environ.get("READINESS_DOCUMENTS_DIR", str(DEFAULT_MOCK_DATA_DIR / "documents"))
        )
    )
    # Real deployments (the webapp) instead upload to an S3 bucket/prefix.
    # When `bucket` and `s3_prefix` are both set, the adapter reads from S3
    # instead of `directory`.
    bucket: str = os.environ.get("READINESS_DOCUMENTS_BUCKET", "")
    s3_prefix: str = ""
    region: str = os.environ.get("AWS_REGION", "us-east-1")


@dataclass
class LLMConfig:
    # Deeper content analysis (sensitive-content nuance, quality issues, use-case
    # fit) via the Anthropic API, on top of the regex-based PII scan. Off by
    # default unless a key is present, so the pipeline still runs standalone
    # with no AWS/Anthropic account at all.
    api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    model: str = os.environ.get("READINESS_LLM_MODEL", "claude-opus-5")
    max_samples: int = int(os.environ.get("READINESS_LLM_MAX_SAMPLES", "40"))
    enabled: bool = _bool_env("READINESS_LLM_ENABLED", True)

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.api_key)


@dataclass
class BedrockConfig:
    # Fallback content-analysis engine for when the direct Anthropic API
    # call fails (billing/credit issues, network, invalid key, or no key
    # configured at all) -- see llm_analyzer.py / bedrock_analyzer.py /
    # agent.py's try-Claude-then-Bedrock chain. Runs Claude via Amazon
    # Bedrock instead, billed through AWS and authenticated with the same
    # credentials already used for S3/RDS/DynamoDB/Comprehend -- no
    # separate Anthropic account needed. Off by default so the CLI/tests
    # still need no AWS account; the webapp turns this on.
    #
    # Requires bedrock:InvokeModel (or Converse) IAM permission, and the
    # model must have "model access" granted in the Bedrock console for
    # this account/region before first use.
    enabled: bool = _bool_env("READINESS_BEDROCK_ENABLED", False)
    region: str = os.environ.get("AWS_BEDROCK_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    # Bedrock's Anthropic model catalog changes over time (older versions
    # get retired -- e.g. claude-3-5-sonnet-20241022-v2:0 has reached
    # end-of-life). If this default 404s/ResourceNotFoundExceptions,
    # check the Bedrock console's model catalog for this account/region
    # and set READINESS_BEDROCK_MODEL_ID to whatever's actually current --
    # newer models sometimes need a region-prefixed inference-profile ID
    # (e.g. "us.anthropic.claude-...") rather than the bare model ID.
    model_id: str = os.environ.get("READINESS_BEDROCK_MODEL_ID", "anthropic.claude-3-7-sonnet-20250219-v1:0")
    max_samples: int = int(os.environ.get("READINESS_LLM_MAX_SAMPLES", "40"))


@dataclass
class OpenRouterConfig:
    # Third content-analysis engine, tried between the direct Anthropic API
    # and the Amazon Bedrock fallback -- for setups (e.g. a hackathon team
    # issued a shared OpenRouter key instead of its own Anthropic/AWS
    # account) that only have an OpenRouter key. Off by default unless a key
    # is present, same pattern as LLMConfig.
    api_key: str = os.environ.get("OPENROUTER_API_KEY", "")
    model: str = os.environ.get("READINESS_OPENROUTER_MODEL", "anthropic.claude-sonnet-5")
    # Defaults to the public OpenRouter API. Some setups (e.g. a hackathon-
    # issued key) route through a custom gateway instead -- if this fails
    # with a 404/wrong-endpoint error, that's the first thing to check.
    base_url: str = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
    max_samples: int = int(os.environ.get("READINESS_LLM_MAX_SAMPLES", "40"))

    @property
    def active(self) -> bool:
        return bool(self.api_key)


@dataclass
class ComprehendConfig:
    # Real AWS PII detection (Amazon Comprehend's DetectPiiEntities) run
    # alongside the zero-cost regex heuristic in profiling/pii.py, for S3
    # and document sources only. Off by default so the CLI/tests still need
    # no AWS account; the webapp turns this on since it's already AWS-backed.
    enabled: bool = _bool_env("READINESS_COMPREHEND_ENABLED", False)
    region: str = os.environ.get("AWS_REGION", "us-east-1")
    language_code: str = os.environ.get("READINESS_COMPREHEND_LANGUAGE", "en")
    # DetectPiiEntities caps at 5000 UTF-8 bytes per call; stay safely under.
    max_chars_per_call: int = 4500


@dataclass
class SecureChannelConfig:
    control_plane_url: str = os.environ.get("CONTROL_PLANE_URL", "")
    shared_secret: str = os.environ.get("CONTROL_PLANE_SHARED_SECRET", "dev-shared-secret-change-me")
    timeout_seconds: float = float(os.environ.get("CONTROL_PLANE_TIMEOUT", "10"))
    max_retries: int = int(os.environ.get("CONTROL_PLANE_MAX_RETRIES", "3"))
    # Per the MVP spec ("Agent authentication must prevent arbitrary clients
    # from submitting results"), every request identifies the agent that's
    # submitting. This is a simple explicit ID for the hackathon MVP, not a
    # full device-management system — swap for an AWS-native identity
    # (IAM role / SigV4) in a production deployment.
    agent_id: str = os.environ.get("READINESS_AGENT_ID", "agent-local-dev")
    # If no control_plane_url is set (local dev), signed results are written
    # here instead of being POSTed, so Member 3 can point their Control Plane
    # ingestion at this directory during integration testing.
    outbox_dir: Path = field(default_factory=lambda: REPO_ROOT / "outbox")


@dataclass
class AgentConfig:
    customer_id: str = os.environ.get("READINESS_CUSTOMER_ID", "demo-customer")
    # environment_id ties this agent's results back to the environment the
    # Control Plane created the assessment for (spec section 8: POST
    # /assessment {"use_case": ..., "environment_id": ...}).
    environment_id: str = os.environ.get("READINESS_ENVIRONMENT_ID", "demo-customer-aws")
    # Which AI use case is being assessed. In the real flow this comes from
    # the Control Plane's "trigger scan" request; it defaults here so the
    # pipeline is runnable standalone for local dev/demo.
    use_case: str = os.environ.get("READINESS_USE_CASE", "customer_support_agent")
    s3: S3Config = field(default_factory=S3Config)
    rds: RDSConfig = field(default_factory=RDSConfig)
    documents: DocumentsConfig = field(default_factory=DocumentsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)
    bedrock: BedrockConfig = field(default_factory=BedrockConfig)
    comprehend: ComprehendConfig = field(default_factory=ComprehendConfig)
    channel: SecureChannelConfig = field(default_factory=SecureChannelConfig)
    # Full assessment results (including the Data Profile, which can contain
    # sample field values) are written here for the customer's own audit
    # trail. This directory stays inside the customer's environment and is
    # NEVER transmitted anywhere — only the minimal ControlPlanePayload is
    # sent over the Secure Result Channel. See assessment/models.py.
    local_audit_dir: Path = field(default_factory=lambda: REPO_ROOT / "local_audit")
    # "local" (default, no AWS account needed) writes the full audit result to
    # local_audit_dir as before. "dynamodb" (used by the webapp) writes it to
    # a DynamoDB table instead. See audit_store.py.
    audit_backend: str = os.environ.get("READINESS_AUDIT_BACKEND", "local")
    dynamodb_table: str = os.environ.get("READINESS_DYNAMODB_TABLE", "ai-readiness-assessments")
    dynamodb_region: str = os.environ.get("AWS_REGION", "us-east-1")


def load_config() -> AgentConfig:
    """Single entry point the rest of the codebase uses to get config."""
    return AgentConfig()
