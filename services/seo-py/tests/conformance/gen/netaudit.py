"""CB-13: a PROCESS-LEVEL network audit hook (``sys.addaudithook``: installed once, cannot be removed) recording every
socket connect / name lookup that targets anything but the loopback address. Farrah's conformance fixtures fail any
test during which ``ESCAPES`` grew."""

from __future__ import annotations

import sys

ESCAPES: list = []
_LOCAL = ("127.0.0.1", "localhost", "::1")
_EVENTS = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex")


def _hook(event, args):
    if event in _EVENTS:
        target = args[1] if event == "socket.connect" else args[0]
        host = target[0] if isinstance(target, tuple) else target
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if host not in _LOCAL:
            ESCAPES.append(event)


def install() -> None:
    if not getattr(sys, "_farrah_audit_hook", False):
        sys.addaudithook(_hook)
        sys._farrah_audit_hook = True
