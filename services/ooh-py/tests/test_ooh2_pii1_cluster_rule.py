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


# ---------------------------------------------------------------- AEGIS r11 source S-1: spellings cannot hide digits

SPELLED = ["1234567%40", "1234567&#64;", "123-45-67%40", "4111 1111 1111 11%40", "12 34 56 7&#64;"]


@pytest.mark.parametrize("text", SPELLED)
def test_cluster_an_at_spelling_cannot_hide_digits(text):
    kept, kinds = pii.screen_free_text(f"note {text} end")
    assert kinds and sum(ch.isdigit() for ch in kept) < 9, (kept, kinds)


@pytest.mark.parametrize("text", SPELLED)
def test_cluster_an_at_spelling_cannot_hide_digits_in_the_fields(h, text):
    test_cluster_vendor_text_is_redacted_and_hashed_redacted(h, text)
    test_cluster_face_text_attribute_is_refused(h, text)


def test_cluster_the_kept_text_never_holds_a_cluster():
    import random
    rnd = random.Random(20261018)
    alphabet = list("0123456789 -._:/,|+%@#&;x4") + ["%40", "&#64;", "&#x40;", "&commat;", "[at]", "(dot)", "\u200b",
                                                       "\u3164", "\ue000", "\u0664", "a", "Z", "&nbsp;", "&ndash;",
                                                       "&amp;", "&#52;", "\u30fc", "\u02bc", "\u2ffc", "\u0378"]
    for _ in range(20000):
        t = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 30)))
        kept, _kinds = pii.screen_free_text(t)
        for form in (kept, pii.screen_form(kept), pii._norm(kept), pii._norm(_unescape(kept))):
            assert not pii.number_cluster_spans(form), (t, kept)


# ---------------------------------------------------------------- founder decisions after r11 (Oct 18 2026)
# 2: only ASCII letters A-Z / a-z end a digit cluster; every other character, Unicode letters that look like
#    punctuation included, joins it (runtime O11-1, security S11-1, source S-5)
# 3: HTML entities are decoded and the decoded text is screened too (security S11-2, data D11-L2)
# 1: unassigned characters are kept as typed, so the kept text and the hash do not depend on the Unicode version (N1)

LOOKALIKE = ['1234ʼ56789', '12ー34ー56ー789', '41ー11ー11ー11ー11ー11ー11ー11', '123ǀ456ǀ789', '123ـ456ـ789', '123ː45ː6789', '123ˉ45ˉ6789', '123ꞏ45ꞏ6789', '123一456一789', '12α34α56789']
ENTITIES = ['4111&nbsp;1111&nbsp;1111&nbsp;1111', '123&ndash;45&ndash;6789', 'SSN 123&nbsp;45&nbsp;6789', '4111&#32;1111&#32;1111&#32;1111', '4111&#x2D;1111&#x2D;1111&#x2D;1111', '4111&amp;nbsp;1111&amp;nbsp;1111&amp;nbsp;1111', '&#52;111 1111 1111 1111']


@pytest.mark.parametrize("text", LOOKALIKE + ENTITIES)
def test_decisions_screen_finds_it(text):
    kept, kinds = pii.screen_free_text(f"note {text} end")
    assert kinds, (text, kept)
    for form in (kept, pii._norm(kept), pii._norm(_unescape(kept))):
        assert not pii.number_cluster_spans(form), (text, kept)


def _unescape(t):
    import html
    for _ in range(64):                  # to a fixed point (AEGIS r12 source S-4: three passes hid deeper nesting)
        once = html.unescape(t)
        if once == t:
            return t
        t = once
    return t


@pytest.mark.parametrize("text", LOOKALIKE + ENTITIES)
def test_decisions_vendor_text_is_redacted_and_the_face_attribute_refused(h, text):
    test_cluster_vendor_text_is_redacted_and_hashed_redacted(h, text)
    test_cluster_face_text_attribute_is_refused(h, text)


@pytest.mark.parametrize("ref", ["123" + chr(0x2bc) + "456789", "4111&nbsp;1111&nbsp;1111&nbsp;1111"])
def test_decisions_client_approval_doc_ref_is_refused(h, ref):
    # the reference's own pattern (ASCII letters, digits, . _ : -) refuses these before the personal-data screen
    ctx = h.to_proposal_sent("acme")
    lines, events = len(h.svc.log), len(h.ledger.events)
    r = h.approve(ctx, client_approval_doc_ref=ref)
    assert r.status_code == 422, r.text
    assert len(h.svc.log) == lines and len(h.ledger.events) == events


def test_decisions_ascii_letters_still_end_a_cluster():
    assert pii.screen_free_text("1234x56789") == ("1234x56789", [])
    assert pii.screen_free_text("14x48 bulletin, 2026-11-01 to 2026-11-30")[1] == []


def test_decisions_entities_without_digits_are_kept_as_typed():
    assert pii.screen_free_text("fine &amp; dandy 14x48") == ("fine &amp; dandy 14x48", [])


@pytest.mark.parametrize("cp", [0x0378, 0x2FFC, 0x31EF, 0x2EBF0])
def test_decisions_unassigned_characters_are_kept_so_the_hash_is_stable(cp):
    # U+2FFC-2FFF, U+31EF and U+2EBF0.. are unassigned in Unicode 15.0 (Python 3.12) and assigned in 15.1 (3.13)
    text = "faces west " + chr(cp) + " lit"
    assert pii.screen_free_text(text) == (text, [])
    import service as SV
    from ledger import payload_sha256 as p
    assert SV.request_sha({"request_id": "r", "vendor_text": text}) == p({"request_id": "r", "vendor_text": text})


# ---------------------------------------------------------------- AEGIS r12 (1a90498) source S-1, S-2, S-4

def _nested(entity, depth):
    return "&" + "amp;" * depth + entity


NESTED = ["123456" + _nested("nbsp;", d) + "789" for d in (1, 3, 4, 8, 15)] + \
         ["123" + _nested("ndash;", 4) + "45" + _nested("ndash;", 4) + "6789"]
DECODED_TWICE = ["1234567&nbsp;%40", "1234567&amp;%40", "1234567&#x25;40", "12345a&zwj;&#769;6789",
                 "12345a&#8205;&#x301;6789", "12345a&shy;&#769;6789", "12345a&#x200b;&#769;6789"]


@pytest.mark.parametrize("text", NESTED + DECODED_TWICE)
def test_r12_nested_entities_and_decoded_forms_are_redacted(text):
    kept, kinds = pii.screen_free_text(f"note {text} end")
    assert kinds and not pii._still_holds_personal_data(kept), (text, kept)


def test_r12_decoding_that_never_settles_redacts_the_whole_value():
    text = "123" + _nested("nbsp;", pii._MAX_DECODE + 2) + " ok"
    assert pii.screen_free_text(text) == ("[redacted]", ["number"])


def test_r12_screen_form_is_idempotent_with_hidden_characters_and_marks():
    for t in ["12345a\u200d\u03016789", "a\u00ad\u0301b", "4\u0303 1", "x\u200b\u0301\u200by"]:
        once = pii.screen_form(t)
        assert pii.screen_form(once) == once, (t, once)


@pytest.mark.parametrize("text", NESTED[:3] + DECODED_TWICE[:4])
def test_r12_the_fields_redact_or_refuse(h, text):
    test_cluster_vendor_text_is_redacted_and_hashed_redacted(h, text)
    test_cluster_face_text_attribute_is_refused(h, text)


def test_r12_backstop_property_on_every_form():
    import random
    rnd = random.Random(1904)
    alphabet = list("0123456789 -._:/,|+%@#&;xa") + ["%40", "&#64;", "&nbsp;", "&ndash;", "&amp;", "&#52;", "&#x34;",
                                                       "&zwj;", "&#769;", "&#x301;", "&shy;", "&#x200b;", "\u200b",
                                                       "\u0301", "\u3164", "\ue000", "\u0664", "\u30fc", "\u02bc",
                                                       "\u2ffc", "\u0378", "&amp;amp;"]
    for _ in range(20000):
        t = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 30)))
        kept, _kinds = pii.screen_free_text(t)
        assert not pii._still_holds_personal_data(kept), (t, kept)
        decoded = _unescape(kept)
        for form in (kept, pii.screen_form(kept), pii._norm(kept), decoded, pii.screen_form(decoded), pii._norm(decoded)):
            assert not pii.number_cluster_spans(form), (t, kept, form)


# ---------------------------------------------------------------- AEGIS r12 security S12-1, reliability R12-2

def test_r12_s12_1_every_digit_valued_character_counts():
    # dingbat and circled digits, Ethiopic: not decimal (Nd) but digits all the same; 1a90498 kept 69 such kinds
    import unicodedata
    kept = [cp for cp in range(0x80, 0x110000) if chr(cp).isdigit()
            and not pii.screen_free_text("note " + chr(cp) * 9 + " end")[1]]
    assert kept == [], [hex(c) for c in kept[:20]]
    assert pii.screen_free_text("❹❶❶❶ ❶❶❶❶ ❶❶❶❶ ❶❶❶❶")[1]
    assert unicodedata.digit("❶") == 1


@pytest.mark.parametrize("cp", [0x2FFC, 0x2FFF, 0x31EF, 0x2EBF0, 0x2EE5D])
def test_r12_r12_2_shapes_do_not_depend_on_the_unicode_version(cp):
    # unassigned in Unicode 15.0 (Python 3.12), assigned in 15.1 (3.13): a domain with it is an address on both
    c = chr(cp)
    assert pii.has_personal_data("a@b" + c + "d.com") and pii.free_text_has_personal_data("a@b" + c + "d.com")
    assert pii.screen_free_text("faces west " + c + " lit") == ("faces west " + c + " lit", [])
