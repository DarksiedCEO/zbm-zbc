# fin-cron: the Railway cron service for Finance (ADR 0026). Runs deploy/scheduler/fin_cron.py once per cron tick and
# exits. Standard library only (no pip install); the same pinned base as the Python services; never root.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 zbm \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin zbm

COPY deploy/scheduler/fin_cron.py /app/fin_cron.py

WORKDIR /app
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/python", "-I", "/app/fin_cron.py"]
