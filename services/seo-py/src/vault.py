"""
The client citation vault (ADR 0051, "SEO client citation vault (Option B)"; number to be confirmed at merge).

A client's citation evidence (the cited URL, the excerpt, the title, taken from AI answers about the client's brand)
is kept in full for the owning client, and never in plaintext on disk or in a log. Each record is JSON, encrypted with
AES-256-GCM under a key that only the environment holds (``SEO_VAULT_KEY``, base64url of 32 random bytes, named by
``SEO_VAULT_KEY_ID``), with a fresh 12-byte random nonce per record and the associated data bound to the tenant, the
record id and the key id. Records are appended to ``SEO_DATA_DIR/citation_vault.jsonl``, one JSON line per record:
``{"v": 1, "id", "t", "k", "n", "c"}`` = record id, keyed tenant tag, key id, nonce, ciphertext, every value base64url
(no padding); no plaintext field. The tenant tag is an HMAC of the tenant id under a sub-key of the vault key, so a
reader of the file cannot tell which client a line belongs to by hashing tenant names.

Fail closed: ``put`` with no key configured raises and writes nothing (there is never a plaintext fallback). ``get``
returns a record only to the tenant that stored it; a wrong tenant, an unknown id, a tampered line, an unknown key id
or a failed decryption all raise the same ``VaultNotFound`` with the same message (no oracle). Error messages come
from a closed set and never quote a key, a tenant record or a plaintext.

Rotation: ``SEO_VAULT_KEY_PREVIOUS`` / ``SEO_VAULT_KEY_PREVIOUS_ID`` name the key being retired. It is used only to
decrypt records written under it; every new write uses the current key. Re-encryption is not built (ADR 0051).

Wired into the service by job 2 (svc_citations.py): a citation run puts each row's full evidence here and the log
keeps only ``ref(vault_id)``. ``MemoryCitationVault`` is the same store without a file (the service's in-memory mode,
used by tests): identical lines, identical encryption, nothing on disk. Stdlib plus ``cryptography``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from config import TENANT_ID

VAULT_NAME = "citation_vault.jsonl"
OPERATOR_VAULT_NAME = "operator_text_vault.jsonl"   # review c0c09bd1: operator free text (svc_optext.py), same scheme
MAX_RECORD_BYTES = 256 * 1024          # the encoded JSON of one record; above it the record is refused unread
_MAX_LINE_BYTES = 4 * (MAX_RECORD_BYTES + 16) // 3 + 1024   # base64 of the largest ciphertext plus the other fields
# review 08eea6e1 SHOULD 1 / 2: a torn final line is cut only when it is a fragment of ONE record; the cut bytes go to
# ``<vault>.torn-<repair id>`` (0600) and the repair's counts to ``<vault>.repair`` before the cut, so a start that
# fails after the cut records it on the next start
QUARANTINE_SUFFIX = ".torn-"
REPAIR_MARKER_SUFFIX = ".repair"
REPAIR_FIELDS = ("torn_lines", "bytes_removed", "records_removed", "records_kept", "newline_added", "quarantined",
                 "repair_id")
REPAIR_ID = re.compile(r"[0-9a-f]{32}")
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
VERSION = 1

ENV_KEY = "SEO_VAULT_KEY"
ENV_KEY_ID = "SEO_VAULT_KEY_ID"
ENV_PREVIOUS_KEY = "SEO_VAULT_KEY_PREVIOUS"
ENV_PREVIOUS_KEY_ID = "SEO_VAULT_KEY_PREVIOUS_ID"

KEY_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
VAULT_ID = re.compile(r"[0-9a-f]{32}")
_B64_KEY = re.compile(r"[A-Za-z0-9_-]{43}=?")
_B64 = re.compile(r"[A-Za-z0-9_-]{1,%d}" % _MAX_LINE_BYTES)
_AAD_TAG = b"seo-citation-vault/v1"
# ADR 0051 job 3: every per-field hash written to the log is an HMAC under this sub-key of the vault key (HKDF, its own
# info label), so a short guessable field (an e-mail or a phone number alone in a hint) cannot be confirmed by hashing
# guesses
FIELD_HASH_INFO = b"seo-citation-vault/v1/log-field-hmac"
FIELD_HASH_TAG = b"seo-log-field/v1"
FIELD_HASH_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_FIELDS = frozenset(("v", "id", "t", "k", "n", "c"))


class VaultError(Exception):
    """Every vault error. The message is from a closed set: never a key, a tenant record or a plaintext."""


class VaultKeyInvalid(VaultError):
    """The vault key settings are not usable (start refuses). Names the variable, never quotes its value."""


class VaultUnavailable(VaultError):
    """No vault key is configured: nothing is stored or read (fail closed)."""


class VaultNotFound(VaultError):
    """The one answer for a wrong tenant, an unknown id, a tampered line and a failed decryption."""


class VaultRecordInvalid(VaultError):
    """The record (or the tenant id) given to ``put`` cannot be stored."""


class VaultWriteError(VaultError):
    """The append did not reach the disk; nothing of it is kept."""


class VaultCorrupt(VaultError):
    """The vault file cannot be opened safely (a torn tail, a symlink, open permissions): opening refuses."""


NOT_FOUND = "citation record not found"


class _Material:
    """One vault key, held as its three derived sub-keys (AES-256-GCM, the tenant-tag HMAC and the log-field HMAC,
    each from HKDF-SHA256 with its own info label). The raw key is not kept. Never printed, pickled or copied."""

    __slots__ = ("key_id", "_aead", "_mac", "_field_mac")

    def __init__(self, key_id: str, raw: bytes):
        self.key_id = key_id
        self._aead = AESGCM(_derive(raw, b"seo-citation-vault/v1/aes-256-gcm"))
        self._mac = _derive(raw, b"seo-citation-vault/v1/tenant-tag")
        self._field_mac = _derive(raw, FIELD_HASH_INFO)

    def tenant_tag(self, tenant_id: str) -> bytes:
        return hmac.new(self._mac, tenant_id.encode("ascii"), hashlib.sha256).digest()

    def field_hash(self, tenant_id: str, field: str, value: str) -> str:
        """HMAC-SHA256 (hex) of one evidence field for the log, bound to the tenant and the field name."""
        msg = b"\x00".join((FIELD_HASH_TAG, tenant_id.encode("ascii"), field.encode("ascii"),
                            value.encode("utf-8", "surrogatepass")))
        return hmac.new(self._field_mac, msg, hashlib.sha256).hexdigest()

    def __repr__(self) -> str:
        return f"<vault key {self.key_id!r}: redacted>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a vault key is never pickled")

    def __reduce_ex__(self, protocol):
        raise TypeError("a vault key is never pickled")

    def __copy__(self):
        raise TypeError("a vault key is never copied")

    def __deepcopy__(self, memo):
        raise TypeError("a vault key is never copied")


class VaultKey:
    """The current vault key (every write) and, during a rotation, the previous one (decrypt only). Built only by
    ``parse_vault_key`` / ``load_vault_key``. The repr names the key ids and nothing else; pickling and copying are
    refused (gsc_client.ServiceAccountKey's rules)."""

    __slots__ = ("key_id", "previous_key_id", "_cur", "_prev")

    def __init__(self, current: _Material, previous: Optional[_Material] = None):
        if type(current) is not _Material or (previous is not None and type(previous) is not _Material):
            raise TypeError("a VaultKey is built by parse_vault_key or load_vault_key")
        self.key_id = current.key_id
        self.previous_key_id = previous.key_id if previous is not None else None
        self._cur = current
        self._prev = previous

    def _material(self, key_id: str) -> Optional[_Material]:
        if key_id == self._cur.key_id:
            return self._cur
        if self._prev is not None and key_id == self._prev.key_id:
            return self._prev
        return None

    def __repr__(self) -> str:
        return f"VaultKey(key_id={self.key_id!r}, previous_key_id={self.previous_key_id!r}, key=<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a vault key is never pickled")

    def __reduce_ex__(self, protocol):
        raise TypeError("a vault key is never pickled")

    def __copy__(self):
        raise TypeError("a vault key is never copied")

    def __deepcopy__(self, memo):
        raise TypeError("a vault key is never copied")


def _derive(raw: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=info).derive(raw)


def _decode_key(text, name: str) -> bytes:
    """base64url (padding optional) of exactly 32 bytes, canonical, not trivially repetitive."""
    msg = f"{name} must be base64url of exactly 32 random bytes (python3 -c \"import base64,os; " \
          f"print(base64.urlsafe_b64encode(os.urandom(32)).decode())\")"
    if not isinstance(text, str):
        raise VaultKeyInvalid(msg)
    text = text.strip()
    raw = b""
    if _B64_KEY.fullmatch(text):
        try:
            raw = base64.urlsafe_b64decode(text.rstrip("=") + "=")
        except ValueError:
            raw = b""
        if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != text.rstrip("="):
            raw = b""                     # non-canonical trailing bits: two spellings of one key are refused
    ok = len(raw) == KEY_BYTES and len(set(raw)) >= 16
    text = None
    if not ok:
        raise VaultKeyInvalid(msg)
    return raw


def _key_id(text, name: str) -> str:
    if not isinstance(text, str) or not KEY_ID.fullmatch(text.strip()):
        raise VaultKeyInvalid(f"{name} must be a short key id (1..32 characters: a-z, 0-9, '-')")
    return text.strip()


def parse_vault_key(key: str, key_id: str, previous_key: Optional[str] = None,
                    previous_key_id: Optional[str] = None) -> VaultKey:
    """Strict: both of a pair or neither; the previous key differs from the current one in bytes and in id."""
    cur_id = _key_id(key_id, ENV_KEY_ID)
    cur_raw = _decode_key(key, ENV_KEY)
    prev = None
    if (previous_key is None) != (previous_key_id is None):
        raise VaultKeyInvalid(f"{ENV_PREVIOUS_KEY} and {ENV_PREVIOUS_KEY_ID} are set together or not at all")
    if previous_key is not None:
        prev_id = _key_id(previous_key_id, ENV_PREVIOUS_KEY_ID)
        prev_raw = _decode_key(previous_key, ENV_PREVIOUS_KEY)
        if prev_id == cur_id:
            raise VaultKeyInvalid(f"{ENV_PREVIOUS_KEY_ID} must differ from {ENV_KEY_ID}")
        if hmac.compare_digest(prev_raw, cur_raw):
            raise VaultKeyInvalid(f"{ENV_PREVIOUS_KEY} must differ from {ENV_KEY}")
        prev = _Material(prev_id, prev_raw)
        prev_raw = None
    current = _Material(cur_id, cur_raw)
    cur_raw = None
    return VaultKey(current, prev)


def load_vault_key(env: Optional[dict] = None) -> Optional[VaultKey]:
    """The vault key from the environment, or None when none of the four variables is set (the vault then refuses
    every write). Anything partial or malformed raises ``VaultKeyInvalid`` (start refuses)."""
    env = os.environ if env is None else env

    def val(name: str) -> Optional[str]:
        v = env.get(name)
        return v if isinstance(v, str) and v.strip() else None
    key, kid, prev, prev_id = val(ENV_KEY), val(ENV_KEY_ID), val(ENV_PREVIOUS_KEY), val(ENV_PREVIOUS_KEY_ID)
    if key is None and kid is None and prev is None and prev_id is None:
        return None
    if key is None or kid is None:
        raise VaultKeyInvalid(f"{ENV_KEY} and {ENV_KEY_ID} are set together (a previous key needs a current one)")
    return parse_vault_key(key, kid, prev, prev_id)


EPHEMERAL_KEY_ID = "ephemeral"


def ephemeral_vault_key() -> VaultKey:
    """A fresh random key held in memory only, for the service's in-memory mode (no data directory, non-production):
    nothing it encrypts outlives the process, so no key needs to be configured. Never used with a data directory."""
    raw = os.urandom(KEY_BYTES)
    while len(set(raw)) < 16:                        # 32 random bytes with < 16 distinct values: practically never
        raw = os.urandom(KEY_BYTES)
    material = _Material(EPHEMERAL_KEY_ID, raw)
    raw = None
    return VaultKey(material)


# The log names a vault record by ``ref(vault_id)``: ``seo-vlt-`` + the 32-hex id + 8 hex of its SHA-256 (a check),
# the shape of the service's other ids (``seo-<kind>-<40 hex>``), which the strict sweep (secret_guard.guard_record)
# keeps under an ``*_id`` key. The ref is opaque: it names the record and says nothing of its content.
REF_PREFIX = "seo-vlt-"
_REF = re.compile(r"seo-vlt-([0-9a-f]{32})([0-9a-f]{8})")


def ref(vault_id: str) -> str:
    if not isinstance(vault_id, str) or not VAULT_ID.fullmatch(vault_id):
        raise VaultRecordInvalid("not a vault id")
    return REF_PREFIX + vault_id + hashlib.sha256(vault_id.encode("ascii")).hexdigest()[:8]


def parse_ref(text) -> Optional[str]:
    """The vault id a ref names, or None (malformed, or the check does not match)."""
    m = _REF.fullmatch(text) if isinstance(text, str) else None
    if m is None or hashlib.sha256(m.group(1).encode("ascii")).hexdigest()[:8] != m.group(2):
        return None
    return m.group(1)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text) -> Optional[bytes]:
    if not isinstance(text, str) or not _B64.fullmatch(text):
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        return None
    return raw if _b64(raw) == text else None


def _new_nonce() -> bytes:
    return os.urandom(NONCE_BYTES)


def _aad(tenant_id: str, vault_id: str, key_id: str) -> bytes:
    # every part is from a closed alphabet without NUL, so the NUL separator is unambiguous
    return b"\x00".join((_AAD_TAG, tenant_id.encode("ascii"), vault_id.encode("ascii"), key_id.encode("ascii")))


def _tenant_matches(material: _Material, tenant_id: str, tag: bytes) -> bool:
    return hmac.compare_digest(material.tenant_tag(tenant_id), tag)


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate field")
        out[k] = v
    return out


def _parse_line(line: bytes) -> Optional[dict]:
    """A stored line -> its decoded fields, or None (anything malformed: the record is then simply not found)."""
    if not line or len(line) > _MAX_LINE_BYTES:
        return None
    try:
        obj = json.loads(line.decode("ascii"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or set(obj) != _FIELDS or obj["v"] != VERSION or type(obj["v"]) is not int:
        return None
    vid, tag, kid, nonce, ct = (_unb64(obj[f]) for f in ("id", "t", "k", "n", "c"))
    if vid is None or len(vid) != 16 or tag is None or len(tag) != 32 or nonce is None or len(nonce) != NONCE_BYTES:
        return None
    if ct is None or len(ct) < TAG_BYTES or kid is None:
        return None
    try:
        key_id = kid.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not KEY_ID.fullmatch(key_id):
        return None
    return {"vault_id": vid.hex(), "tag": tag, "key_id": key_id, "nonce": nonce, "ct": ct}


def _torn_fragment(tail: bytes) -> bool:
    """A torn write of ONE record: shorter than a line, opening one JSON object and holding no record boundary (a
    stored line's only ``}`` is its last byte, so a fragment has none)."""
    return 0 < len(tail) < _MAX_LINE_BYTES and tail[:1] == b"{" and tail.count(b"{") == 1 and b"}" not in tail


def _encode(record) -> Optional[bytes]:
    try:
        return json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        return None


_COLLECT = threading.local()            # (vault, batch) while ``CitationVault.collecting`` is active on this thread


class VaultBatch:
    """Encrypted lines put while collecting, written later by ``CitationVault.write_batch`` in ONE append with ONE fsync
    (review adacb2ca SHOULD 1). Holds ciphertext only; ``discard`` drops it (nothing is written)."""

    def __init__(self):
        self.lines: list = []                           # [(vault id, encrypted line)]

    def discard(self) -> None:
        self.lines = []

    def __len__(self) -> int:
        return len(self.lines)

    def __repr__(self) -> str:
        return f"VaultBatch(lines={len(self.lines)})"

    def __reduce__(self):
        raise TypeError("a vault batch is never pickled")


class CitationVault:
    """Per-tenant encrypted, append-only store under the service's data directory. Thread-safe (one re-entrant lock,
    as ``service.Service`` and ``store.RecordLog``); one process per data directory (the data-directory flock)."""

    def __init__(self, data_dir: str, key: Optional[VaultKey], repair: bool = True, name: str = VAULT_NAME):
        if not isinstance(data_dir, str) or not os.path.isabs(data_dir):
            raise ValueError("the citation vault needs an absolute data directory")
        if key is not None and type(key) is not VaultKey:
            raise TypeError("key must be a VaultKey or None")
        self._key = key
        self.lock = threading.RLock()
        self.data_dir = data_dir
        if name not in (VAULT_NAME, OPERATOR_VAULT_NAME):
            raise ValueError("unknown vault file")
        self.path = os.path.join(data_dir, name)
        self._index: dict[str, tuple[int, int]] = {}    # vault id -> (offset, length) of its line
        self._size = 0
        self.bad_lines = 0                               # lines that do not parse (never served; counted)
        self.fault: Optional[str] = None                 # a failed append could not be cut back: writes refuse
        self.orphans = 0                                 # records written whose run commit then failed (counted)
        self.tail_repair: Optional[dict] = None          # counts of a torn final line cut at open (no content)
        self._repair = repair                            # False: a torn final line refuses (observe-only start)
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        self._load()

    def __repr__(self) -> str:
        return f"CitationVault(records={len(self._index)}, key={'set' if self._key is not None else 'unset'})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("the citation vault is never pickled")

    def __reduce_ex__(self, protocol):
        raise TypeError("the citation vault is never pickled")

    def __copy__(self):
        raise TypeError("the citation vault is never copied")

    def __deepcopy__(self, memo):
        raise TypeError("the citation vault is never copied")

    @property
    def key_configured(self) -> bool:
        return self._key is not None

    def __len__(self) -> int:
        with self.lock:
            return len(self._index)

    def _load(self) -> None:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return
        except OSError:
            raise VaultCorrupt("the vault file cannot be inspected") from None
        if not stat.S_ISREG(st.st_mode):
            raise VaultCorrupt("the vault file is not a regular file (a symlink or something else); refusing")
        if st.st_uid != os.geteuid():
            raise VaultCorrupt("the vault file is not owned by the user running this service")
        if st.st_mode & 0o077:
            raise VaultCorrupt("the vault file is open to group or others; chmod 600 it")
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            raise VaultCorrupt("the vault file cannot be opened") from None
        try:
            chunks = []
            while True:
                b = os.read(fd, 1 << 20)
                if not b:
                    break
                chunks.append(b)
        finally:
            os.close(fd)
        raw = b"".join(chunks)
        torn = bool(raw) and not raw.endswith(b"\n")
        body = raw[:raw.rfind(b"\n") + 1] if torn else raw
        offset = 0
        for line in body.split(b"\n")[:-1] if body else []:
            parsed = _parse_line(line)
            if parsed is None or parsed["vault_id"] in self._index:
                self.bad_lines += 1
            else:
                self._index[parsed["vault_id"]] = (offset, len(line))
            offset += len(line) + 1
        if torn:
            # review 601e328a SHOULD 3 / review 08eea6e1 SHOULD 1: ONLY the final line lacks its newline; a torn line
            # with any other bad line still refuses
            tail = raw[len(body):]
            if self.bad_lines or not self._repair:
                self._index, self.bad_lines = {}, 0
                raise VaultCorrupt("the vault file ends with a torn line (and holds another bad line, or this start "
                                   "repairs nothing); refusing to open (inspect it)")
            parsed = _parse_line(tail)
            if parsed is not None and parsed["vault_id"] not in self._index:
                # (a) a COMPLETE final record lacking only its newline: kept, and the newline appended
                self._index[parsed["vault_id"]] = (len(body), len(tail))
                rep = {"torn_lines": 0, "bytes_removed": 0, "records_removed": 0,
                       "records_kept": len(self._index), "newline_added": 1, "quarantined": 0,
                       "repair_id": hashlib.sha256(b"newline:" + tail).hexdigest()[:32]}
                self._write_marker(rep)
                self._append_newline(len(raw))
                body = raw + b"\n"
                self.tail_repair = rep
            else:
                # (b) a genuine torn fragment of ONE record only (shorter than a line, no record boundary in it);
                # anything else (a file with its newlines removed, records merged) refuses to open
                if not _torn_fragment(tail):
                    self._index, self.bad_lines = {}, 0
                    raise VaultCorrupt("the vault file ends with more than one torn record (newlines removed or "
                                       "records merged); refusing to open (inspect it)")
                rep = {"torn_lines": 1, "bytes_removed": len(tail), "records_removed": 1,
                       "records_kept": len(self._index), "newline_added": 0, "quarantined": 1,
                       "repair_id": hashlib.sha256(tail).hexdigest()[:32]}
                # (c) the cut bytes go to a 0600 quarantine file next to the vault and a durable marker is written
                # BEFORE the cut (review 08eea6e1 SHOULD 2: a start that fails after the cut records it next time)
                self._quarantine(tail, rep["repair_id"])
                self._write_marker(rep)
                self._cut_tail(len(raw), len(body))
                self.tail_repair = rep
        elif self._repair:
            self.tail_repair = self._read_marker()               # a cut from an earlier start not yet recorded
        self._size = len(body)

    @property
    def repairs(self) -> bool:
        return self._repair

    def _marker_path(self) -> str:
        return self.path + REPAIR_MARKER_SUFFIX

    def _fsync_dir(self) -> None:
        try:
            dfd = os.open(self.data_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)

    def _quarantine(self, tail: bytes, repair_id: str) -> None:
        """The cut bytes copied to ``<vault>.torn-<repair id>`` (0600; ciphertext fragment only, never decrypted)."""
        qpath = self.path + QUARANTINE_SUFFIX + repair_id
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(qpath, flags, 0o600)
        except FileExistsError:
            try:
                with open(qpath, "rb") as fh:
                    same = fh.read(len(tail) + 1) == tail
            except OSError:
                same = False
            if not same:
                raise VaultCorrupt("the vault's quarantine file exists with other content; refusing") from None
            return                                               # an earlier start copied it, then stopped
        except OSError:
            raise VaultCorrupt("the vault's torn line cannot be quarantined; refusing") from None
        try:
            os.write(fd, tail)
            os.fsync(fd)
        except OSError:
            raise VaultCorrupt("the vault's torn line cannot be quarantined; refusing") from None
        finally:
            os.close(fd)
        self._fsync_dir()

    def _write_marker(self, rep: dict) -> None:
        """The repair's counts written durably (temp file, fsync, rename, directory fsync) before the cut."""
        tmp = self._marker_path() + ".tmp"
        data = json.dumps({k: rep[k] for k in REPAIR_FIELDS}, sort_keys=True).encode("ascii")
        try:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self._marker_path())
        except OSError:
            raise VaultCorrupt("the vault's repair marker cannot be written; refusing") from None
        self._fsync_dir()

    def _read_marker(self) -> Optional[dict]:
        try:
            with open(self._marker_path(), "rb") as fh:
                obj = json.loads(fh.read(4096).decode("ascii"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeDecodeError):
            raise VaultCorrupt("the vault's repair marker cannot be read; refusing (inspect it)") from None
        if not isinstance(obj, dict) or set(obj) != set(REPAIR_FIELDS) \
                or not all(type(obj[k]) is int and obj[k] >= 0 for k in REPAIR_FIELDS if k != "repair_id") \
                or not isinstance(obj["repair_id"], str) or not REPAIR_ID.fullmatch(obj["repair_id"]):
            raise VaultCorrupt("the vault's repair marker is malformed; refusing (inspect it)")
        obj["from_marker"] = True
        return obj

    def clear_repair_marker(self) -> None:
        """The repair is recorded in the ledger: the marker goes (a failure leaves it; the next start records the same
        event again, which the ledger takes as the same event)."""
        try:
            os.unlink(self._marker_path())
        except FileNotFoundError:
            pass
        except OSError:
            return
        self._fsync_dir()

    def _append_newline(self, size: int) -> None:
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            raise VaultCorrupt("the vault file's final newline cannot be added (open failed); refusing") from None
        try:
            if os.fstat(fd).st_size != size:
                raise VaultCorrupt("the vault file changed while it was opened; refusing")
            os.write(fd, b"\n")
            os.fsync(fd)
        except OSError:
            raise VaultCorrupt("the vault file's final newline cannot be added; refusing") from None
        finally:
            os.close(fd)

    def _cut_tail(self, size: int, keep: int) -> None:
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            raise VaultCorrupt("the vault file's torn line cannot be cut (open failed); refusing") from None
        try:
            if os.fstat(fd).st_size != size:
                raise VaultCorrupt("the vault file changed while it was opened; refusing")
            os.ftruncate(fd, keep)
            os.fsync(fd)
        except OSError:
            raise VaultCorrupt("the vault file's torn line cannot be cut; refusing") from None
        finally:
            os.close(fd)

    def note_orphans(self, n: int) -> None:
        with self.lock:
            self.orphans += max(0, int(n))

    def status(self) -> dict:
        """Counts only: records, unreadable lines, orphaned records (written, run not recorded), torn-tail repair."""
        with self.lock:
            return {"records": len(self._index), "bad_lines": self.bad_lines, "orphaned_records": self.orphans,
                    "tail_repair": dict(self.tail_repair) if self.tail_repair else None}

    def put(self, tenant_id: str, record: dict) -> str:
        """Encrypt ``record`` for ``tenant_id`` and append it; returns its vault id (128 random bits, hex)."""
        key = self._key
        if key is None:
            raise VaultUnavailable("the citation vault has no key configured: nothing is stored (fail closed)")
        if not isinstance(tenant_id, str) or not TENANT_ID.fullmatch(tenant_id):
            raise VaultRecordInvalid("the tenant id is not a valid tenant id")
        if type(record) is not dict:
            raise VaultRecordInvalid("a citation record is a JSON object")
        plaintext = _encode(record)
        if plaintext is None:
            raise VaultRecordInvalid("the citation record is not encodable as strict JSON")
        if len(plaintext) > MAX_RECORD_BYTES:
            plaintext = None
            raise VaultRecordInvalid(f"the citation record is larger than {MAX_RECORD_BYTES} bytes; refused")
        material = key._cur
        vid = secrets.token_bytes(16)
        vault_id = vid.hex()
        nonce = _new_nonce()
        ct = material._aead.encrypt(nonce, plaintext, _aad(tenant_id, vault_id, material.key_id))
        plaintext = None
        line = json.dumps({"v": VERSION, "id": _b64(vid), "t": _b64(material.tenant_tag(tenant_id)),
                           "k": _b64(material.key_id.encode("ascii")), "n": _b64(nonce), "c": _b64(ct)},
                          sort_keys=True, separators=(",", ":")).encode("ascii")
        slot = getattr(_COLLECT, "slot", None)
        if slot is not None and slot[0] is self:
            slot[1].lines.append((vault_id, line))          # written by write_batch (or discarded)
            return vault_id
        with self.lock:
            if vault_id in self._index:           # 2**-128; never overwrite or shadow a record
                raise VaultWriteError("vault id collision; nothing written")
            self._append(line)
            self._index[vault_id] = (self._size, len(line))
            self._size += len(line) + 1
        return vault_id

    def collecting(self, batch: VaultBatch):
        """While active (this thread only), ``put`` encrypts into ``batch`` instead of writing."""
        vault = self

        class _Ctx:
            def __enter__(self):
                self.prev = getattr(_COLLECT, "slot", None)
                _COLLECT.slot = (vault, batch)
                return batch

            def __exit__(self, *exc):
                _COLLECT.slot = self.prev
                return False
        return _Ctx()

    def write_batch(self, batch: VaultBatch) -> None:
        """Append every line of ``batch`` at once: one write, one fsync; all or nothing (a failed write is cut back,
        as ``_append``). The batch is emptied either way."""
        lines, batch.lines = list(batch.lines), []
        if not lines:
            return
        with self.lock:
            ids = [vid for vid, _ in lines]
            if len(set(ids)) != len(ids) or any(vid in self._index for vid in ids):
                raise VaultWriteError("vault id collision; nothing written")
            self._append(b"\n".join(line for _, line in lines))
            offset = self._size
            for vid, line in lines:
                self._index[vid] = (offset, len(line))
                offset += len(line) + 1
            self._size = offset

    def _append(self, line: bytes) -> None:
        """store.RecordLog's rules: the file must be exactly what this process wrote, the write is fsynced, a failed
        write is cut back to the old size (or every later write refuses)."""
        if self.fault:
            raise VaultWriteError("an earlier failed vault write could not be cut back; writes refuse until the file "
                                  "is inspected")
        created = not os.path.lexists(self.path)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
        except OSError as exc:
            raise VaultWriteError(f"vault open failed: {type(exc).__name__}") from None
        try:
            if os.fstat(fd).st_size != self._size:
                raise VaultWriteError("the vault file changed outside this process; refusing to write")
            payload = line + b"\n"
            failure = None
            try:
                written = os.pwrite(fd, payload, self._size)
                if written != len(payload):
                    failure = "short write"
                else:
                    os.fsync(fd)
            except OSError as exc:
                failure = type(exc).__name__
            if failure is not None:
                try:
                    os.ftruncate(fd, self._size)
                    os.fsync(fd)
                except OSError as texc:
                    self.fault = f"a failed vault write ({failure}) could not be cut back ({type(texc).__name__})"
                raise VaultWriteError(f"vault write failed: {failure}")
        finally:
            os.close(fd)
        if created:
            try:
                dfd = os.open(self.data_dir, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError as exc:
                raise VaultWriteError(f"vault directory fsync failed: {type(exc).__name__}") from None

    def _read_line(self, vault_id: str) -> Optional[bytes]:
        with self.lock:
            loc = self._index.get(vault_id)
            if loc is None:
                return None
            offset, length = loc
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
            except OSError:
                return None
            try:
                got = os.pread(fd, length + 1, offset)
            except OSError:
                return None
            finally:
                os.close(fd)
        if len(got) != length + 1 or not got.endswith(b"\n"):
            return None
        return got[:-1]

    def get(self, tenant_id: str, vault_id: str) -> dict:
        """The record, only for the tenant that stored it. Every failure is the same ``VaultNotFound``."""
        key = self._key
        if key is None:
            raise VaultUnavailable("the citation vault has no key configured: nothing can be read (fail closed)")
        record = self._open(key, tenant_id, vault_id)
        if record is None:
            raise VaultNotFound(NOT_FOUND)    # raised outside every except block: no chained context
        return record

    def field_hash(self, tenant_id: str, field: str, value: str) -> tuple:
        """(key id, HMAC-SHA256 hex) of one evidence field for the log (ADR 0051 job 3), under the CURRENT key's
        log-field sub-key, bound to ``tenant_id`` and ``field``. No key: ``VaultUnavailable`` (nothing is hashed)."""
        if self._key is None:
            raise VaultUnavailable("no vault key is configured")
        if not isinstance(tenant_id, str) or not TENANT_ID.fullmatch(tenant_id) or not isinstance(field, str) \
                or not FIELD_HASH_NAME.fullmatch(field) or not isinstance(value, str):
            raise VaultRecordInvalid("the field cannot be hashed")
        return self._key.key_id, self._key._cur.field_hash(tenant_id, field, value)

    def _open(self, key: VaultKey, tenant_id, vault_id) -> Optional[dict]:
        if not isinstance(tenant_id, str) or not TENANT_ID.fullmatch(tenant_id):
            return None
        if not isinstance(vault_id, str) or not VAULT_ID.fullmatch(vault_id):
            return None
        line = self._read_line(vault_id)
        parsed = _parse_line(line) if line is not None else None
        if parsed is None or parsed["vault_id"] != vault_id:
            # ADR 0051 (review adacb2ca NIT): the tenant HMAC is computed for an unknown id too, so that path does the
            # same keyed work as a wrong tenant's (it still does no file read: the paths are not timing-equal)
            _tenant_matches(key._cur, tenant_id, b"\0" * 32)
            return None
        material = key._material(parsed["key_id"])
        if material is None or not _tenant_matches(material, tenant_id, parsed["tag"]):
            return None
        plaintext = None
        try:
            plaintext = material._aead.decrypt(parsed["nonce"], parsed["ct"],
                                               _aad(tenant_id, vault_id, material.key_id))
        except (InvalidTag, ValueError):
            plaintext = None
        if plaintext is None:
            return None
        obj = None
        try:
            obj = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            obj = None
        plaintext = None
        return obj if type(obj) is dict else None


class MemoryCitationVault(CitationVault):
    """The same vault without a file: the service's in-memory mode (no data directory; non-production, tests). Every
    record is the same encrypted line ``CitationVault`` would append, kept in a buffer of this process only; ``put``
    and ``get`` are the base class's, so the encryption, the tenant tag and the fail-closed rules are identical."""

    def __init__(self, key: Optional[VaultKey]):
        if key is not None and type(key) is not VaultKey:
            raise TypeError("key must be a VaultKey or None")
        self._key = key
        self.lock = threading.RLock()
        self.data_dir = None
        self.path = None
        self._index = {}
        self._size = 0
        self._buf = bytearray()
        self.bad_lines = 0
        self.fault = None
        self.orphans = 0
        self.tail_repair = None

    def __repr__(self) -> str:
        return f"MemoryCitationVault(records={len(self._index)}, key={'set' if self._key is not None else 'unset'})"

    __str__ = __repr__

    def _load(self) -> None:
        return None

    def _append(self, line: bytes) -> None:
        if len(self._buf) != self._size:
            raise VaultWriteError("the in-memory vault changed outside put; refusing to write")
        self._buf += line + b"\n"

    def _read_line(self, vault_id: str) -> Optional[bytes]:
        with self.lock:
            loc = self._index.get(vault_id)
            if loc is None:
                return None
            offset, length = loc
            got = bytes(self._buf[offset:offset + length + 1])
        if len(got) != length + 1 or not got.endswith(b"\n"):
            return None
        return got[:-1]

    def lines(self) -> list:
        """The stored (encrypted) lines, for tests that check no plaintext is held."""
        with self.lock:
            return bytes(self._buf).split(b"\n")[:-1]
