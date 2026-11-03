# ledger-rust (the evidence ledger) — production image for Railway (docs/deploy/railway.md). Build context: the
# repository root.
#   docker build -f services/ledger-rust/Dockerfile -t zbm/ledger-rust .
#
# Build stage: rust:1.99.1-slim-bookworm — the current stable that CI's ledger-rust job installs (`rustup toolchain
# install stable`) — a release build from Cargo.lock (--locked). The OCI index digest matches successful CI
# and was rechecked against the registry on 2026-11-01. Crates come from Cargo.lock with checksums.
#
# Runtime stage: python:3.12-slim pinned by the digest ADR 0011 records (glibc newer than bookworm's, which the binary
# was linked against) — it carries /bin/sh and setpriv for deploy/docker/zbm-entrypoint.sh. Runs as uid/gid 10001.
#
# ONE replica, ONE volume: the log, its <log>.lock (single writer, flock) and <log>.head checkpoint live in the
# directory of LEDGER_LOG_PATH, on the volume. Bind: LEDGER_BIND_ADDR (127.0.0.1 unless set; [::] for every interface,
# brackets needed). LEDGER_CALLERS_FILE (optional) is written from the LEDGER_CALLERS variable by the entrypoint.
FROM rust:1.99.1-slim-bookworm@sha256:24707e70a4f1e23d9a7b25202ec43126d28a52294b96aad5c670df1fd1f1d400 AS build
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

# Dormant maintenance package; no enabled credentials or automatic activation.
COPY deploy/docker/maintenance/launcher.py deploy/docker/maintenance/backup_config.py deploy/docker/maintenance/app_env_names.json /opt/zbm-maintenance/
RUN chown -R 0:0 /opt/zbm-maintenance \
    && chmod 0755 /opt/zbm-maintenance \
    && chmod 0644 /opt/zbm-maintenance/launcher.py /opt/zbm-maintenance/backup_config.py /opt/zbm-maintenance/app_env_names.json \
    && /usr/local/bin/python3 -I -B -c 'import sys; assert sys.version_info[:2] == (3, 12); sys.path.insert(0, "/opt/zbm-maintenance"); import backup_config; assert len(backup_config.MEMBERS) == 5; compile(open("/opt/zbm-maintenance/launcher.py").read(), "launcher.py", "exec")' \
    && test -x /usr/bin/setpriv \
    && /usr/bin/setpriv --help > /tmp/zbm-setpriv-help \
    && for flag in reuid regid clear-groups no-new-privs bounding-set inh-caps ambient-caps; do grep -q -- "--$flag" /tmp/zbm-setpriv-help || exit 1; done \
    && rm /tmp/zbm-setpriv-help
# Explicit cold owner package; startup never downloads executables.
ARG TARGETARCH
COPY deploy/docker/maintenance/cold /opt/zbm-maintenance/cold
COPY deploy/docker/maintenance/cold_config.py deploy/docker/maintenance/application-prerequisites.json deploy/docker/maintenance/binary-manifest.json /opt/zbm-maintenance/
COPY deploy/docker/maintenance/vendor /opt/zbm-maintenance/bin
RUN test "$TARGETARCH" = amd64 && test "$(uname -m)" = x86_64 \
    && echo "20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639  /opt/zbm-maintenance/bin/restic" | sha256sum -c - \
    && chown -R 0:0 /opt/zbm-maintenance \
    && chmod -R go-w /opt/zbm-maintenance \
    && chmod 0755 /opt/zbm-maintenance/bin/restic
ENV BACKUP_LAUNCH_ENABLED=0
COPY --from=build /ledger-server /usr/local/bin/ledger-server
COPY --from=build /ledger-server /opt/zbm-maintenance/bin/ledger-server
ENV LEDGER_LOG_PATH=/data/ledger/ledger.jsonl
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/zbm-entrypoint", "--port-var", "LEDGER_PORT", "--state-file-var", "LEDGER_LOG_PATH", "--secret-file", "LEDGER_CALLERS_FILE", "--", "/usr/local/bin/ledger-server"]
