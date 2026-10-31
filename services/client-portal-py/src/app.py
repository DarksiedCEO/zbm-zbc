"""
The client acceptance page (ADR 0028): a minimal public ASGI app, no framework.

Routes (anything else: the uniform 404 page; any other method: 405):
  GET  /health                               -> {"status": "ok"} (Railway's health check; says nothing else)
  GET  /robots.txt                           -> disallow everything
  GET  /accept/not-now                       -> "Nothing was recorded"
  GET  /accept/{token}                       -> the acceptance page for this link (Legal resolves the token)
  POST /accept/{token}                       -> record the acceptance at Legal; the confirmation page
  GET  /accept/{token}/document/{n}          -> document n's exact registered text (text/plain, inline)
  GET  /accept/{token}/document/{n}/download -> the same bytes as an attachment

Defenses:
- every unusable token gets the SAME 404 page (malformed, unknown, expired, revoked, used, documents changed): the
  format check, the Legal lookup and the page are identical in every case; Legal compares token hashes in constant
  time;
- rate limits before any Legal call: per client IP (keyed by an HMAC, never the IP), per token (keyed by an HMAC of
  the token) and a global ceiling on not-found answers;
- request limits: target 2 KiB, head 8 KiB, form body 8 KiB (urlencoded only), body read deadline;
- CSRF: the POST must carry the page's own signed form (HMAC over the token's hash, the page text's hash and the
  issue time, at most PORTAL_FORM_MAX_AGE_SECONDS old) and an ``Origin`` equal to PORTAL_PUBLIC_ORIGIN; a
  ``Sec-Fetch-Site`` other than same-origin is refused; there are no cookies and no session;
- strict security headers on every answer: CSP ``default-src 'none'`` with the one stylesheet by hash, no script,
  ``form-action 'self'``, ``frame-ancestors 'none'``; no-store; ``Referrer-Policy: same-origin`` (the token in the
  path never leaves in a Referer, and the browser still sends ``Origin`` on the POST); nosniff; HSTS; noindex;
- nothing logs a token, a name, an email, an IP address or a user agent: the log line is the route name, the status
  and the time taken.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Callable, Optional
from urllib.parse import parse_qs

import render as V
from config import Settings
from legal_client import AcceptOutcome, LegalClient, LegalUnavailable, PortalView
from mail import Attachment, ConfirmationMailer, ConfirmationMessage, NotWiredMailer
from ratelimit import Limiter

log = logging.getLogger("client_portal")

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
ACCEPT_RE = re.compile(r"^/accept/([^/]{1,200})$")
DOC_RE = re.compile(r"^/accept/([^/]{1,200})/document/([12])(/download)?$")
MAX_TARGET = 2048
MAX_HEAD = 8 * 1024
MAX_BODY = 8 * 1024
BODY_TIMEOUT_S = 10.0
FIELD_MAX = {"full_name": 120, "title": 120, "company": 200}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  \ud800-\udfff]")

SECURITY_HEADERS = [
    (b"content-security-policy",
     (f"default-src 'none'; style-src '{V.CSS_HASH}'; img-src 'none'; form-action 'self'; base-uri 'none'; "
      "frame-ancestors 'none'").encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
    (b"cache-control", b"no-store"),
    (b"pragma", b"no-cache"),
    (b"strict-transport-security", b"max-age=31536000"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"x-robots-tag", b"noindex, nofollow"),
]


def _mac(key: bytes, *parts: str) -> str:
    return hmac.new(key, "\x1f".join(parts).encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()


def _norm_company(s: str) -> str:
    return "".join(ch for ch in s.casefold() if ch.isalnum())


class PortalApp:
    def __init__(self, settings: Settings, legal: LegalClient, mailer: Optional[ConfirmationMailer] = None,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 mail_postal_line: Optional[str] = None, mail_next_step: Optional[str] = None):
        self.s, self.legal, self.mailer = settings, legal, mailer or NotWiredMailer()
        if not isinstance(self.mailer, NotWiredMailer) and not (mail_postal_line and mail_next_step):
            raise RuntimeError("a confirmation mailer needs ZBM's postal line and the next-step sentence (Part D)")
        self.mail_postal_line, self.mail_next_step = mail_postal_line, mail_next_step
        self.clock = clock
        self.by_ip = Limiter(settings.ip_per_minute, 60, clock=monotonic)
        self.by_token = Limiter(settings.token_per_hour, 3600, clock=monotonic)
        self.not_found = Limiter(settings.not_found_per_minute, 60, clock=monotonic)

    # --- ASGI ----------------------------------------------------------------------------------------------------

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        t0 = time.monotonic()
        route, status = "other", 500
        try:
            route, status, headers, body = await self.handle(scope, receive)
        except Exception as exc:  # noqa: BLE001 - only the TYPE is logged (never input, never a secret)
            log.error("unhandled error: %s", type(exc).__name__)
            status, headers, body = 500, [(b"content-type", b"text/html; charset=utf-8")], \
                V.UNAVAILABLE.html().encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": SECURITY_HEADERS + headers + [(b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body",
                    "body": b"" if scope.get("method") == "HEAD" else body})
        log.info("%s %s %d %.0fms", scope.get("method", "?")[:7], route, status, (time.monotonic() - t0) * 1000)

    @staticmethod
    def _page(status: int, page: V.Page, extra: Optional[list] = None):
        return status, [(b"content-type", b"text/html; charset=utf-8")] + (extra or []), page.html().encode("utf-8")

    def _client_ip(self, scope) -> str:
        """The client's address: the X-Forwarded-For entry PORTAL_TRUSTED_PROXY_HOPS from the end (Railway's edge
        appends it), else the socket peer. Used only as an HMAC (rate-limit key, evidence)."""
        hops = self.s.trusted_proxy_hops
        if hops:
            xff = [v.decode("latin-1") for k, v in scope.get("headers", ()) if k == b"x-forwarded-for"]
            entries = [x.strip() for x in ",".join(xff).split(",") if x.strip()]
            if len(entries) >= hops:
                return entries[-hops][:64]
        client = scope.get("client") or ("unknown", 0)
        return str(client[0])[:64]

    @staticmethod
    def _header(scope, name: bytes) -> Optional[str]:
        for k, v in scope.get("headers", ()):
            if k == name:
                return v.decode("latin-1")
        return None

    async def handle(self, scope, receive):
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        raw = scope.get("raw_path") or path.encode("utf-8", "surrogatepass")
        target = len(raw) + len(scope.get("query_string") or b"")
        if target > MAX_TARGET:
            return ("too_long", *self._page(414, V.NOT_FOUND))
        if target + sum(len(k) + len(v) + 4 for k, v in scope.get("headers", ())) > MAX_HEAD:
            return ("too_long", *self._page(431, V.NOT_FOUND))
        if path == "/health":
            if method not in ("GET", "HEAD"):
                return ("health", 405, [(b"allow", b"GET, HEAD")], b"")
            return ("health", 200, [(b"content-type", b"application/json")], b'{"status":"ok"}')
        if path == "/robots.txt" and method in ("GET", "HEAD"):
            return ("robots", 200, [(b"content-type", b"text/plain; charset=utf-8")], b"User-agent: *\nDisallow: /\n")
        if path == "/accept/not-now" and method in ("GET", "HEAD"):
            return ("not_now", *self._page(200, V.NOT_NOW))
        m_doc, m_acc = DOC_RE.match(path), ACCEPT_RE.match(path)
        if not (m_doc or m_acc):
            return ("other", *self._page(404, V.NOT_FOUND))
        allowed = ("GET", "HEAD") if m_doc else ("GET", "HEAD", "POST")
        if method not in allowed:
            return ("method", 405, [(b"allow", ", ".join(allowed).encode())], b"")
        token = (m_doc or m_acc).group(1)
        name = "document" if m_doc else ("accept_post" if method == "POST" else "accept_get")
        ip_key = _mac(self.s.evidence_key, "rl-ip", self._client_ip(scope))
        tok_key = _mac(self.s.evidence_key, "rl-token", token)
        ok_ip, retry_ip = self.by_ip.hit(ip_key)
        ok_tok, retry_tok = self.by_token.hit(tok_key)
        if not (ok_ip and ok_tok):
            return (name, *self._page(429, V.RATE_LIMITED, [(b"retry-after", str(max(retry_ip, retry_tok)).encode())]))
        if method == "POST":
            return (name, *await self._post(scope, receive, token))
        view = await self._resolve(token)
        if not isinstance(view, PortalView):
            return (name, *view)
        if m_doc:
            n = int(m_doc.group(2))
            docs = self._shown(view)
            if n > len(docs):
                return (name, *self._not_found())
            doc = docs[n - 1]
            return (name, 200, V.document_text_headers(n, doc, bool(m_doc.group(3))), doc.text.encode("utf-8"))
        return (name, *self._form(view, token))

    # --- helpers ---------------------------------------------------------------------------------------------------

    def _send_confirmation(self, view: PortalView, out: AcceptOutcome, state: "V.FormState") -> bool:
        """Clickwrap Part D. The stand-in sends nothing (False): the page then says ZBM will email the copy, and Andre
        does (runbook). A wired mailer never raises into the request: the acceptance is already recorded."""
        if isinstance(self.mailer, NotWiredMailer):
            return False
        subject, text = V.confirmation_email(view, out.acceptances, state.full_name, state.title, self.s.display_tz,
                                             self.mail_postal_line or "", self.mail_next_step or "")
        try:
            return self.mailer.send(ConfirmationMessage(view.display.notice_email, subject, text,
                                                        _attachments(view, out.acceptances), view.invitation_id)) is True
        except Exception as exc:  # noqa: BLE001 - only the type is logged
            log.error("confirmation mail failed: %s", type(exc).__name__)
            return False

    def _not_found(self):
        ok, retry = self.not_found.hit("global")
        if not ok:
            return self._page(429, V.RATE_LIMITED, [(b"retry-after", str(retry).encode())])
        return self._page(404, V.NOT_FOUND)

    async def _resolve(self, token: str):
        """The link's view, or the answer to send (uniform 404, 429, 503). Pages the portal does not serve (an
        MSA-only link, or a SOW-only link for a client with no counted MSA acceptance) are the same 404."""
        if not TOKEN_RE.fullmatch(token):
            return self._not_found()
        try:
            view = await asyncio.to_thread(self.legal.resolve, token)
        except LegalUnavailable:
            return self._page(503, V.UNAVAILABLE, [(b"retry-after", b"60")])
        if view is None or not self._servable(view):
            return self._not_found()
        return view

    @staticmethod
    def _servable(view: PortalView) -> bool:
        d = view.display
        if view.page == "first_engagement":
            return bool(view.doc("msa") and view.doc("sow") and d.sow_number and d.sow_title)
        if view.page == "new_sow":
            return bool(view.doc("sow") and not view.doc("msa") and d.sow_number and d.sow_title
                        and view.msa_accepted_version and view.msa_accepted_at)
        return False

    @staticmethod
    def _shown(view: PortalView) -> list:
        return [x for x in (view.doc("msa"), view.doc("sow")) if x is not None]

    def _sign(self, token: str, page_sha: str, issued: int) -> str:
        return _mac(self.s.evidence_key, "form-v1", hashlib.sha256(token.encode()).hexdigest(), page_sha, str(issued))

    def _page_sha(self, view: PortalView, token: str) -> str:
        text = V.acceptance_page(view, token, 0, "", None, display_tz=self.s.display_tz).text
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _form(self, view: PortalView, token: str, state: Optional[V.FormState] = None, status: int = 200):
        page_sha = self._page_sha(view, token)
        issued = int(self.clock())
        pg = V.acceptance_page(view, token, issued, self._sign(token, page_sha, issued), page_sha, state,
                               self.s.display_tz)
        return self._page(status, pg)

    async def _read_body(self, scope, receive) -> Optional[bytes]:
        declared = self._header(scope, b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
            return None
        chunks, size = [], 0
        deadline = time.monotonic() + BODY_TIMEOUT_S
        while True:
            try:
                msg = await asyncio.wait_for(receive(), max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                return None
            if msg["type"] != "http.request":
                return None
            chunk = msg.get("body") or b""
            size += len(chunk)
            if size > MAX_BODY:
                return None
            chunks.append(chunk)
            if not msg.get("more_body"):
                return b"".join(chunks)

    def _csrf_ok(self, scope) -> bool:
        origin = self._header(scope, b"origin")
        if origin is None or origin.strip().lower() != self.s.public_origin:
            return False
        site = self._header(scope, b"sec-fetch-site")
        return site is None or site == "same-origin"

    async def _post(self, scope, receive, token: str):
        ctype = (self._header(scope, b"content-type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return self._page(415, V.FORM_REFUSED)
        body = await self._read_body(scope, receive)
        if body is None:
            return self._page(413, V.TOO_LARGE)
        if not self._csrf_ok(scope):
            return self._page(403, V.FORM_REFUSED)
        try:
            form = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False, max_num_fields=20)
        except (UnicodeDecodeError, ValueError):
            return self._page(400, V.FORM_REFUSED)
        one = {k: v[0] for k, v in form.items() if len(v) == 1}
        if any(len(v) != 1 for v in form.values()):
            return self._page(400, V.FORM_REFUSED)
        issued_raw, sig, page_sha = one.get("issued", ""), one.get("form_sig", ""), one.get("page_sha256", "")
        now = int(self.clock())
        if not (issued_raw.isdigit() and len(issued_raw) <= 12 and re.fullmatch(r"[0-9a-f]{64}", page_sha or "")
                and re.fullmatch(r"[0-9a-f]{64}", sig or "")):
            return self._page(403, V.FORM_REFUSED)
        issued = int(issued_raw)
        if not hmac.compare_digest(sig, self._sign(token, page_sha, issued)) \
                or issued > now + 60 or now - issued > self.s.form_max_age_s:
            return self._page(403, V.FORM_REFUSED)
        view = await self._resolve(token)
        if not isinstance(view, PortalView):
            return view
        shown = self._shown(view)
        fresh = self._page_sha(view, token)
        if not hmac.compare_digest(fresh, page_sha):
            return self._page(409, V.changed_page(token))
        state = V.FormState(*(one.get(k, "").strip()[:FIELD_MAX[k] + 1] for k in ("full_name", "title", "company")))
        errors = {}
        for n, doc in enumerate(shown, start=1):
            if one.get(f"accept_doc_{n}") != "yes":
                label = V.doc_label(view, "msa" if doc.doc_id == "client_msa" else "sow")
                errors[f"accept_doc_{n}"] = f"Tick the box to accept the {label}"
        for k, label in (("full_name", "your full name"), ("title", "your title"), ("company", "the company")):
            v = getattr(state, k)
            if not v:
                errors[k] = f"Enter {label}"
            elif len(v) > FIELD_MAX[k] or _CONTROL.search(v):
                errors[k] = f"Enter {label} in at most {FIELD_MAX[k]} characters, without special characters"
        if "company" not in errors and _norm_company(state.company) != _norm_company(view.display.client_legal_name):
            errors["company"] = f"Enter the company name as it appears in the documents: {view.display.client_legal_name}"
        if errors:
            state.errors = {k: errors[k] for k in [f"accept_doc_{n}" for n in range(1, len(shown) + 1)]
                            + ["full_name", "title", "company"] if k in errors}
            return self._form(view, token, state, status=422)
        page_text = V.acceptance_page(view, token, 0, "", None, display_tz=self.s.display_tz).text.encode("utf-8")
        ua = (self._header(scope, b"user-agent") or "")[:1024]
        sig_block = {"full_name": state.full_name, "title": state.title, "company": state.company}
        rid = "pa-" + _mac(self.s.evidence_key, "request-v1", hashlib.sha256(token.encode()).hexdigest(), page_sha,
                           json.dumps(sig_block, sort_keys=True))[:40]
        req = {"request_id": rid, "token": token,
               "documents": [{"doc_id": d.doc_id, "version": d.version, "doc_sha256": d.doc_sha256,
                              "presented_sha256": hashlib.sha256(d.text.encode("utf-8")).hexdigest(),
                              "affirmative_act": True} for d in shown],
               "presentation": "inline", "page_sha256": hashlib.sha256(page_text).hexdigest(),
               "page_text_b64": base64.b64encode(page_text).decode("ascii"), "signatory": sig_block,
               "ip_hmac_sha256": _mac(self.s.evidence_key, "ip-v1", self._client_ip(scope)),
               "user_agent_hmac_sha256": _mac(self.s.evidence_key, "ua-v1", ua),
               "hmac_key_id": self.s.key_id}
        try:
            out: AcceptOutcome = await asyncio.to_thread(self.legal.accept, req)
        except LegalUnavailable:
            return self._page(503, V.UNAVAILABLE, [(b"retry-after", b"60")])
        if out.kind == "not_found":
            return self._not_found()
        if out.kind != "accepted":
            return self._page(409, V.changed_page(token))
        emailed = self._send_confirmation(view, out, state)
        pg = V.confirmation_page(view, out.acceptances, state.full_name, state.title, self.s.display_tz, emailed)
        return self._page(200, pg)


def _attachments(view: PortalView, accepted) -> tuple:
    by_id = {d.doc_id: d for d in view.documents}
    return tuple(Attachment(f"{a.doc_id}-v{a.version}.md", by_id[a.doc_id].text.encode("utf-8")) for a in accepted)


def create_app(settings: Settings, legal: Optional[LegalClient] = None,
               mailer: Optional[ConfirmationMailer] = None) -> PortalApp:
    legal = legal or LegalClient(settings.legal_url, settings.legal_service_token, settings.legal_caller_token,
                                 timeout=settings.legal_timeout_s)
    return PortalApp(settings, legal, mailer)
