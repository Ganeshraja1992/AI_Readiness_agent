"""Scoring thresholds and weights for the Readiness Engine.

Kept as plain constants (not env-driven) because these are the actual
business rules of "what makes data AI-ready" — they should be reviewed and
changed deliberately, in code review, not silently via env var.
"""
from __future__ import annotations

# Default dimension weights (must sum to 1.0). Overridden per use case below
# so `assess(profile, use_case=...)` scores the same Data Profile
# differently depending on what the customer wants to deploy AI for — a
# customer-support agent that touches PII cares more about privacy_risk
# than, say, an internal analytics dashboard.
DIMENSION_WEIGHTS: dict[str, float] = {
    "completeness": 0.15,
    "uniqueness": 0.10,
    "volume": 0.10,
    "freshness": 0.10,
    "privacy_risk": 0.20,
    "security": 0.20,
    "ai_content_analysis": 0.15,
}

# use_case -> weight overrides. Falls back to DIMENSION_WEIGHTS for any
# use case not listed here. Each entry must sum to 1.0.
USE_CASE_DIMENSION_WEIGHTS: dict[str, dict[str, float]] = {
    "customer_support_agent": {
        "completeness": 0.15,
        "uniqueness": 0.10,
        "volume": 0.05,
        "freshness": 0.10,
        "privacy_risk": 0.25,
        "security": 0.20,
        "ai_content_analysis": 0.15,
    },
    "fraud_detection_model": {
        "completeness": 0.10,
        "uniqueness": 0.10,
        "volume": 0.10,
        "freshness": 0.15,
        "privacy_risk": 0.15,
        "security": 0.25,
        "ai_content_analysis": 0.15,
    },
    "sales_forecasting_model": {
        "completeness": 0.15,
        "uniqueness": 0.15,
        "volume": 0.20,
        "freshness": 0.15,
        "privacy_risk": 0.10,
        "security": 0.15,
        "ai_content_analysis": 0.10,
    },
}


def weights_for_use_case(use_case: str) -> dict[str, float]:
    return USE_CASE_DIMENSION_WEIGHTS.get(use_case, DIMENSION_WEIGHTS)

HIGH_NULL_RATE_THRESHOLD = 0.20  # flag a field if >20% null
HIGH_DUPLICATE_RATE_THRESHOLD = 0.05  # flag a source if >5% duplicate records

VOLUME_BUCKETS = (
    (0, 0),
    (100, 40),
    (500, 65),
    (2000, 85),
    (float("inf"), 100),
)

FRESHNESS_BUCKETS_DAYS = (
    (7, 100),
    (30, 85),
    (90, 65),
    (365, 40),
    (float("inf"), 20),
)

# Severity multiplier applied to the per-finding penalty for privacy scoring
PII_SEVERITY_MULTIPLIER: dict[str, float] = {
    "ssn": 1.5,
    "credit_card": 1.5,
    "email": 1.0,
    "phone": 1.0,
    # Amazon Comprehend entity types (profiling/comprehend_pii.py) — keys
    # match Comprehend's own uppercase taxonomy, so they never collide with
    # the regex heuristic's lowercase keys above.
    "SSN": 1.5,
    "CREDIT_DEBIT_NUMBER": 1.5,
    "BANK_ACCOUNT_NUMBER": 1.5,
    "BANK_ROUTING": 1.5,
    "PASSPORT_NUMBER": 1.5,
    "PIN": 1.5,
    "EMAIL": 1.0,
    "PHONE": 1.0,
    "NAME": 1.0,
    "ADDRESS": 1.0,
}

READINESS_LEVEL_THRESHOLDS = (
    (85, "AI_READY"),
    (70, "READY"),
    (50, "NEEDS_WORK"),
    (0, "NOT_READY"),
)


def bucket_score(value: float, buckets: tuple[tuple[float, float], ...]) -> float:
    for ceiling, score in buckets:
        if value <= ceiling:
            return score
    return buckets[-1][1]


def readiness_level_for(score: float) -> str:
    for floor, level in READINESS_LEVEL_THRESHOLDS:
        if score >= floor:
            return level
    return "NOT_READY"
