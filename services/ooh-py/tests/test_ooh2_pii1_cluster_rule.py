"""OOH2-PII-1, founder design change (Oct 18 2026): in the three screened free-text fields (unit vendor text, a face
text attribute, the client approval document reference), after normalising, any cluster of nine or more digits joined
by characters that are not letters is personal data — whatever the separators are. The card and SSN rules only label
what was redacted. The request hash uses the vendor text as kept (redacted).

This file pins the rule both ways: every spelling that got through a separator list earlier is caught, and the
accepted false positives (ordinary text that holds such a cluster) are refused or redacted, so the trade-off is a
tested, documented property rather than an accident. Test values only."""
from __future__ import annotations

import json

import pytest

import pii
import svc_campaigns as sc
from helpers import rid
from ledger import payload_sha256
from test_aegis_r9_pii_hardening import _unit_body
from test_ooh2_pii1_card_ssn import _absent, h  # noqa: F401  (h is a fixture)

# every separator, grouping and hidden-character spelling seen across rounds 8-10, and some never seen
CAUGHT = ["123-45-6789", "123 - 45 - 6789", "123--45--6789", "123..45..6789", "123,45,6789", "123|45|6789",
          "123+45+6789", "123~45~6789", "123 ** 45 ** 6789", "123  45  6789", "123/45/6789", "123:45:6789",
          "1 2 3 4 5 6 7 8 9", "12-34-56-789", "4111 1111 1111 1111", "4111-1111-1111-1111", "4 1 1 1 1 1 1 1 1 1",
          "4111 1111 1111 1112", "1234 5678 9012 3452", "021000021", "123456789"]
# accepted false positives: ordinary text that holds a cluster of nine or more digits with no letter in it
FALSE_POSITIVES = ["GPS 34.101 -118.3265", "34.0522, 118.2437", "ZIP 90028-1234", "Flight 2026-11-01..2026-11-30",
                   "routing 021000021", "permit 123-456-789", "DOC-2026-10-18-001"]
# still accepted: eight digits or fewer between letters
ACCEPTED = ["Flight 2026-11-01 to 2026-11-30", "lit 06:00-24:00", "1,250,000 weekly impressions", "14x48 bulletin",
            "$12,500.00 per 4 weeks", "Panels 101 and 205 on Sunset", "12345 Ventura Blvd, Studio City CA 91604"]


@pytest.mark.parametrize("text", CAUGHT + FALSE_POSITIVES)
def test_cluster_free_text_screen_finds_it(text):
    kept, kinds = pii.screen_free_text(f"note {text} end")
    assert kinds, text
    assert sum(ch.isdigit() for ch in kept) < 9 and kept.startswith("note") and kept.endswith("end"), kept


@pytest.mark.parametrize("text", ACCEPTED)
def test_cluster_eight_digits_or_fewer_between_letters_pass(text):
    assert pii.screen_free_text(text) == (pii.screen_form(text), [])


def test_cluster_labels_say_what_was_redacted():
    assert pii.screen_free_text("a 4111 1111 1111 1111 b")[1] == ["card"]
    assert pii.screen_free_text("a 123-45-6789 b")[1] == ["ssn"]
    assert pii.screen_free_text("a 21-34-55-89 0 b")[1] == ["number"]
    assert pii.screen_free_text("jane@example.com and 4111 1111 1111 1111")[1] == ["card", "email"]


@pytest.mark.parametrize("text", CAUGHT + FALSE_POSITIVES)
def test_cluster_vendor_text_is_redacted_and_hashed_redacted(h, text):
    cid, body = _unit_body(h, f"contact {text} now")
    r = h.post(f"/tenants/acme/campaigns/{cid}/inventory", body)
    assert r.status_code == 201, r.text
    vt = r.json()["vendor_text"]
    assert vt["personal_data_redacted"] is True and vt["personal_data_kinds"], vt
    line = json.loads(h.svc.log.raw_lines(len(h.svc.log) - 1)[0])
    assert line["data"]["request_sha"] == payload_sha256({**body, "vendor_text": vt["text"]})   # the text as kept
    assert line["data"]["request_sha"] != payload_sha256(body)
    _absent(h, text)


@pytest.mark.parametrize("text", CAUGHT + FALSE_POSITIVES)
def test_cluster_face_text_attribute_is_refused(h, text):
    fid = h.face("lamar", "face-cl")
    src = h.source("lamar")
    f = h.ok(h.get(f"/faces/{fid}"))
    lines, events = len(h.svc.log), len(h.ledger.events)
    r = h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                            "attribute": "location_description", "value": f"note {text}",
                                            "source_id": src})
    assert r.status_code == 422 and r.json()["detail"] == "ATTRIBUTE_VALUE_PERSONAL_DATA", r.text
    assert len(h.svc.log) == lines and len(h.ledger.events) == events


@pytest.mark.parametrize("ref", ["123-45-6789", "123--45--6789", "123..45..6789", "4111-1111-1111-1111",
                                 "DOC-2026-10-18-001", "io-123456789"])
def test_cluster_client_approval_doc_ref_is_refused(h, ref):
    ctx = h.to_proposal_sent("acme")
    lines, events = len(h.svc.log), len(h.ledger.events)
    r = h.approve(ctx, client_approval_doc_ref=ref)
    assert r.status_code == 422 and r.json()["detail"] == "ATTRIBUTE_VALUE_PERSONAL_DATA", r.text
    assert len(h.svc.log) == lines and len(h.ledger.events) == events


@pytest.mark.parametrize("text", ACCEPTED)
def test_cluster_ordinary_text_is_kept_and_hashed_as_kept(h, text):
    cid, body = _unit_body(h, text)
    r = h.post(f"/tenants/acme/campaigns/{cid}/inventory", body)
    assert r.status_code == 201, r.text
    vt = r.json()["vendor_text"]
    assert vt["personal_data_redacted"] is False and vt["personal_data_kinds"] == [] and vt["text"] == text, vt


def test_cluster_the_ref_attribute_kind_and_campaign_names_keep_the_old_screen():
    # the founder scoped the rule to the three fields; other screened values keep the e-mail/phone/card/SSN rules
    assert not pii.has_personal_data("ZIP 90028-1234") and pii.free_text_has_personal_data("ZIP 90028-1234")
    assert sc.names_a_card("4111-1111-1111-1111") and not sc.names_a_card("021000021")
