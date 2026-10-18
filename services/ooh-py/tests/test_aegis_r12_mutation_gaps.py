"""AEGIS round 12 mutation gate at 1a90498 (BLOCK): survivors that no test pinned. The shipped code was right.
- N146: the stored vendor text's sha256 is the hash of the text as kept (redacted), never of the raw text;
- N156: names_a_card reads full-width digits (NFKC) for the client funds reference;
- F001/F002: the integrity check alone (no write first) sees a same-size edit of the file's first or last byte;
- F015: an unreadable log is a problem for the integrity check, never healthy;
- F008: a FIFO at the path of an empty log is a problem, not "no file yet";
- N178/N179: the request-hash cache keeps at most 64 entries and evicts the oldest."""
from __future__ import annotations

import hashlib
import os

import pytest

import service as SV
import svc_campaigns as sc
from store import RecordLog
from test_aegis_h1_file_content import DIFFERS, _disk, _three_lines
from test_aegis_integrity_path import _check, env  # noqa: F401  (env is a fixture)
from test_aegis_store_write_path import _fake_os


def test_n146_vendor_text_sha256_is_of_the_kept_text():
    raw = "my ssn is 123-45-6789"
    o = sc.observed(raw)
    assert o["personal_data_redacted"] is True and "6789" not in o["text"]
    assert o["sha256"] == hashlib.sha256(o["text"].encode("utf-8")).hexdigest()
    assert o["sha256"] != hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_n156_names_a_card_reads_full_width_digits():
    fw = "".join(chr(0xFF10 + int(c)) if c.isdigit() else c for c in "4242 4242 4242 4242")
    assert sc.names_a_card(fw) and sc.names_a_card("4242 4242 4242 4242")


@pytest.mark.parametrize("where", ["first", "last"])
def test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit(env, where):
    led, h = env
    _three_lines(h)
    raw = _disk(h)
    i = 0 if where == "first" else len(raw) - 1
    with open(h.svc.log.path, "wb") as fh:
        fh.write(raw[:i] + (b"[" if where == "first" else b" ") + raw[i + 1:])
    assert _check(h)["problem"] == DIFFERS                         # no write attempted before the check


def test_f015_an_unreadable_log_is_a_problem(env, monkeypatch):
    led, h = env
    _three_lines(h)

    def boom(fd, n, off):
        raise OSError(5, "EIO")

    _fake_os(monkeypatch, pread=boom)
    p = h.svc.log.file_problem()
    assert p == "the local log file cannot be read (OSError)", p


def test_f008_a_fifo_at_the_path_of_an_empty_log_is_a_problem(tmp_path):
    log = RecordLog(str(tmp_path / "d"))
    assert len(log) == 0 and log.file_problem() is None
    os.mkfifo(log.path, 0o600)
    try:
        assert log.file_problem() == DIFFERS
    finally:
        os.unlink(log.path)


def test_n178_n179_the_request_hash_cache_is_bounded_to_64(monkeypatch):
    monkeypatch.setattr(SV, "_SHA_CACHE", type(SV._SHA_CACHE)())
    for i in range(70):
        SV.request_sha({"request_id": f"r-{i}", "vendor_text": "ok"})
    assert len(SV._SHA_CACHE) == 64
    first = hashlib.sha256(SV.canonical({"request_id": "r-0", "vendor_text": "ok"}).encode("utf-8")).hexdigest()
    last = hashlib.sha256(SV.canonical({"request_id": "r-69", "vendor_text": "ok"}).encode("utf-8")).hexdigest()
    assert first not in SV._SHA_CACHE and last in SV._SHA_CACHE      # the oldest is evicted
