"""
TEST-ONLY fakes (spec §F, G3: they live here, never in src/). Each has a switch so a test can make exactly one
gate input absent.

- ``FakeLedgerClient``: the finance-py double (field rules, idempotency, ``fail_all`` / ``fail_on_type``).
- ``FakeDockerCli``: the argv-level double of the Docker CLI (spec C.2). It records EVERY argv the adapter emits.
  ``docker run`` creates a local directory standing in for the run's volume; ``docker exec`` runs the argv locally
  in that directory with deer-flow's virtual prefix ``/mnt/user-data/workspace`` mapped to it (and mapped back in
  the output); ``docker cp`` moves tar streams; ``docker rm``/``volume rm`` remove the directory; ``docker info``
  reports the configured daemon state. It does NOT enforce the container's isolation — that is exactly what only a
  machine with Docker can prove (tests/test_live_docker.py).
- ``FakeChatModel``: the scripted deterministic model behind ``EgressChatModel`` (a ``ChatBackend``), driven by a
  JSON scenario: a flat list of answers, each ``{"tool_calls": [...]}`` or ``{"text": "..."}``; an
  ``{"advance_clock_s": N}`` entry moves the harness clock before the next answer (S6).
- ``FakeMemory``: proves the ``MemoryPort`` contract (A9).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import _tmproot  # noqa: E402  (tests/_tmproot.py; L4)

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zbm_delivery.ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, event_field_problems, payload_sha256  # noqa: E402
from zbm_delivery.policy import WORKSPACE  # noqa: E402
from zbm_delivery.ports import Ack, ChatAnswer, ChatTurn, ExecResult, Fact, MemoryContext, Principal  # noqa: E402


@dataclass
class FakeLedgerClient:
    events: list = field(default_factory=list)
    fail_all: bool = False
    fail_on_type: Optional[str] = None
    readable: bool = True
    calls: int = 0
    by_id: dict = field(default_factory=dict)

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        self.calls += 1
        if self.fail_all or (self.fail_on_type and event_type == self.fail_on_type):
            raise LedgerNotRecorded("simulated ledger outage (test double)")
        bad = event_field_problems(event_id, department, event_type, actor, subject_id, summary)
        if bad:
            raise LedgerNotRecorded(f"HTTP 400 (invalid {bad}, test double)")
        entry = {"event_id": event_id, "department": department, "event_type": event_type, "actor": actor,
                 "subject_id": subject_id, "payload_sha256": payload_sha256(payload), "summary": summary}
        e = self.by_id.get(event_id)
        if e is not None:
            if {k: e[k] for k in entry} == entry:
                return
            raise LedgerConflict("409 (test double)")
        rec = {**entry, "payload": payload}
        self.events.append(rec)
        self.by_id[event_id] = rec

    def verify(self) -> bool:
        return not self.fail_all

    def entries(self) -> list:
        if self.fail_all or not self.readable:
            raise LedgerQueryFailed("simulated ledger outage (test double)")
        return [{k: v for k, v in e.items() if k != "payload"} for e in self.events]

    def of_type(self, t: str) -> list:
        return [e for e in self.events if e["event_type"] == t]


def _exited_unreaped(proc: subprocess.Popen) -> bool:
    """True once ``proc`` has exited, WITHOUT reaping it (its pid — the group id — stays reserved). Where
    ``waitid``/``WNOWAIT`` is missing, ``poll()`` (which reaps; the group kill that follows is then best effort)."""
    if proc.returncode is not None:
        return True
    if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        try:
            return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
    return proc.poll() is not None


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the process group ``proc`` leads (``start_new_session``); a group already gone is not an error."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


_GNU_TOOLS: dict = {}


def _host_tool_is_gnu(tool: str) -> bool:
    if tool not in _GNU_TOOLS:
        try:
            out = subprocess.run([tool, "--version"], capture_output=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = b""
        _GNU_TOOLS[tool] = b"GNU" in out
    return _GNU_TOOLS[tool]


def _gnu_find(cmd: list) -> list:
    """Wave 26b (CI #3 macos-26): the sandbox image is Debian, so the adapter's ``find … -printf '%p\\n'`` is GNU
    find; on a host whose find is not (BSD find has no -printf) the double runs ``-print``, which prints exactly
    ``%p\\n``."""
    if cmd[:1] != ["find"] or "-printf" not in cmd or _host_tool_is_gnu("find"):
        return cmd
    i = cmd.index("-printf")
    if cmd[i + 1:i + 2] != ["%p\n"]:
        return cmd                     # any other format: left to fail loudly, never guessed
    return cmd[:i] + ["-print"] + cmd[i + 2:]


def _gnu_mv_no_target_dir(src: str, dst: str) -> ExecResult:
    """Wave 26b (CI #3 macos-26): GNU ``mv -f -T -- SRC DST`` (the adapter's ``put_bytes``, R7) on a host whose mv
    has no -T (BSD mv). -T: DST is the name itself, never a directory to move INTO — a directory at DST is refused
    for a non-directory SRC (an empty one is replaced by a directory SRC, as rename(2) does); a file or symlink at DST
    is replaced in one rename(2), the link itself, never its target."""
    try:
        if os.path.isdir(dst) and not os.path.islink(dst) and not os.path.isdir(src):
            return ExecResult(1, b"", f"mv: cannot overwrite directory '{dst}' with non-directory\n".encode())
        os.replace(src, dst)
    except OSError as exc:
        return ExecResult(1, b"", f"mv: {exc.strerror}\n".encode())
    return ExecResult(0, b"", b"")


class FakeDockerCli:
    """See the module docstring. ``daemon=False`` simulates ``Cannot connect to the Docker daemon``."""

    def __init__(self, root: str, daemon: bool = True):
        self.root = os.path.abspath(root)
        self.daemon = daemon
        self.calls: list[list[str]] = []
        self.containers: dict[str, str] = {}      # name -> local volume dir
        self.labels: dict[str, str] = {}          # container name -> run label value
        self.volumes: dict[str, str] = {}         # volume name -> run label value
        self.binds: dict[str, dict[str, str]] = {}   # container name -> {dst: host src} for the read-only bind mounts
        self.killed: set[str] = set()             # containers a `docker kill` stopped (every later exec fails)
        self.procs: dict[str, list] = {}          # container name -> running Popen objects (kill terminates them)
        self._procs_lock = threading.Lock()       # wave 25 (M1): a group is killed only while its leader is unreaped
        self.env_files: dict[str, dict] = {}      # container name -> the --env-file's variables (wave 22: CI=1 etc.)
        self.exec_fail_next: Optional[int] = None
        os.makedirs(self.root, exist_ok=True)

    # --- helpers ------------------------------------------------------------------------------------------------------

    def _vol(self, name: str) -> str:
        return os.path.join(self.root, "vol", name)

    def _map_in(self, s: str, vol: str, binds: Optional[dict] = None) -> str:
        s = s.replace(WORKSPACE, vol)
        for dst, src in (binds or {}).items():
            s = s.replace(dst, src)
        return s

    def _map_out(self, b: bytes, vol: str) -> bytes:
        """Every host spelling of the volume back to the container path (wave 21, N20-D-4): the volume as the
        double named it AND its realpath — a process started in a directory reached through a symlink (``TMPDIR``
        behind a link, macOS ``/var`` → ``/private/var``) reports the physical path (``getcwd``), so mapping only
        the spelled path handed host paths back to the engine and its containment checks."""
        spellings = {vol, os.path.realpath(vol)}
        for s in sorted(spellings, key=len, reverse=True):
            b = b.replace(s.encode(), WORKSPACE.encode())
        return b

    def run(self, argv, *, timeout_s: float, stdin: bytes | None = None, output_cap: int = 1024 * 1024) -> ExecResult:
        argv = [str(a) for a in argv]
        self.calls.append(list(argv))
        if not self.daemon:
            return ExecResult(1, b"", b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n")
        if argv[0] == "info":
            return ExecResult(0, b"29.4.3\n", b"")
        if argv[0] == "run":
            name = argv[argv.index("--name") + 1]
            vol = self._vol(name)
            os.makedirs(vol, exist_ok=True)
            self.containers[name] = vol
            self.killed.discard(name)
            if "--label" in argv:
                self.labels[name] = argv[argv.index("--label") + 1].split("=", 1)[1]
            if "--env-file" in argv and os.path.isfile(argv[argv.index("--env-file") + 1]):
                # wave 22: the container's environment comes from the env file, as with the real CLI (PATH and HOME
                # stay the double's: the host's toolchains and the volume as HOME stand in for the image's)
                file_env = {}
                with open(argv[argv.index("--env-file") + 1], encoding="ascii") as fh:
                    for ln in fh.read().splitlines():
                        k, sep, v = ln.partition("=")
                        if sep and k not in ("PATH", "HOME"):
                            file_env[k] = v
                self.env_files[name] = file_env
            for a in argv:
                if a.startswith("type=volume,"):
                    opts = dict(kv.split("=", 1) for kv in a.split(",") if "=" in kv)
                    vname = opts.get("src", "")
                    self.volumes[vname] = opts.get("volume-label", "=").split("=", 1)[1]
                elif a.startswith("type=bind,"):
                    opts = dict(kv.split("=", 1) for kv in a.split(",") if "=" in kv)
                    if opts.get("dst") and opts.get("src"):
                        self.binds.setdefault(name, {})[opts["dst"]] = opts["src"]
            cid = hashlib.sha256(name.encode()).hexdigest()
            return ExecResult(0, (cid + "\n").encode(), b"")
        if argv[0] == "ps":
            if "-q" in argv and "--format" in argv:
                # the real CLI (>= 23) ignores --format when -q is set and prints IDs only (N19-A-5)
                ids = [hashlib.sha256(n.encode()).hexdigest()[:12] for n in self.containers]
                return ExecResult(0, ("\n".join(ids) + ("\n" if ids else "")).encode(),
                                  b"WARNING: Ignoring custom format, because both --format and --quiet are set.\n")
            label = [a for a in argv if a.startswith("label=")]
            key = label[0][len("label="):] if label else None
            sep = "\t" if "--format" in argv and "\t" in argv[argv.index("--format") + 1] else " "
            lines = [f"{n}{sep}{self.labels.get(n, '')}" for n in self.containers if key is None or n in self.labels]
            return ExecResult(0, ("\n".join(lines) + ("\n" if lines else "")).encode(), b"")
        if argv[0] == "kill":
            name = argv[-1]
            if name not in self.containers:
                return ExecResult(1, b"", b"Error response from daemon: No such container\n")
            self.killed.add(name)
            with self._procs_lock:
                # wave 25 (scout B M1): the container stops — every process of every command it runs, not only the
                # one the double started (a `node --test` file process, a cargo/go test binary outlived the suite)
                for proc in self.procs.pop(name, []):
                    _kill_group(proc)
            return ExecResult(0, (name + "\n").encode(), b"")
        if argv[0] == "rm":
            name = argv[-1]
            self.containers.pop(name, None)
            self.labels.pop(name, None)
            self.binds.pop(name, None)
            return ExecResult(0, b"", b"")
        if argv[0] == "volume" and argv[1] == "ls":
            if "--format" in argv and "\t" in argv[argv.index("--format") + 1]:
                lines = [f"{n}\t{lbl}" for n, lbl in self.volumes.items()]
            else:
                lines = [n for n in self.volumes]
            return ExecResult(0, ("\n".join(lines) + ("\n" if lines else "")).encode(), b"")
        if argv[0] == "volume":
            self.volumes.pop(argv[-1], None)
            name = argv[-1].replace("dlv-ws-", "dlv-")
            vol = self._vol(name)
            if os.path.isdir(vol):
                shutil.rmtree(vol)
            return ExecResult(0, b"", b"")
        if argv[0] == "cp":
            return self._cp(argv, stdin)
        if argv[0] == "exec":
            return self._exec(argv, timeout_s, output_cap, stdin)
        return ExecResult(125, b"", b"unknown docker subcommand (test double)\n")

    def _cp(self, argv, stdin) -> ExecResult:
        src, dst = argv[1], argv[2]
        if src == "-":
            name, _, path = dst.partition(":")
            vol = self.containers.get(name)
            if vol is None:
                return ExecResult(1, b"", b"no such container\n")
            dest = self._map_in(path, vol)
            os.makedirs(dest, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(stdin or b""), mode="r") as tar:
                tar.extractall(dest, filter="data")
            return ExecResult(0, b"", b"")
        name, _, path = src.partition(":")
        vol = self.containers.get(name)
        if vol is None:
            return ExecResult(1, b"", b"no such container\n")
        local = self._map_in(path, vol)
        if not os.path.exists(local):
            return ExecResult(1, b"", b"no such path\n")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(local, arcname=os.path.basename(local.rstrip("/")))
        return ExecResult(0, buf.getvalue(), b"")

    def _exec(self, argv, timeout_s, output_cap, stdin=None) -> ExecResult:
        i = 1
        cwd = WORKSPACE
        env = {}
        interactive = False
        while argv[i].startswith("-"):
            if argv[i] == "-i":
                interactive = True
                i += 1
            elif argv[i] == "--user":
                i += 2
            elif argv[i] == "-w":
                cwd = argv[i + 1]
                i += 2
            elif argv[i] == "--env":
                k, _, v = argv[i + 1].partition("=")
                env[k] = v
                i += 2
            else:
                i += 1
        name = argv[i]
        cmd = argv[i + 1:]
        vol = self.containers.get(name)
        if vol is None:
            return ExecResult(1, b"", b"no such container\n")
        if name in self.killed:
            return ExecResult(1, b"", f"Error response from daemon: container {name} is not running\n".encode())
        secs = timeout_s
        if cmd[:3] == ["timeout", "-k", "5"]:
            secs = float(cmd[3])
            cmd = cmd[4:]
        binds = self.binds.get(name)
        cmd = [self._map_in(a, vol, binds) for a in cmd]
        if interactive and stdin:
            # wave 22: what the process reads on stdin names container paths, like its argv — mapped the same way
            stdin = self._map_in(stdin.decode("utf-8", "surrogateescape"), vol, binds).encode("utf-8", "surrogateescape")
        if cmd[0] in ("pytest",):
            # the image's `pytest` script has the script's bin dir as sys.path[0], never the cwd: -P makes the
            # double's `python -m pytest` behave the same (nothing in the service directory shadows a module)
            cmd = [sys.executable, "-P", "-m", "pytest", *cmd[1:]]
        elif cmd[0] in ("python", "python3"):
            cmd = [sys.executable, *cmd[1:]]
        local_cwd = self._map_in(cwd, vol)
        if not os.path.isdir(local_cwd):
            return ExecResult(1, b"", b"cwd missing\n")
        if self.exec_fail_next is not None:
            code, self.exec_fail_next = self.exec_fail_next, None
            return ExecResult(code, b"", b"simulated exec failure\n")
        cmd = _gnu_find(cmd)
        if cmd[:4] == ["mv", "-f", "-T", "--"] and len(cmd) == 6 and not _host_tool_is_gnu("mv"):
            return _gnu_mv_no_target_dir(os.path.join(local_cwd, cmd[4]), os.path.join(local_cwd, cmd[5]))
        full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": vol, "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1", **self.env_files.get(name, {}), **self.toolchain_env(), **env}
        try:
            # wave 25 (scout B M1): its own process group (session), so the deadline and `docker kill` reach every
            # process the command starts — as the real container's end does — never only the direct child
            proc = subprocess.Popen(cmd, cwd=local_cwd, stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full_env, start_new_session=True)
        except OSError as exc:
            return ExecResult(127, b"", f"{type(exc).__name__}\n".encode())
        with self._procs_lock:
            self.procs.setdefault(name, []).append(proc)
        stdout, stderr, timed_out = self._finish(proc, (stdin or b"") if interactive else None, secs)
        if timed_out:
            return ExecResult(124, self._map_out(stdout, vol), self._map_out(stderr, vol), True)
        if name in self.killed:
            return ExecResult(137, self._map_out(stdout, vol), self._map_out(stderr, vol) + b"\n[killed]\n")
        out, err = self._map_out(stdout, vol), self._map_out(stderr, vol)
        truncated = len(out) > output_cap
        return ExecResult(proc.returncode, out[:output_cap], err[:output_cap], False, truncated)

    def _finish(self, proc: subprocess.Popen, stdin: Optional[bytes], secs: float) -> tuple[bytes, bytes, bool]:
        """Feed ``stdin``, wait for the command's own process until ``secs``, then kill its whole process group — on
        the deadline AND after a normal exit — and only then read the pipes to their end (a left-behind process
        holding them no longer delays the answer). Stricter than a real container after a normal exit: there a
        process the command left in the background lives until the container stops; the double has no container
        process to tie it to, and the engine never leaves one on purpose. The group is killed while its leader is
        still unreaped (``waitid(WNOWAIT)``), so its id cannot have been reused by an unrelated process; ``docker
        kill`` takes the same lock."""
        out: list[bytes] = []
        err: list[bytes] = []

        def pump(stream, sink):
            sink.append(stream.read())

        def feed():
            try:
                proc.stdin.write(stdin or b"")
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
        threads = [threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
                   threading.Thread(target=pump, args=(proc.stderr, err), daemon=True)]
        if proc.stdin is not None:
            threads.append(threading.Thread(target=feed, daemon=True))
        for t in threads:
            t.start()
        deadline = time.monotonic() + secs
        timed_out = False
        while not _exited_unreaped(proc):
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.01)
        with self._procs_lock:
            _kill_group(proc)
            proc.wait()
            for procs in self.procs.values():
                if proc in procs:
                    procs.remove(proc)
        for t in threads:
            t.join()
        return b"".join(out), b"".join(err), timed_out

    @staticmethod
    def toolchain_env() -> dict:
        """What the sandbox IMAGE provides and this double must stand in for: the host's rust toolchain (rustup needs
        its home when HOME is the volume) and a Go build cache shared by every fake container of the session (the
        image's cache lives under the container HOME; a cold cache per double would rebuild the race runtime each
        time). Nothing here reaches the engine's argv or the seed. Wave 24 (E6 sweep): TMPDIR — the image's /tmp goes
        with its container; the double's processes run on the host, so their stand-in is the suite's temp root (the
        suite removes it), never the host's /tmp (a go test the double kills on its deadline left go-build* there)."""
        real_home = os.path.expanduser("~")
        # wave 25 (scout B Low, R-HYGIENE): the session's own cache, removed with the session root — no longer left in
        # the host temp dir; DLV_TEST_GOCACHE names a cache an operator wants to keep across sessions (outside TMPDIR)
        gocache = os.environ.get("DLV_TEST_GOCACHE") or os.path.join(_tmproot.SESSION_TMP, "dlv-test-gocache")
        os.makedirs(gocache, exist_ok=True)
        # wave 26b (E-B DLV-HOST): cargo's own home (registry, caches, config) is the session's too, never the host's
        # ~/.cargo; only the toolchain (RUSTUP_HOME) is the host's, used read-only like the image's. The toy-rs fixture
        # has no dependencies and every cargo call is --locked --offline, so an empty home is all it needs.
        cargo_home = os.environ.get("DLV_TEST_CARGO_HOME") or os.path.join(_tmproot.SESSION_TMP, "dlv-test-cargo-home")
        os.makedirs(cargo_home, exist_ok=True)
        return {"RUSTUP_HOME": os.environ.get("RUSTUP_HOME", os.path.join(real_home, ".rustup")),
                "CARGO_HOME": cargo_home,
                "GOCACHE": gocache, "GOPATH": os.path.join(gocache, "gopath"), "TMPDIR": tempfile.gettempdir()}

    # --- inspection ---------------------------------------------------------------------------------------------------

    def argv_of(self, sub: str) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == sub]

    def exec_commands(self) -> list[str]:
        """The ``/bin/bash -lc <cmd>`` strings the adapter asked the container to run."""
        out = []
        for c in self.argv_of("exec"):
            if "-lc" in c:
                out.append(c[c.index("-lc") + 1])
        return out


class FakeChatModel:
    """A ``ChatBackend`` driven by a scenario (see the module docstring)."""

    provider = "fake"
    model = "fake-engineer"
    fake = True

    def __init__(self, scenario: list[dict], clock=None):
        self.scenario = list(scenario)
        self.clock = clock
        self.calls: list[ChatTurn] = []
        self.n = 0

    @classmethod
    def from_file(cls, path: str, clock=None) -> "FakeChatModel":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh), clock)

    def complete(self, turn: ChatTurn) -> ChatAnswer:
        self.calls.append(turn)
        while self.n < len(self.scenario) and "advance_clock_s" in self.scenario[self.n]:
            if self.clock is not None:
                self.clock.advance(seconds=self.scenario[self.n]["advance_clock_s"])
            self.n += 1
        if self.n >= len(self.scenario):
            return ChatAnswer("BLOCKED: scenario exhausted", [], 1, 1)
        step = self.scenario[self.n]
        self.n += 1
        calls = []
        for k, tc in enumerate(step.get("tool_calls", [])):
            calls.append({"id": f"call-{self.n}-{k}", "name": tc["name"], "args": tc.get("args", {})})
        return ChatAnswer(step.get("text", ""), calls, int(step.get("tokens_in", 10)), int(step.get("tokens_out", 5)))

    def seen_tool_results(self) -> list[str]:
        out = []
        for t in self.calls:
            for m in t.messages:
                if m["role"] == "tool":
                    out.append(m["content"])
        return out


class FakeMemory:
    """A backend that honours the MemoryPort contract (per-principal, record-first, purge verified)."""

    name = "fake"

    def __init__(self):
        self.store: dict[tuple[str, str], list[Fact]] = {}
        self.purges: list[str] = []

    def _key(self, principal: Principal, thread_id: str) -> tuple[str, str]:
        return (f"{principal.tenant}:{principal.id}", thread_id)

    def context(self, principal: Principal, thread_id: str) -> MemoryContext:
        facts = self.store.get(self._key(principal, thread_id), [])
        if not facts:
            return MemoryContext(available=True, facts_sha256=None, text=None)
        text = "\n".join(f"{f.key}: {f.text}" for f in facts)
        return MemoryContext(available=True, facts_sha256=hashlib.sha256(text.encode()).hexdigest(), text=text)

    def remember(self, principal: Principal, thread_id: str, facts: list[Fact], request_id: str, record) -> Ack:
        try:
            record("memory_fact_recorded", {"principal_sha256": hashlib.sha256(self._key(principal, thread_id)[0].encode()).hexdigest(),
                                           "count": len(facts), "request_id": request_id})
        except Exception:  # noqa: BLE001 - not recorded → not written
            return Ack(available=True, ok=False)
        self.store.setdefault(self._key(principal, thread_id), []).extend(facts)
        return Ack(available=True, ok=True, reference=request_id)

    def forget(self, principal: Principal, thread_id: str, record) -> Ack:
        self.store.pop(self._key(principal, thread_id), None)
        ok = not self.context(principal, thread_id).text
        record("memory_purge_verified" if ok else "memory_purge_incomplete", {"thread_id": thread_id})
        self.purges.append(thread_id)
        return Ack(available=True, ok=ok)


SECRET_RE = re.compile(r"sk-test-[A-Za-z0-9]{20,}")
