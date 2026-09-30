"""
The engine-owned standalone runner (fix wave 22, lead ruling G1(b); AEGIS N21-D-1). Hash pinned by ``runner.py``,
read-only bind-mounted into every container at ``/mnt/dlv`` (the same mount as ``resolve.py``), never a file of the
tree. It runs ONE pytest-style test function OUTSIDE pytest, so a source change that detects the test runner (a
``"pytest" in sys.modules`` check, a ``PYTEST_CURRENT_TEST`` read, a look at the caller's frames) and "fixes" the
defect only under the runner is seen for what it is: the finding's reproduction must also pass here with the fix and
fail here on the reverted checkout, or the finding never reaches ``candidate_passed_checks``.

Invocation (the engine's argv; nothing here comes from the agent)::

    python3 -I /mnt/dlv/zbm_standalone_runner.py           (stdin: one JSON request)

The request — ``nonce``, ``report`` (an engine path), ``service_dir``, ``paths`` (the seed's ``pythonpath`` entries,
absolute), ``test_file`` (absolute) and ``test`` (``name`` or ``Class::name``) — arrives on stdin, is read once and
is never in argv or the environment (``/proc/self/cmdline`` and ``/proc/self/environ`` do not show it).

Stage 1 (this file, first process): read the request, then re-exec the interpreter (``python -I``) with a SCRUBBED
environment — ``CI``, ``CONTINUOUS_INTEGRATION`` and every name starting ``PYTEST``/``TEST``/``_PYTEST`` (any case)
are gone from the new process's initial environment, so ``/proc/self/environ`` does not carry them either; the
request is handed over on an inherited pipe.

Stage 2 (``--stage2 <fd>``): before anything of the tree is importable,
  1. an import hook refuses ``pytest``, ``_pytest`` and ``pytest_*`` (a ``ModuleNotFoundError`` subclass, so code
     that falls back when pytest is absent sees exactly what production sees); any such module already imported by a
     ``.pth`` at interpreter start is dropped from ``sys.modules``;
  2. ``sys.path`` = the test's import root, the seed's ``pythonpath`` entries, then the interpreter's own entries
     (``-I``: no cwd, no script dir, no ``PYTHONPATH``, no user site) — the order pytest's prepend import mode and
     ``pythonpath`` ini give;
  3. the test module is imported under the name pytest would give it and the function is called. A test that needs
     pytest (it imports pytest, takes fixtures, is parametrized, is async or a generator, relies on xunit
     ``setup_*``/``teardown_*`` or is skipped) cannot run here: the verdict is ``runner_dependent`` — never ``pass``.

Wave 23 (D3): ``runner_dependent`` is returned only for a TEST-side cause. The import hook records, for every refused
import, the frame that asked for it (``blocked_from``: the module name, the file relative to the service, and its
side — ``test`` for the test file, a ``conftest.py`` or a file under a ``tests``/``test`` directory or named
``test_*.py``/``*_test.py``; ``src`` for any other file of the service; ``lib`` outside it). A refused import that
propagates from a ``src`` or ``lib`` frame is the code under test failing outside the runner: verdict ``fail``,
never ``runner_dependent``; the engine fails the round (``fix_imports_test_runner``) on any ``src`` entry of the
verification checkout, caught or not.

The report (``O_CREAT|O_EXCL|O_NOFOLLOW``, 0600, written only after the function returned or raised) carries the
nonce; the exit code must agree with it (0 pass, 1 fail, 3 runner_dependent); anything else is ``unknown`` for the
engine. A source module that ends the process (``os._exit(0)``) leaves no report: ``unknown``, never ``pass``.

Residual (stated, ADR 0011): this is a same-process, same-uid run. Code under test can still tell "not
production" from other signals (``sys.flags.isolated``, the test module present in ``sys.modules``, walking the
frames to this runner) — ``src_content_deny`` refuses the cheap spellings of those in a changed source file; a
determined forgery that walks the frames to the nonce is the same residual as the pytest plugin's.
"""

import os
import sys

_EXIT = {"pass": 0, "fail": 1, "runner_dependent": 3, "unknown": 4}
_SCRUB_EXACT = ("CI", "CONTINUOUS_INTEGRATION", "BUILD_NUMBER", "RUN_ID", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE",
                "JENKINS_URL", "TEAMCITY_VERSION", "TF_BUILD")
_SCRUB_PREFIX = ("PYTEST", "TEST", "_PYTEST", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE")
_XUNIT = ("setup_module", "teardown_module", "setUpModule", "tearDownModule", "setup_function", "teardown_function",
          "setup_method", "teardown_method", "setup_class", "teardown_class", "setup", "teardown")


def _scrubbed_env(env):
    out = {}
    for k, v in env.items():
        u = k.upper()
        if u in _SCRUB_EXACT or u.startswith(_SCRUB_PREFIX):
            continue
        out[k] = v
    return out


def _stage1():
    raw = sys.stdin.buffer.read(1 << 20)
    rfd, wfd = os.pipe()
    if len(raw) > 60000:                        # a pipe buffer holds at least 64 KiB; the request is far smaller
        os._exit(_EXIT["unknown"])
    os.write(wfd, raw)
    os.close(wfd)
    os.set_inheritable(rfd, True)
    os.execve(sys.executable, [sys.executable, "-I", os.path.abspath(__file__), "--stage2", str(rfd)],
              _scrubbed_env(os.environ))


class RunnerDependent(ModuleNotFoundError):
    """An import of pytest (or one of its packages) outside pytest; ``side`` is where the import came from."""

    side = "lib"


def _stage2(fd):
    import importlib
    import importlib.abc
    import inspect
    import json
    import unittest

    chunks = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            break
        chunks.append(b)
    os.close(fd)
    req = json.loads(b"".join(chunks).decode("utf-8"))
    nonce, report = req["nonce"], req["report"]
    state = {"blocked": [], "dropped": [], "from": [], "service_dir": None, "test_file": None}
    here = os.path.realpath(__file__)
    importlib_dir = os.path.dirname(os.path.realpath(importlib.__file__))

    def blocked_name(name):
        root = name.partition(".")[0]
        return root in ("pytest", "_pytest") or root.startswith(("pytest_", "_pytest"))

    def importer():
        """The file of the first frame above the import machinery: the code that asked for the module."""
        f = sys._getframe(2)
        while f is not None:
            fn = f.f_code.co_filename
            if fn.startswith("<") and not fn.startswith(("<frozen ", "<builtin")):
                return fn                     # exec'd / compiled text: nobody can say whose it is (side_of: src)
            if not fn.startswith("<"):
                real = os.path.realpath(fn)
                if real != here and os.path.dirname(real) != importlib_dir:
                    return real
            f = f.f_back
        return ""

    def side_of(real):
        sd, tf = state["service_dir"], state["test_file"]
        if real.startswith("<"):
            return "src", real[:80]           # fail closed: an import from exec'd text never counts as the test's
        if not real or not sd or not (real == sd or real.startswith(sd + os.sep)):
            return "lib", os.path.basename(real)
        rel = os.path.relpath(real, sd)
        parts = rel.split(os.sep)
        base = parts[-1]
        if (real == tf or base == "conftest.py" or any(p in ("tests", "test") for p in parts[:-1])
                or base.startswith("test_") or base.endswith("_test.py")):
            return "test", rel
        return "src", rel

    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if blocked_name(name):
                side, where = side_of(importer())
                state["blocked"].append(name)
                state["from"].append({"name": name[:80], "side": side, "file": where[:200]})
                exc = RunnerDependent(f"No module named {name!r} (the engine's standalone run: pytest is not importable)",
                                      name=name)
                exc.side = side
                raise exc
            return None

    sys.meta_path.insert(0, _Block())
    for k in list(sys.modules):
        if blocked_name(k):
            state["dropped"].append(k)
            del sys.modules[k]
    sys.dont_write_bytecode = True

    def finish(verdict, why, **extra):
        payload = {"nonce": nonce, "verdict": verdict, "why": str(why)[:400], "blocked_imports": state["blocked"][:20],
                   "blocked_from": state["from"][:20], "dropped_at_start": state["dropped"][:20], **extra}
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        try:
            out = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.write(out, data)
            finally:
                os.close(out)
        except OSError:
            os._exit(_EXIT["unknown"])
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(_EXIT[verdict])

    def blocking(exc):
        """The RunnerDependent that ``exc`` came from, or None."""
        seen = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            if isinstance(exc, RunnerDependent):
                return exc
            exc = exc.__cause__ or exc.__context__
        return None

    def classify(exc, what):
        """D3: a refused pytest import is ``runner_dependent`` only when the TEST side asked for it; from the code
        under test (``src``) or a library (``lib``) it is that code failing outside the runner: ``fail``."""
        blk = blocking(exc)
        if blk is None:
            return "fail", f"{what} raised {type(exc).__name__}"
        if blk.side == "test":
            return "runner_dependent", f"{what}: the test needs pytest ({type(exc).__name__})"
        return "fail", f"{what}: the {blk.side} code imports the test runner ({blk.name!r})"

    service_dir = os.path.realpath(req["service_dir"])
    test_file = os.path.realpath(req["test_file"])
    state["service_dir"], state["test_file"] = service_dir, test_file
    test = req["test"]
    if not (test_file == service_dir or test_file.startswith(service_dir + os.sep)) or not os.path.isfile(test_file):
        finish("unknown", "the test file is not a file of the service directory")
    conftest = []
    d = os.path.dirname(test_file)
    while True:
        if os.path.isfile(os.path.join(d, "conftest.py")):
            conftest.append(os.path.relpath(os.path.join(d, "conftest.py"), service_dir))
        if d == service_dir or not d.startswith(service_dir):
            break
        d = os.path.dirname(d)
    info = {"conftest": conftest}
    if "[" in test:
        finish("runner_dependent", "a parametrized test id needs pytest's parametrization", **info)
    # pytest's prepend import mode: the first directory upwards without __init__.py is the import root
    base = os.path.dirname(test_file)
    parts = [os.path.splitext(os.path.basename(test_file))[0]]
    while os.path.isfile(os.path.join(base, "__init__.py")):
        parts.insert(0, os.path.basename(base))
        base = os.path.dirname(base)
    modname = ".".join(parts)
    paths = [os.path.realpath(p) for p in req.get("paths") or []]
    sys.path[:] = [base] + [p for p in paths if p != base] + [p for p in sys.path if p and p not in paths and p != base]
    os.chdir(service_dir)
    try:
        mod = importlib.import_module(modname)
    except BaseException as exc:  # noqa: BLE001 - the verdict is the classification of whatever happened
        finish(*classify(exc, "importing the test module"), **info)
    names = test.split("::")
    owner, obj = None, mod
    try:
        for n in names:
            owner, obj = obj, getattr(obj, n)
    except AttributeError:
        finish("unknown", f"{test} is not defined by the test module", **info)
    if any(callable(getattr(mod, x, None)) for x in _XUNIT):
        finish("runner_dependent", "the module defines xunit setup/teardown functions pytest would call", **info)
    fn = obj
    if isinstance(owner, type):
        if issubclass(owner, unittest.TestCase):
            result = unittest.TestResult()
            try:
                owner(names[-1]).run(result)
            except BaseException as exc:  # noqa: BLE001
                finish(*classify(exc, "the unittest case"), **info)
            if result.skipped:
                finish("runner_dependent", "the unittest case was skipped", **info)
            problems = result.errors + result.failures
            if any("RunnerDependent" in tb for _, tb in problems):
                if any(b["side"] != "test" for b in state["from"]):
                    finish("fail", "the unittest case: the code under test imports the test runner", **info)
                finish("runner_dependent", "the unittest case needs pytest", **info)
            finish("fail" if problems or result.unexpectedSuccesses else "pass",
                   f"unittest: {len(result.errors)} error(s), {len(result.failures)} failure(s)", **info)
        if any(callable(getattr(owner, x, None)) for x in _XUNIT) or "__init__" in vars(owner):
            finish("runner_dependent", "the test class has xunit setup/teardown or an __init__ pytest would not call", **info)
        try:
            fn = getattr(owner(), names[-1])
        except BaseException as exc:  # noqa: BLE001
            finish(*classify(exc, "instantiating the test class"), **info)
    elif owner is not mod:
        finish("unknown", f"{test} is not a module-level function or a class's method", **info)
    if not callable(fn):
        finish("unknown", f"{test} is not callable", **info)
    if inspect.iscoroutinefunction(fn) or inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
        finish("runner_dependent", "an async or generator test needs a pytest plugin", **info)
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        finish("unknown", "the test's signature cannot be read", **info)
    required = [p.name for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    if required:
        finish("runner_dependent", "the test takes fixtures: " + ", ".join(required[:10]), **info)
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 - SystemExit / KeyboardInterrupt from the code under test fail the test
        if isinstance(exc, unittest.SkipTest):
            finish("runner_dependent", "the test skipped itself", **info)
        verdict, why = classify(exc, "the test")
        finish(verdict, why if verdict == "runner_dependent" or blocking(exc) else f"the test raised {type(exc).__name__}", **info)
    finish("pass", "the test function returned", **info)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--stage2":
        try:
            _stage2(int(sys.argv[2]))
        finally:
            os._exit(_EXIT["unknown"])       # reached only when the runner itself failed: no report, unknown
    _stage1()
