"""AEGIS review of f5d9624..1472041: H1 (PII in URL paths), H2 (unbounded ingests), M1 (DNS verifier bounds), M2/M3
(restricted agents blocked everywhere; scorecards from recorded runs only), M4 (drift rule order), L1 (replay before
state checks), L2 (due slot computed after earlier audits). No wall-clock or memory assertions."""

from __future__ import annotations

import base64
import copy
import json
import os
import threading
import time

import pytest

from agents import drift
from agents import logs as logs_mod
from fixture_server import home_html, install_site
from helpers import rid
from ports import DnsBotVerifier
from test_audits import audit, harness, own

pytestmark = pytest.mark.local_http
GOOGLE_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
PII = ("SECRETTOKEN123", "jane.doe", "jane.doe%40example.com", "jane.doe@example.com", "550e8400-e29b-41d4-a716",
       "66.249.66.1", "203.0.113.77", "Googlebot/2.1", "hunter2pass", "4111111111111111")


def clf(ip, path, status=200, ua=GOOGLE_UA):
    return f'{ip} - - [10/Oct/2026:13:55:36 -0700] "GET {path} HTTP/1.1" {status} 2326 "-" "{ua}"'


def b64(lines) -> str:
    return base64.b64encode(("\n".join(lines) + "\n").encode()).decode()


def ingest(h, **kw):
    return h.post("/tenants/zbm/log-ingests", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                               "format": "combined", **kw})


def send(h, iid, seq, data, last=False):
    return h.post(f"/tenants/zbm/log-ingests/{iid}/chunks", {"request_id": rid(), "seq": seq, "data_b64": data,
                                                             "last": last})


# ---------------------------------------------------------------------------------------------- H1

@pytest.mark.parametrize("path,template", [
    ("/reset-password/SECRETTOKEN123", "/reset-password/{token}"),
    ("/users/jane.doe%40example.com/profile", "/users/{email}/profile"),
    ("/a/550e8400-e29b-41d4-a716-446655440000", "/a/{uuid}"),
    ("/orders/4111111111111111", "/orders/{number}"),
    ("/login?user=jane.doe@example.com&pw=hunter2pass", "/login"),
    ("/s/abcdefghijklmnopqrstuvwx", "/s/{token}"),
    ("/blog/how-to-rank", "/blog/how-to-rank"),
    ("/item-12345-blue", "/{token}"),                          # letters with digits: treated as an identifier
    ("/catalog/v2", "/catalog/v2"),
])
def test_h1_path_templates(path, template):
    assert logs_mod.template_path(path) == template


def test_h1_reviewer_pii_paths_appear_nowhere(tmp_path, srv):
    install_site(srv)
    data_dir = tmp_path / "data"
    h = harness(tmp_path, srv, data_dir=str(data_dir))
    own(h)
    h.ok(audit(h), 201)                                       # sitemap sample: / and /about
    iid = h.ok(ingest(h), 201)["ingest_id"]
    lines = [clf("66.249.66.1", "/reset-password/SECRETTOKEN123"),
             clf("203.0.113.77", "/users/jane.doe%40example.com/profile"),
             clf("66.249.66.1", "/a/550e8400-e29b-41d4-a716-446655440000"),
             clf("66.249.66.1", "/login?user=jane.doe@example.com&pw=hunter2pass"),
             clf("66.249.66.1", "/orders/4111111111111111"), clf("66.249.66.1", "/about"),
             "jane.doe@example.com SECRETTOKEN123 not a log line"]
    v = h.ok(send(h, iid, 1, b64(lines), last=True))
    done = h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))
    bad = h.post(f"/tenants/zbm/log-ingests/{iid}/chunks", {"request_id": rid(), "seq": 9,
                                                            "data_b64": b64(["SECRETTOKEN123 jane.doe"])})
    blobs = [json.dumps(v), json.dumps(done), bad.text, json.dumps(h.ledger.events),
             json.dumps(h.ok(h.get("/audit/export", caller="compliance_38"))),
             json.dumps(h.ok(h.get(f"/tenants/zbm/log-ingests/{iid}")))]
    for root, _, files in os.walk(data_dir):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                blobs.append(fh.read().decode("utf-8", "replace"))
    for s in PII:
        assert not any(s in b for b in blobs), s
    fam = done["report"]["envelope"]["facts"]["families"]["Googlebot"]
    assert {t["template"] for t in fam["top_path_templates"]} >= {"/reset-password/{token}", "/users/{email}/profile",
                                                                   "/orders/{number}", "/about"}
    assert fam["distinct_client_ips_estimate"] == 2
    assert done["report"]["envelope"]["facts"]["uncrawled_important_paths"] == ["/"]   # /about matched exactly


# ---------------------------------------------------------------------------------------------- H2

def test_h2_open_and_retained_ingest_caps(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv, SEO_LOG_MAX_OPEN_INGESTS="2", SEO_LOG_MAX_INGESTS="3")
    own(h)
    a = h.ok(ingest(h), 201)["ingest_id"]
    h.ok(ingest(h), 201)
    h.refused(ingest(h), 409, "LOG_INGESTS_OPEN_LIMIT")
    h.ok(send(h, a, 1, b64([clf("66.249.66.1", "/")]), last=True))
    h.ok(h.post(f"/tenants/zbm/log-ingests/{a}/finish", {"request_id": rid()}))
    h.ok(ingest(h), 201)
    h.refused(ingest(h), 409, "LOG_INGESTS_OPEN_LIMIT")
    h.clock.advance(days=91)                                   # outside the retention period: no longer counted
    for g in list(h.svc.log_ingests.values()):
        if g["status"] == "open":                              # an expired ingest takes nothing more
            h.refused(send(h, g["ingest_id"], 1, b64(["x"]), last=True), 409, "INGEST_CLOSED")
    for _ in range(2):
        iid = h.ok(ingest(h), 201)["ingest_id"]
        h.ok(send(h, iid, 1, b64(["x"]), last=True))
        h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))
    h.ok(ingest(h), 201)
    h.refused(ingest(h), 409, "LOG_INGESTS_LIMIT")


def test_h2_tenant_byte_quota(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv, SEO_LOG_TENANT_BYTES=str(1024 * 1024))
    own(h)
    data = b64([clf("1.2.3.4", "/" + "a" * 200, ua="Firefox")] * 250)
    seq, iid = 1, h.ok(ingest(h), 201)["ingest_id"]
    while True:
        r = send(h, iid, seq, data)
        if r.status_code != 200:
            break
        seq += 1
    h.refused(r, 409, "LOG_TENANT_QUOTA")
    assert sum(g["bytes"] for g in h.svc.log_ingests.values()) <= 1024 * 1024


def test_h2_per_ingest_state_is_bounded():
    lines = [clf("1.2.3.4", f"/page-{i}-x/section") for i in range(800)]
    d = logs_mod.process_chunk("combined", ("\n".join(lines) + "\n").encode(), b"k" * 32, lambda ip, t: "x", {},
                               {"left": 0})
    g = d["families"]["Googlebot"]
    assert len(g["templates"]) <= logs_mod.TEMPLATES_PER_FAMILY and len(g["hll"]) == logs_mod.HLL_M
    total = logs_mod.merge(logs_mod.empty_delta(), d)
    total = logs_mod.merge(total, d)
    assert len(total["families"]["Googlebot"]["templates"]) <= logs_mod.TEMPLATES_PER_FAMILY


# ---------------------------------------------------------------------------------------------- M1

def test_m1_non_global_ips_are_never_looked_up():
    calls = []
    v = DnsBotVerifier(rdns=lambda ip: calls.append(ip) or "x.googlebot.com", forward=lambda h: [])
    for ip in ("10.0.0.1", "192.168.1.1", "127.0.0.1", "::1", "fe80::1", "100.64.0.1"):
        assert v.verify(ip, "Googlebot") == "unverifiable"
    assert calls == []


def test_m1_global_lookup_cap():
    calls = []

    def rdns(ip):
        calls.append(ip)
        return "crawl.googlebot.com"
    v = DnsBotVerifier(rdns=rdns, forward=lambda h: ["66.249.66.1"], max_lookups=3)
    assert v.verify("66.249.66.1", "Googlebot") == "verified"         # 2 lookups
    assert v.verify("66.249.66.2", "Googlebot") == "error"            # rdns is the 3rd, forward refused
    assert v.verify("66.249.66.3", "Googlebot") == "error"            # cap reached: no call at all
    assert len(calls) == 2


def test_m1_busy_pool_never_queues_and_timeouts_spawn_no_threads():
    gate = threading.Event()

    def hang(ip):
        gate.wait(5)
        return "crawl.googlebot.com"
    v = DnsBotVerifier(rdns=hang, forward=lambda h: [], timeout_s=0.05)
    try:
        results = [v.verify(f"66.249.66.{i}", "Googlebot") for i in range(12)]
        assert set(results) == {"error"}
        workers = [t for t in threading.enumerate() if t.name.startswith("seo-dns")]
        assert len(workers) <= DnsBotVerifier.POOL_WORKERS
    finally:
        gate.set()
    for _ in range(200):                                              # the slots come back when lookups end
        if v.verify("10.0.0.1", "Googlebot") == "unverifiable" and DnsBotVerifier._slots._value == \
                DnsBotVerifier.POOL_WORKERS:
            break
        time.sleep(0.01)
    assert DnsBotVerifier._slots._value == DnsBotVerifier.POOL_WORKERS


# ---------------------------------------------------------------------------------------------- M2 / M3

def move(h, agent, to, expected):
    return h.ok(h.post(f"/agents/{agent}/lifecycle", {"request_id": rid(), "to": to, "expected_state": expected,
                                                      "reason": "FOUNDER_DECISION"}, caller="dashboard", andre=True))


def test_m2_restricted_osei_is_blocked_everywhere(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)
    a = h.ok(audit(h), 201)
    move(h, "osei", "restricted", "active")
    b = h.ok(audit(h), 201)
    o = next(e for e in b["report"]["agents"] if e["agent"] == "osei")
    assert o["outcome"] == "RESTRICTED" and b["report"]["data_hygiene"] is None
    h.refused(h.get(f"/tenants/zbm/audits/{b['audit_id']}/drift", params={"against": a["audit_id"]}), 403,
              "AGENT_RESTRICTED")
    sc = h.ok(h.get("/department"))["agents"]["osei"]["scorecard"]
    assert sc["runs"] == 2 and sc["outcomes"] == {"OK": 1, "RESTRICTED": 1} and sc["restricted_runs"] == 1


def test_m3_restricted_selene_refuses_upload_and_parsing(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)
    iid = h.ok(ingest(h), 201)["ingest_id"]
    move(h, "selene", "restricted", "active")
    h.refused(send(h, iid, 1, b64([clf("66.249.66.1", "/")])), 403, "AGENT_RESTRICTED")
    h.refused(ingest(h), 403, "AGENT_RESTRICTED")
    assert h.svc.log_ingests[iid]["totals"]["lines"] == 0


# ---------------------------------------------------------------------------------------------- M4

def test_m4_page_and_ruler_both_changed_is_unknown_never_real_change(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)
    a = h.ok(audit(h), 201)["report"]
    srv.html("site.test", "/", home_html(canonical=None))
    b = copy.deepcopy(h.ok(audit(h), 201)["report"])
    b["versions"]["schema_rules"] = "2099-01-01.1"
    c = next(x for x in drift.compare(a, b)["changes"] if x["code"] == "CANONICAL_MISSING")
    assert c["class"] == "UNKNOWN" and c["evidence"]["rule"] == "R3" and "canonicals" in c["evidence"]["changed_fields"]


def test_m4_robots_and_ruler_both_changed_is_unknown(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)
    a = h.ok(audit(h), 201)["report"]
    srv.text("site.test", "/robots.txt", "User-agent: GPTBot\nDisallow: /\n")
    b = copy.deepcopy(h.ok(audit(h), 201)["report"])
    b["versions"]["bot_families"] = "2099-01-01.1"
    c = next(x for x in drift.compare(a, b)["changes"] if x["code"] == "ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER")
    assert c["class"] == "UNKNOWN" and c["evidence"]["rule"] == "R3"


# ---------------------------------------------------------------------------------------------- L1, L2

def test_l1_agent_move_replay_answers_before_state_checks(tmp_path, srv):
    h = harness(tmp_path, srv)
    body = {"request_id": rid(), "to": "watch", "expected_state": "active", "reason": "QUALITY_REVIEW"}
    first = h.ok(h.post("/agents/roman/lifecycle", body, caller="dashboard"))
    again = h.ok(h.post("/agents/roman/lifecycle", body, caller="dashboard"))   # state is now "watch"
    assert first["state"] == again["state"] == "watch" and len(again["history"]) == 1


def test_l2_due_slot_is_computed_after_earlier_audits(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)
    for _ in range(2):
        h.ok(h.post("/tenants/zbm/schedules", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                               "paths": ["/"], "every_days": 1}), 201)
    advanced = []

    def home(handler):
        if not advanced:
            advanced.append(True)
            h.clock.advance(days=1)                     # the first scheduled audit takes "a day"
        handler._send(200, {"Content-Type": "text/html"}, home_html().encode())
    srv.routes[("site.test", "/")] = home
    h.ok(h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))
    slots = sorted(k for s in h.svc.schedules.values() for k in s["slots"])
    assert slots == ["0", "1"]


# ---------------------------------------------------------------------------------------------- AEGIS 4c0a805 T1, T4

@pytest.mark.parametrize("path,template", [
    ("/call/555-123-4567", "/call/{number}"),
    ("/call/(555)%20123%204567", "/call/{number}"),
    ("/ssn/123-45-6789", "/ssn/{number}"),
    ("/ssn/123.45.6789", "/ssn/{number}"),
    ("/x/123/45/6789", "/x/{number}/{number}/{number}"),
    ("/p/%EF%BC%95%EF%BC%95%EF%BC%95%EF%BC%91%EF%BC%92%EF%BC%93%EF%BC%94", "/p/{number}"),   # fullwidth digits
    ("/u/jane%EF%BC%A0example.com", "/u/{email}"),                                              # fullwidth at
    ("/u/jane%EF%B9%ABexample.com", "/u/{email}"),                                              # small at
    ("/u/jane(at)example.com", "/u/{email}"), ("/u/jane[AT]example.com", "/u/{email}"),
    ("/u/jane%2540example.com", "/u/{email}"), ("/u/jane%20at%20example%20dot%20com", "/u/{email}"),
    ("/files/jane-doe-resume.pdf", "/files/{file}.pdf"), ("/img/jane.smith.jpg", "/img/{file}.jpg"),
    ("/assets/site.css", "/assets/site.css"), ("/sitemap.xml", "/sitemap.xml"), ("/order/123456", "/order/{id}"),
])
def test_t1_pii_variants_are_templated(path, template):
    assert logs_mod.template_path(path) == template


T1_PII = ("555-123-4567", "123-45-6789", "jane", "resume", "smith")   # no bare digit runs: hashes contain them


def test_t1_variants_appear_nowhere(tmp_path, srv):
    install_site(srv)
    data_dir = tmp_path / "data"
    h = harness(tmp_path, srv, data_dir=str(data_dir))
    own(h)
    iid = h.ok(ingest(h), 201)["ingest_id"]
    paths = ["/call/555-123-4567", "/ssn/123-45-6789", "/u/jane%EF%BC%A0example.com", "/u/jane(at)example.com",
             "/files/jane-doe-resume.pdf", "/img/jane.smith.jpg", "/x/123/45/6789"]
    h.ok(send(h, iid, 1, b64([clf("66.249.66.1", p) for p in paths]), last=True))
    done = h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))
    blobs = [json.dumps(done), json.dumps(h.ledger.events),
             json.dumps(h.ok(h.get("/audit/export", caller="compliance_38")))]
    for root, _, files in os.walk(data_dir):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                blobs.append(fh.read().decode("utf-8", "replace"))
    for s in T1_PII:
        assert not any(s in b for b in blobs), s


def test_t4_expired_ingests_keep_counts_only_and_are_not_rebuilt(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv, data_dir=str(tmp_path / "data"))
    own(h)
    h.ok(audit(h), 201)
    iid = h.ok(ingest(h), 201)["ingest_id"]
    h.ok(send(h, iid, 1, b64([clf("66.249.66.1", f"/p/x{i}") for i in range(50)]), last=True))
    h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))
    g = h.svc.log_ingests[iid]
    assert "templates" in g["totals"]["families"]["Googlebot"] and g["report"] is not None
    h.clock.advance(days=91)
    v = h.ok(h.get(f"/tenants/zbm/log-ingests/{iid}"))
    assert v["status"] == "expired" and v["totals"]["families"]["Googlebot"]["requests"] == 50
    g = h.svc.log_ingests[iid]
    assert g["evicted"] and g["report"] is None and g["sitemap_sample"] is None
    assert set(g["totals"]["families"]["Googlebot"]) == {"requests", "verified", "spoofed", "claimed"}
    h2 = h.restart()                                            # replay on the same (advanced) clock
    g2 = h2.svc.log_ingests[iid]
    assert g2["evicted"] and g2["report"] is None and g2["totals"]["families"]["Googlebot"]["requests"] == 50
    assert set(g2["totals"]["families"]["Googlebot"]) == {"requests", "verified", "spoofed", "claimed"}
