"""Email confirmation (AEGIS round 1, M1/M2): a change that binds an identity to a record — the 18+ attestation and
the handles an application names, and every tax-reference change — takes effect only after the creator proves they
read the address on the record: a one-time token is mailed there (through the email port, as a fixed service text, no
template) and must come back through the creator portal (``POST /inf/v1/confirmations``, caller ``hub``). A mixin of
InfluencerService.

- The token is ``<confirmation id>.<HMAC under the PII key>``: never stored, re-derived to send and to check; single use;
  valid ``CONFIRM_DAYS`` days.
- A confirmation mail is never sent to a suppressed address or a blocked record; a reply hold does not stop it (it
  answers the creator's own request; it is not outreach).
- A tax-reference change for a creator whose payee is already VERIFIED does not take effect when confirmed: it waits for
  Andre's approval of its hash (``POST /inf/v1/confirmations/{id}/approve``).
- With no outreach domain configured the confirmation is recorded but cannot be mailed (``undeliverable``): nothing
  takes effect.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from typing import Optional

from clock import iso, parse_iso
from errors import Conflict, Forbidden, NotFound, Throttled
from intelligences import i04_suppression, i05_templates
from ledger import derived_id, payload_sha256
from reasons import R

CONFIRM_DAYS = 7
SUBJECTS = {"application": "Confirm your creator application",
            "tax_profile": "Confirm the change to your payout tax details"}


RESEND_HOURS = 24          # AEGIS R2-N1: at most one confirmation mail per address per day
APPLICATIONS_PER_DAY = 5   # AEGIS R2-N1: applications per canonical address per 24 hours (429 beyond)
OPEN = ("pending", "awaiting_andre")


def confirmation_sha256(c: dict) -> str:
    """What Andre approves: a tax change (never the raw reference: its SHA-256 stands in) or, for a suppressed
    creator's application, the exact confirmation to be mailed (the handles by their keyed hashes)."""
    p = c["payload"]
    return payload_sha256({"conf_id": c["conf_id"], "influencer_id": c["influencer_id"], "kind": c["kind"],
                           "email_hash": c["email_hash"],
                           "tax_form": p.get("tax_form"), "tax_ref_sha256": p.get("tax_ref_sha256"),
                           "legal_form": p.get("legal_form"), "country": p.get("country"),
                           "attestation": p.get("attestation"),
                           "handles": [h["handle_hash"] for h in p.get("add_handles", ())]})


class ConfirmMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_confirmation_requested(self, d, at):
        if d.get("record"):
            self._a_influencer_created(d["record"], at)
        for sid in d.get("superseded") or ():
            old = self.confirmations[sid]
            old.update(status="superseded", decided_at=at)
            self._close_mail(old, at)
        if d.get("confirmation"):
            c = dict(d["confirmation"])
            c.update(created_at=at, decided_at=None)
            self.confirmations[c["conf_id"]] = c
        if d.get("message"):
            self._queue_confirmation_mail(d["message"], d["actor"], at)
        if d.get("application_hash"):
            self.app_times.setdefault(d["application_hash"], []).append(at)

    def _queue_confirmation_mail(self, msg: dict, actor: str, at: str) -> None:
        self._a_message_queued({**msg, "actor": actor}, at)
        self.messages[msg["message_id"]]["not_before"] = msg.get("not_before")
        self.conf_mail_at[msg["to_hash"]] = at

    def _a_confirmation_send_approved(self, d, at):
        c = self.confirmations[d["conf_id"]]
        c.update(status="pending", andre_send=True, expires_at=d["expires_at"], message_id=d["message"]["message_id"])
        self._queue_confirmation_mail(d["message"], d["actor"], at)

    def _close_mail(self, c: dict, at: str) -> None:
        m = self.messages.get(c.get("message_id") or "")
        if m is not None and m["status"] == "queued":
            m.update(status="cancelled", reason="CONFIRMATION_CLOSED", updated_at=at)

    def _a_confirmation_applied(self, d, at):
        c = self.confirmations[d["conf_id"]]
        c.update(status="applied", decided_at=at)
        self._close_mail(c, at)
        inf = self.influencers[c["influencer_id"]]
        if c["kind"] == "application":
            self._apply_application(inf, c["payload"], at)
        else:
            inf["email_confirmed"] = True
            self._apply_tax(inf, c["payload"], at)

    def _a_confirmation_escalated(self, d, at):
        c = self.confirmations[d["conf_id"]]
        c.update(status="pending_andre", content_sha256=d["content_sha256"])
        self._close_mail(c, at)

    def _a_confirmation_rejected(self, d, at):
        c = self.confirmations[d["conf_id"]]
        c.update(status="rejected", decided_at=at)
        self._close_mail(c, at)

    # ------------------------------------------------------------------------------------------------ requesting

    def confirmation_token(self, conf_id: str) -> str:
        mac = hmac.new(self.pii_key, f"confirm\x00{conf_id}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{conf_id}.{mac}"

    def _open_confirmation(self, iid: str, kind: str) -> Optional[dict]:
        for c in self.confirmations.values():
            if c["influencer_id"] == iid and c["kind"] == kind and c["status"] in OPEN \
                    and self.now() <= parse_iso(c["expires_at"]):
                return c
        return None

    def _application_rate_problem(self, eh: str) -> None:
        since = self.now() - timedelta(hours=24)
        if sum(1 for t in self.app_times.get(eh, ()) if parse_iso(t) > since) >= APPLICATIONS_PER_DAY:
            raise Throttled(R("APPLICATION_RATE_LIMITED"))

    def _confirmation_mail(self, cid: str, iid: str, eh: str, existing_record: bool) -> dict:
        """The queued confirmation mail: on its own queue and daily cap (AEGIS R2-N1), at most one per address per
        RESEND_HOURS (a later one waits: ``not_before``)."""
        queued = sum(1 for m in self.messages.values() if m["status"] == "queued" and m.get("purpose") == "confirmation")
        if queued >= self.settings.confirmation_queue_max:
            raise Throttled(R("QUEUE_FULL"))
        last = self.conf_mail_at.get(eh)
        not_before = None
        if last and self.now() - parse_iso(last) < timedelta(hours=RESEND_HOURS):
            not_before = iso(parse_iso(last) + timedelta(hours=RESEND_HOURS))
        return {"message_id": derived_id("msg", "confirm", cid, self.now().isoformat()), "channel": "email",
                "purpose": "confirmation", "confirmation_id": cid, "influencer_id": iid, "to_hash": eh, "brand": None,
                "from_domain": self.settings.outreach_domain, "not_before": not_before,
                "existing_record": existing_record}

    def _request_confirmation(self, caller: str, rk: str, body: dict, iid: str, kind: str, email: str, eh: str,
                              payload: dict, record: Optional[dict] = None, ev: Optional[list] = None,
                              application_hash: Optional[str] = None) -> dict:
        """One open confirmation per record and kind (AEGIS R2-N1): the same request again reuses it (no new mail);
        a different one supersedes it (the old token dies) and is mailed no sooner than RESEND_HOURS after the last
        mail to that address. The mail goes to the address ON THE RECORD (AEGIS R2-L-a). A suppressed address gets no
        mail until Andre approves THIS confirmation by its hash (AEGIS R2-L-d); a blocked record or an unconfigured
        outreach domain: ``undeliverable``. Commits one line; returns the confirmation."""
        ev = list(ev or [])
        open_c = self._open_confirmation(iid, kind) if record is None else None
        if open_c is not None and open_c["payload"] == payload:
            self._commit("confirmation_requested", self._req({"reused": open_c["conf_id"],
                                                              "application_hash": application_hash}, caller, rk,
                                                             body, {"influencer_id": iid,
                                                                    "confirmation_id": open_c["conf_id"]}),
                         caller, evidence=ev or None)
            return open_c
        cid = derived_id("cnf", caller, rk)
        conf = {"conf_id": cid, "influencer_id": iid, "kind": kind, "email": email, "email_hash": eh,
                "payload": payload, "expires_at": iso(self.now() + timedelta(days=CONFIRM_DAYS)),
                "message_id": None, "content_sha256": None, "andre_send": False}
        s = self.settings
        inf = self.influencers.get(iid) or {}
        suppressed = eh in self.suppression or i04_suppression.suppressed(self.suppression,
                                                                          i04_suppression.hashes_of(inf))
        msg = None
        if not (s.outreach_domain and s.postal_address) or inf.get("blocked"):
            conf["status"] = "undeliverable"
        elif suppressed:
            conf["status"] = "awaiting_andre"
            conf["content_sha256"] = confirmation_sha256(conf)
        else:
            conf["status"] = "pending"
            msg = self._confirmation_mail(cid, iid, eh, record is None)
            conf["message_id"] = msg["message_id"]
        ev.append(("confirmation_requested", f"influencer:{iid}", {"conf_id": cid, "influencer_id": iid,
                                                                   "kind": kind, "email_hash": eh,
                                                                   "status": conf["status"]}, (caller, rk)))
        data = {"influencer_id": iid, "record": record, "confirmation": conf, "message": msg,
                "superseded": [open_c["conf_id"]] if open_c else [], "application_hash": application_hash}
        self._commit("confirmation_requested", self._req(data, caller, rk, body,
                                                         {"influencer_id": iid, "confirmation_id": cid}),
                     caller, evidence=ev)
        return self.confirmations[cid]

    def render_confirmation(self, msg: dict, conf: dict) -> dict:
        """A fixed service text (no template, nothing generated): what was asked — for an application, the handles
        it would attach — the link, and how to ignore it."""
        s = self.settings
        url = f"https://{msg['from_domain']}/c/{self.confirmation_token(conf['conf_id'])}"
        names = " and ".join(i05_templates.BRAND_DISPLAY.values())
        if conf["kind"] == "application":
            what = "apply as a creator with"
            hs = conf["payload"].get("add_handles") or []
            detail = ("It would add these accounts to your creator profile: " +
                      ", ".join(f"{h['platform']} @{h['handle']}" for h in hs) + "\n") if hs else ""
        else:
            what, detail = "change the payout tax details for", ""
        body = (f"Someone asked to {what} {names} using this address.\n{detail}"
                f"If it was you and this is right, confirm here within {CONFIRM_DAYS} days: {url}\n"
                "If it was not you, or anything above is wrong, ignore this email: nothing changes.\n\n--\n"
                f"{names}\n{s.postal_address}\n")
        return {"from": f"{names} <{s.from_local}@{msg['from_domain']}>",
                "reply_to": f"{s.from_local}@{msg['from_domain']}", "subject": SUBJECTS[conf["kind"]], "body": body,
                "headers": {}}

    def _confirmation_send_problem(self, msg: dict) -> tuple[Optional[str], Optional[dict]]:
        c = self.confirmations.get(msg["confirmation_id"])
        if c is None or c["status"] != "pending" or c.get("message_id") != msg["message_id"]:
            return "CONFIRMATION_CLOSED", None
        if self.now() > parse_iso(c["expires_at"]):
            return "CONFIRMATION_EXPIRED", None
        inf = self.influencers.get(c["influencer_id"]) or {}
        if inf.get("blocked"):
            return "INFLUENCER_BLOCKED", None
        if not c.get("andre_send") and (msg["to_hash"] in self.suppression or i04_suppression.suppressed(
                self.suppression, i04_suppression.hashes_of(inf))):
            return "SUPPRESSED", None
        if inf.get("email_hash") != c["email_hash"] or msg["to_hash"] != c["email_hash"]:
            return "ADDRESS_CHANGED", None
        if msg["from_domain"] != self.settings.outreach_domain or not self.settings.postal_address:
            return "OUTREACH_DOMAIN_CHANGED", None
        return None, self.render_confirmation(msg, c)

    # ------------------------------------------------------------------------------------------------ confirming

    def confirmation_view(self, c: dict) -> dict:
        out = {k: v for k, v in c.items() if k not in ("email", "payload")}
        if c["kind"] == "tax_profile":
            out["change"] = {k: c["payload"][k] for k in ("tax_form", "tax_ref_sha256", "legal_form", "country")}
        return out

    def confirmations_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.confirmation_view(c) for c in self.confirmations.values()
                    if status is None or c["status"] == status][:1000]

    def confirm(self, caller: str, body: dict) -> dict:
        """The token came back: the change takes effect (or, for a tax change to a verified payee, waits for Andre)."""
        with self.lock:
            self._gate()
            token = body["token"]
            cid = token.split(".", 1)[0]
            if cid not in self.confirmations or not hmac.compare_digest(self.confirmation_token(cid), token):
                raise NotFound(R("CONFIRMATION_UNKNOWN"))
            rk = self._rk("confirm", cid, body)
            if self._idem(caller, rk, body):
                return self.confirmation_view(self.confirmations[cid])
            c = self.confirmations[cid]
            if c["status"] != "pending":
                raise Conflict(R("CONFIRMATION_USED"))
            if self.now() > parse_iso(c["expires_at"]):
                raise Conflict(R("CONFIRMATION_EXPIRED"))
            inf = self.influencers[c["influencer_id"]]
            if inf.get("blocked"):
                raise Forbidden(R("INFLUENCER_BLOCKED"))
            if inf.get("email_hash") != c["email_hash"]:
                raise Conflict(R("STATE_CHANGED"))
            iid = inf["influencer_id"]
            base = ("email_confirmed", f"influencer:{iid}", {"influencer_id": iid, "conf_id": cid, "kind": c["kind"]},
                    (caller, rk))
            if c["kind"] == "tax_profile" and inf["payee"]["status"] == "verified":
                sha = confirmation_sha256(c)
                self._commit("confirmation_escalated", self._req({"conf_id": cid, "content_sha256": sha}, caller, rk,
                                                                 body, cid), caller,
                             evidence=[base, ("tax_change_pending_andre", f"influencer:{iid}",
                                              {"influencer_id": iid, "conf_id": cid, "content_sha256": sha},
                                              (caller, rk))])
                return self.confirmation_view(c)
            ev = [base]
            if c["kind"] == "application":
                ev.append(("age_attestation_recorded", f"influencer:{iid}",
                           {"influencer_id": iid, "adult_18_plus": True, **c["payload"]["attestation"]},
                           (caller, rk)))
            else:
                ev.append(self._tax_evidence(iid, c["payload"], caller, rk))
            self._commit("confirmation_applied", self._req({"conf_id": cid}, caller, rk, body, cid), caller,
                         evidence=ev)
            return self.confirmation_view(c)

    def decide_confirmation(self, conf_id: str, body: dict, approve: bool) -> dict:
        """Andre on a confirmed tax-reference change for a creator whose payee was already verified, or on mailing a
        confirmation to a SUPPRESSED creator who applied (AEGIS R2-L-d: only that one mail; the suppression stays in
        place for outreach)."""
        with self.lock:
            self._gate()
            rk = self._rk("confirmation_approve" if approve else "confirmation_reject", conf_id, body)
            if self._idem("andre", rk, body):
                return self.confirmation_view(self.confirmations[conf_id])
            c = self._get(self.confirmations, conf_id, "CONFIRMATION_UNKNOWN")
            if c["status"] not in ("pending_andre", "awaiting_andre"):
                raise Conflict(R("CONFIRMATION_NOT_PENDING"))
            iid = c["influencer_id"]
            if not approve:
                self._commit("confirmation_rejected", self._req({"conf_id": conf_id}, "andre", rk, body, conf_id),
                             "andre", evidence=("tax_change_rejected", f"influencer:{iid}",
                                                {"conf_id": conf_id}, ("andre", rk)))
                return self.confirmation_view(c)
            if body["content_sha256"] != c["content_sha256"] or confirmation_sha256(c) != c["content_sha256"]:
                raise Conflict(R("CONTENT_HASH_MISMATCH"))
            if self.influencers[iid].get("blocked"):
                raise Forbidden(R("INFLUENCER_BLOCKED"))
            if c["status"] == "awaiting_andre":
                expires = iso(self.now() + timedelta(days=CONFIRM_DAYS))
                msg = self._confirmation_mail(conf_id, iid, c["email_hash"], True)
                self._commit("confirmation_send_approved", self._req(
                    {"conf_id": conf_id, "message": msg, "expires_at": expires}, "andre", rk, body, conf_id), "andre",
                    evidence=("confirmation_send_approved", f"influencer:{iid}",
                              {"conf_id": conf_id, "content_sha256": c["content_sha256"]}, ("andre", rk)))
                return self.confirmation_view(c)
            self._commit("confirmation_applied", self._req({"conf_id": conf_id}, "andre", rk, body, conf_id), "andre",
                         evidence=[("tax_change_approved", f"influencer:{iid}",
                                    {"conf_id": conf_id, "content_sha256": c["content_sha256"]}, ("andre", rk)),
                                   self._tax_evidence(iid, c["payload"], "andre", rk)])
            return self.confirmation_view(c)
