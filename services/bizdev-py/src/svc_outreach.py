"""Outreach to prospective partners and pursuit contacts: email only (ADR 0016 decisions 20-21). A mixin of
BizDevService. The CAN-SPAM rules, the shared suppression list and the template / merge guards are sales-py's
(svc_outreach.py, i05 / i08 / i10 as fixed in its AEGIS rounds 1-4), cut down to email.

- Every message comes from an Andre-approved template version, matched by its exact content hash (the request names
  the hash; it must equal the version's stored hash, Andre's approved hash and the hash recomputed now).
- The suppression list is keyed by the HMAC of the address and shared across both brands; it is append-only. Opt-out
  wording, the one-click unsubscribe link, hard bounces and complaints all land on it.
- ANY reply holds further automatic outreach to that contact (both brands) until Andre decides: ``resume`` lifts the
  hold and nothing else; ``opt_out`` suppresses permanently. Opt-out wording suppresses at once as well. A reply that
  cannot be tied to a contact always reaches Andre; addresses written in its body are held, never suppressed.
- No texts and no calls: there is no phone field, channel or port in this service.
The rules are checked when a message is queued and again, from current state, immediately before the port is called.
A send is recorded on the ledger (``outreach_send``) and in the log BEFORE the port is called."""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

from errors import Conflict, Forbidden, Invalid, NotFound, Throttled, Unavailable
from intelligences import i02_identity, i04_assembly, i08_templates, i09_replies, i10_suppression
from ledger import derived_id
from reasons import R

BODY_STRANGERS_MAX = 5           # addresses in a reply body that are NOT contacts, held at most this many
BODY_CONTACTS_MAX = 10           # contacts named in one reply body that get their own hold (AEGIS round 5 M1)
TASK_HOLDS_MAX = 50              # holds linked to one review task; past it a numbered part is opened (round 5)


class OutreachMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_contact_created(self, d, at):
        self.contacts[d["contact_id"]] = {**{k: d[k] for k in ("contact_id", "brand", "email", "email_hash", "name",
                                                               "partner_id", "pursuit_id")},
                                          "first_name": None, "company": None, "created_at": at,
                                          "created_by": d["actor"]}
        self.contact_by_hash[d["email_hash"]] = d["contact_id"]

    def _a_merge_fields_verified(self, d, at):
        c = self.contacts[d["contact_id"]]
        for k in ("first_name", "company"):
            if k in d:
                c[k] = d[k]

    def _a_template_created(self, d, at):
        self.templates[d["template_id"]] = {"template_id": d["template_id"], "template_key": d["template_key"],
                                            "brand": d["brand"], "versions": {}, "created_at": at}
        self._a_template_version_added(d, at)

    def _a_template_version_added(self, d, at):
        self.templates[d["template_id"]]["versions"][str(d["version"])] = {
            "version": d["version"], "subject": d["subject"], "body": d["body"], "content_sha256": d["content_sha256"],
            "status": "draft", "approved_sha256": None, "approved_at": None, "created_at": at}

    def _a_template_approved(self, d, at):
        v = self.templates[d["template_id"]]["versions"][str(d["version"])]
        v.update(status="approved", approved_sha256=d["content_sha256"], approved_at=at)

    def _a_message_queued(self, d, at):
        self.messages[d["message_id"]] = {**{k: d[k] for k in (
            "message_id", "contact_id", "to_hash", "brand", "template_id", "version", "template_sha256",
            "rendered_sha256", "from_domain")}, "status": "queued", "reason": None, "queued_at": at, "updated_at": at,
            "sent_on": None, "provider_ref": None, "events": [], "queued_by": d["actor"]}
        self.msgs_by_key.setdefault(d["to_hash"], set()).add(d["message_id"])       # AEGIS round 4: no full scans
        self.msgs_by_key.setdefault(d["contact_id"], set()).add(d["message_id"])

    def _a_message_sending(self, d, at):
        self.messages[d["message_id"]].update(status="sending", sent_on=d["date"], updated_at=at)
        self.sent_per_day[d["date"]] = self.sent_per_day.get(d["date"], 0) + 1

    def _a_message_result(self, d, at):
        self.messages[d["message_id"]].update(status=d["status"], provider_ref=d.get("provider_ref"), updated_at=at,
                                              reason=None if d["status"] == "sent" else "PROVIDER_FAILED")

    def _a_message_cancelled(self, d, at):
        self.messages[d["message_id"]].update(status="cancelled", reason=d["reason"], updated_at=at)

    def _a_message_event(self, d, at):
        m = self.messages[d["message_id"]]
        m["events"] = (m["events"] + [{"event": d["event"], "at": at}])[-20:]
        if d.get("hashes"):
            self._suppress_apply(d["hashes"], d["event"], d["actor"], at)

    def _a_suppression_added(self, d, at):
        self._suppress_apply(d["hashes"], d["reason"], d["actor"], at)

    def _suppress_apply(self, hashes, reason, actor, at):
        for h in hashes:
            if h not in self.suppression:            # append-only: the first entry stands, nothing is ever removed
                self.suppression[h] = {"hash": h, "reason": reason, "at": at, "by": actor}
        for h in hashes:
            for mid in self.msgs_by_key.get(h, ()):
                m = self.messages[mid]
                if m["status"] == "queued" and m["to_hash"] == h:
                    m.update(status="cancelled", reason="SUPPRESSED", updated_at=at)

    def _a_reply_received(self, d, at):
        if d.get("hashes"):
            self._suppress_apply(d["hashes"], d["reason"], d["actor"], at)
        for h in d.get("holds") or ([d["hold"]] if d.get("hold") else []):
            self.holds[h["hold_id"]] = {"kind": "sender", "suppress_hashes": h.get("hashes", []), **h,
                                        "status": "active", "at": at, "decided_at": None, "decision": None}
            for x in h["hashes"]:                     # active holds indexed by address hash and by contact
                self.hold_by_hash.setdefault(x, set()).add(h["hold_id"])
            for c in h["contact_ids"]:
                self.hold_by_contact.setdefault(c, set()).add(h["hold_id"])
            self._cancel_held(h, at)
            self._link_task(h)

    def _link_task(self, h: dict) -> None:
        t = self.tasks.get(h["task_id"])
        if t is not None:
            ids = t.setdefault("hold_ids", [])
            if h["hold_id"] not in ids:
                ids.append(h["hold_id"])

    def _cancel_held(self, h: dict, at: str) -> None:
        for key in (*h["contact_ids"], *h["hashes"]):
            for mid in self.msgs_by_key.get(key, ()):
                m = self.messages[mid]
                if m["status"] == "queued":
                    m.update(status="cancelled", reason="CONTACT_HELD", updated_at=at)

    def _a_holds_decided(self, d, at):
        """AEGIS round 4: the decision covers EXACTLY the holds Andre named. A review task closes only when every hold
        linked to it is decided; a task with no linked hold is never closed automatically."""
        tasks = set()
        for hid in d["hold_ids"]:
            self._a_hold_decided({**d, "hold_id": hid}, at, close_task=False)
            tasks.add(self.holds[hid]["task_id"])
        for tid in sorted(tasks):
            self._close_task_if_decided(tid, at, d["decision"])

    def _close_task_if_decided(self, tid: str, at: str, outcome: str) -> None:
        t = self.tasks.get(tid)
        ids = (t or {}).get("hold_ids") or []
        if t is not None and t["status"] == "open" and ids and \
                all(self.holds[x]["status"] != "active" for x in ids if x in self.holds):
            t.update(status="closed", closed_at=at, outcome=outcome)

    def _a_hold_decided(self, d, at, close_task: bool = True):
        h = self.holds[d["hold_id"]]
        h.update(status="lifted" if d["decision"] == "resume" else "opted_out", decided_at=at, decision=d["decision"])
        for x in h["hashes"]:
            self.hold_by_hash.get(x, set()).discard(h["hold_id"])
        for c in h["contact_ids"]:
            self.hold_by_contact.get(c, set()).discard(h["hold_id"])
        if d["decision"] == "opt_out":                 # only what this hold may suppress (round 4: never body names)
            self._suppress_apply(h.get("suppress_hashes", h["hashes"]), "andre_opt_out", d["actor"], at)
        if close_task:
            self._close_task_if_decided(h["task_id"], at, d["decision"])

    # ------------------------------------------------------------------------------------------------ checks

    def _held(self, contact: Optional[dict], email_hash: Optional[str] = None) -> bool:
        return bool(self._holds_of(contact, email_hash))

    def _holds_of(self, contact: Optional[dict], *hashes) -> set:
        """Active holds covering a contact or any of the hashes (index lookups, AEGIS round 2 L2)."""
        out = set(self.hold_by_contact.get(contact["contact_id"], ())) if contact else set()
        for x in {*(h for h in hashes if h), *([contact["email_hash"]] if contact and contact.get("email_hash")
                                               else [])}:
            out |= self.hold_by_hash.get(x, set())
        return out

    def _suppressed(self, contact: dict, *more) -> bool:
        return any(i10_suppression.suppressed(self.suppression, h) for h in (contact.get("email_hash"), *more) if h)

    def _template_version(self, template_id: str, version: int) -> tuple[dict, dict]:
        t = self._get(self.templates, template_id, "TEMPLATE_NOT_FOUND")
        v = t["versions"].get(str(version))
        if v is None:
            raise NotFound(R("TEMPLATE_NOT_FOUND"))
        return t, v

    @staticmethod
    def _template_problem(t: dict, v: dict, named_sha: Optional[str] = None) -> Optional[str]:
        """None when this exact content is what Andre approved (and, when given, what the caller named)."""
        if v["status"] != "approved" or not v["approved_sha256"]:
            return "TEMPLATE_NOT_APPROVED"
        current = i08_templates.content_sha256(t["brand"], "email", v["subject"], v["body"])
        if current != v["approved_sha256"] or v["content_sha256"] != v["approved_sha256"] or \
                (named_sha is not None and named_sha != v["approved_sha256"]):
            return "TEMPLATE_HASH_MISMATCH"
        if i08_templates.deceptive_subject(v["subject"]):
            return "SUBJECT_DECEPTIVE"
        return None

    @staticmethod
    def _fields(contact: dict) -> dict:
        # sales-py S2-H1: only values a person verified at the console, never what a form or an agent typed
        return {"first_name": contact.get("first_name"), "company": contact.get("company")}

    def unsubscribe_token(self, message_id: str) -> str:
        mac = hmac.new(self.pii_key, f"unsubscribe\x00{message_id}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{message_id}.{mac}"

    def _render(self, m: dict, t: dict, v: dict, contact: dict) -> dict:
        s = self.settings
        url = f"https://{m['from_domain']}/u/{self.unsubscribe_token(m['message_id'])}"
        return i08_templates.render_email({"brand": t["brand"], "subject": v["subject"], "body": v["body"]},
                                          self._fields(contact), s.from_local, m["from_domain"], s.postal_address, url)

    # ------------------------------------------------------------------------------------------------ contacts

    def contact_view(self, c: dict) -> dict:
        out = {k: v for k, v in c.items() if k != "email"}
        out["suppressed"] = self._suppressed(c)
        out["held"] = self._held(c)
        return out

    def contact(self, cid: str) -> dict:
        with self.lock:
            return self.contact_view(self._get(self.contacts, cid, "CONTACT_NOT_FOUND"))

    def create_contact(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            e = i02_identity.email(body["email"])
            if e is None:
                raise Invalid(R("EMAIL_INVALID"))
            eh = i02_identity.keyed(self.pii_key, "email", e)
            cid = derived_id("cnt", eh)
            rk = self.rk("contact_create", cid, body)
            if self._idem(caller, rk, body):
                return self.contact_view(self.contacts[cid])
            if cid in self.contacts:
                raise Conflict(R("CONTACT_EXISTS"))
            if body.get("partner_id") is not None:
                self._get(self.partners, body["partner_id"], "PARTNER_NOT_FOUND")
            if body.get("pursuit_id") is not None:
                self._get(self.pursuits, body["pursuit_id"], "PURSUIT_NOT_FOUND")
            data = {"contact_id": cid, "brand": body["brand"], "email": e, "email_hash": eh, "name": body["name"],
                    "partner_id": body.get("partner_id"), "pursuit_id": body.get("pursuit_id")}
            self._commit("contact_created", self._req(data, caller, rk, body, cid), caller,
                         evidence=("contact_created", f"contact:{cid}", {"contact_id": cid, "email_hash": eh},
                                   (caller, rk)))
            return self.contact_view(self.contacts[cid])

    def verify_merge_fields(self, cid: str, body: dict) -> dict:
        """The console (a person) sets the only values ``{{first_name}}`` / ``{{company}}`` ever render."""
        with self.lock:
            self._gate()
            rk = self.rk("merge_fields", cid, body)
            if self._idem("dashboard", rk, body):
                return self.contact_view(self.contacts[cid])
            c = self._get(self.contacts, cid, "CONTACT_NOT_FOUND")
            fields = {k: body[k] for k in ("first_name", "company") if body.get(k) is not None}
            if not fields or not all(i08_templates.merge_value_ok(v) and not i08_templates.urls(v)
                                     for v in fields.values()):
                raise Invalid(R("MERGE_FIELD_REFUSED"))
            data = {"contact_id": cid, **fields}
            self._commit("merge_fields_verified", self._req(data, "dashboard", rk, body, cid), "dashboard",
                         evidence=("merge_fields_verified", f"contact:{cid}",
                                   {"contact_id": cid, "fields": sorted(fields),
                                    "values_hash": i02_identity.keyed(self.pii_key, "merge",
                                                                      repr(sorted(fields.items())))},
                                   ("dashboard", rk)))
            return self.contact_view(c)

    # ------------------------------------------------------------------------------------------------ templates

    def _check_template(self, subject: str, body: str) -> None:
        if i08_templates.deceptive_subject(subject):
            raise Invalid(R("SUBJECT_DECEPTIVE"))
        if i08_templates.unknown_placeholders(subject, body):
            raise Invalid(R("PLACEHOLDER_UNKNOWN"))

    def template_view(self, t: dict) -> dict:
        return {**{k: t[k] for k in ("template_id", "template_key", "brand", "created_at")},
                "versions": [dict(v) for _, v in sorted(t["versions"].items(), key=lambda kv: int(kv[0]))]}

    def templates_view(self) -> list[dict]:
        with self.lock:
            return [self.template_view(t) for t in self.templates.values()][:2000]

    def template(self, tid: str) -> dict:
        with self.lock:
            return self.template_view(self._get(self.templates, tid, "TEMPLATE_NOT_FOUND"))

    def create_template(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            tid = derived_id("tpl", body["brand"], body["template_key"])
            rk = self.rk("template_create", tid, body)
            if self._idem(caller, rk, body):
                return self.template_view(self.templates[tid])
            if tid in self.templates:
                raise Conflict(R("TEMPLATE_EXISTS"))
            self._check_template(body["subject"], body["body"])
            sha = i08_templates.content_sha256(body["brand"], "email", body["subject"], body["body"])
            data = {"template_id": tid, "template_key": body["template_key"], "brand": body["brand"], "version": 1,
                    "subject": body["subject"], "body": body["body"], "content_sha256": sha}
            self._commit("template_created", self._req(data, caller, rk, body, tid), caller)
            return self.template_view(self.templates[tid])

    def add_template_version(self, caller: str, tid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("template_version", tid, body)
            if self._idem(caller, rk, body):
                return self.template_view(self.templates[tid])
            t = self._get(self.templates, tid, "TEMPLATE_NOT_FOUND")
            self._check_template(body["subject"], body["body"])
            v = max(int(x) for x in t["versions"]) + 1
            sha = i08_templates.content_sha256(t["brand"], "email", body["subject"], body["body"])
            data = {"template_id": tid, "version": v, "subject": body["subject"], "body": body["body"],
                    "content_sha256": sha}
            self._commit("template_version_added", self._req(data, caller, rk, body, v), caller)
            return self.template_view(t)

    def approve_template(self, tid: str, version: int, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("template_approve", f"{tid}.{version}", body)
            if self._idem("andre", rk, body):
                return self.template_view(self.templates[tid])
            t, v = self._template_version(tid, version)
            if v["status"] == "approved":
                raise Conflict(R("TEMPLATE_ALREADY_APPROVED"))
            current = i08_templates.content_sha256(t["brand"], "email", v["subject"], v["body"])
            if body["content_sha256"] != v["content_sha256"] or current != v["content_sha256"]:
                raise Conflict(R("TEMPLATE_HASH_MISMATCH"))
            self._check_template(v["subject"], v["body"])
            data = {"template_id": tid, "version": version, "content_sha256": current}
            self._commit("template_approved", self._req(data, "andre", rk, body, version), "andre",
                         evidence=("template_approved", f"template:{tid}", data, ("andre", rk)))
            return self.template_view(t)

    # ------------------------------------------------------------------------------------------------ queue

    def message_view(self, m: dict) -> dict:
        return dict(m)

    def messages_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.message_view(m) for m in sorted(self.messages.values(),
                                                         key=lambda x: (x["queued_at"], x["message_id"]))
                    if status is None or m["status"] == status][:2000]

    def queue_email(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("outreach_email", body["contact_id"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.message_view(self.messages[prev[1]])
            s = self.settings
            if not (s.outreach_domain and s.postal_address):
                raise Forbidden(R("OUTREACH_NOT_CONFIGURED"))
            c = self._get(self.contacts, body["contact_id"], "CONTACT_NOT_FOUND")
            t, v = self._template_version(body["template_id"], body["version"])
            if t["brand"] != c["brand"]:
                raise Forbidden(R("TEMPLATE_BRAND_MISMATCH"))
            problem = self._template_problem(t, v, body["content_sha256"]) or \
                i08_templates.merge_problem({"subject": v["subject"], "body": v["body"]}, self._fields(c))
            if problem:
                raise Forbidden(R(problem))
            if self._suppressed(c):
                raise Forbidden(R("SUPPRESSED"))
            if self._held(c):
                raise Forbidden(R("CONTACT_HELD"))
            if sum(1 for m in self.messages.values() if m["status"] == "queued" and m["queued_by"] == caller) \
                    >= s.queue_max_per_caller:
                raise Throttled(R("QUEUE_FULL"))
            mid = derived_id("msg", caller, rk)
            data = {"message_id": mid, "contact_id": c["contact_id"], "to_hash": c["email_hash"], "brand": t["brand"],
                    "template_id": t["template_id"], "version": body["version"], "template_sha256": v["approved_sha256"],
                    "from_domain": s.outreach_domain}
            data["rendered_sha256"] = i08_templates.rendered_sha256(self._render(data, t, v, c))
            self._commit("message_queued", self._req(data, caller, rk, body, mid), caller)
            return self.message_view(self.messages[mid])

    def cancel_message(self, caller: str, mid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("message_cancel", mid, body)
            if self._idem(caller, rk, body):
                return self.message_view(self.messages[mid])
            m = self._get(self.messages, mid, "MESSAGE_NOT_FOUND")
            if m["status"] != "queued":
                raise Conflict(R("MESSAGE_NOT_QUEUED"))
            self._commit("message_cancelled", self._req({"message_id": mid, "reason": "CANCELLED_BY_CALLER"}, caller,
                                                        rk, body, mid), caller)
            return self.message_view(m)

    def _send_problem(self, m: dict) -> tuple[Optional[str], Optional[dict]]:
        """(cancel_code, rendered) for one queued message, from current state."""
        c = self.contacts.get(m["contact_id"])
        if c is None or self._suppressed(c, m["to_hash"]):
            return "SUPPRESSED", None
        if self._held(c, m["to_hash"]):
            return "CONTACT_HELD", None
        t = self.templates.get(m["template_id"])
        v = t["versions"].get(str(m["version"])) if t else None
        if t is None or v is None:
            return "TEMPLATE_NOT_FOUND", None
        problem = self._template_problem(t, v, m["template_sha256"])
        if problem:
            return problem, None
        if m["from_domain"] != self.settings.outreach_domain or not self.settings.postal_address:
            return "OUTREACH_NOT_CONFIGURED", None
        merge = i08_templates.merge_problem({"subject": v["subject"], "body": v["body"]}, self._fields(c))
        if merge:
            return merge, None
        rendered = self._render(m, t, v, c)
        if i08_templates.rendered_sha256(rendered) != m["rendered_sha256"]:
            return "TEMPLATE_HASH_MISMATCH", None
        return None, rendered

    def send_tick(self) -> dict:
        """The ``send-queue`` job (sales-py's, email only): one message at a time; rules re-checked under the lock;
        the send recorded (``outreach_send`` event, then the log line) BEFORE the port is called outside the lock; the
        outcome recorded after. The daily cap counts by the injected clock's date, never the wall-clock hour."""
        summary = {"sent": 0, "failed": 0, "cancelled": 0, "capped": 0, "not_wired": 0}
        with self.lock:
            self._gate()
            ids = [m["message_id"] for m in sorted(self.messages.values(),
                                                   key=lambda x: (x["queued_at"], x["message_id"]))
                   if m["status"] == "queued"]
        for mid in ids:
            try:
                with self.lock:
                    self._gate()
                    m = self.messages[mid]
                    if m["status"] != "queued":
                        continue
                    cancel, rendered = self._send_problem(m)
                    if cancel:
                        self._commit("message_cancelled", {"message_id": mid, "reason": cancel}, "scheduler")
                        summary["cancelled"] += 1
                        continue
                    today = self.today()
                    if self.sent_per_day.get(today, 0) >= self.settings.daily_send_cap:
                        summary["capped"] += 1
                        continue
                    if not self.ports.email.wired:
                        summary["not_wired"] += 1
                        continue
                    to = self.contacts[m["contact_id"]]["email"]
                    self._commit("message_sending", {"message_id": mid, "date": today}, "scheduler",
                                 evidence=("outreach_send", f"msg:{mid}",
                                           {"message_id": mid, "to_hash": m["to_hash"], "brand": m["brand"],
                                            "template_sha256": m["template_sha256"],
                                            "rendered_sha256": m["rendered_sha256"]}, (mid,)))
                try:
                    res = self.ports.email.send(mid, to, rendered)          # outside the lock
                    ok, ref = res.status == "accepted", res.provider_ref
                except Exception:      # noqa: BLE001 - a provider adapter must never take the tick down
                    ok, ref = False, None
                with self.lock:
                    self._commit("message_result", {"message_id": mid, "status": "sent" if ok else "failed",
                                                    "provider_ref": ref if ok else None}, "scheduler")
                    summary["sent" if ok else "failed"] += 1
            except Unavailable:
                raise
            except Exception:      # noqa: BLE001 - AEGIS round 1 M3: one bad item never stalls the queue
                summary.setdefault("errors", []).append(mid)
        return summary

    # ------------------------------------------------------------------------------------------------ suppression

    def _suppress(self, actor: str, rk: str, body: dict, hashes, reason: str) -> None:
        hashes = sorted(set(hashes))
        self._commit("suppression_added", self._req({"hashes": hashes, "reason": reason}, actor, rk, body, hashes),
                     actor, evidence=("suppression_added",
                                      f"suppression:{hashlib.sha256(''.join(hashes).encode()).hexdigest()[:40]}",
                                      {"hashes": hashes, "reason": reason}, (actor, rk)))

    def suppress(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("suppress", body.get("contact_id") or "direct", body)
            prev = self._idem(caller, rk, body)
            if prev:
                return {"suppressed": prev[1]}
            hashes = []
            if body.get("contact_id") is not None:
                hashes.append(self._get(self.contacts, body["contact_id"], "CONTACT_NOT_FOUND")["email_hash"])
            if body.get("email") is not None:
                e = i02_identity.email(body["email"])
                if e is None:
                    raise Invalid(R("EMAIL_INVALID"))
                hashes.append(i02_identity.keyed(self.pii_key, "email", e))
            if not hashes:
                raise Invalid(R("INVALID"))
            self._suppress(caller, rk, body, hashes, "manual")
            return {"suppressed": sorted(set(hashes))}

    def unsubscribe(self, caller: str, body: dict) -> dict:
        """The one-click link (RFC 8058) relayed by the hub: the token names the message; its address and the
        contact's are suppressed for both brands at once."""
        with self.lock:
            self._gate()
            token = body["token"]
            mid = token.split(".", 1)[0]
            if mid not in self.messages or not hmac.compare_digest(self.unsubscribe_token(mid), token):
                raise NotFound(R("UNSUBSCRIBE_TOKEN_UNKNOWN"))
            m = self.messages[mid]
            rk = self.rk("unsubscribe", mid, body)
            if self._idem(caller, rk, body):
                return {"unsubscribed": True}
            c = self.contacts.get(m["contact_id"]) or {}
            hashes = {h for h in (m["to_hash"], c.get("email_hash")) if h}
            if all(h in self.suppression for h in hashes):
                return {"unsubscribed": True}
            self._suppress(caller, rk, body, hashes, "unsubscribe")
            return {"unsubscribed": True}

    def suppressions_view(self) -> list[dict]:
        with self.lock:
            return [dict(x) for x in self.suppression.values()][:20_000]

    def email_event(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("email_event", body["message_id"], body)
            if self._idem(caller, rk, body):
                return self.message_view(self.messages[body["message_id"]])
            m = self._get(self.messages, body["message_id"], "MESSAGE_NOT_FOUND")
            if m["status"] not in ("sending", "sent", "failed"):
                raise Conflict(R("MESSAGE_NOT_SENT"))
            hashes = []
            if body["event"] in ("hard_bounce", "complaint"):
                c = self.contacts.get(m["contact_id"]) or {}
                hashes = sorted({h for h in (m["to_hash"], c.get("email_hash")) if h})
            data = {"message_id": m["message_id"], "event": body["event"], "hashes": hashes}
            ev = ("suppression_added", f"msg:{m['message_id']}", {"hashes": hashes, "reason": body["event"]},
                  (caller, rk)) if hashes else None
            self._commit("message_event", self._req(data, caller, rk, body, m["message_id"]), caller, evidence=ev)
            return self.message_view(m)

    # ------------------------------------------------------------------------------------------------ replies, holds

    def reply(self, caller: str, body: dict) -> dict:
        """A reply is ALWAYS recorded, held and given to Andre (AEGIS round 1 H1): its sender fields never refuse it.

        AEGIS round 4: holds never merge. The reply's SENDER gets one hold — the contact resolved through the message
        (or the sender's address), the addresses that resolve to them, and the keyed raw form of a sender that does
        not parse. Each contact named in the body gets its OWN hold covering only that contact, and each other body
        address (at most BODY_STRANGERS_MAX) its own hold covering only that address. All of them are linked to the
        reply's review task (one per sender per day). An ``opt_out`` decision suppresses only what a hold may
        suppress: the sender hold its RESOLVED sender addresses (never the raw form, never a body address); a named
        hold only its own contact, by a decision on that hold."""
        with self.lock:
            self._gate()
            rk = self.rk("reply", body.get("message_id") or "direct", body)
            prev = self._idem(caller, rk, body)
            if prev:
                return dict(prev[1])
            m = self.messages.get(body["message_id"]) if body.get("message_id") is not None else None
            from_h = raw_h = None
            if body.get("from_email") is not None:
                e = i02_identity.email(body["from_email"])
                if e is not None:
                    from_h = i02_identity.keyed(self.pii_key, "email", e)
                else:
                    raw_h = i02_identity.keyed(self.pii_key, "email_raw", i02_identity.raw_address(body["from_email"]))
            cid = m["contact_id"] if m else self.contact_by_hash.get(from_h)
            c = self.contacts.get(cid) if cid else None
            cls = i09_replies.classify(body["text"], "email")
            reply_id = derived_id("rpl", caller, rk)
            data = {"reply_id": reply_id, "message_id": body.get("message_id"), "contact_id": cid, "class": cls,
                    "message_known": m is not None,
                    "text_sha256": hashlib.sha256(body["text"].encode("utf-8", "surrogatepass")).hexdigest()}
            sender = sorted({h for h in (from_h, m["to_hash"] if m else None, c.get("email_hash") if c else None)
                             if h})
            evidence = []
            if cls == "unsubscribe" and sender:
                data.update(hashes=sender, reason="stop_reply")
                evidence.append(("suppression_added", f"reply:{reply_id}",
                                 {"hashes": sender, "reason": "stop_reply"}, (caller, rk)))
            sender_key = f"contact:{cid}" if cid else (from_h or raw_h or f"reply:{reply_id}")
            resolved = c is not None or m is not None
            body_holds, truncated, strangers, named_n = [], 0, 0, 0
            for e in i02_identity.all_emails_in(body["text"]):     # body addresses: each its own hold, never merged
                eh = i02_identity.keyed(self.pii_key, "email", e)
                if eh in sender:
                    continue
                named = self.contact_by_hash.get(eh)
                # AEGIS round 5 M1: an unresolved outsider's body holds nobody; a resolved sender's holds at most
                # BODY_CONTACTS_MAX contacts and BODY_STRANGERS_MAX other addresses; the rest are counted
                if not resolved or (named and named_n >= BODY_CONTACTS_MAX) or \
                        (not named and strangers >= BODY_STRANGERS_MAX):
                    truncated += 1
                    continue
                if named:
                    named_n += 1
                else:
                    strangers += 1
                body_holds.append({"hold_id": derived_id("hld", reply_id, eh), "reply_id": reply_id, "kind": "named",
                                   "contact_ids": [named] if named else [], "hashes": [eh], "suppress_hashes": [eh]})
            task, existing = self._review_task(sender_key, cls, 1 + len(body_holds), reply_id)
            tid = task["task_id"]
            holds = [{"hold_id": derived_id("hld", reply_id), "reply_id": reply_id, "kind": "sender",
                      "contact_ids": [cid] if cid else [], "hashes": sorted(set(sender) | ({raw_h} if raw_h else set())),
                      "suppress_hashes": sender, "task_id": tid}] + [{**x, "task_id": tid} for x in body_holds]
            data["holds"] = holds
            data["task_note"] = {"task_id": tid, "body_addresses_truncated": truncated, "sender_resolved": resolved}
            # AEGIS round 5 M1: ONE evidence event per reply, listing its hold ids
            evidence.append(("reply_holds_applied", f"reply:{reply_id}",
                             {"reply_id": reply_id, "class": cls, "hold_ids": [x["hold_id"] for x in holds],
                              "sender_resolved": resolved, "body_addresses_truncated": truncated}, (caller, rk)))
            data["tasks"] = [] if existing is not None else [task]
            answer = {"reply_id": reply_id, "class": cls, "suppressed": bool(data.get("hashes")), "held": True,
                      "hold_id": holds[0]["hold_id"], "hold_ids": [x["hold_id"] for x in holds], "task_id": tid,
                      "sender_resolved": bool(sender), "body_addresses_truncated": truncated}
            self._commit("reply_received", self._req(data, caller, rk, body, answer), caller, evidence=evidence)
            return answer

    def _review_task(self, sender_key: str, cls: str, adding: int, reply_id: str) -> tuple[dict, Optional[dict]]:
        """The review task for this sender today (AEGIS round 2 L2): the first OPEN part with room for ``adding``
        more holds; past TASK_HOLDS_MAX a numbered part is opened (round 5). Returns (task, existing-or-None)."""
        today = self.today()
        for part in range(1, 100_000):
            ident = today if part == 1 else f"{today}|part{part}"
            task = self._task("review_reply", sender_key, ident, cls)
            task["part"] = part
            t = self.tasks.get(task["task_id"])
            if t is None:
                return task, None
            if t["status"] == "open" and len(t.get("hold_ids") or []) + adding <= TASK_HOLDS_MAX:
                return task, t
        task = self._task("review_reply", sender_key, f"{today}|{reply_id}", cls)
        return task, None

    # ------------------------------------------------------------------------------------------------ hold decisions

    @staticmethod
    def hold_sha256(h: dict) -> str:
        """A hold's identity and content (holds never change after they are made, only their status)."""
        return i04_assembly.sha({k: h.get(k) for k in ("hold_id", "reply_id", "kind", "contact_ids", "hashes",
                                                       "suppress_hashes", "task_id", "status")})

    @staticmethod
    def decision_sha256(decision: str, holds: list) -> str:
        """What Andre decides: exactly this set of holds, each as (hold_id, reply_id, hold_sha256), and the action.
        The dashboard computes it from GET /holds; README documents the formula."""
        return i04_assembly.sha({"decision": decision,
                                 "holds": sorted([x["hold_id"], x["reply_id"], x["hold_sha256"]] for x in holds)})

    def hold_view(self, h: dict) -> dict:
        out = dict(h)
        out["hold_sha256"] = self.hold_sha256(h)
        return out

    def holds_view(self, status: Optional[str], limit: int = 200, offset: int = 0) -> list[dict]:
        """Paginated BEFORE anything is computed (AEGIS round 4): a filter pass over the ids, one slice, then a
        constant-size view per hold in the page."""
        with self.lock:
            it = (h for h in self.holds.values() if status is None or h["status"] == status)
            page = []
            for i, h in enumerate(it):
                if i < offset:
                    continue
                if len(page) >= limit:
                    break
                page.append(h)
            return [self.hold_view(h) for h in page]

    def decide_holds(self, body: dict) -> dict:
        """Andre decides EXACTLY the holds he names (AEGIS round 4): each must exist, be active and unchanged, and
        carry the named reply; ``decision_sha256`` must be computed over exactly that set and the action. Holds made
        later are untouched (each has its own task), so a reply stream cannot starve a decision. A contact stays held
        while any active hold covers it."""
        named = body["holds"]
        if len({x["hold_id"] for x in named}) != len(named):           # AEGIS round 5: refused here, not only in the API
            raise Invalid(R("INVALID"), field="holds")
        target = i04_assembly.sha(sorted(x["hold_id"] for x in named))[:40]
        with self.lock:
            self._gate()
            rk = self.rk("holds_decide", target, body)
            if self._idem("andre", rk, body):
                return {"decided": sorted(x["hold_id"] for x in named), "decision": body["decision"]}
            current = []
            for x in named:
                h = self._get(self.holds, x["hold_id"], "HOLD_NOT_FOUND")
                if h["status"] != "active":
                    raise Conflict(R("HOLD_CLOSED"))
                if h["reply_id"] != x["reply_id"]:
                    raise Conflict(R("STATE_HASH_MISMATCH"))
                current.append({"hold_id": h["hold_id"], "reply_id": h["reply_id"], "hold_sha256": self.hold_sha256(h)})
            if body["decision_sha256"] != self.decision_sha256(body["decision"], current):
                raise Conflict(R("STATE_HASH_MISMATCH"))
            ids = sorted(x["hold_id"] for x in named)
            data = {"hold_ids": ids, "decision": body["decision"], "decision_sha256": body["decision_sha256"]}
            evidence = [("hold_decided", f"holds:{target}", data, ("andre", rk))]
            supp = sorted({k for i in ids for k in self.holds[i].get("suppress_hashes", self.holds[i]["hashes"])})
            if body["decision"] == "opt_out" and supp:
                evidence.append(("suppression_added", f"holds:{target}", {"hashes": supp, "reason": "andre_opt_out"},
                                 ("andre", rk)))
            out = {"decided": ids, "decision": body["decision"]}
            self._commit("holds_decided", self._req(data, "andre", rk, body, out), "andre", evidence=evidence)
            return out
