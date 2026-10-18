"""OOH2-PII-1 (High, founder ruling Oct 18 2026; AEGIS security M-1): card numbers and SSNs typed into free text were
stored as typed. pii.py now also finds Luhn-checked card numbers (any separators between the digit groups, a
card-network prefix) and SSNs (onboarding-py redaction.py's shapes), so in the three fields of the finding:
- unit ``vendor_text`` is redacted before the line is written;
- a face text attribute is refused 422 ATTRIBUTE_VALUE_PERSONAL_DATA, nothing written;
- ``client_approval_doc_ref`` is refused 422 ATTRIBUTE_VALUE_PERSONAL_DATA, nothing written.
Every test checks the log file on disk, the pending files and every ledger event, not only the HTTP answer.
Test values only: public test card numbers and 123-45-6789."""
from __future__ import annotations

import json
import os

import pytest

import pii
from helpers import Harness, rid

CARDS = ["4111 1111 1111 1111", "4111-1111-1111-1111", "4111.1111.1111.1111", "4111_1111_1111_1111",
         "5555 5555 5555 4444", "3782 822463 10005", "4111111111111111"]
SSNS = ["123-45-6789", "123‐45‐6789", "SSN 123456789", "social security number: 123 45 6789"]
SECRETS = CARDS + SSNS


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


@pytest.fixture
def h(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"))
    h.tenant("acme")
    yield h
    if not h.svc.closed:
        h.svc.close()
    if h.settings.data_dir_lock is not None:
        h.settings.data_dir_lock.release()


def _stored(h) -> str:
    """Everything this service wrote: the log file, the pending files, every ledger event (payload and summary)."""
    parts = []
    d = h.settings.data_dir
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p) and not name.endswith(".lock"):
            with open(p, "rb") as fh:
                parts.append(fh.read().decode("utf-8", "replace"))
    parts.append(json.dumps(h.ledger.events, default=str))
    return "\n".join(parts)


def _absent(h, secret: str) -> None:
    stored = _stored(h)
    assert secret not in stored, secret
    if len(_digits(secret)) >= 9:                     # also with the separators taken out
        assert _digits(secret) not in _digits_runs(stored), secret


def _digits_runs(text: str) -> set:
    """Each run of digits and separators, as digits only."""
    out, cur = set(), ""
    for ch in text + "x":
        if ch.isdigit():
            cur += ch
        elif ch in " -._:‐":
            continue
        else:
            if cur:
                out.add(cur)
            cur = ""
    return out


def _unit(h, vendor_text: str):
    cid = h.campaign("acme")
    h.plan("acme", cid)
    fid = h.face("lamar", f"face-{rid()[:8]}")
    return cid, h.post(f"/tenants/acme/campaigns/{cid}/inventory",
                       {"request_id": rid(), "face_id": fid, "flight_start": "2026-11-01", "flight_end": "2026-11-30",
                        "vendor_text": vendor_text})


# ---------------------------------------------------------------- 1. unit vendor_text: redacted before the write

@pytest.mark.parametrize("secret", SECRETS)
def test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write(h, secret):
    cid, r = _unit(h, f"faces west, contact {secret} for access")
    assert r.status_code == 201, r.text
    vt = r.json()["vendor_text"]
    assert vt["personal_data_redacted"] is True and "[redacted]" in vt["text"], vt
    assert "faces west" in vt["text"]                              # the rest of the text is kept
    _absent(h, secret)
    u = h.get(f"/tenants/acme/campaigns/{cid}/inventory/{r.json()['unit_id']}", caller="ooh_agent")
    assert secret not in u.text


# ---------------------------------------------------------------- 2. face text attribute: refused, nothing written

@pytest.mark.parametrize("secret", SECRETS)
def test_pii1_face_text_attribute_card_or_ssn_is_refused_before_any_write(h, secret):
    fid = h.face("lamar", "face-attr-1")
    src = h.source("lamar")                                       # recorded first: it is a write of its own
    f = h.ok(h.get(f"/faces/{fid}"))
    lines, events = len(h.svc.log), len(h.ledger.events)
    r = h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                            "attribute": "location_description", "value": f"billing {secret}",
                                            "source_id": src})
    assert r.status_code == 422 and r.json()["detail"] == "ATTRIBUTE_VALUE_PERSONAL_DATA", r.text
    assert len(h.svc.log) == lines and len(h.ledger.events) == events
    _absent(h, secret)


# ---------------------------------------------------------------- 3. client_approval_doc_ref: refused, nothing written

REF_SECRETS = ["4111-1111-1111-1111", "4111_1111_1111_1111", "4111.1111.1111.1111", "io-4111-1111-1111-1111",
               "123-45-6789", "SSN:123456789"]


@pytest.mark.parametrize("secret", REF_SECRETS)
def test_pii1_client_approval_doc_ref_card_or_ssn_is_refused_before_any_write(h, secret):
    ctx = h.to_proposal_sent("acme")
    before = h.view("acme", ctx["cid"])
    lines, events = len(h.svc.log), len(h.ledger.events)
    r = h.approve(ctx, client_approval_doc_ref=secret)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "ATTRIBUTE_VALUE_PERSONAL_DATA", r.text
    assert len(h.svc.log) == lines and len(h.ledger.events) == events
    after = h.view("acme", ctx["cid"])
    assert after["state"] == before["state"] == "PROPOSAL_SENT" and after["version"] == before["version"]
    _absent(h, secret)
    h.ok(h.approve(ctx), 200)                                    # control: the signed IO's own reference is accepted


# ---------------------------------------------------------------- controls: what is not a card or an SSN passes

@pytest.mark.parametrize("ref", ["io-acme-signed-1", "DOC-2026-10-18-001", "po-12345", "4111-1111-1111-1112"])
def test_pii1_ordinary_references_are_accepted(h, ref):
    ctx = h.to_proposal_sent("acme")
    r = h.approve(ctx, client_approval_doc_ref=ref)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("text", ["14x48 faces west, lit 6am-midnight, 2026-11-01 to 2026-11-30",
                                  "4111 1111 1111 1112 is not Luhn-valid", "1234 5678 9012 3452 has no card prefix",
                                  "permit 123-456-789", "GPS 34.101 -118.3265"])
def test_pii1_ordinary_vendor_text_is_not_redacted(h, text):
    _, r = _unit(h, text)
    assert r.status_code == 201, r.text
    assert r.json()["vendor_text"] == {**r.json()["vendor_text"], "personal_data_redacted": False, "text": text}


def test_pii1_the_controls_are_what_they_claim():
    assert pii.luhn_card("1234567890123452") is False and _luhn("1234567890123452")    # Luhn-valid, no prefix
    assert not _luhn("4111111111111112")


def _luhn(d: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(d)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


# ---------------------------------------------------------------- pii.py directly

@pytest.mark.parametrize("secret", SECRETS)
def test_pii1_has_personal_data_and_redact(secret):
    text = f"note: {secret} end"
    assert pii.has_personal_data(text)
    red = pii.redact(text)
    assert "[redacted]" in red and red.startswith("note: ") and red.endswith(" end")
    assert _digits(secret) not in _digits(red)


def test_pii1_a_card_followed_by_another_group_is_still_seen():
    assert pii.has_personal_data("ref 4242-4242-4242-4242-1228")
    assert pii.card_spans("x 4111 1111 1111 1111 y") == [(2, 21)]


def test_pii1_a_routing_number_is_not_joined_into_a_card():
    assert pii.card_spans("ACH-256074974-93982757") == []
    assert not pii.has_personal_data("trace 021000021 0000001")


def test_pii1_ssn_glued_to_a_word_or_with_other_digit_counts_is_not_an_ssn():
    for t in ("A123-45-6789", "123-45-67890", "1234-56-7890", "SSN 12345678"):
        assert pii._ssn_spans(pii._norm(t)) == [], t
