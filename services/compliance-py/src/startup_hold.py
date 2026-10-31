"""
Start-up hold (red team Oct 30-31 2026, CH-5 / CH-1; fix wave A, ADR 0025). Shared byte-for-byte by finance-py,
legal-py and compliance-py.

Before: when the evidence ledger was unreachable at start (an outage spanning a project-wide redeploy or a host move)
the service raised at import and the process exited 1. Railway's ON_FAILURE policy restarts a crashed service a
bounded number of times and then leaves it Crashed, so a ledger outage longer than that budget left Finance, Legal
and Compliance down until someone redeployed them by hand. A log that diverged from the ledger also exited, with
the reason only in the deploy log.

Now ``supervise`` builds the real app once. If that fails for a reason that can pass (the ledger cannot be read or
written: ``transient``) or for one that needs Andre (real divergence: ``held``), the process does NOT exit: it
serves a holding app and keeps retrying the build in a background thread, with exponential backoff (1 s doubling to
60 s) for a transient cause and every ``held_retry_s`` (5 min) for divergence (which only changes when Andre acts).

Why hold-and-retry and not a degraded read-only service: nothing is ever served from a local log that has not been
verified against the ledger. The holding app answers ``GET /health`` with 503 and the reason (``starting`` /
``refused``), and every other request with 503 + Retry-After (Stripe redelivers, callers retry). Fail closed, no
restart budget consumed, and the heartbeat monitor sees why. Any other start-up error (bad configuration, seed hash,
data directory busy) still raises: those never fix themselves and must stop the deploy.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, Callable, Optional

from crash_recovery import alert


class StartupTransient(RuntimeError):
    """Start-up could not finish for a reason that can pass (the ledger cannot be read or written now)."""


class StartupHeld(RuntimeError):
    """Start-up refused for a reason only an operator can resolve (the local log diverges from the ledger)."""


class HoldingApp:
    """A minimal ASGI app that answers while the real one cannot be built; it hands every request to the real app
    once ``ready`` is called."""

    def __init__(self, service: str):
        self.service = service
        self.app: Optional[Any] = None
        self.state = "starting"
        self.reason = "starting"
        self.attempts = 0
        self.next_retry_s: Optional[float] = None
        self.lock = threading.Lock()

    def ready(self, app: Any) -> None:
        with self.lock:
            self.app = app
            self.state = "ok"

    def body(self) -> dict:
        with self.lock:
            return {"status": self.state, "service": self.service, "write_ok": False, "ready": False,
                    "degraded_reasons": [self.reason[:500]], "startup_attempts": self.attempts,
                    "next_retry_s": self.next_retry_s}

    async def __call__(self, scope, receive, send) -> None:
        app = self.app
        if app is not None:
            await app(scope, receive, send)
            return
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if scope.get("path") == "/health" and scope.get("method") == "GET":
            payload = self.body()
        else:
            payload = {"detail": f"{self.service} is not ready ({self.state}); nothing took effect, retry later",
                       "took_effect": False}
        data = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"application/json"), (b"retry-after", b"5"),
                                (b"content-length", str(len(data)).encode())]})
        await send({"type": "http.response.body", "body": data})


def supervise(service: str, build: Callable[[], Any], on_ready: Optional[Callable[[Any], None]] = None,
              base_s: float = 1.0, cap_s: float = 60.0, held_retry_s: float = 300.0,
              sleep: Callable[[float], Any] = time.sleep, background: bool = True) -> Any:
    """The real app when it builds now; otherwise a ``HoldingApp`` that keeps retrying ``build`` (see module doc).
    ``on_ready(app)`` runs once the real app exists (at once, or from the retry thread)."""
    try:
        app = build()
    except (StartupTransient, StartupHeld) as exc:
        first = exc
    else:
        if on_ready is not None:
            on_ready(app)
        return app
    hold = HoldingApp(service)

    def note(exc: BaseException, wait: float) -> None:
        held = isinstance(exc, StartupHeld)
        with hold.lock:
            hold.attempts += 1
            hold.state = "refused" if held else "starting"
            hold.reason = str(exc)
            hold.next_retry_s = wait
        alert("log_divergence" if held else "startup_waiting_for_ledger", service, attempt=hold.attempts,
              next_retry_s=wait, reason=str(exc)[:500])
        print(f"{service}: {'REFUSING TO SERVE' if held else 'WAITING'} (attempt {hold.attempts}, retry in "
              f"{wait:.0f} s): {str(exc)[:500]}", file=sys.stderr, flush=True)

    def loop(exc: BaseException) -> None:
        delay = base_s
        while True:
            wait = held_retry_s if isinstance(exc, StartupHeld) else delay
            note(exc, wait)
            sleep(wait)
            delay = min(cap_s, delay * 2)
            try:
                app = build()
            except (StartupTransient, StartupHeld) as again:
                exc = again
                continue
            except BaseException as fatal:  # noqa: BLE001 - a non-retryable error now: stop serving
                alert("startup_failed", service, reason=type(fatal).__name__)
                print(f"{service}: start-up failed for good: {fatal}", file=sys.stderr, flush=True)
                import os
                os._exit(1)
            if on_ready is not None:
                on_ready(app)
            hold.ready(app)
            alert("startup_recovered", service, attempts=hold.attempts)
            return

    if background:
        threading.Thread(target=loop, args=(first,), name=f"{service}-startup-hold", daemon=True).start()
    else:
        loop(first)
    return hold
