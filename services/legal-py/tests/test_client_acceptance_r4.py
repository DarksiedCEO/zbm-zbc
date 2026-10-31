"""AEGIS r3 on ADR 0028 (aegis-client-acceptance-r3.md @5f34539c): the Legal side.

- N3-M1: Andre finds which link a 429 in the portal's log names: the log carries the first 8 hex characters of the
  SHA-256 of the token (non-reversible: the token is 256 random bits), which Legal shows as ``log_fingerprint`` on
  the link's view and answers by ``GET /legal/v1/client-acceptance-invitations/by-fingerprint/{fp}``.
- N3-L1: LEGAL_LINK_TAG_KEY equal to any other secret Legal holds refuses the start.
- N3-L3: blank-glyph symbols (U+1D159, U+1D173-U+1D17A) and names with no letter or digit are refused.
- N3-L4: tokens carry a format version byte; /health reports the link-tag state (version, a key id, or why links
  are off). A malformed key does not stop Legal: link issuance and the portal routes answer 503 with that reason.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

import clickwrap_page as CP
import config as config_mod
import link_tag as LT
from helpers import ANDRE_TOKEN, CALLERS, LINK_TAG_KEY, SERVICE_TOKEN, Harness
from test_client_acceptance_portal import (SIGNATORY, accept, determine_cq19, docs_of, invite, msa, resolve,
                                           sow_fill)


@pytest.fixture
def pair():
    x = Harness()
    x.approve_rules()
    determine_cq19(x)
    m, s = msa(x), sow_fill(x)
    return x, m, s, docs_of(m, s)


# --- N3-M1: the fingerprint Andre looks up -------------------------------------------------------------------------

def test_andre_finds_a_link_by_the_fingerprint_the_portal_logs(pair):
    x, m, s, docs = pair
    out = invite(x, docs)
    fp = hashlib.sha256(out["token"].encode()).hexdigest()[:8]
    assert LT.log_fingerprint(out["token"]) == fp
    view = x.ok(x.client.get(f"/legal/v1/client-acceptance-invitations/{out['invitation_id']}",
                             headers=x.headers(andre=ANDRE_TOKEN)))
    assert view["log_fingerprint"] == fp
    found = x.ok(x.client.get(f"/legal/v1/client-acceptance-invitations/by-fingerprint/{fp}",
                              headers=x.headers(andre=ANDRE_TOKEN)))
    assert [f["invitation_id"] for f in found["links"]] == [out["invitation_id"]]
    assert x.get(f"/legal/v1/client-acceptance-invitations/by-fingerprint/{fp}", caller="client_portal").status_code \
        == 403
    assert x.client.get("/legal/v1/client-acceptance-invitations/by-fingerprint/xyz",
                        headers=x.headers(andre=ANDRE_TOKEN)).status_code == 422


# --- N3-L1 ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("same_as", ["service", "andre", "client_portal", "onboarding", "compliance", "ledger"])
def test_a_tag_key_equal_to_another_secret_refuses_the_start(same_as):
    env = {"LEGAL_SERVICE_TOKEN": SERVICE_TOKEN, "LEGAL_ANDRE_APPROVAL_TOKEN": ANDRE_TOKEN,
           "LEGAL_CALLER_TOKENS": json.dumps(CALLERS), "LEDGER_SERVICE_TOKEN": "l" * 40,
           "LEGAL_COMPLIANCE_URL": "http://compliance.railway.internal:8380", "LEGAL_COMPLIANCE_TOKEN": "c" * 40,
           "LEGAL_COMPLIANCE_CALLER_TOKEN": "k" * 40}
    key = {"service": SERVICE_TOKEN, "andre": ANDRE_TOKEN, "client_portal": CALLERS["client_portal"],
           "onboarding": CALLERS["onboarding"], "compliance": "c" * 40, "ledger": "l" * 40}[same_as]
    with pytest.raises(RuntimeError, match="LEGAL_LINK_TAG_KEY"):
        config_mod.load({**env, "LEGAL_LINK_TAG_KEY": key})


# --- N3-L3 ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cp", [0x1D159] + list(range(0x1D173, 0x1D17B)))
def test_musical_blank_symbols_are_refused(pair, cp):
    x, m, s, docs = pair
    assert CP.has_invisible(f"Pat{chr(cp)}Q")
    tok = invite(x, docs)["token"]
    assert accept(x, tok, docs, signatory={**SIGNATORY, "full_name": f"Pat{chr(cp)}Q"}).status_code == 422


@pytest.mark.parametrize("name", ["́́", "...", "·", "  -  "])
def test_a_name_with_no_letter_or_digit_is_refused(pair, name):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    assert accept(x, tok, docs, signatory={**SIGNATORY, "full_name": name}).status_code == 422


def test_ordinary_names_still_pass():
    for name in ("José Núñez-O'Brien", "李小龙", "Åsa Ölund", "D'Angelo III", "Anne-Marie & Co"):
        assert not CP.has_invisible(name) and CP.has_visible(name), name


# --- N3-L4 ---------------------------------------------------------------------------------------------------------

def test_tokens_carry_a_format_version(pair):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    raw = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4))
    assert raw[0] == LT.VERSION == 1 and len(raw) == 1 + 32 + 16
    other = base64.urlsafe_b64encode(bytes([2]) + raw[1:]).decode().rstrip("=")
    assert not LT.valid(other, LINK_TAG_KEY.encode()) and LT.version_of(other) == 2


def test_health_reports_the_link_tag_state():
    h = Harness().ok(Harness().client.get("/health"))
    assert h["link_tags"] == {"status": "ok", "version": LT.VERSION,
                              "key_id": hashlib.sha256(LINK_TAG_KEY.encode()).hexdigest()[:8]}
    off = Harness(env={"LEGAL_LINK_TAG_KEY": "__unset__"})
    assert off.ok(off.client.get("/health"))["link_tags"]["status"].startswith("off")


@pytest.mark.parametrize("bad", ["short", LINK_TAG_KEY + " ", "${{client-portal.X}}" + "y" * 30])
def test_a_malformed_key_disables_links_only(bad):
    x = Harness(env={"LEGAL_LINK_TAG_KEY": bad})               # Legal starts (the Finance gates stay up)
    x.approve_rules()
    determine_cq19(x)
    h = x.ok(x.client.get("/health"))
    assert h["status"] == "ok" and h["link_tags"]["status"].startswith("off") and "LEGAL_LINK_TAG_KEY" in \
        h["link_tags"]["status"]
    m, s = msa(x), sow_fill(x)
    r = invite(x, docs_of(m, s), code=503)
    assert "LEGAL_LINK_TAG_KEY" in json.dumps(r)
    good = LT.mint(b"x" * 40)
    for path, body in (("/legal/v1/portal/invitations/resolve", {"token": good}),
                       ("/legal/v1/portal/receipts", {"token": good, "request_id": "pa-1", "form_sha256": "0" * 64})):
        r = x.post(path, body, caller="client_portal")
        assert r.status_code == 503 and "LEGAL_LINK_TAG_KEY" in r.text
    assert x.ok(x.get("/legal/v1/documents/client_msa/versions/1.0", caller="finance_31"))["status"] == "approved"
