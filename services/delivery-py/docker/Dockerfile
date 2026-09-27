# ZBM delivery-py service image (DEPT28_SPEC C.7.5). Built from a digest-pinned python:3.12-slim: the digest is a
# REQUIRED build argument (no default — a build without it fails), because this build box cannot reach Docker Hub to
# resolve one (ADR 0011). Record the digest used in ADR 0011 at first build.
#   docker build -f docker/Dockerfile --build-arg BASE_DIGEST=<64 hex> -t registry.zbm.internal/zbm/delivery-py .
# No Lark, no npm, no gateway, no frontend: the venv holds exactly uv.lock's runtime set (uv sync --frozen --no-dev).
ARG BASE_DIGEST
FROM python:3.12-slim@sha256:${BASE_DIGEST}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 65532 zbm && useradd -u 65532 -g 65532 -m -d /srv/delivery zbm
# uv is pinned to the version the lock was produced with (spec 0.3: uv 0.8.17)
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
WORKDIR /srv/delivery
COPY --chown=65532:65532 pyproject.toml uv.lock requirements.txt ./
RUN uv sync --frozen --no-dev --no-group extensions --no-install-project
COPY --chown=65532:65532 src ./src
COPY --chown=65532:65532 config ./config
COPY --chown=65532:65532 seed ./seed
COPY --chown=65532:65532 prompts ./prompts
COPY --chown=65532:65532 skills ./skills
COPY --chown=65532:65532 devtools ./devtools
# the licence gate runs at build too: an image whose site-packages fails it is never produced
RUN /srv/delivery/.venv/bin/python devtools/licence_gate.py
USER 65532:65532
ENV DLV_BIND_ADDR=127.0.0.1 DLV_PORT=8430
WORKDIR /srv/delivery/src
ENTRYPOINT ["/srv/delivery/.venv/bin/python", "-m", "zbm_delivery.api"]
