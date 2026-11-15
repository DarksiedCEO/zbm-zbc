"""The encrypted store of the link index (marcus-pipeline-spec/1 B-MP-32, W-MP-2; CLARIFICATIONS v1.1 GP-4).

Every index object (a staged file's rows, a shard, a manifest, a crawled-source set, a graph-rank document) is written
through ``IndexStore.put`` and nothing else. The index is not tenant data (Q-MP-2): it is encrypted at rest under a
SERVICE key, ``vault._derive(<the vault key>, b"marcus-index/v1")`` (HKDF-SHA256), AES-256-GCM, a random 96-bit nonce
per object, the additional data binding the object's kind, name and key id. Plaintext exists only in memory. An object
on disk is ``MIX1 | len(key id) | key id | nonce | ciphertext+tag``; the manifest records the sha256 of those bytes
(the ciphertext). No vault key: ``IndexKeyUnavailable`` (a build is refused ``INDEX_KEY_UNAVAILABLE``, AC-10).

Writes are atomic (a temporary file, fsync, rename, directory fsync). Without a data directory (non-production) the
objects are held in memory, encrypted the same way."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import vault as vault_mod

HKDF_INFO = vault_mod.INDEX_INFO
MAGIC = b"MIX1"
DIR_NAME = "marcus_index"
KINDS = ("stage", "shard", "manifest", "sources", "graph")
NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,95}")
NONCE = 12


class IndexKeyUnavailable(Exception):
    pass


class StoreCorrupt(Exception):
    """An object that does not decrypt (wrong key, tampered bytes), does not match its recorded hash, or is missing."""


class Keyring:
    """``[(key id, AESGCM)]``, the current key first. Never printed or copied."""

    __slots__ = ("_keys",)

    def __init__(self, keys: list):
        self._keys = list(keys)

    @classmethod
    def from_vault(cls, vault) -> "Keyring":
        try:
            return cls(vault.index_keys())
        except vault_mod.VaultError:
            raise IndexKeyUnavailable() from None

    @classmethod
    def ephemeral(cls) -> "Keyring":
        return cls([("ephemeral", AESGCM(os.urandom(32)))])

    @property
    def current(self) -> tuple:
        return self._keys[0]

    def get(self, key_id: str):
        for k, a in self._keys:
            if k == key_id:
                return a
        return None

    def __repr__(self) -> str:
        return "Keyring(<redacted>)"

    def __reduce__(self):
        raise TypeError("an index keyring is never pickled")


def _aad(kind: str, name: str, key_id: str) -> bytes:
    return b"|".join((HKDF_INFO, kind.encode("ascii"), name.encode("ascii"), key_id.encode("ascii")))


def split_blob(blob: bytes, kind: str, name: str) -> tuple:
    """(nonce, ciphertext, additional data) of a stored object: what ``AESGCM.decrypt`` takes."""
    if len(blob) < 5 + NONCE or blob[:4] != MAGIC:
        raise StoreCorrupt()
    n = blob[4]
    key_id = blob[5:5 + n].decode("ascii", "replace")
    nonce = blob[5 + n:5 + n + NONCE]
    return nonce, blob[5 + n + NONCE:], _aad(kind, name, key_id)


def _check(kind: str, name: str) -> None:
    if kind not in KINDS or not isinstance(name, str) or not NAME.fullmatch(name) or ".." in name:
        raise ValueError("index object name")


class IndexStore:
    def __init__(self, data_dir: Optional[str], keyring: Optional[Keyring]):
        self.root = os.path.join(data_dir, DIR_NAME) if data_dir else None
        self.keyring = keyring
        self._mem: dict = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ encryption

    def _ring(self) -> Keyring:
        if self.keyring is None:
            raise IndexKeyUnavailable()
        return self.keyring

    def encrypt(self, kind: str, name: str, data: bytes) -> bytes:
        _check(kind, name)
        key_id, aead = self._ring().current
        nonce = os.urandom(NONCE)
        kid = key_id.encode("ascii")
        return MAGIC + bytes((len(kid),)) + kid + nonce + aead.encrypt(nonce, data, _aad(kind, name, key_id))

    def decrypt(self, blob: bytes, kind: str, name: str) -> bytes:
        _check(kind, name)
        if not isinstance(blob, (bytes, bytearray)) or len(blob) < 5 + NONCE or blob[:4] != MAGIC:
            raise StoreCorrupt()
        n = blob[4]
        try:
            key_id = bytes(blob[5:5 + n]).decode("ascii")
        except UnicodeDecodeError:
            raise StoreCorrupt() from None
        aead = self._ring().get(key_id)
        if aead is None:
            raise StoreCorrupt()
        nonce, ct, aad = split_blob(bytes(blob), kind, name)
        try:
            return aead.decrypt(nonce, ct, aad)
        except InvalidTag:
            raise StoreCorrupt() from None

    # ------------------------------------------------------------------ objects

    def object_path(self, kind: str, name: str) -> Optional[str]:
        _check(kind, name)
        return os.path.join(self.root, kind, name) if self.root else None

    def put_blob(self, kind: str, name: str, blob: bytes) -> str:
        """Write an already encrypted object (atomic); returns the sha256 of its bytes."""
        path = self.object_path(kind, name)
        if path is None:
            with self.lock:
                self._mem[(kind, name)] = bytes(blob)
        else:
            d = os.path.dirname(path)
            os.makedirs(d, mode=0o700, exist_ok=True)
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(blob)
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            os.replace(tmp, path)
            dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        return hashlib.sha256(blob).hexdigest()

    def put(self, kind: str, name: str, data: bytes) -> str:
        return self.put_blob(kind, name, self.encrypt(kind, name, data))

    def get_blob(self, kind: str, name: str) -> bytes:
        path = self.object_path(kind, name)
        if path is None:
            with self.lock:
                blob = self._mem.get((kind, name))
            if blob is None:
                raise StoreCorrupt()
            return blob
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            raise StoreCorrupt() from None
        with os.fdopen(fd, "rb") as fh:
            return fh.read()

    def get(self, kind: str, name: str, sha: Optional[str] = None) -> bytes:
        blob = self.get_blob(kind, name)
        if sha is not None and hashlib.sha256(blob).hexdigest() != sha:
            raise StoreCorrupt()
        return self.decrypt(blob, kind, name)

    def exists(self, kind: str, name: str) -> bool:
        path = self.object_path(kind, name)
        if path is None:
            with self.lock:
                return (kind, name) in self._mem
        return os.path.isfile(path)

    def delete(self, kind: str, name: str) -> None:
        path = self.object_path(kind, name)
        if path is None:
            with self.lock:
                self._mem.pop((kind, name), None)
            return
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def names(self, kind: str) -> list:
        if kind not in KINDS:
            raise ValueError("index object kind")
        if self.root is None:
            with self.lock:
                return sorted(n for k, n in self._mem if k == kind)
        d = os.path.join(self.root, kind)
        try:
            return sorted(n for n in os.listdir(d) if NAME.fullmatch(n) and not n.endswith(".tmp"))
        except FileNotFoundError:
            return []

    def stage_names(self) -> list:
        return self.names("stage")

    def raw_objects(self) -> list:
        """Every stored object as stored (encrypted): what a reader of the data directory would see."""
        out = []
        for kind in KINDS:
            for n in self.names(kind):
                try:
                    out.append(self.get_blob(kind, n))
                except StoreCorrupt:
                    continue
        return out

    # ------------------------------------------------------------------ JSON documents

    @staticmethod
    def dumps(obj) -> bytes:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")

    def get_json(self, kind: str, name: str, sha: Optional[str] = None):
        try:
            return json.loads(self.get(kind, name, sha))
        except ValueError:
            raise StoreCorrupt() from None

    def read_manifest(self, snapshot_id: str) -> dict:
        return self.get_json("manifest", snapshot_id)

    def read_shard(self, snapshot_id: str, shard_id: str, sha: Optional[str]) -> list:
        data = self.get("shard", shard_id, sha)
        try:
            doc = json.loads(data)
        except ValueError:
            raise StoreCorrupt() from None
        if not isinstance(doc, dict) or doc.get("snapshot_id") != snapshot_id or not isinstance(doc.get("rows"), list):
            raise StoreCorrupt()
        return doc["rows"]

    def read_sources(self, snapshot_id: str, sha: Optional[str]) -> dict:
        doc = self.get_json("sources", snapshot_id, sha)
        if not isinstance(doc, dict) or doc.get("snapshot_id") != snapshot_id:
            raise StoreCorrupt()
        return doc

    def read_graph(self, name: str, sha: Optional[str]) -> dict:
        return self.get_json("graph", name, sha)
