"""C2-3b (core-spec/2 T-1 / T-3 / T-5; lead rulings 14:58 PT F3-2 + F3-6): error bodies are a typed sink.

Domain errors: record kind = the reason code; ``detail`` + a CLOSED set of kwargs per reason, frozen from the static
inventory of every raise site (re-scanned here); an unknown / ill-typed kwarg -> {"detail": <reason>} only, counted.
422 validation: ``msg`` prose replaced by ``msg_code`` (closed set); ``loc`` closed names; ``errors_total`` number.
Values come from the frozen generator VG (conformance.gen_values_v2) at run time; none is printed."""
from __future__ import annotations

import ast
import json
import pathlib

import pytest

import api
import reasons
import sink_fields as sf
import sink_guard as sg
from conformance import gen_values_v2 as vg
from errors import Conflict, Invalid, SeoError, Unavailable
from reasons import R

SRC = pathlib.Path(api.__file__).resolve().parent
ERRS = {"SeoError", "NotFound", "Forbidden", "FounderRefused", "Conflict", "Invalid", "Throttled", "Unavailable"}
SEED = vg.SEEDS[0]
_WORDS = vg.prose(SEED, 1000)
PROSE = [" ".join(_WORDS[i:i + 5]) for i in range(0, 1000, 5)]   # 200 sentences
VALUES = vg.values(SEED, 200)


def _raise_sites():
    out = []
    for p in sorted(SRC.rglob("*.py")):
        for n in ast.walk(ast.parse(p.read_text())):
            if isinstance(n, ast.Call):
                f = n.func
                name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
                if name in ERRS and p.name != "errors.py":
                    out.append((p.name, n))
    return out


SITES = _raise_sites()


def _literal_reason(call):
    """The reason of R('<CODE>') as a literal, or None when it is computed at run time."""
    a = call.args[0]
    inner = a.args[0] if a.args else None
    return inner.value if isinstance(inner, ast.Constant) else None


# ------------------------------------------------------------------------------------------- static inventory (F3-6)
def test_every_raise_site_passes_its_reason_through_R_and_kwargs_by_name():
    assert len(SITES) >= 150
    for fname, call in SITES:
        a = call.args[0] if call.args else None
        assert len(call.args) == 1 and isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "R", \
            (fname, call.lineno)
        assert all(k.arg is not None for k in call.keywords), (fname, call.lineno)   # no **body splat


def test_the_kwarg_inventory_is_exactly_the_closed_table():
    used = {}
    for fname, call in SITES:
        for k in call.keywords:
            used.setdefault(k.arg, set()).add((fname, call.lineno, _literal_reason(call)))
    assert set(used) == {"field", "override_allowed"}
    assert {c.keywords[0].value.value for _f, c in SITES if c.keywords and c.keywords[0].arg == "field"} == \
        set(sf.ERROR_FIELD_NAMES)
    # `field` is raised with the literal reason INVALID only, and a literal request-field name
    assert {r for _f, _l, r in used["field"]} == {"INVALID"} == set(sf.ERROR_FIELD_REASONS)
    for fname, call in SITES:
        for k in call.keywords:
            if k.arg == "field":
                assert isinstance(k.value, ast.Constant) and k.value.value in sf.ERROR_FIELD_NAMES
            if k.arg == "override_allowed":
                assert fname == "svc_invoices.py" and isinstance(k.value, ast.Constant) and type(k.value.value) is bool
                lit = _literal_reason(call)
                assert lit is None or lit in sf.ERROR_OVERRIDE_REASONS
    # the run-time reasons of svc_invoices (R(code) / R(exc.code)) come from finance_client + compliance_client
    raised = set()
    for mod in ("finance_client.py", "compliance_client.py"):
        for n in ast.walk(ast.parse((SRC / mod).read_text())):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in reasons.CODES:
                raised.add(n.value)
    assert raised and raised <= sf.ERROR_OVERRIDE_REASONS
    assert sf.ERROR_OVERRIDE_REASONS <= reasons.CODES
    # no unused table entry: every typed kwarg is raised somewhere
    assert set().union(*sf.ERROR_KWARGS.values()) == set(used)


def test_the_error_table_covers_every_reason_code_and_validation():
    kinds = {k for s, k, _p in sf.SINK_FIELDS if s == "error"}
    assert kinds == set(reasons.CODES) | {"validation", "health"}     # + health: lead ruling 19:05 PT FCL-Q1
    assert {p for s, k, p in sf.SINK_FIELDS if s == "error" and k == "health"} == {"/status"}
    assert "error" in sf.SINKS
    for r in reasons.CODES:
        assert sf.SINK_FIELDS[("error", r, "/detail")].types == {"code"} and sg.classify(r) == "code"


# --------------------------------------------------------------------------------------------- domain bodies (T-5)
@pytest.mark.parametrize("reason", sorted(reasons.CODES))
def test_each_reason_codes_body_is_typed(reason):
    content, refused = sf.error_body(reason, {})
    assert (content, refused) == ({"detail": reason}, None)
    if reason in sf.ERROR_FIELD_REASONS:
        assert sf.error_body(reason, {"field": "path"}) == ({"detail": reason, "field": "path"}, None)
    if reason in sf.ERROR_OVERRIDE_REASONS:
        for b in (True, False):
            assert sf.error_body(reason, {"override_allowed": b}) == ({"detail": reason, "override_allowed": b}, None)
    else:
        assert sf.error_body(reason, {"override_allowed": True}) == ({"detail": reason}, "untyped")


def test_unknown_or_ill_typed_kwarg_is_refused_and_the_body_is_detail_only():
    bad = [("INVALID", {"note": PROSE[0]}, "untyped"), ("TENANT_NOT_FOUND", {"field": "path"}, "untyped"),
           ("INVALID", {"field": "path", "x": 1}, "untyped"), ("INVALID", {"field": PROSE[1]}, "field_type"),
           ("INVALID", {"field": 7}, "field_type"), ("INVALID", {"field": None}, "field_type"),
           ("VAULT_UNAVAILABLE", {"override_allowed": "yes"}, "field_type"),
           ("VAULT_UNAVAILABLE", {"override_allowed": 1}, "field_type")]
    for reason, body, why in bad:
        assert sf.error_body(reason, body) == ({"detail": reason}, why)
    # `field` is an enum (closed constant): no VG value, prose sentence or single prose word is accepted
    assert not [v for v in PROSE + VALUES + _WORDS if v not in sf.ERROR_FIELD_NAMES
                and sf.error_body("INVALID", {"field": v})[1] is None]


@pytest.fixture()
def domain(tmp_path):
    import helpers
    h = helpers.Harness(tmp_path)
    app = h.client.app
    while not hasattr(app, "exception_handlers"):       # the FastAPI app under the ASGI middleware wrappers
        app = app.app
    return app.exception_handlers[SeoError]


def test_the_api_handler_sends_detail_only_and_counts_the_refusal(domain):
    api.ERROR_REFUSED.clear()
    ok = domain(None, Invalid(R("INVALID"), field="path"))
    assert ok.status_code == 422 and json.loads(ok.body) == {"detail": "INVALID", "field": "path"}
    ok = domain(None, Unavailable(R("FINANCE_UNAVAILABLE"), override_allowed=True))
    assert ok.status_code == 503 and json.loads(ok.body) == {"detail": "FINANCE_UNAVAILABLE", "override_allowed": True}
    assert api.ERROR_REFUSED == {}
    for i, p in enumerate(PROSE[:20]):
        r = domain(None, Conflict(R("JOB_RUNNING"), note=p))
        assert r.status_code == 409 and json.loads(r.body) == {"detail": "JOB_RUNNING"}
        r = domain(None, Invalid(R("INVALID"), field=p))
        assert json.loads(r.body) == {"detail": "INVALID"} and p.encode() not in r.body
    assert api.ERROR_REFUSED == {("JOB_RUNNING", "untyped"): 20, ("INVALID", "field_type"): 20}
    api.ERROR_REFUSED.clear()


# ---------------------------------------------------------------------------------------- validation bodies (F3-2)
def _check_validation(out):
    assert set(out) == {"detail", "errors_total"} and type(out["errors_total"]) is int
    for e in out["detail"]:
        assert set(e) == {"loc", "msg_code", "type"}
        assert e["msg_code"] in api.ERROR_MSG_CODES
        assert sf.matches(sf.SINK_FIELDS[("error", "validation", "/detail[]/msg_code")], e["msg_code"])
        assert sf.matches(sf.SINK_FIELDS[("error", "validation", "/detail[]/type")], e["type"])
        for x in e["loc"]:
            # C2-3b2 re-pin (T-1 / lead ruling 15:32 PT F3b-1): an invented key's loc is the code unknown_key, not "*": every loc element, unknown_key included, is a T-1 value of the loc field
            assert (x in api.ERROR_LOC_NAMES or x == api.ERROR_LOC_UNKNOWN or type(x) is int) and \
                sf.matches(sf.SINK_FIELDS[("error", "validation", "/detail[]/loc[]")], x)


def test_validation_msg_code_is_the_closed_type_and_no_prose_survives():
    assert api.ERROR_MSG_CODES == api.ERROR_TYPES_WITH_MESSAGE | {"invalid"}
    assert all(sg.classify(c) == "code" for c in api.ERROR_MSG_CODES)
    errs = []
    for i, (p, v) in enumerate(zip(PROSE, VALUES)):
        t = sorted(api.ERROR_TYPES_WITH_MESSAGE)[i % len(api.ERROR_TYPES_WITH_MESSAGE)] if i % 3 else v
        errs.append({"loc": ("body", p, i, v), "msg": "Value error, " + p, "type": t, "input": v, "ctx": {"e": p}})
    out = api._sanitize(errs)
    _check_validation(out)
    text = json.dumps(out)
    assert not [p for p in PROSE if p in text] and not [v for v in VALUES if v in text]
    assert [e["msg_code"] for e in out["detail"][:3]] == ["invalid", errs[1]["type"], errs[2]["type"]]
    assert out["errors_total"] == len(errs)


def test_no_prose_in_any_error_body_through_the_api(tmp_path):
    import helpers
    h = helpers.Harness(tmp_path)
    bodies = []
    for i, p in enumerate(PROSE[:40]):
        for r in (h.post("/tenants", {"request_id": p, "tenant_id": p, "kind": p, p: 1}, andre=True),
                  h.get("/audit/export", params={"since": p, "limit": VALUES[i]}),
                  h.get("/tenants/zbm/audits/" + VALUES[i].replace("/", "x").replace("?", "x").replace("#", "x"))):
            assert r.status_code in (404, 422), r.status_code
            out = r.json()
            if r.status_code == 422 and isinstance(out.get("detail"), list):
                _check_validation(out)
            else:
                assert set(out) <= {"detail", "field", "override_allowed"} and sg.classify(out["detail"]) == "code"
            bodies.append(r.text)
    text = "\n".join(bodies)
    assert not [p for p in PROSE[:40] if p in text]
    assert '"msg"' not in text


# ------------------------------------------------------------------ C2-3b2 (lead rulings 15:32 PT F3b-2/3/5, 17:50 PT F4-1)
FRAMEWORK_CODES = ("INTERNAL_ERROR", "NOT_FOUND", "METHOD_NOT_ALLOWED")


def test_the_new_catalogue_codes_are_closed_reasons_with_a_typed_error_row():
    for c in FRAMEWORK_CODES:
        assert c in reasons.CODES and R(c) == c and sg.classify(c) == "code"
        assert sf.SINK_FIELDS[("error", c, "/detail")] and sf.error_body(c, {}) == ({"detail": c}, None)
    assert api.ERROR_INTERNAL == "INTERNAL_ERROR" and api.ERROR_LOC_UNKNOWN == "unknown_key"
    assert sf.matches(sf.SINK_FIELDS[("error", "validation", "/detail[]/loc[]")], api.ERROR_LOC_UNKNOWN)
    assert not hasattr(api, "ERROR_MAX_STR")                               # F3b-5: the unused constant is gone


def test_unexpected_500_and_an_unknown_reason_send_INTERNAL_ERROR_counted(domain, tmp_path):
    import helpers
    h = helpers.Harness(tmp_path)
    app = h.client.app
    while not hasattr(app, "exception_handlers"):
        app = app.app
    r = app.exception_handlers[Exception](None, RuntimeError(PROSE[0]))     # F3b-2
    assert r.status_code == 500 and json.loads(r.body) == {"detail": "INTERNAL_ERROR"}
    api.ERROR_REFUSED.clear()
    for p in PROSE[:20]:                                                   # F3b-3: a reason that bypassed R()
        r = domain(None, Conflict(p))
        # C2-3b3 re-pin (G-7; lead ruling 18:20 PT F3b2-3, T-5): an unknown reason is a code bug -> 500, not 409
        assert r.status_code == 500
        assert json.loads(r.body) == {"detail": "INTERNAL_ERROR"} and p not in r.body.decode()
    assert api.ERROR_REFUSED == {("INTERNAL_ERROR", "unknown_reason"): 20}
    api.ERROR_REFUSED.clear()


def test_framework_404_and_405_bodies_are_closed_reason_codes(tmp_path):
    import helpers
    h = helpers.Harness(tmp_path)
    for i, v in enumerate(VALUES[:40]):
        seg = "".join(ch if ch.isalnum() else "x" for ch in v) or "x"
        r = h.client.get("/seo/v1/no-such-route-" + seg + "/" + str(i))   # no route: the framework's default 404
        assert r.status_code == 404 and r.json() == {"detail": "NOT_FOUND"}, r.status_code
    r = h.client.delete("/seo/v1/tenants")                                 # a route without this method: 405
    assert r.status_code == 405 and r.json() == {"detail": "METHOD_NOT_ALLOWED"} and r.headers.get("allow")
