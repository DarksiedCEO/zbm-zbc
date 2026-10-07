"""Outreach: consent registry, the suppression list, Andre-approved templates, queued email / SMS / voice, the send
queue with its warm-up pace, provider events and inbound replies (ADR 0013 decisions 10-13). A mixin of
SalesService.

The rules that carry legal exposure are checked twice: when a message is queued (so an agent gets a clear refusal)
and again, from current state, immediately before it is handed to a provider (a suppression, a revoked consent, an
edited template or the quiet hours that arrive in between win). A send is recorded on the ledger (typed event
``outreach_send``) and in the log BEFORE the provider is called; with the ledger down nothing is sent."""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from typing import Optional

from clock import parse_iso
from errors import Conflict, Forbidden, Invalid, NotFound, Throttled, Unavailable
from intelligences import i02_identity, i05_suppression, i06_consent, i07_quiet_hours, i08_templates, i09_send_cap, \
    i10_replies
import re

from ledger import derived_id, payload_sha256
from reasons import R

SMS_MAX = 400
RESCHEDULE_DAYS = 7
REPLY_TEXT_MAX = 20_000            # sweep A: a longer reply (a quoted thread) is cut to this, never refused
REPLY_CHANNELS = ("email", "sms", "voice")
REPLY_TAIL = 2000                  # AEGIS L2: a cut text keeps its last characters too (an opt-out at the end)


def _head_tail(text: str) -> str:
    """At most REPLY_TEXT_MAX characters: the head and the last REPLY_TAIL, joined by a newline (AEGIS L2)."""
    if len(text) <= REPLY_TEXT_MAX:
        return text
    return text[:REPLY_TEXT_MAX - REPLY_TAIL - 1] + "\n" + text[-REPLY_TAIL:]
CONSENT_FUTURE_SKEW = timedelta(minutes=5)


class OutreachMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_suppression_added(self, d, at):
        for k in d.get("revoke_keys") or ():
            self.consents.setdefault(k, []).append({"event": "revoked", "at": at, "source": d["reason"]})
        for h in d["hashes"]:
            if h not in self.suppression:            # append-only: the first entry stands, nothing is ever removed
                self.suppression[h] = {"hash": h, "reason": d["reason"], "at": at, "by": d["actor"]}
        self._cancel_suppressed(d["hashes"], at)

    def _cancel_suppressed(self, hashes, at):
        hs = set(hashes)
        for msg in self.messages.values():
            if msg["status"] == "queued" and msg["to_hash"] in hs:
                msg.update(status="cancelled", reason="SUPPRESSED", updated_at=at)

    def _a_consent_granted(self, d, at):
        ev = {k: d[k] for k in ("source", "captured_at", "consent_text_version", "consent_text_sha256")}
        self.consents.setdefault(d["key"], []).append({"event": "granted", "at": at, **ev})

    def _a_consent_revoked(self, d, at):
        for k in d["keys"]:
            self.consents.setdefault(k, []).append({"event": "revoked", "at": at, "source": d["source"]})
        self._a_suppression_added({"hashes": [d["phone_hash"]], "reason": "consent_revoked", "actor": d["actor"]}, at)

    def _a_template_created(self, d, at):
        self.templates[d["template_id"]] = {"template_id": d["template_id"], "brand": d["brand"],
                                            "channel": d["channel"], "name": d["name"], "versions": {},
                                            "created_at": at}
        self._a_template_version_added(d, at)

    def _a_template_version_added(self, d, at):
        t = self.templates[d["template_id"]]
        t["versions"][str(d["version"])] = {"version": d["version"], "subject": d.get("subject"), "body": d["body"],
                                            "content_sha256": d["content_sha256"], "status": "draft",
                                            "approved_sha256": None, "approved_at": None, "updated_at": at}

    def _a_template_edited(self, d, at):
        v = self.templates[d["template_id"]]["versions"][str(d["version"])]
        v.update(subject=d.get("subject"), body=d["body"], content_sha256=d["content_sha256"], updated_at=at)
        if v["status"] == "approved":
            v["status"] = "edited_after_approval"     # the approval binds the old hash; nothing sends from this now

    def _a_template_approved(self, d, at):
        v = self.templates[d["template_id"]]["versions"][str(d["version"])]
        v.update(status="approved", approved_sha256=d["content_sha256"], approved_at=at)

    def _a_message_queued(self, d, at):
        self.messages[d["message_id"]] = {**{k: d.get(k) for k in (
            "message_id", "channel", "contact_id", "to_hash", "brand", "template_id", "version", "template_sha256",
            "rendered_sha256", "from_domain", "purpose")}, "status": "queued", "reason": None, "queued_at": at,
            "updated_at": at, "sent_on": None, "provider_ref": None, "events": [], "queued_by": d["actor"]}

    def _a_message_sending(self, d, at):
        msg = self.messages[d["message_id"]]
        msg.update(status="sending", sent_on=d["date"], updated_at=at)
        if msg["channel"] == "email":
            w = self._wu(msg["from_domain"])
            if w["started_on"] is None:
                w["started_on"] = d["date"]
            self._stats(d["date"], msg["from_domain"])["sent"] += 1

    def _a_message_sent(self, d, at):
        self.messages[d["message_id"]].update(status="sent", provider_ref=d.get("provider_ref"), updated_at=at)

    def _a_message_failed(self, d, at):
        self.messages[d["message_id"]].update(status="failed", reason="PROVIDER_FAILED", updated_at=at)

    def _a_message_cancelled(self, d, at):
        self.messages[d["message_id"]].update(status="cancelled", reason=d["reason"], updated_at=at)

    def _a_message_event(self, d, at):
        msg = self.messages[d["message_id"]]
        msg["events"] = (msg["events"] + [{"event": d["event"], "at": at}])[-20:]
        if d["event"] in ("hard_bounce", "complaint") and msg.get("sent_on"):
            self._stats(msg["sent_on"], msg["from_domain"])[d["event"]] += 1
        if d.get("hashes"):
            self._a_suppression_added({"hashes": d["hashes"], "reason": d["reason"], "actor": d["actor"],
                                       "revoke_keys": d.get("revoke_keys")}, at)

    def _a_reply_received(self, d, at):
        if d.get("hold"):
            h = d["hold"]
            self.phone_holds[h["hold_id"]] = {**h, "status": "active", "at": at, "decided_at": None, "decision": None}
            hs = set(h["hashes"])
            for msg in self.messages.values():
                if msg["status"] == "queued" and msg["channel"] in ("sms", "voice") and msg["to_hash"] in hs:
                    msg.update(status="cancelled", reason="PHONE_HOLD", updated_at=at)
        if d.get("revoke_keys"):
            for k in d["revoke_keys"]:
                self.consents.setdefault(k, []).append({"event": "revoked", "at": at, "source": "reply"})
        if d.get("hashes"):
            self._a_suppression_added({"hashes": d["hashes"], "reason": d["reason"], "actor": d["actor"]}, at)
        if d.get("task"):
            self._a_task_opened(d["task"], at)
        if d.get("contact_id"):
            self._activity(f"contact:{d['contact_id']}", {"activity_id": d["reply_id"], "kind": "reply",
                                                          "class": d["class"], "by": d["actor"], "at": at})

    def _a_warmup_advanced(self, d, at):
        w = self._wu(d["domain"])
        w["step"] = d["step"]
        w["advanced_on"] = d["date"]

    def _wu(self, domain: Optional[str]) -> dict:
        """The warm-up state of ONE outreach domain: a new domain starts the schedule from day 1 (AEGIS S1-M2)."""
        return self.warmup.setdefault(domain or "", {"step": 0, "started_on": None, "advanced_on": None})

    def _stats(self, day: str, domain: Optional[str]) -> dict:
        return self.day_stats.setdefault(f"{domain or ''}|{day}", {"sent": 0, "complaint": 0, "hard_bounce": 0})

    # ------------------------------------------------------------------------------------------------ checks

    def _a_phone_hold_decided(self, d, at):
        h = self.phone_holds[d["hold_id"]]
        h.update(status="lifted" if d["decision"] == "not_an_opt_out" else "converted", decided_at=at,
                 decision=d["decision"])
        self.tasks[h["task_id"]].update(status="closed", closed_at=at, outcome=d["decision"])
        if d["decision"] == "opt_out":
            self._a_suppression_added({"hashes": d["hashes"], "reason": "stop_reply", "actor": d["actor"],
                                       "revoke_keys": d["revoke_keys"]}, at)

    def _held(self, phone_hash: Optional[str]) -> bool:
        """An SMS/voice reply that is not a narrow positive form holds the number (AEGIS S2-C1). Lifting a hold
        touches nothing else: a revoked consent or a suppression stays exactly as it was."""
        return bool(phone_hash) and any(h["status"] == "active" and phone_hash in h["hashes"]
                                        for h in self.phone_holds.values())

    def _is_suppressed(self, channel: str, contact: dict) -> bool:
        return i05_suppression.suppressed(self.suppression, i05_suppression.hashes_for(channel, contact))

    def _consent_active(self, contact: dict, channel: str, brand: str) -> bool:
        return bool(contact.get("phone_hash")) and i06_consent.active(self.consents, contact["phone_hash"], channel,
                                                                      brand)

    def _cap_today(self) -> int:
        s = self.settings
        return i09_send_cap.cap(s.warmup, self._wu(s.outreach_domain)["step"], s.daily_send_cap)

    def _template_version(self, template_id: str, version: int, channel: str) -> tuple[dict, dict]:
        t = self.templates.get(template_id)
        if t is None or t["channel"] != channel:
            raise NotFound(R("TEMPLATE_NOT_FOUND"))
        v = t["versions"].get(str(version))
        if v is None:
            raise NotFound(R("TEMPLATE_NOT_FOUND"))
        return t, v

    def _template_usable(self, t: dict, v: dict) -> Optional[str]:
        """None when this exact content is what Andre approved; otherwise the refusal code."""
        if v["status"] == "edited_after_approval" or (v["approved_sha256"] and v["approved_sha256"] !=
                                                       v["content_sha256"]):
            return "TEMPLATE_HASH_MISMATCH"
        if v["status"] != "approved":
            return "TEMPLATE_NOT_APPROVED"
        current = i08_templates.content_sha256(t["brand"], t["channel"], v["subject"], v["body"])
        if current != v["approved_sha256"]:
            return "TEMPLATE_HASH_MISMATCH"
        if t["channel"] == "email" and i08_templates.deceptive_subject(v["subject"] or ""):
            return "SUBJECT_DECEPTIVE"
        return None

    def _fields(self, contact: dict) -> dict:
        acc = self.accounts.get(contact["account_id"]) or {}
        # S2-H1: only values a person verified at the console; never the name typed into a form
        return {"first_name": contact.get("verified_first_name"), "company": acc.get("display_name")}

    def _merge_problem(self, t: dict, v: dict, contact: dict) -> Optional[str]:
        return i08_templates.merge_problem({"subject": v["subject"], "body": v["body"]}, self._fields(contact))

    def unsubscribe_token(self, message_id: str) -> str:
        mac = hmac.new(self.pii_key, f"unsubscribe\x00{message_id}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{message_id}.{mac}"

    def _render(self, msg: dict, t: dict, v: dict, contact: dict) -> dict:
        tpl = {"brand": t["brand"], "subject": v["subject"], "body": v["body"]}
        if msg["channel"] == "email":
            s = self.settings
            url = f"https://{msg['from_domain']}/u/{self.unsubscribe_token(msg['message_id'])}"
            return i08_templates.render_email(tpl, self._fields(contact), s.from_local, msg["from_domain"],
                                              s.postal_address, url)
        return i08_templates.render_sms(tpl, self._fields(contact))

    def message_view(self, msg: dict) -> dict:
        return dict(msg)

    def messages_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.message_view(x) for x in self.messages.values()
                    if status is None or x["status"] == status][:1000]

    # ------------------------------------------------------------------------------------------------ consent

    def grant_consent(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"consent_grant|{body['contact_id']}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return self.contact_view(self.contacts[body["contact_id"]])
            c = self._get(self.contacts, body["contact_id"], "CONTACT_NOT_FOUND")
            if not c.get("phone_hash"):
                raise Invalid(R("CONTACT_NO_PHONE"))
            try:
                captured = parse_iso(body["captured_at"])
            except ValueError:
                raise Invalid(R("INVALID"), field="captured_at") from None
            if captured > self.now() + CONSENT_FUTURE_SKEW:
                raise Invalid(R("CONSENT_IN_FUTURE"))
            if self._is_suppressed("sms", c):
                raise Forbidden(R("SUPPRESSED"))
            key = i06_consent.key(c["phone_hash"], body["channel"], body["brand"])
            ev = {k: body[k] for k in ("channel", "brand", "source", "captured_at", "consent_text_version",
                                       "consent_text_sha256")}
            self._commit("consent_granted", self._req({"key": key, "contact_id": c["contact_id"], **ev}, caller, rk,
                                                      body, c["contact_id"]), caller,
                         evidence=("consent_granted", f"contact:{c['contact_id']}",
                                   {"phone_hash": c["phone_hash"], **ev}, (caller, rk)))
            return self.contact_view(c)

    def _phone_hash_of(self, contact_id: Optional[str], phone: Optional[str]) -> str:
        if contact_id is not None:
            c = self._get(self.contacts, contact_id, "CONTACT_NOT_FOUND")
            if not c.get("phone_hash"):
                raise Invalid(R("CONTACT_NO_PHONE"))
            return c["phone_hash"]
        if phone is not None:
            p = i02_identity.phone(phone)
            if p is None:
                raise Invalid(R("PHONE_INVALID"))
            return i02_identity.keyed(self.pii_key, "phone", p)
        raise Invalid(R("CONTACT_CHANNEL_REQUIRED"))

    def _all_consent_keys(self, phone_hash: str) -> list[str]:
        return [i06_consent.key(phone_hash, ch, b) for ch in i06_consent.CHANNELS for b in ("zbm", "zbc")]

    def revoke_consent(self, caller: str, body: dict) -> dict:
        """A revocation covers every channel and both brands, and suppresses the number (ADR 0013 decision 12)."""
        with self.lock:
            self._gate()
            rk = f"consent_revoke|{body.get('contact_id') or 'phone'}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return {"revoked": True, "phone_hash": prev[1]}
            ph = self._phone_hash_of(body.get("contact_id"), body.get("phone"))
            keys = self._all_consent_keys(ph)
            self._commit("consent_revoked", self._req({"phone_hash": ph, "keys": keys, "source": body["source"]},
                                                      caller, rk, body, ph), caller,
                         evidence=("consent_revoked", f"phone:{ph[6:38]}", {"phone_hash": ph, "source": body["source"]},
                                   (caller, rk)))
            return {"revoked": True, "phone_hash": ph}

    # ------------------------------------------------------------------------------------------------ suppression

    def _opt_out_hashes(self, contact: Optional[dict], *more: Optional[str]) -> set[str]:
        """Every address and number an opt-out covers: all of the contact's, plus any given (AEGIS S1-M4)."""
        hs = {h for h in more if h}
        if contact:
            hs |= {h for h in (contact.get("email_hash"), contact.get("phone_hash")) if h}
        return hs

    def _revoke_keys(self, hashes) -> list[str]:
        return [k for ph in sorted(h for h in hashes if h.startswith("phone:")) for k in self._all_consent_keys(ph)]

    def _suppress(self, actor: str, rk: str, body: dict, hashes, reason: str, kind: str = "suppression_added",
                  extra: Optional[dict] = None) -> None:
        hashes = sorted(set(hashes))
        extra = {"revoke_keys": self._revoke_keys(hashes), **(extra or {})}
        self._commit(kind, self._req({"hashes": sorted(set(hashes)), "reason": reason, **extra}, actor, rk,
                                     body, sorted(set(hashes))), actor,
                     evidence=("suppression_added", f"suppression:{hashlib.sha256(''.join(sorted(hashes)).encode()).hexdigest()[:40]}",
                               {"hashes": sorted(set(hashes)), "reason": reason}, (actor, rk)))

    def suppress(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"suppress|{body.get('contact_id') or 'direct'}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return {"suppressed": prev[1]}
            hashes = []
            if body.get("contact_id") is not None:
                c = self._get(self.contacts, body["contact_id"], "CONTACT_NOT_FOUND")
                hashes += [h for h in (c.get("email_hash"), c.get("phone_hash")) if h]
            if body.get("email") is not None:
                e = i02_identity.email(body["email"])
                if e is None:
                    raise Invalid(R("EMAIL_INVALID"))
                hashes.append(i02_identity.keyed(self.pii_key, "email", e))
            if body.get("phone") is not None:
                p = i02_identity.phone(body["phone"])
                if p is None:
                    raise Invalid(R("PHONE_INVALID"))
                hashes.append(i02_identity.keyed(self.pii_key, "phone", p))
            if not hashes:
                raise Invalid(R("CONTACT_CHANNEL_REQUIRED"))
            self._suppress(caller, rk, body, hashes, body["reason"])
            return {"suppressed": sorted(set(hashes))}

    def unsubscribe(self, caller: str, body: dict) -> dict:
        """The one-click link (RFC 8058) relayed by the hub: the token names the message; its address is suppressed
        across both brands at once."""
        with self.lock:
            self._gate()
            token = body["token"]
            mid = token.split(".", 1)[0]
            if not hmac.compare_digest(self.unsubscribe_token(mid), token) or mid not in self.messages:
                raise NotFound(R("UNSUBSCRIBE_TOKEN_UNKNOWN"))
            msg = self.messages[mid]
            hashes = self._opt_out_hashes(self.contacts.get(msg["contact_id"]), msg["to_hash"])
            rk = f"unsubscribe|{mid}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return {"unsubscribed": True}
            if all(h in self.suppression for h in hashes) and not any(
                    self.consents.get(k) and self.consents[k][-1]["event"] == "granted"
                    for k in self._revoke_keys(hashes)):
                return {"unsubscribed": True}
            self._suppress(caller, rk, body, hashes, "unsubscribe")
            return {"unsubscribed": True}

    def suppressions_view(self) -> list[dict]:
        with self.lock:
            return [dict(x) for x in self.suppression.values()][:10_000]

    # ------------------------------------------------------------------------------------------------ templates

    def _check_template(self, channel: str, subject: Optional[str], body: str) -> None:
        if channel == "email":
            if subject is None:
                raise Invalid(R("SUBJECT_REQUIRED"))
            if i08_templates.deceptive_subject(subject):
                raise Invalid(R("SUBJECT_DECEPTIVE"))
        else:
            if subject is not None:
                raise Invalid(R("INVALID"), field="subject")
            if len(body) > SMS_MAX:
                raise Invalid(R("SMS_TOO_LONG"))
        if i08_templates.unknown_placeholders(subject or "", body):
            raise Invalid(R("PLACEHOLDER_UNKNOWN"))

    def template_view(self, t: dict) -> dict:
        return {**{k: t[k] for k in ("template_id", "brand", "channel", "name", "created_at")},
                "versions": [dict(v) for _, v in sorted(t["versions"].items(), key=lambda kv: int(kv[0]))]}

    def templates_view(self) -> list[dict]:
        with self.lock:
            return [self.template_view(t) for t in self.templates.values()]

    def template(self, template_id: str) -> dict:
        with self.lock:
            return self.template_view(self._get(self.templates, template_id, "TEMPLATE_NOT_FOUND"))

    def create_template(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            tid = derived_id("tpl", body["brand"], body["channel"], body["name"])
            rk = f"template_create|{tid}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return self.template_view(self.templates[tid])
            if tid in self.templates:
                raise Conflict(R("TEMPLATE_EXISTS"))
            self._check_template(body["channel"], body.get("subject"), body["body"])
            sha = i08_templates.content_sha256(body["brand"], body["channel"], body.get("subject"), body["body"])
            self._commit("template_created", self._req({"template_id": tid, "brand": body["brand"],
                                                        "channel": body["channel"], "name": body["name"], "version": 1,
                                                        "subject": body.get("subject"), "body": body["body"],
                                                        "content_sha256": sha}, caller, rk, body, tid), caller)
            return self.template_view(self.templates[tid])

    def add_template_version(self, caller: str, template_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"template_version|{template_id}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return self.template_view(self.templates[template_id])
            t = self._get(self.templates, template_id, "TEMPLATE_NOT_FOUND")
            self._check_template(t["channel"], body.get("subject"), body["body"])
            v = max(int(x) for x in t["versions"]) + 1
            sha = i08_templates.content_sha256(t["brand"], t["channel"], body.get("subject"), body["body"])
            self._commit("template_version_added", self._req({"template_id": template_id, "version": v,
                                                              "subject": body.get("subject"), "body": body["body"],
                                                              "content_sha256": sha}, caller, rk, body, v), caller)
            return self.template_view(t)

    def edit_template(self, caller: str, template_id: str, version: int, body: dict) -> dict:
        """Editing a version in place is allowed, but an approved version that is edited no longer matches the hash
        Andre approved: nothing sends from it until he approves the new content (ADR 0013 decision 10)."""
        with self.lock:
            self._gate()
            rk = f"template_edit|{template_id}.{version}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return self.template_view(self.templates[template_id])
            t = self._get(self.templates, template_id, "TEMPLATE_NOT_FOUND")
            if str(version) not in t["versions"]:
                raise NotFound(R("TEMPLATE_NOT_FOUND"))
            self._check_template(t["channel"], body.get("subject"), body["body"])
            sha = i08_templates.content_sha256(t["brand"], t["channel"], body.get("subject"), body["body"])
            self._commit("template_edited", self._req({"template_id": template_id, "version": version,
                                                       "subject": body.get("subject"), "body": body["body"],
                                                       "content_sha256": sha}, caller, rk, body, version), caller)
            return self.template_view(t)

    def approve_template(self, template_id: str, version: int, body: dict) -> dict:
        """Andre approves exactly the content whose SHA-256 he names."""
        with self.lock:
            self._gate()
            rk = f"template_approve|{template_id}.{version}|{body['request_id']}"
            if self._idem("andre", rk, body):
                return self.template_view(self.templates[template_id])
            t = self._get(self.templates, template_id, "TEMPLATE_NOT_FOUND")
            v = t["versions"].get(str(version))
            if v is None:
                raise NotFound(R("TEMPLATE_NOT_FOUND"))
            if body["content_sha256"] != v["content_sha256"]:
                raise Conflict(R("TEMPLATE_HASH_MISMATCH"))
            if v["status"] == "approved":
                raise Conflict(R("TEMPLATE_ALREADY_APPROVED"))
            self._check_template(t["channel"], v["subject"], v["body"])
            self._commit("template_approved", self._req({"template_id": template_id, "version": version,
                                                         "content_sha256": v["content_sha256"]}, "andre", rk, body,
                                                        version), "andre",
                         evidence=("template_approved", f"template:{template_id}",
                                   {"template_id": template_id, "version": version,
                                    "content_sha256": v["content_sha256"]}, ("andre", rk)))
            return self.template_view(t)

    # ------------------------------------------------------------------------------------------------ queue

    def _queue(self, caller: str, rk: str, body: dict, data: dict) -> dict:
        queued = sum(1 for x in self.messages.values() if x["status"] == "queued" and x["queued_by"] == caller)
        if queued >= self.settings.queue_max_per_caller:          # AEGIS S2: a caller cannot flood the log
            raise Throttled(R("QUEUE_FULL"))
        mid = derived_id("msg", caller, rk)
        self._commit("message_queued", self._req({"message_id": mid, **data}, caller, rk, body, mid), caller)
        return self.message_view(self.messages[mid])

    def _phone_checks(self, c: dict, channel: str, brand: str) -> None:
        if not c.get("phone_hash"):
            raise Invalid(R("CONTACT_NO_PHONE"))
        problem = i07_quiet_hours.phone_problem(c.get("phone"))   # S3-M1 / S3-L1
        if problem:
            raise Forbidden(R(problem))
        if self._is_suppressed(channel, c):
            raise Forbidden(R("SUPPRESSED"))
        if self._held(c["phone_hash"]):
            raise Forbidden(R("PHONE_HOLD"))
        if not self._consent_active(c, channel, brand):
            raise Forbidden(R("CONSENT_REQUIRED"))
        ok = i07_quiet_hours.allowed(self.now(), c.get("time_zone"), c.get("phone"))
        if ok is None:
            raise Forbidden(R("TIME_ZONE_UNKNOWN"))
        if not ok:
            raise Forbidden(R("QUIET_HOURS"))

    def queue_email(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"outreach_email|{body['contact_id']}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return self.message_view(self.messages[prev[1]])
            s = self.settings
            if not (s.outreach_domain and s.postal_address):
                raise Forbidden(R("OUTREACH_NOT_CONFIGURED"))
            c = self._get(self.contacts, body["contact_id"], "CONTACT_NOT_FOUND")
            if not c.get("email_hash"):
                raise Invalid(R("CONTACT_NO_EMAIL"))
            t, v = self._template_version(body["template_id"], body["version"], "email")
            problem = self._template_usable(t, v) or self._merge_problem(t, v, c)
            if problem:
                raise Forbidden(R(problem))
            if self._is_suppressed("email", c):
                raise Forbidden(R("SUPPRESSED"))
            data = {"channel": "email", "contact_id": c["contact_id"], "to_hash": c["email_hash"], "brand": t["brand"],
                    "template_id": t["template_id"], "version": body["version"], "template_sha256": v["approved_sha256"],
                    "from_domain": s.outreach_domain}
            mid = derived_id("msg", caller, rk)
            data["rendered_sha256"] = i08_templates.rendered_sha256(self._render({**data, "message_id": mid}, t, v, c))
            return self._queue(caller, rk, body, data)

    def queue_sms(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"outreach_sms|{body['contact_id']}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return self.message_view(self.messages[prev[1]])
            c = self._get(self.contacts, body["contact_id"], "CONTACT_NOT_FOUND")
            t, v = self._template_version(body["template_id"], body["version"], "sms")
            problem = self._template_usable(t, v) or self._merge_problem(t, v, c)
            if problem:
                raise Forbidden(R(problem))
            self._phone_checks(c, "sms", t["brand"])
            data = {"channel": "sms", "contact_id": c["contact_id"], "to_hash": c["phone_hash"], "brand": t["brand"],
                    "template_id": t["template_id"], "version": body["version"], "template_sha256": v["approved_sha256"]}
            mid = derived_id("msg", caller, rk)
            data["rendered_sha256"] = i08_templates.rendered_sha256(self._render({**data, "message_id": mid}, t, v, c))
            return self._queue(caller, rk, body, data)

    def queue_voice(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"outreach_voice|{body['contact_id']}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return self.message_view(self.messages[prev[1]])
            c = self._get(self.contacts, body["contact_id"], "CONTACT_NOT_FOUND")
            self._phone_checks(c, "voice", body["brand"])
            return self._queue(caller, rk, body, {"channel": "voice", "contact_id": c["contact_id"],
                                                  "to_hash": c["phone_hash"], "brand": body["brand"],
                                                  "purpose": body["purpose"]})

    def cancel_message(self, caller: str, message_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"message_cancel|{message_id}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return self.message_view(self.messages[message_id])
            msg = self._get(self.messages, message_id, "MESSAGE_NOT_FOUND")
            if msg["status"] != "queued":
                raise Conflict(R("MESSAGE_NOT_QUEUED"))
            self._commit("message_cancelled", self._req({"message_id": message_id, "reason": "CANCELLED_BY_CALLER"},
                                                        caller, rk, body, message_id), caller)
            return self.message_view(msg)

    # ------------------------------------------------------------------------------------------------ send tick

    def _send_problem(self, msg: dict) -> tuple[Optional[str], Optional[dict], Optional[str]]:
        """(cancel_code, rendered, defer_code) for one queued message, from current state."""
        c = self.contacts.get(msg["contact_id"])
        if c is None or msg["to_hash"] in self.suppression or self._is_suppressed(msg["channel"], c):
            return "SUPPRESSED", None, None
        if msg["channel"] in ("sms", "voice") and (self._held(msg["to_hash"]) or self._held(c.get("phone_hash"))):
            return "PHONE_HOLD", None, None
        if msg["channel"] in ("sms", "voice") and i07_quiet_hours.phone_problem(c.get("phone")):
            return i07_quiet_hours.phone_problem(c.get("phone")), None, None
        if msg["channel"] == "voice":
            if not self._consent_active(c, "voice", msg["brand"]):
                return "CONSENT_REQUIRED", None, None
            ok = i07_quiet_hours.allowed(self.now(), c.get("time_zone"), c.get("phone"))
            if ok is None:
                return "TIME_ZONE_UNKNOWN", None, None
            return None, {"purpose": msg["purpose"], "brand": msg["brand"]}, None if ok else "QUIET_HOURS"
        t = self.templates.get(msg["template_id"])
        v = t["versions"].get(str(msg["version"])) if t else None
        if t is None or v is None:
            return "TEMPLATE_NOT_FOUND", None, None
        problem = self._template_usable(t, v)
        if problem or v["approved_sha256"] != msg["template_sha256"]:
            return problem or "TEMPLATE_HASH_MISMATCH", None, None
        if msg["channel"] == "email" and msg["from_domain"] != self.settings.outreach_domain:
            return "OUTREACH_DOMAIN_CHANGED", None, None
        merge = self._merge_problem(t, v, c)               # AEGIS S1-H1: again, from current state, at send time
        if merge:
            return merge, None, None
        rendered = self._render(msg, t, v, c)
        if i08_templates.rendered_sha256(rendered) != msg["rendered_sha256"]:
            return "RENDERED_CONTENT_CHANGED", None, None
        if msg["channel"] == "sms":
            if not self._consent_active(c, "sms", msg["brand"]):
                return "CONSENT_REQUIRED", None, None
            ok = i07_quiet_hours.allowed(self.now(), c.get("time_zone"), c.get("phone"))
            if ok is None:
                return "TIME_ZONE_UNKNOWN", None, None
            if not ok:
                return None, rendered, "QUIET_HOURS"
        return None, rendered, None

    def send_tick(self, body: dict) -> dict:
        """The ``send-queue`` job. One message at a time: under the lock the rules are re-checked and the send is
        recorded (typed ``outreach_send`` event, then the log line); the provider is called outside the lock; the
        outcome is recorded after. A message whose outcome could not be recorded stays ``sending`` (never resent
        on its own)."""
        summary = {"sent": 0, "failed": 0, "cancelled": 0, "deferred_quiet_hours": 0, "capped": 0, "not_wired": 0,
                   "stopped": None}
        with self.lock:
            self._gate()
            rk = f"job|send-queue|{body['request_id']}"
            prev = self._idem("scheduler", rk, body)
            if prev:
                return {"job": "send-queue", "already_ran": True, **(prev[1] or {})}
            queue = sorted((m for m in self.messages.values() if m["status"] == "queued"),
                           key=lambda m: (m["queued_at"], m["message_id"]))
            ids = [m["message_id"] for m in queue]
        for mid in ids:
            with self.lock:
                msg = self.messages[mid]
                if msg["status"] != "queued":
                    continue
                cancel, rendered, defer = self._send_problem(msg)
                if cancel:
                    try:
                        self._commit("message_cancelled", {"message_id": mid, "reason": cancel}, "scheduler")
                        summary["cancelled"] += 1
                    except Unavailable:
                        summary["stopped"] = "LEDGER_UNAVAILABLE"
                        break
                    continue
                if defer:
                    summary["deferred_quiet_hours"] += 1
                    continue
                today = self.today()
                if msg["channel"] == "email" and self._stats(today, msg["from_domain"])["sent"] >= self._cap_today():
                    summary["capped"] += 1
                    continue
                port = getattr(self.ports, msg["channel"])
                if not port.wired:
                    summary["not_wired"] += 1           # stays queued, visibly; nothing is recorded as sent
                    continue
                contact = self.contacts[msg["contact_id"]]
                to = contact["email"] if msg["channel"] == "email" else contact["phone"]
                try:
                    self._commit("message_sending", {"message_id": mid, "date": today}, "scheduler",
                                 evidence=("outreach_send", f"msg:{mid}",
                                           {"message_id": mid, "channel": msg["channel"], "to_hash": msg["to_hash"],
                                            "brand": msg["brand"], "template_sha256": msg.get("template_sha256"),
                                            "rendered_sha256": msg.get("rendered_sha256")}, (mid,)))
                except Unavailable:
                    summary["stopped"] = "LEDGER_UNAVAILABLE"
                    break
            try:
                result = port.send(mid, to, rendered)
            except Exception:       # a provider adapter must never take the tick down; the send is "failed"
                result = None
            with self.lock:
                try:
                    if result is not None and result.status == "accepted":
                        self._commit("message_sent", {"message_id": mid, "provider_ref": result.provider_ref},
                                     "scheduler")
                        summary["sent"] += 1
                    else:
                        self._commit("message_failed", {"message_id": mid}, "scheduler")
                        summary["failed"] += 1
                except Unavailable:
                    summary["stopped"] = "LEDGER_UNAVAILABLE"
                    break
        with self.lock:
            if summary["stopped"]:
                raise Unavailable(R("LEDGER_UNAVAILABLE"), **{k: v for k, v in summary.items() if k != "stopped"})
            self._commit("job_ran", self._req({"job": "send-queue"}, "scheduler", rk, body, summary), "scheduler")
        return {"job": "send-queue", **summary}

    def _warmup_reset(self, body: dict, rk: str) -> dict:
        today = self.now().date()
        domain = self.settings.outreach_domain
        w = self._wu(domain)
        step, held = w["step"], None
        if domain is None:
            held = "NO_OUTREACH_DOMAIN"
        elif w["started_on"] is None:
            held = "NOT_STARTED"
        elif w["advanced_on"] == today.isoformat():
            held = "ALREADY_ADVANCED_TODAY"
        elif step >= len(self.settings.warmup) - 1:
            held = "AT_FULL_PACE"
        else:
            st = self._stats((today - timedelta(days=1)).isoformat(), domain)
            if i09_send_cap.may_advance(st["sent"], st["complaint"], st["hard_bounce"]):
                step += 1
            else:
                held = "HELD_BY_YESTERDAY"
        result = {"domain": domain, "step": step + 1, "held": held,
                  "cap_today": i09_send_cap.cap(self.settings.warmup, step, self.settings.daily_send_cap)}
        data = {"domain": domain, "date": today.isoformat() if held is None else w["advanced_on"], "step": step}
        self._commit("warmup_advanced", self._req(data, "scheduler", rk, body, result), "scheduler")
        return result

    # ------------------------------------------------------------------------------------------------ provider events

    def email_event(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"email_event|{body['message_id']}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return self.message_view(self.messages[body["message_id"]])
            msg = self._get(self.messages, body["message_id"], "MESSAGE_NOT_FOUND")
            if msg["channel"] != "email":
                raise Invalid(R("INVALID"), field="message_id")
            if msg["status"] not in ("sending", "sent", "failed"):
                raise Conflict(R("MESSAGE_NOT_SENT"))
            if body["event"] == "complaint":          # a spam complaint is an opt-out: everywhere (S1-M4)
                hashes = sorted(self._opt_out_hashes(self.contacts.get(msg["contact_id"]), msg["to_hash"]))
            else:
                hashes = [msg["to_hash"]] if body["event"] == "hard_bounce" else []
            data = {"message_id": msg["message_id"], "event": body["event"], "hashes": hashes,
                    "reason": body["event"] if hashes else None, "revoke_keys": self._revoke_keys(hashes)}
            ev = None
            if hashes:
                ev = ("suppression_added", f"msg:{msg['message_id']}", {"hashes": hashes, "reason": body["event"]},
                      (caller, rk))
            self._commit("message_event", self._req(data, caller, rk, body, msg["message_id"]), caller, evidence=ev)
            return self.message_view(msg)

    @staticmethod
    def _reply_fields(body: dict, raw: dict) -> dict:
        """What the service reads from a provider's reply (sweep A; influencer-py's AEGIS R2-N3): never an error. A
        text of any length is cut to REPLY_TEXT_MAX characters BEFORE it is classified and hashed; a field of the
        wrong type is absent; an unknown channel is ``other`` (the lower phone opt-out bar applies); an unreadable
        request id is replaced by the SHA-256 of the body (the relay's retry of the same body is the same reply)."""
        def text_of(v, n):
            return v[:n] if isinstance(v, str) else None
        rq = body.get("request_id")
        if not (isinstance(rq, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", rq)):
            rq = "h-" + payload_sha256(raw)[:40]
        ch = body.get("channel")
        return {"request_id": rq, "channel": ch if ch in REPLY_CHANNELS else "other",
                "message_id": text_of(body.get("message_id"), 1000), "from_email": text_of(body.get("from_email"), 1000),
                "from_phone": text_of(body.get("from_phone"), 1000),
                "text": _head_tail(body.get("text")) if isinstance(body.get("text"), str) else ""}

    def reply(self, caller: str, raw_body: dict, raw: Optional[dict] = None) -> dict:
        body = self._reply_fields(raw_body, raw if raw is not None else raw_body)
        idem_body = {**{k: v for k, v in body.items() if k != "text"},
                     "text_sha256": hashlib.sha256(body["text"].encode("utf-8", "surrogatepass")).hexdigest()}
        return self._reply(caller, body, idem_body)

    def _reply(self, caller: str, body: dict, idem_body: dict) -> dict:
        """A reply on any channel. It is NEVER refused for what the provider sent (sweep A; influencer-py's AEGIS
        R1-M4): an unknown message id, a sender address or number that cannot be read are each ignored (named in
        ``ignored``), ``Name <addr>`` is read as ``addr``, and whatever does resolve is used; when nothing resolves
        the reply is still recorded, as a review task for a person (S4-M2)."""
        with self.lock:
            self._gate()
            # influencer-py AEGIS R3-L1: the key is the request id AND the body's hash: the same id with another body
            # is another reply (never a 409 that drops an opt-out); the same body again is the same reply
            target = payload_sha256({k: v for k, v in idem_body.items() if k != "request_id"})[:40]
            rk = f"reply|{target}|{body['request_id']}"
            prev = self._idem(caller, rk, idem_body)
            if prev:
                return dict(prev[1])
            ignored = []
            msg = self.messages.get(body.get("message_id") or "")
            if body.get("message_id") is not None and msg is None:
                ignored.append("message_id")
            email_h = phone_h = None
            if body.get("from_email") is not None:
                e = i02_identity.sender_email(body["from_email"])
                if e is not None:
                    email_h = i02_identity.keyed(self.pii_key, "email", e)
                else:
                    ignored.append("from_email")
            if body.get("from_phone") is not None:
                p = i02_identity.phone(body["from_phone"])
                if p is not None:
                    phone_h = i02_identity.keyed(self.pii_key, "phone", p)
                else:
                    ignored.append("from_phone")
            contact_id = msg["contact_id"] if msg else (self.email_index.get(email_h) if email_h else None) or \
                (self.phone_index.get(phone_h) if phone_h else None)
            c = self.contacts.get(contact_id) if contact_id else None
            cls = i10_replies.classify(body["text"], body["channel"])
            reply_id = derived_id("rpl", caller, rk)
            text_sha = idem_body["text_sha256"]
            data = {"reply_id": reply_id, "channel": body["channel"], "message_id": msg["message_id"] if msg else None,
                    "contact_id": contact_id, "class": cls, "text_sha256": text_sha, "ignored": ignored}
            target = f"contact:{contact_id}" if contact_id else f"reply:{reply_id}"
            # every number tied to the reply: the contact's, the message's, the sender's
            phones = {h for h in (phone_h, msg["to_hash"] if msg and msg["channel"] != "email" else None,
                                  c.get("phone_hash") if c else None) if h}
            # AEGIS S4-M2 / S5-L2: an UNRESOLVED reply may name its sender in the body. A number in the body is
            # HELD (never suppressed: anyone can type anyone's number); an address in the body resolves to a contact
            # whose numbers are held. A person always reviews an unresolved reply and confirms any opt-out.
            body_phones: set = set()
            named_phones: set = set()
            if contact_id is None:
                for p in i02_identity.phones_in(body["text"]):
                    ph = i02_identity.keyed(self.pii_key, "phone", p)
                    body_phones.add(ph)
                    named = self.contacts.get(self.phone_index.get(ph) or "")
                    if named and named.get("phone_hash"):
                        named_phones.add(named["phone_hash"])
                for e in i02_identity.emails_in(body["text"]):
                    named = self.contacts.get(self.email_index.get(i02_identity.keyed(self.pii_key, "email", e)) or "")
                    if named and named.get("phone_hash"):
                        named_phones.add(named["phone_hash"])
            evidence = []
            hold_phones: set = set()
            auto = False
            if cls == "unsubscribe":
                # an opt-out on any channel is honoured everywhere: every address and number we can tie to the sender
                # is suppressed, and any phone's consents are revoked (ADR 0013 decision 13)
                hashes = {h for h in (email_h, phone_h, msg["to_hash"] if msg else None,
                                      c.get("email_hash") if c else None, c.get("phone_hash") if c else None) if h}
                # S5-L2: only the SENDER's own address and number (and the resolved contact's) get the automatic
                # opt-out; a number merely written in the body is held for a person, never suppressed or revoked.
                # Sweep A: nothing resolving is never a refusal — the reply reaches a person as a review task.
                if hashes:
                    phs = sorted(h for h in hashes if h.startswith("phone:"))
                    if phs:
                        data["revoke_keys"] = [k for ph in phs for k in self._all_consent_keys(ph)]
                    data.update(hashes=sorted(hashes), reason="stop_reply")
                    evidence.append(("suppression_added", f"reply:{reply_id}",
                                     {"hashes": sorted(hashes), "reason": "stop_reply"}, (caller, rk)))
                hold_phones = (named_phones | body_phones) - hashes     # named in the body: held for a person
            else:
                # AEGIS S3-C1/C2/H1: no interpretation on any path that can lead to another text or call. ANY reply,
                # on any channel, holds phone outreach (SMS and voice, both brands) for every number of the resolved
                # contact and the number it came from — "yes" and "interested" included (a person follows up and
                # Andre releases). The ONE exception is a reply whose raw body, trimmed and lower-cased only, is
                # exactly one of the fixed auto-reply texts (i10_replies.AUTO_REPLIES): no person's words in it.
                auto = i10_replies.exact_auto_reply(body["text"])
                if auto:
                    cls = data["class"] = "out_of_office"
                hold_phones = set() if auto else phones | body_phones | named_phones
            if hold_phones:
                hold_id = derived_id("hld", reply_id)
                task_id = derived_id("tsk", "review_reply", reply_id)
                data["hold"] = {"hold_id": hold_id, "hashes": sorted(hold_phones), "reply_id": reply_id,
                                "task_id": task_id}
                data["task"] = {"task_id": task_id, "kind": "review_reply", "hold_id": hold_id, "target": target,
                                "due_on": self.today()}
                evidence.append(("phone_hold_applied", f"hold:{hold_id}", {"hold_id": hold_id,
                                                                            "hashes": sorted(hold_phones),
                                                                            "reply_id": reply_id, "class": cls},
                                 (caller, rk)))
            elif contact_id is None:
                # S4-M2: an unresolved reply always reaches a person, opt-outs included
                data["task"] = {"task_id": derived_id("tsk", "review_reply", reply_id), "kind": "review_reply",
                                "target": target, "due_on": self.today()}
            elif cls != "unsubscribe":
                kind, due = {"interested": ("book_call", self.today()),
                             "out_of_office": ("reschedule", (self.now() + timedelta(days=RESCHEDULE_DAYS))
                                               .date().isoformat()),
                             "review": ("review_reply", self.today())}[cls]
                data["task"] = {"task_id": derived_id("tsk", kind, reply_id), "kind": kind, "target": target,
                                "due_on": due}
            ev = evidence or None
            answer = {"reply_id": reply_id, "class": cls, "suppressed": bool(data.get("hashes")),
                      "held": bool(data.get("hold")),
                      "task_id": (data.get("task") or {}).get("task_id"), "ignored": ignored}
            self._commit("reply_received", self._req(data, caller, rk, idem_body, answer), caller, evidence=ev)
            return answer

    def decide_hold(self, task_id: str, body: dict) -> dict:
        """Andre's decision on a held SMS/voice reply (AEGIS S2-C1). ``not_an_opt_out`` lifts the hold and nothing
        else (consents and suppressions are untouched); ``opt_out`` makes it a permanent opt-out."""
        with self.lock:
            self._gate()
            rk = f"hold_decision|{task_id}|{body['request_id']}"
            if self._idem("andre", rk, body):
                return dict(self.tasks[task_id])
            t = self._get(self.tasks, task_id, "TASK_NOT_FOUND")
            hold = self.phone_holds.get(t.get("hold_id") or "")
            if hold is None:
                raise Conflict(R("TASK_HAS_NO_HOLD"))
            if hold["status"] != "active":
                raise Conflict(R("TASK_CLOSED"))
            data = {"hold_id": hold["hold_id"], "decision": body["decision"], "hashes": hold["hashes"],
                    "revoke_keys": self._revoke_keys(hold["hashes"]) if body["decision"] == "opt_out" else []}
            self._commit("phone_hold_decided", self._req(data, "andre", rk, body, task_id), "andre",
                         evidence=("phone_hold_decided", f"hold:{hold['hold_id']}",
                                   {"hold_id": hold["hold_id"], "decision": body["decision"]}, ("andre", rk)))
            return dict(self.tasks[task_id])
