"""Smoke check of the Railway images (CI job `railway-images`; docs/deploy/railway.md). Standard library only.

The images are built by the job's previous step (`docker build`, never pushed) and tagged `zbm-ci/<name>:ci`. For
each image this starts a container the way Railway does — PORT set, the production bind value from
.railway/railway.ts (`::` / `[::]`), throwaway tokens — and checks:

  - GET /health answers 200 through the published port (the service listens on PORT, on every interface);
  - the image's user is 10001:10001 and the service runs as uid 10001;
  - `docker stop` (SIGTERM to the entrypoint, PID 1) stops it with the service's own status, not a SIGKILL;
  - started without its required token, the container exits 1 with the SERVICE's own refusal naming that token
    (fail closed for the right reason: an entrypoint refusal or a crash does not pass).

The ledger starts first and stays up on a private Docker network (alias `ledger`): finance-py, legal-py, seo-py and
compliance-py
refuse to start with a data directory and no reachable ledger (their local log is verified against it), which is also
the startup order on Railway.

seo-py is started as root (`--user 0:0`), the way Railway starts it with RAILWAY_RUN_UID=0: its entrypoint writes
SEO_LOG_HASH_KEY to a secret file, which zbm-entrypoint does only as root (AEGIS L3). Checked once: the secret file is
0600 and owned by 10001, the value is not in the service's environment and the service cannot read PID 1's
environment; started as 10001 with the value set, the entrypoint refuses.

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


# name -> (internal port, env for a healthy start, the variable whose absence must refuse the start, the text the
# service's own refusal prints). Every service refuses with exit status 1.
def images() -> dict[str, tuple[int, dict[str, str], str, str]]:
    return {
        "ledger-rust": (8090, {"LEDGER_BIND_ADDR": "[::]", "LEDGER_SERVICE_TOKEN": LEDGER_TOKEN}, "LEDGER_SERVICE_TOKEN",
                        "ledger-rust: REFUSING TO START — LEDGER_SERVICE_TOKEN is not set"),
        "detection-py": (8000, {"DETECTION_BIND_ADDR": "::", "ZBM_SERVICE_TOKEN": tok()}, "ZBM_SERVICE_TOKEN",
                         "RuntimeError: ZBM_SERVICE_TOKEN is not set"),
        "finance-py": (8410, {"FIN_BIND_ADDR": "::", "FIN_SERVICE_TOKEN": tok(), **LEDGER_ENV}, "FIN_SERVICE_TOKEN",
                       "RuntimeError: FIN_SERVICE_TOKEN is not set"),
        "legal-py": (8420, {"LEGAL_BIND_ADDR": "::", "LEGAL_SERVICE_TOKEN": tok(), **LEDGER_ENV}, "LEGAL_SERVICE_TOKEN",
                     "RuntimeError: LEGAL_SERVICE_TOKEN is not set"),
        "seo-py": (8500, {"SEO_BIND_ADDR": "::", "SEO_SERVICE_TOKEN": tok(), "SEO_LOG_HASH_KEY": tok(), **LEDGER_ENV},
                   "SEO_SERVICE_TOKEN", "RuntimeError: SEO_SERVICE_TOKEN must be set"),
        # ADR 0024: Compliance (38) with its data directory on the image's /data and the ledger reachable
        "compliance-py": (8380, {"COMPLIANCE_BIND_ADDR": "::", "COMPLIANCE_SERVICE_TOKEN": tok(), **LEDGER_ENV},
                          "COMPLIANCE_SERVICE_TOKEN", "RuntimeError: COMPLIANCE_SERVICE_TOKEN is not set"),
        # ADR 0028: the client acceptance page; stateless (no volume, no ledger). Legal is not reachable here: /health
        # still answers (it never calls Legal)
        "client-portal-py": (8480, {"PORTAL_BIND_ADDR": "::", "PORTAL_LEGAL_URL": "http://legal.railway.internal:8420",
                                    "PORTAL_LEGAL_SERVICE_TOKEN": tok(), "PORTAL_LEGAL_CALLER_TOKEN": tok(),
                                    "PORTAL_EVIDENCE_KEY": tok(), "PORTAL_PUBLIC_ORIGIN": "https://accept.example.com"},
                             "PORTAL_LEGAL_SERVICE_TOKEN", "RuntimeError: PORTAL_LEGAL_SERVICE_TOKEN is not set"),
        "orchestrator": (8080, {"ORCHESTRATOR_BIND_ADDR": "[::]", "ORCHESTRATOR_SERVICE_TOKEN": tok(),
                                "DETECTION_SERVICE_TOKEN": tok(), "DETECTION_SERVICE_URL": "http://detection.invalid:8000",
                                **LEDGER_ENV}, "ORCHESTRATOR_SERVICE_TOKEN", "ORCHESTRATOR_SERVICE_TOKEN is not set"),
        "stripe-gateway": (8470, {"STRIPE_GATEWAY_BIND_ADDR": "::", "STRIPE_GATEWAY_FINANCE_URL": "http://127.0.0.1:9",
                                  "STRIPE_GATEWAY_FINANCE_TOKEN": tok(), "STRIPE_GATEWAY_CALLER_TOKEN": tok()},
                           "STRIPE_GATEWAY_CALLER_TOKEN",
                           "stripe-gateway: REFUSING TO START — STRIPE_GATEWAY_CALLER_TOKEN must be set"),
    }


# images whose entrypoint writes a secret file from a variable the smoke sets: zbm-entrypoint does that only as root
# (AEGIS L3), so they start as root, as Railway starts them with RAILWAY_RUN_UID=0 (the service still runs as 10001)
ROOT_START = {"seo-py": "SEO_LOG_HASH_KEY"}
REFUSAL_EXIT = "1"


def user_args(img: str) -> list[str]:
    return ["--user", "0:0"] if img in ROOT_START else []


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    # "CHECK PASS: " / "CHECK FAIL: " (one line each; a failure's detail with its newlines folded): the live-run format
    # devtools/ci_annotate.py turns into one annotation per failure, so a red railway-images job can be read from its
    # check-run annotations (docs/ci.md "Reading a red job"; the job log itself is not readable from the team's box)
    shown = detail if ok else " | ".join(x for x in detail.splitlines() if x.strip())
    print(("CHECK PASS: " if ok else "CHECK FAIL: ") + name + (f" -- {shown}" if shown else ""), flush=True)


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
    r = docker("run", "-d", "--name", name, *net, *user_args(img), "-p", f"127.0.0.1::{port}", "-e", f"PORT={port}",
               *env_args(env), tag)
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


def wait_exit(name: str, secs: float = 45) -> str:
    """'<running> <exit code>' once the container stopped (or the last state seen after secs)."""
    end = time.time() + secs
    state = "?"
    while time.time() < end:
        state = docker("inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", name).stdout.strip()
        if state.startswith("false"):
            break
        time.sleep(1)
    return state


def logs(name: str) -> str:
    r = docker("logs", name)
    return r.stdout + r.stderr


def refuses_without(img: str, port: int, env: dict[str, str], required: str, refusal: str, tag: str) -> None:
    name = f"{PREFIX}-{img}-noauth"
    docker("rm", "-f", name)
    bad = {k: v for k, v in env.items() if k != required}
    r = docker("run", "-d", "--name", name, "--network", NETWORK, *user_args(img), "-e", f"PORT={port}",
               *env_args(bad), tag)
    state = wait_exit(name) if r.returncode == 0 else "not started"
    out = logs(name) if r.returncode == 0 else r.stderr
    # the specific refusal: exit status 1 AND the service's own message naming the missing token (not the
    # entrypoint refusing, not a crash for some other reason)
    ok = state == f"false {REFUSAL_EXIT}" and refusal in out and "zbm-entrypoint: REFUSING" not in out
    check(f"{img}: without {required} the service refuses to start (exit {REFUSAL_EXIT}, '{refusal}')", ok,
          state if ok else f"{state}; logs: {out[-800:]}")
    docker("rm", "-f", name)


def secret_file_root_path(img: str, port: int, env: dict[str, str], tag: str) -> None:
    """AEGIS L3: the secret value never reaches the service; as 10001 with the value set, the entrypoint refuses."""
    value_var = ROOT_START[img]
    name = f"{PREFIX}-{img}-secret"
    docker("rm", "-f", name)
    r = docker("run", "-d", "--name", name, "--network", NETWORK, *user_args(img), "-p", f"127.0.0.1::{port}",
               "-e", f"PORT={port}", *env_args(env), tag)
    healthy, why = wait_health(name, port) if r.returncode == 0 else (False, r.stderr)
    if healthy:
        target = "/run/zbm-secrets/" + f"{value_var}_FILE".lower().replace("_", "-")
        st = docker("exec", name, "stat", "-c", "%u %a", target).stdout.strip()
        check(f"{img}: the {value_var} secret file is owned by 10001, mode 600", st == "10001 600", st)
        script = ('c=$(cut -d" " -f1 /proc/1/task/1/children); '
                  f'if tr "\\0" "\\n" < "/proc/$c/environ" | grep -q "^{value_var}="; then echo in-env; fi; '
                  'if setpriv --reuid=10001 --regid=10001 --clear-groups cat /proc/1/environ >/dev/null 2>&1; '
                  'then echo pid1-readable; fi; echo done')
        out = docker("exec", name, "sh", "-c", script).stdout.split()
        check(f"{img}: {value_var} is not in the service's environment and uid 10001 cannot read PID 1's",
              out == ["done"], " ".join(out))
    else:
        check(f"{img}: starts as root for the secret-file check", False, why[-1200:])
    docker("rm", "-f", name)
    name = f"{PREFIX}-{img}-secret-nonroot"
    docker("rm", "-f", name)
    r = docker("run", "-d", "--name", name, "--network", NETWORK, "-e", f"PORT={port}", *env_args(env), tag)
    state = wait_exit(name) if r.returncode == 0 else "not started"
    out = logs(name) if r.returncode == 0 else r.stderr
    want = f"zbm-entrypoint: REFUSING TO START — --secret-file {value_var}_FILE: {value_var} is set but this container runs as uid 10001, not root"
    check(f"{img}: as uid 10001 with {value_var} set, the entrypoint refuses (exit 1)",
          state == "false 1" and want in out, f"{state}; logs: {out[-600:]}")
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
            port, env, required, refusal = table[img]
            name = start(img, port, env, f"zbm-ci/{img}:ci")
            if name:
                stop_check(img, name)
            refuses_without(img, port, env, required, refusal, f"zbm-ci/{img}:ci")
            if img in ROOT_START:
                secret_file_root_path(img, port, env, f"zbm-ci/{img}:ci")
        if "ledger-rust" in names:
            if ledger:
                stop_check("ledger-rust", ledger)
            refuses_without("ledger-rust", 8090, *table["ledger-rust"][1:], "zbm-ci/ledger-rust:ci")
            ledger_volume("zbm-ci/ledger-rust:ci")
    finally:
        left = docker("ps", "-aq", "--filter", f"name={PREFIX}").stdout.split()
        if left:
            docker("rm", "-f", *left)
        docker("network", "rm", NETWORK)
    failed = [r for r in RESULTS if not r[1]]
    print(json.dumps({"checks": len(RESULTS), "failed": [r[0] for r in failed]}))
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed (railway_smoke)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
