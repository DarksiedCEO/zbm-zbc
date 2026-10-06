"""Proving the mailbox, then acting in a creator session (AEGIS round 4, M1': verify the address first, then take the
application). A mixin of InfluencerService.

Before the click the service cannot tell the creator from a stranger who typed the creator's address, so nothing a
request carries may be bound to the address until the mailbox is proven: any limit on payload-bound confirmations
became a lockout (round 3's three open per address, round 2's five a day).

1. **The public form takes an address only** (``POST /inf/v1/applications``, caller ``hub``). Each canonical address
   has exactly ONE open address-confirmation link; a repeat request reuses it and is never refused for being a repeat.
   The link carries no handle, no attestation, no payload: it only proves the mailbox. It is mailed at most once per
   ``RESEND_HOURS`` counted from SEND time (a queued-and-cancelled mail never delays the next); a stranger flooding
   the form produces at most one harmless email a day, and the creator's own request reuses the same link.
2. **The click opens a creator session** (``POST /inf/v1/confirmations``, the hub relaying the link): short-lived
   (``INF_CREATOR_SESSION_MINUTES``, default 60, on the service clock), bound to the record (or, for a new address, to
   the record the session's application will create). Its token is ``<session id>.<HMAC-SHA-256 under the PII key>``
   — 256 bits, re-derived to check, never stored, never in the log, never on the ledger.
3. **Inside the session** the creator submits the 18+ attestation, their handles and the application details, and a
   tax-reference change. Each applies AT ONCE (the mailbox is proven), each action kind once per session (ADR 0015
   round 4: a token that leaked within its hour does at most one change of each kind, each recorded). No per-address
   cap applies to either. A tax change for a creator whose payee is already VERIFIED still waits for Andre's approval
   of its hash (``POST /inf/v1/confirmations/{id}/approve``).

Carried over: a SUPPRESSED address gets no link until Andre approves mailing it, by hash (``awaiting_andre``, at most
``INF_ANDRE_REVIEW_DAILY_CAP`` a day, the rest in his digest); a blocked record or an unconfigured outreach domain:
``undeliverable``; the mail always goes to the address ON THE RECORD (AEGIS R2-L-a).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from typing import Optional

from clock import iso, parse_iso
from errors import Conflict, Forbidden, Invalid, NotFound, Throttled
from intelligences import i01_intake, i04_suppression, i05_templates
from ledger import derived_id, payload_sha256
from reasons import R

CONFIRM_DAYS = 7
RESEND_HOURS = 24          # at most one link mail SENT per address per day, counted from send time
OPEN = ("pending", "awaiting_andre", "awaiting_andre_digest")
REVIEW = ("awaiting_andre", "awaiting_andre_digest")
SESSION_ACTIONS = ("application", "tax_profile")


def confirmation_sha256(c: dict) -> str:
    """What Andre approves: mailing an address link to a suppressed address, or a tax change for a verified payee (never
    the raw reference: its SHA-256 stands in)."""
    p = c["payload"]
    return payload_sha256({"conf_id": c["conf_id"], "influencer_id": c["influencer_id"], "kind": c["kind"],
                           "email_hash": c["email_hash"],
                           "tax_form": p.get("tax_form"), "tax_ref_sha256": p.get("tax_ref_sha256"),
                           "legal_form": p.get("legal_form"), "country": p.get("country")})


class ConfirmMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_confirmation_requested(self, d, at):
        c = dict(d["confirmation"])
        c.update(created_at=at, decided_at=None,
                 review_day=at[:10] if c["status"] in REVIEW else None)
        self.confirmations[c["conf_id"]] = c
        if d.get("message"):
            self._queue_confirmation_mail(d["message"], d["actor"], at)

    def _a_confirmation_remailed(self, d, at):
        self.confirmations[d["conf_id"]]["message_id"] = d["message"]["message_id"]
        self._queue_confirmation_mail(d["message"], d["actor"], at)

    def _queue_confirmation_mail(self, msg: dict, actor: str, at: str) -> None:
        self._a_message_queued({**msg, "actor": actor}, at)

    def _reviews_today(self) -> int:
        day = self.today()
        return sum(1 for c in self.confirmations.values() if c.get("review_day") == day)

    def _a_confirmation_send_approved(self, d, at):
        c = self.confirmations[d["conf_id"]]
        c.update(status="pending", andre_send=True, expires_at=d["expires_at"], message_id=d["message"]["message_id"])
        self._queue_confirmation_mail(d["message"], d["actor"], at)

    def _close_mail(self, c: dict, at: str) -> None:
        m = self.messages.get(c.get("message_id") or "")
        if m is not None and m["status"] == "queued":
            m.update(status="cancelled", reason="CONFIRMATION_CLOSED", updated_at=at)

    def _a_session_opened(self, d, at):
        c = self.confirmations[d["conf_id"]]
        c.update(status="used", decided_at=at)
        self._close_mail(c, at)
        s = dict(d["session"])
        s.update(opened_at=at, used=[])
        self.sessions[s["session_id"]] = s
        inf = self.influencers.get(s["influencer_id"])
        if inf is not None:
            inf.update(email_confirmed=True, updated_at=at)

    def _use_session(self, d: dict) -> None:
        if d.get("session_id"):
            self.sessions[d["session_id"]]["used"].append(d["action"])

    def _a_session_application(self, d, at):
        if d.get("record"):
            self._a_influencer_created(d["record"], at)
        self._apply_application(self.influencers[d["influencer_id"]], d["payload"], at)
        self._use_session(d)

    def _a_tax_change_applied(self, d, at):
        self._apply_tax(self.influencers[d["influencer_id"]], d["payload"], at)
        self._use_session(d)

    def _a_tax_change_requested(self, d, at):
        c = dict(d["confirmation"])
        c.update(created_at=at, decided_at=None, review_day=None)
        self.confirmations[c["conf_id"]] = c
        self._use_session(d)

    def _a_confirmation_applied(self, d, at):
        c = self.confirmations[d["conf_id"]]
        c.update(status="applied", decided_at=at)
        self._apply_tax(self.influencers[c["influencer_id"]], c["payload"], at)

    def _a_confirmation_rejected(self, d, at):
        c = self.confirmations[d["conf_id"]]
        c.update(status="rejected", decided_at=at)
        self._close_mail(c, at)

    def _a_confirmations_bulk_rejected(self, d, at):
        for cid in d["conf_ids"]:
            self._a_confirmation_rejected({"conf_id": cid}, at)

    # ------------------------------------------------------------------------------------------------ tokens

    def confirmation_token(self, conf_id: str) -> str:
        mac = hmac.new(self.pii_key, f"confirm\x00{conf_id}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{conf_id}.{mac}"

    def session_token(self, session_id: str) -> str:
        """256 bits (the whole HMAC-SHA-256), re-derived to check: never stored, never logged, never on the ledger."""
        mac = hmac.new(self.pii_key, f"creator-session\x00{session_id}".encode(), hashlib.sha256).hexdigest()
        return f"{session_id}.{mac}"

    # ------------------------------------------------------------------------------------------------ rate state

    def _prune_rate_state(self, eh: Optional[str] = None) -> None:
        """AEGIS R3-L5: send times older than 24 hours are dropped (memory only; rebuilt from the log at start)."""
        since = self.now() - timedelta(hours=24)
        for k in ([eh] if eh else list(self.conf_mail_at)):
            t = self.conf_mail_at.get(k)
            if t is not None and parse_iso(t) <= since:
                self.conf_mail_at.pop(k, None)

    def mail_wait(self, eh: str) -> bool:
        """True while a link mail was SENT to this address less than RESEND_HOURS ago (a mail that was queued and
        cancelled never delays the next one)."""
        self._prune_rate_state(eh)
        last = self.conf_mail_at.get(eh)
        return bool(last) and self.now() - parse_iso(last) < timedelta(hours=RESEND_HOURS)

    # ------------------------------------------------------------------------------------------------ the link

    def _confirmation_mail(self, cid: str, iid: Optional[str], eh: str, existing_record: bool) -> dict:
        """The queued link mail: on its own queue and daily cap (AEGIS R2-N1); the send-queue job sends at most one per
        address per RESEND_HOURS, counted from SEND time."""
        queued = sum(1 for m in self.messages.values() if m["status"] == "queued" and m.get("purpose") == "confirmation")
        if queued >= self.settings.confirmation_queue_max:
            raise Throttled(R("QUEUE_FULL"))
        return {"message_id": derived_id("msg", "confirm", cid, self.now().isoformat()), "channel": "email",
                "purpose": "confirmation", "confirmation_id": cid, "influencer_id": iid, "to_hash": eh, "brand": None,
                "from_domain": self.settings.outreach_domain, "existing_record": existing_record}

    def _open_link(self, eh: str) -> Optional[dict]:
        """The one open, unexpired address link of this canonical address, if any."""
        return next((c for c in self.confirmations.values() if c["kind"] == "address" and c["email_hash"] == eh
                     and c["status"] in OPEN and self.now() <= parse_iso(c["expires_at"])), None)

    def _link_answer(self, c: dict) -> dict:
        return {"confirmation_id": c["conf_id"], "confirmation_status": c["status"]}

    def request_link(self, caller: str, body: dict) -> dict:
        """The public apply step: an address (and optionally the brand) only. Never refused for being a repeat: the
        open link is reused, and mailed again only when no mail of it is waiting (the send-queue job then sends it no
        sooner than RESEND_HOURS after the last one SENT). Nothing about identity is taken here."""
        problem = i01_intake.source_problem("inbound_application", caller)
        if problem:
            raise Forbidden(R(problem))
        with self.lock:
            self._gate()
            email, eh = self._email(body["email"])
            rk = self._rk("apply", eh, body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self._link_answer(self.confirmations[prev[1]])
            existing_id = self.email_index.get(eh)
            existing = self.influencers.get(existing_id) if existing_id else None
            if existing is not None and existing.get("blocked"):
                raise Forbidden(R("INFLUENCER_BLOCKED"))
            s = self.settings
            deliverable = bool(s.outreach_domain and s.postal_address)
            c = self._open_link(eh)
            if c is not None:
                m = self.messages.get(c.get("message_id") or "")
                if c["status"] == "pending" and deliverable and (m is None or m["status"] != "queued"):
                    msg = self._confirmation_mail(c["conf_id"], existing_id, eh, existing is not None)
                    self._commit("confirmation_remailed", self._req({"conf_id": c["conf_id"], "message": msg}, caller,
                                                                    rk, body, c["conf_id"]), caller,
                                 evidence=("confirmation_remailed", f"confirmation:{c['conf_id']}",
                                           {"conf_id": c["conf_id"], "message_id": msg["message_id"],
                                            "email_hash": eh}, (caller, rk)))
                return self._link_answer(c)            # otherwise nothing changes: nothing is written
            cid = derived_id("cnf", caller, rk)
            conf = {"conf_id": cid, "influencer_id": existing_id, "kind": "address",
                    "email": existing["email"] if existing else email,   # R2-L-a: the address ON THE RECORD
                    "email_hash": eh, "payload": {}, "expires_at": iso(self.now() + timedelta(days=CONFIRM_DAYS)),
                    "message_id": None, "content_sha256": None, "andre_send": False,
                    "brand": body.get("brand")}
            suppressed = eh in self.suppression or i04_suppression.suppressed(
                self.suppression, i04_suppression.hashes_of(existing or {}))
            msg = None
            if not deliverable:
                conf["status"] = "undeliverable"
            elif suppressed:
                # AEGIS R3-L2: at most INF_ANDRE_REVIEW_DAILY_CAP new review items a day; past it, his daily digest
                conf["status"] = "awaiting_andre" if self._reviews_today() < s.andre_review_daily_cap \
                    else "awaiting_andre_digest"
                conf["content_sha256"] = confirmation_sha256(conf)
            else:
                conf["status"] = "pending"
                msg = self._confirmation_mail(cid, existing_id, eh, existing is not None)
                conf["message_id"] = msg["message_id"]
            ev = ("confirmation_requested", f"confirmation:{cid}", {"conf_id": cid, "influencer_id": existing_id,
                                                                    "kind": "address", "email_hash": eh,
                                                                    "status": conf["status"]}, (caller, rk))
            self._commit("confirmation_requested", self._req({"confirmation": conf, "message": msg}, caller, rk, body,
                                                             cid), caller, evidence=ev)
            return self._link_answer(self.confirmations[cid])

    def render_confirmation(self, msg: dict, conf: dict) -> dict:
        """A fixed service text (no template, nothing generated): the link, what it does, and how to ignore it."""
        s = self.settings
        url = f"https://{msg['from_domain']}/c/{self.confirmation_token(conf['conf_id'])}"
        names = " and ".join(i05_templates.BRAND_DISPLAY.values())
        body = (f"Someone asked to apply as a creator with {names}, or to update a creator profile, using this "
                f"address.\nIf it was you, open this link within {CONFIRM_DAYS} days to continue: {url}\n"
                f"It opens a private session for {s.creator_session_minutes} minutes where you fill in your "
                "application or payout details.\n"
                "If it was not you, ignore this email: nothing changes.\n\n--\n"
                f"{names}\n{s.postal_address}\n")
        return {"from": f"{names} <{s.from_local}@{msg['from_domain']}>",
                "reply_to": f"{s.from_local}@{msg['from_domain']}", "subject": "Confirm your email address",
                "body": body, "headers": {}}

    def _confirmation_send_problem(self, msg: dict) -> tuple[Optional[str], Optional[dict]]:
        c = self.confirmations.get(msg["confirmation_id"])
        if c is None or c["status"] != "pending" or c.get("message_id") != msg["message_id"]:
            return "CONFIRMATION_CLOSED", None
        if self.now() > parse_iso(c["expires_at"]):
            return "CONFIRMATION_EXPIRED", None
        iid = self.email_index.get(c["email_hash"])
        inf = self.influencers.get(iid) if iid else {}
        if inf.get("blocked"):
            return "INFLUENCER_BLOCKED", None
        if not c.get("andre_send") and (msg["to_hash"] in self.suppression or i04_suppression.suppressed(
                self.suppression, i04_suppression.hashes_of(inf))):
            return "SUPPRESSED", None
        if msg["to_hash"] != c["email_hash"] or (c.get("influencer_id") and c["influencer_id"] != iid):
            return "ADDRESS_CHANGED", None
        if msg["from_domain"] != self.settings.outreach_domain or not self.settings.postal_address:
            return "OUTREACH_DOMAIN_CHANGED", None
        return None, self.render_confirmation(msg, c)

    # ------------------------------------------------------------------------------------------------ the click

    def confirmation_view(self, c: dict) -> dict:
        out = {k: v for k, v in c.items() if k not in ("email", "payload")}
        if c["kind"] == "tax_profile":
            out["change"] = {k: c["payload"][k] for k in ("tax_form", "tax_ref_sha256", "legal_form", "country")}
        return out

    def confirmations_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.confirmation_view(c) for c in self.confirmations.values()
                    if status is None or c["status"] == status][:1000]

    def _session_answer(self, sid: str) -> dict:
        s = self.sessions[sid]
        inf = self.influencers.get(s["influencer_id"])
        return {"session_token": self.session_token(sid), "session_id": sid, "influencer_id": s["influencer_id"],
                "expires_at": s["expires_at"], "record_exists": inf is not None,
                "adult_attested": bool(inf and inf.get("adult_attested"))}

    def confirm(self, caller: str, body: dict) -> dict:
        """The link came back: the mailbox is proven and a creator session opens (the link is single use). The answer
        carries the session token; the log keeps only the session id (the token is re-derived)."""
        with self.lock:
            self._gate()
            token = body["token"]
            cid = token.split(".", 1)[0]
            if cid not in self.confirmations or not hmac.compare_digest(self.confirmation_token(cid), token):
                raise NotFound(R("CONFIRMATION_UNKNOWN"))
            rk = self._rk("confirm", cid, body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self._session_answer(prev[1])
            c = self.confirmations[cid]
            if c["kind"] != "address" or c["status"] != "pending":
                raise Conflict(R("CONFIRMATION_USED"))
            if self.now() > parse_iso(c["expires_at"]):
                raise Conflict(R("CONFIRMATION_EXPIRED"))
            existing_id = self.email_index.get(c["email_hash"])
            inf = self.influencers.get(existing_id) if existing_id else None
            if inf is not None and inf.get("blocked"):
                raise Forbidden(R("INFLUENCER_BLOCKED"))
            if c.get("influencer_id") and c["influencer_id"] != existing_id:
                raise Conflict(R("STATE_CHANGED"))
            sid = derived_id("ses", caller, rk)
            iid = existing_id or derived_id("inf", "session", sid)
            session = {"session_id": sid, "conf_id": cid, "influencer_id": iid, "email_hash": c["email_hash"],
                       "email": inf["email"] if inf else c["email"], "brand": c.get("brand"),
                       "expires_at": iso(self.now() + timedelta(minutes=self.settings.creator_session_minutes))}
            ev = [("email_confirmed", f"confirmation:{cid}", {"conf_id": cid, "email_hash": c["email_hash"],
                                                              "influencer_id": existing_id}, (caller, rk)),
                  ("creator_session_opened", f"session:{sid}", {"session_id": sid, "conf_id": cid,
                                                                "influencer_id": iid}, (caller, rk))]
            self._commit("session_opened", self._req({"conf_id": cid, "session": session}, caller, rk, body, sid),
                         caller, evidence=ev)
            return self._session_answer(sid)

    # ------------------------------------------------------------------------------------------------ sessions

    def _session(self, token: str) -> dict:
        sid = token.split(".", 1)[0]
        s = self.sessions.get(sid)
        if s is None or not hmac.compare_digest(self.session_token(sid), token):
            raise Forbidden(R("SESSION_INVALID"))
        return s

    def _session_usable(self, s: dict, action: str, influencer_id: Optional[str] = None) -> None:
        if self.now() > parse_iso(s["expires_at"]):
            raise Forbidden(R("SESSION_EXPIRED"))
        if influencer_id is not None and influencer_id != s["influencer_id"]:
            raise Forbidden(R("SESSION_RECORD_MISMATCH"))
        if action in s["used"]:
            raise Conflict(R("SESSION_ACTION_USED"))

    def submit_application(self, caller: str, body: dict) -> dict:
        """Inside a session: the 18+ attestation, the handles (those no other record holds) and the details apply at
        once. A declared minor is refused; nothing about them is kept, and a record we already hold for that mailbox
        is FROZEN until Andre's minor review (a flag only: no age, no date of birth)."""
        problem = i01_intake.source_problem("inbound_application", caller)
        if problem:
            raise Forbidden(R(problem))
        with self.lock:
            self._gate()
            s = self._session(body["session_token"])
            sid = s["session_id"]
            rk = self._rk("session_application", sid, body)
            prev = self._idem(caller, rk, body)
            if prev:
                if (prev[1] or {}).get("refused"):
                    raise Invalid(R(prev[1]["refused"]))
                return self.influencer_view(self.influencers[prev[1]])
            self._session_usable(s, "application")
            iid = s["influencer_id"]
            inf = self.influencers.get(iid)
            if inf is not None and inf.get("blocked"):
                raise Forbidden(R("INFLUENCER_BLOCKED"))
            age = i01_intake.attestation_problem(body.get("adult_18_plus"))
            if age == "MINOR_REFUSED":
                if inf is not None:
                    self._commit("influencer_blocked", self._req(
                        {"influencer_id": iid, "reason": "MINOR_DECLARED", "session_id": sid, "action": "application"},
                        caller, rk, body, {"refused": "MINOR_REFUSED"}), caller,
                        evidence=("influencer_blocked", f"influencer:{iid}",
                                  {"influencer_id": iid, "reason": "MINOR_DECLARED"}, (caller, rk)))
                raise Invalid(R("MINOR_REFUSED"))
            if age:
                raise Invalid(R(age))
            handles = self._handles(body.get("handles") or [])
            payload = {"attestation": {"text_version": body["attestation_text_version"],
                                       "text_sha256": body["attestation_text_sha256"], "source": "creator_form"},
                       "add_handles": handles}
            record = None
            ev = []
            if inf is None:
                record = {"influencer_id": iid, **self._record("inbound_application", body, s["email"],
                                                               s["email_hash"], [])}
                ev += self._created_events(iid, "inbound_application", caller, rk)
            ev.append(("age_attestation_recorded", f"influencer:{iid}",
                       {"influencer_id": iid, "adult_18_plus": True, **payload["attestation"]}, (caller, rk)))
            ev.append(("creator_session_used", f"session:{sid}", {"session_id": sid, "influencer_id": iid,
                                                                  "action": "application"}, (caller, rk)))
            self._commit("session_application", self._req(
                {"influencer_id": iid, "record": record, "payload": payload, "session_id": sid,
                 "action": "application"}, caller, rk, body, iid), caller, evidence=ev)
            return self.influencer_view(self.influencers[iid])

    def _tax_answer(self, iid: str, conf_id: Optional[str]) -> dict:
        if conf_id:
            return {**self.confirmation_view(self.confirmations[conf_id]), "status": self.confirmations[conf_id]["status"]}
        inf = self.influencers[iid]
        return {"status": "applied", "conf_id": None, "content_sha256": None, "influencer_id": iid,
                "change": {k: inf["tax"][k] for k in ("tax_form", "tax_ref_sha256", "legal_form", "country")}}

    def tax_in_session(self, caller: str, body: dict, payload: dict) -> dict:
        """Inside a session, for the session's own record, once per session: applied at once, or — for a creator
        whose payee is already verified — waiting for Andre's approval of its hash. No per-address cap applies."""
        s = self._session(body["session_token"])
        sid = s["session_id"]
        rk = self._rk("tax_profile", sid, body)
        prev = self._idem(caller, rk, body)
        if prev:
            return self._tax_answer(prev[1]["influencer_id"], prev[1]["conf_id"])
        self._session_usable(s, "tax_profile", body["influencer_id"])
        inf = self._get(self.influencers, body["influencer_id"], "INFLUENCER_NOT_FOUND")
        problem = i01_intake.contractable(inf)
        if problem:
            raise Forbidden(R(problem))
        iid = inf["influencer_id"]
        used = ("creator_session_used", f"session:{sid}", {"session_id": sid, "influencer_id": iid,
                                                           "action": "tax_profile"}, (caller, rk))
        if inf["payee"]["status"] == "verified":
            cid = derived_id("cnf", caller, rk)
            conf = {"conf_id": cid, "influencer_id": iid, "kind": "tax_profile", "email": inf["email"],
                    "email_hash": inf["email_hash"], "payload": payload, "expires_at": None, "message_id": None,
                    "andre_send": False, "status": "pending_andre"}
            conf["content_sha256"] = confirmation_sha256(conf)
            self._commit("tax_change_requested", self._req(
                {"confirmation": conf, "session_id": sid, "action": "tax_profile"}, caller, rk, body,
                {"influencer_id": iid, "conf_id": cid}), caller,
                evidence=[used, ("tax_change_pending_andre", f"influencer:{iid}",
                                 {"influencer_id": iid, "conf_id": cid, "content_sha256": conf["content_sha256"]},
                                 (caller, rk))])
            return self._tax_answer(iid, cid)
        self._commit("tax_change_applied", self._req(
            {"influencer_id": iid, "payload": payload, "session_id": sid, "action": "tax_profile"}, caller, rk, body,
            {"influencer_id": iid, "conf_id": None}), caller, evidence=[used, self._tax_evidence(iid, payload, caller,
                                                                                               rk)])
        return self._tax_answer(iid, None)

    # ------------------------------------------------------------------------------------------------ Andre

    def bulk_reject(self, body: dict) -> dict:
        """Andre clears his review queue in one action (AEGIS R3-L2), bound to the SHA-256 of the exact id list he
        saw (``"\\n".join(conf_ids)``, in his order): a different list is refused, and nothing changes unless every
        id is still waiting for him."""
        with self.lock:
            self._gate()
            rk = self._rk("confirmation_bulk_reject", body["ids_sha256"], body)
            if self._idem("andre", rk, body):
                return {"rejected": list(body["conf_ids"])}
            ids = list(body["conf_ids"])
            if hashlib.sha256("\n".join(ids).encode()).hexdigest() != body["ids_sha256"] or len(set(ids)) != len(ids):
                raise Conflict(R("ID_LIST_MISMATCH"))
            for cid in ids:
                c = self._get(self.confirmations, cid, "CONFIRMATION_UNKNOWN")
                if c["status"] not in ("pending_andre",) + REVIEW:
                    raise Conflict(R("CONFIRMATION_NOT_PENDING"))
            self._commit("confirmations_bulk_rejected", self._req({"conf_ids": ids}, "andre", rk, body, None),
                         "andre", evidence=("confirmations_bulk_rejected", f"andre:{body['ids_sha256'][:40]}",
                                            {"conf_ids": ids, "ids_sha256": body["ids_sha256"]}, ("andre", rk)))
            return {"rejected": ids}

    def decide_confirmation(self, conf_id: str, body: dict, approve: bool) -> dict:
        """Andre on a tax-reference change for a creator whose payee was already verified, or on mailing an address
        link to a SUPPRESSED address (AEGIS R2-L-d: only that one mail; the suppression stays in place for outreach)."""
        with self.lock:
            self._gate()
            rk = self._rk("confirmation_approve" if approve else "confirmation_reject", conf_id, body)
            if self._idem("andre", rk, body):
                return self.confirmation_view(self.confirmations[conf_id])
            c = self._get(self.confirmations, conf_id, "CONFIRMATION_UNKNOWN")
            if c["status"] not in ("pending_andre",) + REVIEW:
                raise Conflict(R("CONFIRMATION_NOT_PENDING"))
            iid = c["influencer_id"]
            subject = f"influencer:{iid}" if iid else f"confirmation:{conf_id}"
            if not approve:
                self._commit("confirmation_rejected", self._req({"conf_id": conf_id}, "andre", rk, body, conf_id),
                             "andre", evidence=("tax_change_rejected" if c["kind"] == "tax_profile"
                                                else "confirmation_rejected", subject, {"conf_id": conf_id},
                                                ("andre", rk)))
                return self.confirmation_view(c)
            if body["content_sha256"] != c["content_sha256"] or confirmation_sha256(c) != c["content_sha256"]:
                raise Conflict(R("CONTENT_HASH_MISMATCH"))
            current = self.email_index.get(c["email_hash"])
            if (self.influencers.get(current) or {}).get("blocked"):
                raise Forbidden(R("INFLUENCER_BLOCKED"))
            if c["status"] in REVIEW:
                expires = iso(self.now() + timedelta(days=CONFIRM_DAYS))
                msg = self._confirmation_mail(conf_id, iid, c["email_hash"], True)
                self._commit("confirmation_send_approved", self._req(
                    {"conf_id": conf_id, "message": msg, "expires_at": expires}, "andre", rk, body, conf_id), "andre",
                    evidence=("confirmation_send_approved", subject,
                              {"conf_id": conf_id, "content_sha256": c["content_sha256"]}, ("andre", rk)))
                return self.confirmation_view(c)
            self._commit("confirmation_applied", self._req({"conf_id": conf_id}, "andre", rk, body, conf_id), "andre",
                         evidence=[("tax_change_approved", subject,
                                    {"conf_id": conf_id, "content_sha256": c["content_sha256"]}, ("andre", rk)),
                                   self._tax_evidence(iid, c["payload"], "andre", rk)])
            return self.confirmation_view(c)
