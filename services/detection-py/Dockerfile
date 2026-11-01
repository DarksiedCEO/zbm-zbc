# detection-py (Revenue Recovery detection agents) — production image for Railway (docs/deploy/railway.md).
# Build context: the repository root.
#   docker build -f services/detection-py/Dockerfile -t zbm/detection-py .
#
# Base: python:3.12-slim pinned by the image index digest ADR 0011 records (2026-10-02), the base delivery-py's images
# already build from and CI has built (Python 3.12 is one of the two versions every service's suite runs on).
# Dependencies: deploy/python/requirements.lock.txt — this service's requirements.txt pins without pytest, every
# transitive dependency pinned and installed with --require-hashes, wheels only.
# Runs as uid/gid 10001; the code is root-owned and read-only to it. deploy/docker/zbm-entrypoint.sh maps Railway's
# PORT onto the service's own port variable and prepares the data directory; every launch guard is the service's own
# (src/serve.py, src/launch_guard.py) and unchanged.
# serve.py takes --host/--port arguments, not variables: DETECTION_BIND_ADDR and DETECTION_PORT are this
# image's names for them (serve.py's own defaults: 127.0.0.1 and 8000). It keeps no state (no volume): store
# snapshots and the per-day sync counts live in process memory (ADR 0020), the evidence is on the ledger.
# Revenue Recovery store connector (ADR 0020; docs/deploy/railway.md "Revenue Recovery: connecting a real store"):
# four --secret-file settings, every one optional (unset value variable = no file = that part stays off, fail closed):
#   DETECTION_LEDGER_TOKEN_FILE   detection's own ledger caller token (record-first: no ledger, no store read)
#   RR_STORE_CONNECTIONS_FILE     the approved connections JSON (secret REFERENCES only), from RR_STORE_CONNECTIONS
#   RR_STORE_SECRET_1_FILE,       credential slots the connections JSON points at by their fixed paths,
#   RR_STORE_SECRET_2_FILE        /run/zbm-secrets/rr-store-secret-1-file and /run/zbm-secrets/rr-store-secret-2-file
# Writing them needs RAILWAY_RUN_UID=0 (zbm-entrypoint starts as root, writes 0600 files owned by uid 10001, then runs
# the service as 10001 through setpriv). Started as 10001 with none of the values set, nothing changes.
# The fixture pool (ADR 0001) is still copied to /app/fixtures for the fixture tenant (refused on Railway by the
# orchestrator, FIXTURE_SCAN_DISABLED).
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 zbm \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin zbm \
    && mkdir -p /data /run/zbm-secrets \
    && chown 10001:10001 /data /run/zbm-secrets \
    && chmod 0700 /data /run/zbm-secrets

COPY deploy/python/requirements.lock.txt /tmp/requirements.lock.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --require-hashes --no-deps --only-binary=:all: -r /tmp/requirements.lock.txt \
    && rm /tmp/requirements.lock.txt

COPY deploy/docker/zbm-entrypoint.sh /usr/local/bin/zbm-entrypoint
RUN chmod 0755 /usr/local/bin/zbm-entrypoint
COPY services/detection-py/src /app/services/detection-py/src
COPY fixtures/*.json /app/fixtures/

WORKDIR /app/services/detection-py/src
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/zbm-entrypoint", "--port-var", "DETECTION_PORT", "--secret-file", "DETECTION_LEDGER_TOKEN_FILE", "--secret-file", "RR_STORE_CONNECTIONS_FILE", "--secret-file", "RR_STORE_SECRET_1_FILE", "--secret-file", "RR_STORE_SECRET_2_FILE", "--", "/bin/sh", "-c", "exec /opt/venv/bin/python serve.py --host \"${DETECTION_BIND_ADDR:-127.0.0.1}\" --port \"${DETECTION_PORT:-8000}\""]
