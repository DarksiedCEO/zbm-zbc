"""The heavy lane's access to the reference releases (SUITE_PLAN 0: re-verified from git history, never vendored):
``export(sha)`` writes ``git archive <sha> services/seo-py`` into a temporary directory (None when the history is
absent: a shallow CI checkout), ``load_guard(tree)`` imports that release's ``agents.secret_guard`` under another
module name, its own ``lookalikes`` beside it, in THIS process (PF-1 times both modules alternately)."""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
RELEASES = {"ffd28ca8": "ffd28ca8", "cbdf5e78": "cbdf5e78", "2b26d980": "2b26d980", "186471eb": "186471eb"}
_CACHE: dict = {}


def export(sha: str) -> Path | None:
    if sha in _CACHE:
        return _CACHE[sha]
    d = Path(tempfile.mkdtemp(prefix=f"conformance-{sha}-"))
    try:
        a = subprocess.run(["git", "-C", str(REPO), "archive", sha, "services/seo-py"], capture_output=True, timeout=120)
        if a.returncode != 0:
            return None
        subprocess.run(["tar", "-x", "-C", str(d)], input=a.stdout, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    _CACHE[sha] = d / "services" / "seo-py"
    return _CACHE[sha]


def load_guard(tree: Path, name: str):
    """That release's secret_guard as module ``name`` (its ``lookalikes`` loaded for it, then the build's restored)."""
    src = str(tree / "src")
    saved = sys.modules.pop("lookalikes", None)
    sys.path.insert(0, src)
    try:
        spec = importlib.util.spec_from_file_location(name, os.path.join(src, "agents", "secret_guard.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(src)
        sys.modules.pop("lookalikes", None)
        if saved is not None:
            sys.modules["lookalikes"] = saved
    return mod
