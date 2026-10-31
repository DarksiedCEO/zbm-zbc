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
import ipaddress
import json
import logging
import re
import time
import unicodedata
from typing import Callable, Optional
from urllib.parse import parse_qs

import link_tag as LT
import render as V
from config import Settings
from legal_client import AcceptOutcome, LegalClient, LegalUnavailable, PortalView
from mail import Attachment, ConfirmationMailer, ConfirmationMessage, NotWiredMailer
from ratelimit import Limiter

log = logging.getLogger("client_portal")

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
    """AEGIS r2 N-L1: legal-py's ``clickwrap_page.norm_company`` exactly (NFKC, case, spaces and punctuation)."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", s).casefold() if ch.isalnum())


def rate_key(ip: str) -> str:
    """AEGIS r2 N-M1: an IPv6 client is counted per /64 (one end site), an IPv4 client per address."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


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
        # AEGIS M-3: one ceiling on EVERY call to Legal (resolve, accept, receipt), charged before the call
        self.legal_calls = Limiter(settings.legal_per_minute, 60, clock=monotonic)

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
        """The client's address, used only as an HMAC (rate-limit key, evidence). AEGIS M-3: Railway's edge sets
        ``X-Real-IP`` (PORTAL_CLIENT_IP_HEADER; a Railway moderator states the edge overwrites it and
        X-Forwarded-For, which is not a documented contract: runbook step 8 checks it). Without a valid one, the
        X-Forwarded-For entry PORTAL_TRUSTED_PROXY_HOPS from the END (never the left-most, which a client can
        write), else the socket peer. If a client could still choose the value, the per-address limit is bypassable;
        the global ceilings on Legal calls and not-found answers still bound the load (ADR 0028)."""
        name = self.s.client_ip_header
        if name:
            raw = (self._header(scope, name.encode("latin-1")) or "").strip()
            try:
                return str(ipaddress.ip_address(raw))
            except ValueError:
                pass
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
        if not LT.valid(token, self.s.link_tag_key):
            # AEGIS r2 N-M1: a token Legal did not mint (junk, forged, mistyped) is refused here, before any budget is
            # charged or Legal is asked: the same 404 page, at the cost of one HMAC
            return (name, *self._page(404, V.NOT_FOUND))
        ok_ip, retry_ip = self.by_ip.hit(_mac(self.s.evidence_key, "rl-ip", rate_key(self._client_ip(scope))))
        if not ok_ip:
            return (name, *self._page(429, V.RATE_LIMITED, [(b"retry-after", str(retry_ip).encode())]))
        if method == "POST":
            return (name, *await self._post(scope, receive, token))
        view = await self._resolve(token)
        if not isinstance(view, PortalView):
            return (name, *view)
        limited = self._link_limited(token)
        if limited:
            return (name, *limited)
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

    async def _receipt_or(self, token: str, rid: str, form_sha: str, state: "V.FormState", otherwise):
        limited = self._legal_limited()
        if limited:
            return limited
        try:
            rec = await asyncio.to_thread(self.legal.receipt, token, rid, form_sha)
        except LegalUnavailable:
            return self._page(503, V.UNAVAILABLE, [(b"retry-after", b"60")])
        if rec is None:
            return otherwise
        limited = self._link_limited(token)
        if limited:
            return limited
        pg = V.confirmation_page(rec, rec.acceptances, state.full_name, state.title, self.s.display_tz, False)
        return self._page(200, pg)

    def _not_found(self):
        ok, retry = self.not_found.hit("global")
        if not ok:
            return self._page(429, V.RATE_LIMITED, [(b"retry-after", str(retry).encode())])
        return self._page(404, V.NOT_FOUND)

    def _legal_limited(self):
        """AEGIS M-3: charged BEFORE every Legal call; at the ceiling, Legal is not called."""
        ok, retry = self.legal_calls.hit("global")
        return None if ok else self._page(429, V.RATE_LIMITED, [(b"retry-after", str(retry).encode())])

    def _link_limited(self, token: str):
        """Per-link limit, charged only for a link Legal confirmed (a view or a receipt): a junk token never enters
        the table, so junk cannot evict a real link's counter (AEGIS M-3)."""
        ok, retry = self.by_token.hit(_mac(self.s.evidence_key, "rl-token", token))
        return None if ok else self._page(429, V.RATE_LIMITED, [(b"retry-after", str(retry).encode())])

    async def _resolve(self, token: str):
        """The link's view, or the answer to send (uniform 404, 429, 503). Pages the portal does not serve (an
        MSA-only link, or a SOW-only link for a client with no counted MSA acceptance: Legal renders no blocks) are the
        same 404."""
        if not LT.valid(token, self.s.link_tag_key):
            return self._page(404, V.NOT_FOUND)
        limited = self._legal_limited()
        if limited:
            return limited
        try:
            view = await asyncio.to_thread(self.legal.resolve, token)
        except LegalUnavailable:
            return self._page(503, V.UNAVAILABLE, [(b"retry-after", b"60")])
        if view is None or not self._servable(view):
            return self._not_found()
        return view

    @staticmethod
    def _servable(view: PortalView) -> bool:
        if not view.blocks or not view.page_sha256:
            return False
        if view.page == "first_engagement":
            return bool(view.doc("msa") and view.doc("sow"))
        if view.page == "new_sow":
            return bool(view.doc("sow") and not view.doc("msa") and view.msa_accepted_version)
        return False

    @staticmethod
    def _shown(view: PortalView) -> list:
        return [x for x in (view.doc("msa"), view.doc("sow")) if x is not None]

    def _sign(self, token: str, page_sha: str, issued: int) -> str:
        return _mac(self.s.evidence_key, "form-v1", hashlib.sha256(token.encode()).hexdigest(), page_sha, str(issued))

    def _page_sha(self, view: PortalView, token: str) -> str:
        text = V.acceptance_page(view, token, 0, "", None, display_tz=self.s.display_tz).text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest != view.page_sha256:      # the layout added or dropped text: never show or record such a page
            raise LegalUnavailable("the laid-out page text differs from Legal's page")
        return digest

    def _form(self, view: PortalView, token: str, state: Optional[V.FormState] = None, status: int = 200):
        try:
            page_sha = self._page_sha(view, token)
        except LegalUnavailable:
            return self._page(503, V.UNAVAILABLE, [(b"retry-after", b"60")])
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
        form_sha = hashlib.sha256(sig.encode("ascii")).hexdigest()     # AEGIS r2 N-L4: binds the receipt to THIS form
        state = V.FormState(*(_typed(one.get(k, ""))[:FIELD_MAX[k] + 1] for k in ("full_name", "title", "company")))
        sig_block = {"full_name": state.full_name, "title": state.title, "company": state.company}
        rid = "pa-" + _mac(self.s.evidence_key, "request-v1", hashlib.sha256(token.encode()).hexdigest(), page_sha,
                           json.dumps(sig_block, sort_keys=True))[:40]
        view = await self._resolve(token)
        if not isinstance(view, PortalView):
            if view[0] == 404:
                # AEGIS M-2: the link is used. If THIS submission used it (a lost response, a refresh), Legal's
                # receipt answers and the client sees the confirmation again; anything else stays the 404
                return await self._receipt_or(token, rid, form_sha, state, view)
            return view
        limited = self._link_limited(token)
        if limited:
            return limited
        shown = self._shown(view)
        try:
            fresh = self._page_sha(view, token)
        except LegalUnavailable:
            return self._page(503, V.UNAVAILABLE, [(b"retry-after", b"60")])
        if not hmac.compare_digest(fresh, page_sha):
            return self._page(409, V.changed_page(token))
        errors = {}
        for n, doc in enumerate(shown, start=1):
            if one.get(f"accept_doc_{n}") != "yes":
                label = V.doc_label(view, "msa" if doc.doc_id == "client_msa" else "sow")
                errors[f"accept_doc_{n}"] = f"Tick the box to accept the {label}"
        for k, label in (("full_name", "your full name"), ("title", "your title"), ("company", "the company")):
            v = getattr(state, k)
            if not v:
                errors[k] = f"Enter {label}"
            elif len(v) > FIELD_MAX[k] or _CONTROL.search(v) or _invisible(v):
                errors[k] = f"Enter {label} in at most {FIELD_MAX[k]} characters, without special characters"
        if "company" not in errors and _norm_company(state.company) != _norm_company(view.display.client_legal_name):
            errors["company"] = f"Enter the company name as it appears in the documents: {view.display.client_legal_name}"
        if errors:
            state.errors = {k: errors[k] for k in [f"accept_doc_{n}" for n in range(1, len(shown) + 1)]
                            + ["full_name", "title", "company"] if k in errors}
            return self._form(view, token, state, status=422)
        page_text = view.page_text.encode("utf-8")
        ua = (self._header(scope, b"user-agent") or "")[:1024]
        limited = self._legal_limited()
        if limited:
            return limited
        req = {"request_id": rid, "token": token,
               "documents": [{"doc_id": d.doc_id, "version": d.version, "doc_sha256": d.doc_sha256,
                              "presented_sha256": hashlib.sha256(d.text.encode("utf-8")).hexdigest(),
                              "affirmative_act": True} for d in shown],
               "presentation": "inline", "page_sha256": hashlib.sha256(page_text).hexdigest(),
               "page_text_b64": base64.b64encode(page_text).decode("ascii"), "signatory": sig_block,
               "ip_hmac_sha256": _mac(self.s.evidence_key, "ip-v1", self._client_ip(scope)),
               "user_agent_hmac_sha256": _mac(self.s.evidence_key, "ua-v1", ua),
               "hmac_key_id": self.s.key_id, "form_sha256": form_sha}
        try:
            out: AcceptOutcome = await asyncio.to_thread(self.legal.accept, req)
        except LegalUnavailable:
            return self._page(503, V.UNAVAILABLE, [(b"retry-after", b"60")])
        if out.kind == "not_found":
            return await self._receipt_or(token, rid, form_sha, state, self._not_found())
        if out.kind != "accepted":
            return self._page(409, V.changed_page(token))
        emailed = self._send_confirmation(view, out, state)
        pg = V.confirmation_page(view, out.acceptances, state.full_name, state.title, self.s.display_tz, emailed)
        return self._page(200, pg)


_INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Co", "Cn"})


def _typed(v: str) -> str:
    """A typed field as recorded: NFKC-normalized, surrounding spaces removed."""
    return unicodedata.normalize("NFKC", v).strip()


# AEGIS r2 N-L3: legal-py's DEFAULT_IGNORABLE (clickwrap_page.py), the same ranges (a test checks the two agree)
_IGNORABLE = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
              (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
              (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
              (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF), (0x2800, 0x2800))


def _invisible(v: str) -> bool:
    """AEGIS L-1 / r2 N-L3: bidi controls, zero-width, default-ignorable, blank fillers, other invisible characters."""
    return any(unicodedata.category(ch) in _INVISIBLE or any(lo <= ord(ch) <= hi for lo, hi in _IGNORABLE)
               for ch in v)


def _attachments(view: PortalView, accepted) -> tuple:
    by_id = {d.doc_id: d for d in view.documents}
    return tuple(Attachment(f"{a.doc_id}-v{a.version}.md", by_id[a.doc_id].text.encode("utf-8")) for a in accepted)


def create_app(settings: Settings, legal: Optional[LegalClient] = None,
               mailer: Optional[ConfirmationMailer] = None) -> PortalApp:
    legal = legal or LegalClient(settings.legal_url, settings.legal_service_token, settings.legal_caller_token,
                                 timeout=settings.legal_timeout_s)
    return PortalApp(settings, legal, mailer)
