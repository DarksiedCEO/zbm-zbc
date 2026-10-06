"""Campaigns (influencer and co-marketing partnerships), sponsored-content briefs with their required FTC disclosure,
deals with the $5,000 aggregate approval rule, contracts through Legal (37), content that Andre approves by hash
before it counts live, and material-connection records (ADR 0015 decisions 14-16). A mixin of InfluencerService.

Partnership marketing here is campaign-level co-marketing with brands and agencies (a campaign of kind
``co_marketing`` names its partner). Referral and alliance partner commissions belong to department 12 and are not
here."""

from __future__ import annotations

import hashlib
import json
from typing import Optional

import money
from errors import Conflict, Forbidden, Invalid, Unavailable
from intelligences import i01_intake, i08_disclosure, i09_deal_approval
from ledger import derived_id
from reasons import R

CONTENT_DEAL_STATUSES = ("approved", "contract_sent", "contracted")


def _sha(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def brief_sha256(campaign_id: str, brand: str, title: str, text: str, disclosure: str) -> str:
    """Over the brief AS ISSUED: the approved text with i08's FTC section appended."""
    return _sha({"campaign_id": campaign_id, "brand": brand, "disclosure": disclosure,
                 "rendered": i08_disclosure.rendered_brief(brand, title, text, disclosure)})


def deal_sha256(d: dict) -> str:
    return _sha({k: d[k] for k in ("influencer_id", "campaign_id", "brief_id", "brief_sha256", "brand", "deliverables",
                                   "fee", "product_value", "total", "currency")})


def content_sha256(c: dict) -> str:
    return _sha({k: c[k] for k in ("deal_id", "platform", "caption", "media_sha256", "platform_label_on",
                                   "disclosure")})


class DealsMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_campaign_created(self, d, at):
        self.campaigns[d["campaign_id"]] = {**{k: d.get(k) for k in ("campaign_id", "brand", "name", "kind", "partner",
                                                                      "objective")},
                                            "status": "active", "created_at": at, "closed_at": None}

    def _a_campaign_closed(self, d, at):
        self.campaigns[d["campaign_id"]].update(status="closed", closed_at=at)

    def _a_brief_created(self, d, at):
        self.briefs[d["brief_id"]] = {**{k: d[k] for k in ("brief_id", "campaign_id", "brand", "title", "text",
                                                            "disclosure", "content_sha256")},
                                      "status": "draft", "approved_at": None, "created_at": at}

    def _a_brief_approved(self, d, at):
        self.briefs[d["brief_id"]].update(status="approved", approved_at=at)

    def _a_deal_created(self, d, at):
        self.deals[d["deal_id"]] = {**{k: d[k] for k in ("deal_id", "influencer_id", "campaign_id", "brief_id",
                                                          "brief_sha256", "brand", "deliverables", "fee",
                                                          "product_value", "total", "currency", "content_sha256",
                                                          "needs_andre")},
                                    "status": "approved" if not d["needs_andre"] else "pending_andre",
                                    "approved_by": None if d["needs_andre"] else "auto", "envelope_ref": None,
                                    "created_at": at, "updated_at": at, "requested": "0.00"}
        if d.get("material"):
            self._a_material(d["material"], at)

    def _a_material(self, mc, at):
        self.material[mc["mc_id"]] = {**mc, "recorded_at": at}

    def _a_deal_approved(self, d, at):
        self.deals[d["deal_id"]].update(status="approved", approved_by="andre", updated_at=at)
        self._a_material(d["material"], at)

    def _a_deal_rejected(self, d, at):
        self.deals[d["deal_id"]].update(status="rejected", updated_at=at)

    def _a_deal_cancelled(self, d, at):
        self.deals[d["deal_id"]].update(status="cancelled", updated_at=at)

    def _a_contract_sent(self, d, at):
        self.deals[d["deal_id"]].update(status="contract_sent", envelope_ref=d["envelope_ref"], updated_at=at)

    def _a_contract_in_force(self, d, at):
        self.deals[d["deal_id"]].update(status="contracted", contracted_at=at, updated_at=at)

    def _a_content_submitted(self, d, at):
        self.contents[d["content_id"]] = {**{k: d[k] for k in ("content_id", "deal_id", "campaign_id",
                                                                "influencer_id", "platform", "caption",
                                                                "media_sha256", "platform_label_on", "disclosure",
                                                                "content_sha256")},
                                          "status": "submitted", "post_ref": None, "submitted_by": d["actor"],
                                          "created_at": at, "updated_at": at}

    def _a_content_approved(self, d, at):
        self.contents[d["content_id"]].update(status="approved", approved_at=at, updated_at=at)

    def _a_content_rejected(self, d, at):
        self.contents[d["content_id"]].update(status="rejected", updated_at=at)

    def _a_content_live(self, d, at):
        self.contents[d["content_id"]].update(status="live", post_ref=d["post_ref"], live_at=at, updated_at=at)

    # ------------------------------------------------------------------------------------------------ campaigns

    def campaign_view(self, c: dict) -> dict:
        live = sum(1 for x in self.contents.values() if x["campaign_id"] == c["campaign_id"] and x["status"] == "live")
        return {**c, "live_content": live,
                "deals": sum(1 for x in self.deals.values() if x["campaign_id"] == c["campaign_id"])}

    def campaigns_view(self) -> list[dict]:
        with self.lock:
            return [self.campaign_view(c) for c in self.campaigns.values()]

    def campaign(self, campaign_id: str) -> dict:
        with self.lock:
            return self.campaign_view(self._get(self.campaigns, campaign_id, "CAMPAIGN_NOT_FOUND"))

    def create_campaign(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("campaign", body["brand"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.campaign_view(self.campaigns[prev[1]])
            if (body["kind"] == "co_marketing") != (body.get("partner") is not None):
                raise Invalid(R("PARTNER_REQUIRED" if body["kind"] == "co_marketing" else "PARTNER_NOT_ALLOWED"))
            cid = derived_id("cmp", caller, rk)
            self._commit("campaign_created", self._req({"campaign_id": cid, **{k: body.get(k) for k in (
                "brand", "name", "kind", "partner", "objective")}}, caller, rk, body, cid), caller,
                evidence=("campaign_created", f"campaign:{cid}",
                          {"campaign_id": cid, "brand": body["brand"], "kind": body["kind"],
                           "partner_ref": (body.get("partner") or {}).get("ref")}, (caller, rk)))
            return self.campaign_view(self.campaigns[cid])

    def close_campaign(self, caller: str, campaign_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("campaign_close", campaign_id, body)
            if self._idem(caller, rk, body):
                return self.campaign_view(self.campaigns[campaign_id])
            c = self._get(self.campaigns, campaign_id, "CAMPAIGN_NOT_FOUND")
            if c["status"] != "active":
                raise Conflict(R("CAMPAIGN_CLOSED"))
            self._commit("campaign_closed", self._req({"campaign_id": campaign_id}, caller, rk, body, campaign_id),
                         caller)
            return self.campaign_view(c)

    # ------------------------------------------------------------------------------------------------ briefs

    def brief_view(self, b: dict) -> dict:
        return {**b, "rendered": i08_disclosure.rendered_brief(b["brand"], b["title"], b["text"], b["disclosure"])}

    def briefs_view(self, campaign_id: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.brief_view(b) for b in self.briefs.values()
                    if campaign_id is None or b["campaign_id"] == campaign_id][:1000]

    def brief(self, brief_id: str) -> dict:
        with self.lock:
            return self.brief_view(self._get(self.briefs, brief_id, "BRIEF_NOT_FOUND"))

    def create_brief(self, caller: str, body: dict) -> dict:
        """Every sponsored-content brief carries a required disclosure from the closed list, and the FTC section is
        appended by the service (it cannot be left out). Briefs are never edited: a change is a new brief."""
        with self.lock:
            self._gate()
            rk = self._rk("brief", body["campaign_id"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.brief_view(self.briefs[prev[1]])
            c = self._get(self.campaigns, body["campaign_id"], "CAMPAIGN_NOT_FOUND")
            if c["status"] != "active":
                raise Conflict(R("CAMPAIGN_CLOSED"))
            if not i08_disclosure.allowed(c["brand"], body["disclosure"]):
                raise Invalid(R("DISCLOSURE_NOT_ALLOWED"))
            if i08_disclosure.hidden_characters(body["title"] + body["text"]):
                raise Invalid(R("CONTENT_HIDDEN_CHARACTERS"))
            bid = derived_id("brf", caller, rk)
            sha = brief_sha256(c["campaign_id"], c["brand"], body["title"], body["text"], body["disclosure"])
            self._commit("brief_created", self._req({"brief_id": bid, "campaign_id": c["campaign_id"],
                                                     "brand": c["brand"], "title": body["title"], "text": body["text"],
                                                     "disclosure": body["disclosure"], "content_sha256": sha},
                                                    caller, rk, body, bid), caller)
            return self.brief_view(self.briefs[bid])

    def _brief_problem(self, b: dict) -> Optional[str]:
        if b["status"] != "approved":
            return "BRIEF_NOT_APPROVED"
        if brief_sha256(b["campaign_id"], b["brand"], b["title"], b["text"], b["disclosure"]) != b["content_sha256"]:
            return "CONTENT_HASH_MISMATCH"
        if not i08_disclosure.allowed(b["brand"], b["disclosure"]):
            return "DISCLOSURE_NOT_ALLOWED"
        return None

    def approve_brief(self, brief_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("brief_approve", brief_id, body)
            if self._idem("andre", rk, body):
                return self.brief_view(self.briefs[brief_id])
            b = self._get(self.briefs, brief_id, "BRIEF_NOT_FOUND")
            if b["status"] != "draft":
                raise Conflict(R("ALREADY_APPROVED"))
            current = brief_sha256(b["campaign_id"], b["brand"], b["title"], b["text"], b["disclosure"])
            if body["content_sha256"] != b["content_sha256"] or current != b["content_sha256"]:
                raise Conflict(R("CONTENT_HASH_MISMATCH"))
            self._commit("brief_approved", self._req({"brief_id": brief_id}, "andre", rk, body, brief_id), "andre",
                         evidence=("brief_approved", f"brief:{brief_id}",
                                   {"brief_id": brief_id, "campaign_id": b["campaign_id"],
                                    "disclosure": b["disclosure"], "content_sha256": b["content_sha256"]},
                                   ("andre", rk)))
            return self.brief_view(b)

    # ------------------------------------------------------------------------------------------------ deals

    # ------------------------------------------------------------------------------------------------ one person

    def _person_keys(self, inf: dict) -> set:
        """What ties records to one PERSON: the tax reference (applied or waiting for confirmation / Andre), the
        Finance payee and Finance's per-person key of the matched TIN."""
        keys = set()
        if inf.get("tax"):
            keys.add(("tax", inf["tax"]["tax_ref_sha256"]))
        if inf["payee"]["payee_ref"]:
            keys.add(("payee", inf["payee"]["payee_ref"]))
        if inf["payee"].get("person_key"):                 # AEGIS R2-N2: Finance's key of the matched TIN
            keys.add(("person", inf["payee"]["person_key"]))
        for c in self.confirmations.values():
            if c["influencer_id"] == inf["influencer_id"] and c["kind"] == "tax_profile" \
                    and c["status"] in ("pending", "pending_andre", "undeliverable"):
                keys.add(("tax", c["payload"]["tax_ref_sha256"]))
        return keys

    def _linked_ids(self, inf: dict, extra_key=None) -> set:
        keys = self._person_keys(inf) | ({extra_key} if extra_key else set())
        ids = {inf["influencer_id"]}
        if keys:
            ids |= {o["influencer_id"] for o in self.influencers.values() if self._person_keys(o) & keys}
        return ids

    def _person_total(self, inf: dict, extra_key=None):
        people = self._linked_ids(inf, extra_key)
        return money.total([x["total"] for x in self.deals.values() if x["influencer_id"] in people
                            and x["status"] not in i09_deal_approval.NOT_COUNTED])

    def deal_view(self, d: dict) -> dict:
        return dict(d)

    def deals_view(self, status: Optional[str], influencer_id: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.deal_view(d) for d in self.deals.values() if (status is None or d["status"] == status)
                    and (influencer_id is None or d["influencer_id"] == influencer_id)][:1000]

    def deal(self, deal_id: str) -> dict:
        with self.lock:
            return self.deal_view(self._get(self.deals, deal_id, "DEAL_NOT_FOUND"))

    def _material(self, d: dict) -> dict:
        """The FTC material-connection record for a deal: what the influencer gets, from which brand, for which
        campaign, and the disclosure their content must carry."""
        kinds = [k for k, v in (("payment", d["fee"]), ("free_product", d["product_value"])) if money.D(v) > 0]
        return {"mc_id": derived_id("mcn", d["deal_id"]), "deal_id": d["deal_id"], "influencer_id": d["influencer_id"],
                "campaign_id": d["campaign_id"], "brand": d["brand"], "kinds": kinds, "fee": d["fee"],
                "product_value": d["product_value"], "currency": d["currency"],
                "disclosure": self.briefs[d["brief_id"]]["disclosure"], "brief_id": d["brief_id"]}

    def _mc_evidence(self, mc: dict, actor: str, rk: str):
        return ("material_connection_recorded", f"deal:{mc['deal_id']}",
                {k: mc[k] for k in ("mc_id", "deal_id", "influencer_id", "campaign_id", "brand", "kinds", "fee",
                                    "product_value", "currency", "disclosure")}, (actor, rk))

    def create_deal(self, caller: str, body: dict) -> dict:
        """A deal at or under ``INF_AUTO_APPROVE_MAX`` in all three aggregates (i09: the deal, the influencer's open
        deals, the influencer's deals in this campaign) is approved here; anything over waits for Andre. Money is
        canonical Decimal strings only; a float is refused before this point (models + money.parse)."""
        with self.lock:
            self._gate()
            rk = self._rk("deal", body["influencer_id"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.deal_view(self.deals[prev[1]])
            try:
                fee = money.parse(body["fee"])
                product = money.parse(body["product_value"])
            except money.MoneyError:
                raise Invalid(R("MONEY_INVALID")) from None
            total = i09_deal_approval.deal_total(fee, product)
            if total <= 0:
                raise Invalid(R("MONEY_INVALID"))
            inf = self._get(self.influencers, body["influencer_id"], "INFLUENCER_NOT_FOUND")
            problem = i01_intake.contractable(inf)
            if problem:
                raise Forbidden(R(problem))
            c = self._get(self.campaigns, body["campaign_id"], "CAMPAIGN_NOT_FOUND")
            if c["status"] != "active":
                raise Conflict(R("CAMPAIGN_CLOSED"))
            b = self._get(self.briefs, body["brief_id"], "BRIEF_NOT_FOUND")
            if b["campaign_id"] != c["campaign_id"]:
                raise Invalid(R("BRIEF_NOT_IN_CAMPAIGN"))
            problem = self._brief_problem(b)
            if problem:
                raise Conflict(R(problem))
            people = self._linked_ids(inf)
            others = [x for x in self.deals.values() if x["influencer_id"] in people]
            reasons = i09_deal_approval.needs_andre(total, others, c["campaign_id"], self.settings.auto_approve_max)
            did = derived_id("dea", caller, rk)
            d = {"deal_id": did, "influencer_id": inf["influencer_id"], "campaign_id": c["campaign_id"],
                 "brief_id": b["brief_id"], "brief_sha256": b["content_sha256"], "brand": c["brand"],
                 "deliverables": [dict(x) for x in body["deliverables"]], "fee": money.fmt(fee),
                 "product_value": money.fmt(product), "total": money.fmt(total), "currency": body["currency"]}
            d["content_sha256"] = deal_sha256(d)
            d["needs_andre"] = reasons
            ev = [("deal_recorded", f"deal:{did}", {"deal_id": did, "influencer_id": d["influencer_id"],
                                                     "campaign_id": d["campaign_id"], "total": d["total"],
                                                     "content_sha256": d["content_sha256"], "needs_andre": reasons},
                   (caller, rk))]
            if not reasons:
                d["material"] = self._material(d)
                ev.append(self._mc_evidence(d["material"], caller, rk))
            self._commit("deal_created", self._req(d, caller, rk, body, did), caller, evidence=ev)
            return self.deal_view(self.deals[did])

    def approve_deal(self, deal_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("deal_approve", deal_id, body)
            if self._idem("andre", rk, body):
                return self.deal_view(self.deals[deal_id])
            d = self._get(self.deals, deal_id, "DEAL_NOT_FOUND")
            if d["status"] != "pending_andre":
                raise Conflict(R("DEAL_NOT_PENDING"))
            if body["content_sha256"] != d["content_sha256"] or deal_sha256(d) != d["content_sha256"]:
                raise Conflict(R("CONTENT_HASH_MISMATCH"))
            problem = i01_intake.contractable(self.influencers[d["influencer_id"]])
            if problem:
                raise Forbidden(R(problem))
            problem = self._brief_problem(self.briefs[d["brief_id"]])
            if problem or self.briefs[d["brief_id"]]["content_sha256"] != d["brief_sha256"]:
                raise Conflict(R(problem or "CONTENT_HASH_MISMATCH"))
            mc = self._material(d)
            self._commit("deal_approved", self._req({"deal_id": deal_id, "material": mc}, "andre", rk, body, deal_id),
                         "andre",
                         evidence=[("deal_approved", f"deal:{deal_id}",
                                    {"deal_id": deal_id, "content_sha256": d["content_sha256"], "total": d["total"]},
                                    ("andre", rk)), self._mc_evidence(mc, "andre", rk)])
            return self.deal_view(d)

    def reject_deal(self, deal_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("deal_reject", deal_id, body)
            if self._idem("andre", rk, body):
                return self.deal_view(self.deals[deal_id])
            d = self._get(self.deals, deal_id, "DEAL_NOT_FOUND")
            if d["status"] != "pending_andre":
                raise Conflict(R("DEAL_NOT_PENDING"))
            self._commit("deal_rejected", self._req({"deal_id": deal_id}, "andre", rk, body, deal_id), "andre",
                         evidence=("deal_rejected", f"deal:{deal_id}", {"deal_id": deal_id}, ("andre", rk)))
            return self.deal_view(d)

    def cancel_deal(self, caller: str, deal_id: str, body: dict) -> dict:
        """Before a contract is in force only: a contracted deal is not cancelled here (Legal's matter)."""
        with self.lock:
            self._gate()
            rk = self._rk("deal_cancel", deal_id, body)
            if self._idem(caller, rk, body):
                return self.deal_view(self.deals[deal_id])
            d = self._get(self.deals, deal_id, "DEAL_NOT_FOUND")
            if d["status"] not in ("pending_andre", "approved", "contract_sent"):
                raise Conflict(R("DEAL_NOT_CANCELLABLE"))
            self._commit("deal_cancelled", self._req({"deal_id": deal_id}, caller, rk, body, deal_id), caller,
                         evidence=("deal_cancelled", f"deal:{deal_id}", {"deal_id": deal_id}, (caller, rk)))
            return self.deal_view(d)

    # ------------------------------------------------------------------------------------------------ contracts

    def _contract_ready(self, d: dict) -> None:
        problem = i01_intake.contractable(self.influencers[d["influencer_id"]])
        if problem:
            raise Forbidden(R(problem))
        if deal_sha256(d) != d["content_sha256"]:
            raise Conflict(R("CONTENT_HASH_MISMATCH"))
        b = self.briefs[d["brief_id"]]
        problem = self._brief_problem(b)
        if problem or b["content_sha256"] != d["brief_sha256"]:
            raise Conflict(R(problem or "CONTENT_HASH_MISMATCH"))

    def send_contract(self, caller: str, deal_id: str, body: dict) -> dict:
        """The influencer agreement goes out through Legal (37). Legal is a stand-in: 503 ``LEGAL_UNAVAILABLE`` and
        nothing is recorded. Legal is never called with the lock held."""
        rk = self._rk("contract_send", deal_id, body)
        with self.lock:
            self._gate()
            if self._idem(caller, rk, body):
                return self.deal_view(self.deals[deal_id])
            d = self._get(self.deals, deal_id, "DEAL_NOT_FOUND")
            if d["status"] != "approved":
                raise Conflict(R("DEAL_NOT_APPROVED"))
            self._contract_ready(d)
            args = (d["deal_id"], d["influencer_id"], d["content_sha256"], d["brief_sha256"],
                    self.briefs[d["brief_id"]]["disclosure"])
        self._begin(rk)
        try:
            try:
                ans = self.ports.legal.send_contract(*args)
            except Exception:  # noqa: BLE001 - a port that raises is unavailable; its text is dropped
                ans = None
            if ans is None or ans.status == "unavailable":
                raise Unavailable(R("LEGAL_UNAVAILABLE"))
            if ans.status != "sent" or not isinstance(ans.envelope_ref, str) or not 1 <= len(ans.envelope_ref) <= 128:
                raise Conflict(R("CONTRACT_REFUSED"))
            with self.lock:
                self._gate()
                d = self.deals[deal_id]
                if d["status"] != "approved" or d["content_sha256"] != args[2]:
                    raise Conflict(R("STATE_CHANGED"))
                self._contract_ready(d)
                self._commit("contract_sent", self._req({"deal_id": deal_id, "envelope_ref": ans.envelope_ref},
                                                        caller, rk, body, deal_id), caller,
                             evidence=("contract_sent", f"deal:{deal_id}",
                                       {"deal_id": deal_id, "content_sha256": d["content_sha256"],
                                        "envelope_sha256": hashlib.sha256(ans.envelope_ref.encode()).hexdigest()},
                                       (caller, rk)))
                return self.deal_view(d)
        finally:
            self._end(rk)

    def confirm_contract(self, caller: str, deal_id: str, body: dict) -> dict:
        """Legal (37) says the agreement for THIS deal (its hash) is in force: the deal is ``contracted``."""
        rk = self._rk("contract_confirm", deal_id, body)
        with self.lock:
            self._gate()
            if self._idem(caller, rk, body):
                return self.deal_view(self.deals[deal_id])
            d = self._get(self.deals, deal_id, "DEAL_NOT_FOUND")
            if d["status"] != "contract_sent":
                raise Conflict(R("CONTRACT_NOT_SENT"))
            self._contract_ready(d)
            args = (d["deal_id"], d["envelope_ref"], d["content_sha256"])
        self._begin(rk)
        try:
            try:
                ans = self.ports.legal.contract_status(*args)
            except Exception:  # noqa: BLE001
                ans = None
            if ans is None or ans.status == "unavailable":
                raise Unavailable(R("LEGAL_UNAVAILABLE"))
            if ans.status != "in_force":
                raise Conflict(R("CONTRACT_NOT_IN_FORCE"))
            with self.lock:
                self._gate()
                d = self.deals[deal_id]
                if d["status"] != "contract_sent" or d["envelope_ref"] != args[1]:
                    raise Conflict(R("STATE_CHANGED"))
                self._contract_ready(d)
                self._commit("contract_in_force", self._req({"deal_id": deal_id}, caller, rk, body, deal_id), caller,
                             evidence=("contract_in_force", f"deal:{deal_id}",
                                       {"deal_id": deal_id, "content_sha256": d["content_sha256"]}, (caller, rk)))
                return self.deal_view(d)
        finally:
            self._end(rk)

    # ------------------------------------------------------------------------------------------------ content

    def content_view(self, c: dict) -> dict:
        return dict(c)

    def contents_view(self, status: Optional[str], deal_id: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.content_view(c) for c in self.contents.values() if (status is None or c["status"] == status)
                    and (deal_id is None or c["deal_id"] == deal_id)][:1000]

    def content(self, content_id: str) -> dict:
        with self.lock:
            return self.content_view(self._get(self.contents, content_id, "CONTENT_NOT_FOUND"))

    def _content_problem(self, c: dict, deal: dict) -> Optional[tuple]:
        """(error class, code) when content cannot be accepted or approved, from current state."""
        problem = i01_intake.contractable(self.influencers[deal["influencer_id"]])
        if problem:
            return Forbidden, problem
        if deal["status"] not in CONTENT_DEAL_STATUSES:
            return Conflict, "DEAL_NOT_APPROVED"
        if c["platform"] not in {x["platform"] for x in deal["deliverables"]}:
            return Invalid, "PLATFORM_NOT_IN_DEAL"
        if c["disclosure"] != self.briefs[deal["brief_id"]]["disclosure"]:
            return Conflict, "CONTENT_HASH_MISMATCH"
        problem = i08_disclosure.caption_problem(c["caption"], c["disclosure"]) or \
            i08_disclosure.label_problem(c["platform"], c["platform_label_on"])
        if problem:
            return Invalid, problem
        if content_sha256(c) != c["content_sha256"]:
            return Conflict, "CONTENT_HASH_MISMATCH"
        return None

    def submit_content(self, caller: str, body: dict) -> dict:
        """FTC fail-closed: content without the brief's disclosure, clearly and up front, is refused (i08)."""
        with self.lock:
            self._gate()
            rk = self._rk("content", body["deal_id"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.content_view(self.contents[prev[1]])
            d = self._get(self.deals, body["deal_id"], "DEAL_NOT_FOUND")
            c = {"deal_id": d["deal_id"], "campaign_id": d["campaign_id"], "influencer_id": d["influencer_id"],
                 "platform": body["platform"], "caption": body["caption"], "media_sha256": list(body["media_sha256"]),
                 "platform_label_on": body["platform_label_on"],
                 "disclosure": self.briefs[d["brief_id"]]["disclosure"]}
            c["content_sha256"] = content_sha256(c)
            problem = self._content_problem(c, d)
            if problem:
                raise problem[0](R(problem[1]))
            cid = derived_id("cnt", caller, rk)
            self._commit("content_submitted", self._req({"content_id": cid, **c}, caller, rk, body, cid), caller)
            return self.content_view(self.contents[cid])

    def approve_content(self, content_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("content_approve", content_id, body)
            if self._idem("andre", rk, body):
                return self.content_view(self.contents[content_id])
            c = self._get(self.contents, content_id, "CONTENT_NOT_FOUND")
            if c["status"] != "submitted":
                raise Conflict(R("CONTENT_NOT_PENDING"))
            if body["content_sha256"] != c["content_sha256"]:
                raise Conflict(R("CONTENT_HASH_MISMATCH"))
            problem = self._content_problem(c, self.deals[c["deal_id"]])
            if problem:
                raise problem[0](R(problem[1]))
            self._commit("content_approved", self._req({"content_id": content_id}, "andre", rk, body, content_id),
                         "andre",
                         evidence=("content_approved", f"content:{content_id}",
                                   {"content_id": content_id, "deal_id": c["deal_id"], "disclosure": c["disclosure"],
                                    "content_sha256": c["content_sha256"]}, ("andre", rk)))
            return self.content_view(c)

    def reject_content(self, content_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self._rk("content_reject", content_id, body)
            if self._idem("andre", rk, body):
                return self.content_view(self.contents[content_id])
            c = self._get(self.contents, content_id, "CONTENT_NOT_FOUND")
            if c["status"] != "submitted":
                raise Conflict(R("CONTENT_NOT_PENDING"))
            self._commit("content_rejected", self._req({"content_id": content_id}, "andre", rk, body, content_id),
                         "andre", evidence=("content_rejected", f"content:{content_id}",
                                            {"content_id": content_id}, ("andre", rk)))
            return self.content_view(c)

    def content_live(self, caller: str, content_id: str, body: dict) -> dict:
        """The campaign counts content live only when Andre approved that exact content (its hash, named again here)
        and the deal's contract is in force."""
        with self.lock:
            self._gate()
            rk = self._rk("content_live", content_id, body)
            if self._idem(caller, rk, body):
                return self.content_view(self.contents[content_id])
            c = self._get(self.contents, content_id, "CONTENT_NOT_FOUND")
            if c["status"] != "approved":
                raise Conflict(R("CONTENT_NOT_APPROVED"))
            if body["content_sha256"] != c["content_sha256"]:
                raise Conflict(R("CONTENT_HASH_MISMATCH"))
            d = self.deals[c["deal_id"]]
            if d["status"] != "contracted":
                raise Conflict(R("CONTRACT_NOT_IN_FORCE"))
            problem = self._content_problem(c, d)
            if problem:
                raise problem[0](R(problem[1]))
            self._commit("content_live", self._req({"content_id": content_id, "post_ref": body["post_ref"]}, caller,
                                                   rk, body, content_id), caller,
                         evidence=("content_live", f"content:{content_id}",
                                   {"content_id": content_id, "deal_id": c["deal_id"],
                                    "campaign_id": c["campaign_id"], "content_sha256": c["content_sha256"],
                                    "post_ref_sha256": hashlib.sha256(body["post_ref"].encode()).hexdigest()},
                                   (caller, rk)))
            return self.content_view(c)

    def material_view(self, influencer_id: Optional[str]) -> list[dict]:
        with self.lock:
            return [dict(x) for x in self.material.values()
                    if influencer_id is None or x["influencer_id"] == influencer_id][:5000]
