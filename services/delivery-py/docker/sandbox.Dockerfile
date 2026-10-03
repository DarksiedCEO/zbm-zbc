# ZBM fix-engine SANDBOX image (DEPT28_SPEC C.2, C.7.5): the per-service toolchains the engineer's tests need,
# installed from lockfiles at build time, run as uid 65532 with NO network at run time (--network none, R4).
# The base digest is a REQUIRED build argument (see docker/Dockerfile for why). Record the resulting image digest
# in ADR 0011 and set DLV_SANDBOX_IMAGE=<registry>/zbm/dlv-sandbox@sha256:<digest>.
#   docker build -f docker/sandbox.Dockerfile --build-arg BASE_DIGEST=<64 hex> --build-arg NODE_SHA256=<64 hex> \
#     --build-arg RUSTUP_INIT_SHA256=<64 hex> --build-arg GO_SHA256=<64 hex> -t registry.zbm.internal/zbm/dlv-sandbox ../..
# (the values: ADR 0011 "Pinned hashes"; .github/workflows/ci.yml delivery-docker-live passes them)
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
# rust stable + go + node, for the ledger-rust / orchestrator-go / node suites (cargo test --locked --offline,
# go test -json -race, node --test). The engineer runs as uid 65532 with HOME=/mnt/user-data/workspace, so the
# rustup proxy needs RUSTUP_HOME/CARGO_HOME outside HOME and world-readable (ADR 0011 toolchain amendment; the
# read-only root's interaction with cargo's $CARGO_HOME/.package-cache lock is not proven on this box).
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
# Fix wave 26b (W26-3b): the rustup installer binary is pinned by version and sha256 (a REQUIRED build argument, value
# recorded in ADR 0011), never `curl https://sh.rustup.rs | sh`; the toolchain is a concrete version, never the
# floating `stable` (rustup checks each component against that release's manifest hashes).
ARG RUSTUP_VERSION=1.29.1
ARG RUSTUP_INIT_SHA256
ARG RUST_TOOLCHAIN=1.99.0
RUN test -n "${RUSTUP_INIT_SHA256}" \
    && curl -sSfL -o /tmp/rustup-init https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init \
    && echo "${RUSTUP_INIT_SHA256}  /tmp/rustup-init" | sha256sum -c - \
    && chmod +x /tmp/rustup-init \
    && /tmp/rustup-init -y --profile minimal --default-toolchain "${RUST_TOOLCHAIN}" --no-modify-path \
    && rm /tmp/rustup-init \
    && ln -s /opt/cargo/bin/* /usr/local/bin/ && chmod -R a+rX /opt/rustup /opt/cargo
# Go: the tarball's sha256 (from https://go.dev/dl/?mode=json) is a REQUIRED build argument, like NODE_SHA256.
ARG GO_VERSION=1.23.1
ARG GO_SHA256
RUN test -n "${GO_SHA256}" \
    && curl -sSfL -o /tmp/go.tar.gz https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz \
    && echo "${GO_SHA256}  /tmp/go.tar.gz" | sha256sum -c - \
    && tar -C /usr/local -xzf /tmp/go.tar.gz && rm /tmp/go.tar.gz \
    && ln -s /usr/local/go/bin/go /usr/local/bin/go && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt
# node 22 (the dashboard's tests need Node 22.18+ type stripping): the tarball's SHA-256 from
# https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt is a REQUIRED build argument, like BASE_DIGEST.
ARG NODE_VERSION=22.22.2
ARG NODE_SHA256
RUN test -n "${NODE_SHA256}" \
    && curl -sSfL -o /tmp/node.tar.xz https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && mkdir -p /opt/node && tar -C /opt/node --strip-components=1 -xJf /tmp/node.tar.xz && rm /tmp/node.tar.xz \
    && ln -s /opt/node/bin/node /usr/local/bin/node && ln -s /opt/node/bin/npm /usr/local/bin/npm
# Round 18 R4: the box has no network (--network none) AND no egress client — curl is purged once the toolchains are
# installed and neither curl nor wget may remain (tests/test_live_docker.py asserts `command -v curl wget` fails
# inside the container). Fix wave 26b (CI #3): wget is not installed, and `apt-get purge curl wget` failed with
# "Unable to locate package wget" (exit 100, the package lists are already removed); the check below still proves
# wget absent.
RUN apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
    && ! command -v curl && ! command -v wget
# the engineer runs as 65532; the root filesystem is mounted read-only at run time; /tmp is a tmpfs
USER 65532:65532
WORKDIR /mnt/user-data/workspace
CMD ["sleep", "infinity"]
