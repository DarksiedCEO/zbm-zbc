"""
Tool-call classifier (spec §B.5, §C.3.1): pure, deterministic, seeded by ``seed/tool_policy_seed.json``.

``classify(seed, tool_name, tool_input, ctx)`` → ``Verdict(klass, needs_token, deny, code, message)``. A bash command
is split into simple commands on ``;``, ``&&``, ``||`` and ``|`` after ``shlex`` tokenisation, every simple command is
classified, and the whole call takes the most restrictive class. The raw string is scanned as well as the tokens:
``$(``, backticks, ``eval``/``exec``, ``xargs``, ``sh -c``/``bash -c`` and a ``python -c`` that mentions
``subprocess``/``os.system`` are refused, so ``g''it push`` (shlex → ``git push``) and ``$(echo git) push`` (raw
rule) are both denied ``git_remote``/``unknown``.

This is a denylist over an unbounded language (df-exec F-03). The BOUNDARY is the sandbox (§C.2: no network, no
docker socket, non-root, read-only root); this classifier is the record and the first refusal.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Any, Optional

WORKSPACE = "/mnt/user-data/workspace"       # deer-flow's virtual prefix (config/paths.py VIRTUAL_PATH_PREFIX)
SKILLS_MOUNT = "/mnt/skills"
ORDER = ("read", "write", "exec", "subagent", "network", "git_remote", "destructive_outside_workspace", "acp", "mcp",
         "self_modify", "unknown")
DENY_UNCONDITIONAL = ("git_remote", "destructive_outside_workspace", "acp", "mcp", "self_modify", "network")
_SPLIT = re.compile(r"\|\||&&|;|\|")
_RM_RECURSIVE = re.compile(r"^-[a-zA-Z]*[rR][a-zA-Z]*$")


@dataclass(frozen=True)
class Verdict:
    klass: str
    needs_token: bool
    deny: bool
    code: str
    message: str

    @property
    def unconditional(self) -> bool:
        return self.klass in DENY_UNCONDITIONAL or self.klass == "unknown"


@dataclass(frozen=True)
class Context:
    service: str
    workspace: str = WORKSPACE
    evidence_root: str = ""


def _rank(klass: str) -> int:
    return ORDER.index(klass)


def _norm(path: str, cwd: str) -> str:
    """Normalise a sandbox path against ``cwd`` (posix, no ``..`` left, no trailing slash)."""
    p = path if path.startswith("/") else posixpath.join(cwd, path)
    return posixpath.normpath(p)


def inside(path: str, root: str) -> bool:
    p, r = posixpath.normpath(path), posixpath.normpath(root)
    return p == r or p.startswith(r.rstrip("/") + "/")


def write_allowed(path: str, ctx: Context, cwd: Optional[str] = None) -> bool:
    """§B.5 write class: inside ``<workspace>/services/<service>/`` or ``<workspace>/docs/adr/00NN-*.md``."""
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    if not path.startswith("/") and not cwd:
        return False
    p = _norm(path, cwd or ctx.workspace)
    if ".." in path.split("/"):
        return False
    svc = posixpath.join(ctx.workspace, "services", ctx.service)
    if inside(p, svc) and p != svc:
        return True
    adr_dir = posixpath.join(ctx.workspace, "docs", "adr")
    name = posixpath.basename(p)
    return posixpath.dirname(p) == adr_dir and re.fullmatch(r"00[0-9][0-9]-[a-z0-9\-]+\.md", name) is not None


def read_allowed(path: str, ctx: Context, cwd: Optional[str] = None) -> bool:
    if not isinstance(path, str) or not path or "\x00" in path:
        return False
    p = _norm(path, cwd or ctx.workspace)
    return inside(p, ctx.workspace) or inside(p, SKILLS_MOUNT)


def _raw_rule_hit(seed: dict, raw: str) -> Optional[str]:
    low = raw
    for needle in seed.get("raw_string_denies", []):
        if needle in low:
            return needle
    return None


def _tokenise(raw: str) -> Optional[list[tuple[list[str], list[str]]]]:
    """shlex with punctuation_chars: control operators and redirections come out as their own tokens, so quotes are
    honoured (``g''it`` → ``git``) and ``;`` inside a quoted string never splits a command. Returns
    ``[(argv, redirect_targets)]`` per simple command, or None when the string cannot be tokenised."""
    try:
        lex = shlex.shlex(raw, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    out: list[tuple[list[str], list[str]]] = []
    argv: list[str] = []
    redirects: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in (";", "&&", "||", "|", "&", ";;", "|&"):
            if argv or redirects:
                out.append((argv, redirects))
            argv, redirects = [], []
        elif t in ("(", ")"):
            return None
        elif t in (">", ">>", "<", "<<", "<<<", ">|", "&>", ">&"):
            if i + 1 < len(tokens):
                if t in (">", ">>", ">|", "&>"):
                    redirects.append(tokens[i + 1])
                i += 1
        elif t.startswith(("1>", "2>", "&>")) or re.fullmatch(r"[0-9]?>{1,2}\S*", t):
            target = re.sub(r"^[0-9]?>{1,2}", "", t)
            if target and target not in ("&1", "&2"):
                redirects.append(target)
            elif i + 1 < len(tokens) and not target:
                redirects.append(tokens[i + 1])
                i += 1
        else:
            argv.append(t)
        i += 1
    if argv or redirects:
        out.append((argv, redirects))
    return out


def _strip_env_assignments(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[i]):
        i += 1
    return argv[i:]


def _basename(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _git_class(seed: dict, argv: list[str]) -> str:
    """argv[0] == git. Read subcommands → read; everything else → git_remote (the engine does the writes)."""
    classes = seed["classes"]
    remote_flags = set(classes["git_remote"].get("flags_anywhere", []))
    if any(a in remote_flags for a in argv[1:]):
        return "git_remote"
    # skip global options like -C <path> / --no-pager
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    sub = argv[i] if i < len(argv) else ""
    rest = argv[i + 1:]
    read_subs = classes["read"].get("git_subcommands", [])
    if sub == "branch":
        listing = ("--list", "--show-current", "-l", "-a", "-r", "-v", "-vv", "--all", "--remotes")
        if all(a in listing or a.startswith("--format") or not a.startswith("-") for a in rest) and \
                not any(a in ("-D", "-d", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "-f", "--force",
                              "--set-upstream-to", "-u") for a in rest) and (not rest or rest[0].startswith("-")):
            return "read"
        return "git_remote"
    if sub in read_subs:
        if sub == "checkout" or any(a.startswith("-") and a in remote_flags for a in rest):
            return "git_remote"
        return "read"
    return "git_remote"


def _network_hit(seed: dict, raw_piece: str, argv: list[str]) -> bool:
    net = seed["classes"]["network"]
    if _basename(argv[0]) in net.get("bash_argv0", []):
        return True
    joined = " ".join(argv) + " "
    for pat in net.get("bash_patterns", []):
        if pat in joined or pat in raw_piece:
            return True
    return False


def _python_c_words(argv: list[str]) -> str:
    """The code passed to ``python -c`` / ``python3 -c`` (empty when absent)."""
    if _basename(argv[0]) not in ("python", "python3") or len(argv) < 3:
        return ""
    for i, a in enumerate(argv[1:], 1):
        if a == "-c" and i + 1 < len(argv):
            return argv[i + 1]
    return ""


def _rm_outside(argv: list[str], ctx: Context, cwd: str) -> bool:
    """rm with a recursive flag (or any rm of .git / the evidence root / '/') whose target escapes the workspace."""
    flags = [a for a in argv[1:] if a.startswith("-")]
    targets = [a for a in argv[1:] if not a.startswith("-")]
    recursive = any(_RM_RECURSIVE.fullmatch(f) for f in flags) or "--recursive" in flags
    for t in targets:
        if ".." in t.split("/"):
            return True
        p = _norm(t, cwd)
        if p in ("/", ctx.workspace) or (ctx.evidence_root and inside(p, ctx.evidence_root)):
            return True
        if posixpath.basename(p) == ".git" or inside(p, posixpath.join(ctx.workspace, ".git")):
            return True
        if t.startswith("~") or "$" in t:
            return True
        if not inside(p, ctx.workspace):
            return True
        if recursive and p == ctx.workspace:
            return True
    return False


def _find_delete_outside(argv: list[str], ctx: Context, cwd: str) -> bool:
    if "-delete" not in argv and "-exec" not in argv:
        return False
    targets = [a for a in argv[1:] if not a.startswith("-")]
    start = targets[0] if targets else "."
    if "-exec" in argv:
        i = argv.index("-exec")
        if i + 1 < len(argv) and _basename(argv[i + 1]) != "rm":
            return False
    return not inside(_norm(start, cwd), ctx.workspace) or ".." in start.split("/")


def _classify_bash(seed: dict, raw: str, ctx: Context) -> tuple[str, str]:
    classes = seed["classes"]
    hit = _raw_rule_hit(seed, raw)
    if hit is not None:
        return "unknown", f"raw string contains {hit!r} (indirect execution is refused)"
    cmds = _tokenise(raw)
    if cmds is None:
        return "unknown", "command could not be tokenised"
    if not cmds:
        return "unknown", "empty command"
    worst, why, cwd = "read", "read-only command", ctx.workspace
    for argv, redirects in cmds:
        raw_piece = raw
        argv = _strip_env_assignments(argv)
        for target in redirects:
            if target.startswith("/dev/"):
                continue
            if ".." in target.split("/") or "$" in target or target.startswith("~") or \
                    not inside(_norm(target, cwd), ctx.workspace):
                return "destructive_outside_workspace", "output redirected outside the workspace"
            if _rank("exec") > _rank(worst):
                worst, why = "exec", "output redirection writes inside the workspace"
        if not argv:
            continue
        name = _basename(argv[0])
        if name == "cd":
            target = argv[1] if len(argv) > 1 else ctx.workspace
            if ".." in target.split("/") or not inside(_norm(target, cwd), ctx.workspace):
                return "destructive_outside_workspace", "cd outside the workspace"
            cwd = _norm(target, cwd)
            continue
        if name in ("sh", "bash", "zsh", "dash") and any(a == "-c" for a in argv[1:]):
            return "unknown", "sh -c / bash -c is refused"
        if name in ("eval", "exec", "xargs", "env", "nohup", "sudo", "su", "doas", "chroot", "nsenter", "unshare"):
            if name == "env" and len(argv) == 1:
                klass, w = "read", "env listing"
            else:
                return "unknown", f"{name} is refused (indirect execution)"
        elif name == "git":
            klass = _git_class(seed, argv)
            w = "git write/remote subcommand (the engine adds, commits and stashes; engineers never do)" if klass == "git_remote" else "git read"
        elif name in classes["git_remote"].get("bash_argv0", []):
            klass, w = "git_remote", f"{name} reaches a remote"
        elif _network_hit(seed, raw_piece, argv):
            klass, w = "network", f"{name} reaches the network / installs a dependency"
        elif name == "rm":
            if _rm_outside(argv, ctx, cwd):
                return "destructive_outside_workspace", "rm target outside the workspace, the workspace .git, the evidence root or /"
            klass, w = "exec", "rm inside the workspace"
        elif name == "find" and _find_delete_outside(argv, ctx, cwd):
            return "destructive_outside_workspace", "find -delete/-exec rm outside the workspace"
        elif name == "find" and ("-delete" in argv or "-exec" in argv):
            klass, w = "exec", "find -delete/-exec inside the workspace"
        else:
            code = _python_c_words(argv)
            if code:
                for word in seed.get("python_c_deny_words", []):
                    if word in code:
                        if word in ("shutil.rmtree", "os.remove", "os.unlink", "os.rmdir"):
                            return "destructive_outside_workspace", f"python -c mentions {word}"
                        return "unknown", f"python -c mentions {word}"
                for word in classes["network"].get("python_c_words", []):
                    if word in code:
                        return "network", f"python -c mentions {word}"
            if name in classes["read"].get("bash_argv0", []):
                klass, w = "read", "read-only command"
                if name == "pytest" or (name in ("python", "python3") and "-m" in argv and "pytest" in argv):
                    klass, w = "exec", "test command"
            elif name in classes["exec"].get("bash_argv0", []):
                klass, w = "exec", f"{name} inside the workspace"
                if name == "pytest" and any(a in classes["read"].get("pytest_flags_read", []) for a in argv[1:]):
                    klass, w = "read", "pytest collection only"
            else:
                klass, w = "unknown", f"{name!r} is not in the tool policy seed"
        if _rank(klass) > _rank(worst):
            worst, why = klass, w
        # any path argument escaping the workspace on a write-capable command
        if klass == "exec" and name in ("cp", "mv", "tee", "touch", "mkdir", "sed", "chmod", "ln"):
            positional = [a for a in argv[1:] if not a.startswith("-")]
            # cp/mv/ln write only their LAST operand (the destination / link name); the others are read
            targets = positional[-1:] if name in ("cp", "mv", "ln") else positional
            for a in targets:
                if a.startswith("/") and not inside(_norm(a, cwd), ctx.workspace) and not inside(_norm(a, cwd), "/tmp"):
                    return "destructive_outside_workspace", f"{name} targets a path outside the workspace"
    return worst, why


def classify(seed: dict, tool_name: str, tool_input: Any, ctx: Context) -> Verdict:
    classes = seed["classes"]
    inp = tool_input if isinstance(tool_input, dict) else {}
    if not isinstance(tool_name, str) or not tool_name:
        return Verdict("unknown", False, True, "TOOL_DENIED", "tool name missing")
    if tool_name.startswith("deerflow_mcp") or tool_name.startswith("mcp__") or tool_name.startswith("mcp_"):
        return Verdict("mcp", False, True, "TOOL_DENIED", "MCP tools are denied unconditionally")
    for klass in ("acp", "self_modify", "network", "subagent"):
        if tool_name in classes[klass].get("tools", []):
            deny = classes[klass]["decision"] != "allow_with_token"
            return Verdict(klass, not deny, deny, "TOOL_DENIED" if deny else "ALLOW",
                           f"{tool_name} is class {klass}")
    if tool_name in classes["read"].get("tools", []):
        path = inp.get("path")
        if path is not None and not read_allowed(path, ctx):
            return Verdict("destructive_outside_workspace", False, True, "TOOL_DENIED",
                           "read path outside the workspace")
        return Verdict("read", False, False, "ALLOW", "read tool inside the workspace")
    if tool_name in classes["write"].get("tools", []):
        path = inp.get("path")
        if not write_allowed(path if isinstance(path, str) else "", ctx):
            return Verdict("destructive_outside_workspace", False, True, "TOOL_DENIED",
                           "write path outside services/<service>/ (or the service ADR)")
        return Verdict("write", True, False, "ALLOW", "write inside the service directory")
    if tool_name == "bash":
        cmd = inp.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return Verdict("unknown", False, True, "TOOL_DENIED", "bash without a command")
        if len(cmd) > 16_384:
            return Verdict("unknown", False, True, "TOOL_DENIED", "bash command longer than 16 KiB")
        klass, why = _classify_bash(seed, cmd, ctx)
        decision = classes[klass]["decision"]
        if decision in ("deny", "deny_unconditionally"):
            return Verdict(klass, False, True, "TOOL_DENIED", why)
        return Verdict(klass, decision == "allow_with_token", False, "ALLOW", why)
    return Verdict("unknown", False, True, "TOOL_DENIED", f"{tool_name!r} is not in the tool policy seed")


def is_test_command(seed_tc: dict, argv: list[str]) -> Optional[str]:
    """The framework name when ``argv`` is one of the seeded test commands (runner-captured), else None."""
    for name, fw in seed_tc["frameworks"].items():
        base = [a for a in fw["suite"]]
        if argv[:len(base)] == base:
            return name
    return None


def rm_recursive_targets(raw: str) -> list[str]:
    """Targets of every recursive ``rm`` in a bash string (for the guardrail's in-sandbox symlink resolution)."""
    cmds = _tokenise(raw) if isinstance(raw, str) else None
    out: list[str] = []
    if not cmds:
        return out
    for argv, _ in cmds:
        argv = _strip_env_assignments(argv)
        if not argv or _basename(argv[0]) != "rm":
            continue
        flags = [a for a in argv[1:] if a.startswith("-")]
        if not (any(_RM_RECURSIVE.fullmatch(f) for f in flags) or "--recursive" in flags):
            continue
        out += [a for a in argv[1:] if not a.startswith("-")]
    return out
