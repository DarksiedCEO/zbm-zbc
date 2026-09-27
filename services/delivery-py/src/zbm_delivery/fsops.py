"""
The ONE place in ``src/`` that deletes anything on the host (spec §B.4, test G6): every removal goes through
``delete_tree``/``delete_file``, which refuse the evidence root, any ``.git`` directory, the data dir's log and any
path outside the roots the caller declares. The static guardrail scan asserts ``shutil.rmtree``/``os.remove``/
``os.unlink`` appear nowhere else under ``src/``.
"""

from __future__ import annotations

import os
import shutil

_protected: list[str] = []


class ProtectedPath(PermissionError):
    pass


def protect(path: str) -> None:
    """Register a root that may never be deleted or deleted under (the evidence root, the data dir)."""
    p = os.path.realpath(path)
    if p not in _protected:
        _protected.append(p)


def protected_roots() -> list[str]:
    return list(_protected)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _check(path: str, within: str) -> str:
    real = os.path.realpath(path)
    for root in _protected:
        if _inside(real, root) or _inside(root, real):
            raise ProtectedPath(f"refusing to delete under a protected root: {os.path.basename(root)}")
    if real == "/" or os.path.basename(real) == ".git" or f"{os.sep}.git{os.sep}" in real + os.sep:
        raise ProtectedPath("refusing to delete a .git directory or /")
    w = os.path.realpath(within)
    if not _inside(real, w) or real == w:
        raise ProtectedPath("refusing to delete outside the declared root")
    return real


def delete_tree(path: str, *, within: str) -> None:
    real = _check(path, within)
    if os.path.isdir(real) and not os.path.islink(real):
        shutil.rmtree(real)
    elif os.path.lexists(real):
        os.remove(real)


def delete_file(path: str, *, within: str) -> None:
    real = _check(path, within)
    if os.path.lexists(real) and not os.path.isdir(real):
        os.remove(real)
