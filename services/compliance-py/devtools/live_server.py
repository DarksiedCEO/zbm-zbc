"""
DEVTOOLS ONLY — never the production entrypoint (that is ``cd src && python3 -m api``).

Runs compliance-py exactly as ``api.main()`` does (same settings from the
environment, same hardened launcher, real ledger), except that the Change
Watcher's fetch port is a FIXTURE fetcher: a page is served from
``$COMPLIANCE_FIXTURE_DIR/<sha256(url)>.bin`` when that file exists, and every
other URL fails (FetchFailed). This lets a live run show fetch -> diff ->
proposal -> Andre approval deterministically and without touching the
network. Everything else (V&I, Finance, Legal, sanctions, accessibility) is
the fail-closed stand-in.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import config as config_mod  # noqa: E402
import serve  # noqa: E402
from clock import SystemClock, iso  # noqa: E402
from fetcher import FetchFailed, FetchResult  # noqa: E402
from service import Ports  # noqa: E402


class FixtureFetcher:
    def __init__(self, directory: str, clock):
        self.dir, self.clock = Path(directory), clock

    def fetch(self, url: str) -> FetchResult:
        p = self.dir / (hashlib.sha256(url.encode()).hexdigest() + ".bin")
        if not p.exists():
            raise FetchFailed("no fixture for this URL (devtools fixture fetcher)")
        return FetchResult(url, 200, p.read_bytes(), iso(self.clock.now()))


def _import_api_inertly():
    """``import api`` builds its module-level app from the environment; do that
    with no ledger and no data dir so it records and writes nothing."""
    saved = {k: os.environ.pop(k, None) for k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN", "COMPLIANCE_DATA_DIR")}
    try:
        import api
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    return api


def main() -> None:
    os.environ.setdefault("COMPLIANCE_WATCHER_ENABLED", "1")
    api = _import_api_inertly()
    settings = config_mod.load()
    clock = SystemClock()
    svc = api.build_service(settings, clock, Ports(fetcher=FixtureFetcher(os.environ["COMPLIANCE_FIXTURE_DIR"], clock)))
    app = api.create_app(svc, settings)
    serve.run(app, host=os.environ.get("COMPLIANCE_BIND_ADDR", "127.0.0.1"), port=int(os.environ["COMPLIANCE_PORT"]))


if __name__ == "__main__":
    main()
