"""
Review flags (fix wave 23, founder design change D2 — "flag, don't chase", Sep 30 2026).

Three review rounds (20-22) showed that the engine cannot win an arms race against source code that detects it is under
test: every detector the engine learned to defeat (pytest in ``sys.modules``, then the environment, then ``test_``
modules, ``argv``, ``sys.flags``) was replaced by the next one. Detection stays as a cheap layer (``src_content_deny``,
the standalone re-run), but the engine's claim no longer rests on it. Instead, every line a fix ADDS to a SOURCE file
(never a test file) that contains a construct able to observe the execution context becomes a review flag — file,
line, the construct, the reason — shown at the top of the report above every "passed" line, recorded on the ledger
(``review_flags_recorded``), and a review that accepts the finding must name each flag id in ``flags_addressed``.

The list is the founder's (D2), no wider: Python ``sys.modules``, ``sys.argv``, ``sys.flags``, ``sys._getframe``,
``inspect``, ``traceback``, ``os.environ``/``getenv``, ``__import__``/``importlib``, ``globals()``/``vars()``/
``getattr`` on modules, ``__main__``, ``atexit``, ``signal``, ``threading.enumerate``, ``gc.get_objects``,
``builtins``, string concatenation used to form identifiers; and the Go / Rust / Node equivalents (``os.Args``,
``os.Getenv``, ``testing.Testing()``, ``cfg!(test)``, ``std::env``, ``process.argv``, ``process.env``,
``require.main``). A flag is not a verdict: it tells the reviewer where to look. It over-flags on purpose (a
``signal`` or ``__main__`` in honest code is flagged too); it cannot under-flag a construct that is spelled out, and a
construct reached WITHOUT any of these spellings (a module object passed in from elsewhere, a C extension) is not
flagged — stated in ADR 0011.

Pure functions: ``scan`` reads a unified diff; ``number`` assigns the ids (``<finding>-F001`` …).
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


def scan(diff_text: str, service: str, *, is_test: Callable[[str], bool]) -> list[dict]:
    """Every flag of the lines ``diff_text`` adds to a source file of ``services/<service>/`` (a file ``is_test``
    says is a test — or test infrastructure — is never scanned). Unnumbered; ``number`` gives the ids."""
    prefix = f"services/{service}/"
    flags: list[dict] = []
    for path, lines in sorted(added_lines(diff_text).items()):
        lang = _lang(path)
        if lang is None or not path.startswith(prefix) or is_test(path):
            continue
        modules = _py_module_names([t for _, t in lines]) if lang == "python" else set()
        for no, text in lines:
            hits: list[tuple[str, str]] = []
            for construct, rx, reason in RULES[lang]:
                if rx.search(text):
                    hits.append((construct, reason))
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


def runner_dependent_flag(finding_id: str, target: str, file: str, line: int, why: str) -> dict:
    """D3: a finding whose reproduction TEST cannot run outside the test runner is a flag, not a parking lot."""
    return {"id": f"{finding_id}-RD", "finding_id": finding_id, "file": file, "line": max(1, int(line or 1)),
            "construct": RUNNER_DEPENDENT, "language": "python",
            "reason": ("the reproduction " + target + " could not be executed outside the test runner (" + why[:200]
                       + "): the engine could not rule out a fix that only works under test"),
            "snippet": ""}
