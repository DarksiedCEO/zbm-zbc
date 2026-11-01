"""
The IPv6 wildcard bound dual-stack, for the launchers of the four Railway Python images (detection-py, finance-py,
legal-py, seo-py; the same file in each). Standard library only.

The Railway images bind ``::`` (.railway/railway.ts). A launcher used to hand that to ``uvicorn.run``, which binds
through asyncio's ``create_server``; asyncio sets IPV6_V6ONLY on every IPv6 socket it binds itself, so the service
listened on IPv6 only and every IPv4 connection was refused: ``ConnectionResetError`` through Docker's IPv4 port
mapping for all four images (CI railway-images, golive-combined), and on Railway an IPv4 health check or IPv4
private-network peer fails the same way, while the Go and Rust services, which bind ``[::]`` dual-stack, worked.

``serve()`` binds the IPv6 wildcard itself with IPV6_V6ONLY cleared (one socket for IPv4 and IPv6, as Go and Rust
do) and hands uvicorn that socket; any other host is passed to ``uvicorn.run`` exactly as before. Binding is the only
change: the protocol, limits and every guard are the uvicorn configuration passed in. This module only binds a
listening socket; it opens no connection.

Not part of launch_guard.py (shared by every Python service, hygiene L4): delivery-py, which also carries that file,
allows no ``socket`` import outside its egress module (guardrail G13), and it is not a Railway image.
"""

from __future__ import annotations

import socket
import sys

IPV6_WILDCARDS = ("::", "[::]")
STARTUP_FAILURE = 3          # uvicorn's own exit status when the server did not start
BIND_FAILURE = 1             # uvicorn's exit status when its bind fails (it logs the error and exits 1)


def dual_stack_socket(host: str, port: int):
    """A bound (not yet listening) TCP socket on the IPv6 wildcard with IPV6_V6ONLY cleared, so it accepts IPv4
    (as v4-mapped addresses) and IPv6; ``None`` for any other host (uvicorn binds those itself, unchanged).
    SO_REUSEADDR as asyncio sets it. Raises OSError if the bind fails (the caller exits like uvicorn does)."""
    if host.strip() not in IPV6_WILDCARDS:
        return None
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
    except BaseException:
        sock.close()
        raise
    return sock


def serve(uvicorn_mod, app, host: str, port: int, name: str, **kwargs) -> None:
    """Serve ``app`` with ``uvicorn_mod`` (the launcher's uvicorn) and ``kwargs`` (its configuration). The IPv6
    wildcard is bound dual-stack here; every other host goes to ``uvicorn_mod.run`` as before. ``name`` is the
    service, for the one line saying where it listens."""
    try:
        sock = dual_stack_socket(host, port)
    except OSError as exc:
        print(f"{name}: cannot bind [::]:{port}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(BIND_FAILURE) from None
    if sock is None:
        uvicorn_mod.run(app, host=host, port=port, **kwargs)
        return
    try:
        config = uvicorn_mod.Config(app, host=host, port=port, **kwargs)
        server = uvicorn_mod.Server(config=config)
        print(f"{name}: listening on [::]:{port} (dual-stack: IPv4 and IPv6)", file=sys.stderr, flush=True)
        server.run(sockets=[sock])
    finally:
        sock.close()
    if not server.started:
        raise SystemExit(STARTUP_FAILURE)
