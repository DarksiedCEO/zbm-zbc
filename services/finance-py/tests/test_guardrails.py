"""Spec §F guardrails G1-G9 (G6 and G7 also in test_reconcile_anchor.py / test_leak_fuzz.py), config refusals, and the
thin clients' negative mappings (A14)."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import time
from pathlib import Path

import httpx
import pytest

import config as config_mod
import ports
import reasons as R
from helpers import ANDRE_TOKEN, Harness, base_env, rid

SRC = Path(__file__).resolve().parents[1] / "src"
ROOT = Path(__file__).resolve().parents[1]


def _src_files():
    return sorted(SRC.rglob("*.py"))


# --- G1 no float in any money path -----------------------------------------------------------------------------------

def test_g1_no_float_in_money_paths():
    allowed = {"serve.py", "clients.py", "clock.py", "api.py", "ledger.py"}  # timeouts, switch intervals; never money
    for f in _src_files():
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "float":
                assert f.name in allowed, f"float() in {f}"
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                assert f.name in allowed, f"float literal {node.value} in {f}:{node.lineno}"
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Decimal" and node.args and \
                    isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, float):
                raise AssertionError(f"Decimal(float) in {f}")


# --- G2 no LLM SDK, no network in tests ---------------------------------------------------------------------------------

def test_g2_no_llm_sdk_imported():
    banned = re.compile(r"^\s*(import|from)\s+(anthropic|openai|google\.generativeai|langchain|llama|transformers|"
                        r"cohere|mistralai)\b", re.M)
    for f in _src_files():
        assert not banned.search(f.read_text()), f


def test_g2_tests_have_no_network():
    import socket
    with pytest.raises(RuntimeError, match="network"):
        socket.create_connection(("example.com", 443))


# --- G3 stand-ins answer unavailable; no passing fake importable from src ------------------------------------------------

def test_g3_every_stand_in_is_negative_and_no_fake_in_src():
    p = ports.Ports()
    assert not p.vi.certification("s").available and not p.vi.clawbacks(0).available
    c = p.compliance
    assert not c.ruling("r").available and not c.holds(()).available and not c.sanctions_status("s", "payee").available
    assert not c.row("HR-12").available and not c.jurisdiction("US", None).available
    assert not p.cn.tier("c", "k").available and p.cn.notify("t", "k", "r") is False
    assert not p.legal.document_status("d", 1, "a" * 64, "a").available
    for r in p.rails.values():
        assert not r.create_account("p", "US").available and not r.account_status("a").available
        assert r.submit("k", "a", "1.00", "i").outcome == "unavailable" and not r.lookup("k", "i").available
        assert not r.balance().available and r.verify_event({}, "sig") is False
    assert not p.bank.balance("zbc", "1020").available and p.bank.transfer("zbc", "1020", "1010", "1.00", "k").outcome == "unavailable"
    assert not p.tax.status("p").available and p.tax.status("p").tin_match == "unavailable"
    assert not p.gl.trial_balance("zbc", "2026-09").available
    assert p.vault.identity_hmac_key() is None and p.vault.contact_ref_valid("vault:x") is None
    assert p.people.second_approver_active() is None and p.push.push("k", {}) is False
    for f in _src_files():
        assert not re.search(r"^class Fake", f.read_text(), re.M), f


def test_g3_day_one_nothing_moves():
    x = Harness(passing=False).ready()
    assert not x.ok(x.handoff())["allowed"]
    assert not x.ok(x.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "p", "kind": "clipper",
                                               "declared_country": "US"}, caller="onboarding"))["allowed"]
    r = x.ok(x.post("/fin/v1/reconciliations/run", {"request_id": rid()}, caller="scheduler"))
    assert not r["fc01"] and all(l["status"] in ("source_unavailable", "matched", "not_in_use") for l in r["recon"]["legs"])
    r = x.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler")
    assert r.status_code == 409


# --- G4 every refusal cites a rule in force ------------------------------------------------------------------------------

def test_g4_every_cited_rule_exists_in_the_seed():
    seed = {r["rule_id"] for r in json.loads((ROOT / "seed" / "fin_rules_seed.json").read_text())["rows"]}
    assert set(R.CATALOG.values()) <= seed
    for f in _src_files():
        for rule in re.findall(r'rule="(FIN-[A-Z0-9-]+)"', f.read_text()):
            assert rule in seed, (f, rule)
        for cq in re.findall(r'"(FIN-CQ-[0-9]{2})"', f.read_text()):
            assert cq in seed, (f, cq)


def test_g4_refusals_from_a_battery_cite_rules_in_force(hr):
    rules = hr.svc.current.by_id()
    hr.fund_campaign(budget="10.00")
    hr.payee()
    bodies = [hr.ok(hr.handoff()), hr.ok(hr.handoff("s2", status="pending"))]
    hr.recon()
    r = hr.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": "stripe"}, caller="scheduler").json()
    bodies.append(r)
    seen = 0
    for b in bodies:
        for x in b.get("reasons") or []:
            assert x["rule_id"] in rules, x
            seen += 1
        for e in b.get("excluded") or []:
            for x in e["reasons"]:
                assert x["rule_id"] in rules, x
                seen += 1
    assert seen >= 3


# --- G5 seed hash pinned ------------------------------------------------------------------------------------------------

def test_g5_seed_hash_is_pinned_in_config_and_adr():
    sha = hashlib.sha256((ROOT / "seed" / "fin_rules_seed.json").read_bytes()).hexdigest()
    assert sha == config_mod.PINNED_SEED_SHA256
    adr = ROOT.parents[1] / "docs" / "adr" / "0009-finance-department-architecture.md"
    assert sha in adr.read_text()


def test_g5_changed_seed_refuses_start_and_unpinned_is_marked(tmp_path):
    p = tmp_path / "seed.json"
    raw = (ROOT / "seed" / "fin_rules_seed.json").read_bytes().replace(b"Owner", b"Owner")
    doc = json.loads(raw)
    doc["rows"][0]["title"] = "Changed title"
    p.write_text(json.dumps(doc))
    with pytest.raises(RuntimeError, match="does not match"):
        Harness(env={"FIN_SEED_PATH": str(p)})
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="pinned"):
        config_mod.load(base_env(FIN_SEED_SHA256=sha))
    x = Harness(env={"FIN_SEED_PATH": str(p), "FIN_SEED_SHA256": sha, "FIN_ALLOW_UNPINNED_SEED": "1"})
    h = x.ok(x.client.get("/health"))
    assert h["rules_pinned"] is False and h["production"] is False


# --- G8 banned words ------------------------------------------------------------------------------------------------------

def test_g8_no_banned_custody_word_in_any_account_title_or_line_code():
    import chart
    from intelligences import i02_receivables as I2
    from textguard import banned_words_in
    for ent, c in chart.CHARTS.items():
        for code, (title, _t, _k) in c.items():
            assert not banned_words_in(title), (ent, code, title)
    assert chart.DEPOSITS_ACCOUNT_TITLE.endswith("ZBC Client Campaign Deposits")
    for codes in I2.LINE_CODES.values():
        for code in codes:
            assert not banned_words_in(code.replace("_", " "))
            assert code not in ("surcharge", "card_fee", "convenience_fee", "processing_fee")


# --- G9 books balance at every test end (conftest fixture) ------------------------------------------------------------------

def test_g9_the_balance_check_fixture_is_armed():
    from conftest import HARNESSES
    x = Harness()
    assert x in HARNESSES


# --- config: refuse to start on anything it cannot honour ----------------------------------------------------------------

@pytest.mark.parametrize("over", [
    {"FIN_SERVICE_TOKEN": "__unset__"}, {"FIN_SERVICE_TOKEN": "short"},
    {"FIN_CUSTODY_MODEL": "fbo"}, {"FIN_CUSTODY_MODEL": "escrow_agent"}, {"FIN_REVENUE_MODEL": "agent"},
    {"FIN_STRIPE_LOSSES": "platform"}, {"FIN_RAILS": "stripe,ach_direct"}, {"FIN_RAIL_STRIPE": "sk_live_x"},
    {"FIN_BANK_FEED": "plaid"}, {"FIN_TAX_AGENT": "trolley"}, {"FIN_VAULT": "hashicorp"}, {"FIN_GL": "xero"},
    {"FIN_RAIL_REVERSAL_ENABLED": "1"}, {"FIN_CARD_PREPAYMENTS": "1"}, {"FIN_LATE_FEES": "1"},
    {"FIN_REFUND_ADMIN_FEE_PCT": "5"}, {"FIN_RESERVE_PCT": "10"}, {"FIN_OFAC_MAX_AGE_DAYS": "3"},
    {"FIN_RELEASE_DELAY_H": "48", "FIN_APPROVAL_TTL_H": "24"}, {"FIN_MIN_PAYOUT": "10"}, {"FIN_MIN_PAYOUT": "0.00"},
    {"FIN_ACCESS_REVIEW_DAYS": "365"}, {"FIN_CLAWBACK_WRITEOFF_MIN_DAYS": "30"}, {"FIN_IDENTITY_HMAC_KEY": "k"},
    {"FIN_VI_URL": "http://127.0.0.1:1"}, {"FIN_COMPLIANCE_URL": "http://x", "FIN_COMPLIANCE_TOKEN": "t"},
    {"FIN_CN_URL": "http://x"}, {"FIN_LEGAL_URL": "http://x"}, {"FIN_UNMATCHED_TIN_POLICY": "pay"},
    {"FIN_PAYEE_CHANGE_COOLING_OFF_H": "1"}, {"FIN_RUN_WEEKDAY": "FUNDAY"}, {"FIN_EVIDENCE_RETENTION_DAYS": "30"},
])
def test_config_refuses_what_it_cannot_honour(over):
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(**over))


def test_bind_default_is_loopback_and_port():
    import api
    src = (SRC / "api.py").read_text()
    assert 'os.environ.get("FIN_BIND_ADDR", "127.0.0.1")' in src and '"FIN_PORT", "8410"' in src
    assert api.app.docs_url is None and api.app.openapi_url is None


# --- thin clients (A14: any failure maps to the negative answer) ----------------------------------------------------------

def _client(cls, handler, timeout=10.0):
    return cls("http://peer.invalid", "svc-token", "caller-token", transport=httpx.MockTransport(handler), timeout=timeout)


def test_thin_clients_map_good_answers():
    from clients import HttpCompliance, HttpVerification

    def vi(req):
        if req.url.path.endswith("/certification"):
            return httpx.Response(200, json={"submission_id": "s1", "certification_id": "vi-cert-1", "status": "certified",
                                             "certified_views": 100, "campaign_id": "c", "clipper_id": "k",
                                             "platform": "tiktok", "window": {"create_time": "2026-09-20T00:00:00Z"},
                                             "certified_at": "2026-10-01T00:00:00Z", "reasons": [], "rules_version": 1})
        return httpx.Response(200, json={"items": [{"clawback_id": "c1", "certification_id": "vi-cert-1",
                                                    "views_delta": -5, "cause": "x", "rule_id": "VI-05", "seq": 3}],
                                         "next_cursor": None})
    v = _client(HttpVerification, vi)
    c = v.certification("s1")
    assert c.available and c.certified_views == 100 and c.create_time == "2026-09-20T00:00:00Z"
    assert v.clawbacks(0).items[0]["views_delta"] == -5

    def cmp_(req):
        if "/rulings/" in req.url.path:
            return httpx.Response(200, json={"ruling_id": "r1", "gate": "payout", "subject_id": "s1", "allowed": True,
                                             "evaluated_at": "2026-10-01T00:00:00Z", "register_version": 3})
        if req.url.path.endswith("/holds"):
            return httpx.Response(200, json=[{"hold_id": "h1", "subject_id": "k", "status": "open"},
                                             {"hold_id": "h2", "subject_id": "zz", "status": "open"}])
        return httpx.Response(200, json={"register_version": 3, "row": {"id": "HR-12", "effective_status": "verified",
                                                                         "expires_at": None}})
    k = _client(HttpCompliance, cmp_)
    assert k.ruling("r1").allowed and k.holds((("payee", "k"),)).open_hold_ids == ("h1",)
    assert k.row("HR-12").effective_status == "verified"
    assert not k.sanctions_status("k", "payee").available and not k.jurisdiction("US", None).available


@pytest.mark.parametrize("resp", ["500", "404", "garbage", "wrong_subject", "encoded", "too_large", "raise"])
def test_thin_clients_fail_negative(resp):
    from clients import HttpCompliance, HttpVerification

    def handler(req):
        if resp == "500":
            return httpx.Response(500)
        if resp == "404":
            return httpx.Response(404)
        if resp == "garbage":
            return httpx.Response(200, content=b"{not json")
        if resp == "encoded":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"x")
        if resp == "too_large":
            return httpx.Response(200, content=b"[" + b"1," * 600000 + b"1]")
        if resp == "raise":
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"submission_id": "other", "ruling_id": "other", "certification_id": "x",
                                         "status": "certified", "allowed": True, "gate": "payout", "subject_id": "o",
                                         "campaign_id": "c", "clipper_id": "k", "row": {"id": "OTHER"},
                                         "register_version": 1})
    assert not _client(HttpVerification, handler).certification("s1").available
    assert not _client(HttpCompliance, handler).ruling("r1").available
    assert not _client(HttpCompliance, handler).row("HR-12").available


def test_thin_client_wall_clock_budget():
    from clients import HttpVerification

    def slow(req):
        time.sleep(3)
        return httpx.Response(200, json={})
    t = time.monotonic()
    assert not _client(HttpVerification, slow, timeout=0.5).certification("s1").available
    assert time.monotonic() - t < 2.0
