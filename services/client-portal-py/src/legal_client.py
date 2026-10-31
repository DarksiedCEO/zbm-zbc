"""
The typed adapter to Legal (37)'s two portal routes (ADR 0028). Nothing else in this service talks to Legal, and
nothing here trusts Legal's answer's shape: every field is checked, and each document's text is hashed here and must
equal the hash Legal names (a mismatch is treated as Legal being unavailable: nothing is shown, nothing is recorded).

- ``resolve(token)`` -> ``PortalView`` | ``None`` (Legal's uniform 404) ; raises ``LegalUnavailable``.
- ``accept(body)``   -> ``AcceptOutcome`` (accepted / not_found / refused) ; raises ``LegalUnavailable``.
- ``receipt(token, request_id)`` -> ``Receipt`` | ``None`` : the receipt of the submission that used the link (AEGIS
  M-2: a retry after a lost response or a refresh) ; raises ``LegalUnavailable``.

The page's words are Legal's (AEGIS M-5): ``PortalView.blocks`` is the page Legal renders; this adapter checks that
the blocks' text hashes to Legal's ``page_sha256`` and that each document block carries the document's exact text.

The token travels in a JSON body (never a URL Legal could log); the client's IP address and user agent never travel
at all (the app sends HMACs made with its own key).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

DOC_IDS = ("client_msa", "sow", "sow_seo_audit", "sow_revenue_recovery")
PAGES = ("first_engagement", "new_sow", "msa_only")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}$")
_LG_ID = re.compile(r"^lg-[a-z]{3}-[0-9A-Z]{26}$")
_TS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z$")
CALLER_HEADER = "X-LEGAL-Caller-Token"
MAX_TEXT = 5 * 1024 * 1024


class LegalUnavailable(Exception):
    """Legal could not be reached, or answered something this adapter does not accept (fail closed)."""


@dataclass(frozen=True)
class Document:
    doc_id: str
    version: str
    doc_sha256: str
    text: str
    label: str = ""


@dataclass(frozen=True)
class Display:
    client_legal_name: str
    notice_email: str
    contact_first_name: Optional[str]
    sow_number: Optional[str]
    sow_title: Optional[str]


@dataclass(frozen=True)
class PortalView:
    invitation_id: str
    page: str
    expires_at: str
    display: Display
    documents: tuple[Document, ...]
    msa_accepted_version: Optional[str]
    msa_accepted_at: Optional[str]
    blocks: tuple = ()
    page_sha256: Optional[str] = None

    @property
    def page_text(self) -> str:
        return page_text(self.blocks)

    def doc(self, kind: str) -> Optional[Document]:
        for d in self.documents:
            if (kind == "msa" and d.doc_id == "client_msa") or (kind == "sow" and d.doc_id != "client_msa"):
                return d
        return None


@dataclass(frozen=True)
class Accepted:
    acceptance_id: str
    doc_id: str
    version: str
    doc_sha256: str
    accepted_at: str


@dataclass(frozen=True)
class Receipt:
    invitation_id: str
    display: Display
    documents: tuple
    acceptances: tuple
    accepted_at: str

    def doc(self, kind: str) -> Optional[Document]:
        for d in self.documents:
            if (kind == "msa" and d.doc_id == "client_msa") or (kind == "sow" and d.doc_id != "client_msa"):
                return d
        return None


@dataclass(frozen=True)
class AcceptOutcome:
    kind: str                                   # "accepted" | "not_found" | "refused"
    acceptances: tuple[Accepted, ...] = ()
    codes: tuple[str, ...] = ()


def _s(obj: Any, key: str, maximum: int, optional: bool = False) -> Optional[str]:
    v = obj.get(key) if isinstance(obj, dict) else None
    if v is None and optional:
        return None
    if not isinstance(v, str) or not v or len(v) > maximum:
        raise LegalUnavailable(f"Legal's answer: {key} not understood")
    return v


def _match(rx: re.Pattern, v: Any, what: str) -> str:
    if not isinstance(v, str) or not rx.fullmatch(v):
        raise LegalUnavailable(f"Legal's answer: {what} not understood")
    return v


class LegalClient:
    def __init__(self, base_url: str, service_token: str, caller_token: str, timeout: float = 5.0,
                 transport: Optional[httpx.BaseTransport] = None):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {service_token}", CALLER_HEADER: caller_token}
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, body: dict) -> httpx.Response:
        try:
            return self._client.post(self._base + path, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise LegalUnavailable(f"Legal unreachable ({type(exc).__name__})") from None

    @staticmethod
    def _json(r: httpx.Response) -> Any:
        if len(r.content) > 2 * MAX_TEXT + 1024 * 1024:
            raise LegalUnavailable("Legal's answer is too large")
        try:
            return r.json()
        except ValueError:
            raise LegalUnavailable("Legal's answer is not JSON") from None

    def resolve(self, token: str) -> Optional[PortalView]:
        r = self._post("/legal/v1/portal/invitations/resolve", {"token": token})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise LegalUnavailable(f"Legal answered {r.status_code}")
        return parse_view(self._json(r))

    def receipt(self, token: str, request_id: str) -> Optional["Receipt"]:
        r = self._post("/legal/v1/portal/receipts", {"token": token, "request_id": request_id})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise LegalUnavailable(f"Legal answered {r.status_code}")
        return parse_receipt(self._json(r))

    def accept(self, body: dict) -> AcceptOutcome:
        r = self._post("/legal/v1/portal/acceptances", body)
        if r.status_code == 404:
            return AcceptOutcome("not_found")
        if r.status_code in (409, 422):
            data = self._json(r)
            codes = tuple(sorted({x.get("code") for x in data.get("reasons", []) if isinstance(x, dict)
                                  and isinstance(x.get("code"), str)}))[:20] if isinstance(data, dict) else ()
            return AcceptOutcome("refused", codes=codes)
        if r.status_code not in (200, 201):
            raise LegalUnavailable(f"Legal answered {r.status_code}")
        return parse_accepted(self._json(r), body)


BLOCK_KINDS = ("title", "p", "doc_heading", "meta", "links", "doc_text", "check", "field", "actions")
FIELD_IDS = ("full_name", "title", "company")


def page_text(blocks) -> str:
    return "\n\n".join(b["text"] for b in blocks) + "\n" if blocks else ""


def _blocks(raw: Any, docs: list) -> tuple:
    if raw is None or raw == []:
        return ()
    if not isinstance(raw, list) or len(raw) > 40:
        raise LegalUnavailable("Legal's answer: blocks not understood")
    out = []
    for b in raw:
        if not isinstance(b, dict) or b.get("k") not in BLOCK_KINDS or not isinstance(b.get("text"), str):
            raise LegalUnavailable("Legal's answer: a block is not understood")
        if b["k"] in ("doc_heading", "meta", "links", "doc_text", "check"):
            if b.get("n") not in range(1, len(docs) + 1):
                raise LegalUnavailable("Legal's answer: a block names no document")
        if b["k"] == "doc_text":
            shown = [d for d in docs if d.doc_id == "client_msa"] + [d for d in docs if d.doc_id != "client_msa"]
            if b["text"] not in [d.text for d in shown]:
                raise LegalUnavailable("a document block is not a document's exact text: nothing is shown")
        if b["k"] == "field" and (b.get("id") not in FIELD_IDS or not isinstance(b.get("label"), str)):
            raise LegalUnavailable("Legal's answer: a field block is not understood")
        if b["k"] == "check" and not isinstance(b.get("label"), str):
            raise LegalUnavailable("Legal's answer: a check block is not understood")
        if len(b["text"]) > MAX_TEXT or any(isinstance(v, str) and len(v) > 2000 for k, v in b.items()
                                            if k not in ("text",)):
            raise LegalUnavailable("Legal's answer: a block is too large")
        out.append(dict(b))
    return tuple(out)


def _documents(docs_raw: Any) -> list:
    if not isinstance(docs_raw, list) or not 1 <= len(docs_raw) <= 2:
        raise LegalUnavailable("Legal's answer: documents not understood")
    docs = []
    for d in docs_raw:
        if not isinstance(d, dict) or d.get("doc_id") not in DOC_IDS:
            raise LegalUnavailable("Legal's answer: a document is not understood")
        text = d.get("text")
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise LegalUnavailable("Legal's answer: a document text is not understood")
        digest = _match(_SHA, d.get("doc_sha256"), "doc_sha256")
        if hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest() != digest:
            raise LegalUnavailable("a document's text does not match its SHA-256: nothing is shown")
        label = d.get("label") if isinstance(d.get("label"), str) and len(d.get("label")) <= 300 else ""
        docs.append(Document(d["doc_id"], _match(_VERSION, d.get("version"), "version"), digest, text, label))
    if len({d.doc_id for d in docs}) != len(docs):
        raise LegalUnavailable("Legal's answer: a document is named twice")
    return docs


def _display(disp: Any) -> Display:
    return Display(_s(disp, "client_legal_name", 200), _s(disp, "notice_email", 254),
                   _s(disp, "contact_first_name", 120, True), _s(disp, "sow_number", 60, True),
                   _s(disp, "sow_title", 200, True))


def parse_receipt(data: Any) -> Receipt:
    if not isinstance(data, dict):
        raise LegalUnavailable("Legal's answer is not an object")
    docs = _documents(data.get("documents"))
    accs = parse_accepted({"acceptances": data.get("acceptances")},
                          {"documents": [{"doc_id": d.doc_id, "version": d.version, "doc_sha256": d.doc_sha256}
                                         for d in docs]})
    return Receipt(_match(_LG_ID, data.get("invitation_id"), "invitation_id"), _display(data.get("display")),
                   tuple(docs), accs.acceptances, _match(_TS, data.get("accepted_at"), "accepted_at"))


def parse_view(data: Any) -> PortalView:
    if not isinstance(data, dict):
        raise LegalUnavailable("Legal's answer is not an object")
    page = data.get("page")
    if page not in PAGES:
        raise LegalUnavailable("Legal's answer: page not understood")
    docs = _documents(data.get("documents"))
    display = _display(data.get("display"))
    blocks = _blocks(data.get("blocks"), docs)
    page_sha = data.get("page_sha256")
    if blocks:
        _match(_SHA, page_sha, "page_sha256")
        if hashlib.sha256(page_text(blocks).encode("utf-8")).hexdigest() != page_sha:
            raise LegalUnavailable("the page blocks do not hash to Legal's page_sha256: nothing is shown")
    msa = data.get("msa_acceptance")
    mv = ma = None
    if msa is not None:
        mv = _match(_VERSION, msa.get("version") if isinstance(msa, dict) else None, "msa_acceptance.version")
        ma = _match(_TS, msa.get("accepted_at"), "msa_acceptance.accepted_at")
    return PortalView(_match(_LG_ID, data.get("invitation_id"), "invitation_id"), page,
                      _match(_TS, data.get("expires_at"), "expires_at"), display, tuple(docs), mv, ma,
                      blocks, page_sha if blocks else None)


def parse_accepted(data: Any, body: dict) -> AcceptOutcome:
    """Every acceptance must be evidence-sufficient, recorded by this caller, and exactly the documents submitted."""
    if not isinstance(data, dict) or not isinstance(data.get("acceptances"), list):
        raise LegalUnavailable("Legal's answer: acceptances not understood")
    out = []
    for a in data["acceptances"]:
        if not isinstance(a, dict) or a.get("evidence_sufficient") is not True or a.get("via") != "client_portal" \
                or a.get("basis_in_force") is not True:
            raise LegalUnavailable("Legal's answer: an acceptance is not a counted page acceptance")
        out.append(Accepted(_match(_LG_ID, a.get("acceptance_id"), "acceptance_id"),
                            a.get("doc_id") if a.get("doc_id") in DOC_IDS else _match(re.compile("^$"), "x", "doc_id"),
                            _match(_VERSION, a.get("version"), "version"), _match(_SHA, a.get("doc_sha256"), "sha"),
                            _match(_TS, a.get("accepted_at"), "accepted_at")))
    sent = sorted((d["doc_id"], d["version"], d["doc_sha256"]) for d in body["documents"])
    if sorted((a.doc_id, a.version, a.doc_sha256) for a in out) != sent:
        raise LegalUnavailable("Legal's answer names other documents than the ones submitted")
    return AcceptOutcome("accepted", tuple(out))
