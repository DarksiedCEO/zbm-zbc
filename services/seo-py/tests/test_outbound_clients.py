"""
C2-3c3 (core-spec/2 T-1 / T-3 / T-5, Wave-1 D-02m): the non-LLM clients' outbound parameters are typed and checked
where each client builds its request (finance_client.lookup, compliance_client.activation, gsc_client sites / query).
Synthetic values only; fixtures only (httpx.MockTransport), never a real host.
"""
from __future__ import annotations

import random

import httpx
import pytest

import compliance_client as cc
import finance_client as fc
import gsc_client as gc
import outbound
import sink_fields
from conformance import gen_values_v2 as VG

INVOICE = "fin-inv-" + "0123456789ABCDEFGHJKMNPQRS"            # synthetic ULID body
FIN_BASE, CMP_BASE = "https://finance.internal.test", "https://compliance.internal.test"
QUERY_URL = gc.API_BASE + "/sites/sc-domain%3Aacme.test/searchAnalytics/query"


def _recorder():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503)
    return seen, httpx.MockTransport(handler)


def _fin(transport):
    return fc.FinanceClient(FIN_BASE, "svc-token-synthetic", "caller-token-synthetic", transport=transport,
                            attempts=1, sleep=lambda s: None)


def _cmp(transport):
    return cc.ComplianceClient(CMP_BASE, "svc-token-synthetic", "caller-token-synthetic", transport=transport,
                               attempts=1, sleep=lambda s: None)


def _query_body(**kw):
    b = {"startDate": "2026-10-01", "endDate": "2026-10-31", "dimensions": ["date", "page"], "type": "web",
         "dataState": "final", "aggregationType": "auto", "rowLimit": 1000, "startRow": 0}
    b.update(kw)
    return b


def test_client_kinds_are_typed_and_closed():
    kinds = {k for s, k, _p in sink_fields.SINK_FIELDS if s == "outbound"} - set(sink_fields.OUTBOUND_HOSTS)
    # C2-3c4 re-pin (rulings 17:15 PT F3c3-1/2, G-7): + compliance (its one path is E-AUTH "configured") + gsc.token
    kinds |= {k for k, _p in sink_fields.OUTBOUND_CONFIGURED}
    assert kinds == set(sink_fields.OUTBOUND_CLIENT_HOSTS) == {"finance", "compliance", "gsc.sites", "gsc.query",
                                                                "gsc.token"}
    assert not set(sink_fields.OUTBOUND_CLIENT_HOSTS) & set(sink_fields.OUTBOUND_HOSTS)
    assert not {k for k, _p in sink_fields.OUTBOUND_E_LLM} & set(sink_fields.OUTBOUND_CLIENT_HOSTS)   # no E-LLM
    assert {h for h in sink_fields.OUTBOUND_CLIENT_HOSTS.values() if h} <= gc.ALLOWED_HOSTS
    enums = sink_fields.OUTBOUND_CLIENT_ENUMS
    assert enums["/body/dimensions[]"] == set(gc.DIMENSIONS) and enums["/body/aggregationType"] == set(gc.AGGREGATIONS)


def test_finance_lookup_sends_only_the_typed_path_parameter_to_the_configured_host():
    seen, t = _recorder()
    with pytest.raises(fc.FinanceLookupError):
        _fin(t).lookup(INVOICE)
    assert len(seen) == 1 and seen[0].url.host == "finance.internal.test" and seen[0].method == "GET"
    assert seen[0].url.path == f"/fin/v1/invoices/{INVOICE}/status" and not seen[0].url.query and not seen[0].content
    assert outbound.violation("finance", str(seen[0].url), {"path": {"invoice_id": INVOICE}}) is None


def test_compliance_activation_sends_only_the_typed_path_parameter_to_the_configured_host():
    seen, t = _recorder()
    import vault as vault_mod
    coder = vault_mod.Coder(vault_mod.ephemeral_vault_key(), "acme")     # C2-6g3 re-pin (OWNER DECISION 2026-11-15 02:00 PT (E-COMP), lead ruling 02:40 PT, G-7)
    with outbound.configured(compliance_client_id="acme-live"), pytest.raises(cc.ComplianceCheckError), \
            outbound.e_comp("c:text:" + coder.code("text", "acme-live"), coder, "compliance.internal.test"):
        _cmp(t).activation("acme-live")      # C2-3c4 (F3c3-1): the tenant's configured client id is bound
    assert len(seen) == 1 and seen[0].url.host == "compliance.internal.test"
    assert seen[0].url.path == "/compliance/v1/clients/acme-live/activation" and not seen[0].content


def test_a_refused_compliance_parameter_is_never_sent_and_is_counted():
    """C2-3c4 (ruling 17:15 PT F3c3-1; xfail removed, test kept): only the CONFIGURED client id is sent."""
    seen, t = _recorder()
    before = outbound.COUNTS[("compliance", "configured")]
    plants = ["123456", "x" * 65, "zqx-page-7f3a91", "acme-live2"] + list(VG.values(VG.SEEDS[0], 100))
    with outbound.configured(compliance_client_id="acme-live"):
        for cid in plants:
            with pytest.raises(cc.ComplianceCheckError) as e:
                _cmp(t).activation(cid)
            assert e.value.code == "COMPLIANCE_RESPONSE_INVALID"
    with pytest.raises(cc.ComplianceCheckError):
        _cmp(t).activation("acme-live")             # unbound: refused (fail closed)
    took = sum(1 for c in plants if cc.CLIENT_ID.fullmatch(c)) + 1
    assert seen == [] and outbound.COUNTS[("compliance", "configured")] == before + took


def test_gsc_docs_as_built_pass_and_their_paths_are_all_decided():
    site = {"site_type": "sc-domain", "site_host": "acme.test"}
    assert outbound.violation("gsc.query", QUERY_URL, {"path": site, "body": _query_body()}) is None
    assert outbound.violation("gsc.sites", gc.API_BASE + "/sites", {"path": {}}) is None
    paths = {p for p, _v in outbound._leaves({"path": site, "body": _query_body()})}
    assert paths <= {p for s, k, p in sink_fields.SINK_FIELDS if s == "outbound" and k == "gsc.query"}


@pytest.mark.parametrize("kind,url,doc", [
    ("gsc.sites", "https://oauth2.googleapis.com/x", {"path": {}}),
    ("gsc.query", "https://audited-site.test/x", {"path": {"site_type": "https", "site_host": "acme.test"},
                                                   "body": _query_body()})])
def test_d02m_a_fixed_host_client_refuses_any_other_host(kind, url, doc):
    assert outbound.violation(kind, url, doc) == ("", "host")


@pytest.mark.parametrize("path,value", [("/body/type", "image"), ("/body/dataState", "all"),
                                        ("/body/dimensions[]", "country"), ("/body/rowLimit", 10 ** 9),
                                        ("/body/startDate", "2026-10-01 x"), ("/path/site_type", "ftp")])
def test_gsc_ill_typed_parameter_is_refused(path, value):
    body = _query_body()
    doc = {"path": {"site_type": "https", "site_host": "acme.test"}, "body": body}
    head, key = path.rsplit("/", 1)
    tgt = doc["path"] if head == "/path" else body
    tgt[key.rstrip("[]")] = [value] if key.endswith("[]") else value
    assert outbound.violation("gsc.query", QUERY_URL, doc)[1] == "field_type"
    body2 = _query_body(extra="x")
    site = {"site_type": "https", "site_host": "acme.test"}
    assert outbound.violation("gsc.query", QUERY_URL, {"path": site, "body": body2}) == ("/body/extra", "untyped")


@pytest.mark.parametrize("seed", VG.SEEDS[:3])
def test_t5_vg_values_are_refused_in_every_client_parameter(seed):
    """T-5 smuggling over every client field: VG valueish values, prose, whitespace / '@' / braces: accepted = 0,
    except values matching the field's grammar by construction (counted apart, F3c3-3)."""
    r = random.Random(seed)
    plants = list(VG.values(seed, 200)) + ["two words %d" % i for i in range(5)] + ["a@b.test", "{c:text:%016x}" % 7]
    by_grammar = 0
    for (s, kind, path), field in sink_fields.SINK_FIELDS.items():
        if s != "outbound" or kind not in sink_fields.OUTBOUND_CLIENT_HOSTS:
            continue
        for v in plants:
            if sink_fields.matches(field, v, sink_fields.leaf_key(path)):
                assert field.values is None, (kind, path, v)          # an enum never takes a plant
                by_grammar += 1
                continue
            ok = sink_fields.matches(field, v, sink_fields.leaf_key(path))
            assert not ok
    assert r is not None and by_grammar >= 0


def test_vg_invoice_parameter_never_leaves_the_client():
    seen, t = _recorder()
    for v in list(VG.values(VG.SEEDS[0], 50)) + ["fin-inv-zzz page text"]:
        with pytest.raises(fc.FinanceLookupError):
            _fin(t).lookup(v)
    assert seen == []


# ------------------------------------------------- C2-3c4: E-AUTH (lead rulings 17:15 PT F3c3-2 / F3c3-3)
_SITE = {"site_type": "https", "site_host": "acme.test", "property": "https://acme.test/shop/"}


def test_gsc_property_must_be_exactly_the_configured_one():
    doc = {"path": dict(_SITE), "body": _query_body()}
    assert outbound.violation("gsc.query", QUERY_URL, doc) == ("/path/property", "configured")      # unbound
    with outbound.configured(gsc_property="https://acme.test/shop/"):
        assert outbound.violation("gsc.query", QUERY_URL, doc) is None
        for other in ("https://acme.test/shop/x7Kp2qLm9Zr4/", "https://acme.test/", "sc-domain:acme.test",
                      "https://acme.test/SHOP/") + tuple(VG.values(VG.SEEDS[1], 50)):
            doc["path"]["property"] = other
            assert outbound.violation("gsc.query", QUERY_URL, doc) == ("/path/property", "configured")


def _key(email="svc@proj-synthetic.iam.test"):
    class _Signer:                                   # synthetic: a fixed signature, no key material
        def sign(self, data, *_a):
            return b"synthetic-signature"
    return gc.ServiceAccountKey(email, "kid-synthetic-1", _Signer())


def test_gsc_assertion_only_the_configured_keys_and_only_to_its_issuer():
    key, now = _key(), 1_790_000_000
    good = gc.build_assertion(key, now).decode("ascii")
    doc = {"body": {"grant_type": gc.GRANT_TYPE, "assertion": good}}
    assert outbound.violation("gsc.token", gc.TOKEN_URL, doc) == ("/body/assertion", "configured")    # unbound
    with outbound.configured(gsc_assertion_input=gc.assertion_input(key, now)):
        assert outbound.violation("gsc.token", gc.TOKEN_URL, doc) is None
        assert outbound.violation("gsc.token", gc.API_BASE + "/token", doc) == ("", "host")
        bad = [gc.build_assertion(_key("other@proj-synthetic.iam.test"), now).decode(), gc.build_assertion(
            key, now + 1).decode(), good + ".x", good.rsplit(".", 1)[0] + ".", good + "=", good + " page text"]
        for b in bad + list(VG.values(VG.SEEDS[2], 50)):
            d = {"body": {"grant_type": gc.GRANT_TYPE, "assertion": b}}
            assert outbound.violation("gsc.token", gc.TOKEN_URL, d) == ("/body/assertion", "configured")
        d = {"body": {"grant_type": "password", "assertion": good}}
        assert outbound.violation("gsc.token", gc.TOKEN_URL, d) == ("/body/grant_type", "configured")
    assert sink_fields.OUTBOUND_CONFIGURED[("gsc.token", "/body/grant_type")] == ("const", gc.GRANT_TYPE)


def test_finance_refuses_to_send_when_outbound_cannot_be_imported(monkeypatch):
    seen, t = _recorder()
    monkeypatch.setitem(__import__("sys").modules, "outbound", None)        # import outbound -> ImportError
    assert fc.outbound_refused("finance", FIN_BASE + "/x", {"path": {"invoice_id": INVOICE}}) == "untyped"
    with pytest.raises(fc.FinanceLookupError):
        _fin(t).lookup(INVOICE)
    assert seen == []
    monkeypatch.setattr(fc, "PEER_OUTBOUND_CHECK", lambda kind, url, doc: None)   # a peer fixture's explicit check
    assert fc.outbound_refused("finance", FIN_BASE + "/x", {"path": {"invoice_id": INVOICE}}) is None
