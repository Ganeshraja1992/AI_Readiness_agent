"""
Audit trail storage for the full AssessmentResult (including the Data
Profile, which can contain sample field values). This NEVER crosses the
Secure Result Channel — only `AssessmentResult.to_control_plane_payload()`
is sent there. See agent.py.

Two backends, selected by `AgentConfig.audit_backend`:
  - "local" (default): writes/reads a JSON file per assessment under
    `local_audit_dir`. No AWS account needed — this is what the CLI and
    tests use.
  - "dynamodb": writes/reads items in a DynamoDB table. Used by the webapp
    once it's pointed at a real AWS account.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ai_readiness_agent.assessment.models import AssessmentResult
from ai_readiness_agent.config import AgentConfig


def write_audit(config: AgentConfig, result: AssessmentResult) -> str:
    if config.audit_backend == "dynamodb":
        return _write_dynamodb(config, result)
    return _write_local(config, result)


def read_audit(config: AgentConfig, assessment_id: str) -> AssessmentResult | None:
    if config.audit_backend == "dynamodb":
        return _read_dynamodb(config, assessment_id)
    return _read_local(config, assessment_id)


def list_audits(config: AgentConfig, limit: int = 100) -> list[dict]:
    """Summary rows for the home page table: assessment_id, use_case,
    environment_id, overall_score, readiness_level, generated_at (datetime),
    newest first."""
    if config.audit_backend == "dynamodb":
        return _list_dynamodb(config, limit)
    return _list_local(config, limit)


def delete_audit(config: AgentConfig, assessment_id: str) -> None:
    if config.audit_backend == "dynamodb":
        _delete_dynamodb(config, assessment_id)
    else:
        _delete_local(config, assessment_id)


# ----------------------------------------------------------------------
# Local backend
# ----------------------------------------------------------------------
def _write_local(config: AgentConfig, result: AssessmentResult) -> str:
    audit_dir = config.local_audit_dir
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{result.assessment_id}.json"
    path.write_text(result.to_json(), encoding="utf-8")
    return str(path)


def _read_local(config: AgentConfig, assessment_id: str) -> AssessmentResult | None:
    path: Path = config.local_audit_dir / f"{assessment_id}.json"
    if not path.exists():
        return None
    return AssessmentResult.model_validate_json(path.read_text())


def _delete_local(config: AgentConfig, assessment_id: str) -> None:
    path: Path = config.local_audit_dir / f"{assessment_id}.json"
    path.unlink(missing_ok=True)


def _list_local(config: AgentConfig, limit: int) -> list[dict]:
    audit_dir = config.local_audit_dir
    if not audit_dir.exists():
        return []
    rows = []
    for path in sorted(audit_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            result = AssessmentResult.model_validate_json(path.read_text())
            rows.append(_summary_row(result))
        except Exception:  # noqa: BLE001 - skip unreadable/legacy files
            continue
    return rows


# ----------------------------------------------------------------------
# DynamoDB backend
# ----------------------------------------------------------------------
def _table(config: AgentConfig):
    import boto3  # imported lazily so the local backend has zero AWS dependency

    resource = boto3.resource("dynamodb", region_name=config.dynamodb_region)
    return resource.Table(config.dynamodb_table)


def _write_dynamodb(config: AgentConfig, result: AssessmentResult) -> str:
    table = _table(config)
    table.put_item(
        Item={
            "assessment_id": result.assessment_id,
            "use_case": result.use_case,
            "environment_id": result.environment_id,
            "overall_score": Decimal(str(round(result.overall_score, 2))),
            "readiness_level": result.readiness_level.value,
            "generated_at": result.generated_at.isoformat(),
            "result_json": result.to_json(),
        }
    )
    return f"dynamodb://{config.dynamodb_table}/{result.assessment_id}"


def _read_dynamodb(config: AgentConfig, assessment_id: str) -> AssessmentResult | None:
    table = _table(config)
    item = table.get_item(Key={"assessment_id": assessment_id}).get("Item")
    if not item:
        return None
    return AssessmentResult.model_validate_json(item["result_json"])


def _delete_dynamodb(config: AgentConfig, assessment_id: str) -> None:
    table = _table(config)
    table.delete_item(Key={"assessment_id": assessment_id})


def _list_dynamodb(config: AgentConfig, limit: int) -> list[dict]:
    table = _table(config)
    items: list[dict] = []
    scan_kwargs: dict = {}
    while True:
        page = table.scan(**scan_kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page or len(items) >= 1000:
            break
        scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    rows = []
    for item in items:
        try:
            rows.append(
                {
                    "assessment_id": item["assessment_id"],
                    "use_case": item.get("use_case", ""),
                    "environment_id": item.get("environment_id", ""),
                    "overall_score": round(float(item.get("overall_score", 0))),
                    "readiness_level": item.get("readiness_level", ""),
                    "generated_at": datetime.fromisoformat(item["generated_at"]),
                }
            )
        except Exception:  # noqa: BLE001 - skip malformed items
            continue
    rows.sort(key=lambda r: r["generated_at"], reverse=True)
    return rows[:limit]


def _summary_row(result: AssessmentResult) -> dict:
    return {
        "assessment_id": result.assessment_id,
        "use_case": result.use_case,
        "environment_id": result.environment_id,
        "overall_score": round(result.overall_score),
        "readiness_level": result.readiness_level.value,
        "generated_at": result.generated_at,
    }
