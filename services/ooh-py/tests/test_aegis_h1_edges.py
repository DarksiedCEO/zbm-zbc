"""AEGIS round 8 at 99569bd (mutation BLOCK, test-verification BLOCK, security M-1): edges of the H-1 whole-file compare
that no test pinned, and the integrity check's read of the file.
- the compare covers the file's first and last byte (X016, X017);
- adoption needs this line AND its newline (X011), fsyncs the adopted line (M092/M093), writes nothing again (M091),
  and completes short reads of the tail (M080);
- the integrity check reads the file of an empty log too (X024);
- ``_pread_exact`` stops at the end of the file (M011/M013/X008);
- the log file is created 0600 (M044);
- the integrity check never follows a symlink, never blocks on a FIFO, never reads a device, and compares the size
  before reading (security M-1, L-2)."""
from __future__ import annotations

import os
import stat
import threading

import pytest

import store as ST
from store import RecordLog, StoreWriteError, _pread_exact
from test_aegis_h1_file_content import DIFFERS, _disk, _three_lines
from test_aegis_integrity_path import _check, _new_tenant, env  # noqa: F401  (env is a fixture)
from test_aegis_store_write_path import _fake_os


def _write(h, raw: bytes) -> None:
    with open(h.svc.log.path, "wb") as fh:
        fh.write(raw)


# ---------------------------------------------------------------- the first and the last byte (X016, X017)

@pytest.mark.parametrize("where", ["first", "last"])
def test_edges_a_same_size_edit_of_the_files_first_or_last_byte_is_refused(env, where):
    led, h = env
    _three_lines(h)
    raw = _disk(h)
    i = 0 if where == "first" else len(raw) - 1
    edited = raw[:i] + (b"[" if where == "first" else b" ") + raw[i + 1:]
    assert len(edited) == len(raw) and edited != raw
    _write(h, edited)
    n = len(h.svc.log)
    r = _new_tenant(h, f"edge-{where}")
    assert r.status_code == 503 and r.json() == {"detail": "STORE_UNAVAILABLE", "maybe": True}, r.text
    assert _disk(h) == edited and len(h.svc.log) == n
    assert _check(h)["ok"] is False and _disk(h) == edited       # the anchored line cannot roll forward over it either


# ---------------------------------------------------------------- adoption (X011, M092/M093, M091, M080)

def _prepared(h, tid):
    return h.svc.log.prepare("tenant_created", "2026-10-18T09:00:00Z", {"tenant_id": tid, "actor": "andre"})


def test_edges_adoption_refuses_this_line_without_its_newline(env):
    led, h = env
    log = h.svc.log
    rec, line = _prepared(h, "nl-1")
    with open(log.path, "ab") as fh:
        fh.write(line + b"x")                                    # the right size, but not the line and its newline
    want, n = _disk(h), len(log)
    with pytest.raises(StoreWriteError, match="the log file and memory disagree"):
        log.append_prepared(rec, line)
    assert _disk(h) == want and len(log) == n


def _spy(monkeypatch, h):
    calls = []
    real = {k: getattr(os, k) for k in ("open", "fsync", "pwrite")}
    fds = {}

    def opener(path, *a, **k):
        fd = real["open"](path, *a, **k)
        fds[fd] = os.fspath(path)
        return fd

    def fsync(fd):
        calls.append(("fsync", fds.get(fd)))
        return real["fsync"](fd)

    def pwrite(fd, data, offset):
        calls.append(("pwrite", fds.get(fd)))
        return real["pwrite"](fd, data, offset)

    _fake_os(monkeypatch, open=opener, fsync=fsync, pwrite=pwrite)
    return calls


def test_edges_adoption_fsyncs_the_file_and_writes_nothing_again(env, monkeypatch):
    led, h = env
    log = h.svc.log
    rec, line = _prepared(h, "ad-fs")
    with open(log.path, "ab") as fh:
        fh.write(line + b"\n")
    want = _disk(h)
    calls = _spy(monkeypatch, h)
    log.append_prepared(rec, line)
    monkeypatch.undo()
    assert ("fsync", log.path) in calls                          # the adopted line is made durable before the answer
    assert ("pwrite", log.path) not in calls                     # and never written a second time
    assert _disk(h) == want and log.raw_lines(len(log) - 1) == [line]


def test_edges_adoption_completes_short_reads_of_the_tail(env, monkeypatch):
    led, h = env
    _three_lines(h)
    log = h.svc.log
    rec, line = _prepared(h, "ad-short")
    with open(log.path, "ab") as fh:
        fh.write(line + b"\n")
    real_pread = os.pread
    _fake_os(monkeypatch, pread=lambda fd, n, off: real_pread(fd, min(n, 7), off))
    log.append_prepared(rec, line)
    monkeypatch.undo()
    assert log.raw_lines(len(log) - 1) == [line] and log.file_problem() is None


# ---------------------------------------------------------------- the file of an empty log (X024)

def test_edges_integrity_check_reads_the_file_of_an_empty_log(tmp_path):
    from helpers import Harness
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"))
    try:
        assert len(h.svc.log) == 0 and h.svc.integrity["ok"] is True
        _write(h, b"junk\n")
        assert _check(h)["problem"] == DIFFERS
    finally:
        h.svc.close()
        if h.settings.data_dir_lock is not None:
            h.settings.data_dir_lock.release()


# ---------------------------------------------------------------- _pread_exact stops at the end (M011/M013/X008)

def test_edges_pread_exact_stops_at_the_end_of_the_file(tmp_path, monkeypatch):
    p = tmp_path / "f"
    p.write_bytes(b"abcdef")
    fd = os.open(p, os.O_RDONLY)
    try:
        assert _pread_exact(fd, 100, 0) == b"abcdef"             # asked for more than there is: what there is
        assert _pread_exact(fd, 100, 4) == b"ef"
        assert _pread_exact(fd, 3, 6) == b""
        assert _pread_exact(fd, 0, 0) == b""
        calls = []
        real_pread = os.pread

        def counting(fd_, n, off):
            calls.append(n)
            assert len(calls) < 50, "pread called again and again at the end of the file"
            return real_pread(fd_, min(n, 2), off)

        _fake_os(monkeypatch, pread=counting)
        assert _pread_exact(fd, 100, 0) == b"abcdef"             # short reads completed, then the end stops it
        assert len(calls) == 4
    finally:
        os.close(fd)


def test_edges_a_file_that_shrinks_after_fstat_is_refused_not_spun_on(env, monkeypatch):
    led, h = env
    _three_lines(h)
    log = h.svc.log
    real_pread = os.pread
    state = {"cut": False}

    def shrinking(fd, n, off):
        if not state["cut"]:
            state["cut"] = True
            with open(log.path, "r+b") as fh:                     # someone truncates the file mid-compare
                fh.truncate(10)
        return real_pread(fd, n, off)

    _fake_os(monkeypatch, pread=shrinking)
    done = {}

    def run():
        try:
            log.append("tenant_created", "2026-10-18T09:00:00Z", {"tenant_id": "shrink", "actor": "andre"})
        except StoreWriteError as exc:
            done["err"] = str(exc)

    t = threading.Thread(target=run)
    t.start()
    t.join(10)
    assert not t.is_alive(), "the compare did not stop at the end of the shrunk file"
    assert "the log file and memory disagree" in done.get("err", "")


# ---------------------------------------------------------------- created 0600 (M044)

def test_edges_the_log_file_is_created_owner_only(tmp_path):
    log = RecordLog(str(tmp_path / "d"))
    rec, line = log.prepare("tenant_created", "2026-10-18T09:00:00Z", {"tenant_id": "m-1", "actor": "andre"})
    old = os.umask(0)
    try:
        log.append_prepared(rec, line)
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(log.path).st_mode) == 0o600


# ---------------------------------------------------------------- what the integrity check reads (security M-1, L-2)

def _swap(h, make):
    path = h.svc.log.path
    keep = path + ".keep"
    os.replace(path, keep)
    make(path, keep)
    return keep


def test_edges_integrity_check_does_not_block_on_a_fifo(env):
    led, h = env
    _three_lines(h)
    keep = _swap(h, lambda path, keep: os.mkfifo(path, 0o600))
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("i", _check(h)))
    t.start()
    t.join(10)
    try:
        assert not t.is_alive(), "the integrity check blocked on a FIFO"
        assert out["i"]["ok"] is False and out["i"]["problem"] == DIFFERS, out
        r = _new_tenant(h, "fifo-w")
        assert r.status_code == 503, r.text
    finally:
        if t.is_alive():                                          # unblock it so the test run can end
            fd = os.open(h.svc.log.path, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        os.unlink(h.svc.log.path)
        os.replace(keep, h.svc.log.path)


def test_edges_integrity_check_does_not_follow_a_symlink_even_to_identical_bytes(env):
    led, h = env
    _three_lines(h)
    _swap(h, lambda path, keep: os.symlink(keep, path))
    i = _check(h)
    assert i["ok"] is False and i["problem"].startswith("the local log file cannot be read"), i
    r = _new_tenant(h, "link-w")
    assert r.status_code == 503, r.text                          # the write path refuses a symlink too (O_NOFOLLOW)


def test_edges_integrity_check_does_not_read_a_device(env):
    led, h = env
    _three_lines(h)
    if not os.path.exists("/dev/zero"):
        pytest.skip("no /dev/zero")
    _swap(h, lambda path, keep: os.symlink("/dev/zero", path))
    i = _check(h)
    assert i["ok"] is False, i


def test_edges_integrity_check_compares_the_size_before_reading(env):
    import tracemalloc
    led, h = env
    _three_lines(h)
    with open(h.svc.log.path, "ab") as fh:
        fh.truncate(256 << 20)                                    # a sparse 256 MiB file
    tracemalloc.start()
    try:
        assert _check(h)["problem"] == DIFFERS
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < (16 << 20), peak                                # refused on its size, not read into memory
