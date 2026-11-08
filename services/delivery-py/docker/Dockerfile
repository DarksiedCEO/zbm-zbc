# ZBM delivery-py service image (DEPT28_SPEC C.7.5). Built from a digest-pinned python:3.12-slim: the digest is a
# REQUIRED build argument (no default — a build without it fails), because this build box cannot reach Docker Hub to
# resolve one (ADR 0011). Record the digest used in ADR 0011 at first build.
#   docker build -f docker/Dockerfile --build-arg BASE_DIGEST=<64 hex> -t registry.zbm.internal/zbm/delivery-py .
# No Lark, no npm, no gateway, no frontend: the venv holds exactly uv.lock's runtime set (uv sync --frozen --no-dev).
ARG BASE_DIGEST
FROM python:3.12-slim@sha256:${BASE_DIGEST}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
# Supply-chain hardening (2026-11-08): apt is pinned to a dated snapshot.debian.org archive of the base's own Debian
# release, so `apt-get install` resolves to exactly one version of each package on every build (the live mirrors
# move daily). Packages stay signed by the Debian archive key the base image ships. Moving the date is a reviewed
# change; the installed versions are written to /usr/share/zbm/apt-packages.txt for the image record.
ARG DEBIAN_SNAPSHOT=20261101T000000Z
RUN echo "${DEBIAN_SNAPSHOT}" | grep -Eq '^[0-9]{8}T[0-9]{6}Z$' \
    && sed -Ei -e "s#https?://deb\.debian\.org/debian-security#https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}#" \
        -e "s#https?://deb\.debian\.org/debian\$#https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}#" \
        /etc/apt/sources.list.d/debian.sources \
    && ! grep -q 'deb\.debian\.org' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && mkdir -p /usr/share/zbm && dpkg-query -W -f='${Package}=${Version}\n' git ca-certificates > /usr/share/zbm/apt-packages.txt \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 65532 zbm && useradd -u 65532 -g 65532 -m -d /srv/delivery zbm
# uv is pinned to the version the lock was produced with (spec 0.3: uv 0.8.17)
COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /usr/local/bin/uv
WORKDIR /srv/delivery
COPY --chown=65532:65532 pyproject.toml uv.lock requirements.txt ./
# (2026-11-08) `--no-group extensions` removed: pyproject.toml defines no `extensions` group and the pinned uv 0.8.17
# refuses the build with "Group `extensions` is not defined in the project's `dependency-groups` table". --no-dev
# already excludes the only group (dev).
RUN uv sync --frozen --no-dev --no-install-project
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
