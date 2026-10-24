# ledger-rust (the evidence ledger) — production image for Railway (docs/deploy/railway.md). Build context: the
# repository root.
#   docker build -f services/ledger-rust/Dockerfile -t zbm/ledger-rust .
#
# Build stage: rust:1.99.1-slim-bookworm — the current stable that CI's ledger-rust job installs (`rustup toolchain
# install stable`) — a release build of the server binary from Cargo.lock (--locked). OPEN ITEM: pinned by exact tag only — the build box this was written on cannot reach Docker Hub to resolve a digest; pin it by
# digest (docs/deploy/railway.md, "Open items"). Crates come from crates.io as Cargo.lock pins them (checksummed).
#
# Runtime stage: python:3.12-slim pinned by the digest ADR 0011 records (glibc newer than bookworm's, which the binary
# was linked against) — it carries /bin/sh and setpriv for deploy/docker/zbm-entrypoint.sh. Runs as uid/gid 10001.
#
# ONE replica, ONE volume: the log, its <log>.lock (single writer, flock) and <log>.head checkpoint live in the
# directory of LEDGER_LOG_PATH, on the volume. Bind: LEDGER_BIND_ADDR (127.0.0.1 unless set; [::] for every interface,
# brackets needed). LEDGER_CALLERS_FILE (optional) is written from the LEDGER_CALLERS variable by the entrypoint.
FROM rust:1.99.1-slim-bookworm AS build
WORKDIR /src
COPY services/ledger-rust/Cargo.toml services/ledger-rust/Cargo.lock ./
COPY services/ledger-rust/src ./src
RUN cargo build --release --locked --bin server \
    && cp target/release/server /ledger-server

FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
RUN groupadd --gid 10001 zbm \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin zbm \
    && mkdir -p /data /run/zbm-secrets \
    && chown 10001:10001 /data /run/zbm-secrets \
    && chmod 0700 /data /run/zbm-secrets
COPY deploy/docker/zbm-entrypoint.sh /usr/local/bin/zbm-entrypoint
RUN chmod 0755 /usr/local/bin/zbm-entrypoint
COPY --from=build /ledger-server /usr/local/bin/ledger-server
ENV LEDGER_LOG_PATH=/data/ledger/ledger.jsonl
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/zbm-entrypoint", "--port-var", "LEDGER_PORT", "--state-file-var", "LEDGER_LOG_PATH", "--secret-file", "LEDGER_CALLERS_FILE", "--", "/usr/local/bin/ledger-server"]
