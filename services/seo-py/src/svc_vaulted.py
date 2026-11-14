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
from primitives import codes as codes_mod
from reasons import R
from svc_optext import text_view, url_view

REPORT_FIELD = "audit_report"
DRIFT_FIELD = "drift_report"
SECTION_FIELD = "gsc_section"
# sink job D: the last free text leaves the log
ENTITY_FIELD = "entity_content"              # one blob per entity version (operator text vault)
PROMPT_SET_FIELD = "prompt_set_content"      # one blob per prompt-set version (operator text vault)
INGEST_INPUTS_FIELD = "log_ingest_inputs"    # robots.txt and the sitemap sample fixed when an ingest is created
LOG_DELTA_FIELD = "log_chunk_delta"          # one chunk's aggregate delta (path templates are its keys)
LOG_REPORT_FIELD = "log_report"              # the finished ingest's report
EVIDENCE_FIELD = "evidence_map"              # spec 0.1: {code: raw value} of one audit, read only for owner displays
REQUEST_FIELD = "request_text"               # a non-machine string of a request body, in its idempotency hash
REQUEST_TENANT = "seo"                       # the binding of ``REQUEST_FIELD`` (the request key scopes the request)
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


_CODE_SLOT = "{c}"          # stands for one store code while the rest of a text goes through the masker


def _masked(text: str) -> str:
    if sink_guard.classify(text) in _PLAIN_CLASSES:
        return text
    if text.startswith(("http://", "https://")) and len(text.split()) == 1:
        return url_view(text)[0]
    return text_view(text)[0]


def _shown(text: str, memo: dict) -> str:
    """One string of an owner view through the client-view masker. A store code (wave1-spec/1 0.1) is a machine
    value: its 16 hex digits are never judged (a digit-heavy code looked like a phone number to the masker and was
    hidden, about 2 in 10,000); the text around the codes is masked as before."""
    got = memo.get(text)
    if got is None:
        codes = [m.group(0) for m in codes_mod.CODE_RX.finditer(text)]
        if codes and _CODE_SLOT not in text:
            got = _masked(codes_mod.CODE_RX.sub(_CODE_SLOT, text))
            if got.count(_CODE_SLOT) == len(codes):
                parts = got.split(_CODE_SLOT)
                got = parts[0] + "".join(c + rest for c, rest in zip(codes, parts[1:]))
            else:
                got = _masked(text)                  # the masker changed the text around the codes: as before
        else:
            got = _masked(text)
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

    __slots__ = ("pending", "ref", "hmac", "vault")

    def __init__(self, pending, ref: str, mac: str, vault=None):
        self.pending, self.ref, self.hmac, self.vault = pending, ref, mac, vault

    def discard(self) -> None:
        self.pending.discard()


class VaultedMixin:
    def _vaulted_key_check(self) -> None:
        """No vault key: refused up front, nothing written (there is never a plaintext fallback)."""
        if not self.vault.key_configured:
            raise Unavailable(R("VAULT_UNAVAILABLE"))

    # ------------------------------------------------------------------ store codes (spec 0.1, B-CD-01)

    def _codes_coder(self, tid: str):
        """The tenant's coder under the current vault key. No key: VAULT_UNAVAILABLE (before any fetch)."""
        try:
            return self.vault.coder(tid)
        except vault_mod.VaultError:
            raise Unavailable(R("VAULT_UNAVAILABLE")) from None

    def _codes_key_id(self):
        return self.vault._key.key_id if self.vault.key_configured else None

    def _codes_own(self, tid: str) -> tuple:
        """D-01 H: the tenant's registered domains (readable with their www. twin)."""
        t = self.tenants.get(tid) or {}
        return tuple(t.get("domains") or ())

    def _vaulted_hmac(self, tid: str, field: str, obj, vault=None) -> str:
        """The keyed HMAC of ``obj``'s canonical JSON. Raises ``vault.VaultError`` (no key, a bad value)."""
        return (self.vault if vault is None else vault).field_hash(tid, field, canonical(obj))[1]

    def _vaulted_seal(self, tid: str, field: str, obj, vault=None) -> Sealed:
        """OUTSIDE the service lock: ``obj`` (already ``normalised``) encrypted for ``tid`` into a pending batch, and
        its keyed HMAC. Nothing is written. Raises ``vault.VaultError``. ``vault``: the operator text vault for
        operator-typed content (sink job D); the citation vault otherwise."""
        vault = self.vault if vault is None else vault       # (a vault has a length: never ``or``)
        pending = vault_mod.VaultBatch()
        try:
            mac = self._vaulted_hmac(tid, field, obj, vault)
            with vault.collecting(pending):
                ref = vault.put_blob(tid, obj)
        except BaseException:
            pending.discard()
            raise
        return Sealed(pending, ref, mac, vault)

    def _vaulted_commit(self, sealed: Sealed, commit) -> None:
        """UNDER the service lock: the pending lines in one append (one fsync), then ``commit()``. A commit that fails
        after the append leaves an orphan blob: counted, and the failure is raised."""
        vault = self.vault if sealed.vault is None else sealed.vault
        written = len(sealed.pending)
        vault.write_batch(sealed.pending)
        try:
            commit()
        except BaseException:
            vault.note_orphans(written)
            raise

    def _vaulted_open(self, tid: str, field: str, ref, mac, vault=None):
        """OUTSIDE the service lock: the document for its OWNING tenant, or None (no key, a lost or changed blob,
        another tenant's ref, an HMAC that does not match). Never raises."""
        vault = self.vault if vault is None else vault       # (a vault has a length: never ``or``)
        try:
            obj = vault.get_blob(tid, ref)
            if not vault.field_hash_matches(tid, field, canonical(obj), mac):
                return None
            return obj
        except (vault_mod.VaultError, TypeError, ValueError, RecursionError):
            return None

    # ------------------------------------------------------------------ sink job D: small operator documents

    def _doc_seal(self, tid: str, field: str, obj, vault=None) -> Sealed:
        """``_vaulted_seal`` for a request path: a vault failure is the request's refusal (VAULT_UNAVAILABLE without
        a key, STORE_UNAVAILABLE otherwise); nothing is written."""
        try:
            return self._vaulted_seal(tid, field, obj, vault)
        except vault_mod.VaultUnavailable:
            raise Unavailable(R("VAULT_UNAVAILABLE")) from None
        except vault_mod.VaultError:
            raise Unavailable(R("STORE_UNAVAILABLE")) from None

    def _doc_commit(self, sealed: Sealed, obj, commit) -> None:
        """UNDER the service lock: ``_vaulted_commit`` for a document whose ``_apply`` handler reads it back. The
        plaintext is handed to the handler in memory (``_doc_handoff``: ref -> document, this commit only), so the
        live path does not decrypt what it has just encrypted; a replay reads the blob."""
        self._doc_handoff[sealed.ref] = obj
        try:
            try:
                self._vaulted_commit(sealed, commit)
            except vault_mod.VaultError:
                raise Unavailable(R("STORE_UNAVAILABLE")) from None
        finally:
            self._doc_handoff.pop(sealed.ref, None)
            sealed.discard()

    def _doc_applied(self, tid: str, field: str, ref, mac, vault=None):
        """In an ``_apply`` handler: the document of a record (the live commit's hand-off, else the blob read for the
        owning tenant with its HMAC checked), or None when it cannot be read."""
        got = self._doc_handoff.get(ref)
        if got is not None:
            return got
        return self._vaulted_open(tid, field, ref, mac, vault)

    def _machine_body(self, body):
        """Sink job D: a request body as its idempotency hash sees it. Every string that is not a machine value
        (sink_guard) is replaced by its keyed HMAC (``REQUEST_FIELD``), so ``request_sha`` is never a plain SHA-256
        over client text; a body of machine values only is returned as it is (its hash is unchanged). No vault key
        and a non-machine string: VAULT_UNAVAILABLE (never a plain hash instead)."""
        if not sink_guard.violations("request_body", body):
            return body
        vault = self.optext_vault

        def walk(x):
            if isinstance(x, str):
                if sink_guard.classify(x) is not None:
                    return x
                try:
                    return vault.field_hash(REQUEST_TENANT, REQUEST_FIELD, x)[1]
                except vault_mod.VaultUnavailable:
                    raise Unavailable(R("VAULT_UNAVAILABLE")) from None
                except vault_mod.VaultError:
                    raise Unavailable(R("STORE_UNAVAILABLE")) from None
            if isinstance(x, dict):
                return {(walk(k) if isinstance(k, str) else k): walk(v) for k, v in x.items()}
            if isinstance(x, (list, tuple)):
                return [walk(v) for v in x]
            return x
        return walk(body)
