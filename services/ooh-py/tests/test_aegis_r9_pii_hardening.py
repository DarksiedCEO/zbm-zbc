"""AEGIS round 9 at 91ea9a1 (security BLOCK F1; F2, F3, F5, F6; source S-1; runtime RT9-1; reliability F1, F2):
- F1: the request hash kept in the log is taken over the body with personal data redacted, so it never stands for a
  card number or an SSN (a plain hash of the raw body gave the number back by search); bodies without personal data
  hash as before;
- F2: a zero-width or other format character inside a digit group no longer hides a card or an SSN;
- S-1 / F5: digits of other scripts are read as ASCII digits;
- F3: SSNs split by a dot, an underscore, a colon, a slash or a space, touching letters, or after "TIN";
- F6: ``names_a_card`` reads the dotless ı as a separator again (as at 99569bd);
- RT9-1: start-up never follows a symlinked log, never blocks on a FIFO, never reads a device or a directory;
- reliability F1: a failed close of the integrity check's read-only descriptor changes nothing.
Test values only: public test card numbers and 123-45-6789."""
from __future__ import annotations

import json
import os
import threading

import pytest

import pii
import service as SV
import store as ST
import svc_campaigns as sc
from helpers import Harness, rid
from ledger import payload_sha256
from store import RecordLog, StoreCorrupt
from test_aegis_store_write_path import _fake_os
from test_ooh2_pii1_card_ssn import _absent, h  # noqa: F401  (h is a fixture)

ZW_CARDS = ["4​111 1111 1111 1111", "4111 11\u206011 1111 1111", "4111 1111 1111 111﻿1",
            "4111 1111 ⁢1111 1111"]
SCRIPT_CARDS = ["٤١١١ ١١١١ ١١١١ ١١١١",
                "४१११-११११-११११-११११"]
SSN_FORMS = ["123.45.6789", "123_45_6789", "123:45:6789", "123/45/6789", "123 45 6789", "x123-45-6789",
             "io-123-45-6789", "SSN123456789", "TIN 123456789", "123-4​5-6789"]


def _unit_body(h, vendor_text):
    cid = h.campaign("acme")
    h.plan("acme", cid)
    fid = h.face("lamar", f"face-{rid()[:8]}")
    return cid, {"request_id": rid(), "face_id": fid, "flight_start": "2026-11-01", "flight_end": "2026-11-30",
                 "vendor_text": vendor_text}


# ---------------------------------------------------------------- F1: the request hash

def test_r9_f1_request_hash_is_over_the_redacted_body(h):
    secret = "4111 1111 1111 1111"
    cid, body = _unit_body(h, f"call {secret} now")
    r = h.post(f"/tenants/acme/campaigns/{cid}/inventory", body)
    assert r.status_code == 201, r.text
    line = json.loads(h.svc.log.raw_lines(len(h.svc.log) - 1)[0])
    kept = line["data"]["request_sha"]
    assert kept != payload_sha256(body)                                     # never the raw body's hash
    assert kept == payload_sha256({**body, "vendor_text": pii.redact(body["vendor_text"])})
    n = len(h.svc.log)
    again = h.post(f"/tenants/acme/campaigns/{cid}/inventory", body)        # the same request is still a replay
    assert again.status_code in (200, 201) and again.json()["unit_id"] == r.json()["unit_id"], again.text
    assert len(h.svc.log) == n                                              # and appends nothing
    _absent(h, secret)


def test_r9_f1_a_body_without_personal_data_hashes_as_before():
    body = {"request_id": "r-1", "name": "Fall push", "markets": ["la"], "nested": {"a": ["x", 1]}}
    assert SV.request_sha(body) == payload_sha256(body)


def test_r9_f1_only_vendor_text_is_redacted_for_the_hash():
    a = {"request_id": "r-1", "face_id": "ooh-fce-1234567890123456789012345678901234567890", "vendor_text": "ok"}
    b = {**a, "face_id": "ooh-fce-1234567890123456789012345678901234567891"}
    assert SV.request_sha(a) == payload_sha256(a) and SV.request_sha(a) != SV.request_sha(b)   # ids hashed as sent
    c = {**a, "vendor_text": "call 4111 1111 1111 1111"}
    d = {**a, "nested": [{"vendor_text": "SSN 123-45-6789"}]}
    assert SV.request_sha(c) == payload_sha256({**a, "vendor_text": pii.redact(c["vendor_text"])})
    assert SV.request_sha(d) == payload_sha256({**a, "nested": [{"vendor_text": pii.redact("SSN 123-45-6789")}]})


# ---------------------------------------------------------------- F2, S-1/F5, F3: forms that got through

@pytest.mark.parametrize("secret", ZW_CARDS + SCRIPT_CARDS + SSN_FORMS)
def test_r9_vendor_text_forms_are_redacted(h, secret):
    cid, body = _unit_body(h, f"faces west, contact {secret} for access")
    r = h.post(f"/tenants/acme/campaigns/{cid}/inventory", body)
    assert r.status_code == 201, r.text
    vt = r.json()["vendor_text"]
    assert vt["personal_data_redacted"] is True and "[redacted]" in vt["text"], vt
    _absent(h, secret)
    _absent(h, pii.strip_format(secret))


@pytest.mark.parametrize("secret", ZW_CARDS + SCRIPT_CARDS + SSN_FORMS)
def test_r9_face_text_attribute_forms_are_refused(h, secret):
    fid = h.face("lamar", "face-r9")
    src = h.source("lamar")
    f = h.ok(h.get(f"/faces/{fid}"))
    lines, events = len(h.svc.log), len(h.ledger.events)
    r = h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                            "attribute": "location_description", "value": f"billing {secret}",
                                            "source_id": src})
    assert r.status_code == 422 and r.json()["detail"] == "ATTRIBUTE_VALUE_PERSONAL_DATA", r.text
    assert len(h.svc.log) == lines and len(h.ledger.events) == events
    _absent(h, pii.strip_format(secret))


@pytest.mark.parametrize("secret", ["123.45.6789", "123_45_6789", "123:45:6789", "SSN123456789", "io-123-45-6789",
                                    "doc-123-45-6789", "TIN:123456789"])
def test_r9_client_approval_doc_ref_ssn_forms_are_refused(h, secret):
    ctx = h.to_proposal_sent("acme")
    lines, events = len(h.svc.log), len(h.ledger.events)
    r = h.approve(ctx, client_approval_doc_ref=secret)
    assert r.status_code == 422 and r.json()["detail"] == "ATTRIBUTE_VALUE_PERSONAL_DATA", r.text
    assert len(h.svc.log) == lines and len(h.ledger.events) == events
    _absent(h, secret)


# OOH2-PII-1 digit-cluster rule: GPS, ZIP+4 and the like moved to test_ooh2_pii1_cluster_rule.py
@pytest.mark.parametrize("text", ["v1.2.3 build 2026.10.18", "14x48 faces west, 2026-11-01 to 2026-11-30",
                                  "unit 12 45 678"])
def test_r9_ordinary_text_still_passes(h, text):
    cid, body = _unit_body(h, text)
    r = h.post(f"/tenants/acme/campaigns/{cid}/inventory", body)
    assert r.status_code == 201, r.text
    assert r.json()["vendor_text"]["personal_data_redacted"] is False, r.json()["vendor_text"]


# ---------------------------------------------------------------- F6: names_a_card as at 99569bd

def test_r9_f6_dotless_i_is_a_separator_for_the_funds_screen():
    assert sc.names_a_card("4111ı1111ı1111ı1111")
    assert pii.card_spans("4111ı1111ı1111ı1111") == [(0, 19)]
    assert pii.card_spans("4111A1111B1111C1111") == []                     # letters of either case still break a run


# ---------------------------------------------------------------- RT9-1: what start-up reads

def _started_or_refused(data_dir):
    out = {}

    def run():
        try:
            out["log"] = RecordLog(data_dir)
        except StoreCorrupt as exc:
            out["err"] = str(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    return t.is_alive(), out


def _data_dir_with(tmp_path, make):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    make(str(d / ST.LOG_NAME), tmp_path)
    return str(d)


def test_r9_rt1_start_refuses_a_fifo_without_blocking(tmp_path):
    d = _data_dir_with(tmp_path, lambda p, t: os.mkfifo(p, 0o600))
    alive, out = _started_or_refused(d)
    if alive:                                                       # unblock the thread so the run can end
        os.close(os.open(os.path.join(d, ST.LOG_NAME), os.O_WRONLY | os.O_NONBLOCK))
    os.unlink(os.path.join(d, ST.LOG_NAME))                         # r10 REL F5: leave no FIFO behind
    assert not alive, "start-up blocked on a FIFO"
    assert "not a regular file" in out.get("err", ""), out


def test_r9_rt1_start_refuses_a_symlinked_log(tmp_path):
    real = tmp_path / "real.jsonl"
    log = RecordLog(str(tmp_path / "src"))
    rec, line = log.prepare("tenant_created", "2026-10-18T09:00:00Z", {"tenant_id": "s-1", "actor": "andre"})
    log.append_prepared(rec, line)
    real.write_bytes(open(log.path, "rb").read())
    d = _data_dir_with(tmp_path, lambda p, t: os.symlink(str(real), p))
    alive, out = _started_or_refused(d)
    os.unlink(os.path.join(d, ST.LOG_NAME))
    assert not alive and "it must be a regular file, not a link" in out.get("err", ""), out


def test_r9_rt1_start_refuses_a_link_to_a_device_without_reading_it(tmp_path):
    if not os.path.exists("/dev/zero"):
        pytest.skip("no /dev/zero")
    d = _data_dir_with(tmp_path, lambda p, t: os.symlink("/dev/zero", p))
    alive, out = _started_or_refused(d)
    os.unlink(os.path.join(d, ST.LOG_NAME))
    assert not alive and "err" in out, out


def test_r9_rt1_start_refuses_a_directory(tmp_path):
    d = _data_dir_with(tmp_path, lambda p, t: os.mkdir(p))
    alive, out = _started_or_refused(d)
    assert not alive and "not a regular file" in out.get("err", ""), out


def test_r9_rt1_start_still_reads_a_regular_log(tmp_path):
    log = RecordLog(str(tmp_path / "d"))
    rec, line = log.prepare("tenant_created", "2026-10-18T09:00:00Z", {"tenant_id": "ok-1", "actor": "andre"})
    log.append_prepared(rec, line)
    again = RecordLog(str(tmp_path / "d"))
    assert again.raw_lines() == [line] and again.file_problem() is None


# ---------------------------------------------------------------- reliability F1: a failed close

def test_r9_rel1_a_failed_close_in_the_integrity_check_changes_nothing(tmp_path, monkeypatch):
    log = RecordLog(str(tmp_path / "d"))
    rec, line = log.prepare("tenant_created", "2026-10-18T09:00:00Z", {"tenant_id": "c-1", "actor": "andre"})
    log.append_prepared(rec, line)
    real_close = os.close

    def bad_close(fd):
        real_close(fd)
        raise OSError(5, "EIO")

    _fake_os(monkeypatch, close=bad_close)
    assert log.file_problem() is None
