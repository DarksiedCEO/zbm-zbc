"""
Local append-only record log and the data-directory lock (spec §B, ADR 0006 decisions 4-5 pattern; ADR 0009 amendment
"bug sweep Oct 6 2026"). Stdlib only. The append path and the lock are backported from clientfix-py's ``store.py``
(itself service-py's / security-py's, AEGIS rounds 1-5): exact-size append with adopt / truncate, ``O_NOFOLLOW``,
an empty line refuses start, a closed instance refuses every write, and the flock with a single-use adopt token and
a mutex (sweep F-3/E-5: before this, ONE fsync error left a line on disk that memory did not hold, and every later
append, and every restart, failed: the log was bricked for good).

``FIN_DATA_DIR/fin_log.jsonl``: one JSON object per line,
``{"seq", "kind", "at", "data", "prev_line_sha256", "record_sha256"}``, where ``prev_line_sha256`` is the SHA-256 of
the previous line's exact bytes (the first line carries 64 zeros) and ``record_sha256`` is the line's own hash
(without that field), so an edit of the LAST line is caught too. The chain detects edits, deletions, reordering and
torn writes; a whole-file rewrite with recomputed hashes, a truncated tail or a deleted log is caught against the
ledger instead: the service anchors every line (``prepare`` gives the exact line before it is written) on the ledger
first (AEGIS N14-4, ``intelligences/i10_evidence_audit.anchor_problems``). Every append is fsynced before the caller
lets the record take effect. At start the whole chain is verified; any mismatch refuses start-up.

Without a data dir the log lives in memory (``in_memory = True``, reported by /health) and nothing survives a
restart -- after a restart no rule version is in force (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Iterator, Optional

GENESIS = "0" * 64
LOG_NAME = "fin_log.jsonl"


class StoreCorrupt(RuntimeError):
    pass


class StoreWriteError(RuntimeError):
    pass


def _line_sha(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


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
        self.closed = False            # V5r-Info: set by the service's close(); every write then refuses
        if data_dir:
            os.makedirs(data_dir, mode=0o700, exist_ok=True)
            self.path = os.path.join(data_dir, LOG_NAME)
            if os.path.exists(self.path):
                with open(self.path, "rb") as fh:
                    raw = fh.read()
                if raw and not raw.endswith(b"\n"):
                    raise StoreCorrupt("log ends with a torn line; refusing to start (inspect the file)")
                parts = raw.split(b"\n")[:-1] if raw else []
                if any(not ln for ln in parts):
                    raise StoreCorrupt(f"log has an empty line (line {parts.index(b'') + 1}); refusing to start: "
                                       "remove it (the service never writes one)")
                self._lines = parts
                verify_lines(self._lines)

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
            rec = {"seq": len(self._lines) + 1, "kind": kind, "at": at, "data": data, "prev_line_sha256": prev}
            rec["record_sha256"] = hashlib.sha256(encode(rec)).hexdigest()
            return rec, encode(rec)

    def append(self, kind: str, at: str, data: dict) -> dict:
        rec, line = self.prepare(kind, at, data)
        return self.append_prepared(rec, line)

    def _refuse_if_closed(self) -> None:
        if self.closed:
            raise StoreWriteError("this service instance is closed; its log refuses writes")

    def append_prepared(self, rec: dict, line: bytes) -> dict:
        """Append one line. The file must be exactly the in-memory lines before the write (else refused); a
        failed write is cut back to that size, so a line is never half in (AEGIS round 3, R3-3). If the file
        already ends with exactly this line (a write that reached the disk before an fsync error), it is adopted
        instead of written twice."""
        with self.lock:
            self._refuse_if_closed()        # inside the lock: close() takes it too (round 5c item 2)
            if self.fail_next_append:
                self.fail_next_append = False
                raise StoreWriteError("simulated local store failure")
            if rec["seq"] != len(self._lines) + 1:
                raise StoreWriteError("log moved on since the line was prepared")
            if self.path:
                expected = sum(len(ln) + 1 for ln in self._lines)
                try:
                    fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                except OSError as exc:
                    raise StoreWriteError(f"local log open failed: {type(exc).__name__}") from exc
                try:
                    size = os.fstat(fd).st_size
                    if size == expected + len(line) + 1 and os.pread(fd, len(line) + 1, expected) == line + b"\n":
                        os.fsync(fd)                  # already on disk: adopt it
                    elif size != expected:
                        raise StoreWriteError("the log file and memory disagree; refusing to write")
                    else:
                        try:
                            os.pwrite(fd, line + b"\n", expected)
                            os.fsync(fd)
                        except OSError as exc:
                            try:
                                os.ftruncate(fd, expected)
                                os.fsync(fd)
                            except OSError:
                                pass
                            raise StoreWriteError(f"local log write failed: {type(exc).__name__}") from exc
                except OSError as exc:
                    raise StoreWriteError(f"local log write failed: {type(exc).__name__}") from exc
                finally:
                    os.close(fd)
            self._lines.append(line)
            return rec

    def verify(self) -> bool:
        """Re-verify the chain as stored (the file, when there is one)."""
        with self.lock:
            try:
                if self.path:
                    with open(self.path, "rb") as fh:
                        raw = fh.read()
                    if raw and not raw.endswith(b"\n"):
                        return False
                    lines = raw.split(b"\n")[:-1] if raw else []
                    if len(lines) != len(self._lines) or any(not ln for ln in lines):   # R4-2: no empty line
                        return False
                else:
                    lines = self._lines
                verify_lines(lines)
                return True
            except (OSError, StoreCorrupt):
                return False


LOCK_NAME = "fin.lock"


class DataDirBusy(StoreCorrupt):
    """Another holder has this data directory (another process's flock, or another service instance's claim)."""


BUSY = "another finance-py process holds this data directory; refusing to start"


class DataDirLock:
    """One process per data directory (fcntl.flock, released by the kernel when the process ends). A second
    instance on the same directory refuses to start: two writers would fork the log.

    AEGIS round 5 (V5-L1): the flock is per PROCESS (config.load caches it); each service instance must ``claim()``
    it, once: a second claim in the same process is refused like a second process, and ``release_claim()`` (the
    service's ``close()``) gives it back. V5-I1: ``fin.lock`` must be a regular file (``lstat``: a symlink, FIFO
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
