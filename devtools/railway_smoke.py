"""Smoke check of the Railway images (CI job `railway-images`; docs/deploy/railway.md). Standard library only.

The images are built by the job's previous step (`docker build`, never pushed) and tagged `zbm-ci/<name>:ci`. For
each image this starts a container the way Railway does — PORT set, the production bind value from
.railway/railway.ts (`::` / `[::]`), throwaway tokens — and checks:

  - GET /health answers 200 through the published port (the service listens on PORT, on every interface);
  - the image's user is 10001:10001 and the service runs as uid 10001;
  - `docker stop` (SIGTERM to the entrypoint, PID 1) stops it with the service's own status, not a SIGKILL;
  - started without its required token, the container exits non-zero (fail closed).

The ledger starts first and stays up on a private Docker network (alias `ledger`): finance-py, legal-py and seo-py
refuse to start with a data directory and no reachable ledger (their local log is verified against it), which is also
the startup order on Railway.

And once, for ledger-rust, the Railway volume path: a root-owned named volume at /data, the container started as
root with RAILWAY_RUN_UID=0 — the entrypoint must hand /data/ledger to uid 10001 and run the ledger as 10001.

    python3 devtools/railway_smoke.py [--only NAME ...]

Exit 0 only when every check passed. Containers and volumes it creates are removed whatever happens.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

PREFIX = "zbm-ci-smoke"
NETWORK = f"{PREFIX}-net"
LEDGER_TOKEN = secrets.token_hex(32)
LEDGER_ENV = {"LEDGER_SERVICE_URL": "http://ledger:8090", "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}


def tok() -> str:
    return secrets.token_hex(32)


# name -> (internal port, env for a healthy start, the variable whose absence must refuse the start)
def images() -> dict[str, tuple[int, dict[str, str], str]]:
    return {
        "ledger-rust": (8090, {"LEDGER_BIND_ADDR": "[::]", "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}, "LEDGER_SERVICE_TOKEN"),
        "detection-py": (8000, {"DETECTION_BIND_ADDR": "::", "ZBM_SERVICE_TOKEN": tok()}, "ZBM_SERVICE_TOKEN"),
        "finance-py": (8410, {"FIN_BIND_ADDR": "::", "FIN_SERVICE_TOKEN": tok(), **LEDGER_ENV}, "FIN_SERVICE_TOKEN"),
        "legal-py": (8420, {"LEGAL_BIND_ADDR": "::", "LEGAL_SERVICE_TOKEN": tok(), **LEDGER_ENV}, "LEGAL_SERVICE_TOKEN"),
        "seo-py": (8500, {"SEO_BIND_ADDR": "::", "SEO_SERVICE_TOKEN": tok(), "SEO_LOG_HASH_KEY": tok(), **LEDGER_ENV},
                   "SEO_SERVICE_TOKEN"),
        "orchestrator": (8080, {"ORCHESTRATOR_BIND_ADDR": "[::]", "ORCHESTRATOR_SERVICE_TOKEN": tok(),
                                "DETECTION_SERVICE_TOKEN": tok(), "DETECTION_SERVICE_URL": "http://detection.invalid:8000",
                                **LEDGER_ENV}, "ORCHESTRATOR_SERVICE_TOKEN"),
        "stripe-gateway": (8470, {"STRIPE_GATEWAY_BIND_ADDR": "::", "STRIPE_GATEWAY_FINANCE_URL": "http://127.0.0.1:9",
                                  "STRIPE_GATEWAY_FINANCE_TOKEN": tok(), "STRIPE_GATEWAY_CALLER_TOKEN": tok()},
                           "STRIPE_GATEWAY_CALLER_TOKEN"),
    }


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail else ""), flush=True)


def docker(*args: str, timeout: float = 120, check_rc: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} failed ({r.returncode}): {r.stderr.strip()[-400:]}")
    return r


def env_args(env: dict[str, str]) -> list[str]:
    out: list[str] = []
    for k, v in env.items():
        out += ["-e", f"{k}={v}"]
    return out


def published_port(name: str, port: int) -> int:
    out = docker("port", name, f"{port}/tcp", check_rc=True).stdout.split()
    first = next(x for x in out if x.startswith("127.0.0.1:"))
    return int(first.rsplit(":", 1)[1])


def wait_health(name: str, port: int, secs: float = 90) -> tuple[bool, str]:
    end = time.time() + secs
    last = ""
    while time.time() < end:
        state = docker("inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", name).stdout.strip()
        if not state.startswith("true"):
            return False, f"container stopped ({state}): " + docker("logs", "--tail", "30", name).stderr[-1500:]
        try:
            hp = published_port(name, port)
            with urllib.request.urlopen(f"http://127.0.0.1:{hp}/health", timeout=3) as r:
                if r.status == 200:
                    return True, r.read(300).decode("utf-8", "replace")
                last = f"HTTP {r.status}"
        except (urllib.error.URLError, OSError, StopIteration, RuntimeError) as e:
            last = repr(e)[:200]
        time.sleep(1)
    return False, f"no 200 within {secs:.0f}s ({last}); logs: " + docker("logs", "--tail", "30", name).stderr[-1500:]


def service_uid(name: str) -> str:
    """uid of the entrypoint's child (the service) inside the container."""
    script = ('c=$(cut -d" " -f1 /proc/1/task/1/children); '
              'awk \'/^Uid:/ {print $2}\' "/proc/$c/status"')
    return docker("exec", name, "sh", "-c", script).stdout.strip()


def start(img: str, port: int, env: dict[str, str], tag: str, alias: str | None = None) -> str | None:
    name = f"{PREFIX}-{img}"
    docker("rm", "-f", name)
    user = docker("image", "inspect", "--format", "{{.Config.User}}", tag).stdout.strip()
    check(f"{img}: image user is 10001:10001", user == "10001:10001", user)
    net = ["--network", NETWORK] + (["--network-alias", alias] if alias else [])
    r = docker("run", "-d", "--name", name, *net, "-p", f"127.0.0.1::{port}", "-e", f"PORT={port}", *env_args(env), tag)
    if r.returncode != 0:
        check(f"{img}: container starts", False, r.stderr[-400:])
        return None
    ok, detail = wait_health(name, port)
    check(f"{img}: GET /health is 200 on PORT={port} with the production bind", ok, detail)
    if not ok:
        return None
    uid = service_uid(name)
    check(f"{img}: the service runs as uid 10001", uid == "10001", uid)
    return name


def stop_check(img: str, name: str) -> None:
    t0 = time.time()
    docker("stop", "-t", "20", name, timeout=60)
    code = docker("inspect", "--format", "{{.State.ExitCode}}", name).stdout.strip()
    took = time.time() - t0
    # 137 = SIGKILL after the timeout: the SIGTERM was ignored (the PID 1 problem the entrypoint exists for)
    check(f"{img}: docker stop (SIGTERM) stops it without a SIGKILL", code != "137" and took < 19,
          f"exit {code} after {took:.1f}s")
    docker("rm", "-f", name)


def refuses_without(img: str, port: int, env: dict[str, str], required: str, tag: str) -> None:
    name = f"{PREFIX}-{img}-noauth"
    docker("rm", "-f", name)
    bad = {k: v for k, v in env.items() if k != required}
    r = docker("run", "-d", "--name", name, "--network", NETWORK, "-e", f"PORT={port}", *env_args(bad), tag)
    end = time.time() + 45
    state = "?"
    while r.returncode == 0 and time.time() < end:
        state = docker("inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", name).stdout.strip()
        if state.startswith("false"):
            break
        time.sleep(1)
    check(f"{img}: without {required} the container refuses to start (fail closed)",
          r.returncode == 0 and state.startswith("false") and not state.endswith(" 0"), state)
    docker("rm", "-f", name)


def ledger_volume(tag: str) -> None:
    """The Railway volume path: root-owned volume, RAILWAY_RUN_UID=0 (container started as root)."""
    name, vol = f"{PREFIX}-ledger-volume", f"{PREFIX}-ledger-data"
    docker("rm", "-f", name)
    docker("volume", "rm", "-f", vol)
    docker("volume", "create", vol, check_rc=True)
    try:
        r = docker("run", "-d", "--name", name, "--user", "0:0", "-v", f"{vol}:/data", "-p", "127.0.0.1::8090",
                   "-e", "PORT=8090", "-e", "RAILWAY_RUN_UID=0", "-e", "LEDGER_BIND_ADDR=[::]",
                   "-e", f"LEDGER_SERVICE_TOKEN={tok()}", tag)
        if r.returncode != 0:
            check("ledger-rust (root-owned volume, RAILWAY_RUN_UID=0): container starts", False, r.stderr[-400:])
            return
        ok, detail = wait_health(name, 8090)
        check("ledger-rust (root-owned volume, RAILWAY_RUN_UID=0): GET /health is 200", ok, detail)
        if ok:
            uid = service_uid(name)
            check("ledger-rust (root-owned volume): the ledger runs as uid 10001, not root", uid == "10001", uid)
            st = docker("exec", name, "stat", "-c", "%u %a", "/data/ledger").stdout.strip()
            check("ledger-rust (root-owned volume): /data/ledger is owned by 10001, mode 700", st == "10001 700", st)
            files = docker("exec", name, "ls", "/data/ledger").stdout.split()
            check("ledger-rust (root-owned volume): the lock and head checkpoint are on the volume",
                  "ledger.jsonl.lock" in files, " ".join(files))
            docker("stop", "-t", "20", name, timeout=60)
    finally:
        docker("rm", "-f", name)
        docker("volume", "rm", "-f", vol)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", nargs="*", help="image names (default: all)")
    a = ap.parse_args()
    table = images()
    names = a.only or list(table)
    unknown = [n for n in names if n not in table]
    if unknown:
        print(f"unknown image(s): {unknown}; known: {list(table)}")
        return 2
    try:
        docker("network", "rm", NETWORK)
        docker("network", "create", NETWORK, check_rc=True)
        ledger = None
        if "ledger-rust" in names:     # first, and kept up while the others run
            ledger = start("ledger-rust", *table["ledger-rust"][:2], "zbm-ci/ledger-rust:ci", alias="ledger")
        for img in names:
            if img == "ledger-rust":
                continue
            port, env, required = table[img]
            name = start(img, port, env, f"zbm-ci/{img}:ci")
            if name:
                stop_check(img, name)
            refuses_without(img, port, env, required, f"zbm-ci/{img}:ci")
        if "ledger-rust" in names:
            if ledger:
                stop_check("ledger-rust", ledger)
            refuses_without("ledger-rust", 8090, table["ledger-rust"][1], "LEDGER_SERVICE_TOKEN",
                            "zbm-ci/ledger-rust:ci")
            ledger_volume("zbm-ci/ledger-rust:ci")
    finally:
        left = docker("ps", "-aq", "--filter", f"name={PREFIX}").stdout.split()
        if left:
            docker("rm", "-f", *left)
        docker("network", "rm", NETWORK)
    failed = [r for r in RESULTS if not r[1]]
    print(json.dumps({"checks": len(RESULTS), "failed": [r[0] for r in failed]}))
    print(f"railway_smoke: {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
