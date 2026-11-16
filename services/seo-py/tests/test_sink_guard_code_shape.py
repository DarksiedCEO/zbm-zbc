"""core-spec/2 T-1 / T-5 step (3) (lead rulings 21:25 / 22:00 PT, card C2-6b, finding F6-1): the closed shape rule of
the open-`code` strict check (``sink_guard.code_shape_refusal``). Both directions: the shape classes F6-1 found
accepted are refused; the code base's own open codes (every string constant of src/ in the code alphabet) are not.
Values here are hand-written structural examples, never generator values."""
import ast
import pathlib

import pytest

import sink_guard as sg

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

# string constants of src/ the rule refuses, each checked to be no value any writer puts in a sink (C2-6b read):
# a hex alphabet fragment used by a character test, the float-format literal, a JWT prefix used by a detector
NOT_WRITTEN = frozenset(("abcdef", "NaN", "eyJ"))


@pytest.mark.parametrize("value, rule", [
    ("aB3:xYz", "mixed_case_digits"),          # F6-1 (a): mixed case + digits + one code symbol
    ("Qr7.tUv", "mixed_case_digits"),
    ("k9-Lm", "mixed_case_digits"),
    ("word-page-7f3a91", "hex_run"),           # F6-1 (b): word + '-' + 6 lower hex
    ("word-page-fadedb", "hex_run"),           # word-only variant: the hex run has no digit
    ("word-page-123456", "digit_run"),         # digit-only variant
    ("ab_xQzT", "case"),                       # a random-case piece is no camel word
    ("ABC_D3EF", "inner_digits"),              # digits inside a single-case piece
    ("x_3abc", "inner_digits"),                # leading digits before more than 2 letters
])
def test_the_f6_1_shape_classes_are_refused_as_codes(value, rule):
    assert sg.code_shape_refusal(value) == rule
    assert sg.classify(value) is None
    assert sg._judge(value)[1] in ("shape", "secret")


@pytest.mark.parametrize("value", [
    "agent_moved", "AGENT_UNKNOWN", "rateLimitExceeded", "OpenAIEngine", "ChatGPT-User", "ValueError",
    "JSONDecodeError", "api_401", "http_404", "CANONICAL_TARGET_NOT_200", "utf-8", "sha256", "seo_02", "2nd", "5xx",
    "web_search_20250305", "ISBN-10", "managed_3", "wilson_95", "lookalikes-1",
])
def test_open_codes_of_the_code_base_keep_passing(value):
    assert sg.code_shape_refusal(value) is None
    assert sg.classify(value) == "code"


def test_every_code_constant_of_src_passes_the_shape_rule():
    """Every string constant of src/ that is a code today passes the rule, except the closed NOT_WRITTEN list."""
    refused, seen = set(), 0
    for f in SRC.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            v = getattr(n, "value", None) if isinstance(n, ast.Constant) else None
            if type(v) is not str or not 0 < len(v) <= sg.CODE_MAX or v.strip(sg._CODE_CHARS):
                continue
            seen += 1
            if sg.code_shape_refusal(v) is not None and sg.classify(v) is None and sg._check(v) is None:
                refused.add(v)
    assert seen > 2000
    assert refused <= NOT_WRITTEN, sorted(refused - NOT_WRITTEN)


def test_enums_and_t2_refs_keep_their_exact_checks():
    """Catalogue members are codes by membership (the rule never judges them); a T-2 ref keeps its own grammar."""
    assert all(sg.classify(c) is not None for c in sg.CODES)
    import sink_fields as sf
    ref = "c:text:" + "0123456789abcdef"
    assert sf.is_ref(ref)
    assert sg.code_shape_refusal("text:0123456789abcdef") == "hex_run"   # outside a T-2 field it is no code


def test_the_rule_leaves_the_charset_gate_its_own_verdict():
    assert sg.code_shape_refusal("ab@cd") is None and sg.code_shape_refusal("two words") is None
    assert sg._judge("ab@cd")[1] == "at" and sg._judge("two words")[1] == "space"


def test_the_rule_is_linear_and_total_on_the_code_alphabet():
    for v in ("a" * 64, "A" * 64, "aB" * 32, "1" * 64, "_" * 63 + "a", "a-" * 32, "Ab" * 32, "aA1" * 21):
        sg.code_shape_refusal(v)                # never raises


@pytest.mark.parametrize("value", ["report/", "rules/", "a/", "seo-report/"])
def test_a_named_version_with_an_empty_version_is_refused_never_raised(value):
    """C2-6c, lead ruling 22:50 PT F6b-3 (DEFECT): ``<name>/`` with an empty version part made ``_is_version("")``
    index an empty string (IndexError -> a 5xx-shaped path). It must be a plain refusal: no class, a reason, and the
    write refused (T-5, SINK_REFUSED), never an exception. Hand-written structural values."""
    assert sg._is_version("") is False
    assert sg._named_version(value) is False
    cls, reason = sg._judge(value)
    assert cls is None and reason
    assert sg.classify(value) is None
    assert sg.violations("job_ran", {"job": value}) != []
    with pytest.raises(sg.SinkViolation):
        sg.enforce("job_ran", {"job": value})
    for ok in ("report/3", "rules/2026-11-14", "renderer/1.2.0"):      # the shape it guards still classifies
        assert sg.classify(ok) == "version"


# ----------------------------------------------------------------- C2-6f (lead ruling 01:20 PT (B)); synthetic values
import sink_fields as _sf  # noqa: E402


@pytest.mark.parametrize("canary", ["word-page-a1b2c3", "facade-page-0fa9ce", "page-12ab34", "x_beef42"])
def test_rule3_a_hex_run_with_a_digit_is_still_refused(canary):
    assert sg.code_shape_refusal(canary) == "hex_run"
    assert sg.classify(canary) is None


_TID40 = "abcdef" * 6 + "abcd"     # 40-letter tenant id of the FCL-IDS grammar (synthetic; the max-bound shape)
_EID = "ent-" + _TID40


def test_entity_id_fields_take_the_exact_grammar_instead_of_the_shape_rule():
    assert sg.classify(_EID) is None              # the open-code shape rule alone refuses it (rule (3))
    for key, rx in _sf.ENTITY_ID_FIELDS.items():
        f = _sf.SINK_FIELDS[key]
        assert f.grammar is rx and f.types == {"code"} and not f.ref and f.values is None
        good = ("entity:" + _EID) if key[2].endswith("subject_id") else _EID
        assert _sf.matches(f, good), key
        assert _sf.matches(f, good.replace(_TID40, "zbm")), key
    kind = "entity_created"
    rec = {"entity_id": _EID, "_obj": _EID, "evidence": [{"subject_id": "entity:" + _EID,
                                                         "payload": {"entity_id": _EID}}]}
    own = {"/entity_id", "/_obj", "/evidence[]/subject_id", "/evidence[]/payload/entity_id"}
    unreg = "ent-" + "fedcba" * 6 + "dcba"       # same grammar, never registered (ruling 02:15 PT F6f3-1)
    rec_u = {"entity_id": unreg, "_obj": unreg, "evidence": [{"subject_id": "entity:" + unreg,
                                                             "payload": {"entity_id": unreg}}]}
    assert {p for p, _r in sg.violations(kind, rec_u)} >= own
    sg.register_tenant(_TID40)                    # ent- + a REGISTERED tenant id only
    paths = {p for p, _r in sg.violations(kind, rec)}
    assert not paths & own


@pytest.mark.parametrize("bad", ["ent-", "ent-9abc", "ent-Abc", "ent-" + "a" * 41, "acme-page-a1b2c3", "ent-a b",
                                 "{c:text:0123456789abcdef}", "c:text:0123456789abcdef", "ENT-acme"])
def test_entity_id_fields_refuse_what_the_grammar_does_not_admit(bad):
    for key in _sf.ENTITY_ID_FIELDS:
        assert not _sf.matches(_sf.SINK_FIELDS[key], bad), key
    paths = {p for p, _r in sg.violations("entity_created", {"entity_id": bad})}
    assert "/entity_id" in paths


def test_entity_grammar_waives_the_shape_verdict_only_never_the_same_value_elsewhere():
    paths = {p for p, _r in sg.violations("entity_created", {"seed": _EID})}
    assert "/seed" in paths                               # an open code field keeps the shape rule


# --- C2-6f3 F6f-1 (lead ruling 01:50 PT, within ruling 21:45 PT's pattern; FCL-IDS, O-4 I / T-7): every tenant id the
# route accepts (config.TENANT_ID = models.TenantCreate's pattern) is accepted on the tenant_id sink fields and, as
# ent-<tid>, on the entity-id fields; the same strings elsewhere, and non-matching values there, keep the strict check.
import random as _random  # noqa: E402
import string as _string  # noqa: E402

import config as _cfg  # noqa: E402

_AL = _string.ascii_lowercase + _string.digits


def _tids(n=60, seed=20261115):
    rnd = _random.Random(seed)
    out = [rnd.choice(_string.ascii_lowercase) + "".join(rnd.choice(_AL) for _ in range(39)) for _ in range(n)]
    out += [rnd.choice(_string.ascii_lowercase) + "".join(rnd.choice(_string.digits) for _ in range(39))]
    out += [rnd.choice("abcdef") + "".join(rnd.choice("abcdef0123456789") for _ in range(k)) for k in (5, 15, 39)]
    assert all(_cfg.TENANT_ID.fullmatch(t) for t in out)
    return out


def _nest(path, value):
    segs = [x for x in path.split("/") if x]

    def at(i):
        if i == len(segs):
            return value
        return {segs[i][:-2]: [at(i + 1)]} if segs[i].endswith("[]") else {segs[i]: at(i + 1)}
    return at(0)


def _kind(sink, kind):
    return sg.LEDGER + kind if sink == "ledger" else kind


_TID_FIELDS = [k for k, f in _sf.SINK_FIELDS.items() if k[2].endswith("/tenant_id") and "code" in f.types and not f.ref]


def _refused_at(sink, kind, path, value):
    return any(p == path for p, _r in sg.violations(_kind(sink, kind), _nest(path, value)))




def test_f6f3_route_tenant_ids_accepted_only_once_registered():
    """Ruling 02:15 PT F6f3-1: tenant_id fields take a REGISTERED config.TENANT_ID id; ent- fields ent- + a registered
    id. The same grammar-valid ids unregistered are refused (T-5 canaries are such ids)."""
    # C2-6i (ruling 07:40 PT) re-pin: a registered id is accepted only when the route's own check
    # (sg.identifier_refused(prose=True), the function svc_tenants.create_tenant calls) takes it; registered ids the
    # route refuses stay refused at every cell. Short ids are added so the route-accepted side is not empty.
    assert _TID_FIELDS and _sf.ENTITY_ID_FIELDS
    rnd = _random.Random(20261117)
    tids = _tids(seed=20261116) + [t for t in (rnd.choice(_string.ascii_lowercase) + "".join(
        rnd.choice(_string.ascii_lowercase + _string.digits + "-") for _ in range(rnd.randint(3, 11)))
        for _ in range(80)) if _cfg.TENANT_ID.fullmatch(t)]
    assert sum(sg._judge(t)[1] is not None for t in tids) > 0       # the strict heuristic alone refuses some
    acc = [t for t in tids if not sg.identifier_refused(t, prose=True)]
    ref = [t for t in tids if sg.identifier_refused(t, prose=True)]
    assert acc and ref

    def bad(ids):
        n = sum(_refused_at(s, k, p, t) for t in ids for s, k, p in _TID_FIELDS)
        return n + sum(_refused_at(s, k, p, ("entity:ent-" if p.endswith("subject_id") else "ent-") + t)
                       for t in ids for s, k, p in _sf.ENTITY_ID_FIELDS)
    per = len(_TID_FIELDS) + len(_sf.ENTITY_ID_FIELDS)
    assert bad(tids) == len(tids) * per                              # unregistered: every cell refused
    for t in tids:
        sg.register_tenant(t)
    assert bad(acc) == 0                                             # registered + route-accepted: every cell accepted
    assert bad(ref) == len(ref) * per                                # registered but route-refused: every cell refused


def test_f6f3_same_ids_elsewhere_and_non_matching_values_keep_the_strict_check():
    tids = [t for t in _tids(seed=20261117) if sg._judge(t)[1] is not None]
    assert tids
    for t in tids:
        sg.register_tenant(t)
    for t in tids[:10]:
        assert "/seed" in {p for p, _r in sg.violations("entity_created", {"seed": t})}
        assert "/seed" in {p for p, _r in sg.violations("entity_created", {"seed": "ent-" + t})}
    s, k, p = _TID_FIELDS[0]
    es, ek, ep = next(x for x in _sf.ENTITY_ID_FIELDS if not x[2].endswith("subject_id"))
    for t in tids[:10]:
        for v in (t + "x" * (41 - len(t)), t.upper(), "_" + t[1:], t[:-1] + "_"):     # outside the grammar
            assert not _cfg.TENANT_ID.fullmatch(v)
            assert _refused_at(s, k, p, v)
            assert _refused_at(es, ek, ep, "ent-" + v)
    sg.register_tenant("Not-A-Tenant")                               # off-grammar: never registered
    assert "Not-A-Tenant" not in sg._REGISTERED


def test_f6f3_2_ledger_kind_first_call_in_a_fresh_process_does_not_crash():
    """Ruling 02:15 PT F6f3-2: _t2_ref_ok loads the field table before the kind test (fresh interpreter)."""
    import pathlib  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415
    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    code = ("import sys; sys.path.insert(0, %r); import sink_guard as g; "
            "r = g.violations(g.LEDGER + 'tenant_created', {'tenant_id': 'A' * 8 + '9f' * 12 + 'Zq', 'kind': 'client'}); "
            "print(len(r) > 0)") % str(src)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip() == "True", out.returncode
