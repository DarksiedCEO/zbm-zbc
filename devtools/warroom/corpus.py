"""Seed corpus extraction (ADR 0018): the war room's seed phrases come out of the services' own pinned tests and
source by AST, never retyped. A scenario library names WHERE a corpus lives (file, test function, and an assignment
name, the n-th ``for`` loop over a literal, or a ``pytest.mark.parametrize`` table); this module reads it with
``ast.literal_eval``. A reference that no longer resolves, or resolves to nothing, is an error (the run reports the
scenario as ERROR), so a moved test can never silently empty a scenario. Standard library only."""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFUSABLES_SOURCE = ("services/creative-py/src/shared/text.py", "CONFUSABLES")
# A second, independent and deliberately small lookalike table (sales-py's reply classifier, AEGIS S2-C1). A
# lookalike BOTH tables list is a core homoglyph (Cyrillic / Greek letters that render like Latin ones); the rest of
# creative-py's table is the wide set (Cherokee, insular, small capitals...).
CORE_CONFUSABLES_SOURCE = ("services/sales-py/src/intelligences/i10_replies.py", "_CONFUSABLE")


class CorpusError(Exception):
    pass


_TREES: dict[Path, ast.Module] = {}


def _tree(path: Path) -> ast.Module:
    if path not in _TREES:
        _TREES[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _TREES[path]


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise CorpusError(f"function {name} not found")


def _strings(values, field) -> list[str]:
    out = []
    for v in values:
        if field is not None:
            v = v[field]
        if not isinstance(v, str):
            raise CorpusError(f"corpus item is not a string: {v!r}")
        out.append(v)
    return out


def module_assign(path: Path, name: str):
    for node in _tree(path).body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) \
            else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets) and node.value is not None:
            v = node.value
            if isinstance(v, ast.Call) and getattr(v.func, "attr", "") == "maketrans" and len(v.args) == 1:
                v = v.args[0]                   # str.maketrans({...}): the dict literal it is built from
            if isinstance(v, ast.Call) and getattr(v.func, "id", "") in ("frozenset", "set", "tuple") \
                    and len(v.args) == 1 and not v.keywords:
                v = v.args[0]                   # frozenset({...}): the literal it is built from (seo-py FORBIDDEN_KEYS)
            out = ast.literal_eval(v)
            if isinstance(out, (set, frozenset)):
                out = sorted(out)               # a set has no source order; sorted keeps the case ids reproducible
            return out
    raise CorpusError(f"{name} not found at module level")


def extract(ref: dict, service_dir: Path) -> list[str]:
    """One corpus reference -> its strings, in source order.

    ``{"file": <path under the service>, "test": <function>, "name": <local tuple name>}``
    ``{"file": ..., "test": ..., "loop": <n>, "field": <index or null>, "where": {<index>: <value>}}``
    ``{"file": ..., "test": ..., "parametrize": true, "field": ..., "where": ...}``
    ``{"file": ..., "module": <module-level name>, "after": <first item kept>, "before": <first item dropped>}``
    """
    path = service_dir / ref["file"]
    if not path.is_file():
        raise CorpusError(f"{ref['file']} does not exist")
    field = ref.get("field")
    where = {int(k): v for k, v in (ref.get("where") or {}).items()}

    def keep(v) -> bool:
        return all(v[k] == want for k, want in where.items())

    if "module" in ref:
        got = module_assign(path, ref["module"])
        vals = [got] if isinstance(got, str) else list(got)     # one module-level string is one seed
        if "after" in ref:
            vals = vals[vals.index(ref["after"]):]
        if "before" in ref:
            vals = vals[:vals.index(ref["before"])]
        out = _strings(vals, field)
    else:
        fn = _function(_tree(path), ref["test"])
        if "name" in ref:
            vals = None
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == ref["name"]
                                                        for t in node.targets):
                    vals = ast.literal_eval(node.value)
                    break
            if vals is None:
                raise CorpusError(f"{ref['test']}: no assignment to {ref['name']}")
        elif "loop" in ref:
            loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
            loops.sort(key=lambda n: (n.lineno, n.col_offset))
            if ref["loop"] >= len(loops):
                raise CorpusError(f"{ref['test']}: no for loop #{ref['loop']}")
            it = loops[ref["loop"]].iter
            if isinstance(it, ast.Call) and getattr(it.func, "id", "") == "enumerate":
                it = it.args[0]
            vals = ast.literal_eval(it)
        elif ref.get("parametrize"):
            vals = None
            for d in fn.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "parametrize":
                    vals = ast.literal_eval(d.args[1])
            if vals is None:
                raise CorpusError(f"{ref['test']}: no parametrize table")
        else:
            raise CorpusError(f"corpus reference names no name / loop / parametrize / module: {ref}")
        out = _strings([v for v in vals if keep(v)], field)
    if not out:
        raise CorpusError(f"corpus reference resolved to nothing: {ref}")
    return out


def confusables() -> dict[str, str]:
    path, name = CONFUSABLES_SOURCE
    table = module_assign(REPO / path, name)
    if not isinstance(table, dict) or not table:
        raise CorpusError(f"{path}:{name} is not a non-empty dict literal")
    return table


def homoglyph_table(core: bool = False) -> dict[str, list[str]]:
    """Latin letter -> its lookalikes (one-letter folds to an ASCII letter only), sorted for determinism. ``core``:
    only the lookalikes both repo tables list."""
    table = confusables()
    if core:
        path, name = CORE_CONFUSABLES_SOURCE
        small = module_assign(REPO / path, name)
        table = {k: v for k, v in table.items() if small.get(k) == v}
    inv: dict[str, list[str]] = {}
    for look, latin in table.items():
        if len(latin) == 1 and "a" <= latin <= "z" and len(look) == 1:
            inv.setdefault(latin, []).append(look)
    return {k: sorted(v) for k, v in sorted(inv.items())}
