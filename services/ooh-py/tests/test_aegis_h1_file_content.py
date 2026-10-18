"""AEGIS H-1 (runtime omega7; Ω RED at 4a0a4b4): the write path compared only the log file's SIZE with memory, so a
same-size in-place edit of an existing line while the service ran was written over, writes answered 201 and the
integrity check said ok. Now:
- every write compares the whole file with the in-memory lines, byte for byte, and refuses (503, file untouched);
- every integrity check does the same (``RecordLog.file_problem``), so a changed file stops writes even before one;
- a refused write was anchored first, so once the file is put back the next check rolls it forward."""
from __future__ import annotations

import os

import pytest

from store import RecordLog, StoreWriteError
from test_aegis_integrity_path import _anchors, _check, _new_tenant, env  # noqa: F401  (env is a fixture)
from test_aegis_store_write_path import _fake_os

DIFFERS = "the local log file differs from the lines this service holds: an operator must inspect it"


def _disk(h):
    with open(h.svc.log.path, "rb") as fh:
        return fh.read()


def _same_size_edit(h, index):
    """Change one hex digit of line ``index``'s record hash in place: same size, other bytes."""
    raw = _disk(h)
    lines = raw.split(b"\n")[:-1]
    target = lines[index]
    at = target.index(b'"record_sha256":"') + len(b'"record_sha256":"')
    flipped = b"0" if target[at:at + 1] != b"0" else b"1"
    offset = sum(len(ln) + 1 for ln in lines[:index]) + at
    with open(h.svc.log.path, "r+b") as fh:
        fh.seek(offset)
        fh.write(flipped)
    edited = _disk(h)
    assert len(edited) == len(raw) and edited != raw
    return raw, edited


def _three_lines(h):
    h.ok(_new_tenant(h, "t-2"), 201)
    h.ok(_new_tenant(h, "t-3"), 201)
    assert len(h.svc.log) >= 3


@pytest.mark.parametrize("which", ["first", "middle", "last"])
def test_h1_same_size_edit_of_an_existing_line_refuses_the_next_write(env, which):
    led, h = env
    _three_lines(h)
    n = len(h.svc.log)
    index = {"first": 0, "middle": n // 2, "last": n - 1}[which]
    _, edited = _same_size_edit(h, index)
    r = _new_tenant(h, "after-edit")
    assert r.status_code == 503 and r.json() == {"detail": "STORE_UNAVAILABLE", "maybe": True}, r.text
    assert _disk(h) == edited                                     # the file is left exactly as found
    assert len(h.svc.log) == n and "after-edit" not in h.svc.tenants
    assert h.svc.integrity["ok"] is False
    assert _check(h)["ok"] is False                               # still refused at the next check
    assert _disk(h) == edited
    assert _new_tenant(h, "after-check").json()["detail"] == "INTEGRITY_UNVERIFIED"


def test_h1_integrity_check_reports_a_changed_file_before_any_write(env):
    led, h = env
    _three_lines(h)
    _, edited = _same_size_edit(h, 1)
    i = _check(h)
    assert i["ok"] is False and i["problem"] == DIFFERS, i
    r = _new_tenant(h, "blocked")
    assert r.status_code == 503 and r.json()["detail"] == "INTEGRITY_UNVERIFIED", r.text
    assert _disk(h) == edited


def test_h1_integrity_check_is_ok_on_an_unchanged_file(env):
    led, h = env
    _three_lines(h)
    assert h.svc.log.file_problem() is None
    assert _check(h)["ok"] is True


def test_h1_putting_the_file_back_rolls_the_refused_write_forward(env):
    led, h = env
    _three_lines(h)
    n = len(h.svc.log)
    original, _ = _same_size_edit(h, n - 1)
    r = _new_tenant(h, "late-1")
    assert r.status_code == 503 and r.json()["maybe"] is True, r.text
    with open(h.svc.log.path, "wb") as fh:                        # the operator restores the file
        fh.write(original)
    for _ in range(2):
        _check(h)
    assert h.svc.integrity["ok"] is True, h.svc.integrity
    assert len(h.svc.log) == n + 1 and "late-1" in h.svc.tenants  # anchored before the refusal: rolled forward
    assert len(_anchors(led)) == len(h.svc.log)
    assert _disk(h) == original + h.svc.log.raw_lines(n)[0] + b"\n"


def test_h1_adoption_needs_the_whole_file_to_match(env):
    led, h = env
    _three_lines(h)
    log = h.svc.log
    rec, line = log.prepare("tenant_created", "2026-10-18T09:00:00Z", {"tenant_id": "ad-1", "actor": "andre"})
    with open(log.path, "ab") as fh:                              # this line already on disk (an fsync error)...
        fh.write(line + b"\n")
    _, edited = _same_size_edit(h, 0)                             # ...but an earlier line was changed
    n = len(log)
    with pytest.raises(StoreWriteError, match="the log file and memory disagree"):
        log.append_prepared(rec, line)
    assert _disk(h) == edited and len(log) == n


def test_h1_adoption_still_works_when_the_file_matches(env):
    led, h = env
    log = h.svc.log
    rec, line = log.prepare("tenant_created", "2026-10-18T09:00:00Z", {"tenant_id": "ad-2", "actor": "andre"})
    with open(log.path, "ab") as fh:
        fh.write(line + b"\n")
    want = _disk(h)
    log.append_prepared(rec, line)
    assert _disk(h) == want and log.raw_lines(len(log) - 1) == [line]


def test_h1_adoption_refuses_another_line_of_the_same_size(env):
    led, h = env
    log = h.svc.log
    rec, line = log.prepare("tenant_created", "2026-10-18T09:00:00Z", {"tenant_id": "ad-3", "actor": "andre"})
    other = line[:-2] + (b"0" if line[-2:-1] != b"0" else b"1") + line[-1:]
    with open(log.path, "ab") as fh:
        fh.write(other + b"\n")
    want = _disk(h)
    n = len(log)
    with pytest.raises(StoreWriteError, match="the log file and memory disagree"):
        log.append_prepared(rec, line)
    assert _disk(h) == want and len(log) == n


def test_h1_deleted_log_file_is_reported_and_refused(env):
    led, h = env
    _three_lines(h)
    n = len(h.svc.log)
    os.unlink(h.svc.log.path)
    i = _check(h)
    assert i["ok"] is False and i["problem"] == DIFFERS, i
    rec, line = h.svc.log.prepare("tenant_created", "2026-10-18T09:00:00Z", {"tenant_id": "del-1", "actor": "andre"})
    with pytest.raises(StoreWriteError, match="the log file and memory disagree"):
        h.svc.log.append_prepared(rec, line)
    assert _disk(h) == b"" and len(h.svc.log) == n                # nothing written into the re-created file


def test_h1_unreadable_log_file_is_reported(env):
    led, h = env
    path = h.svc.log.path
    os.chmod(path, 0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that reads mode-0 files (root)")
        i = _check(h)
        assert i["ok"] is False and i["problem"] == "the local log file cannot be read (PermissionError)", i
    finally:
        os.chmod(path, 0o600)


def test_h1_no_file_yet_and_an_in_memory_log_have_no_file_problem(tmp_path):
    assert RecordLog(str(tmp_path / "fresh")).file_problem() is None      # no line written: no file is right
    assert RecordLog(None).file_problem() is None


def test_h1_short_reads_are_completed(env, monkeypatch):
    led, h = env
    _three_lines(h)
    real_pread = os.pread

    def one_byte(fd, n, offset):
        return real_pread(fd, min(n, 1), offset)

    _fake_os(monkeypatch, pread=one_byte)
    h.ok(_new_tenant(h, "slow-read"), 201)
    monkeypatch.undo()
    assert "slow-read" in h.svc.tenants and h.svc.log.file_problem() is None
