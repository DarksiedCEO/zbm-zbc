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
from errors import Conflict, Forbidden, NotFound
from intelligences import i04_suppression, i05_templates
from ledger import derived_id, payload_sha256
from reasons import R

CONFIRM_DAYS = 7
SUBJECTS = {"application": "Confirm your creator application",
            "tax_profile": "Confirm the change to your payout tax details"}


def confirmation_sha256(c: dict) -> str:
    """What Andre approves for a tax change: the change itself, never the raw reference (its SHA-256 stands in)."""
    p = c["payload"]
    return payload_sha256({"conf_id": c["conf_id"], "influencer_id": c["influencer_id"], "kind": c["kind"],
                           "tax_form": p.get("tax_form"), "tax_ref_sha256": p.get("tax_ref_sha256"),
                           "legal_form": p.get("legal_form"), "country": p.get("country")})


class ConfirmMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_confirmation_requested(self, d, at):
        c = dict(d["confirmation"])
        c.update(status="pending" if d.get("message") else "undeliverable", created_at=at, decided_at=None)
        self.confirmations[c["conf_id"]] = c
        if d.get("message"):
            self._a_message_queued({**d["message"], "actor": d["actor"]}, at)

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
        self.confirmations[d["conf_id"]].update(status="rejected", decided_at=at)

    # ------------------------------------------------------------------------------------------------ requesting

    def confirmation_token(self, conf_id: str) -> str:
        mac = hmac.new(self.pii_key, f"confirm\x00{conf_id}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{conf_id}.{mac}"

    def _confirmation(self, caller: str, rk: str, iid: str, kind: str, email: str, eh: str,
                      payload: dict) -> tuple[dict, Optional[dict], list]:
        """(confirmation, queued message or None, typed events) for one request; nothing is committed here."""
        cid = derived_id("cnf", caller, rk)
        conf = {"conf_id": cid, "influencer_id": iid, "kind": kind, "email": email, "email_hash": eh,
                "payload": payload, "expires_at": iso(self.now() + timedelta(days=CONFIRM_DAYS)),
                "message_id": None, "content_sha256": None}
        s = self.settings
        inf = self.influencers.get(iid)
        deliverable = bool(s.outreach_domain and s.postal_address) and eh not in self.suppression \
            and not (inf and (inf.get("blocked") or i04_suppression.suppressed(
                self.suppression, i04_suppression.hashes_of(inf))))
        msg = None
        if deliverable:
            self._queue_room(caller)
            mid = derived_id("msg", "confirm", cid)
            conf["message_id"] = mid
            msg = {"message_id": mid, "channel": "email", "purpose": "confirmation", "confirmation_id": cid,
                   "influencer_id": iid, "to_hash": eh, "brand": None, "from_domain": s.outreach_domain}
        ev = [("confirmation_requested", f"influencer:{iid}", {"conf_id": cid, "influencer_id": iid, "kind": kind,
                                                               "email_hash": eh, "deliverable": deliverable},
               (caller, rk))]
        return conf, msg, ev

    def render_confirmation(self, msg: dict, conf: dict) -> dict:
        """A fixed service text (no template, nothing generated): what was asked, the link, and how to ignore it."""
        s = self.settings
        url = f"https://{msg['from_domain']}/c/{self.confirmation_token(conf['conf_id'])}"
        names = " and ".join(i05_templates.BRAND_DISPLAY.values())
        body = (f"Someone asked to {('apply as a creator with' if conf['kind'] == 'application' else 'change the payout tax details for')} "
                f"{names} using this address.\nIf it was you, confirm here within {CONFIRM_DAYS} days: {url}\n"
                "If it was not you, ignore this email: nothing changes.\n\n--\n"
                f"{names}\n{s.postal_address}\n")
        return {"from": f"{names} <{s.from_local}@{msg['from_domain']}>", "reply_to": f"{s.from_local}@{msg['from_domain']}",
                "subject": SUBJECTS[conf["kind"]], "body": body, "headers": {}}

    def _confirmation_send_problem(self, msg: dict) -> tuple[Optional[str], Optional[dict]]:
        c = self.confirmations.get(msg["confirmation_id"])
        if c is None or c["status"] != "pending":
            return "CONFIRMATION_CLOSED", None
        if self.now() > parse_iso(c["expires_at"]):
            return "CONFIRMATION_EXPIRED", None
        inf = self.influencers.get(c["influencer_id"]) or {}
        if inf.get("blocked"):
            return "INFLUENCER_BLOCKED", None
        if msg["to_hash"] in self.suppression or i04_suppression.suppressed(self.suppression,
                                                                             i04_suppression.hashes_of(inf)):
            return "SUPPRESSED", None
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
        """Andre on a confirmed tax-reference change for a creator whose payee was already verified."""
        with self.lock:
            self._gate()
            rk = self._rk("confirmation_approve" if approve else "confirmation_reject", conf_id, body)
            if self._idem("andre", rk, body):
                return self.confirmation_view(self.confirmations[conf_id])
            c = self._get(self.confirmations, conf_id, "CONFIRMATION_UNKNOWN")
            if c["status"] != "pending_andre":
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
            self._commit("confirmation_applied", self._req({"conf_id": conf_id}, "andre", rk, body, conf_id), "andre",
                         evidence=[("tax_change_approved", f"influencer:{iid}",
                                    {"conf_id": conf_id, "content_sha256": c["content_sha256"]}, ("andre", rk)),
                                   self._tax_evidence(iid, c["payload"], "andre", rk)])
            return self.confirmation_view(c)
