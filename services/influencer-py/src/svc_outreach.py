"""Outreach: the suppression list (one list, both brands), Andre-approved email templates, queued email under
CAN-SPAM from the separate outreach domain, platform DM drafts that Andre approves one by one, provider events,
replies and the holds they put on an influencer (ADR 0015 decisions 10-13). A mixin of InfluencerService.

The rules are checked twice: when a message is queued or a DM approved (a clear refusal) and again, from current
state, immediately before it is handed to a provider (a suppression, a hold, an edited template or a block that
arrives in between wins). A send is recorded on the ledger (typed event ``outreach_send``) and in the log BEFORE the
provider is called; with the ledger down nothing is sent. Nothing generated is ever sent: an email is an approved
template's exact content plus the service's footer, a DM is exactly the text Andre approved by its hash."""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from errors import Conflict, Forbidden, Invalid, NotFound, Throttled, Unavailable
from intelligences import i02_identity, i04_suppression, i05_templates, i06_send_cap, i07_replies, i10_dm_guard
from ledger import derived_id
from reasons import R


def _set_sha(hashes) -> str:
    return hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest()[:40]


class OutreachMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_suppression_added(self, d, at):
        for h in d["hashes"]:
            if h not in self.suppression:            # append-only: the first entry stands, nothing is ever removed
                self.suppression[h] = {"hash": h, "reason": d["reason"], "at": at, "by": d["actor"]}
        self._cancel_queued(set(d["hashes"]), at, "SUPPRESSED")

    def _cancel_queued(self, hashes: set, at: str, reason: str, influencer_id: Optional[str] = None) -> None:
        for msg in self.messages.values():
            if msg["status"] != "queued":
                continue
            if reason == "REPLY_HOLD" and msg.get("purpose") == "confirmation":
                continue            # a confirmation answers the creator's own request; a hold stops outreach only
            inf = self.influencers.get(msg["influencer_id"]) or {}
            if msg["influencer_id"] == influencer_id or msg["to_hash"] in hashes \
                    or hashes & set(i04_suppression.hashes_of(inf)):
                msg.update(status="cancelled", reason=reason, updated_at=at)

    def _a_template_created(self, d, at):
        self.templates[d["template_id"]] = {"template_id": d["template_id"], "brand": d["brand"], "name": d["name"],
                                            "versions": {}, "created_at": at}
        self._a_template_version_added(d, at)

    def _a_template_version_added(self, d, at):
        t = self.templates[d["template_id"]]
        t["versions"][str(d["version"])] = {"version": d["version"], "subject": d["subject"], "body": d["body"],
                                            "content_sha256": d["content_sha256"], "status": "draft",
                                            "approved_sha256": None, "approved_at": None, "updated_at": at}

    def _a_template_approved(self, d, at):
        v = self.templates[d["template_id"]]["versions"][str(d["version"])]
        v.update(status="approved", approved_sha256=d["content_sha256"], approved_at=at)

    def _a_message_queued(self, d, at):
        self.messages[d["message_id"]] = {**{k: d.get(k) for k in (
            "message_id", "channel", "platform", "influencer_id", "to_hash", "brand", "template_id", "version",
            "template_sha256", "draft_id", "content_sha256", "rendered_sha256", "from_domain", "purpose",
            "confirmation_id")},
            "status": "queued", "reason": None, "queued_at": at, "updated_at": at, "sent_on": None,
            "provider_ref": None, "events": [], "queued_by": d["actor"]}

    def _a_message_sending(self, d, at):
        msg = self.messages[d["message_id"]]
        msg.update(status="sending", sent_on=d["date"], updated_at=at)
        if msg["channel"] == "email":
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
        if d["event"] in ("hard_bounce", "complaint") and msg.get("sent_on") and msg["channel"] == "email":
            self._stats(msg["sent_on"], msg["from_domain"])[d["event"]] += 1
        if d.get("hashes"):
            self._a_suppression_added({"hashes": d["hashes"], "reason": d["reason"], "actor": d["actor"]}, at)

    def _a_dm_drafted(self, d, at):
        self.dm_drafts[d["draft_id"]] = {**{k: d[k] for k in ("draft_id", "influencer_id", "platform", "handle_hash",
                                                               "brand", "text", "content_sha256")},
                                         "status": "draft", "message_id": None, "drafted_by": d["actor"],
                                         "created_at": at, "updated_at": at}

    def _a_dm_approved(self, d, at):
        dr = self.dm_drafts[d["draft_id"]]
        dr.update(status="approved", message_id=d["message_id"], updated_at=at)
        self._a_message_queued({"message_id": d["message_id"], "channel": "dm", "platform": dr["platform"],
                                "influencer_id": dr["influencer_id"], "to_hash": dr["handle_hash"],
                                "brand": dr["brand"], "draft_id": dr["draft_id"],
                                "content_sha256": dr["content_sha256"], "actor": d["actor"]}, at)

    def _a_dm_rejected(self, d, at):
        self.dm_drafts[d["draft_id"]].update(status="rejected", updated_at=at)

    def _a_reply_received(self, d, at):
        self.replies[d["reply_id"]] = {k: d.get(k) for k in ("reply_id", "channel", "message_id", "influencer_id",
                                                               "class", "text_sha256")}
        self.replies[d["reply_id"]]["at"] = at
        if d.get("hashes"):
            self._a_suppression_added({"hashes": d["hashes"], "reason": d["reason"], "actor": d["actor"]}, at)
        if d.get("hold"):
            h = d["hold"]
            self.holds[h["hold_id"]] = {**h, "status": "active", "at": at, "decided_at": None, "decision": None}
            self._cancel_queued(set(h["hashes"]), at, "REPLY_HOLD", h.get("influencer_id"))

    def _a_hold_decided(self, d, at):
        h = self.holds[d["hold_id"]]
        h.update(status="lifted" if d["decision"] == "continue" else "opted_out", decided_at=at,
                 decision=d["decision"])
        if d["decision"] == "opt_out":
            self._a_suppression_added({"hashes": d["hashes"], "reason": "hold_opt_out", "actor": d["actor"]}, at)

    def _stats(self, day: str, domain: Optional[str]) -> dict:
        return self.day_stats.setdefault(f"{domain or ''}|{day}", {"sent": 0, "complaint": 0, "hard_bounce": 0})

    # ------------------------------------------------------------------------------------------------ checks

    def _held(self, inf: dict) -> bool:
        """ANY reply on ANY channel holds every further automatic outreach to that influencer (email and DMs, both
        brands) until Andre decides (sales-py's fail-closed reply design, S3-C1). A hold covers the influencer it
        resolved to AND every address and handle it names, so an unresolved reply still holds whoever they turn out
        to be."""
        hs = set(i04_suppression.hashes_of(inf))
        return any(h["status"] == "active" and (h.get("influencer_id") == inf["influencer_id"] or hs & set(h["hashes"]))
                   for h in self.holds.values())

    def _outreach_problem(self, inf: dict) -> Optional[str]:
        if inf.get("blocked"):
            return "INFLUENCER_BLOCKED"
        if i04_suppression.suppressed(self.suppression, i04_suppression.hashes_of(inf)):
            return "SUPPRESSED"
        if self._held(inf):
            return "REPLY_HOLD"
        return None

    def _template_version(self, template_id: str, version: int) -> tuple[dict, dict]:
        t = self.templates.get(template_id)
        v = t["versions"].get(str(version)) if t else None
        if t is None or v is None:
            raise NotFound(R("TEMPLATE_NOT_FOUND"))
        return t, v

    def _template_usable(self, t: dict, v: dict) -> Optional[str]:
        """None when this exact content is what Andre approved; otherwise the refusal code."""
        if v["status"] != "approved":
            return "TEMPLATE_NOT_APPROVED"
        current = i05_templates.content_sha256(t["brand"], v["subject"], v["body"])
        if current != v["approved_sha256"] or current != v["content_sha256"]:
            return "TEMPLATE_HASH_MISMATCH"
        if i05_templates.deceptive_subject(v["subject"]):
            return "SUBJECT_DECEPTIVE"
        return None

    def _fields(self, inf: dict) -> dict:
        return {"first_name": inf.get("verified_first_name")}      # S2-H1: only what a person verified

    def unsubscribe_token(self, message_id: str) -> str:
        mac = hmac.new(self.pii_key, f"unsubscribe\x00{message_id}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{message_id}.{mac}"

    def _render_email(self, msg: dict, t: dict, v: dict, inf: dict) -> dict:
        s = self.settings
        url = f"https://{msg['from_domain']}/u/{self.unsubscribe_token(msg['message_id'])}"
        return i05_templates.render_email({"brand": t["brand"], "subject": v["subject"], "body": v["body"]},
                                          self._fields(inf), s.from_local, msg["from_domain"], s.postal_address, url)

    def message_view(self, msg: dict) -> dict:
        return dict(msg)

    def messages_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.message_view(x) for x in self.messages.values()
                    if status is None or x["status"] == status][:1000]

    # ------------------------------------------------------------------------------------------------ suppression

    def _suppression_evidence(self, hashes, reason: str, actor: str, rk: str):
        return ("suppression_added", f"suppression:{_set_sha(hashes)}", {"hashes": sorted(hashes), "reason": reason},
                (actor, rk))

    def suppress(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("suppress", body.get("influencer_id") or "direct", body)
            prev = self._idem(caller, rk, body)
            if prev:
                return {"suppressed": prev[1]}
            hashes = set()
            if body.get("influencer_id") is not None:
                inf = self._get(self.influencers, body["influencer_id"], "INFLUENCER_NOT_FOUND")
                hashes |= set(i04_suppression.hashes_of(inf))
            if body.get("email") is not None:
                hashes.add(self._email(body["email"])[1])
            if body.get("handle") is not None:
                hashes.add(self._handles([body["handle"]])[0]["handle_hash"])
            if not hashes:
                raise Invalid(R("TARGET_REQUIRED"))
            hashes = sorted(hashes)
            self._commit("suppression_added", self._req({"hashes": hashes, "reason": body["reason"]}, caller, rk,
                                                        body, hashes), caller,
                         evidence=self._suppression_evidence(hashes, body["reason"], caller, rk))
            return {"suppressed": hashes}

    def unsubscribe(self, caller: str, body: dict) -> dict:
        """The one-click link (RFC 8058) relayed by the hub: the token names the message; every address and handle of
        that influencer is suppressed across both brands at once (email AND DMs)."""
        with self.lock:
            self._gate()
            token = body["token"]
            mid = token.split(".", 1)[0]
            if not hmac.compare_digest(self.unsubscribe_token(mid), token) or mid not in self.messages:
                raise NotFound(R("UNSUBSCRIBE_TOKEN_UNKNOWN"))
            rk = self._rk("unsubscribe", mid, body)
            if self._idem(caller, rk, body):
                return {"unsubscribed": True}
            msg = self.messages[mid]
            inf = self.influencers.get(msg["influencer_id"]) or {}
            hashes = sorted(set(i04_suppression.hashes_of(inf)) | {msg["to_hash"]})
            if all(h in self.suppression for h in hashes):
                return {"unsubscribed": True}
            self._commit("suppression_added", self._req({"hashes": hashes, "reason": "unsubscribe"}, caller, rk, body,
                                                        True), caller,
                         evidence=self._suppression_evidence(hashes, "unsubscribe", caller, rk))
            return {"unsubscribed": True}

    def suppressions_view(self) -> list[dict]:
        with self.lock:
            return [dict(x) for x in self.suppression.values()][:10_000]

    # ------------------------------------------------------------------------------------------------ templates

    def _check_template(self, subject: str, body: str) -> None:
        if i05_templates.deceptive_subject(subject):
            raise Invalid(R("SUBJECT_DECEPTIVE"))
        if i05_templates.unknown_placeholders(subject, body):
            raise Invalid(R("PLACEHOLDER_UNKNOWN"))

    def template_view(self, t: dict) -> dict:
        return {**{k: t[k] for k in ("template_id", "brand", "name", "created_at")},
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
            tid = derived_id("tpl", body["brand"], body["name"])
            rk = self._rk("template_create", tid, body)
            if self._idem(caller, rk, body):
                return self.template_view(self.templates[tid])
            if tid in self.templates:
                raise Conflict(R("TEMPLATE_EXISTS"))
            self._check_template(body["subject"], body["body"])
            sha = i05_templates.content_sha256(body["brand"], body["subject"], body["body"])
            self._commit("template_created", self._req({"template_id": tid, "brand": body["brand"],
                                                        "name": body["name"], "version": 1,
                                                        "subject": body["subject"], "body": body["body"],
                                                        "content_sha256": sha}, caller, rk, body, tid), caller)
            return self.template_view(self.templates[tid])

    def add_template_version(self, caller: str, template_id: str, body: dict) -> dict:
        """Templates are never edited in place: a change is a new version, which Andre approves by its own hash."""
        with self.lock:
            self._gate()
            rk = self._rk("template_version", template_id, body)
            if self._idem(caller, rk, body):
                return self.template_view(self.templates[template_id])
            t = self._get(self.templates, template_id, "TEMPLATE_NOT_FOUND")
            self._check_template(body["subject"], body["body"])
            v = max(int(x) for x in t["versions"]) + 1
            sha = i05_templates.content_sha256(t["brand"], body["subject"], body["body"])
            self._commit("template_version_added", self._req({"template_id": template_id, "version": v,
                                                              "subject": body["subject"], "body": body["body"],
                                                              "content_sha256": sha}, caller, rk, body, v), caller)
            return self.template_view(t)

    def approve_template(self, template_id: str, version: int, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("template_approve", f"{template_id}.{version}", body)
            if self._idem("andre", rk, body):
                return self.template_view(self.templates[template_id])
            t, v = self._template_version(template_id, version)
            current = i05_templates.content_sha256(t["brand"], v["subject"], v["body"])
            if body["content_sha256"] != v["content_sha256"] or current != v["content_sha256"]:
                raise Conflict(R("CONTENT_HASH_MISMATCH"))
            if v["status"] == "approved":
                raise Conflict(R("ALREADY_APPROVED"))
            self._check_template(v["subject"], v["body"])
            self._commit("template_approved", self._req({"template_id": template_id, "version": version,
                                                         "content_sha256": v["content_sha256"]}, "andre", rk, body,
                                                        version), "andre",
                         evidence=("template_approved", f"template:{template_id}",
                                   {"template_id": template_id, "version": version,
                                    "content_sha256": v["content_sha256"]}, ("andre", rk)))
            return self.template_view(t)

    # ------------------------------------------------------------------------------------------------ email queue

    def _queue_room(self, caller: str) -> None:
        queued = sum(1 for x in self.messages.values() if x["status"] == "queued" and x["queued_by"] == caller)
        if queued >= self.settings.queue_max_per_caller:          # sales-py S2: a caller cannot flood the log
            raise Throttled(R("QUEUE_FULL"))

    def queue_email(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("outreach_email", body["influencer_id"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.message_view(self.messages[prev[1]])
            s = self.settings
            if not (s.outreach_domain and s.postal_address):
                raise Forbidden(R("OUTREACH_NOT_CONFIGURED"))
            inf = self._get(self.influencers, body["influencer_id"], "INFLUENCER_NOT_FOUND")
            if not inf.get("email_hash"):
                raise Invalid(R("INFLUENCER_NO_EMAIL"))
            problem = self._outreach_problem(inf)
            if problem:
                raise Forbidden(R(problem))
            t, v = self._template_version(body["template_id"], body["version"])
            problem = self._template_usable(t, v) or i05_templates.merge_problem(
                {"subject": v["subject"], "body": v["body"]}, self._fields(inf))
            if problem:
                raise Forbidden(R(problem))
            self._queue_room(caller)
            mid = derived_id("msg", caller, rk)
            data = {"message_id": mid, "channel": "email", "influencer_id": inf["influencer_id"],
                    "to_hash": inf["email_hash"], "brand": t["brand"], "template_id": t["template_id"],
                    "version": body["version"], "template_sha256": v["approved_sha256"],
                    "from_domain": s.outreach_domain}
            data["rendered_sha256"] = i05_templates.rendered_sha256(self._render_email(data, t, v, inf))
            self._commit("message_queued", self._req(data, caller, rk, body, mid), caller)
            return self.message_view(self.messages[mid])

    def cancel_message(self, caller: str, message_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("message_cancel", message_id, body)
            if self._idem(caller, rk, body):
                return self.message_view(self.messages[message_id])
            msg = self._get(self.messages, message_id, "MESSAGE_NOT_FOUND")
            if msg["status"] != "queued":
                raise Conflict(R("MESSAGE_NOT_QUEUED"))
            self._commit("message_cancelled", self._req({"message_id": message_id, "reason": "CANCELLED_BY_CALLER"},
                                                        caller, rk, body, message_id), caller)
            return self.message_view(msg)

    # ------------------------------------------------------------------------------------------------ DMs

    def _dm_handle(self, inf: dict, platform: str) -> dict:
        h = next((h for h in inf["handles"] if h["platform"] == platform), None)
        if h is None:
            raise Invalid(R("INFLUENCER_NO_HANDLE"))
        return h

    def draft_view(self, dr: dict) -> dict:
        return dict(dr)

    def drafts_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.draft_view(x) for x in self.dm_drafts.values()
                    if status is None or x["status"] == status][:1000]

    def draft_dm(self, caller: str, body: dict) -> dict:
        """The agent drafts; nothing is sent until Andre approves THIS text by its hash (Andre, Oct 6 2026)."""
        with self.lock:
            self._gate()
            rk = self._rk("dm_draft", body["influencer_id"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.draft_view(self.dm_drafts[prev[1]])
            inf = self._get(self.influencers, body["influencer_id"], "INFLUENCER_NOT_FOUND")
            problem = self._outreach_problem(inf) or i10_dm_guard.problem(body["text"])
            if problem:
                raise (Forbidden if problem in ("INFLUENCER_BLOCKED", "SUPPRESSED", "REPLY_HOLD") else Invalid)(
                    R(problem))
            h = self._dm_handle(inf, body["platform"])
            did = derived_id("dmd", caller, rk)
            sha = i10_dm_guard.content_sha256(inf["influencer_id"], body["platform"], h["handle_hash"], body["brand"],
                                              body["text"])
            self._commit("dm_drafted", self._req({"draft_id": did, "influencer_id": inf["influencer_id"],
                                                  "platform": body["platform"], "handle_hash": h["handle_hash"],
                                                  "brand": body["brand"], "text": body["text"],
                                                  "content_sha256": sha}, caller, rk, body, did), caller)
            return self.draft_view(self.dm_drafts[did])

    def _draft_problem(self, dr: dict) -> Optional[str]:
        inf = self.influencers.get(dr["influencer_id"])
        if inf is None:
            return "INFLUENCER_NOT_FOUND"
        problem = self._outreach_problem(inf) or i10_dm_guard.problem(dr["text"])
        if problem:
            return problem
        h = next((h for h in inf["handles"] if h["platform"] == dr["platform"]), None)
        if h is None or h["handle_hash"] != dr["handle_hash"]:
            return "INFLUENCER_NO_HANDLE"
        if i10_dm_guard.content_sha256(dr["influencer_id"], dr["platform"], dr["handle_hash"], dr["brand"],
                                       dr["text"]) != dr["content_sha256"]:
            return "CONTENT_HASH_MISMATCH"
        return None

    def approve_dm(self, draft_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("dm_approve", draft_id, body)
            if self._idem("andre", rk, body):
                return self.draft_view(self.dm_drafts[draft_id])
            dr = self._get(self.dm_drafts, draft_id, "DRAFT_NOT_FOUND")
            if dr["status"] != "draft":
                raise Conflict(R("DRAFT_NOT_PENDING"))
            if body["content_sha256"] != dr["content_sha256"]:
                raise Conflict(R("CONTENT_HASH_MISMATCH"))
            problem = self._draft_problem(dr)
            if problem:
                raise (Conflict if problem == "CONTENT_HASH_MISMATCH" else Forbidden)(R(problem))
            mid = derived_id("msg", "andre", rk)
            self._commit("dm_approved", self._req({"draft_id": draft_id, "message_id": mid}, "andre", rk, body,
                                                  draft_id), "andre",
                         evidence=("dm_approved", f"draft:{draft_id}",
                                   {"draft_id": draft_id, "message_id": mid, "influencer_id": dr["influencer_id"],
                                    "platform": dr["platform"], "handle_hash": dr["handle_hash"],
                                    "content_sha256": dr["content_sha256"]}, ("andre", rk)))
            return self.draft_view(dr)

    def reject_dm(self, draft_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("dm_reject", draft_id, body)
            if self._idem("andre", rk, body):
                return self.draft_view(self.dm_drafts[draft_id])
            dr = self._get(self.dm_drafts, draft_id, "DRAFT_NOT_FOUND")
            if dr["status"] != "draft":
                raise Conflict(R("DRAFT_NOT_PENDING"))
            self._commit("dm_rejected", self._req({"draft_id": draft_id}, "andre", rk, body, draft_id), "andre")
            return self.draft_view(dr)

    # ------------------------------------------------------------------------------------------------ send tick

    def _send_problem(self, msg: dict) -> tuple[Optional[str], Optional[dict]]:
        """(cancel_code, payload) for one queued message, from current state."""
        if msg.get("purpose") == "confirmation":
            return self._confirmation_send_problem(msg)
        inf = self.influencers.get(msg["influencer_id"])
        if inf is None:
            return "INFLUENCER_NOT_FOUND", None
        if msg["to_hash"] in self.suppression:
            return "SUPPRESSED", None
        problem = self._outreach_problem(inf)
        if problem:
            return problem, None
        if msg["channel"] == "dm":
            dr = self.dm_drafts.get(msg["draft_id"])
            if dr is None or dr["status"] != "approved" or dr["content_sha256"] != msg["content_sha256"]:
                return "CONTENT_HASH_MISMATCH", None
            problem = self._draft_problem(dr)
            if problem:
                return problem, None
            h = self._dm_handle(inf, dr["platform"])
            return None, {"platform": dr["platform"], "handle": h["handle"], "text": dr["text"]}
        t = self.templates.get(msg["template_id"])
        v = t["versions"].get(str(msg["version"])) if t else None
        if t is None or v is None:
            return "TEMPLATE_NOT_FOUND", None
        problem = self._template_usable(t, v)
        if problem or v["approved_sha256"] != msg["template_sha256"]:
            return problem or "TEMPLATE_HASH_MISMATCH", None
        if msg["from_domain"] != self.settings.outreach_domain or not self.settings.postal_address:
            return "OUTREACH_DOMAIN_CHANGED", None
        if inf.get("email_hash") != msg["to_hash"]:
            return "ADDRESS_CHANGED", None
        merge = i05_templates.merge_problem({"subject": v["subject"], "body": v["body"]}, self._fields(inf))
        if merge:
            return merge, None
        rendered = self._render_email(msg, t, v, inf)
        if i05_templates.rendered_sha256(rendered) != msg["rendered_sha256"]:
            return "RENDERED_CONTENT_CHANGED", None
        return None, rendered

    def send_tick(self, body: dict) -> dict:
        """The ``send-queue`` job. One message at a time: under the lock the rules are re-checked and the send is
        recorded (typed ``outreach_send`` event, then the log line); the provider is called outside the lock; the
        outcome is recorded after. A message whose outcome could not be recorded stays ``sending`` (never resent on
        its own: a duplicate message to a creator is worse than a missing one)."""
        summary = {"sent": 0, "failed": 0, "cancelled": 0, "capped": 0, "not_wired": 0, "stopped": None}
        with self.lock:
            self._gate()
            rk = self._rk("job", "send-queue", body)
            prev = self._idem("scheduler", rk, body)
            if prev:
                return {"job": "send-queue", "already_ran": True, **(prev[1] or {})}
            ids = [m["message_id"] for m in sorted((m for m in self.messages.values() if m["status"] == "queued"),
                                                   key=lambda m: (m["queued_at"], m["message_id"]))]
        for mid in ids:
            with self.lock:
                if self._closed:
                    summary["stopped"] = "SERVICE_CLOSED"
                    break
                msg = self.messages[mid]
                if msg["status"] != "queued":
                    continue
                cancel, payload = self._send_problem(msg)
                if cancel:
                    try:
                        self._commit("message_cancelled", {"message_id": mid, "reason": cancel}, "scheduler")
                        summary["cancelled"] += 1
                    except Unavailable:
                        summary["stopped"] = "LEDGER_UNAVAILABLE"
                        break
                    continue
                today = self.today()
                if msg["channel"] == "email" and not i06_send_cap.may_send(
                        self._stats(today, msg["from_domain"])["sent"], self.settings.daily_send_cap):
                    summary["capped"] += 1
                    continue
                port = self.ports.email if msg["channel"] == "email" else self.ports.dm
                if not port.wired:
                    summary["not_wired"] += 1           # stays queued, visibly; nothing is recorded as sent
                    continue
                inf = self.influencers[msg["influencer_id"]]
                try:
                    self._commit("message_sending", {"message_id": mid, "date": today}, "scheduler",
                                 evidence=("outreach_send", f"msg:{mid}",
                                           {"message_id": mid, "channel": msg["channel"], "to_hash": msg["to_hash"],
                                            "influencer_id": msg["influencer_id"], "brand": msg["brand"],
                                            "template_sha256": msg.get("template_sha256"),
                                            "content_sha256": msg.get("content_sha256"),
                                            "rendered_sha256": msg.get("rendered_sha256")}, (mid,)))
                except Unavailable:
                    summary["stopped"] = "LEDGER_UNAVAILABLE"
                    break
            try:
                if msg.get("purpose") == "confirmation":
                    result = port.send(mid, self.confirmations[msg["confirmation_id"]]["email"], payload)
                elif msg["channel"] == "email":
                    result = port.send(mid, inf["email"], payload)
                else:
                    result = port.send(mid, payload["platform"], payload["handle"], payload["text"])
            except Exception:  # noqa: BLE001 - a provider adapter must never take the tick down; the send is "failed"
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
                raise Unavailable(R(summary["stopped"]), **{k: v for k, v in summary.items() if k != "stopped"})
            self._commit("job_ran", self._req({"job": "send-queue"}, "scheduler", rk, body, summary), "scheduler")
        return {"job": "send-queue", **summary}

    # ------------------------------------------------------------------------------------------------ provider events

    def email_event(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("email_event", body["message_id"], body)
            if self._idem(caller, rk, body):
                return self.message_view(self.messages[body["message_id"]])
            msg = self._get(self.messages, body["message_id"], "MESSAGE_NOT_FOUND")
            if msg["channel"] != "email":
                raise Invalid(R("INVALID"), field="message_id")
            if msg["status"] not in ("sending", "sent", "failed"):
                raise Conflict(R("MESSAGE_NOT_SENT"))
            inf = self.influencers.get(msg["influencer_id"]) or {}
            if body["event"] == "complaint":          # a spam complaint is an opt-out: everywhere (sales-py S1-M4)
                hashes = sorted(set(i04_suppression.hashes_of(inf)) | {msg["to_hash"]})
            else:
                hashes = [msg["to_hash"]] if body["event"] == "hard_bounce" else []
            data = {"message_id": msg["message_id"], "event": body["event"], "hashes": hashes,
                    "reason": body["event"] if hashes else None}
            ev = self._suppression_evidence(hashes, body["event"], caller, rk) if hashes else None
            self._commit("message_event", self._req(data, caller, rk, body, msg["message_id"]), caller, evidence=ev)
            return self.message_view(msg)

    def reply(self, caller: str, body: dict) -> dict:
        """A reply on any channel. ANY reply holds every further automatic outreach to that influencer until Andre
        decides (no interpretation: "yes" and "interested" hold too); the one exception is an email whose raw body is
        exactly a fixed machine auto-reply. Opt-out wording also suppresses at once, everywhere. The text is never
        stored: only its SHA-256 and the label.

        A reply is NEVER refused for its sender fields (AEGIS R1-M4): an unknown message id, an address that cannot be
        read, ``Name <addr>`` (the address inside is used), a handle on an email reply or a handle that cannot be read
        are each ignored and whatever does resolve is used; when nothing resolves the reply is still recorded, as a
        review entry for Andre (an active hold with no target)."""
        with self.lock:
            self._gate()
            rk = self._rk("reply", "direct", body)
            prev = self._idem(caller, rk, body)
            if prev:
                return dict(prev[1])
            ignored = []
            msg = self.messages.get(body.get("message_id") or "")
            if body.get("message_id") is not None and msg is None:
                ignored.append("message_id")
            email_h = handle_h = None
            if body.get("from_email") is not None:
                addr = i02_identity.sender_email(body["from_email"])
                if addr:
                    email_h = i02_identity.email_hash(self.pii_key, addr)
                else:
                    ignored.append("from_email")
            if body.get("from_handle") is not None:
                canon = i02_identity.handle(body["channel"], body["from_handle"]) if body["channel"] != "email" \
                    else None
                if canon:
                    handle_h = i02_identity.handle_hash(self.pii_key, body["channel"], canon)
                else:
                    ignored.append("from_handle")
            iid = (msg["influencer_id"] if msg else None) or (self.email_index.get(email_h) if email_h else None) \
                or (self.handle_index.get(handle_h) if handle_h else None)
            inf = self.influencers.get(iid) if iid else None
            cls = i07_replies.classify(body["text"], body["channel"])
            auto = body["channel"] == "email" and i07_replies.exact_auto_reply(body["text"])
            if auto:
                cls = "out_of_office"
            reply_id = derived_id("rpl", caller, rk)
            data = {"reply_id": reply_id, "channel": body["channel"], "message_id": msg["message_id"] if msg else None,
                    "influencer_id": iid, "class": cls, "ignored": ignored,
                    "text_sha256": hashlib.sha256(body["text"].encode("utf-8", "surrogatepass")).hexdigest()}
            named = {h for h in (email_h, handle_h, msg["to_hash"] if msg else None) if h}
            if inf is not None:
                named |= set(i04_suppression.hashes_of(inf))
            evidence = []
            if cls == "unsubscribe" and named:
                data.update(hashes=sorted(named), reason="opt_out_reply")
                evidence.append(self._suppression_evidence(named, "opt_out_reply", caller, rk))
            # nothing resolved: still a hold (a review entry for Andre), even for an exact auto-reply
            if not auto or not named:
                hold_id = derived_id("hld", reply_id)
                data["hold"] = {"hold_id": hold_id, "influencer_id": iid, "hashes": sorted(named),
                                "reply_id": reply_id, "class": cls, "unresolved": not named}
                evidence.append(("outreach_hold_applied", f"hold:{hold_id}",
                                 {"hold_id": hold_id, "influencer_id": iid, "hashes": sorted(named),
                                  "reply_id": reply_id, "class": cls, "unresolved": not named}, (caller, rk)))
            answer = {"reply_id": reply_id, "class": cls, "influencer_id": iid,
                      "suppressed": bool(data.get("hashes")), "held": bool(data.get("hold")),
                      "hold_id": (data.get("hold") or {}).get("hold_id"), "ignored": ignored}
            self._commit("reply_received", self._req(data, caller, rk, body, answer), caller,
                         evidence=evidence or None)
            return answer

    def holds_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [dict(h) for h in self.holds.values() if status is None or h["status"] == status][:1000]

    def decide_hold(self, hold_id: str, body: dict) -> dict:
        """Andre's decision on a held influencer. ``continue`` lifts the hold and nothing else (a suppression stays
        exactly as it is); ``opt_out`` makes it a permanent suppression of every address and handle it names."""
        with self.lock:
            self._gate()
            rk = self._rk("hold_decision", hold_id, body)
            if self._idem("andre", rk, body):
                return dict(self.holds[hold_id])
            h = self._get(self.holds, hold_id, "HOLD_NOT_FOUND")
            if h["status"] != "active":
                raise Conflict(R("HOLD_NOT_ACTIVE"))
            data = {"hold_id": hold_id, "decision": body["decision"], "hashes": h["hashes"]}
            ev = [("hold_decided", f"hold:{hold_id}", {"hold_id": hold_id, "decision": body["decision"]},
                   ("andre", rk))]
            if body["decision"] == "opt_out" and h["hashes"]:
                ev.append(self._suppression_evidence(h["hashes"], "hold_opt_out", "andre", rk))
            self._commit("hold_decided", self._req(data, "andre", rk, body, hold_id), "andre", evidence=ev)
            return dict(h)
