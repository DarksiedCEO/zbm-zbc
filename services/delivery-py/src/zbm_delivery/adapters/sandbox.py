"""
Sandbox adapter — Docker only, ours (spec §C.2, D2; F-01, F-02, ST-03).

``ZbmDockerSandboxProvider`` (``deerflow.sandbox.sandbox_provider.SandboxProvider``) and ``ZbmDockerSandbox``
(``deerflow.sandbox.sandbox.Sandbox``) drive the ``docker`` CLI with fixed argv (``shell=False``) through the
``DockerCli`` port; never a mounted ``docker.sock``, never ``--privileged``, never ``seccomp=unconfined``, never a
tag-only image, never ``LocalSandboxProvider``. No daemon → ``DockerUnavailable`` → no run starts (C.1.11).

Container per run: ``docker run -d --name dlv-<run_id> --user 65532:65532 --cap-drop=ALL --security-opt
no-new-privileges --read-only --tmpfs /tmp:rw,nosuid,size=512m --pids-limit 512 --memory <mem> --cpus <cpus>
--network <internal net> --mount type=volume,src=dlv-ws-<run_id>,dst=/mnt/user-data/workspace --mount
type=bind,src=<skills root>,dst=/mnt/skills,ro --env-file <allowlisted env file> <image>@sha256:<digest> sleep infinity``.
The workspace is deer-flow's virtual prefix ``/mnt/user-data/workspace`` (config/paths.py VIRTUAL_PATH_PREFIX; the
bash tool prepends ``cd`` to it and the file tools address it) — the spec's ``/workspace`` would leave every DF
tool pointing at a path that does not exist (ADR 0011, spec defect). The volume is populated by ``docker cp`` of a
tar stream whose entries are owned by 65532 (a bind mount of the host repo is never made). Every exec is recorded
``sandbox_exec_requested`` before spawn and ``sandbox_exec_completed`` after; output is capped at 1 MiB with
``truncated``; the in-container ``timeout`` binary bounds the command at ``min(timeout, DLV_CMD_TIMEOUT_S,
remaining wall clock)`` with the host-side subprocess timeout as the backstop.

deer-flow calls ``provider.release(sandbox_id)`` after EVERY agent turn (sandbox/middleware.py after_agent); that is
a lease return here — the container lives for the run and is torn down by ``destroy`` (``docker rm -f`` + ``docker
volume rm``) only after the runner copied the diff and the evidence out (ADR 0011 choice).
"""

from __future__ import annotations

import hashlib
import io
import os
import posixpath
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

from zbm_delivery import registry
from zbm_delivery.ledger import derived_id
from zbm_delivery.policy import SKILLS_MOUNT, WORKSPACE, inside
from zbm_delivery.ports import DockerCli, DockerUnavailable, ExecResult

UID = "65532:65532"
OUTPUT_CAP = 1024 * 1024
COPY_CAP = 512 * 1024 * 1024
ACTOR = "intel_02_sandbox"
FORBIDDEN_RUN_TOKENS = ("--privileged", "seccomp=unconfined", "docker.sock", "--pid=host", "--network=host",
                        "--cap-add", "--device", "--userns=host", "--security-opt=apparmor=unconfined")
EXTRA_ENV_ALLOWLIST = ("PYTHONDONTWRITEBYTECODE", "CI", "PYTHONHASHSEED", "TZ", "LANG")
CONTAINER_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": WORKSPACE, "LANG": "C.UTF-8", "TZ": "UTC",
                 "PYTHONDONTWRITEBYTECODE": "1", "CI": "1"}


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


def tar_of_dir(root: str, *, exclude_dirs: tuple = (".git",), uid: int = 65532) -> bytes:
    """A tar stream of ``root``'s contents (top-level entries relative to root), every entry owned by ``uid``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs and not os.path.islink(os.path.join(dirpath, d)))
            rel = os.path.relpath(dirpath, root)
            for name in sorted(dirnames):
                arc = name if rel == "." else posixpath.join(rel, name)
                info = tarfile.TarInfo(arc)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.uid = info.gid = uid
                info.mtime = int(time.time())
                tar.addfile(info)
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                arc = name if rel == "." else posixpath.join(rel, name)
                info = tar.gettarinfo(full, arcname=arc)
                info.uid = info.gid = uid
                info.uname = info.gname = ""
                with open(full, "rb") as fh:
                    tar.addfile(info, fh)
    return buf.getvalue()


def tar_of_file(name: str, content: bytes, uid: int = 65532, mode: int = 0o644) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        info.mode = mode
        info.uid = info.gid = uid
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(content))
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

    def _contain(self, path: str, *, write: bool = False) -> str:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise PermissionError("path missing")
        if not path.startswith("/"):
            path = posixpath.join(WORKSPACE, path)
        if ".." in path.split("/"):
            raise PermissionError("path traversal refused")
        p = posixpath.normpath(path)
        roots = (WORKSPACE,) if write else (WORKSPACE, SKILLS_MOUNT)
        if not any(inside(p, r) for r in roots):
            raise PermissionError("path outside the sandbox workspace")
        real = self.realpath(p)
        if real is not None and not any(inside(real, r) for r in roots):
            raise PermissionError("symlink escape refused")
        return p

    def realpath(self, path: str) -> Optional[str]:
        """``readlink -f`` inside the container (None when the path does not resolve)."""
        r = self.exec_argv(["readlink", "-f", "--", path], timeout=20)
        if r.exit_code != 0:
            return None
        return r.stdout.decode("utf-8", "replace").strip() or None

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

    def put_bytes(self, p: str, data: bytes) -> None:
        parent, name = posixpath.split(p)
        mk = self.exec_argv(["mkdir", "-p", "--", parent], timeout=30)
        if mk.exit_code != 0:
            raise OSError("mkdir failed")
        self.binding.exec_seq += 1
        seq = self.binding.exec_seq
        self._record_exec("requested", _sha(data), parent, [], {"op": "cp_in", "bytes": len(data)}, seq)
        r = self._rt().docker.run(["cp", "-", f"{self.container}:{parent}"], timeout_s=120,
                                  stdin=tar_of_file(name, data))
        self._record_exec("completed", _sha(data), parent, [], {"op": "cp_in", "exit": r.exit_code}, seq)
        if r.exit_code != 0:
            raise OSError("write failed")

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
        return "none"

    # --- ours -----------------------------------------------------------------------------------------------------

    @staticmethod
    def run_argv(settings, run_id: str, env_file: str) -> list[str]:
        """The exact ``docker run`` argv (asserted token by token by the unit suite)."""
        image = settings.sandbox_image
        if not image or "@sha256:" not in image:
            raise PermissionError("tag-only image refused; the image must be pinned by digest (ST-03)")
        argv = ["run", "-d", "--name", f"dlv-{run_id}", "--user", UID, "--cap-drop=ALL",
                "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=512m",
                "--pids-limit", "512", "--memory", settings.sandbox_mem, "--cpus", settings.sandbox_cpus,
                "--network", settings.sandbox_network,
                "--mount", f"type=volume,src=dlv-ws-{run_id},dst={WORKSPACE}",
                "--mount", f"type=bind,src={os.path.realpath(settings.skills_root)},dst={SKILLS_MOUNT},ro",
                "--env-file", env_file, image, "sleep", "infinity"]
        for tok in argv:
            for bad in FORBIDDEN_RUN_TOKENS:
                if bad in tok:
                    raise PermissionError(f"forbidden docker flag {bad!r}")
        return argv

    @staticmethod
    def env_file_text(test_seed: dict) -> str:
        env = dict(CONTAINER_ENV)
        for k, v in (test_seed.get("service_env") or {}).items():
            if k in EXTRA_ENV_ALLOWLIST:
                env[k] = str(v)
        return "".join(f"{k}={env[k]}\n" for k in sorted(env))

    def _start_container(self, rt: registry.Runtime, binding: registry.RunBinding, sandbox_id: str) -> ZbmDockerSandbox:
        if not daemon_available(rt.docker):
            raise DockerUnavailable("docker daemon not reachable")
        # the env file lives under the data dir; in-memory mode (no data dir) uses a private temp dir, never cwd
        base = rt.settings.data_dir or os.path.join(tempfile.gettempdir(), f"dlv-{os.getpid()}")
        env_dir = os.path.join(base, "sandbox-env")
        os.makedirs(env_dir, exist_ok=True)
        env_file = os.path.join(env_dir, f"{binding.run_id}.env")
        with open(env_file, "w", encoding="ascii") as fh:
            fh.write(self.env_file_text(rt.test_seed))
        os.chmod(env_file, 0o600)
        argv = self.run_argv(rt.settings, binding.run_id, env_file)
        rt.record(derived_id("dk", binding.run_id, "run"), "crossing_docker_requested", ACTOR, binding.run_id,
                  {"run_id": binding.run_id, "op": "run", "argv_sha256": _sha("\0".join(argv).encode()),
                   "image": rt.settings.sandbox_image, "network": rt.settings.sandbox_network},
                  f"docker run requested ({binding.run_id})")
        r = rt.docker.run(argv, timeout_s=120)
        if r.exit_code != 0:
            raise DockerUnavailable(f"docker run failed (exit {r.exit_code})")
        container_id = r.stdout.decode("ascii", "replace").strip()
        box = ZbmDockerSandbox(sandbox_id, sandbox_id, binding, self)
        box.container_id_sha256 = _sha(container_id.encode()) if container_id else None
        return box

    def copy_in(self, sandbox_id: str, host_dir: str) -> int:
        """Populate the volume from ``host_dir`` (a tar stream, owner 65532; never a bind mount). Returns bytes."""
        box = self._require(sandbox_id)
        data = tar_of_dir(host_dir)
        r = registry.runtime().docker.run(["cp", "-", f"{box.container}:{WORKSPACE}"], timeout_s=600, stdin=data)
        if r.exit_code != 0:
            raise DockerUnavailable("docker cp into the volume failed")
        return len(data)

    def copy_out(self, sandbox_id: str, container_path: str) -> bytes:
        """A tar stream of ``container_path`` (inside the workspace) from the container."""
        box = self._require(sandbox_id)
        p = posixpath.normpath(container_path)
        if not inside(p, WORKSPACE) or ".." in container_path.split("/"):
            raise PermissionError("copy_out path outside the workspace")
        r = registry.runtime().docker.run(["cp", f"{box.container}:{p}", "-"], timeout_s=600, output_cap=COPY_CAP)
        if r.exit_code != 0 or r.truncated:
            raise DockerUnavailable("docker cp out of the volume failed")
        return r.stdout

    def destroy(self, sandbox_id: str, run_id: str) -> None:
        """``docker rm -f`` + ``docker volume rm`` — only the runner calls this, after copying evidence out."""
        rt = registry.runtime()
        with self._lock:
            box = self._boxes.pop(sandbox_id, None)
        if box is None:
            return
        rt.docker.run(["rm", "-f", box.container], timeout_s=60)
        rt.docker.run(["volume", "rm", "-f", f"dlv-ws-{run_id}"], timeout_s=60)
        box.binding.finished = True

    def _require(self, sandbox_id: str) -> ZbmDockerSandbox:
        with self._lock:
            box = self._boxes.get(sandbox_id)
        if box is None:
            raise RuntimeError("no such sandbox")
        return box

