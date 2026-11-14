"""A-22 (code stability and isolation, O-03), A-23 (vault unavailable), A-24 (kill switch mid-run), A-25 (cross-tenant
access incl. the evidence-map display)."""

from __future__ import annotations

import json
import re

import pytest

from cert_web import ORG_LD, Web, page, site
from common import check_ran, rng
from gen import plants
from harness.service import Svc
from helpers import rid

CODE = re.compile(r"\{c:[a-z]+:[0-9a-f]{16}\}")


def _site(m):
    web = site(Web())
    web.route("/", page(canonical=f"http://{m}.test/x", ld=ORG_LD))
    return web


def test_A_22_O_03_codes_stable_within_a_tenant_and_distinct_across_tenants(tmp_path):
    m = plants.uniq(rng(7, "a22"))
    s = Svc(tmp_path, _site(m))
    try:
        s.h.tenant("acme", kind="own", domains=("other.test",))
        s.web.route("/", page(canonical=f"http://{m}.test/x", ld=ORG_LD), host="other.test")
        s.web.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain", host="other.test")
        a1 = s.audit()
        a2 = s.audit()
        b1 = s.audit(tenant="acme", domain="other.test")
        c1, c2, cb = (set(CODE.findall(json.dumps(x["report"]))) for x in (a1, a2, b1))
        assert c1 and c1 == c2
        assert not c1 & cb
        d = s.h.ok(s.get(f"/tenants/zbm/audits/{a2['audit_id']}/drift?against={a1['audit_id']}"))
        assert d["changes"] == [] or all(c["class"] != "REAL_CHANGE" for c in d["changes"])
        assert a1["report"]["codes_key_id"] == a2["report"]["codes_key_id"]
    finally:
        s.close()
    check_ran("A-22", 3)


def test_A_23_no_key_refused_before_any_fetch(tmp_path):
    web = site(Web())
    s = Svc(tmp_path, web, tenant=None, SEO_VAULT_KEY_FILE=None)
    try:
        s.h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True)
        r = s.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                           "paths": ["/"]})
        assert r.status_code in (409, 503) and "VAULT_UNAVAILABLE" in r.text
        assert web.requests == []
    finally:
        s.close()


def test_A_23_evidence_blob_lost_owner_display_not_available(tmp_path):
    m = plants.uniq(rng(8, "a23"))
    s = Svc(tmp_path, _site(m))
    try:
        a = s.audit()
        s.h.svc.audits[a["audit_id"]]["evidence_ref"] = "seo-vlt-" + "0" * 40   # the evidence blob is lost
        r = s.audit_resolved(a["audit_id"])
        assert r.status_code == 200 and r.json().get("evidence_available") is False
        assert m not in r.text and "[not available]" in r.text
    finally:
        s.close()
    check_ran("A-23", 4)


@pytest.mark.parametrize("switch", ["global", "write", "tenant:zbm", "capability:audit", "capability:fetch",
                                    "provider:web", "agent:selene", "agent:delia"])
def test_A_24_kill_switch_before_run_no_request(tmp_path, switch):
    web = site(Web())
    s = Svc(tmp_path, web)
    try:
        s.h.switch(switch, andre=True)
        r = s.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                           "paths": ["/"]})
        assert r.status_code != 500
        if switch in ("global", "write", "tenant:zbm", "capability:audit", "capability:fetch", "provider:web"):
            assert web.requests == [], (switch, web.requests)
    finally:
        s.close()
    check_ran("A-24", 8)


ROUTES = ["/tenants/{t}/audits/{a}", "/tenants/{t}/audits/{a}?resolve=evidence", "/tenants/{t}/audits",
          "/tenants/{t}/audits/{a}/drift?against={a}", "/tenants/{t}/log-ingests", "/tenants/{t}/audits/{a}/client-reports"]


def test_A_25_cross_tenant_every_route_and_the_evidence_display(tmp_path):
    m = plants.uniq(rng(9, "a25"))
    s = Svc(tmp_path, _site(m))
    ran = 0
    try:
        s.h.tenant("acme", domains=())
        a = s.audit()
        for route in ROUTES:
            mine = route.format(t="zbm", a=a["audit_id"])
            other = route.format(t="acme", a=a["audit_id"])
            for caller, tenant in (("hub", "acme"), ("hub", None), ("compliance_38", None), ("finance_31", None),
                                   ("seo_agent", None), ("dashboard", None), ("hub", "globex")):
                for path in (other, mine):
                    r = s.h.get(path, caller=caller, tenant=tenant)
                    if path == mine and caller == "hub" and tenant != "zbm":
                        assert r.status_code in (401, 403, 404), (path, caller, tenant, r.status_code)
                    if "resolve=evidence" in path and not (caller == "hub" and tenant == "zbm"):
                        assert m not in r.text, (path, caller, tenant)
                    assert r.status_code != 500
                    ran += 1
        ok = s.audit_resolved(a["audit_id"])
        assert ok.status_code == 200 and m in ok.text            # D-06: the owner's own evidence resolves
    finally:
        s.close()
    check_ran("A-25", ran)
