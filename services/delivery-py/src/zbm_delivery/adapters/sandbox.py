"""
Sandbox adapter — Docker only, ours (spec §C.2, D2; F-01, F-02, ST-03).

``ZbmDockerSandboxProvider`` (``deerflow.sandbox.sandbox_provider.SandboxProvider``) and ``ZbmDockerSandbox``
(``deerflow.sandbox.sandbox.Sandbox``) drive the ``docker`` CLI with fixed argv (``shell=False``) through the
``DockerCli`` port; never a mounted ``docker.sock``, never ``--privileged``, never ``seccomp=unconfined``, never a
tag-only image, never ``LocalSandboxProvider``. No daemon → ``DockerUnavailable`` → no run starts (C.1.11).

Container per run: ``docker run -d --name dlv-<run_id> --label zbm.dlv.run=<run_id> --user 65532:65532
--cap-drop=ALL --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,nosuid,size=512m --pids-limit 512
--memory <mem> --cpus <cpus> --network none --mount type=volume,src=dlv-ws-<run_id>,dst=/mnt/user-data/workspace,
volume-label=zbm.dlv.run=<run_id> --mount type=bind,src=<skills root>,dst=/mnt/skills,ro --env-file <allowlisted env
file> <image>@sha256:<digest> sleep infinity``. Round 18 R4: the network is ``none`` and nothing else (the LLM is
reached by the engine process on the host through the egress adapter; the sandbox image ships no curl/wget); the
copied workspace holds no ``.git`` (verified after copy-in), so a ``git push`` inside the box has no repository.
The workspace is deer-flow's virtual prefix ``/mnt/user-data/workspace`` (config/paths.py VIRTUAL_PATH_PREFIX; the
bash tool prepends ``cd`` to it and the file tools address it) — the spec's ``/workspace`` would leave every DF
tool pointing at a path that does not exist (ADR 0011, spec defect). The volume is populated by ``docker cp`` of a
tar stream whose entries are owned by 65532 (a bind mount of the host repo is never made). Every exec is recorded
``sandbox_exec_requested`` before spawn and ``sandbox_exec_completed`` after; output is capped at 1 MiB with
``truncated``; the in-container ``timeout`` binary bounds the command at ``min(timeout, DLV_CMD_TIMEOUT_S,
remaining wall clock)`` with the host-side subprocess timeout as the backstop.

deer-flow calls ``provider.release(sandbox_id)`` after EVERY agent turn (sandbox/middleware.py after_agent); that is
a lease return here — the container lives for the run and is torn down by ``destroy`` (``docker rm -f`` + ``docker
volume rm``) only after the runner copied the diff and the evidence out (ADR 0011 choice). Round 18 R6: ``destroy``
checks both exit codes and records ``sandbox_release_failed`` (raising) when either fails; ``reap`` removes every
container and volume carrying our run label (at service start and on ``stop()``), one ``sandbox_reaped`` event each.

Path containment (R7): every path is resolved INSIDE the container on its longest existing prefix; an unresolvable
path is refused (never passed through). A write lands in a staging directory first and is moved into place by an
in-container ``mv`` after the destination is re-resolved — the resolve→mv window is milliseconds and needs a
concurrent process inside the box (documented residual, ADR 0011).

Round 19 (R1, R7, R8, R10, R14): the AGENT's container is the only one the agent ever has a process in; every
engine verdict (RED, GREEN, the verification checkouts, the disproof re-run, the suites) runs in a FRESH container
(``start_engine_box``: ``dlv-<run_id>-<tag>-<nonce>`` on its own volume, same run label, same argv otherwise)
populated from the engine's own tree by ``docker cp`` and destroyed right after — ``.dlv-engine`` and ``.dlv-verify``
no longer exist in the agent's volume, and its result files are never visible to the agent's process. Resolution is
ONE exec per decision: ``python3 -I /mnt/dlv/resolve.py -- <operands>`` — a hash-pinned helper bind-mounted
read-only from the engine's own package (``adapters/tools/resolve.py``), never a file the agent could rewrite —
capped at 16 operands and 64 path components. File-tool writes are contained to the WRITE ROOTS
(``services/<service>/`` and ``docs/adr/00NN-*.md``), symlink-resolved, not to the workspace. Every ``docker cp``
in or out is record-first (``sandbox_cp_requested`` / ``sandbox_cp_completed``); the reaper uses ``--format``
without ``-q`` and records ``sandbox_reap_failed`` on any listing or removal failure; ``kill_run`` sends ``docker
kill`` to every container of a run (cancel/deadline); ``destroy`` with a failing removal AND a failing ledger marks
the run ``unrecorded_failure``.
"""

from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import secrets
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import timezone
from typing import Optional, Sequence

from deerflow.sandbox.sandbox import Sandbox, _validate_extra_env
from deerflow.sandbox.sandbox_provider import SandboxProvider
from deerflow.sandbox.search import GrepMatch

from zbm_delivery import fsops, policy, registry
from zbm_delivery.ledger import derived_id
from zbm_delivery.policy import SKILLS_MOUNT, WORKSPACE, inside
from zbm_delivery.ports import DockerCli, DockerUnavailable, ExecResult

UID = "65532:65532"
OUTPUT_CAP = 1024 * 1024
COPY_CAP = 512 * 1024 * 1024
ACTOR = "intel_02_sandbox"
# R14 (N19-A-11): every flag that widens the container, in its one-token, ``=`` and two-token forms
FORBIDDEN_RUN_TOKENS = ("--privileged", "seccomp=unconfined", "apparmor=unconfined", "systempaths=unconfined", "label=disable",
                        "docker.sock", "--cap-add", "--device", "--add-host", "--gpus", "--dns", "--dns-option", "--dns-search",
                        "--sysctl", "--pid", "--userns", "--ipc", "--cgroupns", "--uts", "--cgroup-parent", "-v", "--volume",
                        "--volumes-from", "--net", "--network", "--link", "--expose", "--publish", "-p", "-P", "--publish-all",
                        "--runtime", "--oom-kill-disable", "--init-path", "--device-cgroup-rule", "--security-opt")
_ALLOWED_FLAG_VALUES = {"--network": ("none",), "--net": ("none",), "--security-opt": ("no-new-privileges",)}
_UNICODE_DASHES = re.compile("[\u2010-\u2015\u2212\ufe58\ufe63\uff0d\u00ad\u2043]")
RUN_LABEL = "zbm.dlv.run"
ENGINE_DIR = f"{WORKSPACE}/.dlv-engine"        # engine-owned files inside a FRESH engine container's volume (R1)
STAGE_DIR = f"{WORKSPACE}/.dlv-write"          # prefix of the transient per-write staging directory (file tools, R7)
TOOLS_MOUNT = "/mnt/dlv"                       # read-only bind of the engine's helper directory (R8)
TOOLS_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
RESOLVE_HELPER = os.path.join(TOOLS_SRC_DIR, "resolve.py")
RESOLVE_HELPER_SHA256 = "f08a0d94198cc091f9277f63e7f6dd0b25180e61e038fd0a8eb0f98f81454b03"
MAX_RESOLVE_OPERANDS = policy.MAX_WRITE_OPERANDS
MAX_PATH_DEPTH = policy.MAX_PATH_DEPTH
_MOUNT_PATH_RE = re.compile(r"^/[^,=:\x00-\x1f\x7f]+$")
# the seed's service_env may set only these: determinism switches of the toolchains, never a path, a token or a loader
EXTRA_ENV_ALLOWLIST = ("PYTHONDONTWRITEBYTECODE", "CI", "PYTHONHASHSEED", "TZ", "LANG", "NO_COLOR", "FORCE_COLOR",
                       "CARGO_TERM_COLOR", "CARGO_NET_OFFLINE", "GOPROXY", "GOTOOLCHAIN")
CONTAINER_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": WORKSPACE, "LANG": "C.UTF-8", "TZ": "UTC",
                 "PYTHONDONTWRITEBYTECODE": "1", "CI": "1"}


_TEMP_BASE: Optional[str] = None
_TEMP_BASE_LOCK = threading.Lock()


def _private_temp_base() -> str:
    """In-memory mode (no data dir): one private 0700 temp dir per process for the sandbox env files, removed at
    exit (fix wave 21, L4: it was ``<tmp>/dlv-<pid>``, predictable and never removed)."""
    global _TEMP_BASE
    with _TEMP_BASE_LOCK:
        if _TEMP_BASE is None:
            import atexit
            _TEMP_BASE = tempfile.mkdtemp(prefix="dlv-sbx-")
            atexit.register(fsops.drop_own_temp, _TEMP_BASE)
        return _TEMP_BASE


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class RealDockerCli:
    """argv runner for the docker binary (the only subprocess site of this module; ``shell=False``)."""

    def __init__(self, binary: str = "docker"):
        self.binary = binary

    def run(self, argv: Sequence[str], *, timeout_s: float, stdin: bytes | None = None,
            output_cap: int = OUTPUT_CAP) -> ExecResult:
        full = [self.binary, *argv]
        try:
            proc = subprocess.Popen(full, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        except OSError as exc:
            return ExecResult(127, b"", f"docker client unavailable: {type(exc).__name__}".encode(), False, False)
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        sizes = [0, 0]
        truncated = [False]

        def pump(stream, chunks, idx):
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                if sizes[idx] + len(chunk) > output_cap:
                    chunks.append(chunk[: max(0, output_cap - sizes[idx])])
                    sizes[idx] = output_cap
                    truncated[0] = True
                    # keep draining so the child never blocks on a full pipe
                    while stream.read(65536):
                        pass
                    return
                chunks.append(chunk)
                sizes[idx] += len(chunk)

        t_out = threading.Thread(target=pump, args=(proc.stdout, out_chunks, 0), daemon=True)
        t_err = threading.Thread(target=pump, args=(proc.stderr, err_chunks, 1), daemon=True)
        t_out.start()
        t_err.start()
        timed_out = False
        try:
            if stdin is not None:
                try:
                    proc.stdin.write(stdin)
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait()
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        return ExecResult(proc.returncode if proc.returncode is not None else -1, b"".join(out_chunks),
                          b"".join(err_chunks), timed_out, truncated[0])


def daemon_available(cli: DockerCli) -> bool:
    r = cli.run(["info", "--format", "{{.ServerVersion}}"], timeout_s=10)
    return r.exit_code == 0 and bool(r.stdout.strip())


def forbidden_run_token(argv: Sequence[str]) -> Optional[str]:
    """The forbidden flag found in a ``docker run`` argv, else None. Token by token: a forbidden flag as its own
    token, as ``flag=value`` or as a substring (``docker.sock``); ``--network``/``--net`` and ``--security-opt``
    only with their one allowed value; a ``--mount`` must be our volume or a read-only bind of the two engine
    directories; any unicode dash (a look-alike the daemon would reject but a reviewer might read as the real
    flag) is forbidden outright."""
    toks = [str(t) for t in argv]
    for i, tok in enumerate(toks):
        if _UNICODE_DASHES.search(tok):
            return tok
        name, has_eq, value = tok.partition("=")
        if name in FORBIDDEN_RUN_TOKENS:
            allowed = _ALLOWED_FLAG_VALUES.get(name)
            if allowed is None:
                return name
            val = value if has_eq else (toks[i + 1] if i + 1 < len(toks) else "")
            if val.strip() not in allowed:
                return tok if has_eq else f"{name} {val}".strip()
            continue
        for bad in FORBIDDEN_RUN_TOKENS:
            if not bad.startswith("-") and bad in tok:
                return bad
        if name == "--mount":
            val = value if has_eq else (toks[i + 1] if i + 1 < len(toks) else "")
            if not _mount_ok(val):
                return f"--mount {val}"
    joined = " ".join(toks)
    m = re.search(r"(?:^|\s)--net(?:work)?(?:=|\s)+(?!none(?:\s|$))", joined)
    return m.group(0).strip() if m else None


def _mount_ok(spec: str) -> bool:
    opts = {}
    for kv in spec.split(","):
        k, _, v = kv.partition("=")
        opts[k.strip()] = v
    kind = opts.get("type")
    if kind == "volume":
        return opts.get("dst") == WORKSPACE and (opts.get("src") or "").startswith("dlv-ws-")
    if kind == "bind":
        return opts.get("dst") in (SKILLS_MOUNT, TOOLS_MOUNT) and "ro" in opts and "docker.sock" not in spec
    return False


def resolve_helper_bytes() -> bytes:
    """The resolver's bytes, verified against the pin (R8: a modified helper never ships)."""
    with open(RESOLVE_HELPER, "rb") as fh:
        data = fh.read()
    if hashlib.sha256(data).hexdigest() != RESOLVE_HELPER_SHA256:
        raise PermissionError("adapters/tools/resolve.py does not match its pinned hash (R8)")
    return data


def path_depth(path: str) -> int:
    return len([c for c in path.split("/") if c])


def _tar_names(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        return [m.name for m in tar.getmembers()]


def reap(cli: DockerCli, record, *, where: str) -> list[dict]:
    """Remove every container and volume carrying our run label (R6/R10). Each removal is recorded
    ``sandbox_reaped`` BEFORE the ``docker rm``; a listing or removal that fails is recorded ``sandbox_reap_failed``
    (never silently "reaped"); the returned list describes what was reaped (kind, name, run_id, exit)."""
    out: list[dict] = []
    if not daemon_available(cli):
        _reap_failed(record, where, "daemon", "", "docker daemon not reachable")
        return out
    fmt = "{{.Names}}\t{{.Label \"" + RUN_LABEL + "\"}}"
    ps = cli.run(["ps", "-a", "--filter", f"label={RUN_LABEL}", "--format", fmt], timeout_s=30)
    vfmt = "{{.Name}}\t{{.Label \"" + RUN_LABEL + "\"}}"
    vols = cli.run(["volume", "ls", "--filter", f"label={RUN_LABEL}", "--format", vfmt], timeout_s=30)
    items: list[tuple[str, str, str]] = []
    for kind, res in (("container", ps), ("volume", vols)):
        if res.exit_code != 0:
            _reap_failed(record, where, kind, "", f"docker listing failed (exit {res.exit_code})")
            continue
        for ln in res.stdout.decode("utf-8", "replace").splitlines():
            parts = ln.split("\t")
            name = parts[0].strip()
            label = parts[1].strip() if len(parts) > 1 else ""
            if name and not label:
                _reap_failed(record, where, kind, "", "listing line without the run label (format not honoured)", name)
                continue
            if name:
                items.append((kind, name, label))
    for kind, name, run_id in items:
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,128}", name):
            continue
        subject = run_id or "reaper"
        try:
            record(derived_id("rp", where, kind, name), "sandbox_reaped", ACTOR, subject,
                   {"run_id": run_id, "kind": kind, "name_sha256": _sha(name.encode()), "where": where},
                   f"Sandbox {kind} reaped at {where}")
        except Exception:  # noqa: BLE001 - unrecorded → not removed (record-first)
            continue
        argv = ["rm", "-f", name] if kind == "container" else ["volume", "rm", "-f", name]
        r = cli.run(argv, timeout_s=60)
        if r.exit_code != 0:
            _reap_failed(record, where, kind, run_id, f"docker rm failed (exit {r.exit_code})", name)
        out.append({"kind": kind, "name": name, "run_id": run_id, "exit": r.exit_code})
    return out


def _reap_failed(record, where: str, kind: str, run_id: str, why: str, name: str = "") -> None:
    try:
        record(derived_id("rpf", where, kind, name, why), "sandbox_reap_failed", ACTOR, run_id or "reaper",
               {"run_id": run_id, "kind": kind, "name_sha256": _sha(name.encode()) if name else None, "where": where, "why": why[:120]},
               f"Sandbox reap failed at {where}: {kind}")
    except Exception:  # noqa: BLE001 - the ledger is down too: nothing else to do here
        pass


def tar_of_dir(root: str, *, exclude_dirs: tuple = (".git",), uid: int = 65532) -> bytes:
    """A tar stream of ``root``'s contents (top-level entries relative to root), every entry owned by ``uid``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs and not os.path.islink(os.path.join(dirpath, d)))
            rel = os.path.relpath(dirpath, root)
            for name in sorted(dirnames):
                arc = name if rel == "." else posixpath.join(rel, name)
                info = tarfile.TarInfo(arc)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.uid = info.gid = uid
                info.mtime = time.time()
                tar.addfile(info)
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full) or not os.path.isfile(full) or name in exclude_dirs:
                    continue                        # a linked worktree's .git is a FILE (gitdir pointer): excluded too
                arc = name if rel == "." else posixpath.join(rel, name)
                info = tar.gettarinfo(full, arcname=arc)
                info.uid = info.gid = uid
                info.uname = info.gname = ""
                with open(full, "rb") as fh:
                    tar.addfile(info, fh)
    return buf.getvalue()


def tar_of_file(name: str, content: bytes, uid: int = 65532, mode: int = 0o644) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        info.mode = mode
        info.uid = info.gid = uid
        info.mtime = time.time()          # PAX keeps the fraction: a file written in the same second as a build must
        tar.addfile(info, io.BytesIO(content))    # still be NEWER than that build's fingerprint (cargo compares mtimes)
    return buf.getvalue()


def extract_tar(data: bytes, dest: str) -> list[str]:
    """Extract with the ``data`` filter (no absolute paths, no ``..``, no links outside); returns member names."""
    names = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for m in tar.getmembers():
            if m.issym() or m.islnk() or m.isdev():
                continue
            names.append(m.name)
        tar.extractall(dest, members=[m for m in tar.getmembers() if not (m.issym() or m.islnk() or m.isdev())],
                       filter="data")
    return names


class ZbmDockerSandbox(Sandbox):
    """One container (one run). Every method is a fixed argv through ``DockerCli``; every exec is recorded first."""

    persistent_shell_sessions = False       # each exec is a fresh /bin/bash -lc (tool receipts stay VERIFIED)

    def __init__(self, sandbox_id: str, container: str, binding: registry.RunBinding, provider: "ZbmDockerSandboxProvider"):
        super().__init__(sandbox_id)
        self.container = container
        self.binding = binding
        self.provider = provider
        self.volume: str = ""
        self.tag: str = ""
        self.started_at: float = time.monotonic()

    # --- plumbing ---------------------------------------------------------------------------------------------------

    def _rt(self) -> registry.Runtime:
        return registry.runtime()

    def _remaining(self) -> float:
        now = self._rt().clock.now().astimezone(timezone.utc)
        return max(0.0, (self.binding.deadline_at - now).total_seconds())

    def _bound_timeout(self, timeout: Optional[float]) -> int:
        cap = self._rt().settings.cmd_timeout_s
        t = min(float(timeout) if timeout else cap, cap, self._remaining())
        return max(1, int(t))

    def _record_exec(self, phase: str, command_sha: str, cwd: str, env_names: list[str], extra: dict, seq: int) -> None:
        rt = self._rt()
        try:
            rt.record(derived_id("sx", self.binding.run_id, seq, phase), f"sandbox_exec_{phase}", ACTOR,
                      self.binding.run_id, {"run_id": self.binding.run_id, "seq": seq, "command_sha256": command_sha,
                                            "cwd": cwd, "env_names": sorted(env_names), **extra},
                      f"Sandbox exec {seq} {phase} ({self.binding.run_id})")
        except Exception as exc:  # noqa: BLE001 - a record that failed: nothing may run (record-first)
            rt.on_ledger_failure(self.binding.run_id, f"sandbox_exec_{phase} record failed: {type(exc).__name__}")
            raise RuntimeError("sandbox exec refused: the ledger record failed (record-first)") from None

    def exec_argv(self, argv: Sequence[str], *, cwd: str = WORKSPACE, env: Optional[dict] = None,
                  timeout: Optional[float] = None) -> ExecResult:
        """Run ``argv`` in the container with no shell (the runner's test commands)."""
        if self.binding.finished:
            raise RuntimeError("sandbox released")
        argv = [str(a) for a in argv]
        if not argv or any("\x00" in a for a in argv):
            raise ValueError("empty argv or NUL in argv")
        if not inside(cwd, WORKSPACE):
            raise PermissionError("cwd outside the workspace")
        env = dict(env or {})
        _validate_extra_env(env)
        for k in env:
            if k not in EXTRA_ENV_ALLOWLIST:
                raise ValueError(f"env key {k!r} is not on the sandbox env allowlist")
        secs = self._bound_timeout(timeout)
        self.binding.exec_seq += 1
        seq = self.binding.exec_seq
        command_sha = _sha(("\0".join(argv)).encode("utf-8", "surrogatepass"))
        self._record_exec("requested", command_sha, cwd, list(env), {"argv_len": len(argv), "timeout_s": secs}, seq)
        docker_argv = ["exec", "--user", UID, "-w", cwd]
        for k in sorted(env):
            docker_argv += ["--env", f"{k}={env[k]}"]
        docker_argv += [self.container, "timeout", "-k", "5", str(secs), *argv]
        started = time.monotonic()
        r = self._rt().docker.run(docker_argv, timeout_s=secs + 15, output_cap=OUTPUT_CAP)
        if r.exit_code == 124 or r.timed_out:
            r = ExecResult(r.exit_code if r.exit_code else 124, r.stdout, r.stderr, True, r.truncated)
        self._record_exec("completed", command_sha, cwd, list(env),
                          {"exit": r.exit_code, "stdout_sha256": _sha(r.stdout), "stderr_sha256": _sha(r.stderr),
                           "bytes": len(r.stdout) + len(r.stderr), "truncated": r.truncated, "timed_out": r.timed_out,
                           "elapsed_ms": int((time.monotonic() - started) * 1000)}, seq)
        return r

    # --- DF Sandbox contract ------------------------------------------------------------------------------------------

    def execute_command(self, command: str, env: dict[str, str] | None = None, timeout: float | None = None) -> str:
        if not isinstance(command, str) or not command or "\x00" in command:
            return "Error: empty command"
        if policy.has_line_separator(command):                 # R6, second layer behind the guardrail's deny
            return "Error: a command must be a single line (multiline_command refused)"
        r = self.exec_argv(["/bin/bash", "-lc", command], env=env, timeout=timeout)
        out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
        tail = ""
        if r.timed_out:
            tail += "\n[command timed out after the sandbox deadline]"
        if r.truncated:
            tail += "\n[output truncated at 1 MiB]"
        if r.exit_code != 0:
            tail += f"\n[exit code {r.exit_code}]"
        return out + tail

    def _ctx(self) -> "policy.Context":
        return policy.Context(service=self.binding.service, workspace=self.binding.workspace)

    def _write_ok(self, p: str) -> bool:
        """R7: inside ``services/<service>/`` (not the directory itself) or exactly ``docs/adr/00NN-*.md``."""
        return policy.write_allowed(p, self._ctx())

    def _contain(self, path: str, *, write: bool = False) -> str:
        if not isinstance(path, str) or not path or policy.has_control(path) or policy.has_line_separator(path):
            raise PermissionError("path missing or carries a control character")
        if not path.startswith("/"):
            path = posixpath.join(WORKSPACE, path)
        if ".." in path.split("/"):
            raise PermissionError("path traversal refused")
        p = posixpath.normpath(path)
        if path_depth(p) > MAX_PATH_DEPTH:
            raise PermissionError("path deeper than the resolver's cap (R8)")
        if write:
            if not self._write_ok(p):
                raise PermissionError("write path outside services/<service>/ and docs/adr/00NN-*.md (R7)")
        elif not any(inside(p, r) for r in (WORKSPACE, SKILLS_MOUNT)):
            raise PermissionError("path outside the sandbox workspace")
        real = self.realpath(p)
        if real is None:
            raise PermissionError("path could not be resolved inside the sandbox (fail closed)")
        if write:
            if not self._write_ok(real):
                raise PermissionError("symlink escape refused: the write resolves outside the write roots (R7)")
        elif not any(inside(real, r) for r in (WORKSPACE, SKILLS_MOUNT)):
            raise PermissionError("symlink escape refused")
        return p

    def realpath(self, path: str) -> Optional[str]:
        return self.realpath_many([path])[0]

    def realpath_many(self, paths: Sequence[str]) -> list[Optional[str]]:
        """Every path resolved INSIDE the container by the pinned helper in ONE exec (R8): the real path of the
        longest existing prefix plus the not-yet-existing remainder, or None for a path that cannot be resolved,
        that is not absolute/normalised, or when the operand count or a path's depth breaks the cap (fail closed)."""
        paths = [str(p) for p in paths]
        if not paths or len(paths) > MAX_RESOLVE_OPERANDS:
            return [None] * len(paths)
        for p in paths:
            if "\x00" in p or "\n" in p or ".." in p.split("/") or not p.startswith("/") or path_depth(p) > MAX_PATH_DEPTH:
                return [None] * len(paths)
        r = self.exec_argv(["python3", "-I", f"{TOOLS_MOUNT}/resolve.py", "--", *paths], timeout=20)
        if r.exit_code != 0 or r.truncated:
            return [None] * len(paths)
        fields = r.stdout.decode("utf-8", "replace").split("\0")
        if len(fields) != len(paths):
            return [None] * len(paths)
        out: list[Optional[str]] = []
        for f in fields:
            f = f.strip("\n")
            out.append(f if f.startswith("/") and posixpath.normpath(f) == f and ".." not in f.split("/") else None)
        return out

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        p = self._contain(path)
        r = self.exec_argv(["cat", "--", p], timeout=60)
        if r.exit_code != 0:
            raise OSError(r.stderr.decode("utf-8", "replace")[:300] or "read failed")
        text = r.stdout.decode("utf-8", "replace")
        if start_line is None and end_line is None:
            return text
        lines = text.splitlines(keepends=True)
        s = max(1, start_line or 1)
        e = end_line or len(lines)
        return "".join(lines[s - 1:e])

    def download_file(self, path: str) -> bytes:
        p = self._contain(path)
        r = self.exec_argv(["cat", "--", p], timeout=60)
        if r.exit_code != 0:
            raise OSError("download failed")
        return r.stdout

    def list_dir(self, path: str, max_depth=2) -> list[str]:
        p = self._contain(path)
        r = self.exec_argv(["find", p, "-maxdepth", str(int(max_depth)), "-mindepth", "1", "-printf", "%p\n"], timeout=60)
        if r.exit_code != 0:
            raise FileNotFoundError(p)
        return sorted(ln for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln)

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        p = self._contain(path, write=True)
        data = content.encode("utf-8")
        if append:
            existing = self.exec_argv(["cat", "--", p], timeout=60)
            if existing.exit_code == 0:
                data = existing.stdout + data
        self.put_bytes(p, data)

    def put_bytes(self, p: str, data: bytes, *, contained: bool = True) -> None:
        """Write ``data`` at ``p`` (already contained): stage under a transient random directory, re-resolve the
        destination parent inside the container, then ``mv`` (R7). ``mkdir -p`` of the parent happens only after
        the re-check. ``contained=False`` is the engine's own write into an engine container (its engine dir)."""
        parent, name = posixpath.split(p)
        stage = f"{STAGE_DIR}-{secrets.token_hex(8)}"
        mk = self.exec_argv(["mkdir", "--", stage], timeout=30)
        if mk.exit_code != 0:
            raise OSError("mkdir failed")
        try:
            self._cp_in(tar_of_file(name, data), stage, len(data))
            ok = self._parent_ok if contained else (lambda rp: inside(rp, WORKSPACE))
            real_parent = self.realpath(parent)
            if real_parent is None or not ok(real_parent):
                raise PermissionError("destination resolved outside the write roots at write time (symlink swap refused)")
            mk = self.exec_argv(["mkdir", "-p", "--", parent], timeout=30)
            if mk.exit_code != 0:
                raise OSError("mkdir failed")
            real_parent = self.realpath(parent)
            if real_parent is None or not ok(real_parent):
                raise PermissionError("destination resolved outside the write roots at write time (symlink swap refused)")
            mv = self.exec_argv(["mv", "-f", "-T", "--", f"{stage}/{name}", p], timeout=30)
            if mv.exit_code != 0:
                raise OSError("write failed (mv)")
        finally:
            self.exec_argv(["rm", "-rf", "--", stage], timeout=30)

    def _parent_ok(self, real_parent: str) -> bool:
        """The parent of a file-tool write: inside the service directory (the directory itself included) or exactly
        ``docs/adr`` (R7)."""
        svc, adr = policy.write_roots(self._ctx())
        return inside(real_parent, svc) or real_parent == adr

    def _record_cp(self, phase: str, op: str, path: str, extra: dict, seq: int) -> None:
        self._record_exec(phase, _sha(path.encode()), path, [], {"op": op, **extra}, seq)

    def _cp_in(self, data: bytes, dest: str, nbytes: Optional[int] = None) -> None:
        """``docker cp`` a tar stream into ``dest`` (record-first: ``sandbox_cp_requested`` / ``_completed``, R10)."""
        self.binding.exec_seq += 1
        seq = self.binding.exec_seq
        n = len(data) if nbytes is None else nbytes
        self._record_cp("requested", "cp_in", dest, {"bytes": n, "kind": "sandbox_cp"}, seq)
        r = self._rt().docker.run(["cp", "-", f"{self.container}:{dest}"], timeout_s=600, stdin=data)
        self._record_cp("completed", "cp_in", dest, {"bytes": n, "exit": r.exit_code, "kind": "sandbox_cp"}, seq)
        if r.exit_code != 0:
            raise OSError("docker cp into the container failed")

    def _cp_out(self, path: str) -> Optional[bytes]:
        """The tar stream of ``path`` read out with ``docker cp`` (record-first, R10); None when missing/too big."""
        self.binding.exec_seq += 1
        seq = self.binding.exec_seq
        self._record_cp("requested", "cp_out", path, {"kind": "sandbox_cp"}, seq)
        r = self._rt().docker.run(["cp", f"{self.container}:{path}", "-"], timeout_s=600, output_cap=COPY_CAP)
        self._record_cp("completed", "cp_out", path, {"exit": r.exit_code, "bytes": len(r.stdout), "truncated": r.truncated,
                                                      "kind": "sandbox_cp"}, seq)
        if r.exit_code != 0 or r.truncated:
            return None
        return r.stdout

    def get_bytes(self, p: str) -> Optional[bytes]:
        """The bytes of one file read out with ``docker cp`` (the daemon reads it, not a process in the box); None
        when the path is missing. Used by the runner for its report files (R2), record-first (R10)."""
        if not inside(posixpath.normpath(p), WORKSPACE) or ".." in p.split("/"):
            raise PermissionError("path outside the workspace")
        data = self._cp_out(p)
        if data is None:
            return None
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            for m in tar.getmembers():
                if m.isfile():
                    fh = tar.extractfile(m)
                    return fh.read() if fh else None
        return None

    def update_file(self, path: str, content: bytes) -> None:
        p = self._contain(path, write=True)
        self.put_bytes(p, content)

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        p = self._contain(path)
        if not isinstance(pattern, str) or "\x00" in pattern or len(pattern) > 512:
            raise ValueError("bad pattern")
        argv = ["find", p]
        if not include_dirs:
            argv += ["-type", "f"]
        if "/" in pattern or "**" in pattern:
            argv += ["-path", posixpath.join(p, pattern.replace("**/", "*")) if not pattern.startswith("/") else pattern]
        else:
            argv += ["-name", pattern]
        r = self.exec_argv(argv, timeout=60)
        if r.exit_code != 0:
            return [], False
        matches = sorted(ln for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln)
        return matches[:max_results], len(matches) > max_results or r.truncated

    def grep(self, path: str, pattern: str, *, glob: str | None = None, literal: bool = False,
             case_sensitive: bool = False, max_results: int = 100) -> tuple[list[GrepMatch], bool]:
        p = self._contain(path)
        if not isinstance(pattern, str) or "\x00" in pattern or len(pattern) > 1024:
            raise ValueError("bad pattern")
        argv = ["grep", "-rn", "-I", "--max-count", str(max_results)]
        if literal:
            argv.append("-F")
        else:
            argv.append("-E")
        if not case_sensitive:
            argv.append("-i")
        if glob:
            argv += ["--include", glob]
        argv += ["-e", pattern, "--", p]
        r = self.exec_argv(argv, timeout=60)
        out = []
        for ln in r.stdout.decode("utf-8", "replace").splitlines():
            parts = ln.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                out.append(GrepMatch(path=parts[0], line_number=int(parts[1]), line=parts[2]))
            if len(out) >= max_results:
                break
        return out, r.truncated or len(out) >= max_results


class ZbmDockerSandboxProvider(SandboxProvider):
    """One container per run, acquired by the runner before the agent starts (idempotent for the same thread)."""

    uses_thread_data_mounts = False
    needs_upload_permission_adjustment = False
    supports_agent_skill_isolation = True    # only our hash-pinned (empty) root is mounted; nothing else can appear

    def __init__(self, **kwargs):
        self._boxes: dict[str, ZbmDockerSandbox] = {}
        self._engine_boxes: dict[str, ZbmDockerSandbox] = {}
        self._lock = threading.RLock()

    # --- DF SandboxProvider contract ----------------------------------------------------------------------------------

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        rt = registry.runtime()
        binding = registry.lookup(thread_id)
        now = rt.clock.now().astimezone(timezone.utc)
        if binding is None or not binding.live(now):
            raise PermissionError("sandbox acquire refused: no live run token is bound to this thread (spec C.2)")
        if user_id != binding.principal_user_id or user_id in (None, "", "default"):
            try:
                rt.record(derived_id("idm", binding.run_id, "sandbox", str(user_id)[:64]), "identity_mismatch", ACTOR,
                          binding.run_id, {"run_id": binding.run_id, "where": "sandbox_acquire",
                                           "expected_sha256": _sha(binding.principal_user_id.encode()),
                                           "got_sha256": _sha(str(user_id).encode())},
                          f"Identity mismatch at sandbox acquire ({binding.run_id})")
            finally:
                pass
            raise PermissionError("sandbox acquire refused: user id is not the run's principal (spec C.5)")
        sandbox_id = f"dlv-{binding.run_id}"
        with self._lock:
            if sandbox_id in self._boxes:
                return sandbox_id
            box = self._start_container(rt, binding, sandbox_id)
            self._boxes[sandbox_id] = box
            binding.container_name = sandbox_id
            return sandbox_id

    def get(self, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            return self._boxes.get(sandbox_id)

    def release(self, sandbox_id: str) -> None:
        """deer-flow's per-turn lease return: a no-op for the container (see the module docstring)."""
        return None

    def reset(self) -> None:
        return None

    def sandbox_network_mode(self) -> str:
        return "none"                          # R4: literally none

    # --- ours -----------------------------------------------------------------------------------------------------

    @staticmethod
    def run_argv(settings, run_id: str, env_file: str, *, name: Optional[str] = None, volume: Optional[str] = None,
                 tools_dir: str = TOOLS_SRC_DIR) -> list[str]:
        """The exact ``docker run`` argv (asserted token by token by the unit suite). ``name``/``volume`` default to
        the agent's container; an engine container passes its own (R1). Both bind sources are validated before
        they are spliced into a CSV ``--mount`` option (R14: no comma, ``=`` or ``:`` can widen the mount)."""
        image = settings.sandbox_image
        if not image or "@sha256:" not in image:
            raise PermissionError("tag-only image refused; the image must be pinned by digest (ST-03)")
        if settings.sandbox_network != "none":
            raise PermissionError("sandbox network must be none (R4)")
        skills = os.path.realpath(settings.skills_root)
        tools = os.path.realpath(tools_dir)
        for label, path in (("skills root", skills), ("tools dir", tools)):
            if not _MOUNT_PATH_RE.fullmatch(path) or not os.path.isdir(path):
                raise PermissionError(f"the {label} path cannot be a mount source (must be an absolute directory with no ',', '=' or ':')")
        name = name or f"dlv-{run_id}"
        volume = volume or f"dlv-ws-{run_id}"
        if not re.fullmatch(r"dlv-[A-Za-z0-9_.\-]{1,120}", name) or not re.fullmatch(r"dlv-ws-[A-Za-z0-9_.\-]{1,120}", volume):
            raise PermissionError("bad container/volume name")
        argv = ["run", "-d", "--name", name, "--label", f"{RUN_LABEL}={run_id}", "--user", UID, "--cap-drop=ALL",
                "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=512m",
                "--pids-limit", "512", "--memory", settings.sandbox_mem, "--cpus", settings.sandbox_cpus,
                "--network", "none",
                "--mount", f"type=volume,src={volume},dst={WORKSPACE},volume-label={RUN_LABEL}={run_id}",
                "--mount", f"type=bind,src={skills},dst={SKILLS_MOUNT},ro",
                "--mount", f"type=bind,src={tools},dst={TOOLS_MOUNT},ro",
                "--env-file", env_file, image, "sleep", "infinity"]
        bad = forbidden_run_token(argv)
        if bad is not None:
            raise PermissionError(f"forbidden docker flag {bad!r}")
        return argv

    @staticmethod
    def env_file_text(test_seed: dict) -> str:
        env = dict(CONTAINER_ENV)
        for k, v in (test_seed.get("service_env") or {}).items():
            if k in EXTRA_ENV_ALLOWLIST:
                env[k] = str(v)
        return "".join(f"{k}={env[k]}\n" for k in sorted(env))

    def _env_file(self, rt: registry.Runtime, run_id: str) -> str:
        # the env file lives under the data dir; in-memory mode (no data dir) uses a private temp dir, never cwd
        base = rt.settings.data_dir or _private_temp_base()
        env_dir = os.path.join(base, "sandbox-env")
        os.makedirs(env_dir, exist_ok=True)
        env_file = os.path.join(env_dir, f"{run_id}.env")
        with open(env_file, "w", encoding="ascii") as fh:
            fh.write(self.env_file_text(rt.test_seed))
        os.chmod(env_file, 0o600)
        return env_file

    def _start_container(self, rt: registry.Runtime, binding: registry.RunBinding, sandbox_id: str,
                         *, name: Optional[str] = None, volume: Optional[str] = None, op: str = "run") -> ZbmDockerSandbox:
        if not daemon_available(rt.docker):
            raise DockerUnavailable("docker daemon not reachable")
        resolve_helper_bytes()                                    # R8: the helper we are about to mount matches its pin
        env_file = self._env_file(rt, binding.run_id)
        argv = self.run_argv(rt.settings, binding.run_id, env_file, name=name, volume=volume)
        rt.record(derived_id("dk", binding.run_id, op, name or "agent"), "crossing_docker_requested", ACTOR, binding.run_id,
                  {"run_id": binding.run_id, "op": op, "argv_sha256": _sha("\0".join(argv).encode()),
                   "image": rt.settings.sandbox_image, "network": rt.settings.sandbox_network,
                   "container": name or f"dlv-{binding.run_id}"},
                  f"docker run requested ({binding.run_id})")
        r = rt.docker.run(argv, timeout_s=120)
        if r.exit_code != 0:
            raise DockerUnavailable(f"docker run failed (exit {r.exit_code})")
        container_id = r.stdout.decode("ascii", "replace").strip()
        box = ZbmDockerSandbox(sandbox_id, name or sandbox_id, binding, self)
        box.container_id_sha256 = _sha(container_id.encode()) if container_id else None
        box.volume = volume or f"dlv-ws-{binding.run_id}"
        return box

    def copy_in(self, sandbox_id: str, host_dir: str) -> int:
        """Populate the volume from ``host_dir`` (a tar stream, owner 65532, ``.git`` excluded; never a bind mount).
        Verifies afterwards that no ``.git`` exists in the box (R4). Returns bytes. Record-first (R10)."""
        box = self._require(sandbox_id)
        data = tar_of_dir(host_dir)
        if any(n == ".git" or n.startswith(".git/") for n in _tar_names(data)):
            raise PermissionError("the copy-in stream carries a .git (refused)")
        try:
            box._cp_in(data, WORKSPACE)
        except OSError:
            raise DockerUnavailable("docker cp into the volume failed") from None
        chk = box.exec_argv(["test", "!", "-e", f"{WORKSPACE}/.git"], timeout=20)
        if chk.exit_code != 0:
            raise PermissionError("a .git exists inside the sandbox workspace (refused)")
        return len(data)

    def copy_out(self, sandbox_id: str, container_path: str) -> bytes:
        """A tar stream of ``container_path`` (inside the workspace) from the container (record-first, R10)."""
        box = self._require(sandbox_id)
        p = posixpath.normpath(container_path)
        if not inside(p, WORKSPACE) or ".." in container_path.split("/"):
            raise PermissionError("copy_out path outside the workspace")
        data = box._cp_out(p)
        if data is None:
            raise DockerUnavailable("docker cp out of the volume failed")
        return data

    # --- engine containers (R1) --------------------------------------------------------------------------------------

    def start_engine_box(self, binding: registry.RunBinding, tag: str) -> ZbmDockerSandbox:
        """A FRESH container on a fresh volume for one engine verdict run: same argv as the agent's container, its
        own name/volume (``dlv-<run_id>-<tag>-<nonce>``), the run's label so the reaper covers it. Recorded
        ``crossing_docker_requested`` (record-first) and ``engine_box_started``."""
        rt = registry.runtime()
        if not re.fullmatch(r"[a-z0-9]{1,24}", tag):
            raise ValueError("bad engine box tag")
        nonce = secrets.token_hex(4)
        name = f"dlv-{binding.run_id}-{tag}-{nonce}"
        volume = f"dlv-ws-{binding.run_id}-{tag}-{nonce}"
        box = self._start_container(rt, binding, name, name=name, volume=volume, op="run_engine")
        box.started_at = time.monotonic()
        box.tag = tag
        with self._lock:
            self._engine_boxes[name] = box
        try:
            rt.record(derived_id("ebx", binding.run_id, name), "engine_box_started", ACTOR, binding.run_id,
                      {"run_id": binding.run_id, "tag": tag, "container": name, "container_id_sha256": box.container_id_sha256},
                      f"Engine container started for {tag} ({binding.run_id})")
        except Exception:  # noqa: BLE001 - the ledger is down: the box must not be used
            self.destroy_box(box, binding.run_id)
            raise
        return box

    def ship_tree(self, box: ZbmDockerSandbox, host_dir: str, dest: str) -> int:
        """Ship a host tree (an engine-built checkout) into ``dest`` of an engine container: the tar carries the
        tree's entries under ``basename(dest)`` and is copied into ``dirname(dest)`` (record-first). Returns bytes."""
        parent = posixpath.dirname(dest)
        mk = box.exec_argv(["mkdir", "-p", "--", parent], cwd=WORKSPACE, timeout=30)
        if mk.exit_code != 0:
            raise RuntimeError("could not create the checkout's parent directory in the engine container")
        data = tar_of_dir(host_dir, exclude_dirs=(".git",))
        buf = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as src, tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as dst:
            base = posixpath.basename(dest)
            info = tarfile.TarInfo(base)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = info.gid = 65532
            info.mtime = time.time()
            dst.addfile(info)
            for m in src.getmembers():
                m.name = posixpath.join(base, m.name)
                dst.addfile(m, src.extractfile(m) if m.isfile() else None)
        data = buf.getvalue()
        box._cp_in(data, parent)
        return len(data)

    def destroy_box(self, box: ZbmDockerSandbox, run_id: str) -> None:
        """Remove an engine container and its volume; recorded ``engine_box_released`` (with the elapsed time) or
        ``sandbox_release_failed``."""
        rt = registry.runtime()
        with self._lock:
            self._engine_boxes.pop(box.container, None)
        r1 = rt.docker.run(["rm", "-f", box.container], timeout_s=60)
        r2 = rt.docker.run(["volume", "rm", "-f", box.volume], timeout_s=60) if r1.exit_code == 0 else None
        failed = [("container", r1.exit_code)] if r1.exit_code != 0 else []
        if r2 is not None and r2.exit_code != 0:
            failed.append(("volume", r2.exit_code))
        elapsed_ms = int((time.monotonic() - getattr(box, "started_at", time.monotonic())) * 1000)
        if failed:
            self._record_release_failed(rt, run_id, box.container, failed)
            raise DockerUnavailable("engine container release failed: " + ", ".join(f"{k} exit {e}" for k, e in failed))
        try:
            rt.record(derived_id("ebr", run_id, box.container), "engine_box_released", ACTOR, run_id,
                      {"run_id": run_id, "tag": getattr(box, "tag", ""), "container": box.container, "elapsed_ms": elapsed_ms},
                      f"Engine container released ({run_id}, {elapsed_ms} ms)")
        except Exception:  # noqa: BLE001 - removed but unrecorded: the run is marked (R10)
            rt.on_ledger_failure(run_id, "engine_box_released record failed")

    def kill_run(self, run_id: str) -> list[dict]:
        """``docker kill`` every container of ``run_id`` this provider knows (the agent's and any engine box in
        flight): a running exec ends immediately (N19-A-6). Record-first (``sandbox_kill_requested``)."""
        rt = registry.runtime()
        with self._lock:
            names = [b.container for b in self._boxes.values() if b.binding.run_id == run_id]
            names += [b.container for b in self._engine_boxes.values() if b.binding.run_id == run_id]
        out = []
        for name in names:
            try:
                rt.record(derived_id("kill", run_id, name), "sandbox_kill_requested", ACTOR, run_id,
                          {"run_id": run_id, "container": name}, f"docker kill requested ({run_id})")
            except Exception:  # noqa: BLE001 - unrecorded → not killed; the deadline in the container still bounds it
                continue
            r = rt.docker.run(["kill", name], timeout_s=30)
            out.append({"container": name, "exit": r.exit_code})
        return out

    def _record_release_failed(self, rt, run_id: str, sandbox_id: str, failed: list) -> None:
        try:
            rt.record(derived_id("srf", run_id, sandbox_id), "sandbox_release_failed", ACTOR, run_id,
                      {"run_id": run_id, "sandbox_id": sandbox_id, "failed": [{"kind": k, "exit": e} for k, e in failed]},
                      f"Sandbox release failed ({run_id}): " + ", ".join(k for k, _ in failed))
        except Exception:  # noqa: BLE001 - rm failed AND the ledger is down: the run is unrecorded_failure (R10)
            rt.on_ledger_failure(run_id, "sandbox_release_failed could not be recorded")

    def destroy(self, sandbox_id: str, run_id: str) -> None:
        """``docker rm -f`` + ``docker volume rm`` — only the runner calls this, after copying evidence out. R6: both
        exit codes are checked; a failure is recorded ``sandbox_release_failed`` and raised (the reaper at the next
        start or ``stop()`` removes what is left by label); R10: a failure that cannot be recorded marks the run."""
        rt = registry.runtime()
        with self._lock:
            box = self._boxes.pop(sandbox_id, None)
            stray = [b for b in self._engine_boxes.values() if b.binding.run_id == run_id]
        for b in stray:
            try:
                self.destroy_box(b, run_id)
            except DockerUnavailable:
                pass
        if box is None:
            return
        box.binding.finished = True
        r1 = rt.docker.run(["rm", "-f", box.container], timeout_s=60)
        r2 = rt.docker.run(["volume", "rm", "-f", f"dlv-ws-{run_id}"], timeout_s=60) if r1.exit_code == 0 else None
        failed = [("container", r1.exit_code)] if r1.exit_code != 0 else []
        if r2 is not None and r2.exit_code != 0:
            failed.append(("volume", r2.exit_code))
        if failed:
            self._record_release_failed(rt, run_id, sandbox_id, failed)
            raise DockerUnavailable("sandbox release failed: " + ", ".join(f"{k} exit {e}" for k, e in failed))

    def _require(self, sandbox_id: str) -> ZbmDockerSandbox:
        with self._lock:
            box = self._boxes.get(sandbox_id)
        if box is None:
            raise RuntimeError("no such sandbox")
        return box

