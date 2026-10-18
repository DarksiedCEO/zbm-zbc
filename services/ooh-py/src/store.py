"""
Local append-only record log and the data-directory lock (ADR 0019; seo-py's, ADR 0017, itself bizdev-py's, ADR
0016). Stdlib only. Copied from services/seo-py/src/store.py (originally services/service-py/src/store.py (itself security-py's as fixed in AEGIS rounds 1-5, ADR 0012 amendments, and
service-py's rounds 1-5e): own pending line kept in memory, a pending line found on disk appended only when the
ledger already holds its anchor (else set aside to ``pending.discarded``), an empty line refuses start, exact-size
append with adopt / truncate, a closed instance refuses every write, and the flock with a single-use adopt token
and a mutex); since the AEGIS H-1 fix (Oct 18 2026) it differs from seo-py's: the whole file is compared with memory
before every write and at every integrity check, and ``verify()`` is replaced by ``file_problem()``. This department
stores no document bodies outside the log (vendor text is kept bounded, in the line).

``OOH_DATA_DIR/ooh_log.jsonl``: one JSON object per line, ``{"seq", "kind", "at", "data",
"prev_line_sha256", "record_sha256"}``; ``prev_line_sha256`` is the SHA-256 of the previous line's exact bytes
(64 zeros first) and ``record_sha256`` the line's own hash, so an edit, deletion, reordering or torn write is
caught at start (start-up refuses). While the service runs, the file must stay exactly the lines it holds: every
write compares the whole file with them first, and so does every integrity check that runs (AEGIS H-1; an unforced
check returns the last result while it is ok, and a forced one runs at most every 10 s, so between checks it is the
next write that refuses). A whole-file rewrite, a truncated tail or a deleted log is caught against
the ledger instead: every line is anchored on the ledger BEFORE it is written (service._commit).

The pending line closes the crash window between "anchored on the ledger" and "appended here": the exact line
is fsynced to ``pending.line`` before the anchor, and removed after the append.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from typing import Iterator, Optional

GENESIS = "0" * 64
LOG_NAME = "ooh_log.jsonl"
# AEGIS R2-L3: every line carries the log format it was written in. A log of another format refuses start with a
# reason (StoreCorrupt), never a KeyError from a replay handler. There is no migration: the service is unreleased, so
# an older log is discarded (start from an empty data directory), not converted (ADR 0019).
# Format 3 (Oct 16 2026 round 2): a format-2 log can hold agent-made HOLD_CONFIRMED moves, which only Andre may now
# make; replaying one would bring such a hold back (AEGIS step 2 L2), so format 2 refuses start like any other.
# Format 4 (OOH-2..4 review): face keys fold leading zeros and object ids include the caller; a format-3 log's ids and
# keys would not match what this build derives, so it refuses start too.
FORMAT_VERSION = 4


class StoreCorrupt(RuntimeError):
    pass


class StoreWriteError(RuntimeError):
    pass


class _ShortWrite(OSError):
    """``pwrite`` / ``write`` wrote fewer bytes than asked (bug sweep E; finance-py AEGIS 5a56a3a M1)."""


def _line_sha(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _pread_exact(fd: int, n: int, offset: int) -> bytes:
    """Up to ``n`` bytes at ``offset``; fewer only at the end of the file (``pread`` may return a short read)."""
    chunks = []
    while n > 0:
        b = os.pread(fd, n, offset)
        if not b:
            break
        chunks.append(b)
        n -= len(b)
        offset += len(b)
    return b"".join(chunks)


def encode(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def verify_lines(lines: list[bytes]) -> list[dict]:
    """Verify a whole chain; returns the decoded records or raises StoreCorrupt."""
    prev = GENESIS
    out = []
    for i, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except ValueError as exc:
            raise StoreCorrupt(f"log line {i + 1} is not JSON") from exc
        if not isinstance(rec, dict) or rec.get("prev_line_sha256") != prev or rec.get("seq") != i + 1:
            raise StoreCorrupt(f"log chain broken at line {i + 1}")
        body = {k: v for k, v in rec.items() if k != "record_sha256"}
        if rec.get("record_sha256") != hashlib.sha256(encode(body)).hexdigest():
            raise StoreCorrupt(f"log line {i + 1} does not match its own hash")
        if encode(rec) != line:
            raise StoreCorrupt(f"log line {i + 1} is not in canonical form")
        if rec.get("fmt") != FORMAT_VERSION:
            found = rec.get("fmt")
            what = "has no log format (written before format 2)" if found is None else \
                "is in an unknown log format" if not isinstance(found, int) else f"is in log format {found}"
            raise StoreCorrupt(f"log line {i + 1} {what}; this build reads log format {FORMAT_VERSION} only and has no "
                               "migration (the service is unreleased): move the old data directory aside and start "
                               "from an empty one")
        prev = _line_sha(line)
        out.append(rec)
    return out


class RecordLog:
    def __init__(self, data_dir: Optional[str]):
        self.lock = threading.RLock()
        self.data_dir = data_dir
        self.in_memory = not data_dir
        self._lines: list[bytes] = []
        self.path: Optional[str] = None
        self.fail_next_append = False  # tests: simulate a disk failure
        self.fault: Optional[str] = None   # bug sweep E: a failed write could not be cut back (fail closed)
        self.closed = False            # V5r-Info: set by the service's close(); every write then refuses
        if data_dir:
            os.makedirs(data_dir, mode=0o700, exist_ok=True)
            self.path = os.path.join(data_dir, LOG_NAME)
            raw = self._read_at_start()
            if raw is not None:
                if raw and not raw.endswith(b"\n"):
                    raise StoreCorrupt("log ends with a torn line; refusing to start (inspect the file)")
                parts = raw.split(b"\n")[:-1] if raw else []
                if any(not ln for ln in parts):
                    raise StoreCorrupt(f"log has an empty line (line {parts.index(b'') + 1}); refusing to start: "
                                       "remove it (the service never writes one)")
                self._lines = parts
                verify_lines(self._lines)

    def _read_at_start(self) -> Optional[bytes]:
        """The log's bytes, or None when there is none yet. Like the write path and the integrity check (AEGIS r9
        runtime RT9-1): never through a symlink, never blocking on a FIFO, never a device."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreCorrupt(f"the log cannot be opened ({type(exc).__name__}); refusing to start: it must be a "
                               "regular file, not a link") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise StoreCorrupt("the log is not a regular file; refusing to start (inspect the data directory)")
            return _pread_exact(fd, st.st_size, 0)
        except OSError as exc:
            raise StoreCorrupt(f"the log cannot be read ({type(exc).__name__}); refusing to start") from exc
        finally:
            _close_quietly(fd)

    @property
    def records(self) -> list[dict]:
        with self.lock:
            return [json.loads(ln) for ln in self._lines]

    def __len__(self) -> int:
        return len(self._lines)

    def iter_records(self, start_seq: int = 1) -> Iterator[dict]:
        with self.lock:
            lines = list(self._lines[max(0, start_seq - 1):])
        for ln in lines:
            yield json.loads(ln)

    def raw_lines(self, start: int = 0) -> list[bytes]:
        """The raw lines from index ``start`` on (no parsing: cheap enough to take under the service lock)."""
        with self.lock:
            return list(self._lines[start:])

    @property
    def epoch(self) -> Optional[str]:
        """Identity of THIS log: the first 16 hex of its first line's SHA-256 (None while empty).
        Ledger anchors carry it, so another log's anchors are never mistaken for ours."""
        with self.lock:
            return _line_sha(self._lines[0])[:16] if self._lines else None

    def line_shas(self) -> list[str]:
        with self.lock:
            return [_line_sha(ln) for ln in self._lines]

    def prepare(self, kind: str, at: str, data: dict) -> tuple[dict, bytes]:
        """The exact next line, without writing it (so it can be anchored on the ledger first)."""
        with self.lock:
            prev = _line_sha(self._lines[-1]) if self._lines else GENESIS
            rec = {"seq": len(self._lines) + 1, "kind": kind, "at": at, "data": data, "prev_line_sha256": prev,
                   "fmt": FORMAT_VERSION}
            rec["record_sha256"] = hashlib.sha256(encode(rec)).hexdigest()
            return rec, encode(rec)

    def append(self, kind: str, at: str, data: dict) -> dict:
        rec, line = self.prepare(kind, at, data)
        return self.append_prepared(rec, line)

    def _refuse_if_closed(self) -> None:
        if self.closed:
            raise StoreWriteError("this service instance is closed; its log refuses writes")

    def _memory_bytes(self) -> bytes:
        return b"".join(ln + b"\n" for ln in self._lines)

    def append_prepared(self, rec: dict, line: bytes) -> dict:
        """Append one line. The file must be exactly the in-memory lines before the write, byte for byte (else
        refused and nothing written: AEGIS H-1, a same-size edit of an existing line is refused too; a deleted file is
        re-created empty by the open, nothing more); the file must be a regular file (AEGIS r8 M-1); a failed write
        is cut back to that size, so a line is never half in (AEGIS round 3, R3-3). If the file already holds exactly
        the in-memory lines followed by this line (a write that reached the disk before an fsync error), it is
        adopted instead of written twice. The whole file is read before every write: the cost grows with the log."""
        with self.lock:
            self._refuse_if_closed()        # inside the lock: close() takes it too (round 5c item 2)
            if self.fail_next_append:
                self.fail_next_append = False
                raise StoreWriteError("simulated local store failure")
            if rec["seq"] != len(self._lines) + 1:
                raise StoreWriteError("log moved on since the line was prepared")
            if self.fault:
                raise StoreWriteError(f"LOCAL_LOG_WRITE_FAULT: {self.fault}")
            if self.path:
                mem = self._memory_bytes()
                expected = len(mem)
                try:
                    fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)   # r9 REL F2
                except OSError as exc:
                    raise StoreWriteError(f"local log open failed: {type(exc).__name__}") from exc
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):        # AEGIS r8 security M-1: never a FIFO or a device
                        raise StoreWriteError("the log file and memory disagree; refusing to write")
                    size = st.st_size
                    adopt = size == expected + len(line) + 1
                    if not (adopt or size == expected) or _pread_exact(fd, expected, 0) != mem or \
                            (adopt and _pread_exact(fd, len(line) + 1, expected) != line + b"\n"):
                        raise StoreWriteError("the log file and memory disagree; refusing to write")
                    if adopt:
                        os.fsync(fd)                  # already on disk: adopt it
                    else:
                        payload = line + b"\n"
                        try:
                            written = os.pwrite(fd, payload, expected)
                            if written != len(payload):
                                # bug sweep E (finance-py AEGIS 5a56a3a M1): a short write (disk full, quota, a signal)
                                # would leave a partial line that memory does not hold. Fail closed: cut back.
                                raise _ShortWrite(f"short write: {written} of {len(payload)} bytes")
                            os.fsync(fd)
                        except OSError as exc:
                            try:
                                os.ftruncate(fd, expected)
                                os.fsync(fd)
                            except OSError as texc:
                                # the partial bytes may still be on disk: every later append refuses (fail closed),
                                # and says why so Andre sees it (integrity LOCAL_LOG_WRITE_FAULT, status view)
                                self.fault = (f"a failed log write could not be cut back ({type(exc).__name__}, then "
                                              f"{type(texc).__name__} on truncate): the log refuses writes until the "
                                              f"file is inspected and its torn tail removed (line {rec['seq']})")
                            detail = f" ({exc})" if isinstance(exc, _ShortWrite) else ""
                            raise StoreWriteError(f"local log write failed: {type(exc).__name__}{detail}") from exc
                except OSError as exc:
                    raise StoreWriteError(f"local log write failed: {type(exc).__name__}") from exc
                finally:
                    os.close(fd)
            self._lines.append(line)
            return rec

    def file_problem(self) -> Optional[str]:
        """None when the log file holds exactly the in-memory lines, else why not (AEGIS H-1: the integrity check
        reads this, so a file changed under a running service is reported, not only refused at the next write)."""
        with self.lock:
            if not self.path:
                return None
            mem = self._memory_bytes()
            differs = "the local log file differs from the lines this service holds: an operator must inspect it"
            # AEGIS r8 security M-1: like the write path, never follow a symlink, never block on a FIFO, never read a
            # device or a file of another size (the read happens under the service lock)
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                return differs if mem else None   # no line written yet: no file (a deleted file with lines differs)
            except OSError as exc:
                return f"the local log file cannot be read ({type(exc).__name__})"
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_size != len(mem) or _pread_exact(fd, len(mem), 0) != mem:
                    return differs
                return None
            except OSError as exc:
                return f"the local log file cannot be read ({type(exc).__name__})"
            finally:
                _close_quietly(fd)                # r9 REL F1: a failed close of a read-only fd changes nothing


    # ------------------------------------------------------------------ pending line (ADR 0012 decision 7)

    @property
    def pending_path(self) -> Optional[str]:
        return os.path.join(self.data_dir, PENDING_NAME) if self.data_dir else None

    _mem_pending: Optional[bytes] = None

    def write_pending(self, line: bytes) -> None:
        """Fsync the exact next line aside before its ledger anchor is recorded."""
        with self.lock:
            self._refuse_if_closed()
            if not self.data_dir:
                self._mem_pending = line
                return
            _write_file(self.pending_path, line, "pending line")

    def clear_pending(self) -> None:
        """Remove the pending line. A closed instance refuses: it must never delete a file the instance that now
        owns the data directory wrote (round 5c item 2)."""
        with self.lock:
            self._refuse_if_closed()
            self._mem_pending = None
            if self.data_dir:
                try:
                    os.unlink(self.pending_path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise StoreWriteError(f"pending line could not be removed: {type(exc).__name__}") from exc

    # a pending line found at start whose anchor is not on the ledger is kept aside, not trusted (AEGIS R4-1):
    # if the ledger later shows its anchor (it was in flight when the process stopped), it is appended then

    _mem_discarded: Optional[bytes] = None

    def write_discarded(self, line: bytes) -> None:
        with self.lock:
            self._refuse_if_closed()
            if not self.data_dir:
                self._mem_discarded = line
                return
            _write_file(os.path.join(self.data_dir, DISCARDED_NAME), line, "discarded line")

    def read_discarded(self) -> Optional[bytes]:
        if not self.data_dir:
            return self._mem_discarded
        try:
            with open(os.path.join(self.data_dir, DISCARDED_NAME), "rb") as fh:
                return fh.read(PENDING_MAX_BYTES) or None
        except FileNotFoundError:
            return None

    def clear_discarded(self) -> None:
        with self.lock:
            self._refuse_if_closed()        # a closed instance never deletes the live instance's file (round 5c)
            self._mem_discarded = None
            if self.data_dir:
                try:
                    os.unlink(os.path.join(self.data_dir, DISCARDED_NAME))
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise StoreWriteError(f"discarded line could not be removed: {type(exc).__name__}") from exc

    def read_pending(self) -> Optional[bytes]:
        if not self.data_dir:
            return self._mem_pending
        try:
            with open(self.pending_path, "rb") as fh:
                data = fh.read(PENDING_MAX_BYTES)
        except FileNotFoundError:
            return None
        return data or None


PENDING_NAME = "pending.line"
DISCARDED_NAME = "pending.discarded"
PENDING_MAX_BYTES = 16 * 1024 * 1024    # far above the largest line a route can produce (security-py AEGIS L5)


def _write_file(path: str, data: bytes, what: str) -> None:
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            written = os.write(fd, data)
            if written != len(data):     # bug sweep E: a short write never becomes the file
                raise _ShortWrite(f"short write: {written} of {len(data)} bytes")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as exc:
        try:
            os.unlink(tmp)               # never leave a partial temp file behind
        except OSError:
            pass
        raise StoreWriteError(f"{what} write failed: {type(exc).__name__}") from exc


LOCK_NAME = "ooh.lock"


class DataDirBusy(StoreCorrupt):
    """Another holder has this data directory (another process's flock, or another service instance's claim)."""


BUSY = "another ooh-py process holds this data directory; refusing to start"


class DataDirLock:
    """One process per data directory (fcntl.flock, released by the kernel when the process ends). A second
    instance on the same directory refuses to start: two writers would fork the log.

    AEGIS round 5 (V5-L1): the flock is per PROCESS (config.load caches it); each service instance must ``claim()``
    it, once: a second claim in the same process is refused like a second process, and ``release_claim()`` (the
    service's ``close()``) gives it back. V5-I1: ``ooh.lock`` must be a regular file (``lstat``: a symlink, FIFO
    or directory planted there refuses with a clear message, never a raw OSError)."""

    def __init__(self, data_dir: Optional[str]):
        self._fd = None
        self.claimed = False
        self._token: Optional[str] = None
        self._adopted = False
        self._mutex = threading.Lock()   # AEGIS a5dd261 L3: claim / holds / adopt / release_claim are atomic
        if not data_dir:
            return
        import fcntl
        import stat as stat_mod
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        path = os.path.join(data_dir, LOCK_NAME)
        not_regular = StoreCorrupt(f"{LOCK_NAME} in the data directory is not a regular file (a symlink, FIFO, device "
                                   "or directory); refusing to start: remove it (the service creates it)")
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            st = None
        if st is not None and not stat_mod.S_ISREG(st.st_mode):
            raise not_regular
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError:
            raise not_regular from None
        if not stat_mod.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise not_regular
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise DataDirBusy(BUSY)
        self._fd = fd

    def claim(self) -> Optional[str]:
        """Claim the directory for one service instance; returns the claim's ownership token (None when there is no
        data directory). Only the holder of the token can release the claim or hand it to a service."""
        if self._fd is None:
            return None
        import secrets
        with self._mutex:
            if self.claimed:
                raise DataDirBusy("another service instance in this process already holds this data directory; "
                                  "refusing to start (close the first instance)")
            self._token = secrets.token_hex(16)
            self._adopted = False
            self.claimed = True
            return self._token

    def _holds(self, token: Optional[str]) -> bool:
        import hmac as hmac_mod
        current = self._token
        return bool(self.claimed and token and current and hmac_mod.compare_digest(token, current))

    def holds(self, token: Optional[str]) -> bool:
        """True only for the token of the CURRENT claim (AEGIS round 5c item 1)."""
        with self._mutex:
            return self._holds(token)

    def adopt(self, token: Optional[str]) -> Optional[str]:
        """Hand the current claim to ONE service instance: only for the current claim's token, and only once
        (AEGIS a5dd261 L4: a second service handed the same token is refused). Returns a NEW token that only the
        adopting service holds; the claimer's token stops working, so it can no longer release the adopted claim
        (AEGIS cc27b69 Info). None when refused."""
        import secrets
        with self._mutex:
            if self._adopted or not self._holds(token):
                return None
            self._adopted = True
            self._token = secrets.token_hex(16)
            return self._token

    def release_claim(self, token: Optional[str] = None) -> bool:
        """Give the claim back. Only the current claim's token releases it: a stale or wrong token is a no-op (it
        never releases another instance's claim). Returns whether it released."""
        with self._mutex:
            if not self._holds(token):
                return False
            self.claimed = False
            self._token = None
            self._adopted = False
            return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
