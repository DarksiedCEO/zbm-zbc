"""C2-4 (core-spec/2 T-4): every sink field has a type decision (pattern of Wave-1 test_every_hash_field_has_a_decision).

(a) static: an ast scan of every writer call site in src (seo_log `_commit`, ledger evidence tuples, outbound
    `outbound.check` / `outbound_refused`, SeoError kwargs, own logging calls): every literal field path is decided in
    sink_fields.SINK_FIELDS (or a LOG_TEMPLATES literal / E-LLM / E-AUTH path); sites whose record is built at run time
    are counted and left to (b).
(b) dynamic: tests/t4_recorder.py (installed by conftest.py) records every (sink, kind, path) actually written; with
    T4_RECORD_OUT=<evidence.json> it records the whole run (gates G-1 full suite, G-5 war room) and this test, run last,
    fails on any undecided path. Without the flag, a smoke drives one write per sink and plants one undecided path per
    sink: the recorder must flag exactly those (G-6 mutant "T-4 dynamic half off").
"""
import ast
import logging
from pathlib import Path

import pytest
import t4_recorder as rec

import log_sink
import outbound
import sink_fields as sf
import sink_guard

SRC = Path(__file__).resolve().parent.parent / "src"
FILES = sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)
REQ_FIELDS = ("actor", "request_id", "request_sha", "_obj")        # service._req adds these to a line
EVIDENCE_FIELDS = ("rk", "seq")                                     # service._commit adds these to every payload
LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}


def _decided_static(sink, kind, path) -> bool:
    if rec.decided(sink, kind, path):
        return True
    return any(s == sink and k == kind and (p.startswith(path + "/") or p.startswith(path + "[]"))
               for s, k, p in sf.SINK_FIELDS) or (sink == "outbound" and any(
               k == kind and (p.startswith(path + "/") or p.startswith(path + "[]"))
               for k, p in (*sf.OUTBOUND_E_LLM, *sf.OUTBOUND_CONFIGURED)))


def _assigns(fn) -> dict:
    out = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            out.setdefault(n.targets[0].id, []).append(n.value)
    return out


def _dict_paths(node, env, prefix="", seen=0):
    """(paths, resolved) of a record expression; literal keys only (a non-literal key / spread -> not resolved)."""
    if seen > 8:
        return [], False
    if isinstance(node, ast.Dict):
        paths, ok = [], True
        for k, v in zip(node.keys, node.values):
            if k is None:
                sub, ok2 = _dict_paths(v, env, prefix, seen + 1)
                paths += sub
                ok = ok and ok2
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                p = prefix + "/" + sf._seg(k.value)          # sink_guard._key normalisation (shaN, ...)
                if isinstance(v, ast.Dict):
                    sub, ok2 = _dict_paths(v, env, p, seen + 1)
                    paths += sub
                    ok = ok and ok2
                else:
                    paths.append(p)
            else:
                ok = False
        return paths, ok
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_req" and node.args:
        sub, ok = _dict_paths(node.args[0], env, prefix, seen + 1)
        return sub + [prefix + "/" + f for f in REQ_FIELDS], ok
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "dict" and len(node.args) == 1:
        return _dict_paths(node.args[0], env, prefix, seen + 1)
    if isinstance(node, ast.IfExp):
        a, oka = _dict_paths(node.body, env, prefix, seen + 1)
        b, okb = _dict_paths(node.orelse, env, prefix, seen + 1)
        return a + b, oka and okb
    if isinstance(node, ast.Name) and node.id in env:
        paths, ok = [], True
        for value in env[node.id]:
            sub, ok2 = _dict_paths(value, env, prefix, seen + 1)
            paths += sub
            ok = ok and ok2
        return paths, ok
    return [], False


def _loggers(tree) -> dict:
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and getattr(n.value.func, "attr", "") == \
                "getLogger" and n.value.args and isinstance(n.value.args[0], ast.Constant):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n.value.args[0].value
    return out


def _error_classes(trees) -> set:
    names = {"SeoError"}
    for _ in range(4):
        for tree in trees.values():
            for n in ast.walk(tree):
                if isinstance(n, ast.ClassDef) and any(getattr(b, "id", getattr(b, "attr", "")) in names
                                                       for b in n.bases):
                    names.add(n.name)
    return names


def static_scan() -> dict:
    trees = {p: ast.parse(p.read_text()) for p in FILES}
    errors = _error_classes(trees)
    kwargs_ok = set().union(*sf.ERROR_KWARGS.values()) | {"reason"}
    res = {"sites": {}, "dynamic_only": {}, "undecided": []}

    def site(sink, rel, line, kind, paths, ok):
        res["sites"][sink] = res["sites"].get(sink, 0) + 1
        if not ok:
            res["dynamic_only"].setdefault(sink, []).append(f"{rel}:{line}")
        for p in sorted(set(paths)):
            if not _decided_static(sink, kind, p):
                res["undecided"].append((sink, kind, p, f"{rel}:{line}"))

    for path, tree in trees.items():
        rel, logs = str(path.relative_to(SRC)), _loggers(tree)
        fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        calls, tuples = {}, {}
        for fn in fns:
            env = _assigns(fn)
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    calls.setdefault(id(n), (n, env))
                elif isinstance(n, ast.Tuple):
                    tuples.setdefault(id(n), (n, env))
        for t, env in tuples.values():                     # ledger evidence: (event_type, subject, payload, id_parts)
            e = t.elts
            if len(e) == 4 and isinstance(e[0], ast.Constant) and isinstance(e[0].value, str) and \
                    isinstance(e[3], (ast.Tuple, ast.List)):
                paths, ok = _dict_paths(e[2], env)
                site("ledger", rel, t.lineno, e[0].value, paths + ["/" + f for f in EVIDENCE_FIELDS], ok)
        for n, env in calls.values():
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            lit = n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
            if name == "_commit" and len(n.args) >= 2:
                kind = n.args[0].value if lit else None
                paths, ok = _dict_paths(n.args[1], env)
                paths += ["/actor"] + (["/evidence"] if any(k.arg == "evidence" for k in n.keywords) else [])
                if kind is not None and kind not in sf.LOG_KINDS:
                    res["undecided"].append(("seo_log", kind, "(kind not typed)", f"{rel}:{n.lineno}"))
                site("seo_log", rel, n.lineno, kind, paths if kind else [], ok and kind is not None)
            elif name in ("check", "outbound_refused") and len(n.args) == 3 and (
                    name == "outbound_refused" or getattr(f.value, "id", "") == "outbound"):
                kind = n.args[0].value if lit else None
                paths, ok = _dict_paths(n.args[2], env)
                site("outbound", rel, n.lineno, kind, paths if kind else [], ok and kind is not None)
            elif name in errors:
                bad = [k.arg for k in n.keywords if k.arg not in kwargs_ok]
                site("error", rel, n.lineno, "kwargs", [], all(k.arg for k in n.keywords))
                res["undecided"] += [("error", "kwargs", "/" + str(b), f"{rel}:{n.lineno}") for b in bad]
            elif name in LEVELS and isinstance(f, ast.Attribute) and getattr(f.value, "id", "") in logs:
                logger = logs[f.value.id]
                ok = bool(lit) and n.args[0].value in sf.LOG_TEMPLATES.get(logger, ())
                site("logging", rel, n.lineno, logger, [], True)
                if not ok:
                    res["undecided"].append(("logging", logger, "/template", f"{rel}:{n.lineno}"))
                res["undecided"] += [("logging", logger, f"/args/{i}", f"{rel}:{n.lineno}")
                                     for i in range(len(n.args) - 1) if not rec.decided("logging", logger, f"/args/{i}")]
    return res


def test_t4_static_half_every_literal_writer_path_is_decided():
    res = static_scan()
    assert res["sites"].get("seo_log", 0) >= 30 and res["sites"].get("logging", 0) >= 10, res["sites"]
    assert res["sites"].get("error", 0) >= 150 and res["sites"].get("outbound", 0) >= 4, res["sites"]
    assert res["sites"].get("ledger", 0) >= 10, res["sites"]
    assert res["undecided"] == []


def test_t4_recorder_flags_exactly_the_planted_undecided_paths(monkeypatch):
    code = sorted(sf.ERROR_KWARGS)[0]
    from starlette.responses import JSONResponse
    with rec.recording():
        # C2-3b2 re-pin (T-5 / lead ruling 17:50 PT F4-0): an unknown seo_log kind is now refused at enforce; the
        # planted undecided seo_log write goes through a bypassed kind check, like the logging / outbound plants
        with pytest.raises(sink_guard.SinkViolation):
            sink_guard.enforce("t4_planted_kind", {"note": 1})
        monkeypatch.setattr(sink_guard, "_unknown_log_kind", lambda k: False)   # a bypassed kind check
        sink_guard.enforce("t4_planted_kind", {"note": 1})                    # seo_log: an untyped kind let through
        sink_guard.enforce("citation_run_interrupted", {"reason": "PROCESS_LOST", "actor": "seo"})   # seo_log: decided
        sink_guard.enforce(sink_guard.LEDGER + "log_anchor", {"seq": 3})       # ledger: decided (C2-2 seed)
        rec.observe("ledger", "log_anchor", ["/planted_extra"])
        log_sink.scrub_record(logging.LogRecord("seo.api", logging.ERROR, __file__, 1, "unexpected %s",
                                                ("ValueError",), None))
        JSONResponse(status_code=409, content={"detail": code})
        JSONResponse(status_code=409, content={"detail": code, "planted_extra": 1})
        monkeypatch.setattr(log_sink, "violation", lambda r: None)             # a bypassed log check
        log_sink.scrub_record(logging.LogRecord("seo.t4planted", logging.ERROR, __file__, 1, "t4 %s", (1,), None))
        monkeypatch.setattr(outbound, "violation", lambda *a: None)            # a bypassed outbound check
        outbound.check("openai", "https://api.openai.com/v1/responses", {"model": "m", "planted_extra": 1})
        got, rows = rec.undecided(), set(rec.SEEN)
    assert got == sorted([("error", code, "/planted_extra"), ("ledger", "log_anchor", "/planted_extra"),
                          ("logging", "seo.t4planted", "/args/0"), ("logging", "seo.t4planted", "/template"),
                          ("outbound", "openai", "/planted_extra"), ("seo_log", "t4_planted_kind", "/note")])
    for sink in sf.SINKS:
        assert any(r[0] == sink and r[3] for r in rows), sink                 # >= 1 decided write recorded per sink


@pytest.mark.sink_last
def test_every_sink_field_has_a_type_decision():
    res = static_scan()
    assert res["undecided"] == [], res["undecided"]                            # (a)
    if rec.STATE["out"]:                                                       # (b) gate run: everything written
        rec.write()
        assert rec.STATE["active"] and sum(rec.SEEN.values()) > 0
        assert rec.undecided() == [], rec.undecided()[:50]
    else:
        assert rec.STATE["installed"] and sink_guard.enforce.__wrapped__ is not None
