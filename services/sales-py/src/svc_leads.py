"""Leads and the pipeline: intake from the four sources, dedupe, scoring, routing, accounts, contacts,
opportunities, stages, owners, activities and tasks (ADR 0013 decisions 6-8 and 18). A mixin of SalesService."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from pydantic import ValidationError

import models as m
from clock import parse_iso
from errors import Conflict, Forbidden, Invalid, Unavailable
from intelligences import i01_intake, i02_identity, i03_scoring, i04_routing, i07_quiet_hours, i08_templates
from ledger import derived_id
from ports import NotWired
from reasons import R
from textguard import forbidden_keys

OPEN_LEAD = ("new", "nurture", "qualified")
MAX_ACTIVITIES = 200
MAX_EVIDENCE = 20


class LeadsMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_lead_created(self, d, at):
        if d.get("account"):
            a = {**d["account"], "created_at": at}
            self.accounts[a["account_id"]] = a
            if a.get("domain_hash"):
                self.domain_index.setdefault(a["domain_hash"], a["account_id"])
        if d.get("contact"):
            c = {**d["contact"], "created_at": at, "updated_at": at}
            self.contacts[c["contact_id"]] = c
            if c.get("email_hash"):
                self.email_index.setdefault(c["email_hash"], c["contact_id"])
            if c.get("phone_hash"):
                self.phone_index.setdefault(c["phone_hash"], c["contact_id"])
        lead = {k: d[k] for k in ("lead_id", "source", "brand", "product_lines", "queue", "score", "evidence",
                                  "signals", "referrer", "contact_id", "account_id")}
        lead.update(status=d["score"]["status"], owner=None, created_at=at, updated_at=at, opportunity_id=None,
                    disqualified_reason=None)
        self.leads[d["lead_id"]] = lead
        if d.get("task"):
            self._a_task_opened(d["task"], at)

    def _a_lead_import_ran(self, d, at):
        pass                                         # the request record (_apply) is the point: the import's answer

    def _a_lead_evidence_added(self, d, at):
        lead = self.leads[d["lead_id"]]
        lead["evidence"] = (lead["evidence"] + [d["evidence"]])[-MAX_EVIDENCE:]
        lead["score"] = d["score"]
        if lead["status"] in OPEN_LEAD:
            lead["status"] = d["score"]["status"]
        lead["updated_at"] = at

    def _a_lead_owner_set(self, d, at):
        lead = self.leads[d["lead_id"]]
        lead["owner"], lead["updated_at"] = d["owner"], at

    def _a_lead_disqualified(self, d, at):
        lead = self.leads[d["lead_id"]]
        lead.update(status="disqualified", disqualified_reason=d["reason_code"], updated_at=at)

    def _a_lead_converted(self, d, at):
        lead = self.leads[d["lead_id"]]
        lead.update(status="converted", opportunity_id=d["opportunity_id"], updated_at=at,
                    owner=d["owner"] or lead["owner"])
        self.opps[d["opportunity_id"]] = {
            "opportunity_id": d["opportunity_id"], "lead_id": d["lead_id"], "account_id": lead["account_id"],
            "contact_id": lead["contact_id"], "brand": lead["brand"], "product_lines": lead["product_lines"],
            "stage": "qualified", "owner": d["owner"] or lead["owner"], "created_at": at, "updated_at": at,
            "proposal_ids": []}

    def _a_leads_aged(self, d, at):
        for lid in d["lead_ids"]:
            self.leads[lid].update(status="stale", updated_at=at)

    def _a_account_display_name_verified(self, d, at):
        a = self.accounts[d["account_id"]]
        a["display_name"], a["display_name_verified_at"] = d["display_name"], at

    def _a_contact_first_name_verified(self, d, at):
        c = self.contacts[d["contact_id"]]
        c["verified_first_name"], c["updated_at"] = d["first_name"], at

    def _a_contact_time_zone_set(self, d, at):
        c = self.contacts[d["contact_id"]]
        c["time_zone"], c["updated_at"] = d["time_zone"], at

    def _a_opportunity_stage_set(self, d, at):
        o = self.opps[d["opportunity_id"]]
        o["stage"], o["updated_at"] = d["stage"], at

    def _a_activity_logged(self, d, at):
        self._activity(d["target"], {"activity_id": d["activity_id"], "kind": d["kind"], "note": d.get("note"),
                                     "by": d["actor"], "at": at})

    def _a_task_opened(self, d, at):
        self.tasks[d["task_id"]] = {"task_id": d["task_id"], "kind": d["kind"], "target": d["target"],
                                    "due_on": d.get("due_on"), "status": "open", "opened_at": at, "closed_at": None,
                                    "outcome": None, "hold_id": d.get("hold_id")}

    def _a_task_closed(self, d, at):
        t = self.tasks[d["task_id"]]
        t.update(status="closed", closed_at=at, outcome=d["outcome"])

    def _activity(self, target: str, entry: dict) -> None:
        lst = self.activities.setdefault(target, [])
        lst.append(entry)
        del lst[:-MAX_ACTIVITIES]

    # ------------------------------------------------------------------------------------------------ views

    def contact_view(self, c: dict) -> dict:
        consent = {f"{ch}:{b}": self._consent_active(c, ch, b) for ch in ("sms", "voice") for b in ("zbm", "zbc")}
        return {k: c.get(k) for k in ("contact_id", "account_id", "name", "email", "phone", "title", "time_zone",
                                      "verified_first_name")} | {
            "email_suppressed": self._is_suppressed("email", c), "phone_suppressed": self._is_suppressed("sms", c),
            "phone_held": self._held(c.get("phone_hash")),
            "consent": consent}

    def lead_view(self, lead: dict, duplicate: bool = False) -> dict:
        out = dict(lead)
        out["contact"] = {k: self.contacts[lead["contact_id"]].get(k) for k in ("name", "email", "phone", "title")}
        acc = self.accounts.get(lead["account_id"]) or {}
        out["account"] = {k: acc.get(k) for k in ("name", "domain", "industry")}
        if duplicate:
            out["duplicate"] = True
        return out

    def lead(self, lead_id: str) -> dict:
        with self.lock:
            return self.lead_view(self._get(self.leads, lead_id, "LEAD_NOT_FOUND"))

    def leads_view(self, status: Optional[str], brand: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.lead_view(x) for x in self.leads.values()
                    if (status is None or x["status"] == status) and (brand is None or x["brand"] == brand)][:1000]

    def contact(self, contact_id: str) -> dict:
        with self.lock:
            return self.contact_view(self._get(self.contacts, contact_id, "CONTACT_NOT_FOUND"))

    def opportunity(self, opp_id: str) -> dict:
        with self.lock:
            o = self._get(self.opps, opp_id, "OPPORTUNITY_NOT_FOUND")
            return {**o, "activities": list(self.activities.get(f"opportunity:{opp_id}", []))}

    def opportunities_view(self, stage: Optional[str]) -> list[dict]:
        with self.lock:
            return [dict(o) for o in self.opps.values() if stage is None or o["stage"] == stage][:1000]

    def tasks_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [dict(t) for t in self.tasks.values() if status is None or t["status"] == status][:1000]

    # ------------------------------------------------------------------------------------------------ intake

    def _normalise_contact(self, c: dict) -> dict:
        email = phone = None
        if c.get("email") is not None:
            email = i02_identity.email(c["email"])
            if email is None:
                raise Invalid(R("EMAIL_INVALID"))
        if c.get("phone") is not None:
            phone = i02_identity.phone(c["phone"])
            if phone is None:
                raise Invalid(R("PHONE_INVALID"))
        if email is None and phone is None:
            raise Invalid(R("CONTACT_CHANNEL_REQUIRED"))
        tz = c.get("time_zone")
        if tz is not None and i07_quiet_hours.zone(tz) is None:
            raise Invalid(R("TIME_ZONE_INVALID"))
        if not i07_quiet_hours.zone_fits_phone(tz, phone):
            raise Invalid(R("TIME_ZONE_PHONE_MISMATCH"))
        k = self.pii_key
        return {"name": c["name"], "email": email, "phone": phone, "title": c.get("title"), "time_zone": tz,
                "email_hash": i02_identity.keyed(k, "email", email) if email else None,
                "phone_hash": i02_identity.keyed(k, "phone", phone) if phone else None}

    def _normalise_account(self, a: Optional[dict], email: Optional[str]) -> tuple[dict, bool]:
        a = dict(a or {})
        dom = None
        if a.get("domain") is not None:
            dom = i02_identity.domain(a["domain"])
            if dom is None:
                raise Invalid(R("DOMAIN_INVALID"))
        company = i02_identity.company_domain(email, dom)
        a["domain"] = company
        a["domain_hash"] = i02_identity.keyed(self.pii_key, "domain", company) if company else None
        return a, company is not None

    def intake(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"lead_create|{body['source']}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return self.lead_view(self.leads[prev[1]])
            ev = body["evidence"]
            if not i01_intake.admissible(caller, body["source"], ev["kind"]):
                raise Forbidden(R("SOURCE_NOT_ALLOWED"))
            if i01_intake.needs_referrer(body["source"]) != (body.get("referrer") is not None):
                raise Invalid(R("REFERRER_REQUIRED") if body.get("referrer") is None else R("REFERRER_NOT_ALLOWED"))
            if ev.get("scan_findings") is not None and ev["kind"] != "rr_scan":
                raise Invalid(R("INVALID"), field="evidence.scan_findings")
            return self._ingest(caller, body["source"], body, rk, body)

    def _ingest(self, actor: str, source: str, lead_in: dict, rk: str, body: dict) -> dict:
        ev = dict(lead_in["evidence"])
        try:
            captured = parse_iso(ev["captured_at"])
        except ValueError:
            raise Invalid(R("INVALID"), field="evidence.captured_at") from None
        if captured > self.now() + timedelta(minutes=5):
            raise Invalid(R("INVALID"), field="evidence.captured_at")
        routed, problem = i04_routing.route(lead_in.get("brand"), list(lead_in.get("product_interest") or []),
                                            ev["kind"])
        if problem:
            raise Invalid(R(problem))
        contact = self._normalise_contact(lead_in["contact"])
        account, company = self._normalise_account(lead_in.get("account"), contact["email"])
        ev = {k: v for k, v in ev.items() if v is not None}
        signals = {**{k: v for k, v in (lead_in.get("signals") or {}).items() if v is not None},
                   **{k: account.get(k) for k in ("industry", "employees_band", "revenue_band") if account.get(k)}}
        existing_cid = (self.email_index.get(contact["email_hash"]) if contact["email_hash"] else None) or \
                       (self.phone_index.get(contact["phone_hash"]) if contact["phone_hash"] else None)
        if existing_cid:
            open_lead = next((x for x in self.leads.values() if x["contact_id"] == existing_cid
                              and x["status"] in OPEN_LEAD and x["brand"] in (routed["brand"], None)), None)
            if open_lead is not None:
                evidence = (open_lead["evidence"] + [ev])[-MAX_EVIDENCE:]
                score = i03_scoring.score(open_lead["source"], open_lead["brand"] or "", {**open_lead["signals"],
                                                                                         **signals}, company, evidence)
                self._commit("lead_evidence_added", self._req({"lead_id": open_lead["lead_id"], "evidence": ev,
                                                               "score": score, "source": source}, actor, rk, body,
                                                              open_lead["lead_id"]), actor)
                return self.lead_view(self.leads[open_lead["lead_id"]], duplicate=True)
        lead_id = derived_id("led", actor, rk)
        new_contact = None
        if existing_cid:
            contact_id = existing_cid
            account_id = self.contacts[existing_cid]["account_id"]
            new_account = None
        else:
            account_id = self.domain_index.get(account["domain_hash"]) if account["domain_hash"] else None
            new_account = None
            if account_id is None:
                account_id = derived_id("acc", account["domain_hash"] or lead_id)
                new_account = {"account_id": account_id, "name": account.get("name") or account["domain"]
                               or contact["name"], "domain": account["domain"], "domain_hash": account["domain_hash"],
                               "industry": account.get("industry"), "employees_band": account.get("employees_band"),
                               "revenue_band": account.get("revenue_band")}
            contact_id = derived_id("con", contact["email_hash"] or contact["phone_hash"])
            new_contact = {"contact_id": contact_id, "account_id": account_id, **contact}
        score = i03_scoring.score(source, routed["brand"] or "", signals, company, [ev])
        data = {"lead_id": lead_id, "source": source, "brand": routed["brand"], "product_lines": routed["product_lines"],
                "queue": routed["queue"], "score": score, "evidence": [ev], "signals": signals,
                "referrer": lead_in.get("referrer"), "contact_id": contact_id, "account_id": account_id,
                "account": new_account, "contact": new_contact}
        if routed["queue"] == "unrouted":
            data["task"] = {"task_id": derived_id("tsk", "route", lead_id), "kind": "route_lead",
                            "target": f"lead:{lead_id}", "due_on": self.today()}
        self._commit("lead_created", self._req(data, actor, rk, body, lead_id), actor)
        return self.lead_view(self.leads[lead_id])

    def import_leads(self, caller: str, body: dict) -> dict:
        """Public-data and paid-provider leads come only through their ports (not built: 503 SOURCE_NOT_WIRED).

        Sweep A: the import's answer is committed under its request key (``lead_import_ran``), so the same body again
        answers what the first call did (``already_ran``) and the same request_id with another body is 409
        REQUEST_ID_REUSED. Before, nothing was ever recorded under the import's own key: a reuse with another body
        ran a second, different import. A retry after a failure part-way skips the items already committed (their
        own keys ``<rk>|<i>``)."""
        source = body["source"]
        port = self.ports.sources[source]
        rk = f"lead_import|{source}|{body['request_id']}"
        with self.lock:
            self._gate()
            prev = self._idem(caller, rk, body)
            if prev:
                return {**(prev[1] or {"source": source}), "already_ran": True}
        try:
            found = port.fetch(body["limit"])
        except NotWired:
            raise Unavailable(R("SOURCE_NOT_WIRED")) from None
        created, duplicates, refused = [], 0, 0
        with self.lock:
            self._gate()
            for i, raw in enumerate(list(found)[:body["limit"]]):
                if (caller, f"{rk}|{i}") in self.requests:
                    duplicates += 1
                    continue
                try:
                    if not isinstance(raw, dict) or forbidden_keys(raw):
                        raise Invalid(R("FORBIDDEN_FIELD"))
                    item = m.ImportedLead.model_validate(raw).model_dump(mode="json")
                    if not i01_intake.port_admissible(source, item["evidence"]["kind"]):
                        raise Invalid(R("SOURCE_NOT_ALLOWED"))
                    view = self._ingest(caller, source, item, f"{rk}|{i}", body)
                except (ValidationError, Invalid, TypeError):
                    refused += 1
                    continue
                if view.get("duplicate"):
                    duplicates += 1
                else:
                    created.append(view["lead_id"])
            prev = self._idem(caller, rk, body)             # another call with this key finished meanwhile
            if prev:
                return {**(prev[1] or {"source": source}), "already_ran": True}
            result = {"source": source, "created": created, "duplicates": duplicates, "refused": refused}
            self._commit("lead_import_ran", self._req({"source": source, "created": len(created),
                                                       "duplicates": duplicates, "refused": refused},
                                                      caller, rk, body, result), caller)
            return result

    # ------------------------------------------------------------------------------------------------ pipeline

    def set_owner(self, caller: str, lead_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"lead_owner|{lead_id}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return self.lead_view(self.leads[lead_id])
            self._get(self.leads, lead_id, "LEAD_NOT_FOUND")
            self._commit("lead_owner_set", self._req({"lead_id": lead_id, "owner": body["owner"]}, caller, rk, body,
                                                     lead_id), caller)
            return self.lead_view(self.leads[lead_id])

    def disqualify(self, caller: str, lead_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"lead_disqualify|{lead_id}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return self.lead_view(self.leads[lead_id])
            lead = self._get(self.leads, lead_id, "LEAD_NOT_FOUND")
            if lead["status"] not in OPEN_LEAD + ("stale",):
                raise Conflict(R("LEAD_CLOSED"))
            self._commit("lead_disqualified", self._req({"lead_id": lead_id, "reason_code": body["reason_code"]},
                                                        caller, rk, body, lead_id), caller)
            return self.lead_view(lead)

    def convert(self, caller: str, lead_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"lead_convert|{lead_id}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return dict(self.opps[prev[1]])
            lead = self._get(self.leads, lead_id, "LEAD_NOT_FOUND")
            if lead["status"] != "qualified":
                raise Conflict(R("LEAD_NOT_QUALIFIED"))
            if lead["brand"] is None:
                raise Conflict(R("LEAD_UNROUTED"))
            oid = derived_id("opp", lead_id)
            self._commit("lead_converted", self._req({"lead_id": lead_id, "opportunity_id": oid,
                                                      "owner": body.get("owner")}, caller, rk, body, oid), caller)
            return dict(self.opps[oid])

    def set_time_zone(self, caller: str, contact_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"contact_tz|{contact_id}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return self.contact_view(self.contacts[contact_id])
            c = self._get(self.contacts, contact_id, "CONTACT_NOT_FOUND")
            if i07_quiet_hours.zone(body["time_zone"]) is None:
                raise Invalid(R("TIME_ZONE_INVALID"))
            if not i07_quiet_hours.zone_fits_phone(body["time_zone"], c.get("phone")):
                raise Invalid(R("TIME_ZONE_PHONE_MISMATCH"))
            # AEGIS S1-M1: the zone decides quiet hours, so every change is a typed ledger event before it applies
            data = {"contact_id": contact_id, "time_zone": body["time_zone"]}
            self._commit("contact_time_zone_set", self._req(data, caller, rk, body, contact_id), caller,
                         evidence=("contact_time_zone_set", f"contact:{contact_id}",
                                   {**data, "previous": c.get("time_zone")}, (caller, rk)))
            return self.contact_view(self.contacts[contact_id])

    def _verify_value(self, value: str) -> str:
        v = " ".join(value.split())
        if not v or not i08_templates.merge_value_ok(v):
            raise Invalid(R("MERGE_FIELD_REFUSED"))
        return v

    def verify_display_name(self, caller: str, account_id: str, body: dict) -> dict:
        """AEGIS S2-H1: the only name ``{{company}}`` ever renders, set by a person at the console."""
        with self.lock:
            self._gate()
            rk = f"display_name|{account_id}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return dict(self.accounts[account_id])
            self._get(self.accounts, account_id, "ACCOUNT_NOT_FOUND")
            data = {"account_id": account_id, "display_name": self._verify_value(body["display_name"])}
            self._commit("account_display_name_verified", self._req(data, caller, rk, body, account_id), caller,
                         evidence=("account_display_name_verified", f"account:{account_id}", data, (caller, rk)))
            return dict(self.accounts[account_id])

    def verify_first_name(self, caller: str, contact_id: str, body: dict) -> dict:
        """AEGIS S2-H1: the only name ``{{first_name}}`` ever renders, set by a person at the console."""
        with self.lock:
            self._gate()
            rk = f"first_name|{contact_id}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return self.contact_view(self.contacts[contact_id])
            self._get(self.contacts, contact_id, "CONTACT_NOT_FOUND")
            data = {"contact_id": contact_id, "first_name": self._verify_value(body["first_name"])}
            self._commit("contact_first_name_verified", self._req(data, caller, rk, body, contact_id), caller,
                         evidence=("contact_first_name_verified", f"contact:{contact_id}",
                                   {"contact_id": contact_id}, (caller, rk)))
            return self.contact_view(self.contacts[contact_id])

    def set_stage(self, caller: str, opp_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"opp_stage|{opp_id}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return dict(self.opps[opp_id])
            o = self._get(self.opps, opp_id, "OPPORTUNITY_NOT_FOUND")
            if o["stage"] in ("closed_won", "closed_lost"):
                raise Conflict(R("OPPORTUNITY_CLOSED"))
            self._commit("opportunity_stage_set", self._req({"opportunity_id": opp_id, "stage": body["stage"]},
                                                            caller, rk, body, opp_id), caller)
            return dict(o)

    def log_activity(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            target = f"{body['target_kind']}:{body['target_id']}"
            rk = f"activity|{target}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return {"activity_id": prev[1], "target": target}
            table = {"lead": self.leads, "opportunity": self.opps, "account": self.accounts}[body["target_kind"]]
            if body["target_id"] not in table:
                raise Invalid(R("TARGET_NOT_FOUND"))
            aid = derived_id("act", caller, rk)
            self._commit("activity_logged", self._req({"activity_id": aid, "target": target, "kind": body["kind"],
                                                       "note": body.get("note")}, caller, rk, body, aid), caller)
            return {"activity_id": aid, "target": target}

    def close_task(self, caller: str, task_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"task_close|{task_id}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return dict(self.tasks[task_id])
            t = self._get(self.tasks, task_id, "TASK_NOT_FOUND")
            if t["status"] != "open":
                raise Conflict(R("TASK_CLOSED"))
            if t.get("hold_id"):
                raise Forbidden(R("HOLD_NEEDS_ANDRE_DECISION"))      # S2-C1: only Andre's decision closes it
            self._commit("task_closed", self._req({"task_id": task_id, "outcome": body["outcome"]}, caller, rk, body,
                                                  task_id), caller)
            return dict(t)

    def _age_leads(self, body: dict, rk: str) -> dict:
        cutoff = self.now() - timedelta(days=self.settings.stale_lead_days)
        ids = sorted(lid for lid, x in self.leads.items() if x["status"] in OPEN_LEAD
                     and parse_iso(x["updated_at"]) < cutoff)
        result = {"aged": ids}
        self._commit("leads_aged", self._req({"lead_ids": ids}, "scheduler", rk, body, result), "scheduler")
        return result
