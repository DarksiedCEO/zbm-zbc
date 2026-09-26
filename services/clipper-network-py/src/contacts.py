"""
Contact store (spec §B.1, §C.9 step 5, CN-24): the ONLY place raw contact
data lives — a clipper's email and display name, connected-account display
handles, and an opt-in's email. It is deliberately NOT the hash-chained log
(an append-only log cannot forget), so exit deletion is real deletion.

Integrity: the log records the SHA-256 of every value written here
(``contact_sha256``) and every deletion. At start the service compares the
two: a value whose hash differs from the log's (edited file — a redirected
email, say) refuses start-up; a value the log never referenced (a write
whose log line never landed) is an orphan and is purged; a value the log
expects but that is missing makes that contact unavailable (no message can
be sent to it — fail closed) and is reported by /health.

Order inside an operation: the value is written here FIRST, then the ledger
records and the log line; if those fail, the value is an orphan and is
purged at the next start (and immediately, best effort). The file is
replaced atomically (temp + fsync + rename), mode 0600.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Optional

FILE = "cn_contacts.json"


def value_sha(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                          .encode("ascii")).hexdigest()


class ContactStoreError(RuntimeError):
    pass


class ContactStore:
    def __init__(self, data_dir: Optional[str]):
        self.lock = threading.RLock()
        self.path = os.path.join(data_dir, FILE) if data_dir else None
        self._data: dict[str, dict] = {}
        self.fail_next_write = False       # tests: simulate a disk failure
        if self.path and os.path.exists(self.path):
            with open(self.path, "rb") as fh:
                raw = fh.read()
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                raise ContactStoreError("contact store is not valid JSON; refusing to start (inspect the file)") from None
            if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in data.items()):
                raise ContactStoreError("contact store has an unexpected shape; refusing to start")
            self._data = data

    def _flush(self) -> None:
        if self.fail_next_write:
            self.fail_next_write = False
            raise ContactStoreError("simulated contact store failure")
        if not self.path:
            return
        tmp = self.path + ".tmp"
        data = json.dumps(self._data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
            dfd = os.open(os.path.dirname(self.path), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as exc:
            raise ContactStoreError(f"contact store write failed: {type(exc).__name__}") from exc

    def put(self, key: str, value: dict) -> str:
        with self.lock:
            before = self._data.get(key)
            self._data[key] = dict(value)
            try:
                self._flush()
            except ContactStoreError:
                if before is None:
                    self._data.pop(key, None)
                else:
                    self._data[key] = before
                raise
            return value_sha(value)

    def get(self, key: str) -> Optional[dict]:
        with self.lock:
            v = self._data.get(key)
            return dict(v) if v is not None else None

    def delete(self, key: str) -> bool:
        with self.lock:
            if key not in self._data:
                return False
            before = self._data.pop(key)
            try:
                self._flush()
            except ContactStoreError:
                self._data[key] = before
                raise
            return True

    def keys(self) -> list[str]:
        with self.lock:
            return sorted(self._data)

    def reconcile(self, expected: dict[str, str]) -> tuple[list[str], list[str]]:
        """``expected``: key -> SHA-256 the log last recorded (deleted keys absent). Returns (tampered, missing);
        orphans (keys the log does not expect) are purged."""
        with self.lock:
            tampered = sorted(k for k, sha in expected.items() if k in self._data and value_sha(self._data[k]) != sha)
            missing = sorted(k for k in expected if k not in self._data)
            orphans = [k for k in self._data if k not in expected]
            for k in orphans:
                self._data.pop(k)
            if orphans:
                self._flush()
            return tampered, missing

    def raw_bytes(self) -> bytes:
        """Tests (byte scans): the persisted bytes, or the in-memory JSON."""
        with self.lock:
            if self.path and os.path.exists(self.path):
                with open(self.path, "rb") as fh:
                    return fh.read()
            return json.dumps(self._data, sort_keys=True).encode()
