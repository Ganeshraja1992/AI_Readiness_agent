"""Lightweight, dependency-free PII heuristics.

Not a substitute for a real DLP/PII scanner (e.g. AWS Macie) — this is a
fast, explainable first pass that's enough to flag risk in the readiness
score and tell the customer roughly what to go look at.
"""
from __future__ import annotations

import re

PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<!\d)(\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3,4}[\s.-]\d{4}(?!\d)"),
    "ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    "credit_card": re.compile(r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)"),
}


def scan_text(text: str) -> dict[str, int]:
    """Return counts of each PII kind found in a blob of text."""
    findings: dict[str, int] = {}
    if not text:
        return findings
    for kind, pattern in PII_PATTERNS.items():
        matches = pattern.findall(text) if pattern.groups == 0 else pattern.findall(text)
        count = len(pattern.findall(text))
        if count:
            findings[kind] = count
    return findings


def scan_text_detailed(text: str) -> dict[str, list[str]]:
    """Return the actual matched substrings per PII kind (not just counts)
    -- used by the PII-masking remediation, which needs to know exactly
    what to redact. Uses finditer().group(0) rather than findall(): the
    "phone" pattern has a capturing group, and findall() on a pattern with
    exactly one group returns that group's text (often empty, since it's
    optional) instead of the full match."""
    findings: dict[str, list[str]] = {}
    if not text:
        return findings
    for kind, pattern in PII_PATTERNS.items():
        matches = [m.group(0) for m in pattern.finditer(text)]
        if matches:
            findings[kind] = matches
    return findings


def merge_findings(*finding_dicts: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for findings in finding_dicts:
        for kind, count in findings.items():
            merged[kind] = merged.get(kind, 0) + count
    return merged
