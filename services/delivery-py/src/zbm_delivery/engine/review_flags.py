"""
Review flags (fix wave 23, founder design change D2 — "flag, don't chase", Sep 30 2026; fix wave 24, E1).

Three review rounds (20-22) showed that the engine cannot win an arms race against source code that detects it is under
test: every detector the engine learned to defeat (pytest in ``sys.modules``, then the environment, then ``test_``
modules, ``argv``, ``sys.flags``) was replaced by the next one. Detection stays as a cheap layer (``src_content_deny``,
the standalone re-run), but the engine's claim no longer rests on it. Every line a fix ADDS to a SOURCE file (never a
test file) that contains one of the spellings below becomes a review flag — file, line, the construct, the reason —
shown at the top of the report, recorded on the ledger (``review_flags_recorded``); a review that accepts the finding
must carry a note per flag id and the hash of the full source diff it read (wave 24, E2).

Wave 24 (E1, the lead's principle): this is a SPELLING list. It flags the spellings below and claims nothing about
anything else: absence of flags proves nothing, and the report says so above the flags. What carries weight is a
reviewer who read the entire source diff (embedded in the report, bound by ``src_diff_sha256``). Known residual —
ways to observe the execution context this list does NOT spell, stated so nobody reads an empty list as a clean diff:
a module object reached without an import the scanner can read (passed in as an argument, ``type(x)``, ``__loader__``/
``__spec__``, a function's ``__globals__``, a frame from an exception or a generator); bracket or attribute access
built from pieces the patterns do not cover; exec/eval of encoded text (flagged as exec/eval of a non-literal, but
what the text does is not decoded); renamed or re-exported modules (a local module that re-exports ``sys``); file
and /proc probes whose path is built at run time, filesystem probes for ``conftest.py``/``tests`` under a computed
name, the process tree (``ps``), sockets, timing, ctypes and C extensions; and other languages beyond the few Go,
Rust and Node spellings below (any other language: none).

The spellings. Python: ``sys.modules``, ``sys.argv``, ``sys.flags``, ``sys._getframe``, ``inspect``, ``traceback``,
``os.environ``/``getenv``, ``__import__``/``importlib``, ``globals()``/``vars()``/``getattr`` on modules, ``__main__``,
``atexit``, ``signal``, ``threading.enumerate``, ``gc.get_objects``, ``builtins``, string concatenation used to form
identifiers; and (wave 24, cheap and sound for what they spell) the listed modules through an alias (``import sys as
s``, ``from sys import modules as m``: the alias's uses read as the module's) or a star import (``from sys import *``,
flagged itself), ``eval``/``exec``/``compile`` called on anything but a string literal, ``/proc`` path literals,
string literals naming ``conftest``/``pytest``/``test``, and an import of ``pytest``/``_pytest``/``unittest`` in a
source file (also refused by ``src_content_deny``). Go / Rust / Node: ``os.Args``, ``os.Getenv``,
``testing.Testing()``, ``cfg!(test)``, ``std::env``, ``process.argv``, ``process.env``, ``require.main`` and the few
neighbours in ``RULES``. A flag is not a verdict: it tells the reviewer where to look, and it over-flags on purpose.

Pure functions: ``scan`` reads a unified diff (and, when given, each file's new text, so an alias bound on an
unchanged line is known); ``number`` assigns the ids (``<finding>-F001`` …).
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from zbm_delivery import textguard

LANGS = {".py": "python", ".pyi": "python", ".go": "go", ".rs": "rust", ".js": "node", ".mjs": "node", ".cjs": "node",
         ".ts": "node", ".mts": "node", ".cts": "node", ".jsx": "node", ".tsx": "node"}
SNIPPET_MAX = 160
RUNNER_DEPENDENT = "runner_dependent_reproduction"

# string concatenation used to form an identifier: two identifier-shaped literals joined by ``+``, a ``join`` over a
# literal tuple/list of such literals, a ``%`` / ``.format`` template made only of placeholders, an f-string whose
# expression is a string literal, ``chr(...)`` concatenated
_CONCAT = [
    re.compile(r"""[rRbBuUfF]{0,2}(['"`])[A-Za-z_][A-Za-z0-9_.]*\1\s*\+\s*[rRbBuUfF]{0,2}(['"`])[A-Za-z0-9_.]*\2"""),
    re.compile(r"""\.\s*join\s*\(\s*[\(\[]\s*[rRbBuUfF]{0,2}(['"])[A-Za-z0-9_.]*\1\s*,"""),
    re.compile(r"""(['"])(?:%s|\{\})+\1\s*(?:%|\.\s*format\s*\()"""),
    re.compile(r"""\bf(['"])[^'"]*\{\s*['"][A-Za-z0-9_.]*['"]\s*\}"""),
    re.compile(r"""\bchr\s*\([^)]*\)\s*\+"""),
]
CONCAT = "string concatenation forming an identifier"

_PY_MODULES = {"sys", "os", "builtins", "importlib", "inspect", "gc", "threading", "signal", "atexit", "traceback",
               "types", "__main__"}

# (construct, pattern, reason) per language; a line may carry several constructs (one flag each)
RULES: dict[str, list[tuple[str, re.Pattern, str]]] = {
    "python": [
        ("sys.modules", re.compile(r"\bsys\s*\.\s*modules\b|\bfrom\s+sys\s+import\b[^#\n]*\bmodules\b"),
         "reads the table of loaded modules (can see which test runner or test module is loaded)"),
        ("sys.argv", re.compile(r"\bsys\s*\.\s*(?:orig_)?argv\b|\bfrom\s+sys\s+import\b[^#\n]*\b(?:orig_)?argv\b"),
         "reads the process's command line (can see the runner's arguments)"),
        ("sys.flags", re.compile(r"\bsys\s*\.\s*flags\b|\bfrom\s+sys\s+import\b[^#\n]*\bflags\b"),
         "reads the interpreter's start-up flags (can tell an isolated/test interpreter from production)"),
        ("sys._getframe", re.compile(r"\b_getframe\b|\b_current_frames\b|\btb_frame\b|\bf_back\b"),
         "walks the call stack (can see who called it: a test, a runner)"),
        ("inspect", re.compile(r"\binspect\b"), "the inspect module can walk frames and read callers' source"),
        ("traceback", re.compile(r"\btraceback\b|__traceback__"), "reads a stack / traceback (can see a test's frames)"),
        ("os.environ/getenv", re.compile(r"\benvironb?\b|\bgetenvb?\b|\bputenv\b|\bunsetenv\b"),
         "reads or changes the process environment (CI / PYTEST_* / TEST* markers)"),
        ("__import__/importlib", re.compile(r"__import__|\bimportlib\b"),
         "imports by computed name (a module name the reader cannot see statically)"),
        ("globals()/vars()", re.compile(r"\bglobals\s*\(|\bvars\s*\(|\blocals\s*\(|__dict__"),
         "reads a namespace dictionary (a module's or an object's attributes by computed name)"),
        ("__main__", re.compile(r"__main__"), "refers to the __main__ module (can tell how the process was started)"),
        ("atexit", re.compile(r"\batexit\b"), "registers or inspects process-exit hooks"),
        ("signal", re.compile(r"\bsignal\b"), "installs or inspects signal handlers"),
        ("threading.enumerate", re.compile(r"\bthreading\s*\.\s*enumerate\b|\bfrom\s+threading\s+import\b[^#\n]*\benumerate\b"),
         "lists the process's threads (can see a runner's helper threads)"),
        ("gc.get_objects", re.compile(r"\bgc\s*\.\s*get_(?:objects|referrers|referents)\b|\bfrom\s+gc\s+import\b"),
         "walks every live object (can find the test runner's objects)"),
        ("builtins", re.compile(r"\bbuiltins\b|__builtins__"), "reads or patches the builtins namespace"),
    ],
    "go": [
        ("os.Args", re.compile(r"\bos\s*\.\s*Args\b|\bflag\s*\.\s*(?:Lookup|Args|CommandLine|Parsed)\b"),
         "reads the process's command line / flags (can see `-test.*` flags)"),
        ("os.Getenv", re.compile(r"\bos\s*\.\s*(?:Getenv|LookupEnv|Environ|Setenv|Unsetenv)\b|\bsyscall\s*\.\s*Getenv\b"),
         "reads or changes the process environment (CI markers)"),
        ("testing.Testing()", re.compile(r"\btesting\s*\.\s*Testing\s*\(|\"testing\""),
         "asks the testing package whether a test binary is running"),
        ("runtime.Caller", re.compile(r"\bruntime\s*\.\s*(?:Caller|Callers|Stack|FuncForPC)\b|\bdebug\s*\.\s*Stack\b"),
         "walks the call stack (can see a test's frames)"),
        ("signal", re.compile(r"\bsignal\s*\.\s*Notify\b|\"os/signal\""), "installs signal handlers"),
    ],
    "rust": [
        ("cfg!(test)", re.compile(r"cfg!\s*\(\s*test\b|#\s*!?\s*\[\s*cfg(?:_attr)?\s*\(\s*(?:[^)]*\b)?test\b"),
         "compiles differently under `cargo test` (inline test modules are flagged too: the engine does not parse Rust)"),
        ("std::env", re.compile(r"\bstd\s*::\s*env\b|\benv\s*::\s*(?:var|var_os|vars|vars_os|args|args_os)\b|\b(?:option_)?env!\s*\("),
         "reads the process environment or command line"),
        ("backtrace", re.compile(r"\b[Bb]acktrace\b"), "captures a stack trace (can see a test's frames)"),
    ],
    "node": [
        ("process.argv", re.compile(r"\bprocess\s*\.\s*(?:argv0?|execArgv)\b"), "reads the process's command line"),
        ("process.env", re.compile(r"\bprocess\s*\.\s*env\b"), "reads or changes the process environment (NODE_TEST_CONTEXT, CI)"),
        ("require.main", re.compile(r"\brequire\s*\.\s*main\b|\bmodule\s*\.\s*parent\b|\bimport\s*\.\s*meta\s*\.\s*main\b"),
         "asks which module started the process"),
        ("stack introspection", re.compile(r"\bcaptureStackTrace\b|\bprepareStackTrace\b|new\s+Error\s*\([^)]*\)\s*\.\s*stack\b"),
         "reads a stack trace (can see a test's frames)"),
        ("process exit/signal hooks", re.compile(r"\bprocess\s*\.\s*(?:on|once|prependListener)\s*\(\s*['\"](?:exit|beforeExit|SIG[A-Z]+)"),
         "installs process exit or signal hooks"),
        ("globalThis", re.compile(r"\bglobalThis\b|\bglobal\s*\."), "reads the global object (globals() equivalent)"),
        ("dynamic require/import", re.compile(r"\brequire\s*\(\s*[^'\"`\s)]|\bimport\s*\(\s*[^'\"`\s)]"),
         "imports by computed name"),
    ],
}

_GETATTR = re.compile(r"\b(?:get|has|set|del)attr\s*\(\s*(__import__\s*\(|[A-Za-z_][A-Za-z0-9_.]*)")
_PY_IMPORT = re.compile(r"^\s*import\s+(.+)$|^\s*from\s+[A-Za-z0-9_.]+\s+import\s+(.+)$")

# wave 24 (E1): aliases of the listed modules, star imports, eval/exec/compile of a non-literal, /proc and
# test-context literals, test-framework imports — cheap, and sound for what they spell (nothing more is claimed)
_ALIAS_IMPORT = re.compile(r"^\s*import\s+(.+)$")
_FROM_IMPORT = re.compile(r"^\s*from\s+([A-Za-z0-9_.]+)\s+import\s+\(?([^#)]*)\)?\s*$")
STAR = "star import of a listed module"
EVAL = "eval/exec/compile of a non-literal"
PROC = "/proc path literal"
TESTLIT = "test-context string literal"
FRAMEWORK = "test framework import"
# a call of the builtin (not a method: `re.compile(p)` is not it) whose first argument is not a string literal
_EVAL_CALL = re.compile(r"""(?<![\w.])(?:eval|exec|compile)\s*\(\s*(?![rRbBuUfF]{0,2}['"])""")
_STR_LIT = re.compile(r"""[rRbBuUfF]{0,2}('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`[^`\n]*`)""")
_TEST_WORD = re.compile(r"(?i)conftest|pytest|(?<![a-z])test")
_PROC_WORD = re.compile(r"/proc\b")
_FRAMEWORK_IMPORT = re.compile(r"^\s*(?:import\s+[^#\n]*?(?<![\w.])(?:pytest|_pytest|unittest)\b|"
                               r"from\s+(?:pytest|_pytest|unittest)\b)")


def _py_aliases(lines: list[str]) -> dict[str, str]:
    """{local name: listed module, or module.attr} bound by ``import m as a`` or ``from m import x [as y]`` where m
    is one of the listed modules (wave 24, E1)."""
    out: dict[str, str] = {}
    for ln in lines:
        code = ln.split("#", 1)[0]
        m = _FROM_IMPORT.match(code)
        if m:
            if m.group(1).split(".")[0] in _PY_MODULES:
                for part in m.group(2).split(","):
                    name, _, alias = part.strip().partition(" as ")
                    name = name.strip()
                    if name and name != "*":
                        out[(alias or name).strip()] = f"{m.group(1)}.{name}"
            continue
        m = _ALIAS_IMPORT.match(code)
        if m:
            for part in m.group(1).split(","):
                name, _, alias = part.strip().partition(" as ")
                name = name.strip()
                if alias and name.split(".")[0] in _PY_MODULES:
                    out[alias.strip()] = name
    return out


def _dealias(text: str, aliases: dict[str, str]) -> str:
    """``text`` with every alias of a listed module written as what it names (``_s.modules`` → ``sys.modules``)."""
    for alias, target in aliases.items():
        if alias != target:
            text = re.sub(rf"(?<![\w.]){re.escape(alias)}(?!\w)", target, text)
    return text


def _context_lines(diff_text: str) -> dict[str, list[str]]:
    """{repo path: the context and added lines a diff shows of the new file} (used when no full text is given)."""
    out: dict[str, list[str]] = {}
    cur: Optional[str] = None
    in_hunk = False
    for ln in diff_text.splitlines():
        if ln.startswith("diff --git "):
            cur, in_hunk = None, False
            continue
        if not in_hunk and ln.startswith("+++ "):
            p = ln[4:].strip()
            cur = (p[2:] if p.startswith("b/") else p) if p != "/dev/null" else None
            continue
        if ln.startswith("@@ "):
            in_hunk = True
            continue
        if in_hunk and cur is not None and (ln.startswith("+") or ln.startswith(" ")):
            out.setdefault(cur, []).append(ln[1:])
    return out


def _py_module_names(lines: list[str]) -> set[str]:
    """Names the added lines of one Python file bind by ``import`` (``import a.b as c`` → ``c``; ``import a.b`` →
    ``a``; ``from x import y as z`` → ``z``: y may be a module) — the receivers ``getattr`` is flagged on."""
    out = set(_PY_MODULES)
    for ln in lines:
        m = _PY_IMPORT.match(ln.split("#", 1)[0])
        if not m:
            continue
        for part in (m.group(1) or m.group(2) or "").strip("() ").split(","):
            part = part.strip()
            if not part:
                continue
            name, _, alias = part.partition(" as ")
            out.add(alias.strip() if alias else name.strip().split(".")[0])
    return out


def added_lines(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """{repo path: [(new-file line number, text)]} for every ``+`` line of a unified diff (context lines advance the
    counter, ``-`` lines do not)."""
    out: dict[str, list[tuple[int, str]]] = {}
    cur: Optional[str] = None
    n = 0
    in_hunk = False
    hunk = re.compile(r"^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)? @@")
    for ln in diff_text.splitlines():
        if ln.startswith("diff --git "):
            cur, in_hunk = None, False
            continue
        if not in_hunk and ln.startswith("--- "):
            continue
        if ln.startswith("+++ ") and not in_hunk:
            p = ln[4:].strip()
            cur = (p[2:] if p.startswith("b/") else p) if p != "/dev/null" else None
            if cur is not None:
                out.setdefault(cur, [])
            continue
        m = hunk.match(ln)
        if m:
            n, in_hunk = int(m.group(1)), True
            continue
        if not in_hunk or cur is None:
            continue
        if ln.startswith("+"):
            out[cur].append((n, ln[1:]))
            n += 1
        elif ln.startswith(" ") or ln == "":
            n += 1
        elif ln.startswith("\\"):
            continue
        elif ln.startswith("-"):
            continue
        else:
            in_hunk = False
    return out


def _lang(path: str) -> Optional[str]:
    dot = path.rfind(".")
    return LANGS.get(path[dot:].lower()) if dot >= 0 else None


def _snippet(text: str) -> str:
    s = textguard.redact(text.strip())
    return s if len(s) <= SNIPPET_MAX else s[:SNIPPET_MAX - 1] + "…"


def scan(diff_text: str, service: str, *, is_test: Callable[[str], bool],
         file_text: Optional[Callable[[str], Optional[str]]] = None) -> list[dict]:
    """Every flag of the lines ``diff_text`` adds to a source file of ``services/<service>/`` (a file ``is_test``
    says is a test — or test infrastructure — is never scanned). ``file_text(path)``, when given, is the file's new
    text, so an alias bound on an unchanged line is known too (wave 24); without it, the lines the diff shows.
    Unnumbered; ``number`` gives the ids. A spelling list: an empty result says nothing about the diff."""
    prefix = f"services/{service}/"
    flags: list[dict] = []
    shown = _context_lines(diff_text)
    for path, lines in sorted(added_lines(diff_text).items()):
        lang = _lang(path)
        if lang is None or not path.startswith(prefix) or is_test(path):
            continue
        modules = _py_module_names([t for _, t in lines]) if lang == "python" else set()
        aliases: dict[str, str] = {}
        if lang == "python":
            whole = file_text(path) if file_text is not None else None
            aliases = _py_aliases(whole.splitlines() if whole is not None else shown.get(path, [t for _, t in lines]))
            modules |= set(aliases)
        for no, text in lines:
            hits: list[tuple[str, str]] = []
            plain = _dealias(text, aliases) if aliases else text
            for construct, rx, reason in RULES[lang]:
                if rx.search(text) or rx.search(plain):
                    hits.append((construct, reason))
            if lang == "python":
                code = text.split("#", 1)[0]
                m = _FROM_IMPORT.match(code)
                if m and m.group(1).split(".")[0] in _PY_MODULES and m.group(2).strip() == "*":
                    hits.append((STAR, "imports every name of a listed module unqualified (its uses cannot be read "
                                       "as that module's)"))
                if _EVAL_CALL.search(code):
                    hits.append((EVAL, "runs code built at run time (the engine does not decode what it runs)"))
                if _FRAMEWORK_IMPORT.search(code):
                    hits.append((FRAMEWORK, "a source file imports a test framework (production never needs it)"))
            for lit in _STR_LIT.findall(text):
                if _PROC_WORD.search(lit):
                    hits.append((PROC, "names a /proc path (the process table, its own or another process's state)"))
                if _TEST_WORD.search(lit):
                    hits.append((TESTLIT, "a string naming a test, a test runner or conftest (a name it may look for)"))
            if lang == "python":
                for m in _GETATTR.finditer(text):
                    recv = m.group(1)
                    if recv.startswith("__import__") or recv.split(".")[0] in modules:
                        hits.append(("getattr on a module", "reads a module attribute by computed name"))
                        break
            if any(rx.search(text) for rx in _CONCAT):
                hits.append((CONCAT, "builds a name from string pieces (a name the reader cannot see statically)"))
            seen = set()
            for construct, reason in hits:
                if construct in seen:
                    continue
                seen.add(construct)
                flags.append({"file": path, "line": no, "construct": construct, "reason": reason, "language": lang,
                              "snippet": _snippet(text)})
    return flags


def number(flags: list[dict], finding_id: str) -> list[dict]:
    """The flags of one finding with their ids (``<finding>-F001`` …, in file/line/construct order)."""
    out = []
    for i, fl in enumerate(sorted(flags, key=lambda f: (f["file"], f["line"], f["construct"])), start=1):
        out.append({"id": f"{finding_id}-F{i:03d}", "finding_id": finding_id, **fl})
    return out


def def_line(text: Optional[str], name: str) -> int:
    """The line of ``def <name>`` (the last ``::`` part, without a ``[param]``) in a test file's text; 1 when absent."""
    leaf = name.split("::")[-1].split("[", 1)[0]
    for i, ln in enumerate((text or "").splitlines(), start=1):
        if re.match(rf"\s*(?:async\s+)?def\s+{re.escape(leaf)}\s*\(", ln):
            return i
    return 1


def runner_dependent_flag(finding_id: str, target: str, file: str, line: int, why: str) -> dict:
    """D3: a finding whose reproduction TEST cannot run outside the test runner is a flag, not a parking lot."""
    return {"id": f"{finding_id}-RD", "finding_id": finding_id, "file": file, "line": max(1, int(line or 1)),
            "construct": RUNNER_DEPENDENT, "language": "python",
            "reason": ("the reproduction " + target + " could not be executed outside the test runner (" + why[:200]
                       + "): the engine could not rule out a fix that only works under test"),
            "snippet": ""}
