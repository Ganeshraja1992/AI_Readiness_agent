"""
Simple local demo web app for the AI Readiness Agent.

This is NOT Member 3's Control Plane (no persistence service, no state
machine, no multi-tenant auth) — it's a thin Flask wrapper around the same
`AIReadinessAgent` the CLI uses. New assessments are run through a guided
wizard that mirrors the product flow:

    Select AI Use Case -> Connect / Upload Data -> Analyze Data Estate ->
    Calculate AI Readiness -> Identify Critical Blockers ->
    Generate Remediation Plan -> Show Projected Readiness

Every run still goes through the exact same audit boundary: the full result
(with the Data Profile) is written to the DynamoDB audit table, and only
the minimal `ControlPlanePayload` is shown as "what would be sent to the
Control Plane." Data-source connectors (S3 bucket, RDS database) are saved
to AWS Systems Manager Parameter Store, and uploaded documents go to a real
S3 bucket — see connectors.py and DOCUMENTS_BUCKET below. This app expects
real AWS credentials in the environment (e.g. an active `aws sso login`
session); it is not a zero-AWS-account demo.

Run:
    pip install -r requirements.txt
    python webapp/app.py
    # then open http://127.0.0.1:5000

Login defaults to admin / changeme — override with WEBAPP_USERNAME /
WEBAPP_PASSWORD env vars. This is single-user, session-cookie auth meant
for a local demo, not production multi-tenant auth.
"""
from __future__ import annotations

import functools
import os
import sys
import uuid
from pathlib import Path

# Let this run as `python webapp/app.py` (script directory is auto-added
# to sys.path) or imported as `webapp.app:app` under gunicorn (it isn't) —
# add both `src/` and this directory explicitly so `connectors` and
# `ai_readiness_agent` resolve either way.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "src"))

from flask import Flask, Response, redirect, render_template, request, session, url_for  # noqa: E402
from werkzeug.utils import secure_filename  # noqa: E402

import connectors  # noqa: E402
from ai_readiness_agent import audit_store  # noqa: E402
from ai_readiness_agent.agent import AIReadinessAgent  # noqa: E402
from ai_readiness_agent.config import load_config  # noqa: E402
from ai_readiness_agent.engine import rules  # noqa: E402
from ai_readiness_agent.export.csv_report import generate_csv_report  # noqa: E402
from ai_readiness_agent.export.pdf_report import generate_pdf_report  # noqa: E402

def _resolve_secret_key() -> str:
    """WEBAPP_SECRET_KEY wins if set. Otherwise, persist a generated key to
    a local file instead of regenerating one on every process start —
    the debug reloader restarts this process on every file save, and a
    fresh random key each time invalidates every logged-in session."""
    env_key = os.environ.get("WEBAPP_SECRET_KEY")
    if env_key:
        return env_key
    key_path = ROOT / ".flask_secret_key"
    if key_path.exists():
        return key_path.read_text().strip()
    key = os.urandom(24).hex()
    key_path.write_text(key)
    return key


app = Flask(__name__)
app.secret_key = _resolve_secret_key()

WEBAPP_USERNAME = os.environ.get("WEBAPP_USERNAME", "admin")
WEBAPP_PASSWORD = os.environ.get("WEBAPP_PASSWORD", "changeme")

# The webapp is the AWS-backed deployment: assessment history goes to
# DynamoDB, uploaded documents go to S3, and S3/document content gets a
# real Amazon Comprehend PII scan on top of the regex heuristic — instead
# of the CLI's local-file/no-AWS defaults (used for standalone/test runs).
DOCUMENTS_BUCKET = os.environ.get("READINESS_DOCUMENTS_BUCKET", "ai-readiness-agent-docs-853973692277")


def _config():
    config = load_config()
    config.audit_backend = "dynamodb"
    config.documents.bucket = DOCUMENTS_BUCKET
    config.comprehend.enabled = True
    return config

_READINESS_BADGE = {
    "NOT_READY": "critical",
    "NEEDS_WORK": "warning",
    "READY": "good",
    "AI_READY": "good",
}
_SEVERITY_BADGE = {
    "info": "info",
    "warning": "warning",
    "critical": "critical",
}

# Four-tier concern labels applied to any 0-100 score (a single dimension's
# score, or an assessment's overall_score) -- ascending concern, Warning
# being the most severe. Deliberately the same 85/70/50 boundaries the
# engine already uses for NOT_READY/NEEDS_WORK/READY/AI_READY (rules.py's
# READINESS_LEVEL_THRESHOLDS), just relabeled for this more general use.
_SEVERITY_TIER_THRESHOLDS = ((85, "Low"), (70, "Medium"), (50, "High"))
_SEVERITY_TIER_DEFAULT = "Warning"
_SEVERITY_TIER_BADGE = {"Low": "good", "Medium": "warning", "High": "serious", "Warning": "critical"}
_LEVEL_TO_TIER = {
    "AI_READY": "Low",
    "READY": "Medium",
    "NEEDS_WORK": "High",
    "NOT_READY": "Warning",
}


def _severity_tier(score: float) -> str:
    for floor, label in _SEVERITY_TIER_THRESHOLDS:
        if score >= floor:
            return label
    return _SEVERITY_TIER_DEFAULT

# The 7-stage product flow this wizard walks through. "usecase" and
# "connect" are their own pages (steps 1-2); "analyze".."projected" are all
# rendered from the single already-computed AssessmentResult (steps 3-7) —
# scoring is fast enough to run synchronously in one pass, so these stages
# are a guided *reveal* of that one result rather than separate compute
# passes.
ALL_STAGES = [
    ("usecase", "Select AI Use Case"),
    ("connect", "Connect / Upload Data"),
    ("analyze", "Analyze Data Estate"),
    ("readiness", "Calculate AI Readiness"),
    ("blockers", "Identify Critical Blockers"),
    ("remediation", "Generate Remediation Plan"),
    ("projected", "Show Projected Readiness"),
]
RESULT_STAGES = [key for key, _ in ALL_STAGES[2:]]  # analyze..projected


@app.context_processor
def inject_stepper_stages():
    return {"stepper_stages": ALL_STAGES}


@app.template_filter("readiness_badge")
def readiness_badge(level: str) -> str:
    return _READINESS_BADGE.get(level, "info")


@app.template_filter("severity_badge")
def severity_badge(severity: str) -> str:
    return _SEVERITY_BADGE.get(severity, "info")


@app.template_filter("dimension_label")
def dimension_label(name: str) -> str:
    return name.replace("_", " ").title().replace("Ai ", "AI ")


@app.template_filter("severity_tier")
def severity_tier_filter(score: float) -> str:
    return _severity_tier(score)


@app.template_filter("severity_tier_badge")
def severity_tier_badge_filter(tier: str) -> str:
    return _SEVERITY_TIER_BADGE.get(tier, "info")


@app.template_filter("level_tier")
def level_tier_filter(level: str) -> str:
    return _LEVEL_TO_TIER.get(level, "Warning")


def _use_case_choices() -> list[str]:
    from ai_readiness_agent.engine.readiness_engine import DEFAULT_USE_CASE

    choices = sorted(rules.USE_CASE_DIMENSION_WEIGHTS.keys())
    if DEFAULT_USE_CASE not in choices:
        choices.append(DEFAULT_USE_CASE)
    return choices


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == WEBAPP_USERNAME and password == WEBAPP_PASSWORD:
            session["user"] = username
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        error = "Incorrect username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


_LEVEL_ORDER = ["NOT_READY", "NEEDS_WORK", "READY", "AI_READY"]
_TREND_MAX_POINTS = 15
_SPARKLINE_W, _SPARKLINE_H, _SPARKLINE_PAD = 400, 80, 10


def _dashboard_stats(assessments: list[dict]) -> dict:
    """Summary stats + a trend sparkline for the home page dashboard, all
    derived from the same list_audits() rows the history table already
    renders -- no extra DynamoDB reads."""
    level_counts = {lvl: 0 for lvl in _LEVEL_ORDER}
    total = len(assessments)
    if total == 0:
        return {
            "total": 0,
            "avg_score": 0,
            "level_counts": level_counts,
            "level_pct": {lvl: 0 for lvl in _LEVEL_ORDER},
            "needs_attention": 0,
            "trend_points": "",
            "trend_last": None,
            "trend_count": 0,
        }

    for a in assessments:
        level_counts[a["readiness_level"]] = level_counts.get(a["readiness_level"], 0) + 1
    avg_score = round(sum(a["overall_score"] for a in assessments) / total)
    level_pct = {lvl: round(count * 100 / total) for lvl, count in level_counts.items()}

    trend_source = sorted(assessments, key=lambda a: a["generated_at"])[-_TREND_MAX_POINTS:]
    n = len(trend_source)
    w, h, pad = _SPARKLINE_W, _SPARKLINE_H, _SPARKLINE_PAD
    if n == 1:
        x, y = w / 2, pad + (1 - trend_source[0]["overall_score"] / 100) * (h - 2 * pad)
        points = [(x, y)]
    else:
        step = (w - 2 * pad) / (n - 1)
        points = [
            (pad + i * step, pad + (1 - t["overall_score"] / 100) * (h - 2 * pad))
            for i, t in enumerate(trend_source)
        ]

    return {
        "total": total,
        "avg_score": avg_score,
        "level_counts": level_counts,
        "level_pct": level_pct,
        "needs_attention": level_counts["NOT_READY"] + level_counts["NEEDS_WORK"],
        "trend_points": " ".join(f"{x:.1f},{y:.1f}" for x, y in points),
        "trend_last": points[-1],
        "trend_count": n,
    }


@app.route("/")
def home():
    """Public landing page — explains the product and the 7-stage
    workflow. No login required; logged-in visitors get a "Go to
    dashboard" CTA instead of "Log in"."""
    return render_template("home.html", user=session.get("user"))


@app.route("/dashboard")
@login_required
def dashboard():
    assessments = audit_store.list_audits(_config())
    stats = _dashboard_stats(assessments)
    return render_template("dashboard.html", user=session.get("user"), assessments=assessments, stats=stats)


# ----------------------------------------------------------------------
# Wizard step 1: Select AI Use Case
# ----------------------------------------------------------------------
@app.route("/wizard")
@login_required
def wizard_step1():
    config = _config()
    return render_template(
        "wizard_step1.html",
        user=session.get("user"),
        use_cases=_use_case_choices(),
        default_use_case=config.use_case,
        default_environment_id=config.environment_id,
        stepper_current_index=0,
    )


# ----------------------------------------------------------------------
# Wizard step 2: Connect / Upload Data
# ----------------------------------------------------------------------
@app.route("/wizard/connect")
@login_required
def wizard_step2():
    config = _config()
    use_case = request.args.get("use_case") or config.use_case
    environment_id = request.args.get("environment_id") or config.environment_id
    return render_template(
        "wizard_step2.html",
        user=session.get("user"),
        use_case=use_case,
        environment_id=environment_id,
        s3_connector=connectors.load_s3_connector(),
        rds_connector=connectors.load_rds_connector(),
        stepper_current_index=1,
    )


def _connect_error(use_case: str, environment_id: str, message: str):
    return render_template(
        "wizard_step2.html",
        user=session.get("user"),
        use_case=use_case,
        environment_id=environment_id,
        s3_connector=connectors.load_s3_connector(),
        rds_connector=connectors.load_rds_connector(),
        stepper_current_index=1,
        error=message,
    ), 400


@app.route("/connectors/s3", methods=["POST"])
@login_required
def save_s3_connector():
    use_case = request.form.get("use_case", "")
    environment_id = request.form.get("environment_id", "")
    raw_bucket = request.form.get("bucket", "").strip()
    prefix = request.form.get("prefix", "").strip()
    region = request.form.get("region", "").strip()

    # Accept "s3://bucket/prefix" or a bare bucket name in the bucket field.
    value = raw_bucket
    if value.startswith("s3://"):
        value = value[len("s3://"):]
    bucket, _, inline_prefix = value.partition("/")
    if not bucket:
        return _connect_error(use_case, environment_id, "S3 bucket name is required to save a connector.")

    connectors.save_s3_connector(bucket, prefix or inline_prefix, region)
    return redirect(url_for("wizard_step2", use_case=use_case, environment_id=environment_id))


@app.route("/connectors/s3/delete", methods=["POST"])
@login_required
def delete_s3_connector():
    use_case = request.form.get("use_case", "")
    environment_id = request.form.get("environment_id", "")
    connectors.delete_s3_connector()
    return redirect(url_for("wizard_step2", use_case=use_case, environment_id=environment_id))


@app.route("/connectors/rds", methods=["POST"])
@login_required
def save_rds_connector():
    use_case = request.form.get("use_case", "")
    environment_id = request.form.get("environment_id", "")
    host = request.form.get("host", "").strip()
    if not host:
        return _connect_error(use_case, environment_id, "RDS host is required to save a connector.")

    # An "Edit connection" submit leaves the password field blank to mean
    # "keep the existing password" rather than clearing it.
    password = request.form.get("password", "")
    if not password:
        existing = connectors.load_rds_connector()
        password = existing["password"] if existing else ""

    connectors.save_rds_connector(
        engine=request.form.get("engine", "postgresql"),
        host=host,
        port=request.form.get("port", ""),
        database=request.form.get("database", ""),
        table=request.form.get("table", ""),
        username=request.form.get("username", ""),
        password=password,
    )
    return redirect(url_for("wizard_step2", use_case=use_case, environment_id=environment_id))


@app.route("/connectors/rds/delete", methods=["POST"])
@login_required
def delete_rds_connector():
    use_case = request.form.get("use_case", "")
    environment_id = request.form.get("environment_id", "")
    connectors.delete_rds_connector()
    return redirect(url_for("wizard_step2", use_case=use_case, environment_id=environment_id))


def _apply_s3_connector(config, connector: connectors.S3Connector) -> None:
    """Point config.s3 at the saved connector. Overrides the env-var default
    and switches off mock mode since a real bucket was configured."""
    config.s3.bucket = connector["bucket"]
    config.s3.prefix = connector["prefix"]
    if connector["region"]:
        config.s3.region = connector["region"]
    config.s3.use_mock = False


def _apply_rds_connector(config, connector: connectors.RDSConnector) -> None:
    """Point config.rds at the saved connector and switch off mock mode
    since a real database was configured."""
    config.rds.engine = connector["engine"]
    config.rds.host = connector["host"]
    config.rds.port = int(connector["port"])
    config.rds.database = connector["database"]
    config.rds.table = connector["table"]
    config.rds.username = connector["username"]
    config.rds.password = connector["password"]
    config.rds.use_mock = False


def _upload_documents_to_s3(config, files) -> str | None:
    """Upload PDF/CSV/etc. files to a fresh prefix in the documents bucket
    the DocumentsAdapter can scan for this run. Returns the S3 prefix, or
    None if nothing was uploaded."""
    files = [f for f in files if f and f.filename]
    if not files:
        return None

    import boto3

    client = boto3.client("s3", region_name=config.documents.region)
    prefix = f"uploads/{uuid.uuid4()}/"
    for f in files:
        filename = secure_filename(f.filename)
        if filename:
            client.upload_fileobj(f, config.documents.bucket, prefix + filename)
    return prefix


def _delete_uploaded_documents(config, prefix: str) -> None:
    import boto3

    client = boto3.client("s3", region_name=config.documents.region)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.documents.bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=config.documents.bucket, Delete={"Objects": keys})


_VALID_ACTIONS = {"s3", "security", "documents", "rds"}


@app.route("/run", methods=["POST"])
@login_required
def run_assessment():
    use_case = request.form.get("use_case") or None
    environment_id = request.form.get("environment_id") or None
    action = request.form.get("action", "")

    config = _config()

    def _error(message: str):
        return _connect_error(use_case or config.use_case, environment_id or config.environment_id, message)

    if action not in _VALID_ACTIONS:
        return _error(f"Unknown action: {action!r}")

    sources: set[str]
    upload_prefix: str | None = None
    s3_include_data = True
    s3_include_security = True

    if action in ("s3", "security"):
        connector = connectors.load_s3_connector()
        if not connector:
            return _error("No S3 connector configured. Connect a bucket above first.")
        _apply_s3_connector(config, connector)
        sources = {"s3"}
        # Both actions list bucket records; "security" additionally runs the
        # bucket-config checks (public access/encryption/versioning) so its
        # Analyze Data Estate page isn't empty.
        s3_include_data = True
        s3_include_security = action == "security"
    elif action == "rds":
        connector = connectors.load_rds_connector()
        if not connector:
            return _error("No RDS connector configured. Connect a database above first.")
        _apply_rds_connector(config, connector)
        sources = {"rds"}
    else:  # action == "documents"
        upload_prefix = _upload_documents_to_s3(config, request.files.getlist("documents"))
        if not upload_prefix:
            return _error("Upload at least one document for this action.")
        config.documents.s3_prefix = upload_prefix
        sources = {"documents"}

    try:
        agent = AIReadinessAgent(config)
        result, receipt = agent.run(
            deliver=True,
            use_case=use_case,
            environment_id=environment_id,
            sources=sources,
            s3_include_data=s3_include_data,
            s3_include_security=s3_include_security,
        )
    finally:
        if upload_prefix:
            _delete_uploaded_documents(config, upload_prefix)

    # Steps 3-7: hand the freshly computed result to the guided reveal,
    # starting at "Analyze Data Estate".
    return redirect(url_for("wizard_result", assessment_id=result.assessment_id, stage=RESULT_STAGES[0]))


def _load_assessment(assessment_id: str):
    return audit_store.read_audit(_config(), assessment_id)


# ----------------------------------------------------------------------
# Wizard steps 3-7: Analyze -> Calculate -> Blockers -> Remediation -> Projected
# ----------------------------------------------------------------------
@app.route("/wizard/result/<assessment_id>/<stage>")
@login_required
def wizard_result(assessment_id: str, stage: str):
    if stage not in RESULT_STAGES:
        return redirect(url_for("wizard_result", assessment_id=assessment_id, stage=RESULT_STAGES[0]))

    result = _load_assessment(assessment_id)
    if not result:
        return redirect(url_for("dashboard"))

    idx = RESULT_STAGES.index(stage)
    prev_stage = RESULT_STAGES[idx - 1] if idx > 0 else None
    next_stage = RESULT_STAGES[idx + 1] if idx < len(RESULT_STAGES) - 1 else None
    critical_findings = [f for f in result.findings if f.severity in ("critical", "warning")]

    return render_template(
        "wizard_result.html",
        user=session.get("user"),
        result=result,
        stage=stage,
        stage_label=dict(ALL_STAGES)[stage],
        prev_stage=prev_stage,
        next_stage=next_stage,
        critical_findings=critical_findings,
        payload_json=result.to_control_plane_payload().to_json() if stage == "projected" else None,
        stepper_current_index=2 + idx,
    )


@app.route("/assessment/<assessment_id>")
@login_required
def view_assessment(assessment_id: str):
    result = _load_assessment(assessment_id)
    if not result:
        assessments = audit_store.list_audits(_config())
        return render_template(
            "dashboard.html",
            user=session.get("user"),
            assessments=assessments,
            stats=_dashboard_stats(assessments),
            error=f"No assessment found for {assessment_id}.",
        ), 404

    payload = result.to_control_plane_payload()
    return render_template(
        "result.html",
        user=session.get("user"),
        result=result,
        payload_json=payload.to_json(),
    )


@app.route("/assessment/<assessment_id>/export.pdf")
@login_required
def export_assessment_pdf(assessment_id: str):
    result = _load_assessment(assessment_id)
    if not result:
        return redirect(url_for("dashboard"))
    pdf_bytes = generate_pdf_report(result)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="assessment-{assessment_id[:8]}.pdf"'},
    )


@app.route("/assessment/<assessment_id>/export.csv")
@login_required
def export_assessment_csv(assessment_id: str):
    result = _load_assessment(assessment_id)
    if not result:
        return redirect(url_for("dashboard"))
    csv_text = generate_csv_report(result)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="assessment-{assessment_id[:8]}.csv"'},
    )


@app.route("/assessment/<assessment_id>/delete", methods=["POST"])
@login_required
def delete_assessment(assessment_id: str):
    audit_store.delete_audit(_config(), assessment_id)
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    debug = os.environ.get("WEBAPP_DEBUG", "true").lower() in {"1", "true", "yes"}
    port = int(os.environ.get("WEBAPP_PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=debug)
