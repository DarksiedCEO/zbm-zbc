"""War room worker: runs one department's cases inside that service's own in-process harness (ADR 0018).

Started by the engine as ``python -B worker.py <service dir> <driver file>``, one process per service, so the
services' shared module names (``service``, ``api``, ``store``, ``helpers``...) never collide. Cases arrive as JSON
on stdin; each result is printed as one ``WARROOM-RESULT <json>`` line (anything else the service prints is
ignored).

Sandbox guarantee, enforced here before any service code is imported:
- every socket connect raises (as each service's conftest does): nothing reaches a network, a provider, a ledger;
- every service env variable the driver names is cleared and test-only values are set (as the conftests do);
- each case gets its own fresh harness and its own temporary directory, removed when the case ends;
- the harnesses wire recording fakes or the services' fail-closed stand-ins: nothing is sent, charged or revoked
  outside the process.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.dont_write_bytecode = True


def _refuse(*a, **k):
    raise RuntimeError("war room sandbox: network access attempted (not allowed)")


def _seal_network() -> None:
    socket.socket.connect = _refuse            # type: ignore[method-assign]
    socket.socket.connect_ex = _refuse         # type: ignore[method-assign]
    socket.create_connection = _refuse         # type: ignore[assignment]


def _load_driver(path: Path):
    spec = importlib.util.spec_from_file_location(f"warroom_driver_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)               # type: ignore[union-attr]
    return mod


def _resolve(args: dict, inp: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        out[k] = inp[v[1:]] if isinstance(v, str) and v.startswith("$") else v
    return out


def run_case(driver, case: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="warroom-") as tmp:
        ctx = driver.setup(Path(tmp))
        try:
            for step in case["steps"]:
                fn = driver.ACTIONS[step["action"]]
                name = step.get("as", step["action"])      # the label invariants address this step by
                resp = fn(ctx, **_resolve(step.get("args"), case["input"]))
                if isinstance(resp, dict):          # an action that made several calls reports its last one
                    ctx.steps.append({"action": name, **resp})
                elif resp is not None:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = resp.text[:2000]
                    ctx.steps.append({"action": name, "status": resp.status_code, "body": body})
            state = driver.observe(ctx)
        finally:
            driver.teardown(ctx)
    return {"steps": ctx.steps, "state": state}


def main() -> int:
    service_dir, driver_file = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    _seal_network()
    for p in (service_dir / "tests", service_dir / "src"):
        sys.path.insert(0, str(p))
    os.chdir(service_dir)
    driver = _load_driver(driver_file)
    driver.prepare_env(os.environ)
    driver.import_harness()
    cases = json.loads(sys.stdin.read())
    for case in cases:
        t0 = time.perf_counter()
        try:
            obs = run_case(driver, case)
            obs["error"] = None
        except Exception as e:  # noqa: BLE001 - a crash is reported as ERROR, never as a pass
            tb = traceback.format_exception(type(e), e, e.__traceback__)
            obs = {"steps": [], "state": {}, "error": f"{type(e).__name__}: {e}\n" + "".join(tb[-4:])}
        obs["case_id"] = case["case_id"]
        obs["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        sys.stdout.write("WARROOM-RESULT " + json.dumps(obs, ensure_ascii=True, default=str) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
