"""
Tool-call classifier (spec §B.5, §C.3.1): pure, deterministic, seeded by ``seed/tool_policy_seed.json``.

``classify(seed, tool_name, tool_input, ctx)`` → ``Verdict(klass, needs_token, deny, code, message)``. A bash command
is split into simple commands on ``;``, ``&&``, ``||`` and ``|`` after ``shlex`` tokenisation, every simple command is
classified, and the whole call takes the most restrictive class. The raw string is scanned as well as the tokens:
``$(``, backticks, ``eval``/``exec``, ``xargs``, ``sh -c``/``bash -c`` and a ``python -c`` that mentions
``subprocess``/``os.system`` are refused, so ``g''it push`` (shlex → ``git push``) and ``$(echo git) push`` (raw
rule) are both denied ``git_remote``/``unknown``.

This is a denylist over an unbounded language (df-exec F-03). The BOUNDARY is the sandbox (§C.2: ``--network none``,
no docker socket, non-root, read-only root, no ``.git``); this classifier is the record and the first refusal.

Round 18 R5: the classifier cannot parse shells, so an interpreter / shell / ``make`` / ``find -delete`` / ``git -c``
invocation is recorded honestly as ``allow_opaque`` (class ``exec``, ``opaque=True``) — a distinct decision value the
ledger and the report count — never as a classified read/exec. Every write-capable operand (``rm``, ``cp``/``mv``/
``ln`` destinations, ``tee``, ``touch``, ``mkdir``, ``chmod``, ``sed -i``, ``find -delete`` start, redirections) is
normalised against the cwd (after ``cd``), must resolve inside ``services/<service>/`` or ``docs/adr/``, and anything
unresolvable here (a glob, a brace, ``$``, ``~``) is refused; the guardrail then resolves the same operands INSIDE the
container (``readlink -f`` on the longest existing prefix) and denies when that fails or lands outside.

Round 19 (R6, R7, R9): a command carrying any line separator bash knows (``\n``, ``\r``, NUL, ``\f``, ``\v``,
U+2028/2029, U+0085) is refused ``multiline_command`` BEFORE tokenising — the classifier reads one line, bash would
run several. Every other free-text argument the guardrail classifies (tool ``path``/``pattern``) is refused on a
control character too. Destinations named by ``-t DIR`` / ``--target-directory`` are the write target of
``cp``/``mv``/``ln``/``install``; ``chmod``/``chown``/``sed -i``/``--in-place`` operands are targets whatever the
flag spelling; a ``find -exec`` command is classified like any simple command; a bash write under ``docs/adr/`` must
be a ``00NN-*.md`` file, as for the write tool; ``mypy``/``pylint``/``black``/``isort``/``pre-commit``/``ruff``/
``gofmt`` are opaque.

Round 20 (wave 21, R4/R5): a PIPE into any interpreter/shell of ``OPAQUE_ARGV0`` (or any ``*sh`` basename, however
the path is spelled: ``|bash``, ``|  bash``, ``| /bin/bash``, ``| /usr/bin/python3``, ``|& sh``) is refused
``pipe_to_interpreter`` at the token level, after shlex has normalised whitespace and quoting; so is a here-string /
here-document fed to one (``bash <<< ...``: the program comes from the command line, like ``-c``). Running an
interpreter directly (``python3 x.py``, ``bash x.sh``) stays ``allow_opaque``. A HARD link is write-class on both
operands: ``ln`` without ``-s``/``--symbolic``, ``cp`` with ``-l``/``--link`` (alone or in a cluster such as
``-al``) — every source must normalise inside the write roots too (and is re-resolved in the container by the
guardrail), else ``destructive_outside_workspace``; ``link`` is not in the seed and stays ``unknown`` (denied).
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
_UNRESOLVABLE = re.compile(r"[*?\[\]{}$~`]")
# R5: argv0s whose effect the classifier cannot see (they interpret files, programs or Makefiles)
OPAQUE_ARGV0 = ("python", "python3", "pytest", "sh", "bash", "zsh", "dash", "node", "awk", "gawk", "mawk", "sed", "make",
                "cargo", "go", "npm", "npx", "rustc", "perl", "ruby",
                # R9: linters/formatters load plugins or configuration from the tree they are pointed at
                "mypy", "pylint", "black", "isort", "pre-commit", "ruff", "gofmt")
# R4 (wave 21): a pipe or here-string into one of these hands it a PROGRAM on stdin (also every *sh basename)
PIPE_INTERPRETERS = frozenset(OPAQUE_ARGV0)
_SHELL_NAME = re.compile(r"^[a-z]*sh$")
WRITE_ARGV0 = ("cp", "mv", "tee", "touch", "mkdir", "sed", "chmod", "chown", "chgrp", "ln", "rm", "find", "rmdir", "install",
               "truncate", "dd")
# R8: the resolver's caps, enforced here too (the classifier is the record and the first refusal)
MAX_WRITE_OPERANDS = 16
MAX_PATH_DEPTH = 64
# R6: every separator bash treats as a line/command boundary that shlex would fold into whitespace or a word
LINE_SEPARATORS = ("\n", "\r", "\x00", "\x0c", "\x0b", "\u2028", "\u2029", "\x85")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x85\u2028\u2029]")
_CHMOD_MODE = re.compile(r"^(?:[0-7]{3,4}|[ugoa]*[-+=][rwxXstugo]*(?:,[ugoa]*[-+=][rwxXstugo]*)*)$")
_SHORT_CLUSTER = re.compile(r"^-[A-Za-z]+")
# options of cp/mv/ln/install/sed/chmod/chown that consume the NEXT token (so it is never an operand)
_OPT_WITH_ARG = {"cp": ("-t", "-S", "--target-directory", "--suffix", "--backup", "--context"),
                 "mv": ("-t", "-S", "--target-directory", "--suffix", "--backup"),
                 "ln": ("-t", "-S", "--target-directory", "--suffix", "--backup"),
                 "install": ("-t", "-S", "-m", "-o", "-g", "-D", "--target-directory", "--suffix", "--backup", "--mode",
                             "--owner", "--group", "--context", "--strip-program"),
                 "sed": ("-e", "-f", "-l", "--expression", "--file", "--line-length"),
                 "chmod": ("--reference",), "chown": ("--reference", "--from"), "chgrp": ("--reference",),
                 "touch": ("-d", "-t", "-r", "--date", "--reference")}


@dataclass(frozen=True)
class Verdict:
    klass: str
    needs_token: bool
    deny: bool
    code: str
    message: str
    opaque: bool = False                      # R5: an allowed exec whose effect the classifier cannot see
    write_targets: tuple = ()                 # cwd-normalised paths the guardrail re-resolves inside the container

    @property
    def unconditional(self) -> bool:
        return self.klass in DENY_UNCONDITIONAL or self.klass == "unknown"

    @property
    def decision(self) -> str:
        if self.deny:
            return "deny"
        return "allow_opaque" if self.opaque else "allow"


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


def write_roots(ctx: Context) -> tuple[str, str]:
    return posixpath.join(ctx.workspace, "services", ctx.service), posixpath.join(ctx.workspace, "docs", "adr")


def exec_write_ok(target: str, ctx: Context, cwd: str, *, allow_root: bool = False) -> Optional[str]:
    """None when ``target`` (an operand of a write-capable exec command) is a plain path that normalises inside the
    write roots; else the reason it is refused (R5: fail closed on anything unresolvable)."""
    if not target or "\x00" in target:
        return "empty operand"
    if _UNRESOLVABLE.search(target):
        return "operand with glob/brace/variable/tilde (unresolvable before execution)"
    if ".." in target.split("/"):
        return "operand with .."
    p = _norm(target, cwd)
    if p in ("/", ctx.workspace) or posixpath.basename(p) == ".git" or inside(p, posixpath.join(ctx.workspace, ".git")):
        return "operand is the workspace, / or a .git"
    if ctx.evidence_root and inside(p, ctx.evidence_root):
        return "operand inside the evidence root"
    roots = write_roots(ctx)
    if (p in roots and not allow_root) or not any(inside(p, r) for r in roots):
        return "operand outside services/<service>/ and docs/adr/"
    if inside(p, roots[1]) and p != roots[1] and not adr_name_ok(p, ctx):
        return "a write under docs/adr/ must be docs/adr/00NN-<name>.md (R7)"
    return None


def adr_name_ok(p: str, ctx: Context) -> bool:
    """``<workspace>/docs/adr/00NN-<name>.md`` exactly (no subdirectory, no other name) — the write tool's rule,
    applied to bash operands too (N19-A-8)."""
    adr_dir = posixpath.join(ctx.workspace, "docs", "adr")
    name = posixpath.basename(p)
    return posixpath.dirname(p) == adr_dir and re.fullmatch(r"00[0-9][0-9]-[a-z0-9\-]+\.md", name) is not None


def has_line_separator(text: str) -> bool:
    """R6: any separator bash treats as the end of a command that the tokeniser would not (or would fold)."""
    return any(sep in text for sep in LINE_SEPARATORS)


def has_control(text: str) -> bool:
    return bool(_CONTROL.search(text))


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
    parsed = _tokenise_meta(raw)
    return None if parsed is None else [(argv, redirects) for argv, redirects, _ in parsed]


def _tokenise_meta(raw: str) -> Optional[list[tuple[list[str], list[str], dict]]]:
    """``_tokenise`` plus, per simple command, how its stdin is fed (R4, wave 21): ``piped`` when it follows ``|`` or
    ``|&``, ``here`` when it carries a here-string / here-document (``<<<``, ``<<``)."""
    try:
        lex = shlex.shlex(raw, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    out: list[tuple[list[str], list[str], dict]] = []
    argv: list[str] = []
    redirects: list[str] = []
    meta: dict = {"piped": False, "here": False}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in (";", "&&", "||", "|", "&", ";;", "|&"):
            if argv or redirects:
                out.append((argv, redirects, meta))
            argv, redirects = [], []
            meta = {"piped": t in ("|", "|&"), "here": False}
        elif t in ("(", ")"):
            return None
        elif t in (">", ">>", "<", "<<", "<<<", ">|", "&>", ">&", "<<-"):
            if t in ("<<", "<<<", "<<-"):
                meta["here"] = True
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
        out.append((argv, redirects, meta))
    return out


def _stdin_program_target(argv: list[str]) -> Optional[str]:
    """The interpreter/shell a command runs when its stdin is a program: argv[0]'s basename (any spelling of the
    path) when it is in ``PIPE_INTERPRETERS`` or a ``*sh`` name, else None (R4)."""
    words = _strip_env_assignments(argv)
    if not words:
        return None
    name = _basename(words[0])
    return name if name in PIPE_INTERPRETERS or _SHELL_NAME.fullmatch(name) else None


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


def _rm_outside(argv: list[str], ctx: Context, cwd: str) -> Optional[str]:
    """Any rm operand that does not resolve inside the write roots (recursive or not; R5 fails closed on expansion)."""
    for t in (a for a in argv[1:] if not a.startswith("-")):
        why = exec_write_ok(t, ctx, cwd)
        if why:
            return why
    return None


def _find_delete_outside(argv: list[str], ctx: Context, cwd: str) -> Optional[str]:
    if "-delete" not in argv and "-exec" not in argv and "-execdir" not in argv and "-ok" not in argv:
        return None
    targets = [a for a in argv[1:] if not a.startswith("-")]
    start = targets[0] if targets else "."
    return exec_write_ok(start, ctx, cwd, allow_root=True)


def _split_options(name: str, argv: list[str]) -> tuple[list[str], list[str], dict]:
    """(positionals, option tokens, {option: value}) with ``--`` honoured and the options that consume the next
    token (``-t DIR``, ``-e SCRIPT``, ``--target-directory DIR``) taken out of the positionals."""
    takes = _OPT_WITH_ARG.get(name, ())
    positional: list[str] = []
    options: list[str] = []
    values: dict = {}
    i = 1
    after_dd = False
    while i < len(argv):
        a = argv[i]
        if after_dd or a == "-":
            positional.append(a)
        elif a == "--":
            after_dd = True
        elif a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            options.append(k)
            values[k] = v
        elif a.startswith("-") and a in takes:
            options.append(a)
            if i + 1 < len(argv):
                values[a] = argv[i + 1]
                i += 1
        elif a.startswith("-") and len(a) > 2 and not a.startswith("--") and a[:2] in takes:
            options.append(a[:2])                       # -tDIR / -eSCRIPT
            values[a[:2]] = a[2:]
        elif a.startswith("-"):
            options.append(a)
        else:
            positional.append(a)
        i += 1
    return positional, options, values


def _target_directory(values: dict) -> Optional[str]:
    return values.get("-t") if "-t" in values else values.get("--target-directory")


def _write_operands(name: str, argv: list[str], cwd: str) -> Optional[list[str]]:
    """The write-capable operands of one simple command (cwd-relative as written), or None when the command's
    destination cannot be determined (fail closed: the caller refuses)."""
    positional, options, values = _split_options(name, argv)
    if name in ("cp", "mv", "ln", "install"):
        if name == "install" and ("-d" in options or "--directory" in options):
            return positional
        tdir = _target_directory(values)
        if tdir is not None:
            return [tdir]
        if name == "ln" and len(positional) == 1:
            return [posixpath.basename(positional[0]) or "."]        # ln -s TARGET → link named TARGET in cwd
        if len(positional) < 2:
            return None
        return positional[-1:]
    if name == "sed":
        in_place = any(o in ("--in-place",) or (not o.startswith("--") and "i" in o[1:]) for o in options)
        if not in_place:
            return []
        scripted = any(o in ("-e", "-f", "--expression", "--file") for o in options)
        return positional if scripted else positional[1:]
    if name == "find":
        return []
    if name in ("chmod", "chown", "chgrp"):
        if "--reference" in values:
            return positional
        if name == "chmod":
            modes = [i for i, a in enumerate(positional) if _CHMOD_MODE.match(a)]
            if not modes and not any(_CHMOD_MODE.match(o) for o in options):
                return None                                          # no mode at all: not a chmod we understand
            if modes:
                return positional[:modes[0]] + positional[modes[0] + 1:]
            return positional                                        # the mode was a dash form (-x, -w): every positional is a target
        return positional[1:] if positional else None
    if name == "dd":
        return [a[3:] for a in argv[1:] if a.startswith("of=")]
    return positional


def _source_write_operands(name: str, argv: list[str]) -> list[str]:
    """R5 (wave 21): the SOURCE operands a command writes, else []: a hard link — ``ln`` without ``-s``/
    ``--symbolic``, ``cp`` with ``-l``/``--link`` (alone or in a short cluster: ``-al``, ``-la``, ``-rl``) — makes
    the source file writable through the link; ``mv`` removes its sources (swept in the same wave: ``mv
    services/other/a services/<svc>/b`` was allowed)."""
    positional, options, values = _split_options(name, argv)
    shorts = "".join(o[1:] for o in options if o.startswith("-") and not o.startswith("--"))
    if name == "mv":
        pass
    elif name == "ln":
        if "s" in shorts or "--symbolic" in options:
            return []
    elif name == "cp":
        if "l" not in shorts and "--link" not in options:
            return []
    else:
        return []
    if _target_directory(values) is not None:
        return positional                                   # -t DIR: every positional is a source
    if name == "ln" and len(positional) == 1:
        return positional                                   # ln TARGET: a link to TARGET in the cwd
    return positional[:-1]


def _find_subcommands(argv: list[str]) -> list[list[str]]:
    """The commands ``find -exec/-execdir/-ok/-okdir … ;|+`` would run, with ``{}`` standing for the start
    directory (a path under it resolves inside the same root)."""
    out: list[list[str]] = []
    start = next((a for a in argv[1:] if not a.startswith("-")), ".")
    i = 1
    while i < len(argv):
        if argv[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            sub: list[str] = []
            while j < len(argv) and argv[j] not in (";", "+"):
                sub.append(start if argv[j] == "{}" else argv[j])
                j += 1
            if sub:
                out.append(sub)
            i = j
        i += 1
    return out


def _classify_bash(seed: dict, raw: str, ctx: Context) -> tuple[str, str, bool, list[str]]:
    """(class, why, opaque, write targets normalised against the cwd)."""
    classes = seed["classes"]
    if has_line_separator(raw):
        return "unknown", "multiline_command: a command must be a single line (a line separator would run a second command the classifier never saw)", False, []
    if has_control(raw.replace("\t", "")):
        return "unknown", "control character in the command", False, []
    hit = _raw_rule_hit(seed, raw)
    if hit is not None:
        return "unknown", f"raw string contains {hit!r} (indirect execution is refused)", False, []
    parsed = _tokenise_meta(raw)
    if parsed is None:
        return "unknown", "command could not be tokenised", False, []
    if not parsed:
        return "unknown", "empty command", False, []
    # R4 (wave 21): a program fed to an interpreter/shell on stdin is `sh -c` by another name — refused however the
    # pipe, the whitespace or the interpreter's path is spelled (shlex has already normalised all three)
    for argv, _redirects, meta in parsed:
        target = _stdin_program_target(argv)
        if target and meta["piped"]:
            return "unknown", (f"pipe_to_interpreter: output piped into {target} (indirect execution is refused, even when "
                               "harmless; edit files with read_file/str_replace/write_file)"), False, []
        if target and meta["here"]:
            return "unknown", (f"pipe_to_interpreter: a here-string/here-document fed to {target} (indirect execution is "
                               "refused, even when harmless; edit files with read_file/str_replace/write_file)"), False, []
    cmds = [(argv, redirects) for argv, redirects, _ in parsed]
    worst, why, cwd = "read", "read-only command", ctx.workspace
    opaque = False
    targets: list[str] = []
    pending = [(argv, redirects) for argv, redirects in cmds]
    while pending:
        argv, redirects = pending.pop(0)
        raw_piece = raw
        argv = _strip_env_assignments(argv)
        for target in redirects:
            if target.startswith("/dev/"):
                continue
            bad = exec_write_ok(target, ctx, cwd)
            if bad:
                return "destructive_outside_workspace", f"output redirected outside the service directory ({bad})", False, []
            targets.append(_norm(target, cwd))
            if _rank("exec") > _rank(worst):
                worst, why = "exec", "output redirection writes inside the service directory"
        if not argv:
            continue
        name = _basename(argv[0])
        if name == "cd":
            target = argv[1] if len(argv) > 1 else ctx.workspace
            if _UNRESOLVABLE.search(target) or ".." in target.split("/") or not inside(_norm(target, cwd), ctx.workspace):
                return "destructive_outside_workspace", "cd outside the workspace (or unresolvable)", False, []
            cwd = _norm(target, cwd)
            continue
        if name in ("sh", "bash", "zsh", "dash") and any(a == "-c" for a in argv[1:]):
            return "unknown", "sh -c / bash -c is refused", False, []
        if name in ("eval", "exec", "xargs", "env", "nohup", "sudo", "su", "doas", "chroot", "nsenter", "unshare"):
            if name == "env" and len(argv) == 1:
                klass, w = "read", "env listing"
            else:
                return "unknown", f"{name} is refused (indirect execution)", False, []
        elif name == "git":
            klass = _git_class(seed, argv)
            w = "git write/remote subcommand (the engine adds, commits and stashes; engineers never do)" if klass == "git_remote" else "git read"
            if klass == "read" and any(a in ("-c", "--git-dir", "--work-tree", "--exec-path") or a.startswith(("-c", "--git-dir=", "--work-tree=", "--exec-path="))
                                       for a in argv[1:]):
                klass, w, opaque = "exec", "git with -c/--git-dir/--work-tree (opaque: a config value can run a command)", True
        elif name in classes["git_remote"].get("bash_argv0", []):
            klass, w = "git_remote", f"{name} reaches a remote"
        elif _network_hit(seed, raw_piece, argv):
            klass, w = "network", f"{name} reaches the network / installs a dependency"
        elif name == "rm":
            bad = _rm_outside(argv, ctx, cwd)
            if bad:
                return "destructive_outside_workspace", f"rm refused: {bad}", False, []
            klass, w = "exec", "rm inside the service directory"
        elif name == "find" and ("-delete" in argv or "-exec" in argv or "-execdir" in argv or "-ok" in argv):
            bad = _find_delete_outside(argv, ctx, cwd)
            if bad:
                return "destructive_outside_workspace", f"find -delete/-exec refused: {bad}", False, []
            klass, w, opaque = "exec", "find -delete/-exec inside the service directory (opaque)", True
            targets.append(_norm(next((a for a in argv[1:] if not a.startswith("-")), "."), cwd))
            pending = [(sub, []) for sub in _find_subcommands(argv)] + pending    # R9: the -exec'd command is classified too
        else:
            code = _python_c_words(argv)
            if code:
                for word in seed.get("python_c_deny_words", []):
                    if word in code:
                        if word in ("shutil.rmtree", "os.remove", "os.unlink", "os.rmdir"):
                            return "destructive_outside_workspace", f"python -c mentions {word}", False, []
                        return "unknown", f"python -c mentions {word}", False, []
                for word in classes["network"].get("python_c_words", []):
                    if word in code:
                        return "network", f"python -c mentions {word}", False, []
            if name in classes["read"].get("bash_argv0", []):
                klass, w = "read", "read-only command"
                if name == "pytest" or (name in ("python", "python3") and "-m" in argv and "pytest" in argv):
                    klass, w, opaque = "exec", "test command (opaque)", True
            elif name in classes["exec"].get("bash_argv0", []):
                klass, w = "exec", f"{name} inside the workspace"
                if name == "pytest" and any(a in classes["read"].get("pytest_flags_read", []) for a in argv[1:]):
                    klass, w = "exec", "pytest collection only (opaque: conftest and test modules are imported)"
                if name in OPAQUE_ARGV0:
                    opaque = True
                    w = f"{name} (opaque: an interpreter/shell/build tool whose effect the classifier cannot see)"
            else:
                klass, w = "unknown", f"{name!r} is not in the tool policy seed"
        if _rank(klass) > _rank(worst):
            worst, why = klass, w
        # every write-capable operand must normalise inside the write roots (relative operands too, after cd)
        if klass == "exec" and name in WRITE_ARGV0:
            operands = _write_operands(name, argv, cwd)
            if operands is None:
                return "destructive_outside_workspace", f"{name} refused: destination operand missing or unrecognised", False, []
            for a in operands:
                bad = exec_write_ok(a, ctx, cwd)
                if bad:
                    return "destructive_outside_workspace", f"{name} refused: {bad}", False, []
                targets.append(_norm(a, cwd))
            # R5 (wave 21): a hard link's SOURCE is write-class too (the link shares the file); mv removes its sources
            for a in _source_write_operands(name, argv):
                bad = exec_write_ok(a, ctx, cwd)
                if bad:
                    what = "source" if name == "mv" else "hard-link source"
                    return "destructive_outside_workspace", f"{name} refused: {what} {bad}", False, []
                targets.append(_norm(a, cwd))
    return worst, why, opaque, targets


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
    for key in ("path", "pattern", "glob", "old_str", "new_str", "content"):
        val = inp.get(key)
        if key in ("path", "pattern", "glob") and isinstance(val, str) and (has_control(val) or has_line_separator(val)):
            return Verdict("unknown", False, True, "TOOL_DENIED", f"{key} carries a control character or line separator (R6)")
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
        # R7: the guardrail resolves the path inside the container too (a symlink under the service directory)
        return Verdict("write", True, False, "ALLOW", "write inside the service directory", write_targets=(_norm(path, ctx.workspace),))
    if tool_name == "bash":
        cmd = inp.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return Verdict("unknown", False, True, "TOOL_DENIED", "bash without a command")
        if len(cmd) > 16_384:
            return Verdict("unknown", False, True, "TOOL_DENIED", "bash command longer than 16 KiB")
        klass, why, opaque, targets = _classify_bash(seed, cmd, ctx)
        if len(targets) > MAX_WRITE_OPERANDS:
            return Verdict("unknown", False, True, "TOOL_DENIED", f"more than {MAX_WRITE_OPERANDS} write operands in one call (R8)")
        if any(len([c for c in t.split("/") if c]) > MAX_PATH_DEPTH for t in targets):
            return Verdict("unknown", False, True, "TOOL_DENIED", f"a write operand deeper than {MAX_PATH_DEPTH} path components (R8)")
        decision = classes[klass]["decision"]
        if decision in ("deny", "deny_unconditionally"):
            return Verdict(klass, False, True, "TOOL_DENIED", why)
        return Verdict(klass, decision == "allow_with_token", False, "ALLOW", why, opaque=opaque, write_targets=tuple(targets))
    return Verdict("unknown", False, True, "TOOL_DENIED", f"{tool_name!r} is not in the tool policy seed")


def is_test_command(seed_tc: dict, argv: list[str]) -> Optional[str]:
    """The framework name when ``argv`` is one of the seeded test commands (runner-captured), else None."""
    for name, fw in seed_tc["frameworks"].items():
        base = [a for a in fw["suite"]]
        if argv[:len(base)] == base:
            return name
    return None
