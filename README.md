# AI Readiness Agent

This is the customer-side half of the AI Readiness Engine architecture:

```
Customer AWS (S3 / RDS / Documents)
        |
AI Readiness Agent          <- this repo
        |
Data Profile
        |
Readiness Engine
        |
Assessment Result
        |
Secure Result Channel
        |
CONTROL PLANE  ------------ Member 3 (backend/cloud)
        |
API / Persistence
        |
UI  ------------------------ Member 4
```

It ingests data from a customer's S3 buckets, RDS tables, and document
stores, builds a **Data Profile**, scores it with the **Readiness Engine**
against a selected AI use case, and hands the result to Member 3's Control
Plane over a **Secure Result Channel**.

It was built against `Member_3_Backend_Cloud_MVP_Spec.pdf` (the Backend &
Cloud team's MVP spec), and the code deliberately enforces the spec's
central constraint:

> Raw enterprise data should remain [in the customer's AWS environment].
> The control plane receives only the minimum assessment information
> required for orchestration and the dashboard.
>
> Never require raw S3 objects, database rows, or documents to be uploaded
> to the control plane.

See **"The local-data boundary"** below for exactly how that's enforced in
code, not just by convention.

## Quick start

```bash
pip install -r requirements.txt   # pydantic, boto3, SQLAlchemy, requests, PyYAML
                                   # (boto3/SQLAlchemy are only needed for real AWS mode)

PYTHONPATH=src python -m ai_readiness_agent.cli run
```

This runs the whole pipeline against the bundled `mock_data/` fixtures (no
AWS account needed) and prints the full local Assessment Result to stdout.
The minimal payload that would actually be sent to the Control Plane is
printed to stderr for visibility, followed by the delivery receipt.

Since no `CONTROL_PLANE_URL` is set by default, the signed payload is
written to `outbox/<assessment_id>.json` instead of being POSTed — point
Member 3's backend ingestion at that directory for integration testing, or
set `CONTROL_PLANE_URL` to POST directly.

Useful flags:

```bash
python -m ai_readiness_agent.cli run \
  --use-case customer_support_agent \
  --environment-id demo-customer-aws \
  --assessment-id assessment-001 \
  --no-deliver          # skip sending, just print
```

`--use-case`, `--environment-id`, and `--assessment-id` are normally
supplied by the Control Plane's "trigger scan" request (spec BACKEND-008);
they default from config/env vars so the pipeline is runnable standalone.

## Web app (local demo UI)

There's also a Flask app (`webapp/`) — a public landing page, a login-gated
dashboard, and a 7-stage guided wizard — for running/viewing assessments in
a browser instead of the CLI. It's a thin wrapper around the same
`AIReadinessAgent`, but unlike the CLI's zero-AWS-account defaults, the
webapp is AWS-backed: connectors live in SSM Parameter Store, assessment
history in DynamoDB, and PII scanning adds a real Amazon Comprehend call
alongside the regex heuristic. It is **not** Member 3's Control Plane or
Member 4's UI; it's a demo harness for this component.

```bash
pip install -r requirements.txt   # now includes Flask + gunicorn
python webapp/app.py
# open http://127.0.0.1:5000
```

Log in with `admin` / `changeme` (override via `WEBAPP_USERNAME` /
`WEBAPP_PASSWORD` env vars — this is single-user session-cookie auth for a
local demo, not production auth). From the dashboard you can start the
wizard: select an AI use case, then independently scan an S3 bucket, run
an S3 security check, scan an RDS database, or assess uploaded documents —
each produces its own result, walked through Analyze → Calculate →
Blockers → Remediation → Projected.

Other env vars: `WEBAPP_PORT` (default `5000`), `WEBAPP_DEBUG` (default
`true` — set `false` outside local dev), `WEBAPP_SECRET_KEY` (session
signing key; if unset, a generated key persists to a local
`.flask_secret_key` file so restarts don't drop sessions).

Since ingestion runs synchronously inside the request, a real (non-mock)
scan against large S3/RDS sources would need to move to a background job
— that's a deliberate simplification for this demo, consistent with the
spec's note that the *real* Control Plane must not block the HTTP request
on a long scan.

## Docker

The webapp also runs as a container, served by gunicorn instead of Flask's
dev server. The image doesn't bundle AWS or Anthropic credentials — you
supply those at run time.

```bash
docker build -t ai-readiness-agent .

docker run -p 5000:5000 \
  -e WEBAPP_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(24))')" \
  -e WEBAPP_USERNAME=admin -e WEBAPP_PASSWORD=changeme \
  -e AWS_REGION=us-east-1 \
  -v ~/.aws:/home/appuser/.aws:ro \
  ai-readiness-agent
# open http://localhost:5000
```

The `~/.aws` mount reuses your existing credentials/SSO session (an
assumed role from `aws sso login` works fine) so the container can reach
S3, RDS, SSM, DynamoDB, and Comprehend. Add `-e ANTHROPIC_API_KEY=...` to
enable the LLM content-analysis dimension.

That dimension has two engines, tried in order (see
`profiling/llm_analyzer.py` / `profiling/bedrock_analyzer.py` /
`agent.py`): a direct Anthropic API call first, falling back to Amazon
Bedrock (same model family, billed through AWS) if the direct call fails
for any reason — no key configured, invalid key, network error, or a
billing/credit issue on the Anthropic account. The webapp turns the
Bedrock fallback on by default (`READINESS_BEDROCK_ENABLED`); it needs:
- `bedrock:InvokeModel` on the instance's IAM role (uses InvokeModel, not
  the newer Converse API, specifically so this only needs the one
  long-established action name every IAM policy editor recognizes)
- **Model access granted in the Bedrock console** for the model in
  `READINESS_BEDROCK_MODEL_ID` (default
  `anthropic.claude-3-5-sonnet-20241022-v2:0`) — unlike Comprehend, a
  correct IAM policy alone isn't enough; Bedrock requires this separate,
  one-time per-account/region opt-in before the first call succeeds.

Or with Compose, which wires up the same mount and reads env vars from a
file instead of a long `-e` list:

```bash
cp .env.docker.example .env.docker   # fill in your values
docker compose up --build
```

`WEBAPP_SECRET_KEY` is worth setting explicitly for Docker even though the
app falls back to a persisted file — that file lives inside the
container's writable layer, so it won't survive `docker run` recreating
the container (only `docker start`/`stop` of the *same* container keeps
it).

## GitHub Actions: CI + auto-deploy to EC2

`.github/workflows/ci-cd.yml` runs `pytest` on every push/PR, and on a
successful push to `main` redeploys to EC2 instance `i-02dd6db7607e51d84`
over **AWS SSM Send-Command** — no SSH key, no open port 22 needed. The
instance pulls the latest code, rebuilds the image, and restarts the
container.

**One-time setup, before the first pipeline-driven deploy:**

1. **Give the instance SSM management + app permissions.** Attach the
   AWS-managed `AmazonSSMManagedInstanceCore` policy to the instance's IAM
   role (needed for it to receive SSM commands at all), alongside the
   S3/DynamoDB/SSM-parameter/Comprehend policy from the EC2 deployment
   guide above.

2. **Create an IAM user for GitHub Actions** with this policy (least
   privilege: only `ssm:SendCommand` on this one instance + the
   `AWS-RunShellScript` document, plus read-only status checks):
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "ssm:SendCommand",
         "Resource": [
           "arn:aws:ec2:us-east-1:853973692277:instance/i-02dd6db7607e51d84",
           "arn:aws:ssm:us-east-1::document/AWS-RunShellScript"
         ]
       },
       {
         "Effect": "Allow",
         "Action": ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
         "Resource": "*"
       }
     ]
   }
   ```
   Create an access key for that user, then add two **repository secrets**
   in GitHub (Settings → Secrets and variables → Actions):
   `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.

3. **Place a real `.env.docker` on the instance once**, at
   `/opt/ai-readiness-agent/.env.docker` (same format as
   `.env.docker.example`). The pipeline deliberately never creates or
   overwrites this file — secrets never pass through the SSM command
   itself, only a pre-existing file on disk does. If `/opt` needs `sudo`
   to create, run `sudo mkdir -p /opt/ai-readiness-agent && sudo chown
   $USER /opt/ai-readiness-agent` first.

After that, every push to `main` that passes tests redeploys automatically.
Check progress under the repo's **Actions** tab; the deploy step prints the
remote script's stdout/stderr either way.

## Running the tests

```bash
PYTHONPATH=src pytest
```

16 tests cover the profiler (null/type inference, duplicate detection, PII
heuristics), the readiness engine (scoring, use-case weighting, threshold
mapping), the secure channel (HMAC signing/verification, agent
authentication), and — most importantly — a compliance test that asserts
raw record values never make it into the payload that crosses the Secure
Result Channel.

## Architecture

```
src/ai_readiness_agent/
  config.py              # env-driven config; mock-mode by default
  agent.py               # orchestrator: ingest -> profile -> score -> deliver
  cli.py                 # `ai-readiness run` entry point

  ingestion/
    base.py               # DataSourceAdapter contract (SourceBatch/SourceRecord)
    s3_adapter.py          # S3: boto3 in real mode, mock_data/s3/* otherwise
    rds_adapter.py         # RDS: SQLAlchemy in real mode, mock_data/rds_customers.csv otherwise
    documents_adapter.py   # Documents: local folder scan (pluggable PDF/DOCX extraction point)

  profiling/
    models.py              # DataProfile / SourceProfile / FieldProfile
    pii.py                 # regex-based PII heuristics (email/phone/ssn/credit_card)
    profiler.py            # SourceBatch -> SourceProfile (nulls, types, dupes, PII, freshness)

  engine/
    rules.py               # scoring thresholds + per-use-case dimension weights
    readiness_engine.py    # DataProfile -> AssessmentResult (score, findings, remediation)

  assessment/
    models.py               # AssessmentResult (full, local) + ControlPlanePayload (minimal, transmitted)

  channel/
    secure_channel.py       # HMAC-signs + sends ControlPlanePayload (HTTPS or local outbox)

webapp/                    # optional local demo UI (Flask) — see "Web app" below
  app.py
  templates/                # login.html, home.html, result.html
  static/style.css
```

### Ingestion adapters

Each adapter implements `DataSourceAdapter.fetch() -> list[SourceBatch]`.
Real and mock code paths live side by side in the same file so switching a
customer over to real AWS is just flipping `READINESS_USE_MOCK_AWS=false`
and setting credentials/connection info — no code changes:

| Adapter | Real mode | Mock mode |
|---|---|---|
| `S3Adapter` | `boto3` list + get objects (`.json`/`.jsonl`/`.csv`) under `bucket/prefix` | reads `mock_data/s3/*` |
| `RDSAdapter` | `SQLAlchemy` against PostgreSQL, MySQL, MariaDB, Oracle, or SQL Server (every engine Amazon RDS offers) | reads `mock_data/rds_customers.csv` |
| `DocumentsAdapter` | scans a local/EFS/mounted folder | same code path — point it at `mock_data/documents` |

One S3 bucket is split into **one batch per object**, not one batch for
the whole bucket — a bucket routinely holds files with unrelated schemas
(`orders.json` vs `support_tickets.csv`), and merging them would produce
misleading null-rate/type stats.

`DocumentsAdapter` does not do real PDF/DOCX text extraction — it's a
named extension point (`_extract_text_sample`) so a production deployment
can drop in `pdfminer.six` / `python-docx` without touching anything else.

### Data Profile

`profiling/profiler.py` turns each `SourceBatch` into a `SourceProfile`:
per-field null rate, inferred type (`string`/`numeric`/`boolean`/`date`),
distinct-value counts, a duplicate-record rate (via a canonical JSON
fingerprint of each record), a lightweight PII scan (regex heuristics for
email/phone/SSN/credit-card patterns — **not** a substitute for a real DLP
tool like AWS Macie, just a fast first pass), and freshness (age of the
newest record).

### Readiness Engine

`engine/readiness_engine.py` scores five dimensions — completeness,
uniqueness, volume, freshness, privacy_risk — and combines them into an
overall 0-100 score, weighted by the selected **use case**
(`engine/rules.py::USE_CASE_DIMENSION_WEIGHTS`). A customer-support agent
that touches PII weights `privacy_risk` at 0.30; a sales-forecasting model
weights `volume` at 0.30 instead. Unknown use cases fall back to a
balanced default.

It also produces:
- **findings** — one per issue detected (sparse field, duplicate rate,
  stale source, PII detected, ingestion error), each with a severity,
  type, title, and impact description.
- **remediation** — one action per underperforming dimension, with a
  rough effort estimate and a `projected_score_delta`.
- **projected_score** — `overall_score` plus the sum of projected deltas
  from the suggested remediations (capped at 100), matching the
  `projected_score` field in the Control Plane's result contract.

### Assessment Result contract

`assessment/models.py::ControlPlanePayload` matches the exact shape from
section 9 of Member 3's spec:

```json
{
  "assessment_id": "...",
  "environment_id": "...",
  "use_case": "customer_support_agent",
  "status": "COMPLETED",
  "score": 77,
  "readiness_status": "READY",
  "dimensions": { "completeness": {"score": 96, "weight": 0.25, "summary": "..."}, ... },
  "findings": [ {"finding_id": "...", "severity": "...", "type": "...", "title": "...", "impact": "..."} ],
  "remediation": [ {"action_id": "...", "action": "...", "effort": "...", "projected_score_delta": 8.5} ],
  "projected_score": 92
}
```

## The local-data boundary

This is the most important design decision in the repo, so it's enforced
structurally rather than left as a convention to remember:

- `AssessmentResult` (the full internal model) embeds the entire
  `DataProfile`, which can contain **sample field values** pulled straight
  from S3/RDS/documents (see `FieldProfile.sample_values`). This is useful
  for the customer's own audit trail but must never leave their
  environment.
- `AIReadinessAgent.run()` writes that full result to
  `local_audit/<assessment_id>.json` — a directory that lives inside the
  customer's own environment and is never transmitted anywhere.
- `SecureResultChannel.send()` only accepts a `ControlPlanePayload`
  (enforced by its type signature) — it has no way to accidentally
  transmit a full `AssessmentResult`. The only way to get a
  `ControlPlanePayload` is `AssessmentResult.to_control_plane_payload()`,
  which builds it from aggregate dimension scores, finding
  titles/descriptions, and remediation actions — never from raw field
  values.
- `tests/test_no_raw_data_leak.py` asserts this directly: it seeds a
  profile with a distinctive fake SSN/email/name, confirms they *do* show
  up in the full local result, and confirms they do *not* show up
  anywhere in the transmitted payload.

## Secure Result Channel / agent authentication

`channel/secure_channel.py` HMAC-SHA256-signs the canonical JSON of the
`ControlPlanePayload` with a shared secret, and tags the envelope with
`agent_id` (`READINESS_AGENT_ID` env var) so the Control Plane can reject
submissions from clients it doesn't recognize — this is the MVP-level
"Agent authentication must prevent arbitrary clients from submitting
results" requirement from the spec (section 15). The envelope shape:

```json
{
  "payload": { ...ControlPlanePayload... },
  "signature": "hex hmac-sha256",
  "signature_algorithm": "HMAC-SHA256",
  "agent_id": "agent-local-dev",
  "assessment_id": "..."
}
```

`SecureResultChannel.verify(envelope, shared_secret)` is provided as a
reference implementation Member 3 can port to their backend's language to
validate an incoming envelope before trusting it.

For a real deployment, swap the HMAC step for SigV4 signing (if the
Control Plane sits behind an AWS-native auth boundary) or rely on mTLS at
the transport layer (if it's a private VPC link) — the call site
(`SecureResultChannel.send`) is the only place that would need to change.

## Configuration

Everything is environment-variable driven (see `config.py` for the full
list and defaults) so this drops into a container/Lambda/ECS task with
real credentials with no code changes:

| Variable | Purpose |
|---|---|
| `READINESS_USE_MOCK_AWS` | `true` (default) reads `mock_data/`; `false` uses real S3/RDS |
| `READINESS_S3_BUCKET`, `READINESS_S3_PREFIX`, `AWS_REGION` | S3 source config |
| `READINESS_RDS_ENGINE` | `postgresql` (default), `mysql`, `mariadb`, `oracle`, or `mssql` |
| `READINESS_RDS_HOST/PORT/DATABASE/TABLE/USER/PASSWORD` | RDS source config |
| `READINESS_DOCUMENTS_DIR` | folder to scan for documents |
| `ANTHROPIC_API_KEY`, `READINESS_LLM_MODEL` | direct-Anthropic content-analysis engine (tried first) |
| `READINESS_BEDROCK_ENABLED`, `READINESS_BEDROCK_MODEL_ID`, `AWS_BEDROCK_REGION` | Amazon Bedrock content-analysis fallback (tried if Anthropic fails) |
| `READINESS_ENVIRONMENT_ID`, `READINESS_USE_CASE` | defaults for standalone runs |
| `READINESS_AGENT_ID` | agent identity sent with every submission |
| `CONTROL_PLANE_URL`, `CONTROL_PLANE_SHARED_SECRET`, `CONTROL_PLANE_TIMEOUT`, `CONTROL_PLANE_MAX_RETRIES` | Secure Result Channel target |

## What's intentionally out of scope here

Per the explicit boundaries in Member 3's spec, this repo owns data
discovery/profiling, PII detection, readiness scoring, severity decisions,
and remediation business logic — and deliberately does **not** implement
any of: the `/assessment` REST API, the assessment state machine
(QUEUED/SCANNING/ANALYZING/COMPLETED/FAILED), persistence (DynamoDB or
otherwise), or the UI. Those belong to Member 3 (backend/control plane)
and Member 4 (UI) respectively.
