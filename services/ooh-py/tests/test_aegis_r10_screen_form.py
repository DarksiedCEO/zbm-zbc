"""AEGIS round 10 at 0f5fbe3 (security BLOCK F1 residual; source M-1, M-2, L-1..L-3):
- F1: every screen and the stored vendor text read one form (``pii.screen_form``), so a value redacted in the line
  is also redacted in the request hash, and a face text attribute that storage would redact is refused: "123  45
  6789" (two spaces), a tab or a no-break space between the groups;
- M-1: invisible characters that are not format characters (combining grapheme joiner, variation selectors, Hangul
  and Mongolian fillers, the blank braille pattern), private-use characters and a combining mark on a digit no
  longer hide a card or an SSN; letters keep their marks;
- consistency: for every probe text, the stored vendor text is redacted exactly when the screen finds personal data.
Test values only: public test card numbers and 123-45-6789."""
from __future__ import annotations

import json

import pytest

import pii
import svc_campaigns as sc
from helpers import rid
from ledger import payload_sha256
from test_aegis_r9_pii_hardening import _unit_body
from test_ooh2_pii1_card_ssn import _absent, h  # noqa: F401  (h is a fixture)

SPACED = ["123  45  6789", "123\t45\t6789", "123 45 6789", "4111  1111  1111  1111", "4111 1111 1111 1111"]
HIDDEN = ["4111 11͏11 1111 1111", "4111 11️11 1111 1111", "4111 1111 11ㅤ11 1111",
          "4111 1111 11⠀11 1111", "4111 1111 11឵11 1111", "4111 1111 11\U000e010011 1111",
          "4111 1111 11᠋11 1111", "4111 1111 11ᅟ11 1111", "4111 1111 1111 1111",
          "4111 1111 11̃11 1111", "123-4͏5-6789", "123-4ㅤ5-6789"]


@pytest.mark.parametrize("secret", SPACED + HIDDEN)
def test_r10_vendor_text_is_redacted_and_the_hash_never_holds_it(h, secret):
    cid, body = _unit_body(h, f"contact {secret} now")
    r = h.post(f"/tenants/acme/campaigns/{cid}/inventory", body)
    assert r.status_code == 201, r.text
    assert r.json()["vendor_text"]["personal_data_redacted"] is True, r.json()["vendor_text"]
    line = json.loads(h.svc.log.raw_lines(len(h.svc.log) - 1)[0])
    assert line["data"]["request_sha"] != payload_sha256(body)              # never the hash of the clear body
    _absent(h, pii.screen_form(secret))


@pytest.mark.parametrize("secret", SPACED + HIDDEN)
def test_r10_face_text_attribute_is_refused(h, secret):
    fid = h.face("lamar", "face-r10")
    src = h.source("lamar")
    f = h.ok(h.get(f"/faces/{fid}"))
    lines, events = len(h.svc.log), len(h.ledger.events)
    r = h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                            "attribute": "location_description", "value": f"billing {secret}",
                                            "source_id": src})
    assert r.status_code == 422 and r.json()["detail"] == "ATTRIBUTE_VALUE_PERSONAL_DATA", r.text
    assert len(h.svc.log) == lines and len(h.ledger.events) == events


PROBES = SPACED + HIDDEN + ["faces west 14x48", "123 - 45 - 6789", "ref 2026-10-18", "नमस्ते 14x48",
                            "call 310 555 1234", "x" * 50 + " 4111 1111 1111 1111", "café open 6am"]


@pytest.mark.parametrize("text", PROBES)
def test_r10_storage_redacts_exactly_when_the_screen_finds_personal_data(text):
    assert sc.observed(text)["personal_data_redacted"] is pii.free_text_has_personal_data(text)   # r11: one screen


def test_r10_marks_on_letters_are_kept():
    assert pii.screen_form("नमस्ते") == "नमस्ते"
    assert pii.screen_form("café") == "café"                       # NFKC composes it
    assert pii.screen_form("4̃ 1") == "4 1"                              # a mark on a digit is dropped


def test_r10_screen_form_is_idempotent():
    for t in PROBES:
        assert pii.screen_form(pii.screen_form(t)) == pii.screen_form(t)


# ---------------------------------------------------------------- r10 data D10-H1 / D10-M1: separators and hidden characters

D10 = ['123 - 45 - 6789', '123 – 45 – 6789', 'SSN: 123 - 45 - 6789', '123--45--6789', '123..45..6789', '123 . 45 . 6789', '123,45,6789', 'SSN 123,45,6789', '123|45|6789', '123·45·6789', '123+45+6789', '1̃23-45-6789', '4̃111 1111 1111 1111', '123\u168045\u16806789', '123\ue00045\ue0006789', '123\u037845\u03786789', '123\u206045\u20606789', 'TIN 123 45 6789']
D10_REFS = ['123--45--6789', '123..45..6789', 'SSN:123-45-6789']


@pytest.mark.parametrize("secret", D10)
def test_r10_d10_vendor_text_is_redacted_and_the_hash_never_holds_it(h, secret):
    test_r10_vendor_text_is_redacted_and_the_hash_never_holds_it(h, secret)
    for form in (pii.screen_form(secret), pii.screen_form(secret, " ")):
        _absent(h, form)


@pytest.mark.parametrize("secret", D10)
def test_r10_d10_face_text_attribute_is_refused(h, secret):
    test_r10_face_text_attribute_is_refused(h, secret)


@pytest.mark.parametrize("secret", D10_REFS)
def test_r10_d10_client_approval_doc_ref_is_refused(h, secret):
    ctx = h.to_proposal_sent("acme")
    lines, events = len(h.svc.log), len(h.ledger.events)
    r = h.approve(ctx, client_approval_doc_ref=secret)
    assert r.status_code == 422 and r.json()["detail"] == "ATTRIBUTE_VALUE_PERSONAL_DATA", r.text
    assert len(h.svc.log) == lines and len(h.ledger.events) == events


@pytest.mark.parametrize("secret", D10)
def test_r10_d10_storage_and_screen_agree(secret):
    assert sc.observed("x " + secret + " y")["personal_data_redacted"] is pii.has_personal_data(secret) is True
    assert not any(ch.isdigit() for ch in pii.redact(secret))


def test_r10_hidden_characters_rejoin_a_card_and_separate_an_ssn():
    card = "4111 11" + chr(0xe000) + "11 1111 1111"                       # removed: the group is whole again
    ssn = "123" + chr(0xe000) + "45" + chr(0xe000) + "6789"               # read as a space: the groups are apart
    assert pii.has_personal_data(card) and pii.has_personal_data(ssn)
    assert pii.screen_form(card) == "4111 1111 1111 1111" and pii.screen_form(ssn, " ") == "123 45 6789"
