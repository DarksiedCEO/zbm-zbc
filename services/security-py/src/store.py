"""
Local append-only record log, the sealed-secret store and the data-directory lock (ADR 0012 decisions 6-9).
Stdlib only. The record log is legal-py's (itself verification-py's) with one addition, the pending line.

``SEC_DATA_DIR/security_log.jsonl``: one JSON object per line, ``{"seq", "kind", "at", "data",
"prev_line_sha256", "record_sha256"}``; ``prev_line_sha256`` is the SHA-256 of the previous line's exact bytes
(64 zeros first) and ``record_sha256`` the line's own hash, so an edit, deletion, reordering or torn write is
caught at start (start-up refuses). A whole-file rewrite, a truncated tail or a deleted log is caught against
the ledger instead: every line is anchored on the ledger BEFORE it is written (service.Anchor).

The pending line closes the crash window between "anchored on the ledger" and "appended here": the exact line
is fsynced to ``pending.line`` before the anchor, and removed after the append. At start a pending line is
appended if (and only if) the ledger holds its anchor and it is the next line; otherwise it is discarded.

The record log holds metadata only: no secret value, no key, no ciphertext ever enters it.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Iterator, Optional

GENESIS = "0" * 64
LOG_NAME = "security_log.jsonl"


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
        if data_dir:
            os.makedirs(data_dir, mode=0o700, exist_ok=True)
            self.path = os.path.join(data_dir, LOG_NAME)
            if os.path.exists(self.path):
                with open(self.path, "rb") as fh:
                    raw = fh.read()
                if raw and not raw.endswith(b"\n"):
                    raise StoreCorrupt("log ends with a torn line; refusing to start (inspect the file)")
                self._lines = [ln for ln in raw.split(b"\n") if ln] if raw else []
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

    def append_prepared(self, rec: dict, line: bytes) -> dict:
        with self.lock:
            if self.fail_next_append:
                self.fail_next_append = False
                raise StoreWriteError("simulated local store failure")
            if rec["seq"] != len(self._lines) + 1:
                raise StoreWriteError("log moved on since the line was prepared")
            if self.path:
                try:
                    fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                    try:
                        os.write(fd, line + b"\n")
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError as exc:
                    raise StoreWriteError(f"local log write failed: {type(exc).__name__}") from exc
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
                    lines = [ln for ln in raw.split(b"\n") if ln]
                    if len(lines) != len(self._lines):
                        return False
                else:
                    lines = self._lines
                verify_lines(lines)
                return True
            except (OSError, StoreCorrupt):
                return False


    # ------------------------------------------------------------------ pending line (ADR 0012 decision 7)

    @property
    def pending_path(self) -> Optional[str]:
        return os.path.join(self.data_dir, PENDING_NAME) if self.data_dir else None

    _mem_pending: Optional[bytes] = None

    def write_pending(self, line: bytes) -> None:
        """Fsync the exact next line aside before its ledger anchor is recorded."""
        if not self.data_dir:
            self._mem_pending = line
            return
        _write_file(self.pending_path, line, "pending line")

    def clear_pending(self) -> None:
        self._mem_pending = None
        if self.data_dir:
            try:
                os.unlink(self.pending_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise StoreWriteError(f"pending line could not be removed: {type(exc).__name__}") from exc

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
PENDING_MAX_BYTES = 16 * 1024 * 1024    # above the largest line a route can produce (AEGIS L5: a scan line is ~1.4 MiB)


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


SEALED_DIR = "sealed"
_SEALED_NAME = __import__("re").compile(r"(sc-sec-[0-9a-f]{40})\.v([0-9]{1,6})")


class SealedStore:
    """One file per secret version, ``sealed/<secret_id>.v<version>``: the envelope (wrapped data key, nonce,
    ciphertext; crypto.Envelope) as JSON. Mutable on purpose: destroying a secret deletes its files, which is
    what makes the destruction final (the data key exists nowhere else). A file is written BEFORE the log line
    that cites it; at start a file no live log record cites is an orphan of a failed write and is removed."""

    MAX_BYTES = 64 * 1024

    def __init__(self, data_dir: Optional[str]):
        self.lock = threading.RLock()
        self.dir = os.path.join(data_dir, SEALED_DIR) if data_dir else None
        self._mem: dict[str, bytes] = {}
        self.fail_next_put = False   # tests
        if self.dir:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            for name in os.listdir(self.dir):
                if name.endswith(".tmp"):
                    os.unlink(os.path.join(self.dir, name))
                elif not _SEALED_NAME.fullmatch(name):
                    raise StoreCorrupt(f"sealed store holds a file that is not a sealed secret: {name[:40]}")

    @staticmethod
    def name(secret_id: str, version: int) -> str:
        return f"{secret_id}.v{version}"

    def put(self, secret_id: str, version: int, data: bytes) -> None:
        if len(data) > self.MAX_BYTES:
            raise StoreWriteError("sealed secret larger than 64 KiB")
        name = self.name(secret_id, version)
        if not _SEALED_NAME.fullmatch(name):
            raise StoreWriteError("sealed secret name")
        with self.lock:
            if self.fail_next_put:
                self.fail_next_put = False
                raise StoreWriteError("simulated sealed store failure")
            if not self.dir:
                self._mem[name] = data
                return
            _write_file(os.path.join(self.dir, name), data, "sealed secret")

    def get(self, secret_id: str, version: int) -> Optional[bytes]:
        name = self.name(secret_id, version)
        if not _SEALED_NAME.fullmatch(name):
            return None
        with self.lock:
            if not self.dir:
                return self._mem.get(name)
            try:
                with open(os.path.join(self.dir, name), "rb") as fh:
                    return fh.read(self.MAX_BYTES + 1)
            except OSError:
                return None

    def names(self) -> set[str]:
        with self.lock:
            if not self.dir:
                return set(self._mem)
            return {n for n in os.listdir(self.dir) if _SEALED_NAME.fullmatch(n)}

    def delete(self, secret_id: str, version: int) -> None:
        """Overwrite, fsync and unlink (best effort against recovery from the same disk; the guarantee is that the
        wrapped data key is gone, so the ciphertext alone is useless)."""
        name = self.name(secret_id, version)
        with self.lock:
            if not self.dir:
                self._mem.pop(name, None)
                return
            path = os.path.join(self.dir, name)
            try:
                size = os.path.getsize(path)
                fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
                try:
                    os.write(fd, b"\0" * size)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.unlink(path)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise StoreWriteError(f"sealed secret could not be destroyed: {type(exc).__name__}") from exc


LOCK_NAME = "security.lock"


class DataDirLock:
    """One process per data directory (fcntl.flock, released by the kernel when the process ends). A second
    instance on the same directory refuses to start: two writers would fork the log."""

    def __init__(self, data_dir: Optional[str]):
        self._fd = None
        if not data_dir:
            return
        import fcntl
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(data_dir, LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise StoreCorrupt("another security-py process holds this data directory; refusing to start")
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
