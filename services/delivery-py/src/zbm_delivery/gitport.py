"""
GitPort (spec §C.8.2, D8): fixed argv, ``shell=False``, 60 s per command, every call recorded
``crossing_git_requested`` first. The port exposes exactly the read subcommands (``status``, ``diff``, ``log``,
``rev-parse``, ``merge-base``, ``for-each-ref``), the engine's writes (``add``, ``commit``, ``worktree add``) and the
one revert-check form of ``stash`` (``push --include-untracked -- <paths>`` / ``pop``); there is no generic
``run(argv)``, so a push, pull, fetch, merge, reset or branch deletion cannot be expressed through it.

``for-each-ref`` is the read-only listing D8 needs to compute ``N`` (the highest existing ``fix<N>-`` branch); the
spec's list omits it (ADR 0011). Commit messages end with the FIX_WAVE_1 attribution lines and the run id.

Round 18: ``remote`` (no arguments: the listing) is a read — a run worktree must have NO remotes (R4: ``git push``
has nowhere to go even if the classifier is wrong; a linked worktree shares the repository's config, so the
repository itself must be remote-less); ``archive`` (``git archive --format=tar <sha> -- <path>``) is the read the
engine's split-diff verification checkout is built from (R1).

Round 19 R11 (N19-A-7): every git command runs with a PRIVATE empty ``HOME`` (a temp directory the port owns),
``GIT_CONFIG_GLOBAL=/dev/null``, ``GIT_CONFIG_NOSYSTEM=1`` and ``-c core.hooksPath=<empty engine dir>`` /
``-c core.fsmonitor=false`` on every argv — a ``.gitconfig`` or a hooks directory tracked in the worktree can
neither become git's global config nor run a hook on the engine's commit.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from zbm_delivery import fsops
from zbm_delivery.ledger import derived_id

ACTOR = "intel_08_engine"
TIMEOUT_S = 60
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_BRANCH_RE = re.compile(r"^fix([0-9]{1,6})-([a-z0-9][a-z0-9\-]{0,60})$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,200}$")
ATTRIBUTION = ("Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>",
               "Claude-Session: https://claude.ai/code/session_01PpaQ7aW6QSQ7YuApikJFfv")


class GitRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class GitResult:
    exit_code: int
    stdout: str
    stderr: str


def _check_path(p: str) -> str:
    if not isinstance(p, str) or not p or p.startswith("-") or "\x00" in p or ".." in p.split("/"):
        raise GitRefused("bad path")
    return p


# Wave 25 (scout C C6-3): the private HOME and EMPTY hooks dir are made on FIRST USE, once per process, never at
# import — a process that only imported the package and was then killed by a signal (no atexit) left a `dlv-git-*`
# dir in its temp dir. A process killed after its first git command can still leave one: atexit is the only cleanup.
_ISOLATION: list = []                      # [(isolation dir, private home, empty hooks dir)] once made
_ISOLATION_LOCK = threading.Lock()


def _isolation() -> tuple[str, str]:
    """(private HOME, EMPTY hooks dir) of this process, made on first call and removed at exit (fix wave 21, L4)."""
    with _ISOLATION_LOCK:
        if not _ISOLATION:
            d = tempfile.mkdtemp(prefix="dlv-git-")
            atexit.register(fsops.drop_own_temp, d)
            home, hooks = os.path.join(d, "home"), os.path.join(d, "hooks")
            os.makedirs(home, exist_ok=True)
            os.makedirs(hooks, exist_ok=True)
            _ISOLATION.append((d, home, hooks))
        return _ISOLATION[0][1], _ISOLATION[0][2]


def isolation_args() -> tuple[str, ...]:
    """The ``-c`` options on every git argv (R11): the empty hooks dir, no fsmonitor; fix wave 24 (E3, N23-D-2):
    rename/copy detection off for every command — a file moved or copied into src/ is a full addition (every line
    shown, every line scanned), never a "similarity index 100%" with no + lines."""
    return ("-c", f"core.hooksPath={_isolation()[1]}", "-c", "core.fsmonitor=false", "-c", "diff.renames=false",
            "-c", "status.renames=false")


def git_env() -> dict:
    """The environment of every git command (R11): a private empty HOME, no global or system config."""
    home = _isolation()[0]
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "XDG_CONFIG_HOME": os.path.join(home, "xdg"),
            "GIT_AUTHOR_NAME": "zbm-fix-engine", "GIT_AUTHOR_EMAIL": "fix-engine@zbm.invalid",
            "GIT_COMMITTER_NAME": "zbm-fix-engine", "GIT_COMMITTER_EMAIL": "fix-engine@zbm.invalid"}


class GitPort:
    def __init__(self, repo_path: str, *, record: Callable[..., str], runner: Optional[Callable] = None):
        if not repo_path or not os.path.isdir(os.path.join(repo_path, ".git")) and not os.path.isfile(os.path.join(repo_path, ".git")):
            raise GitRefused("DLV_REPO_PATH is not a git repository")
        self.repo = os.path.realpath(repo_path)
        self.record = record
        self._run = runner or self._subprocess
        self.calls: list[list[str]] = []
        self._seq = 0

    # --- plumbing ---------------------------------------------------------------------------------------------------

    @staticmethod
    def _subprocess(argv: Sequence[str], cwd: str) -> GitResult:
        try:
            r = subprocess.run(list(argv), cwd=cwd, capture_output=True, timeout=TIMEOUT_S, shell=False, env=git_env(),
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return GitResult(124, "", "git command timed out")
        except OSError as exc:
            return GitResult(127, "", type(exc).__name__)
        return GitResult(r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace"))

    def _git(self, op: str, args: list[str], cwd: str, run_id: str = "-") -> GitResult:
        argv = ["git", *isolation_args(), "-C", cwd, *args]
        self._seq += 1
        self.record(derived_id("gt", run_id, self._seq, op, hashlib.sha256("\0".join(argv).encode()).hexdigest()),
                    "crossing_git_requested", ACTOR, run_id if run_id != "-" else "git",
                    {"op": op, "argv_sha256": hashlib.sha256("\0".join(argv).encode()).hexdigest(), "seq": self._seq,
                     "run_id": run_id}, f"git {op} requested")
        self.calls.append(argv)
        return self._run(argv, cwd)

    def _ok(self, r: GitResult, what: str) -> str:
        if r.exit_code != 0:
            raise GitRefused(f"git {what} failed (exit {r.exit_code})")
        return r.stdout

    # --- reads --------------------------------------------------------------------------------------------------------

    def rev_parse(self, ref: str, cwd: Optional[str] = None, run_id: str = "-") -> str:
        if not _REF_RE.fullmatch(ref) or ref.startswith("-"):
            raise GitRefused("bad ref")
        out = self._ok(self._git("rev-parse", ["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd or self.repo, run_id),
                       "rev-parse").strip()
        if not _SHA_RE.fullmatch(out):
            raise GitRefused("rev-parse answered no sha")
        return out

    def blob_exists(self, sha: str, path: str, run_id: str = "-") -> bool:
        if not _SHA_RE.fullmatch(sha):
            raise GitRefused("bad sha")
        _check_path(path)
        r = self._git("rev-parse", ["rev-parse", "--verify", "-q", f"{sha}:{path}"], self.repo, run_id)
        return r.exit_code == 0

    def is_ancestor(self, base_sha: str, ref: str, run_id: str = "-") -> bool:
        if not _SHA_RE.fullmatch(base_sha) or not _REF_RE.fullmatch(ref) or ref.startswith("-"):
            raise GitRefused("bad sha/ref")
        r = self._git("merge-base", ["merge-base", "--is-ancestor", base_sha, ref], self.repo, run_id)
        return r.exit_code == 0

    def branches(self, run_id: str = "-") -> list[str]:
        out = self._ok(self._git("for-each-ref", ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
                                 self.repo, run_id), "for-each-ref")
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def next_fix_branch(self, service: str, run_id: str = "-") -> str:
        """D8: ``fix<N>-<service>`` with N = 1 + the highest existing ``fix<N>-`` number in the repo."""
        highest = 0
        for b in self.branches(run_id):
            m = _BRANCH_RE.fullmatch(b)
            if m:
                highest = max(highest, int(m.group(1)))
        return f"fix{highest + 1}-{service}"

    def status(self, worktree: str, run_id: str = "-") -> str:
        return self._ok(self._git("status", ["status", "--porcelain=v1", "--untracked-files=all"], worktree, run_id), "status")

    def diff(self, worktree: str, *, staged: bool = False, run_id: str = "-") -> str:
        args = ["diff", "--no-renames", "--no-color", "--no-ext-diff"] + (["--cached"] if staged else [])
        return self._ok(self._git("diff", args, worktree, run_id), "diff")

    def diff_name_only(self, worktree: str, pathspec: Sequence[str] = (), run_id: str = "-", commit: Optional[str] = None,
                       base: Optional[str] = None) -> list[str]:
        """Changed paths: of the worktree, of one ``commit``, or between ``base`` and ``commit`` (wave 24). Renames are
        never detected (E3): a moved file is its old path deleted and its new path added."""
        args = ["diff", "--no-renames", "--name-only", "--no-color"]
        if commit:
            if not _SHA_RE.fullmatch(commit) or (base is not None and not _SHA_RE.fullmatch(base)):
                raise GitRefused("bad sha")
            args = ["diff", "--no-renames", "--name-only", "--no-color", base or f"{commit}^", commit]
        if pathspec:
            args += ["--", *[_check_path(p) for p in pathspec]]
        out = self._ok(self._git("diff", args, worktree, run_id), "diff --name-only")
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def commit_diff(self, worktree: str, sha: str, run_id: str = "-") -> str:
        if not _SHA_RE.fullmatch(sha):
            raise GitRefused("bad sha")
        r = self._git("diff", ["diff", "--no-renames", "--no-color", "--no-ext-diff", f"{sha}^", sha], worktree, run_id)
        return r.stdout if r.exit_code == 0 else ""

    def range_diff(self, cwd: str, base: str, head: str, paths: Sequence[str], run_id: str = "-") -> str:
        """Wave 24 (E2): the complete diff ``base..head`` of ``paths`` (renames off: a moved file in full). An empty
        path list is an empty diff (never the whole tree)."""
        if not _SHA_RE.fullmatch(base) or not _SHA_RE.fullmatch(head):
            raise GitRefused("bad sha")
        if not paths:
            return ""
        args = ["diff", "--no-renames", "--no-color", "--no-ext-diff", base, head, "--", *[_check_path(p) for p in paths]]
        return self._ok(self._git("diff", args, cwd, run_id), "diff base..head")

    def changed_paths(self, worktree: str, run_id: str = "-") -> list[str]:
        """Modified, added and untracked paths of the worktree (from ``status --porcelain``)."""
        out = []
        for ln in self.status(worktree, run_id).splitlines():
            if len(ln) > 3:
                p = ln[3:]
                if " -> " in p:
                    p = p.split(" -> ", 1)[1]
                out.append(p.strip('"'))
        return out

    def show_file(self, ref: str, path: str, cwd: str, run_id: str = "-") -> Optional[str]:
        """``git show <ref>:<path>`` (a read); None when the path is not in that commit."""
        if not _REF_RE.fullmatch(ref) or ref.startswith("-"):
            raise GitRefused("bad ref")
        _check_path(path)
        r = self._git("show", ["show", f"{ref}:{path}"], cwd, run_id)
        return r.stdout if r.exit_code == 0 else None

    def remotes(self, cwd: str, run_id: str = "-") -> list[str]:
        """``git remote`` (the listing only; never add/remove/set-url)."""
        out = self._ok(self._git("remote", ["remote"], cwd, run_id), "remote")
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def archive(self, sha: str, path: str, run_id: str = "-") -> bytes:
        """A tar stream of ``path`` at commit ``sha`` (read-only; the verification checkout's base tree, R1)."""
        if not _SHA_RE.fullmatch(sha):
            raise GitRefused("bad sha")
        _check_path(path)
        argv = ["git", *isolation_args(), "-C", self.repo, "archive", "--format=tar", sha, "--", path]
        self._seq += 1
        self.record(derived_id("gt", run_id, self._seq, "archive", hashlib.sha256("\0".join(argv).encode()).hexdigest()),
                    "crossing_git_requested", ACTOR, run_id if run_id != "-" else "git",
                    {"op": "archive", "argv_sha256": hashlib.sha256("\0".join(argv).encode()).hexdigest(), "seq": self._seq,
                     "run_id": run_id}, "git archive requested")
        self.calls.append(argv)
        r = self._run_bytes(argv, self.repo)
        if r[0] != 0:
            raise GitRefused(f"git archive failed (exit {r[0]})")
        return r[1]

    @staticmethod
    def _run_bytes(argv: Sequence[str], cwd: str) -> tuple[int, bytes]:
        try:
            r = subprocess.run(list(argv), cwd=cwd, capture_output=True, timeout=TIMEOUT_S, shell=False, env=git_env(),
                               stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError):
            return 124, b""
        return r.returncode, r.stdout

    def log(self, worktree: str, n: int = 20, run_id: str = "-") -> list[str]:
        out = self._ok(self._git("log", ["log", f"--max-count={max(1, min(int(n), 200))}", "--format=%H"], worktree, run_id), "log")
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    # --- writes (the engine's own) ------------------------------------------------------------------------------------

    def worktree_add(self, path: str, branch: str, base_sha: str, run_id: str = "-") -> None:
        if not _BRANCH_RE.fullmatch(branch) or not _SHA_RE.fullmatch(base_sha):
            raise GitRefused("bad branch/sha")
        if not os.path.isabs(path) or os.path.exists(path):
            raise GitRefused("worktree path must be absolute and absent")
        if self.remotes(self.repo, run_id):
            raise GitRefused("the engine's repository has remotes; a run worktree must have none (R4: strip them from "
                             "DLV_REPO_PATH — the engine only ever works on a remote-less local repository)")
        self._ok(self._git("worktree add", ["worktree", "add", "-b", branch, "--", path, base_sha], self.repo, run_id),
                 "worktree add")
        if self.remotes(path, run_id):
            raise GitRefused("the new worktree has remotes (R4); refusing to run in it")

    def worktree_add_existing(self, path: str, branch: str, run_id: str = "-") -> None:
        """Re-open an existing fix branch in a fresh worktree (a review-fail rerun, §C.8.7)."""
        if not _BRANCH_RE.fullmatch(branch):
            raise GitRefused("bad branch")
        if not os.path.isabs(path) or os.path.exists(path):
            raise GitRefused("worktree path must be absolute and absent")
        self._ok(self._git("worktree add", ["worktree", "add", "--", path, branch], self.repo, run_id), "worktree add")

    def add(self, worktree: str, paths: Sequence[str], run_id: str = "-") -> None:
        if not paths:
            raise GitRefused("nothing to add")
        self._ok(self._git("add", ["add", "--", *[_check_path(p) for p in paths]], worktree, run_id), "add")

    def commit(self, worktree: str, subject: str, body: str, run_id: str, *, allow_empty: bool = False) -> str:
        subject = " ".join(subject.split())[:120]
        if not subject:
            raise GitRefused("empty commit subject")
        message = f"{subject}\n\n{body.strip()}\n\nRun: {run_id}\n\n" + "\n".join(ATTRIBUTION) + "\n"
        args = ["commit", "--quiet", "--no-gpg-sign", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        self._ok(self._git("commit", args, worktree, run_id), "commit")
        return self.rev_parse("HEAD", worktree, run_id)

    def stash_push(self, worktree: str, paths: Sequence[str], run_id: str = "-") -> None:
        """The one extra subcommand (§C.8.4 step 4), restricted to this form."""
        if not paths:
            raise GitRefused("stash needs paths")
        self._ok(self._git("stash push", ["stash", "push", "--quiet", "--include-untracked", "--",
                                          *[_check_path(p) for p in paths]], worktree, run_id), "stash push")

    def stash_pop(self, worktree: str, run_id: str = "-") -> None:
        self._ok(self._git("stash pop", ["stash", "pop", "--quiet"], worktree, run_id), "stash pop")
