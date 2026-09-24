"""
Entry point: `python3 serve.py` (from services/creative-py/src).

Binds 127.0.0.1 by default — never 0.0.0.0 by default (house rule; same
class of bug fixed in ledger-rust and orchestrator-go). Override with
CREATIVE_BIND_ADDR; port from CREATIVE_PORT (default 8300).
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("CREATIVE_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("CREATIVE_PORT", "8300"))
    uvicorn.run("api:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
