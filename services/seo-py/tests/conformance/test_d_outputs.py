"""D-01 (the allowlist), D-02a-m (per output), D-03 (hashes), D-04 (never stored), D-07 (availability)."""

from __future__ import annotations

import json
import logging
import re

import pytest

from cert_web import ORG_LD, Web, page, site
from common import rng
from gen import plants
from harness.service import Svc, clf

CODE = re.compile(r"\{c:[a-z]+:[0-9a-f]{16}\}")


@pytest.fixture
def svc(tmp_path):
    m = plants.uniq(rng(3, "dout"))
    web = site(Web(), robots=f"User-agent: *\nDisallow: /{m}\nSitemap: http://site.test/sitemap.xml\n")
    web.route("/", page(canonical=f"http://{m}.test/a", ld=ORG_LD))
    s = Svc(tmp_path, web)
    s.m = m
    yield s
    s.close()


def test_D_01_allowlist_readable_and_nothing_else(svc):
    a = svc.audit()
    rep = json.dumps(a["report"])
    assert "site.test" in rep and "robots.txt" in rep and "sitemap.xml" in rep
    assert svc.m not in rep
    from primitives import codes
    assert codes.rule_readable("/") and codes.rule_readable("/*") and codes.rule_readable("/$")
    assert codes.rule_readable("/robots.txt") and not codes.rule_readable("/private")


def test_D_02a_b_log_and_ledger_hold_no_code_and_no_crawled_value(svc):
    svc.audit()
    svc.log_ingest([clf("192.0.2.5", "/" + svc.m)])
    data = svc.tmp / "data" / "seo_log.jsonl"
    text = data.read_text(encoding="utf-8") if data.exists() else ""
    assert svc.m not in text and not CODE.search(text)
    assert svc.m not in json.dumps(svc.h.ledger.events, default=str)


def test_D_02c_python_logging_every_level(svc):
    logging.getLogger("httpx").debug("probe %s", "x")
    svc.audit()
    assert not [x for x in svc.cap.lines if svc.m in x]


def test_D_02e_audit_view_is_d01_only(svc):
    a = svc.audit()
    got = svc.h.ok(svc.get(f"/tenants/zbm/audits/{a['audit_id']}"))
    assert svc.m not in json.dumps(got) and CODE.search(json.dumps(got))
    assert got["report"]["codes_key_id"]


def test_D_02h_resolve_only_for_the_owner_tenant(svc):
    a = svc.audit()
    for caller, tenant in (("compliance_38", None), ("seo_agent", None), ("finance_31", None)):
        r = svc.h.get(f"/tenants/zbm/audits/{a['audit_id']}?resolve=evidence", caller=caller, tenant=tenant)
        assert svc.m not in r.text
    ok = svc.audit_resolved(a["audit_id"])
    assert ok.status_code == 200 and svc.m in ok.text


def test_D_02l_error_bodies_echo_nothing(svc):
    r = svc.post("/tenants/zbm/audits", {"request_id": "r-1", "domain": svc.m + ".test", "scheme": "http",
                                         "paths": ["/"]})
    assert r.status_code >= 400 and svc.m not in r.text


def test_D_02m_no_planted_value_sent_to_another_host(svc):
    svc.audit()
    assert not [(h, p) for h, p in svc.web.requests if h != "site.test" and svc.m in p]


def test_D_03_plain_sha256_only_inside_the_report(svc):
    svc.audit()
    data = svc.tmp / "data" / "seo_log.jsonl"
    text = data.read_text(encoding="utf-8") if data.exists() else ""
    assert '"text_sha256"' not in text and '"fingerprint"' not in text


def test_D_04_ip_ua_referer_never_stored_even_coded(svc):
    r = rng(4, "d04")
    ip = plants.shape_value("ipv4", r)
    ref = plants.uniq(r)
    ua = "Googlebot/2.1 " + plants.uniq(r)
    svc.log_ingest([clf(ip, "/", ua=ua, ref=f"http://{ref}.test/")])
    out = svc.stored_outputs()
    assert ip not in out and ref not in out and ua.split()[-1] not in out
    assert "{c:ip:" not in out and "{c:ua:" not in out and "{c:ref:" not in out


def test_D_07_no_sink_refusal_in_a_legitimate_audit(svc):
    a = svc.audit()
    assert a["status"] == "completed"
    assert not [x for x in svc.cap.lines if "refused by the sink guard" in x]


def test_D_02e_the_owner_masker_never_hides_a_store_code():
    """D-02e: the owner view masks crawled-looking text, never a store code (a machine value): a code whose hex is
    all digits stays as it is; text around it is still masked (found: about 2 in 10,000 random codes were hidden
    as a phone number)."""
    from svc_vaulted import owner_view
    r = rng(23, "d02e")
    for kind in ("seg", "host", "path", "text"):
        for _ in range(200):
            c = "{c:%s:%s}" % (kind, "".join(r.choice("0123456789") for _ in range(16)))
            assert owner_view("/" + c + "/{token}") == "/" + c + "/{token}"
            assert owner_view({c: [c]}) == {c: [c]}
    assert owner_view("call 555 010 0199 about {c:seg:" + "1" * 16 + "}").startswith("[hidden")
