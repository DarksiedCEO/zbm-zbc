"""
Purgeable raw platform data (spec §B.5, ADR 0007 choice 3).

The evidence log is append-only and hash-chained, so nothing in it can ever be deleted. Raw platform data
that the platforms' terms limit — YouTube channel/video ids, TikTok ids and share URLs, Instagram media ids,
X post ids, the raw ``post_ref`` that names them — therefore NEVER enters the log: the log holds only their
SHA-256 (or HMAC), and the raw value lives here, in a small mutable store the retention job purges:
- 30 calendar days after the last refresh (a successful fetch refreshes it) — VI-15b/c/d/e;
- after ``revision_watch_end`` (only the hashes remain);
- within 24 h of a connection's revocation (at once, in fact) — VI-15b;
- within VI_MINOR_PURGE_HOURS of a ``minor`` age result (at once, in fact).

``VI_DATA_DIR/vi_platform_data.json`` is rewritten atomically (temp file, fsync, rename, 0600); without a
data dir it lives in memory. It carries no tokens (those never leave the vault) and no captions (hash only).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Iterable, Optional

NAME = "vi_platform_data.json"


class SideStoreError(RuntimeError):
    pass


class PlatformDataStore:
    def __init__(self, data_dir: Optional[str]):
        self.lock = threading.RLock()
        self.path = os.path.join(data_dir, NAME) if data_dir else None
        self.entries: dict[str, dict] = {}
        self.fail_next_save = False   # tests
        if self.path and os.path.exists(self.path):
            with open(self.path, "rb") as fh:
                raw = fh.read()
            try:
                doc = json.loads(raw) if raw else {"entries": {}}
            except ValueError as exc:
                raise SideStoreError("platform data store is not JSON; refusing to start") from exc
            if not isinstance(doc, dict) or not isinstance(doc.get("entries"), dict):
                raise SideStoreError("platform data store has an unexpected shape; refusing to start")
            self.entries = doc["entries"]

    def put(self, key: str, owner_id: str, platform: str, field: str, value: str, at: str, rule: str,
            owner_kind: str) -> None:
        with self.lock:
            self.entries[key] = {"owner_id": owner_id, "owner_kind": owner_kind, "platform": platform, "field": field,
                                 "value": value, "stored_at": at, "rule": rule}

    def get(self, key: str) -> Optional[str]:
        with self.lock:
            e = self.entries.get(key)
            return e["value"] if e else None

    def refresh(self, key: str, at: str) -> None:
        with self.lock:
            if key in self.entries:
                self.entries[key]["stored_at"] = at

    def delete(self, keys: Iterable[str]) -> list[dict]:
        with self.lock:
            out = []
            for k in list(keys):
                e = self.entries.pop(k, None)
                if e is not None:
                    out.append({"key": k, **{f: e[f] for f in ("owner_id", "platform", "field", "rule")}})
            return out

    def keys_for(self, owner_ids: Iterable[str]) -> list[str]:
        ids = set(owner_ids)
        with self.lock:
            return sorted(k for k, e in self.entries.items() if e["owner_id"] in ids)

    def all(self) -> list[tuple[str, dict]]:
        with self.lock:
            return sorted((k, dict(e)) for k, e in self.entries.items())

    def save(self) -> None:
        with self.lock:
            if self.fail_next_save:
                self.fail_next_save = False
                raise SideStoreError("simulated platform data store failure")
            if not self.path:
                return
            data = json.dumps({"entries": self.entries}, sort_keys=True, separators=(",", ":")).encode("utf-8")
            tmp = self.path + ".tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, self.path)
            except OSError as exc:
                raise SideStoreError(f"platform data store write failed: {type(exc).__name__}") from exc
