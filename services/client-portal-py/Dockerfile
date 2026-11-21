# client-portal-py (the client acceptance page, ADR 0028) — production image for Railway (docs/deploy/railway.md).
# Build context: the repository root.
#   docker build -f services/client-portal-py/Dockerfile -t zbm/client-portal-py .
#
# Base: python:3.12-slim pinned by the image index digest ADR 0011 records (2026-10-02), the base every Python
# Railway image builds from.
# Dependencies: deploy/python/requirements.lock.txt — the shared hash lock (this service's requirements.txt pins are a
# subset of requirements.in), every transitive dependency pinned and installed with --require-hashes, wheels only.
# Runs as uid/gid 10001; the code is root-owned and read-only to it. deploy/docker/zbm-entrypoint.sh maps Railway's
# PORT onto PORTAL_PORT. No state, no volume, no secret file: the service keeps nothing (Legal keeps every record).
# Bind: PORTAL_BIND_ADDR.
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
COPY services/client-portal-py/src /app/services/client-portal-py/src

WORKDIR /app/services/client-portal-py/src
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/zbm-entrypoint", "--port-var", "PORTAL_PORT", "--", "/opt/venv/bin/python", "-m", "api"]
