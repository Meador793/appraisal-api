# syntax=docker/dockerfile:1
#
# Appraisal Adjustment API
#
# Build for ECS Fargate:
#     docker build --platform linux/amd64 -t appraisal-api:v1 .
#
# The --platform flag is not optional on an Apple Silicon Mac. Without it
# docker build produces an arm64 image, Fargate defaults to x86_64, and the
# task dies at runtime with "exec format error" -- a confusing failure,
# because the build and the push both succeed. The alternative is setting
# runtimePlatform.cpuArchitecture to ARM64 in the task definition, which is
# also slightly cheaper. Either is fine; pick one and be consistent.

FROM python:3.12-slim AS base

# PYTHONUNBUFFERED matters on ECS specifically: without it, stdout is block
# buffered and your log lines sit in a buffer instead of reaching CloudWatch,
# so a container that crashes appears to have logged nothing at all.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp1 is XGBoost's OpenMP runtime. It is not in python:*-slim, and
# without it `import xgboost` fails at container start with a linker error.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements copied first so the dependency layer caches independently of
# the application code. Editing main.py then rebuilds in seconds, not minutes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Non-root. Fargate will run whatever user the image specifies; running as
# root is a finding in every security review and costs nothing to avoid.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/artifacts \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Container-level health check. ECS can use this OR a task-definition
# healthCheck block; the task definition version is the one the scheduler
# acts on, so keep them in agreement.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# One worker per task. Scale by running more tasks, not more workers inside
# one task: each worker loads its own copy of the model into memory, and
# 0.5 GB does not hold several.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
