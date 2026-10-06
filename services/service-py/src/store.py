"""
Local append-only record log, the content-addressed message-body store and the data-directory lock (ADR 0014).
Stdlib only. The record log, its pending line and its roll-forward design are security-py's exactly as fixed in
AEGIS rounds 1-5 (ADR 0012 amendments): own pending line kept in memory, a pending line found on disk appended only
when the ledger already holds its anchor (else set aside to ``pending.discarded``), an empty line refuses start,
exact-size append with adopt / truncate.

``SVC_DATA_DIR/service_log.jsonl``: one JSON object per line, ``{"seq", "kind", "at", "data",
"prev_line_sha256", "record_sha256"}``; ``prev_line_sha256`` is the SHA-256 of the previous line's exact bytes
(64 zeros first) and ``record_sha256`` the line's own hash, so an edit, deletion, reordering or torn write is
caught at start (start-up refuses). A whole-file rewrite, a truncated tail or a deleted log is caught against
the ledger instead: every line is anchored on the ledger BEFORE it is written (service.Anchor).

The pending line closes the crash window between "anchored on the ledger" and "appended here": the exact line
is fsynced to ``pending.line`` before the anchor, and removed after the append. At start a pending line is
appended if (and only if) the ledger holds its anchor and it is the next line; otherwise it is discarded.

The record log never holds a message body: bodies are stored once, content-addressed, in ``bodies/<sha256>``
(BodyStore below), and lines cite them by hash. Neither bodies nor contact details ever reach the ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from typing import Iterator, Optional

GENESIS = "0" * 64
LOG_NAME = "service_log.jsonl"


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


    # ------------------------------------------------------------------ pending line (ADR 0014, from ADR 0012 decision 7)

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
            os.write(fd, data)
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
        raise StoreWriteError(f"{what} write failed: {type(exc).__name__}") from exc


BODIES_DIR = "bodies"
_BODY_NAME = __import__("re").compile(r"[0-9a-f]{64}")


class BodyStore:
    """Message bodies (and consent texts), stored once each under the HMAC-SHA-256 (keyed: SVC_HMAC_KEY_FILE,
    AEGIS round 1 V1-M2) of their exact UTF-8 bytes; fields citing them keep the ``*_sha256`` names. A body is
    written BEFORE the log line that cites it; at start a body no live log record cites is an orphan of a failed
    write and is removed (never while a pending line may still cite it). Reading checks the hash, so an edited body
    file is never returned. Bodies never leave this service except to the caller entitled to the ticket."""

    MAX_BYTES = 64 * 1024

    def __init__(self, data_dir: Optional[str], key: bytes):
        if len(key) < 32:
            raise ValueError("BodyStore needs a key of at least 32 bytes")
        self._key = key
        self.lock = threading.RLock()
        self.dir = os.path.join(data_dir, BODIES_DIR) if data_dir else None
        self._mem: dict[str, bytes] = {}
        self.fail_next_put = False   # tests
        self.closed = False          # V5r-Info: set by the service's close(); put / delete then refuse
        if self.dir:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            for name in os.listdir(self.dir):
                if name.endswith(".tmp"):
                    os.unlink(os.path.join(self.dir, name))
                elif not _BODY_NAME.fullmatch(name):
                    raise StoreCorrupt(f"body store holds a file that is not a body: {name[:40]}")

    def digest(self, data: bytes) -> str:
        return hmac.new(self._key, data, hashlib.sha256).hexdigest()

    def put(self, text: str) -> str:
        data = text.encode("utf-8")
        if len(data) > self.MAX_BYTES:
            raise StoreWriteError("body larger than 64 KiB")
        digest = self.digest(data)
        with self.lock:
            if self.closed:
                raise StoreWriteError("this service instance is closed; its body store refuses writes")
            if self.fail_next_put:
                self.fail_next_put = False
                raise StoreWriteError("simulated body store failure")
            if not self.dir:
                self._mem[digest] = data
                return digest
            path = os.path.join(self.dir, digest)
            if self._read(path) == data:
                return digest                     # stored once
            _write_file(path, data, "body")
            return digest

    @staticmethod
    def _read(path: str) -> Optional[bytes]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            return os.read(fd, BodyStore.MAX_BYTES + 1)
        finally:
            os.close(fd)

    def get(self, digest: str) -> Optional[str]:
        if not isinstance(digest, str) or not _BODY_NAME.fullmatch(digest):
            return None
        with self.lock:
            data = self._mem.get(digest) if not self.dir else self._read(os.path.join(self.dir, digest))
        if data is None or not hmac.compare_digest(self.digest(data), digest):
            return None
        return data.decode("utf-8")

    def names(self) -> set[str]:
        with self.lock:
            if not self.dir:
                return set(self._mem)
            return {n for n in os.listdir(self.dir) if _BODY_NAME.fullmatch(n)}

    def delete(self, digest: str) -> None:
        with self.lock:
            if self.closed:
                raise StoreWriteError("this service instance is closed; its body store refuses writes")
            if not self.dir:
                self._mem.pop(digest, None)
                return
            try:
                os.unlink(os.path.join(self.dir, digest))
            except FileNotFoundError:
                return
            except OSError as exc:
                raise StoreWriteError(f"body could not be removed: {type(exc).__name__}") from exc


LOCK_NAME = "service.lock"


class DataDirBusy(StoreCorrupt):
    """Another holder has this data directory (another process's flock, or another service instance's claim)."""


BUSY = "another service-py process holds this data directory; refusing to start"


class DataDirLock:
    """One process per data directory (fcntl.flock, released by the kernel when the process ends). A second
    instance on the same directory refuses to start: two writers would fork the log.

    AEGIS round 5 (V5-L1): the flock is per PROCESS (config.load caches it); each service instance must ``claim()``
    it, once: a second claim in the same process is refused like a second process, and ``release_claim()`` (the
    service's ``close()``) gives it back. V5-I1: ``service.lock`` must be a regular file (``lstat``: a symlink, FIFO
    or directory planted there refuses with a clear message, never a raw OSError)."""

    def __init__(self, data_dir: Optional[str]):
        self._fd = None
        self.claimed = False
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
        if self.claimed:
            raise DataDirBusy("another service instance in this process already holds this data directory; refusing "
                              "to start (close the first instance)")
        import secrets
        self._token = secrets.token_hex(16)
        self.claimed = True
        return self._token

    def holds(self, token: Optional[str]) -> bool:
        """True only for the token of the CURRENT claim (AEGIS round 5c item 1)."""
        import hmac as hmac_mod
        current = getattr(self, "_token", None)
        return bool(self.claimed and token and current and hmac_mod.compare_digest(token, current))

    def release_claim(self, token: Optional[str] = None) -> bool:
        """Give the claim back. Only the current claim's token releases it: a stale or wrong token is a no-op (it
        never releases another instance's claim). Returns whether it released."""
        if not self.holds(token):
            return False
        self.claimed = False
        self._token = None
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
