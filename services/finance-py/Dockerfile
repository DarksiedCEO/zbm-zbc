# finance-py (Finance 31) — production image for Railway (docs/deploy/railway.md).
# Build context: the repository root.
#   docker build -f services/finance-py/Dockerfile -t zbm/finance-py .
#
# Base: python:3.12-slim pinned by the image index digest ADR 0011 records (2026-10-02), the base delivery-py's images
# already build from and CI has built (Python 3.12 is one of the two versions every service's suite runs on).
# Dependencies: deploy/python/requirements.lock.txt — this service's requirements.txt pins without pytest, every
# transitive dependency pinned and installed with --require-hashes, wheels only.
# Runs as uid/gid 10001; the code is root-owned and read-only to it. deploy/docker/zbm-entrypoint.sh maps Railway's
# PORT onto the service's own port variable and prepares the data directory; every launch guard is the service's own
# (src/serve.py, src/launch_guard.py) and unchanged.
# State: FIN_DATA_DIR (the volume; one process per directory, flock). Secrets read from files
# (FIN_STRIPE_SECRET_KEY_FILE, FIN_STRIPE_WEBHOOK_SECRET_FILE) are written from Railway variables by the
# entrypoint. Bind: FIN_BIND_ADDR (127.0.0.1 unless set).
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
COPY services/finance-py/src /app/services/finance-py/src
COPY services/finance-py/seed /app/services/finance-py/seed

WORKDIR /app/services/finance-py/src
ENV FIN_DATA_DIR=/data/finance
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/zbm-entrypoint", "--port-var", "FIN_PORT", "--state-dir-var", "FIN_DATA_DIR", "--secret-file", "FIN_STRIPE_SECRET_KEY_FILE", "--secret-file", "FIN_STRIPE_WEBHOOK_SECRET_FILE", "--secret-file", "RESEND_API_KEY_FILE", "--secret-file", "FIN_CONTACT_HASH_KEY_FILE", "--", "/opt/venv/bin/python", "-m", "api"]
