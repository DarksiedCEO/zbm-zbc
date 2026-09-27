# ZBM fix-engine SANDBOX image (DEPT28_SPEC C.2, C.7.5): the per-service toolchains the engineer's tests need,
# installed from lockfiles at build time, run as uid 65532 with no network at run time (--network <internal>).
# The base digest is a REQUIRED build argument (see docker/Dockerfile for why). Record the resulting image digest
# in ADR 0011 and set DLV_SANDBOX_IMAGE=<registry>/zbm/dlv-sandbox@sha256:<digest>.
#   docker build -f docker/sandbox.Dockerfile --build-arg BASE_DIGEST=<64 hex> -t registry.zbm.internal/zbm/dlv-sandbox ../..
ARG BASE_DIGEST
FROM python:3.12-slim@sha256:${BASE_DIGEST}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl build-essential pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 65532 zbm && useradd -u 65532 -g 65532 -M zbm \
    && mkdir -p /mnt/user-data/workspace /mnt/skills && chown -R 65532:65532 /mnt/user-data
# python: the monorepo's pinned FastAPI stack (BUILD_CONTRACTS §0) so every *-py service's suite runs offline
COPY services/finance-py/requirements.txt /tmp/req-python.txt
RUN pip install --no-cache-dir -r /tmp/req-python.txt && rm /tmp/req-python.txt
# rust stable + go, for ledger-rust and orchestrator-go suites (cargo test --offline / go test)
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable \
    && ln -s /root/.cargo/bin/* /usr/local/bin/
ARG GO_VERSION=1.23.1
RUN curl -sSfL https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz | tar -C /usr/local -xz \
    && ln -s /usr/local/go/bin/go /usr/local/bin/go && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt
# the engineer runs as 65532; the root filesystem is mounted read-only at run time; /tmp is a tmpfs
USER 65532:65532
WORKDIR /mnt/user-data/workspace
CMD ["sleep", "infinity"]
