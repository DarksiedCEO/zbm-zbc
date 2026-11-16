"""core-spec/2 G-6: the mutant list over the changed code of core-spec/2 (>= 30), incl. the 8 named in G-6:
VS length 8 -> 9 (vs_len_9); piece cap 24 -> 23 (piece_cap_23); J minus each character (j_minus_00..10); joiner
rule off (joiner_rule_off); one exempt shape added (exempt_shape_added); T-4 dynamic half off (t4_dynamic_off); RV
audit written after the value (rv_audit_after_value); AL accepting K-1 (al_accepts_k1).

Same runner contract as tools/mutants.py (core-spec/1): each mutant is ONE exact source edit (old text must occur
exactly once); the runner copies services/seo-py into a temporary directory, applies it and runs the G-2 CI lane
(lanes_core2.CI, the mutant's own group first, -x). KILLED = a test fails. Counts and names only. A SURVIVOR must be
shown output-identical on VM, FS and the CV-8 corpora (G-6) - the runner only reports it.

    python tests/conformance/tools/mutants_core2.py --check          every edit applies exactly once (no tests run)
    python tests/conformance/tools/mutants_core2.py [name,...]       run (long: lead launches it in the background)
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
from conformance import lanes_core2 as L  # noqa: E402

CV, AL, RV, API = "src/agents/cv_shape.py", "src/agents/cv_allowlist.py", "src/svc_reveal.py", "src/api.py"
SG, SK, T4 = "src/agents/secret_guard.py", "src/sink_guard.py", "tests/t4_recorder.py"
_RV_COMMIT = "        self._commit(LOG_KIND, data, actor, evidence=(AUDIT_EVENT, tid, row, (tid, actor, at, code)))\n"
_RV_TAIL = ('        if over:\n            raise Throttled(R("REVEAL_RATE_LIMITED"))\n        if value is None:\n'
            '            raise NotFound(R("NOT_FOUND"))\n        return {"value": value}')


def _j_mutants() -> dict:
    """One mutant per character of cv_shape.J: the J literal as written in the source minus that character."""
    line = next(x for x in (ROOT / CV).read_text(encoding="utf-8").splitlines() if x.startswith('J = frozenset("'))
    lit = line[len('J = frozenset("'):line.index('")')]
    toks, k = [], 0
    while k < len(lit):
        n = 2 if lit[k] == "\\" else 1
        toks.append(lit[k:k + n])
        k += n
    out = {}
    for i in range(len(toks)):
        new = 'J = frozenset("' + "".join(toks[:i] + toks[i + 1:]) + '")'
        out[f"j_minus_{i:02d}"] = (CV, 'J = frozenset("' + lit + '")', new)
    return out


MUTANTS = {
    # ---- G-6 named (CV-3 / CV-4 / CV-2 / CV-6)
    "vs_len_9": (CV, "TOKEN_MIN = 8 ", "TOKEN_MIN = 9 "),
    "piece_cap_23": (CV, "PIECE_MAX = 24 ", "PIECE_MAX = 23 "),
    **_j_mutants(),
    "joiner_rule_off": (CV, "            if c in JN and 0 < k < last and _letter(core[k - 1]) and _letter(core[k + 1]):",
                        "            if False:"),
    "exempt_shape_added": (CV, '    ("tyre", ', '    ("hex_run", r"[0-9a-f]{8,}"),\n    ("tyre", '),
    # ---- G-6 named (T-4 / RV / AL)
    "t4_dynamic_off": (T4, '    if STATE["installed"]:\n        return\n    import log_sink',
                       '    if True:\n        return\n    import log_sink'),
    "rv_audit_after_value": (RV, _RV_COMMIT + _RV_TAIL, _RV_TAIL.replace('        return {"value": value}', (
        '        out = {"value": value}\n        try:\n    ' + _RV_COMMIT + '        except Exception:\n'
        '            pass\n        return out'))),
    "al_accepts_k1": (AL, "    return not _never(view_text)\n", "    return True\n"),
    # ---- other CV-3 / CV-4 rules
    "vs_classes_3": (CV, "sum(classes(c)) < 2:", "sum(classes(c)) < 3:"),
    "run_max_65": (CV, "RUN_MAX = 64 ", "RUN_MAX = 65 "),
    "fragment_no_symbol": (CV, "    _lt, dg, sy = classes(core(piece))\n    return dg or sy",
                           "    _lt, dg, sy = classes(core(piece))\n    return dg"),
    "run_needs_3": (CV, "        if len(cur) >= 2:\n            yield", "        if len(cur) >= 3:\n            yield"),
    "run_two_j": (CV, "p[0] == cur[-1][1] + 1", "p[0] <= cur[-1][1] + 2"),
    "al_split_listed_off": (CV, "                kept += _split_listed(text, v, src, s)", "                pass"),
    # ---- AL / RV
    "al_never_whole_span_only": (AL, 'for s in [view_text] + [cv_shape.core(t) for t in view_text.split(" ")]:',
                                 "for s in [view_text]:"),
    "rv_rate_cap_off": (RV, '            raise Throttled(R("REVEAL_RATE_LIMITED"))', "            pass"),
    "rv_no_store_off": (API, '(b"cache-control", b"no-store")', '(b"cache-control", b"private")'),
    # ---- BL (secret_guard prose-value read, C2-16b)
    "bl_marks_are_symbols": (SG, '    tags.discard("m")\n', ""),
    "bl_capital_rule_off": (SG, "    return not _cv_one_script_word(val) or any(c.isupper() for c in val[1:])",
                            "    return not _cv_one_script_word(val)"),
    "bl_mixed_script_is_prose": (SG, "        return False\n    return True\n", "        return True\n    return True\n"),
    "bl_lookalike_pair_off": (SG, 'if len(tags) == 2 and "L" in tags:', "if False:"),
    # ---- T-5 / T-2 withhold (sink_guard)
    "t5_enforce_off": (SK, "    if bad:\n        raise SinkViolation(kind, bad)", "    if False:\n        raise SinkViolation(kind, bad)"),
    "withhold_nested_off": (SK, "            return any(m.fullmatch(np) for m in _ref_paths(tuple(ref_sink))) "
                            "and not _FIELDS.is_ref(obj)", "            return False"),
    "withhold_ref_off": (SK, "        return field.ref and not _FIELDS.is_ref(obj)", "        return False"),
    "withhold_t2_waiver_any": (SK, "            or (kind is not None and _t2_ref_field(kind, path, v))",
                               "            or (kind is not None)"),
}
GROUP = {CV: "CV-10", AL: "AL", RV: "RV", API: "RV", SG: "BL", SK: "T", T4: "T"}


def tests_for(path: str) -> list:
    first = L.CI[GROUP[path]]
    return first + [p for p in L.paths("ci") if p not in first]


def check() -> dict:
    bad = {}
    for name, (path, old, _new) in MUTANTS.items():
        n = (ROOT / path).read_text(encoding="utf-8").count(old)
        if n != 1:
            bad[name] = n
    return {"mutants": len(MUTANTS), "not_exactly_once": bad}


def run(name: str) -> dict:
    path, old, new = MUTANTS[name]
    tmp = Path(tempfile.mkdtemp(prefix=f"mut2-{name}-"))
    try:
        dst = tmp / "seo-py"
        shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".data*"))
        p = dst / path
        src = p.read_text(encoding="utf-8")
        if src.count(old) != 1:
            return {"name": name, "result": "NOT_APPLIED", "count": src.count(old)}
        p.write_text(src.replace(old, new), encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-m", "core2_ci",
                            *tests_for(path)], cwd=dst, capture_output=True, text=True, timeout=3 * 3600)
        failed = [x.split(" - ")[0] for x in r.stdout.splitlines() if x.startswith(("FAILED", "ERROR"))]
        return {"name": name, "result": "KILLED" if r.returncode != 0 else "SURVIVED", "by": failed[:1]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--check" in sys.argv:
        print(json.dumps(check()))
        sys.exit(0)
    names = sys.argv[1].split(",") if len(sys.argv) > 1 else list(MUTANTS)
    out = []
    for n in names:
        out.append(run(n))
        print(json.dumps(out[-1]), flush=True)
    print(json.dumps({"run": len(out), "killed": sum(o["result"] == "KILLED" for o in out),
                      "survived": [o["name"] for o in out if o["result"] == "SURVIVED"],
                      "not_applied": [o["name"] for o in out if o["result"] == "NOT_APPLIED"]}))
