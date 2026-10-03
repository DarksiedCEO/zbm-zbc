#!/usr/bin/env python3
"""Repo-wide hygiene check (fix wave 25, founder ruling R-HYGIENE / H9). Standard library only.

Two halves:

``run`` — wraps ONE full suite run (any language) and fails it when, for that run:
  R1 tracked   a tracked file changed (``git status --porcelain`` differs after the run from before it, or a path it
               already listed before the run has different content after it; CI starts clean, so any difference is
               the suite's);
  R2 ignored   a new git-ignored file or directory appeared in the checkout (``git status --porcelain --ignored``,
               before vs after; build outputs a job made BEFORE the run are in the "before" set; paths given with
               --allow-ignored, e.g. a venv, are allowed);
  R3 tmp       the run's private TMPDIR is not empty at the end, or a new entry appeared directly in the system temp
               directory (/tmp) during the run (the suite runs with TMPDIR, TMP and TEMP pointing at a fresh private
               directory OUTSIDE /tmp, so anything new in /tmp was written past it; on a shared machine another
               process can create /tmp entries too — the names are printed so a person can tell; on a CI runner the
               machine is the job's alone);
  R4 procs     a process the suite started is still alive after the suite's command exited. Tracked by process
               group and session (the suite starts as its own session leader), by an environment marker every
               descendant inherits unless it scrubs its environment, and — on Linux — by being the suite's child
               subreaper (PR_SET_CHILD_SUBREAPER), so even a double-forked, setsid'd, env-scrubbed grandchild is
               re-parented to this process and found. Elsewhere (macOS: no subreaper, no readable environment,
               table from ``ps``) only the process group and its descendants are tracked — a setsid'd orphan
               escapes there — and the checker's own ``ps`` helper is excluded by its pid. Each leftover is printed
               with pid, ppid, pgid, stat and its full command, then killed (by PID; they are this run's own) so
               the next job is not poisoned;
  R5 skips     a test was skipped for a reason not on the suite's expected-skip list (devtools/hygiene_allowlist.json,
               "expected_skips"); Go and Rust suites expect no skip or ignored test, the dashboard none;
  R6 counts    the number of tests the run executed/collected differs from the suite's row in docs/test-counts.md
               (``--counts check``), or rewrites that row (``--counts write``). Tests that exist on some OSes only
               are named in the allowlist ("platform_only_tests"); elsewhere they must be absent by name and the
               expected count is lower by that many (printed in the summary line).

``lint`` — static rules over the whole tree:
  L1 wallclock a test asserts an UPPER bound on a wall-clock delta against a literal (Python: an ``assert``,
               ``self.assertLess*`` / ``assertTrue`` / ``assertFalse`` or an ``if … : raise / pytest.fail`` comparing ``time.time()/perf_counter()/monotonic()`` (or ``*_ns``) deltas — direct or
               through a local name — with a numeric literal; Rust/Go/TS: an ``elapsed``/``time.Since``/``Date.now()``
               delta compared with a literal duration). Fails unless an allowlist entry with a reason covers it. Lower
               bounds (delta >= literal) are not flagged: load can only lengthen elapsed time, so they cannot flake;
               CPU-time and same-work-ratio measurements are not wall-clock;
  L2 ports     a test binds or targets a hard-coded TCP port instead of port 0 / the port its child announced / the
               shared helper (an integer literal in 1024-65535 used as a port: ``bind((h, N))``, ``port=N``,
               ``"127.0.0.1:N"``, a ``*PORT*`` name or env entry set to a literal, a default port range literal);
  L3 counts    a README / docs/ci.md / ADR / ci.yml comment states a test count by hand (``N tests``, ``N/N passing``,
               ``N unit + M integration``, ``# N tests``) outside docs/test-counts.md. A count tied to a commit (a
               resolvable commit id in the same paragraph) is a historical record and allowed;
  L4 shared    the shared files are byte-identical in every service that has them (graceful_close.py, the shared
               graceful-close test files modulo their two service constants, tests/_procinfo.py with the shared port
               helper, tests/test_procinfo.py, tests/test_shared_ports.py) and graceful_close.py matches the sha256
               the tests pin.

``counts`` — prints docs/test-counts.md's table, or (``--check``) verifies every suite row is well-formed.

Exit status: 0 clean, 1 violations, 2 usage/environment error. Every violation is printed as
``HYGIENE <rule> <suite-or-path>: <detail>``.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ALLOWLIST = REPO / "devtools" / "hygiene_allowlist.json"
COUNTS_DOC = REPO / "docs" / "test-counts.md"
PLUGIN_DIR = REPO / "devtools" / "pytest_plugin"
MARKER_ENV = "ZBM_HYGIENE_RUN"

PY_SERVICES = ("detection-py", "fulfillment-py", "onboarding-py", "creative-py", "compliance-py", "verification-py",
               "clipper-network-py", "finance-py", "legal-py", "delivery-py")


def violation(rule: str, where: str, detail: str) -> str:
    return f"HYGIENE {rule} {where}: {detail}"


def load_allowlist() -> dict:
    return json.loads(ALLOWLIST.read_text())


# ======================================================================================================== run

def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"hygiene_check: git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def git_state() -> tuple[set[str], set[str]]:
    plain = {x for x in git("status", "--porcelain", "--untracked-files=all").splitlines() if x}
    ignored = {x[3:] for x in git("status", "--porcelain", "--ignored").splitlines() if x.startswith("!! ")}
    return plain, ignored


def dirty_digests(plain: set[str]) -> dict[str, str]:
    """sha256 of every path ``git status`` already lists before the run (a local checkout may be dirty; CI's is not):
    a suite that changes such a file again leaves its status line as it was, so R1 compares the CONTENT too
    (fix wave 25, E-C review: the E0 check compared status lines only)."""
    import hashlib
    out = {}
    for line in plain:
        path = line[3:].split(" -> ")[-1].strip('"')
        f = REPO / path
        try:
            out[path] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "<not a file>"
        except OSError:
            out[path] = "<unreadable>"
    return out


def system_tmp() -> Path:
    # The system temp dir as the OS defines it, not as this process's TMPDIR says.
    return Path("/tmp") if Path("/tmp").is_dir() else Path(tempfile.gettempdir())


def list_dir(p: Path) -> set[str]:
    try:
        return set(os.listdir(p))
    except OSError:
        return set()


def _set_subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(36, 1, 0, 0, 0) == 0          # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        return False


def _reap_zombies() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _proc_table() -> list[dict]:
    """Every process: pid, ppid, pgid, sid, state, cmd, and whether its environment carries the run marker."""
    rows = []
    if Path("/proc/self/stat").exists():
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                stat = Path(f"/proc/{d}/stat").read_text()
                rest = stat.rsplit(")", 1)[1].split()
                cmd = Path(f"/proc/{d}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
                try:
                    env = Path(f"/proc/{d}/environ").read_bytes()
                except OSError:
                    env = b""
            except (OSError, IndexError):
                continue
            rows.append({"pid": int(d), "state": rest[0], "stat": rest[0], "ppid": int(rest[1]), "pgid": int(rest[2]),
                         "sid": int(rest[3]), "cmd": cmd, "env": env})
        return rows
    # macOS / BSD: ps (no environment; the marker check is Linux-only). `ps -ax` lists ITSELF, as a child of this
    # checker: that row is the checker's own helper, never the suite's, and is dropped by its pid (fix wave 26a,
    # W26-1 — on every macOS job of CI #2 it was reported as "still alive: ps -axo pid=,ppid=,pgid=,stat=,command=").
    helper = subprocess.Popen(["ps", "-axo", "pid=,ppid=,pgid=,stat=,command="], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
    out, _ = helper.communicate()
    for line in out.splitlines():
        f = line.split(None, 4)
        if len(f) < 4 or not f[0].isdigit() or int(f[0]) == helper.pid:
            continue
        rows.append({"pid": int(f[0]), "ppid": int(f[1]), "pgid": int(f[2]), "sid": -1, "state": f[3][:1],
                     "stat": f[3], "cmd": f[4] if len(f) > 4 else "", "env": b""})
    return rows


def leftover_processes(leader: int, token: str, subreaper: bool) -> list[dict]:
    """The suite's surviving processes: its process group / session (``leader``), anything carrying the run marker,
    every descendant of those, and — ONLY when this checker is the child subreaper — this checker's own children
    (orphans re-parented to it). Without a subreaper (macOS) a child of this checker is one of its own helpers
    (``ps``), never the suite's, so it is not walked (fix wave 26a, W26-1)."""
    me = os.getpid()
    rows = [r for r in _proc_table() if r["pid"] != me and r["state"] != "Z"]
    by_parent: dict[int, list[dict]] = {}
    for r in rows:
        by_parent.setdefault(r["ppid"], []).append(r)
    found: dict[int, dict] = {}
    marker = f"{MARKER_ENV}={token}".encode()
    for r in rows:
        if r["pgid"] == leader or r["sid"] == leader or marker in r["env"].split(b"\0"):
            found[r["pid"]] = r
    todo = ([r["pid"] for r in by_parent.get(me, [])] if subreaper else []) + list(found)   # orphans + descendants
    while todo:
        pid = todo.pop()
        row = next((r for r in rows if r["pid"] == pid), None)
        if row is None:
            continue
        found[pid] = row
        todo.extend(k["pid"] for k in by_parent.get(pid, []) if k["pid"] not in found)
    return sorted(found.values(), key=lambda r: r["pid"])


def kill_pids(pids: list[int]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in pids:
            try:
                os.kill(p, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _reap_zombies()
            alive = []
            for p in pids:
                try:
                    os.kill(p, 0)
                    alive.append(p)
                except ProcessLookupError:
                    pass
            if not alive:
                return
            time.sleep(0.05)


def _count_from_output(kind: str, text: str) -> int | None:
    if kind == "cargo":
        tot = 0
        found = False
        for m in re.finditer(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", text):
            found = True
            tot += sum(int(x) for x in m.groups())
        return tot if found else None
    if kind == "go":
        names = re.findall(r"^=== RUN\s+(\S+)$", text, re.M)
        return len([n for n in names if "/" not in n]) if names else None
    if kind == "node":
        m = re.findall(r"^# tests (\d+)$", text, re.M)
        return int(m[-1]) if m else None
    return None


def _names_from_output(kind: str, text: str) -> set[str] | None:
    """Names of the tests that ran, as the runner prints them (cargo: ``test <name> ... <outcome>``; go: top-level
    ``=== RUN <name>``); None for a kind whose output names no tests here (pytest, node)."""
    if kind == "cargo":
        return set(re.findall(r"^test (\S+) \.\.\. ", text, re.M))
    if kind == "go":
        return {n for n in re.findall(r"^=== RUN\s+(\S+)$", text, re.M) if "/" not in n}
    return None


def current_os() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


def platform_only(allow: dict, suite: str) -> list[dict]:
    """The suite's tests that exist on some OSes only (allowlist "platform_only_tests"): each entry names the test
    as the runner prints it, the OSes it runs on (``only_on``, sys.platform names, "linux" for Linux) and why.
    docs/test-counts.md's row counts EVERY listed test; on an OS outside a test's ``only_on`` that test must be
    absent and the expected count is one lower per such test (fix wave 26a, W26-5: an anonymous per-OS number
    applied silently before)."""
    return allow.get("platform_only_tests", {}).get(suite, [])


def platform_note(entries: list[dict]) -> str:
    """The docs/test-counts.md "Platform-only tests" cell for a suite (generated from the allowlist)."""
    if not entries:
        return "—"
    return "; ".join(f"`{e['test']}` only on {', '.join(e['only_on'])}" for e in entries)


def _skips_from_output(kind: str, text: str) -> list[dict]:
    if kind == "cargo":
        out = []
        for m in re.finditer(r"test result: \w+\. \d+ passed; \d+ failed; (\d+) ignored", text):
            if int(m.group(1)):
                out.append({"nodeid": "(cargo)", "kind": "ignored", "reason": f"{m.group(1)} ignored test(s)"})
        return out
    if kind == "go":
        return [{"nodeid": n, "kind": "skip", "reason": "go test SKIP"} for n in re.findall(r"--- SKIP: (\S+)", text)]
    if kind == "node":
        m = re.findall(r"^# skipped (\d+)$", text, re.M)
        n = int(m[-1]) if m else 0
        return [{"nodeid": "(node)", "kind": "skip", "reason": f"{n} skipped test(s)"}] if n else []
    return []


def read_counts_doc() -> dict[str, int]:
    rows = {}
    if COUNTS_DOC.exists():
        for m in re.finditer(r"^\| `([^`]+)` \| (\d+) \|", COUNTS_DOC.read_text(), re.M):
            rows[m.group(1)] = int(m.group(2))
    return rows


ROW_RE = re.compile(r"^\| `([^`]+)` \| (\d+) \| ([^|\n]+) \|(?: ([^|\n]*) \|)?[ \t]*$", re.M)


def read_counts_notes() -> dict[str, str]:
    """suite -> its "Platform-only tests" cell ("" for a row without that column)."""
    if not COUNTS_DOC.exists():
        return {}
    return {m.group(1): (m.group(4) or "").strip() for m in ROW_RE.finditer(COUNTS_DOC.read_text())}


COUNTS_HEADER = """# Test counts (generated)

Generated by `devtools/hygiene_check.py run --counts write` from real suite runs; checked by every CI test job
(`--counts check`), which fails when a suite's count differs from its row here. Do not edit by hand, and do not
quote a test count anywhere else in the docs: link here (the hygiene lint, rule L3, fails a hand-written count).

How each suite is counted: Python — tests collected by pytest (skips included); Rust — tests run by `cargo test`
(passed + failed + ignored, every test binary, doc tests included); Go — top-level tests run by `go test -v`
(`=== RUN` lines without a `/`); dashboard — `# tests` reported by `node --test`.

A row counts every test of the suite, including tests that exist on some operating systems only. Those are named in
the last column (from `devtools/hygiene_allowlist.json` "platform_only_tests", with the reason there): on any other
OS the check expects exactly those tests to be absent — by name — and the count to be lower by that many, and it
says so in its summary line; a listed test that runs where it should not, or is missing where it should run, fails
the check.

| Suite | Tests | Counted by | Platform-only tests |
|---|---|---|---|
"""

KIND_LABEL = {"pytest": "pytest collection", "cargo": "cargo test", "go": "go test -v", "node": "node --test"}


def write_counts_doc(updates: dict[str, tuple[int, str]], allow: dict | None = None) -> None:
    allow = load_allowlist() if allow is None else allow
    rows = {}
    if COUNTS_DOC.exists():
        for m in ROW_RE.finditer(COUNTS_DOC.read_text()):
            rows[m.group(1)] = (int(m.group(2)), m.group(3).strip())
    for suite, (n, kind) in updates.items():
        rows[suite] = (n, KIND_LABEL.get(kind, kind))
    body = "".join(f"| `{s}` | {n} | {k} | {platform_note(platform_only(allow, s))} |\n"
                   for s, (n, k) in sorted(rows.items()))
    COUNTS_DOC.write_text(COUNTS_HEADER + body)


def cmd_run(a: argparse.Namespace) -> int:
    cmd = a.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("hygiene_check run: no command given", file=sys.stderr)
        return 2
    allow = load_allowlist()
    suite = a.suite
    kind = a.kind
    cwd = (REPO / a.cwd).resolve() if a.cwd else Path.cwd()
    work_root = Path(a.work_dir or os.environ.get("RUNNER_TEMP") or Path.home() / ".cache" / "zbm-hygiene")
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"hyg-{re.sub(r'[^A-Za-z0-9]+', '-', suite)}-", dir=work_root))
    private_tmp = work / "tmp"
    private_tmp.mkdir()
    results = work / "results.json"
    log_path = work / "output.log"
    systmp = Path(a.system_tmp) if a.system_tmp else system_tmp()
    if work_root.resolve() == systmp.resolve():
        # the private TMPDIR would itself be a new top-level entry of the directory R3 watches
        print(f"hygiene_check run: --work-dir must not be {systmp} itself (a subdirectory is fine)", file=sys.stderr)
        shutil.rmtree(work, ignore_errors=True)
        return 2

    token = uuid.uuid4().hex
    env = dict(os.environ, TMPDIR=str(private_tmp), TMP=str(private_tmp), TEMP=str(private_tmp),
               PYTHONDONTWRITEBYTECODE="1", **{MARKER_ENV: token})
    if kind == "pytest":
        # `python -m pytest ARGS` -> `python -c BOOT ARGS`: the plugin's directory goes on THIS interpreter's sys.path
        # only (never PYTHONPATH, which everything the tests start would inherit); sys.path[0] is the cwd either way.
        try:
            i = cmd.index("-m")
            assert cmd[i + 1] == "pytest"
        except (ValueError, IndexError, AssertionError):
            print("hygiene_check run --kind pytest: the command must be `<python> -m pytest ...`", file=sys.stderr)
            return 2
        boot = (f"import sys; sys.path.insert(1, {str(PLUGIN_DIR)!r}); import pytest; "
                "sys.exit(pytest.main(sys.argv[1:] + ['-p', 'zbm_pytest_hygiene', "
                "'-o', 'tmp_path_retention_policy=none']))")
        cmd = cmd[:i] + ["-c", boot] + cmd[i + 2:]
        env["ZBM_HYGIENE_RESULTS"] = str(results)

    plain_before, ignored_before = git_state()
    digests_before = dirty_digests(plain_before)
    tmp_before = list_dir(systmp)
    subreaper = _set_subreaper()
    print(f"hygiene_check: suite {suite} in {cwd}\n  private TMPDIR {private_tmp}\n  subreaper {subreaper}\n"
          f"  command {shlex.join(cmd)}", flush=True)
    with open(log_path, "wb") as log:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True)
        assert p.stdout is not None
        for chunk in iter(lambda: p.stdout.read1(65536), b""):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            log.write(chunk)
        rc = p.wait()
    leader = p.pid
    _reap_zombies()
    out_text = log_path.read_text(errors="replace")

    problems: list[str] = []
    # R4 first, so a leftover process cannot keep writing while the rest is checked
    left = leftover_processes(leader, token, subreaper)
    if left:
        for r in left:
            # every field a person needs to tell what it is, and the FULL command line (fix wave 26a, W26-1)
            problems.append(violation("R4-procs", suite, f"pid {r['pid']} (ppid {r['ppid']}, pgid {r['pgid']}, "
                                                         f"stat {r.get('stat', r['state'])}) still alive: "
                                                         f"command {r['cmd'] or '<none>'}"))
        kill_pids([r["pid"] for r in left])

    plain_after, ignored_after = git_state()
    for line in sorted(plain_after ^ plain_before):
        problems.append(violation("R1-tracked", suite, f"git status changed: {line!r}"))
    for path, digest in sorted(dirty_digests(plain_before & plain_after).items()):
        if digests_before.get(path) != digest:
            problems.append(violation("R1-tracked", suite, f"{path} (already changed before the run) was changed again"))
    allowed = [x.rstrip("/") + "/" for x in a.allow_ignored] + [x for x in a.allow_ignored if not x.endswith("/")]
    for path in sorted(ignored_after - ignored_before):
        if any(path == x.rstrip("/") or path.startswith(x.rstrip("/") + "/") for x in allowed):
            continue
        problems.append(violation("R2-ignored", suite, f"new git-ignored path in the checkout: {path}"))

    leftovers = sorted(os.listdir(private_tmp))
    for name in leftovers:
        problems.append(violation("R3-tmp", suite, f"left in the private TMPDIR: {name}"))
    ignore_re = [re.compile(x) for x in a.tmp_ignore]
    for name in sorted(list_dir(systmp) - tmp_before):
        if any(r.search(name) for r in ignore_re):
            continue
        problems.append(violation("R3-tmp", suite, f"new entry in {systmp} during the run: {name}"))

    # R5 skips and R6 counts
    if kind == "pytest":
        try:
            res = json.loads(results.read_text())
        except (OSError, ValueError):
            res = None
            problems.append(violation("R6-counts", suite, "the pytest plugin wrote no results (did pytest start?)"))
        count = res["collected"] if res else None
        skips = res["skips"] if res else []
    else:
        count = _count_from_output(kind, out_text)
        skips = _skips_from_output(kind, out_text)
    expected = allow.get("expected_skips", {}).get(suite, [])
    for s in skips:
        if not any(re.search(e["reason_regex"], s["reason"]) for e in expected):
            problems.append(violation("R5-skips", suite, f"unexpected {s['kind']}: {s['nodeid']}: {s['reason']}"))
    count_note = ""
    if a.counts != "off" and kind != "none":
        # docs/test-counts.md counts every test; tests that exist on some OSes only are NAMED in the allowlist
        # ("platform_only_tests"): here they must be present or absent by name, and the expected count is lower by
        # the absent ones — stated in the summary line (fix wave 26a, W26-5).
        osname = a.count_os or current_os()
        entries = platform_only(allow, suite)
        absent = [e for e in entries if osname not in e["only_on"]]
        names = _names_from_output(kind, out_text) if entries else None
        if entries and names is None:
            problems.append(violation("R6-counts", suite, f"platform_only_tests are listed but a {kind} run's "
                                                          f"output names no tests, so they cannot be checked"))
        for e in entries if names is not None else []:
            here = osname in e["only_on"]
            if here and e["test"] not in names:
                problems.append(violation("R6-counts", suite, f"platform-only test {e['test']} (only on "
                                          f"{', '.join(e['only_on'])}) did not run on {osname}"))
            if not here and e["test"] in names:
                problems.append(violation("R6-counts", suite, f"platform-only test {e['test']} (only on "
                                          f"{', '.join(e['only_on'])}) ran on {osname}: remove its allowlist "
                                          f"entry or restore its platform gate"))
        if absent:
            count_note = (f" ({len(absent)} platform-only test(s) not on {osname}: "
                          + ", ".join(e["test"] for e in absent) + ")")
        if count is None:
            problems.append(violation("R6-counts", suite, "could not determine how many tests ran"))
        elif a.counts == "write":
            write_counts_doc({suite: (count + len(absent), kind)}, allow)
            print(f"hygiene_check: docs/test-counts.md row `{suite}` = {count + len(absent)}{count_note}")
        else:
            doc = read_counts_doc()
            if suite not in doc:
                problems.append(violation("R6-counts", suite, f"no row in docs/test-counts.md (this run: {count})"))
            elif doc[suite] - len(absent) != count:
                problems.append(violation("R6-counts", suite, f"docs/test-counts.md says {doc[suite]}"
                                          + (f" (expected {doc[suite] - len(absent)} on {osname}{count_note})"
                                             if absent else "")
                                          + f", this run counted {count}; regenerate with --counts write"))
            elif absent:
                count_note += f"; docs/test-counts.md row {doc[suite]}"

    shutil.rmtree(work, ignore_errors=True)
    print(f"hygiene_check: suite {suite} exited {rc}; tests counted: {count}{count_note}; skips: {len(skips)}; "
          f"hygiene violations: {len(problems)}", flush=True)
    for line in problems:
        print(line, flush=True)
    if rc != 0:
        return rc
    return 1 if problems else 0


# ======================================================================================================= lint

TIME_FUNCS = {"time", "perf_counter", "monotonic", "time_ns", "perf_counter_ns", "monotonic_ns"}


def _is_clock_call(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in TIME_FUNCS:
            return isinstance(f.value, ast.Name) and f.value.id in ("time", "_time")
        if isinstance(f, ast.Name) and f.id in ("perf_counter", "monotonic", "perf_counter_ns", "monotonic_ns"):
            return True
        if isinstance(f, ast.Attribute) and f.attr == "time" and isinstance(f.value, ast.Call):
            # loop.time() / asyncio.get_running_loop().time()
            g = f.value.func
            return isinstance(g, ast.Attribute) and g.attr in ("get_running_loop", "get_event_loop")
    return False


def _numeric_literal(node: ast.AST, consts: frozenset[str] = frozenset()) -> bool:
    """A numeric literal, an arithmetic expression of literals, or a NAME bound only to such an expression (module- or
    function-level ``PROMPT = 1.0``): a bound moved into a constant is still a literal bound (fix wave 25, E-C)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return True
    if isinstance(node, ast.Name):
        return node.id in consts
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _numeric_literal(node.operand, consts)
    if isinstance(node, ast.BinOp):
        return _numeric_literal(node.left, consts) and _numeric_literal(node.right, consts)
    return False


def _literal_names(scope: ast.AST) -> frozenset[str]:
    """Names in ``scope`` (not descending into nested functions/classes) whose every binding is a numeric literal
    expression; a name bound anything else even once is not a constant."""
    good: set[str] = set()
    bad: set[str] = set()
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        n = todo.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        todo.extend(ast.iter_child_nodes(n))
        targets: list[ast.AST] = []
        value = None
        if isinstance(n, ast.Assign):
            targets, value = n.targets, n.value
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            targets, value = [n.target], n.value
        elif isinstance(n, (ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr, ast.With, ast.AsyncWith)):
            tgts = [i.optional_vars for i in n.items if i.optional_vars is not None] \
                if isinstance(n, (ast.With, ast.AsyncWith)) else [n.target]
            for t in tgts:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        bad.add(x.id)
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                (good if _numeric_literal(value) else bad).add(t.id)
            else:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        bad.add(x.id)
    return frozenset(good - bad)


class _ClockFlow(ast.NodeVisitor):
    """Per function: names bound to clock readings (t0 = time.monotonic()) and to clock deltas
    (took = time.monotonic() - t0); then flags upper-bound assertions of a delta against a literal."""

    def __init__(self, path: str, lines: list[str], module_consts: frozenset[str] = frozenset()):
        self.path, self.lines, self.hits = path, lines, []
        self.func_stack: list[str] = []
        self.module_consts = module_consts

    def _scan_function(self, node):
        self.func_stack.append(node.name)
        stamps: set[str] = set()
        deltas: set[str] = set()
        local = _literal_names(node)
        assigned_here = {t.id for t in ast.walk(node) if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)}
        consts = frozenset(local | (self.module_consts - assigned_here))

        def lit(e: ast.AST) -> bool:
            return _numeric_literal(e, consts)

        def is_delta(e: ast.AST) -> bool:
            if isinstance(e, ast.Name):
                return e.id in deltas
            if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Sub):
                lhs, rhs = e.left, e.right
                lc = _is_clock_call(lhs) or (isinstance(lhs, ast.Name) and lhs.id in stamps)
                rc = _is_clock_call(rhs) or (isinstance(rhs, ast.Name) and rhs.id in stamps)
                return lc and rc
            if isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id in ("round", "abs", "float"):
                return bool(e.args) and is_delta(e.args[0])
            if isinstance(e, ast.BinOp) and isinstance(e.op, (ast.Mult, ast.Div)):
                return (is_delta(e.left) and lit(e.right)) or (is_delta(e.right) and lit(e.left))
            return False

        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                name = sub.targets[0].id
                if _is_clock_call(sub.value):
                    stamps.add(name)
                elif is_delta(sub.value):
                    deltas.add(name)
            elif isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Tuple) \
                    and isinstance(sub.value, ast.Tuple):
                for t, v in zip(sub.targets[0].elts, sub.value.elts):
                    if isinstance(t, ast.Name) and _is_clock_call(v):
                        stamps.add(t.id)

        def upper_bound(cmp: ast.Compare) -> bool:
            left = cmp.left
            for op, right in zip(cmp.ops, cmp.comparators):
                if isinstance(op, (ast.Lt, ast.LtE)) and is_delta(left) and lit(right):
                    return True
                if isinstance(op, (ast.Gt, ast.GtE)) and lit(left) and is_delta(right):
                    return True
                left = right
            return False

        def exceeded(cmp: ast.Compare) -> bool:
            """The negation of an upper bound: ``delta > literal`` (a failure condition)."""
            left = cmp.left
            for op, right in zip(cmp.ops, cmp.comparators):
                if isinstance(op, (ast.Gt, ast.GtE)) and is_delta(left) and lit(right):
                    return True
                if isinstance(op, (ast.Lt, ast.LtE)) and lit(left) and is_delta(right):
                    return True
                left = right
            return False

        def fails(body: list[ast.stmt]) -> bool:
            """A block that fails the test: ``raise``, ``pytest.fail(...)`` or ``self.fail(...)``."""
            for st in body:
                for x in ast.walk(st):
                    if isinstance(x, ast.Raise):
                        return True
                    if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr == "fail":
                        return True
            return False

        def any_compare(e: ast.AST, pred) -> bool:
            return any(isinstance(c, ast.Compare) and pred(c) for c in ast.walk(e))

        for sub in ast.walk(node):
            if isinstance(sub, ast.Assert):
                if any_compare(sub.test, upper_bound):
                    self.hits.append((sub.lineno, node.name))
            elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and \
                    sub.func.attr in ("assertLess", "assertLessEqual") and len(sub.args) >= 2:
                if is_delta(sub.args[0]) and lit(sub.args[1]):
                    self.hits.append((sub.lineno, node.name))
            elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.args and \
                    sub.func.attr in ("assertTrue", "assertFalse"):
                # fix wave 25 (E-C review): assertTrue(took < 1) / assertFalse(took > 1) are the same bound
                pred = upper_bound if sub.func.attr == "assertTrue" else exceeded
                if any_compare(sub.args[0], pred):
                    self.hits.append((sub.lineno, node.name))
            elif isinstance(sub, ast.If) and any_compare(sub.test, exceeded) and fails(sub.body):
                # ``if took > 1: pytest.fail(...)`` / ``raise AssertionError`` — the same bound, spelled as a branch
                self.hits.append((sub.lineno, node.name))
        self.func_stack.pop()

    def visit_FunctionDef(self, node):
        self._scan_function(node)

    visit_AsyncFunctionDef = visit_FunctionDef


PORT_NAME = re.compile(r"(?:^|_)ports?(?:_|$)|^port|port$", re.I)
HTTP_CALLS = {"get", "post", "put", "patch", "delete", "head", "request", "stream", "urlopen", "Request", "fetch"}
HOSTPORT_STR = re.compile(r"(?:https?://)?(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\]|::1)[:](\d{2,5})\b")
RANGE_STR = re.compile(r"^\s*(\d{4,5})\s*[-:]\s*(\d{4,5})\s*$")


def _port_literal(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, int) and not isinstance(v, bool) and 1024 <= v <= 65535:
            return v
        if isinstance(v, str):
            if v.isdigit() and 1024 <= int(v) <= 65535:
                return int(v)
            m = RANGE_STR.match(v)
            if m and 1024 <= int(m.group(1)) <= 65535:
                return int(m.group(1))
    return None


def _py_port_hits(tree: ast.AST) -> list[tuple[int, str, str]]:
    hits = []
    funcs: dict[int, str] = {}
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(f):
                if hasattr(sub, "lineno"):
                    funcs.setdefault(sub.lineno, f.name)
    for node in ast.walk(tree):
        where = funcs.get(getattr(node, "lineno", 0), "<module>")
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if name in ("bind", "connect", "connect_ex", "create_connection") and node.args and \
                    isinstance(node.args[0], ast.Tuple) and len(node.args[0].elts) >= 2:
                v = _port_literal(node.args[0].elts[1])
                if v:
                    hits.append((node.lineno, where, f"{name}((host, {v}))"))
            for kw in node.keywords:
                if kw.arg and PORT_NAME.search(kw.arg):
                    v = _port_literal(kw.value)
                    if v:
                        hits.append((node.lineno, where, f"{kw.arg}={v}"))
            if name in ("get", "setdefault", "getenv") and len(node.args) >= 2 and \
                    isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) and \
                    PORT_NAME.search(node.args[0].value):
                v = _port_literal(node.args[1])
                if v:
                    hits.append((node.lineno, where, f"{node.args[0].value} defaults to {node.args[1].value!r}"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                tname = t.id if isinstance(t, ast.Name) else (t.attr if isinstance(t, ast.Attribute) else None)
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str):
                    tname = t.slice.value
                if tname and PORT_NAME.search(tname) and node.value is not None:
                    v = _port_literal(node.value)
                    if v:
                        hits.append((node.lineno, where, f"{tname} = {v}"))
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and PORT_NAME.search(k.value):
                    pv = _port_literal(v)
                    if pv:
                        hits.append((node.lineno, where, f"{k.value!r}: {pv}"))
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if name == "range" and len(node.args) == 2 and all(
                    isinstance(x, ast.Constant) and isinstance(x.value, int) for x in node.args):
                lo, hi = node.args[0].value, node.args[1].value
                if 10000 <= lo <= 65535 and 0 < hi - lo <= 200:
                    hits.append((node.lineno, where, f"port range literal range({lo}, {hi})"))
            if name in HTTP_CALLS:
                # a request sent to a literal address (data strings — parser inputs, error-message fixtures — are not)
                for arg in list(node.args[:1]) + [k.value for k in node.keywords if k.arg in ("url", "base_url")]:
                    texts = [arg.value] if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else \
                        [v.value for v in getattr(arg, "values", []) if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                    for t in texts:
                        for m in HOSTPORT_STR.finditer(t):
                            if 1024 <= int(m.group(1)) <= 65535:
                                hits.append((node.lineno, where, f"request to the literal address {m.group(0)!r}"))
    return hits


# Other languages: line-based patterns (each names the construct it catches).
OTHER_WALLCLOCK = {
    ".rs": [re.compile(r"\belapsed(?:\(\))?\s*<=?\s*(?:Duration::from_\w+\(\s*\d|\d)"),
            re.compile(r"\.elapsed\(\)\s*<=?\s*(?:Duration::from_\w+\(\s*\d|\d)")],
    ".go": [re.compile(r"(?:time\.Since\([^)]*\)|\belapsed\b|\bopen\b)\s*>=?\s*\d+\s*\*?\s*time\.\w+"),
            re.compile(r"(?:time\.Since\([^)]*\)|\belapsed\b)\s*>=?\s*time\.Duration\(\d")],
    ".ts": [re.compile(r"(?:Date\.now\(\)|performance\.now\(\))\s*-\s*\w+\s*<=?\s*\d"),
            re.compile(r"\belapsed\w*\s*<=?\s*\d")],
}
OTHER_WALLCLOCK[".mjs"] = OTHER_WALLCLOCK[".ts"]
# A bound held in a named constant whose value is a literal duration is a literal bound too (fix wave 25, E-C):
# per file, the constants are collected, then the same comparisons are matched against their names ({C}).
OTHER_CONST_DEF = {
    ".rs": re.compile(r"\bconst\s+([A-Z_][A-Z0-9_]*)\s*:\s*Duration\s*=\s*Duration::from_\w+\(\s*\d[\d_]*\s*\)\s*;"),
    ".go": re.compile(r"^\s*(?:const\s+)?([A-Za-z_]\w*)\s*(?:time\.Duration\s*)?=\s*\d+\s*\*\s*time\.\w+\s*$", re.M),
    ".ts": re.compile(r"\bconst\s+([A-Za-z_]\w*)\s*(?::\s*number\s*)?=\s*[\d_]+\s*;"),
}
OTHER_CONST_DEF[".mjs"] = OTHER_CONST_DEF[".ts"]
OTHER_WALLCLOCK_CONST = {
    ".rs": [r"\belapsed(?:\(\))?\s*<=?\s*{C}\b", r"\.elapsed\(\)\s*<=?\s*{C}\b"],
    ".go": [r"(?:time\.Since\([^)]*\)|\belapsed\b|\bopen\b)\s*>=?\s*{C}\b"],
    ".ts": [r"(?:Date\.now\(\)|performance\.now\(\))\s*-\s*\w+\s*<=?\s*{C}\b", r"\belapsed\w*\s*<=?\s*{C}\b"],
}
OTHER_WALLCLOCK_CONST[".mjs"] = OTHER_WALLCLOCK_CONST[".ts"]


def _const_wallclock_patterns(suffix: str, text: str) -> list[re.Pattern]:
    d = OTHER_CONST_DEF.get(suffix)
    names = sorted(set(d.findall(text))) if d else []
    if not names:
        return []
    alt = "(?:" + "|".join(re.escape(n) for n in names) + ")"
    return [re.compile(t.replace("{C}", alt)) for t in OTHER_WALLCLOCK_CONST.get(suffix, [])]
OTHER_PORTS = {
    ".rs": [re.compile(r"\"(?:127\.0\.0\.1|localhost|0\.0\.0\.0):([1-9]\d{3,4})\"")],
    ".go": [re.compile(r"\"(?:127\.0\.0\.1|localhost|0\.0\.0\.0)?:([1-9]\d{3,4})\""),
            re.compile(r"\b\w*[Pp]ort\w*\s*(?::?=|=)\s*([1-9]\d{3,4})\b")],
    ".ts": [re.compile(r"\b\w*PORT\w*\b[^\n]*\?\?\s*([1-9]\d{3,4})\b"), re.compile(r"\blisten\(\s*([1-9]\d{3,4})\b"),
            re.compile(r"\bfetch\(\s*[\"'`]https?://(?:127\.0\.0\.1|localhost):([1-9]\d{3,4})\b")],
}
OTHER_PORTS[".mjs"] = OTHER_PORTS[".ts"]


def _test_files() -> list[Path]:
    files = []
    for f in git("ls-files", "--cached", "--others", "--exclude-standard").splitlines():
        p = Path(f)
        if p.suffix == ".py" and "tests" in p.parts and p.parts[0] == "services":
            files.append(p)
        elif p.suffix == ".rs" and p.parts[:2] == ("services", "ledger-rust") and "tests" in p.parts:
            files.append(p)
        elif p.name.endswith("_test.go"):
            files.append(p)
        elif p.parts[:2] == ("apps", "dashboard-ts") and "tests" in p.parts and p.suffix in (".ts", ".mjs"):
            files.append(p)
    return sorted(files)


def _allowed(entries: list[dict], rule: str, path: str, func: str, text: str) -> dict | None:
    for e in entries:
        if e.get("rule") != rule or e.get("path") != path:
            continue
        if e.get("function") not in (None, func):
            continue
        if e.get("contains") and e["contains"] not in text:
            continue
        if not e.get("reason"):
            continue
        return e
    return None


def _enclosing(lines: list[str], idx: int, suffix: str) -> str:
    pat = {".rs": r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", ".go": r"^func\s+(?:\([^)]*\)\s*)?(\w+)",
           ".ts": r"^\s*(?:test|it|describe)\(\s*[\"'`]([^\"'`]+)|^\s*(?:async\s+)?function\s+(\w+)"}
    pat[".mjs"] = pat[".ts"]
    r = re.compile(pat.get(suffix, r"$^"))
    for i in range(idx, -1, -1):
        m = r.search(lines[i])
        if m:
            return next(g for g in m.groups() if g)
    return "<module>"


_N = r"(?<![\w.,#-])(\d{1,3}(?:,\d{3})+|\d{2,5})"
COUNT_CLAIM = re.compile(
    _N + r"\s*/\s*[\d,]{2,7}\s+(?:passing|passed|tests?)\b"
    r"|" + _N + r"\s+(?:tests?|unit)\b(?!-)"
    r"|" + _N + r"\s+passed\b"
    r"|(?<![\w.,#-])(\d{1,5})\s+(?:real-binary\s+)?integration\b",
    re.I)
COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _commit_exists(h: str, cache: dict) -> bool:
    if h not in cache:
        r = subprocess.run(["git", "cat-file", "-e", f"{h}^{{commit}}"], cwd=REPO, capture_output=True)
        cache[h] = r.returncode == 0
    return cache[h]


def _doc_files() -> list[Path]:
    out = [Path("README.md"), Path("docs/ci.md"), Path(".github/workflows/ci.yml")]
    out += sorted(Path(p) for p in git("ls-files", "docs/adr").splitlines() if p.endswith(".md"))
    out += sorted(Path(p) for p in git("ls-files", "services/*/README.md", "apps/*/README.md").splitlines())
    return [p for p in out if (REPO / p).exists()]


def lint_counts(allow: dict) -> list[str]:
    problems = []
    cache: dict = {}
    for rel in _doc_files():
        text = (REPO / rel).read_text()
        lines = text.splitlines()
        # paragraphs: blank-line separated (Markdown) / one comment block (YAML)
        start = 0
        paras: list[tuple[int, int]] = []
        for i, line in enumerate(lines + [""]):
            if not line.strip() or (rel.suffix in (".yml", ".yaml") and not line.lstrip().startswith("#")):
                if i > start:
                    paras.append((start, i))
                start = i + 1
        for a, b in paras:
            block = "\n".join(lines[a:b])
            if rel.suffix in (".yml", ".yaml") and not block.lstrip().startswith("#"):
                continue
            # an id made only of digits counts too (fix wave 26b, W26-ST): ~1 short id in 27 has no a-f, and skipping
            # those lost the pin then; a digit run that is not a commit still resolves to nothing
            pinned = any(_commit_exists(h, cache) for h in COMMIT_RE.findall(block))
            if pinned:
                continue
            # matched over the whole paragraph (line breaks as spaces, offsets kept), so a count wrapped across two
            # lines ("`cargo test`: 84\n  passed") is caught too (fix wave 25, E-C); reported at its first line
            flat = block.replace("\n", " ")
            for m in COUNT_CLAIM.finditer(flat):
                i = a + block.count("\n", 0, m.start())
                snippet = " ".join(m.group(0).split())
                e = _allowed(allow.get("allow", []), "L3-counts", str(rel), "<doc>", lines[i])
                if e:
                    continue
                problems.append(violation("L3-counts", f"{rel}:{i + 1}",
                                          f"hand-written test count {snippet!r} — link docs/test-counts.md, or "
                                          f"tie a historical count to its commit"))
    return problems


def _source_strings(root: Path) -> list[str]:
    """Every string literal in the Python files under `root`, an f-string as its constant text (placeholders
    dropped, so a reason regex anchored at the start still matches it)."""
    import ast
    out: list[str] = []
    for f in sorted(root.rglob("*.py")):
        if any(part in (".venv", "node_modules", "__pycache__") for part in f.parts):
            continue
        try:
            tree = ast.parse(f.read_text(), str(f))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                out.append("".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append(node.value)
    return out


def lint_expected_skips(allow: dict) -> list[str]:
    """Fix wave 26b (W25-EB-R3): an expected skip that no source of its suite can produce is stale (delivery-py kept
    one for a test removed in d0876fd). Each `reason_regex` must match a string literal, or an f-string's constant
    text, in that suite's directory (services/<name> or apps/<name>); with its trailing `$` dropped when matched
    against an f-string's text, whose placeholders may have stood at the end."""
    problems = []
    for suite, items in sorted(allow.get("expected_skips", {}).items()):
        name = suite.partition(":")[2]
        root = next((REPO / d / name for d in ("services", "apps") if (REPO / d / name).is_dir()), None)
        strings = _source_strings(root) if root else []
        for e in items:
            rx = e.get("reason_regex", "")
            loose = rx[:-1] if rx.endswith("$") and not rx.endswith("\\$") else rx
            if not any(re.search(rx, s) or re.search(loose, s) for s in strings):
                where = root.relative_to(REPO) if root else f"<no directory for {suite}>"
                problems.append(violation("L9-allowlist", "devtools/hygiene_allowlist.json",
                                          f"expected skip of {suite} matches no skip reason in {where}: {rx!r}"))
    return problems


SHARED_TESTS = ("test_live_graceful_close_module.py", "test_fix21_graceful_close.py")
SERVICE_CONSTANTS = re.compile(r'^(MODULE|PROTOCOL) = "[\w.]+"$', re.M)


def lint_shared() -> list[str]:
    import hashlib
    problems = []
    svc = REPO / "services"
    gc = sorted(svc.glob("*/src/graceful_close.py")) + sorted(svc.glob("*/src/*/graceful_close.py"))
    digests = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in gc}
    pin_files = sorted(svc.glob("*/tests/test_live_graceful_close_module.py"))
    pins = set()
    for f in pin_files:
        m = re.search(r'PINNED_SHA256 = "([0-9a-f]{64})"', f.read_text())
        if m:
            pins.add(m.group(1))
    if (gc or pin_files) and len(pins) != 1:
        problems.append(violation("L4-shared", "graceful_close", f"the module tests pin {len(pins)} different sha256s"))
    for p, d in digests.items():
        if pins and d not in pins:
            problems.append(violation("L4-shared", str(p.relative_to(REPO)), "differs from the pinned graceful_close.py"))
    groups = {
        "test_live_graceful_close_module.py": pin_files,
        "test_fix21_graceful_close.py": sorted(svc.glob("*/tests/test_fix21_graceful_close.py"))
        + sorted(svc.glob("delivery-py/tests/test_live_graceful_close.py")),
        "_procinfo.py": sorted(svc.glob("*/tests/_procinfo.py")),
        "test_procinfo.py": sorted(svc.glob("*/tests/test_procinfo.py")),
        "test_shared_ports.py": sorted(svc.glob("*/tests/test_shared_ports.py")),
    }
    for name, files in groups.items():
        norm = {f: SERVICE_CONSTANTS.sub(r'\1 = "<service>"', f.read_text()) for f in files}
        ref = next(iter(norm.values()), None)
        for f, t in norm.items():
            if t != ref:
                problems.append(violation("L4-shared", str(f.relative_to(REPO)),
                                          f"not identical to {files[0].relative_to(REPO)} (shared file {name})"))
    return problems


def cmd_lint(a: argparse.Namespace) -> int:
    allow = load_allowlist()
    entries = allow.get("allow", [])
    problems: list[str] = []
    used: set[int] = set()
    rules = set(a.rules.split(",")) if a.rules else {"L1", "L2", "L3", "L4"}
    for rel in _test_files():
        path = REPO / rel
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        if rel.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError as e:
                problems.append(violation("L0-parse", str(rel), str(e)))
                continue
            if "L1" in rules:
                v = _ClockFlow(str(rel), lines, _literal_names(tree))
                v.visit(tree)
                for ln, func in v.hits:
                    e = _allowed(entries, "L1-wallclock", str(rel), func, lines[ln - 1])
                    if e:
                        used.add(id(e))
                        continue
                    problems.append(violation("L1-wallclock", f"{rel}:{ln}", f"in {func}: {lines[ln - 1].strip()[:140]}"))
            if "L2" in rules:
                for ln, func, what in _py_port_hits(tree):
                    e = _allowed(entries, "L2-ports", str(rel), func, lines[ln - 1])
                    if e:
                        used.add(id(e))
                        continue
                    problems.append(violation("L2-ports", f"{rel}:{ln}", f"in {func}: hard-coded port ({what})"))
        else:
            suffix = rel.suffix
            wallclock = {suffix: OTHER_WALLCLOCK.get(suffix, []) + _const_wallclock_patterns(suffix, text)}
            for i, line in enumerate(lines):
                if line.lstrip().startswith(("//", "#", "*")):
                    continue
                for rule, table, label in (("L1-wallclock", wallclock, "wall-clock upper bound"),
                                           ("L2-ports", OTHER_PORTS, "hard-coded port")):
                    if rule[:2] not in rules:
                        continue
                    for pat in table.get(suffix, []):
                        if pat.search(line):
                            func = _enclosing(lines, i, suffix)
                            e = _allowed(entries, rule, str(rel), func, line)
                            if e:
                                used.add(id(e))
                                break
                            problems.append(violation(rule, f"{rel}:{i + 1}", f"in {func}: {label}: {line.strip()[:140]}"))
                            break
    if "L3" in rules:
        problems += lint_counts(allow)
    if "L4" in rules:
        problems += lint_shared()
    if a.strict_allowlist:
        for e in entries:
            if e.get("rule") in ("L1-wallclock", "L2-ports") and id(e) not in used:
                problems.append(violation("L9-allowlist", e.get("path", "?"), f"allowlist entry matches nothing: {e}"))
        problems += lint_expected_skips(allow)
    by_service: dict[str, int] = {}
    for line in problems:
        where = line.split(" ", 3)[2]
        parts = where.split("/")
        key = "/".join(parts[:2]) if parts[0] in ("services", "apps") else parts[0].split(":")[0]
        by_service[key] = by_service.get(key, 0) + 1
    for line in problems:
        print(line)
    print(f"hygiene_check lint: {len(problems)} violation(s)" +
          ("".join(f"\n  {k}: {v}" for k, v in sorted(by_service.items())) if problems else ""))
    return 1 if problems else 0


SUITES = tuple(f"python:{s}" for s in PY_SERVICES) + ("rust:ledger-rust", "go:orchestrator-go", "node:dashboard-ts")


def cmd_counts(a: argparse.Namespace) -> int:
    rows = read_counts_doc()
    if a.check:
        missing = [s for s in SUITES if s not in rows]
        extra = [s for s in rows if s not in SUITES]
        for s in missing:
            print(f"HYGIENE R6-counts docs/test-counts.md: no row for suite {s}")
        for s in extra:
            print(f"HYGIENE R6-counts docs/test-counts.md: row for unknown suite {s}")
        # the platform-only column is generated from the allowlist: the two must agree, and every entry is
        # well-formed (fix wave 26a, W26-5)
        allow = load_allowlist()
        notes = read_counts_notes()
        bad = []
        for s, entries in allow.get("platform_only_tests", {}).items():
            if s not in SUITES:
                bad.append(f"allowlist platform_only_tests names unknown suite {s}")
            for e in entries:
                if not (isinstance(e.get("test"), str) and e["test"] and isinstance(e.get("only_on"), list)
                        and e["only_on"] and all(isinstance(o, str) and o for o in e["only_on"]) and e.get("why")):
                    bad.append(f"allowlist platform_only_tests entry for {s} needs test, only_on and why: {e}")
        if "count_os_delta" in allow:
            bad.append("allowlist key count_os_delta is no longer read: name the tests in platform_only_tests")
        for s in rows:
            want = platform_note(platform_only(allow, s))
            if notes.get(s, "") != want:
                bad.append(f"row `{s}` platform-only column is {notes.get(s, '')!r}, the allowlist says {want!r}; "
                           f"regenerate with --counts write")
        for b in bad:
            print(f"HYGIENE R6-counts docs/test-counts.md: {b}")
        if missing or extra or bad:
            return 1
        print(f"docs/test-counts.md: {len(rows)} suites, one row each")
        return 0
    for k, v in sorted(rows.items()):
        print(f"{k}\t{v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one suite under the hygiene rules R1-R6")
    r.add_argument("--suite", required=True, help="suite id, e.g. python:detection-py, rust:ledger-rust")
    r.add_argument("--kind", required=True, choices=("pytest", "cargo", "go", "node", "none"),
                   help="how tests and skips are counted; none = not a test suite (R5/R6 off), e.g. a live run")
    r.add_argument("--cwd", help="directory to run in, relative to the repo root")
    r.add_argument("--work-dir", help="where the private TMPDIR is made (default $RUNNER_TEMP or ~/.cache/zbm-hygiene)")
    r.add_argument("--allow-ignored", action="append", default=[], help="git-ignored path the run may create")
    r.add_argument("--tmp-ignore", action="append", default=[],
                   help="regex of /tmp entry names another process on this machine creates (never in CI)")
    r.add_argument("--system-tmp", help="the directory R3 watches for new entries (default /tmp; the self-test "
                                        "points it at a private directory so other processes' /tmp entries cannot "
                                        "fail it; never in CI)")
    r.add_argument("--counts", choices=("check", "write", "off"), default="check")
    r.add_argument("--count-os", help="OS whose platform-only tests R6 applies (default: this one; the self-test "
                                      "uses it to check another OS's rules)")
    r.add_argument("command", nargs=argparse.REMAINDER)
    lp = sub.add_parser("lint", help="static rules L1-L4 over the whole tree")
    lp.add_argument("--rules", help="comma list of L1,L2,L3,L4 (default all)")
    lp.add_argument("--strict-allowlist", action="store_true", help="also fail on allowlist entries that match nothing")
    c = sub.add_parser("counts", help="show docs/test-counts.md")
    c.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    return {"run": cmd_run, "lint": cmd_lint, "counts": cmd_counts}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
