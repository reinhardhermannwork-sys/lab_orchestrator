# Lab orchestrator image (M8, architecture doc §17).
#
# One process, one uvicorn worker: the janitor, in-process provisioning
# tasks, and SQLite's locking all assume a single process. Don't add
# --workers. State lives in /data (mount a volume); config and secrets are
# mounted read-only at run time, never baked in beyond the default
# machines.yaml.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && rm -rf src build

# Default machine definitions; overridden by a read-only mount in compose.
COPY config ./config

# Fixed uid so host-side secret files can be made readable to it
# (see deploy/.env.example).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin laborch \
    && mkdir /data && chown laborch /data
USER laborch

ENV LAB_ORCH_DB_PATH=/data/orchestrator.db \
    LAB_ORCH_MACHINES_CONFIG_PATH=/app/config/machines.yaml

EXPOSE 8000

# No curl in the slim image; urlopen raises (non-zero exit) on failure.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["uvicorn", "lab_orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
