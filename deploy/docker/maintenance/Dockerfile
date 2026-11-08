# Optional coordinator/observer/trigger image. No service is provisioned by this file.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
COPY deploy/python/requirements.lock.txt /tmp/requirements.lock.txt
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --require-hashes --no-deps --only-binary=:all: -r /tmp/requirements.lock.txt && rm /tmp/requirements.lock.txt
COPY deploy/docker/maintenance/cold /opt/zbm-maintenance/cold
COPY deploy/docker/maintenance/admin_launcher.py /opt/zbm-maintenance/admin_launcher.py
COPY deploy/docker/maintenance/vendor/restic /opt/zbm-maintenance/bin/restic
ARG TARGETARCH
RUN test "$TARGETARCH" = amd64 && test "$(uname -m)" = x86_64 && echo "20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639  /opt/zbm-maintenance/bin/restic" | sha256sum -c - && chmod -R go-w /opt/zbm-maintenance && chmod 0755 /opt/zbm-maintenance/bin/restic
ENV BACKUP_LAUNCH_ENABLED=0 PYTHONDONTWRITEBYTECODE=1
# Explicit root build profile is a separate rollout approval, never an env toggle.
USER 10001:10001
ENTRYPOINT ["/opt/venv/bin/python", "-I", "-B", "/opt/zbm-maintenance/admin_launcher.py"]
CMD ["observer"]
