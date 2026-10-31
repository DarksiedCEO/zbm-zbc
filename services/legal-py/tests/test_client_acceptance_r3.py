"""AEGIS r2 on ADR 0028 (aegis-client-acceptance-r2.md @591161b0): the Legal side.

- N-M1: a link token is self-authenticating: 32 random bytes + the first 16 bytes of HMAC-SHA256(LEGAL_LINK_TAG_KEY,
  "zbm-link-v1" | random), base64url (64 characters). The portal holds the same key (PORTAL_LINK_TAG_KEY) and refuses a
  token whose tag is wrong without asking Legal. Legal also refuses a bad tag before looking at its records, and
  issues no link while the key is not set.
- N-L1: a client legal name must be its own NFKC form (what a client can type round-trips to it).
- N-L2: a link names the latest approved fill of each client document for that client.
- N-L3: Default_Ignorable_Code_Point characters, the Hangul fillers and U+2800 are refused in typed names.
- N-L4: the receipt also needs the hash of the form signature the accepted submission carried.
- N-L5: the view carries Legal's time zone and its label.
- N-L8: a SOW whose stored fill values cannot be read fails the MSA-version rule closed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

import clickwrap_page as CP
import config as config_mod
import link_tag as LT
from helpers import ANDRE_TOKEN, CALLERS, LINK_TAG_KEY, Harness, rid
from test_client_acceptance_portal import (CLIENT, DISPLAY, SIGNATORY, accept, accept_body, determine_cq19, docs_of,
                                           founder_approve, invite, msa, resolve, sow_fill)


@pytest.fixture
def pair():
    x = Harness()
    x.approve_rules()
    determine_cq19(x)
    m, s = msa(x), sow_fill(x)
    return x, m, s, docs_of(m, s)


# --- N-M1 ----------------------------------------------------------------------------------------------------------

def test_a_link_token_carries_a_tag_under_the_shared_key(pair):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    raw = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4))
    assert len(tok) == 66 and len(raw) == 49 and raw[0] == LT.VERSION       # r3 N3-L4: a version byte leads
    want = hmac.new(LINK_TAG_KEY.encode(), b"zbm-link\x1f" + raw[:1] + b"\x1f" + raw[1:33], hashlib.sha256).digest()[:16]
    assert raw[33:] == want and LT.valid(tok, LINK_TAG_KEY.encode())
    forged = base64.urlsafe_b64encode(raw[:33] + bytes(16)).decode().rstrip("=")
    assert not LT.valid(forged, LINK_TAG_KEY.encode()) and not LT.valid("A" * 43, LINK_TAG_KEY.encode())


def test_legal_refuses_a_bad_tag_before_looking_at_its_records(pair, monkeypatch):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    raw = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4))
    forged = base64.urlsafe_b64encode(raw[:33] + bytes(16)).decode().rstrip("=")
    looked = []
    monkeypatch.setattr(x.svc, "_client_invitation_by_token", lambda t: looked.append(t))
    for path, body in (("/legal/v1/portal/invitations/resolve", {"token": forged}),
                       ("/legal/v1/portal/receipts", {"token": forged, "request_id": "pa-1", "form_sha256": "0" * 64})):
        r = x.post(path, body, caller="client_portal")
        assert r.status_code == 404 and r.json()["detail"] == "not found"     # r4 N4-L2: resolve adds "final"
    assert looked == []


def test_no_link_is_issued_without_the_tag_key():
    x = Harness(env={"LEGAL_LINK_TAG_KEY": "__unset__"})
    x.approve_rules()
    determine_cq19(x)
    m, s = msa(x), sow_fill(x)
    r = invite(x, docs_of(m, s), code=503)
    assert "LEGAL_LINK_TAG_KEY" in json.dumps(r)
    assert not x.svc.client_invitations


@pytest.mark.parametrize("bad", ["short", "${{client-portal.PORTAL_LINK_TAG_KEY}}" + "x" * 20])
def test_a_bad_tag_key_turns_links_off_without_stopping_legal(bad):
    # AEGIS r3 N3-L4 supersedes r2: a malformed key must not take the finance gates down with it
    cfg = config_mod.load({"LEGAL_SERVICE_TOKEN": "s" * 40, "LEGAL_LINK_TAG_KEY": bad})
    assert cfg.link_tag_key is None and cfg.link_tag_problem.startswith("off")


# --- N-L1 ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Acme™ LLC", "ＡＣＭＥ LLC", "Acme² LLC", "Acme LLC"])
def test_a_legal_name_a_client_cannot_type_back_is_refused(pair, name):
    x, m, s, docs = pair
    r = invite(x, docs, display={**DISPLAY, "client_legal_name": name}, code=422)
    assert "NFKC" in json.dumps(r)


def test_the_company_match_is_one_shared_normalization(pair):
    x, m, s, docs = pair
    assert CP.norm_company("ＡＣＭＥ, l.l.c.") == CP.norm_company("Acme LLC") == "acmellc"
    assert CP.norm_company("AcmeTM LLC") != CP.norm_company("Acme LLC")


# --- N-L2 ----------------------------------------------------------------------------------------------------------

def test_a_link_to_an_older_fill_is_refused_once_a_newer_one_is_approved(pair):
    x, m, s, docs = pair
    newer = sow_fill(x, fee="1700.00")
    r = invite(x, docs, code=409)
    assert "VERSION_SUPERSEDED" in [i["code"] for i in r["reasons"]]
    invite(x, docs_of(m, newer))


# --- N-L3 ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ch", ["ㅤ", "ᅟ", "ᅠ", "ﾠ", "⠀", "͏", "᠎", "⁥",
                                "︀", "\U000e0101", "឴", "\U0001bca0"])
def test_default_ignorables_and_blank_fillers_are_refused(pair, ch):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    assert accept(x, tok, docs, signatory={**SIGNATORY, "full_name": f"Pat{ch}Q"}).status_code == 422
    assert CP.has_invisible(f"a{ch}b")


# --- N-L4 ----------------------------------------------------------------------------------------------------------

def test_the_receipt_needs_the_form_the_accepted_submission_carried(pair):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    body = accept_body(tok, docs, h=x)
    x.ok(x.post("/legal/v1/portal/acceptances", body, caller="client_portal"), 201)
    ok = {"token": tok, "request_id": body["request_id"], "form_sha256": body["form_sha256"]}
    assert x.post("/legal/v1/portal/receipts", ok, caller="client_portal").status_code == 200
    for bad in ({**ok, "form_sha256": "f" * 64}, {**ok, "request_id": "pa-other"}):
        assert x.post("/legal/v1/portal/receipts", bad, caller="client_portal").status_code == 404
    assert x.post("/legal/v1/portal/receipts", {"token": tok, "request_id": body["request_id"]},
                  caller="client_portal").status_code == 422


# --- N-L5 ----------------------------------------------------------------------------------------------------------

def test_the_view_names_legals_time_zone_and_its_label(pair):
    x, m, s, docs = pair
    r = x.ok(resolve(x, invite(x, docs)["token"]))
    assert r["time_zone"] == "America/Los_Angeles" and r["time_zone_label"] == "Pacific Time"
    assert CP.zone_label("America/New_York") == "Eastern Time" and CP.zone_label("Europe/Paris") == "Europe/Paris time"


# --- N-L8 ----------------------------------------------------------------------------------------------------------

def test_the_msa_version_rule_fails_closed_when_stored_fill_values_are_missing(pair):
    x, m, s, docs = pair
    vars_ = {"client_legal_name": {"type": "string", "max_length": 200},
             "msa_version": {"type": "string", "max_length": 12}}
    t = x.upload("sow_revenue_recovery", "SOW for {{client_legal_name}} under MSA {{msa_version}}.", "1.0", "zbm",
                 template_variables=vars_)
    founder_approve(x, "sow_revenue_recovery", "1.0", t["sha256"])
    f = x.ok(x.apost("/legal/v1/documents/sow_revenue_recovery/versions",
                     {"request_id": rid(), "entity": "zbm", "party_ref": CLIENT,
                      "variables": {"client_legal_name": "Acme LLC", "msa_version": "1.0"}}), 201)
    founder_approve(x, "sow_revenue_recovery", f["version"], f["sha256"], rationale="Acme SOW under MSA 1.0, approved.")
    v = x.svc.doc_versions[f"sow_revenue_recovery@{f['version']}"]
    x.svc.blobs.get = (lambda orig: (lambda h: None if h == v["variables_sha256"] else orig(h)))(x.svc.blobs.get)
    r = invite(x, docs_of(m, {**f, "doc_id": "sow_revenue_recovery"}), code=409)
    assert "MSA_VERSION_MISMATCH" in [i["code"] for i in r["reasons"]]
