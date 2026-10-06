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
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from intelligences import i02_identity, i05_suppression, i06_consent, i07_quiet_hours, i08_templates, i09_send_cap, \
    i10_replies
from ledger import derived_id
from reasons import R

SMS_MAX = 400
RESCHEDULE_DAYS = 7
CONSENT_FUTURE_SKEW = timedelta(minutes=5)


class OutreachMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_suppression_added(self, d, at):
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
            if self.warmup["started_on"] is None:
                self.warmup["started_on"] = d["date"]
            self._stats(d["date"])["sent"] += 1

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
            self._stats(msg["sent_on"])[d["event"]] += 1
        if d.get("hashes"):
            self._a_suppression_added({"hashes": d["hashes"], "reason": d["reason"], "actor": d["actor"]}, at)

    def _a_reply_received(self, d, at):
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
        self.warmup["step"] = d["step"]
        self.warmup["advanced_on"] = d["date"]

    def _stats(self, day: str) -> dict:
        return self.day_stats.setdefault(day, {"sent": 0, "complaint": 0, "hard_bounce": 0})

    # ------------------------------------------------------------------------------------------------ checks

    def _is_suppressed(self, channel: str, contact: dict) -> bool:
        return i05_suppression.suppressed(self.suppression, i05_suppression.hashes_for(channel, contact))

    def _consent_active(self, contact: dict, channel: str, brand: str) -> bool:
        return bool(contact.get("phone_hash")) and i06_consent.active(self.consents, contact["phone_hash"], channel,
                                                                      brand)

    def _cap_today(self) -> int:
        s = self.settings
        return i09_send_cap.cap(s.warmup, self.warmup["step"], s.daily_send_cap)

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
        return {"first_name": i08_templates.first_name(contact["name"]), "company": acc.get("name") or ""}

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

    def _suppress(self, actor: str, rk: str, body: dict, hashes: list[str], reason: str, kind: str = "suppression_added",
                  extra: Optional[dict] = None) -> None:
        self._commit(kind, self._req({"hashes": sorted(set(hashes)), "reason": reason, **(extra or {})}, actor, rk,
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
            h = self.messages[mid]["to_hash"]
            if h in self.suppression:
                return {"unsubscribed": True}
            rk = f"unsubscribe|{mid}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return {"unsubscribed": True}
            self._suppress(caller, rk, body, [h], "unsubscribe")
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
        mid = derived_id("msg", caller, rk)
        self._commit("message_queued", self._req({"message_id": mid, **data}, caller, rk, body, mid), caller)
        return self.message_view(self.messages[mid])

    def _phone_checks(self, c: dict, channel: str, brand: str) -> None:
        if not c.get("phone_hash"):
            raise Invalid(R("CONTACT_NO_PHONE"))
        if self._is_suppressed(channel, c):
            raise Forbidden(R("SUPPRESSED"))
        if not self._consent_active(c, channel, brand):
            raise Forbidden(R("CONSENT_REQUIRED"))
        ok = i07_quiet_hours.allowed(self.now(), c.get("time_zone"))
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
            problem = self._template_usable(t, v)
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
            problem = self._template_usable(t, v)
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
        if msg["channel"] == "voice":
            if not self._consent_active(c, "voice", msg["brand"]):
                return "CONSENT_REQUIRED", None, None
            ok = i07_quiet_hours.allowed(self.now(), c.get("time_zone"))
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
        rendered = self._render(msg, t, v, c)
        if i08_templates.rendered_sha256(rendered) != msg["rendered_sha256"]:
            return "RENDERED_CONTENT_CHANGED", None, None
        if msg["channel"] == "sms":
            if not self._consent_active(c, "sms", msg["brand"]):
                return "CONSENT_REQUIRED", None, None
            ok = i07_quiet_hours.allowed(self.now(), c.get("time_zone"))
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
                if msg["channel"] == "email" and self._stats(today)["sent"] >= self._cap_today():
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
        w = self.warmup
        step, held = w["step"], None
        if w["started_on"] is None:
            held = "NOT_STARTED"
        elif w["advanced_on"] == today.isoformat():
            held = "ALREADY_ADVANCED_TODAY"
        elif step >= len(self.settings.warmup) - 1:
            held = "AT_FULL_PACE"
        else:
            st = self._stats((today - timedelta(days=1)).isoformat())
            if i09_send_cap.may_advance(st["sent"], st["complaint"], st["hard_bounce"]):
                step += 1
            else:
                held = "HELD_BY_YESTERDAY"
        result = {"step": step + 1, "held": held, "cap_today": i09_send_cap.cap(self.settings.warmup, step,
                                                                                 self.settings.daily_send_cap)}
        data = {"date": today.isoformat() if held is None else w["advanced_on"], "step": step}
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
            hashes = [msg["to_hash"]] if body["event"] in ("hard_bounce", "complaint") else []
            data = {"message_id": msg["message_id"], "event": body["event"], "hashes": hashes,
                    "reason": body["event"] if hashes else None}
            ev = None
            if hashes:
                ev = ("suppression_added", f"msg:{msg['message_id']}", {"hashes": hashes, "reason": body["event"]},
                      (caller, rk))
            self._commit("message_event", self._req(data, caller, rk, body, msg["message_id"]), caller, evidence=ev)
            return self.message_view(msg)

    def reply(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"reply|{body.get('message_id') or 'direct'}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return dict(prev[1])
            msg = None
            if body.get("message_id") is not None:
                msg = self._get(self.messages, body["message_id"], "MESSAGE_NOT_FOUND")
            email_h = phone_h = None
            if body.get("from_email") is not None:
                e = i02_identity.email(body["from_email"])
                if e is None:
                    raise Invalid(R("EMAIL_INVALID"))
                email_h = i02_identity.keyed(self.pii_key, "email", e)
            if body.get("from_phone") is not None:
                p = i02_identity.phone(body["from_phone"])
                if p is None:
                    raise Invalid(R("PHONE_INVALID"))
                phone_h = i02_identity.keyed(self.pii_key, "phone", p)
            if msg is None and email_h is None and phone_h is None:
                raise Invalid(R("REPLY_SENDER_REQUIRED"))
            contact_id = msg["contact_id"] if msg else (self.email_index.get(email_h) if email_h else None) or \
                (self.phone_index.get(phone_h) if phone_h else None)
            c = self.contacts.get(contact_id) if contact_id else None
            cls = i10_replies.classify(body["text"])
            reply_id = derived_id("rpl", caller, rk)
            text_sha = hashlib.sha256(body["text"].encode("utf-8")).hexdigest()
            data = {"reply_id": reply_id, "channel": body["channel"], "message_id": body.get("message_id"),
                    "contact_id": contact_id, "class": cls, "text_sha256": text_sha}
            ev = None
            if cls == "unsubscribe":
                # an opt-out on any channel is honoured everywhere: every address and number we can tie to the sender
                # is suppressed, and any phone's consents are revoked (ADR 0013 decision 13)
                hashes = {h for h in (email_h, phone_h, msg["to_hash"] if msg else None,
                                      c.get("email_hash") if c else None, c.get("phone_hash") if c else None) if h}
                phones = sorted(h for h in hashes if h.startswith("phone:"))
                if phones:
                    data["revoke_keys"] = [k for ph in phones for k in self._all_consent_keys(ph)]
                if not hashes:
                    raise Invalid(R("REPLY_SENDER_REQUIRED"))
                data.update(hashes=sorted(hashes), reason="stop_reply")
                ev = ("suppression_added", f"reply:{reply_id}", {"hashes": sorted(hashes), "reason": "stop_reply"},
                      (caller, rk))
            else:
                target = f"contact:{contact_id}" if contact_id else f"reply:{reply_id}"
                kind, due = {"interested": ("book_call", self.today()),
                             "out_of_office": ("reschedule", (self.now() + timedelta(days=RESCHEDULE_DAYS)).date()
                                               .isoformat()),
                             "review": ("review_reply", self.today())}[cls]
                data["task"] = {"task_id": derived_id("tsk", kind, reply_id), "kind": kind, "target": target,
                                "due_on": due}
            answer = {"reply_id": reply_id, "class": cls, "suppressed": bool(data.get("hashes")),
                      "task_id": (data.get("task") or {}).get("task_id")}
            self._commit("reply_received", self._req(data, caller, rk, body, answer), caller, evidence=ev)
            return answer
