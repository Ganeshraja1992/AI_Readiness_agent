# AI Readiness Agent — webapp image.
#
# This container runs the Flask webapp (webapp/app.py) behind gunicorn. It
# does NOT bundle AWS or Anthropic credentials — those must be supplied at
# `docker run` / compose time (see README.md's Docker section). The webapp
# always talks to real AWS (S3, RDS, SSM, DynamoDB, Comprehend), so a valid
# AWS credential source is required for it to be useful, not just to build.

FROM python:3.11-slim AS base

# Faster, quieter Python; unbuffered so `docker logs` shows output live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so this layer is cached across code-only changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App code.
COPY src/ ./src/
COPY webapp/ ./webapp/
COPY mock_data/ ./mock_data/
COPY pyproject.toml ./

# Run as a non-root user. AWS credentials get mounted into this user's
# home directory at run time (see README) rather than baked into the image.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/local_audit /app/outbox \
    && chown -R appuser:appuser /app
USER appuser
ENV HOME=/home/appuser

EXPOSE 5000

# 120s worker timeout: real S3/RDS/Comprehend/Anthropic calls in a single
# request can legitimately take longer than gunicorn's 30s default.
#
# Exactly 1 worker: the background scan-job tracking (webapp/app.py's
# _SCAN_JOBS) is a plain in-memory dict, not shared across processes -- a
# second worker would mean the page polling for a job's progress could hit
# a different process than the one running it and see "unknown job". Fine
# for this single-user demo app; would need a shared store (e.g. the same
# DynamoDB table already used for audit results) to scale past one worker.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--timeout", "120", "webapp.app:app"]
