"""core-spec/1 IM-1 / IM-2: the module imports, and its import-time list checks hold, on every interpreter and in every
environment of the spec. CI: the running interpreter, the f-string backslash scan, every installed interpreter of
3.11 / 3.12 / 3.13 compiling the module, 12 environment combinations; heavy: 288 combinations per interpreter."""
import ast
import itertools
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.conformance
SRC = Path(__file__).resolve().parents[2] / "src"
MODULE = SRC / "agents" / "secret_guard.py"
INTERPRETERS = [p for p in (shutil.which(f"python3.{v}") for v in (11, 12, 13)) if p]


def test_core_IM_1_api_imports_and_the_service_constructs(tmp_path):
    import api  # noqa: F401
    from helpers import Harness
    assert Harness(tmp_path).svc is not None


def test_core_IM_1_no_backslash_inside_an_f_string_expression():
    """IM-1 (rv-sink7 F4): an f-string expression holding a backslash is a SyntaxError on 3.11."""
    src = MODULE.read_text(encoding="ascii")
    bad = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.FormattedValue) and "\\" in (ast.get_source_segment(src, v.value) or ""):
                    bad.append(v.lineno)
    assert not bad, bad


@pytest.mark.parametrize("py", INTERPRETERS or [sys.executable])
def test_core_IM_1_every_interpreter_compiles_the_module(py, tmp_path):
    p = subprocess.run([py, "-c", "import sys, py_compile; py_compile.compile(sys.argv[1], cfile=sys.argv[2], "
                        "doraise=True)", str(MODULE), str(tmp_path / "sg.pyc")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-500:]


def test_core_IM_2_the_import_checks_read_module_constants_only():
    """IM-2: the import-time checks read no environment, configuration or data file."""
    src = MODULE.read_text(encoding="ascii")
    assert "os.environ" not in src and "getenv" not in src and "open(" not in src


ENVS = {"PYTHONHASHSEED": ("0", "1", "2", "3", "7", "42"), "LC_ALL": ("C", "C.UTF-8", "tr_TR.UTF-8", "POSIX")}
FLAGS = ((), ("-O",), ("-X", "utf8=0"), ("-E",))
# the heavy lane: every subset of -O / -X utf8=0 / -E, and -OO with the subsets of the other two: 12 x 6 x 4 = 288
HEAVY_FLAGS = [tuple(x for f in fs for x in f) for k in range(4) for fs in itertools.combinations(FLAGS[1:], k)]
HEAVY_FLAGS += [("-OO",) + tuple(x for f in fs for x in f) for k in range(3)
                for fs in itertools.combinations(FLAGS[2:], k)]


def _run(py, seed, lc, flags) -> int:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "LC_"))}
    env.update(PYTHONHASHSEED=seed, LC_ALL=lc, PYTHONDONTWRITEBYTECODE="1")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from agents import secret_guard as s; "
            "s._check_label_lists(); assert s.check('the password: Kx7#mQ2zB') == 'a_label'")
    return subprocess.run([py, *flags, "-c", code, str(SRC)], env=env, capture_output=True, text=True).returncode


def test_core_IM_2_twelve_environment_combinations():
    combos = list(itertools.product(("0", "42"), ("C", "tr_TR.UTF-8"), FLAGS[1:]))
    bad = [c for c in combos if _run(sys.executable, *c) != 0]
    print(f"IM-2: combinations={len(combos)} failures={len(bad)}")
    assert len(combos) == 12 and not bad, bad


@pytest.mark.conformance_heavy
@pytest.mark.parametrize("py", INTERPRETERS or [sys.executable])
def test_core_IM_2_every_combination_on_every_interpreter(py):
    combos = list(itertools.product(ENVS["PYTHONHASHSEED"], ENVS["LC_ALL"], HEAVY_FLAGS))
    assert len(combos) == 288
    bad = [c for c in combos if _run(py, *c) != 0]
    print(f"IM-2 heavy {py}: combinations={len(combos)} failures={len(bad)}")
    assert not bad, bad[:5]
