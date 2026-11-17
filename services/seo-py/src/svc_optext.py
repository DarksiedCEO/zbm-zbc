"""
Operator free text in the tenant's vault (review c0c09bd1, design change "Option B for operator text").

Prompt-set prompts and brand-term context / negative terms, entity values and provenance, audit / schedule paths (with
their queries) and URL-prefix Search Console properties are typed by the operator for a purpose (sent to an engine,
fetched, queried, compared). Refusing them when they fail the strict check refused 20-30 % of legitimate input
("how to reset my password on X", "/reset-password", "/cart?variant=<14 digits>"). Instead:

  * the full text is stored in the tenant's citation vault (vault.py: AES-GCM under the tenant's binding, the same
    mechanism as citation evidence, ADR 0051; its own file ``operator_text_vault.jsonl``), ONE record per
    request (``{"kind": KIND, "field": ..., "texts": [...]}``);
  * the log record keeps, per text, only a STUB: ``{"vault_id", "i", "key_id", "hmac", "flag"}`` (the vault id, the
    text's index in the record, the keyed HMAC of the text bound to tenant + field (vault.field_hash), and the strict
    check's verdict name or None). Content hashes, request hashes (idempotency) and ledger evidence are computed over the
    HMACs, never over the text;
  * memory holds the text (the apply handler opens the stubs: the same path live and on replay); a stub that cannot be
    opened (no key, a record lost) leaves the text unavailable and every use of it fails closed (VAULT_UNAVAILABLE);
  * the owner's reads (GET prompt set, entity, audit / schedule config, tenant) decrypt for the owning tenant only and
    show the text through the client-view masker (callum._display).

Refused before anything is written or sent: a strong-key prefix, a PEM / PGP block, URL user info with a password
(secret_guard.operator_refusal) and, in a URL that is fetched or queried, a value under a credential query key
(secret_guard.operator_url_refusal). No vault key configured: the request is refused VAULT_UNAVAILABLE (fail closed;
operator text is never kept in plaintext instead).
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import vault as vault_mod
from agents import secret_guard
from errors import Unavailable
from reasons import R

KIND = "operator_text/v1"
STUB_KEYS = frozenset(("vault_id", "i", "key_id", "hmac", "flag"))


def is_stub(x) -> bool:
    return isinstance(x, dict) and set(x) == STUB_KEYS


def stub_hmac(x):
    """What a hash / an idempotency body carries for a text: its HMAC (a stub's), the text itself never."""
    return x["hmac"] if is_stub(x) else x


def url_flag(url: str):
    """The strict verdict of a fetched URL (``credential`` / ``personal``) or None."""
    try:
        return secret_guard._url_verdict(url)
    except Exception:  # noqa: BLE001 - fail closed
        return secret_guard.CREDENTIAL


def path_view(scheme: str, domain: str, path: str) -> str:
    """The owner's view of an audited path: the client-view URL masker on the full URL, the path part returned."""
    shown, kind = secret_guard.client_view_url(f"{scheme}://{domain}{path}")
    if kind is None:
        return path
    if shown is None:
        return secret_guard.CLIENT_HIDDEN
    p = urlsplit(shown)
    return urlunsplit(("", "", p.path or "/", p.query, p.fragment))


def text_view(text) -> tuple:
    """(shown, alert) for the owner's view of an operator text: the client-view text masker (callum._display)."""
    from agents import callum
    if text is None:
        return None, False
    d = callum._display("operator_text", text)
    return d["value"], bool(d["alert"])


def url_view(url) -> tuple:
    from agents import callum
    if url is None:
        return None, False
    d = callum._display("cited_url", url)
    return d["value"], bool(d["alert"])


class OperatorTextMixin:
    def _optext_key_check(self) -> None:
        if not self.optext_vault.key_configured:
            raise Unavailable(R("VAULT_UNAVAILABLE"))

    def _optext_hmac(self, tid: str, field: str, text: str) -> str:
        """The keyed HMAC of ``text`` (tenant + field bound). No key: VAULT_UNAVAILABLE."""
        try:
            return self.optext_vault.field_hash(tid, field, text)[1]
        except vault_mod.VaultUnavailable:
            raise Unavailable(R("VAULT_UNAVAILABLE")) from None
        except vault_mod.VaultError:
            raise Unavailable(R("STORE_UNAVAILABLE")) from None

    def _optext_seal(self, tid: str, field: str, texts: list, flags: list | None = None) -> list:
        """One vault record holding ``texts``; returns one stub per text (``flags``: the strict verdicts, else
        ``secret_guard.check`` of each)."""
        if not texts:
            return []
        try:
            vid = self.optext_vault.put(tid, {"kind": KIND, "field": field, "texts": list(texts)})
            kid = self.optext_vault.field_hash(tid, field, "")[0]
        except vault_mod.VaultUnavailable:
            raise Unavailable(R("VAULT_UNAVAILABLE")) from None
        except vault_mod.VaultError:
            raise Unavailable(R("STORE_UNAVAILABLE")) from None
        self._optext_pending = getattr(self, "_optext_pending", 0) + 1
        return [{"vault_id": vid, "i": i, "key_id": kid, "hmac": self._optext_hmac(tid, field, t),
                 "flag": (flags[i] if flags is not None else secret_guard.check(t))} for i, t in enumerate(texts)]

    def _optext_committed(self) -> None:
        self._optext_pending = 0

    def _optext_orphaned(self) -> None:
        """A request whose vault record was written but whose log line was not: counted (as citation orphans)."""
        n = getattr(self, "_optext_pending", 0)
        if n:
            self.optext_vault.note_orphans(n)
        self._optext_pending = 0

    def _optext_open(self, tid: str, x, cache: dict | None = None):
        """The text of a stub (a plain string from a record written before review c0c09bd1 is returned as is), or None
        when it cannot be opened (no key, a lost record, another tenant's record)."""
        if not is_stub(x):
            return x
        cache = {} if cache is None else cache
        vid = x["vault_id"]
        if vid not in cache:
            try:
                rec = self.optext_vault.get(tid, vid)
            except vault_mod.VaultError:
                rec = None
            cache[vid] = rec if isinstance(rec, dict) and rec.get("kind") == KIND else None
        rec = cache[vid]
        texts = rec.get("texts") if rec is not None else None
        i = x["i"]
        if not isinstance(texts, list) or not isinstance(i, int) or not 0 <= i < len(texts) \
                or not isinstance(texts[i], str):
            return None
        return texts[i]
