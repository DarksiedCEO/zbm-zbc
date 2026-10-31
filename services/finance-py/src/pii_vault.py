"""
The client contact vault (ADR 0027 decision 3; MSA 15.5): the ONLY place Finance keeps a client's billing contact
(email address and name). Redaction by design: the hash-chained log and the evidence ledger are append-only and can
never forget, so personal information never goes there -- they hold a random-looking ``contact_ref`` only. Deleting a
client's contact is deleting one file here.

``FIN_DATA_DIR/pii/<sha256(client id)>.json`` (directory 0700, files 0600, written by atomic replace with fsync):
``{"client_id", "contact_ref", "email", "name", "salt"}``. ``salt`` keys the HMAC that stands in for the address in the
idempotency hash, so once the file is gone even that link is gone. No data directory: memory only (tests, local runs).
Nothing here ever logs an address.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from typing import Optional

DIR_NAME = "pii"


def _fname(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest() + ".json"


class ContactVault:
    def __init__(self, data_dir: Optional[str]):
        self.dir = os.path.join(data_dir, DIR_NAME) if data_dir else None
        self._mem: dict[str, dict] = {}
        self._lock = threading.Lock()
        if self.dir:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)

    def _path(self, client_id: str) -> str:
        return os.path.join(self.dir, _fname(client_id))

    def get(self, client_id: str) -> Optional[dict]:
        with self._lock:
            if self.dir is None:
                rec = self._mem.get(client_id)
                return dict(rec) if rec else None
            try:
                fd = os.open(self._path(client_id), os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
            except FileNotFoundError:
                return None
            try:
                raw = os.read(fd, 64 * 1024)
            finally:
                os.close(fd)
            try:
                rec = json.loads(raw)
            except ValueError:
                return None
            return rec if isinstance(rec, dict) and rec.get("client_id") == client_id else None

    def salt(self, client_id: str) -> str:
        rec = self.get(client_id)
        return rec["salt"] if rec and isinstance(rec.get("salt"), str) else secrets.token_hex(16)

    @staticmethod
    def digest(salt: str, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return hmac.new(bytes.fromhex(salt), value.encode("utf-8"), hashlib.sha256).hexdigest()

    def put(self, client_id: str, contact_ref: str, email: str, name: Optional[str], salt: str) -> None:
        rec = {"client_id": client_id, "contact_ref": contact_ref, "email": email, "name": name, "salt": salt}
        with self._lock:
            if self.dir is None:
                self._mem[client_id] = rec
                return
            path = self._path(client_id)
            tmp = path + ".tmp"
            data = json.dumps(rec, sort_keys=True).encode("utf-8")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         0o600)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            self._fsync_dir()

    def delete(self, client_id: str) -> int:
        """Remove the client's contact (and any half-written temp file). Returns how many records were removed."""
        with self._lock:
            if self.dir is None:
                return 1 if self._mem.pop(client_id, None) is not None else 0
            n = 0
            for p in (self._path(client_id), self._path(client_id) + ".tmp"):
                try:
                    os.unlink(p)
                    n += 1 if not p.endswith(".tmp") else 0
                except FileNotFoundError:
                    pass
            self._fsync_dir()
            return n

    def _fsync_dir(self) -> None:
        fd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
