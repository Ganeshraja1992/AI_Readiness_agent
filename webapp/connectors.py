"""
Persisted data-source connectors for the webapp demo.

Unlike the env-var-sourced defaults in ai_readiness_agent.config, a
connector is entered once via the wizard's "Connect / upload data" step and
saved to AWS Systems Manager Parameter Store (as an encrypted SecureString),
so the Scan buttons don't need to ask for connection details on every click
and nothing sensitive (like an RDS password) sits in a plaintext file on
disk.
"""
from __future__ import annotations

import json
import os
from typing import Optional, TypedDict

SSM_PREFIX = "/ai-readiness-agent/connectors"
S3_PARAM = f"{SSM_PREFIX}/s3"
RDS_PARAM = f"{SSM_PREFIX}/rds"
_REGION = os.environ.get("AWS_REGION", "us-east-1")


def _ssm_client():
    import boto3  # imported lazily so importing this module has zero AWS dependency

    return boto3.client("ssm", region_name=_REGION)


def _load_param(name: str) -> Optional[dict]:
    from botocore.exceptions import ClientError

    try:
        resp = _ssm_client().get_parameter(Name=name, WithDecryption=True)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise
    try:
        return json.loads(resp["Parameter"]["Value"])
    except (json.JSONDecodeError, KeyError):
        return None


def _save_param(name: str, value: dict) -> None:
    _ssm_client().put_parameter(
        Name=name, Value=json.dumps(value), Type="SecureString", Overwrite=True
    )


def _delete_param(name: str) -> None:
    from botocore.exceptions import ClientError

    try:
        _ssm_client().delete_parameter(Name=name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise


# ----------------------------------------------------------------------
# S3 connector
# ----------------------------------------------------------------------
class S3Connector(TypedDict):
    bucket: str
    prefix: str
    region: str


def load_s3_connector() -> Optional[S3Connector]:
    data = _load_param(S3_PARAM)
    if not data:
        return None
    bucket = (data.get("bucket") or "").strip()
    if not bucket:
        return None
    return {"bucket": bucket, "prefix": data.get("prefix", ""), "region": data.get("region", "")}


def save_s3_connector(bucket: str, prefix: str, region: str) -> None:
    _save_param(S3_PARAM, {"bucket": bucket.strip(), "prefix": prefix.strip(), "region": region.strip()})


def delete_s3_connector() -> None:
    _delete_param(S3_PARAM)


# ----------------------------------------------------------------------
# RDS connector
# ----------------------------------------------------------------------
class RDSConnector(TypedDict):
    host: str
    port: str
    database: str
    table: str
    username: str
    password: str


def load_rds_connector() -> Optional[RDSConnector]:
    data = _load_param(RDS_PARAM)
    if not data:
        return None
    host = (data.get("host") or "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": data.get("port", "5432"),
        "database": data.get("database", ""),
        "table": data.get("table", ""),
        "username": data.get("username", ""),
        "password": data.get("password", ""),
    }


def save_rds_connector(
    host: str, port: str, database: str, table: str, username: str, password: str
) -> None:
    _save_param(
        RDS_PARAM,
        {
            "host": host.strip(),
            "port": port.strip() or "5432",
            "database": database.strip(),
            "table": table.strip(),
            "username": username.strip(),
            "password": password,
        },
    )


def delete_rds_connector() -> None:
    _delete_param(RDS_PARAM)
