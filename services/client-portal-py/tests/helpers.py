"""Test harness: an in-process ASGI driver (no server, no network) and a fake of Legal's two portal routes."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import config as config_mod
from app import PortalApp
from legal_client import AcceptOutcome, Accepted, Display, Document, LegalUnavailable, PortalView, Receipt

# Legal's own page renderer (standard library only), loaded by path: the fake Legal below answers with the blocks the
# real Legal renders, so the page's words in these tests are the founder-approved clickwrap text (AEGIS M-5)
_spec = importlib.util.spec_from_file_location(
    "legal_clickwrap_page", Path(__file__).resolve().parents[2] / "legal-py" / "src" / "clickwrap_page.py")
CP = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CP)

ORIGIN = "https://accept.zbm.example"
ENV = {"PORTAL_LEGAL_URL": "http://legal.railway.internal:8420",
       "PORTAL_LEGAL_SERVICE_TOKEN": "test-portal-legal-service-token-do-not-use",
       "PORTAL_LEGAL_CALLER_TOKEN": "test-portal-legal-caller-token-do-not-use-0",
       "PORTAL_EVIDENCE_KEY": "test-portal-evidence-key-do-not-use-0123456789",
       "PORTAL_PUBLIC_ORIGIN": ORIGIN}
TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE"
MSA_TEXT = "# Z Best Media Master Services Agreement\n\n1. Services. ZBM provides the services in each SOW.\n"
SOW_TEXT = "# Statement of Work SOW-2026-001\n\nFee: 1,500.00 USD, due in 15 days.\n"
DISPLAY = Display("Acme LLC", "legal@acme.example", "Pat", "SOW-2026-001", "SEO and AI Visibility Audit")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def view(page="first_engagement", docs=None, display=DISPLAY, msa_acc=(None, None)) -> PortalView:
    """A link as Legal answers it: Legal renders blocks only for a page it serves (an MSA and a SOW; a SOW alone for
    a client with a counted MSA acceptance), and only with the SOW's number and title."""
    docs = docs if docs is not None else [Document("client_msa", "1.0", sha(MSA_TEXT), MSA_TEXT),
                                          Document("sow_seo_audit", "1.1", sha(SOW_TEXT), SOW_TEXT)]
    disp = dataclasses.asdict(display)
    docs = [dataclasses.replace(d, label=CP.doc_label(d.doc_id, disp) if disp["sow_number"] else "") for d in docs]
    ids = [d.doc_id for d in docs]
    servable = disp["sow_number"] and disp["sow_title"] and (
        (page == "first_engagement" and "client_msa" in ids and len(ids) == 2)
        or (page == "new_sow" and "client_msa" not in ids and msa_acc[0]))
    bl = ()
    if servable:
        ordered = sorted(docs, key=lambda d: d.doc_id != "client_msa")
        bl = tuple(CP.blocks(page, disp, [dataclasses.asdict(d) for d in ordered],
                             {"version": msa_acc[0], "accepted_at": msa_acc[1]} if msa_acc[0] else None))
    page_sha = hashlib.sha256(CP.page_text(list(bl)).encode()).hexdigest() if bl else None
    return PortalView("lg-cin-" + "A" * 26, page, "2026-10-15T17:00:00Z", display, tuple(docs), *msa_acc,
                      blocks=bl, page_sha256=page_sha)


@dataclass
class FakeLegal:
    views: dict = field(default_factory=dict)
    unavailable: bool = False
    accept_kind: str = "accepted"
    resolves: list = field(default_factory=list)
    accepts: list = field(default_factory=list)
    receipts: list = field(default_factory=list)
    used: set = field(default_factory=set)
    used_by: dict = field(default_factory=dict)        # token -> (request_id, AcceptOutcome)

    def resolve(self, token: str) -> Optional[PortalView]:
        self.resolves.append(token)
        if self.unavailable:
            raise LegalUnavailable("down")
        if token in self.used:
            return None
        return self.views.get(token)

    def receipt(self, token: str, request_id: str):
        """Legal's receipt: only for the submission (request id) that used this link."""
        self.receipts.append((token, request_id))
        if self.unavailable:
            raise LegalUnavailable("down")
        got = self.used_by.get(token)
        if not got or got[0] != request_id:
            return None
        v = self.views[token]
        return Receipt(v.invitation_id, v.display, v.documents, got[1].acceptances, "2026-10-01T17:05:00Z")

    def accept(self, body: dict) -> AcceptOutcome:
        self.accepts.append(body)
        if self.unavailable:
            raise LegalUnavailable("down")
        if self.accept_kind != "accepted":
            return AcceptOutcome(self.accept_kind, codes=("ACCEPTANCE_HASH_MISMATCH",))
        if body["token"] in self.used:
            # Legal answers an identical retry from its idempotency record; any other use of a used link is 404
            prior = [b for b in self.accepts[:-1] if b["token"] == body["token"]]
            if not prior or prior[0]["request_id"] != body["request_id"]:
                return AcceptOutcome("not_found")
        self.used.add(body["token"])
        out = AcceptOutcome("accepted", tuple(
            Accepted("lg-acc-" + str(i) * 26, d["doc_id"], d["version"], d["doc_sha256"], "2026-10-01T17:05:00Z")
            for i, d in enumerate(body["documents"], start=1)))
        self.used_by.setdefault(body["token"], (body["request_id"], out))
        return out


@dataclass
class Resp:
    status: int
    headers: dict
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


def settings(**over):
    return config_mod.load({**ENV, **over})


class Clock:
    def __init__(self, t: float = 1_790_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(legal: Optional[FakeLegal] = None, env: Optional[dict] = None, **kw):
    legal = legal or FakeLegal(views={TOKEN: view()})
    clock, mono = Clock(), Clock(1000.0)
    app = PortalApp(settings(**(env or {})), legal, clock=clock, monotonic=mono, **kw)
    return app, legal, clock, mono


def call(app, method: str, path: str, headers: Optional[dict] = None, body: bytes = b"",
         client=("203.0.113.7", 50000), chunks: Optional[list] = None) -> Resp:
    hdrs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode("utf-8"), "query_string": b"",
             "headers": hdrs, "client": client, "scheme": "https", "http_version": "1.1"}
    msgs = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)] \
        if chunks is not None else [{"type": "http.request", "body": body, "more_body": False}]
    out = {"status": None, "headers": {}, "body": b""}

    async def receive():
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
            for k, v in m["headers"]:
                out["headers"][k.decode("latin-1")] = v.decode("latin-1")
        else:
            out["body"] += m.get("body", b"")
    asyncio.run(app(scope, receive, send))
    return Resp(out["status"], out["headers"], out["body"])


def hidden(page_html: str) -> dict:
    return dict(re.findall(r'<input type="hidden" name="([a-z_0-9]+)" value="([^"]*)"', page_html))


def form_post(app, token=TOKEN, fields=None, headers=None, drop=(), client=("203.0.113.7", 50000), get=None):
    """GET the page, fill the form as a client would, POST it from the page's own origin."""
    page = get or call(app, "GET", f"/accept/{token}", client=client)
    assert page.status == 200, page.text[:500]
    data = hidden(page.text)
    data.update({"accept_doc_1": "yes", "accept_doc_2": "yes", "full_name": "Patricia Q. Example",
                 "title": "Managing Member", "company": "Acme LLC"})
    data.update(fields or {})
    for k in drop:
        data.pop(k, None)
    h = {"content-type": "application/x-www-form-urlencoded", "origin": ORIGIN, "sec-fetch-site": "same-origin",
         "user-agent": "Mozilla/5.0 (TestBrowser)"}
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}
    return call(app, "POST", f"/accept/{token}", h, urlencode(data).encode("utf-8"), client=client)
