"""
Client-owned documents in the tenant's vault (sink job B): audit reports and drift detail.

An audit report (limitations, methodology, findings detail, page titles and URLs, prompt text, the robots matrix) and
a drift report (rules text, changes with URLs and keys) are the client's content. The log and the ledger take machine
values only (sink_guard.py), so:

  * the WHOLE document is stored as one blob in the tenant's citation vault (``vault.put_blob``: chunked, AES-GCM,
    bound to the tenant and the blob id). It is encrypted OUTSIDE the service lock into a pending batch; under the lock
    there is one vault append (one fsync) and then the commit;
  * the log record keeps the blob ref, the keyed HMAC of the document's canonical JSON (``vault.field_hash``: the
    vault key's log-field sub-key, bound to the tenant and the field name; never a plain SHA-256 of client text) and
    the machine facts ``_apply`` and the non-owner surfaces need (ids, outcome codes, counts);
  * a reader loads the blob for the OWNING tenant outside the lock and checks the HMAC. A blob that is lost, changed,
    another tenant's, or unreadable without the key is ``None``: the view says ``vault_unavailable`` and every
    dependent operation (client report, drift) refuses ``VAULT_UNAVAILABLE``;
  * a blob written whose commit failed is an orphan: counted (``vault.note_orphans``), never served (nothing holds
    its ref).

The owner's view of a document goes through ``owner_view``: every string that is not a machine value passes the
client-view masker (the same one as the citation evidence and the operator text; a masked value is replaced whole, a
URL keeps every part but the offending ones).
"""

from __future__ import annotations

import json
from typing import Optional

import sink_guard
import vault as vault_mod
from errors import Unavailable
from reasons import R
from svc_optext import text_view, url_view

REPORT_FIELD = "audit_report"
DRIFT_FIELD = "drift_report"
SECTION_FIELD = "gsc_section"
# machine classes a secret or personal data cannot hide in (the strict check already passed, or the shape is closed)
_PLAIN_CLASSES = frozenset(("empty", "service_id", "scoped_id", "invoice_id", "request_id", "timestamp", "date",
                            "number", "version", "boolean", "actor", "hostname", "code"))


def canonical(obj) -> str:
    """The canonical JSON of a document (sorted keys, no spaces, ASCII): what the keyed HMAC is computed over."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalised(obj):
    """``obj`` as it reads back from the vault (JSON types only, string keys), so the HMAC computed at the write is
    the HMAC a reader computes. A value JSON cannot carry is written as its ``str`` (as the report hash always did)."""
    return json.loads(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False))


def _shown(text: str, memo: dict) -> str:
    got = memo.get(text)
    if got is None:
        if sink_guard.classify(text) in _PLAIN_CLASSES:
            got = text
        elif text.startswith(("http://", "https://")) and len(text.split()) == 1:
            got = url_view(text)[0]
        else:
            got = text_view(text)[0]
        memo[text] = got
    return got


def owner_view(obj, memo: Optional[dict] = None):
    """The owner's view of a vaulted document: keys and values through the client-view masker. Two keys masked to
    the same text stay apart (``<mask> (2)``)."""
    memo = {} if memo is None else memo
    if isinstance(obj, str):
        return _shown(obj, memo)
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            shown = _shown(k, memo) if isinstance(k, str) else k
            if shown in out:
                n = 2
                while f"{shown} ({n})" in out:
                    n += 1
                shown = f"{shown} ({n})"
            out[shown] = owner_view(v, memo)
        return out
    if isinstance(obj, (list, tuple)):
        return [owner_view(v, memo) for v in obj]
    return obj


class Sealed:
    """A document prepared for the vault outside the service lock: its pending (encrypted, unwritten) lines, its blob
    ref and its keyed HMAC. Holds no plaintext."""

    __slots__ = ("pending", "ref", "hmac")

    def __init__(self, pending, ref: str, mac: str):
        self.pending, self.ref, self.hmac = pending, ref, mac

    def discard(self) -> None:
        self.pending.discard()


class VaultedMixin:
    def _vaulted_key_check(self) -> None:
        """No vault key: refused up front, nothing written (there is never a plaintext fallback)."""
        if not self.vault.key_configured:
            raise Unavailable(R("VAULT_UNAVAILABLE"))

    def _vaulted_hmac(self, tid: str, field: str, obj) -> str:
        """The keyed HMAC of ``obj``'s canonical JSON. Raises ``vault.VaultError`` (no key, a bad value)."""
        return self.vault.field_hash(tid, field, canonical(obj))[1]

    def _vaulted_seal(self, tid: str, field: str, obj) -> Sealed:
        """OUTSIDE the service lock: ``obj`` (already ``normalised``) encrypted for ``tid`` into a pending batch, and
        its keyed HMAC. Nothing is written. Raises ``vault.VaultError``."""
        pending = vault_mod.VaultBatch()
        try:
            mac = self._vaulted_hmac(tid, field, obj)
            with self.vault.collecting(pending):
                ref = self.vault.put_blob(tid, obj)
        except BaseException:
            pending.discard()
            raise
        return Sealed(pending, ref, mac)

    def _vaulted_commit(self, sealed: Sealed, commit) -> None:
        """UNDER the service lock: the pending lines in one append (one fsync), then ``commit()``. A commit that fails
        after the append leaves an orphan blob: counted, and the failure is raised."""
        written = len(sealed.pending)
        self.vault.write_batch(sealed.pending)
        try:
            commit()
        except BaseException:
            self.vault.note_orphans(written)
            raise

    def _vaulted_open(self, tid: str, field: str, ref, mac):
        """OUTSIDE the service lock: the document for its OWNING tenant, or None (no key, a lost or changed blob,
        another tenant's ref, an HMAC that does not match). Never raises."""
        try:
            obj = self.vault.get_blob(tid, ref)
            if not self.vault.field_hash_matches(tid, field, canonical(obj), mac):
                return None
            return obj
        except (vault_mod.VaultError, TypeError, ValueError, RecursionError):
            return None
