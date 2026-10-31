"""Launcher for the client acceptance page (ADR 0028): ``cd src && python3 -m api``.

uvicorn with h11, no access log (a request path holds the acceptance token), no ``Server`` header, no proxy-header
rewriting (the app reads X-Forwarded-For itself, PORTAL_TRUSTED_PROXY_HOPS from the end), bounded concurrency, a
short keep-alive and a bounded request head. Refuses to start on any configuration error (config.py).
"""

from __future__ import annotations

import logging
import sys

import config as config_mod
from app import create_app

HEAD_MAX = 16 * 1024


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    try:
        settings = config_mod.load()
    except RuntimeError as exc:
        print(f"RuntimeError: {exc}", file=sys.stderr, flush=True)
        return 1
    import uvicorn
    app = create_app(settings)
    uvicorn.run(app, host=settings.bind_addr, port=settings.port, http="h11", access_log=False, server_header=False,
                proxy_headers=False, forwarded_allow_ips="", limit_concurrency=64, timeout_keep_alive=5,
                h11_max_incomplete_event_size=HEAD_MAX, log_level="warning", lifespan="on")
    return 0


if __name__ == "__main__":
    sys.exit(main())
