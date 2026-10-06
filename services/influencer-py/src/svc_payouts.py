"""Paying influencers (ADR 0015 decision 18; Andre, Oct 6 2026: Stripe payouts, the same as ZBC clippers). A mixin of
InfluencerService.

- **Tax information is a reference, never a number.** The creator's tax form is collected by Finance (31) / Stripe
  (or the vault); this service stores only the form kind, legal form, country and a provider reference
  (``stripe:acct_...`` or ``vault:<uuid>``) and its SHA-256. A raw TIN, SSN or EIN anywhere in a request is refused
  422 ``TAX_ID_REFUSED`` (textguard.py). A reference takes effect only once the creator confirms it from the address
  on record, and for a creator whose payee is already verified only once Andre approves it (AEGIS R1-M2); a new
  reference resets the payee: verification starts again.
- **Verified before paid.** ``POST /inf/v1/payees/{influencer_id}/verify`` registers the payee at Finance (from the
  reference) and reads its verification (KYC and TIN match happen at Finance and Stripe, never here); only Finance's
  ``verified`` counts. A payout request re-reads it from Finance at request time.
- **Payouts go through the Finance port; this service never calls Stripe.** A payout is for a contracted deal, for
  content Andre approved that is live, at most the deal's cash fee in total; it is recorded on the ledger
  (``payout_requested``) and in the log BEFORE Finance is called, and ``payout-retry`` re-sends what Finance could not
  take. A payout for a PERSON whose lifetime deals exceed the $5,000 limit, on a deal Andre did not approve himself,
  waits for Andre (AEGIS R1-M5). Nothing is ever paid here: Finance pays, and only after its own approvals.
"""

from __future__ import annotations

import hashlib
from typing import Optional

import money
from errors import Conflict, Forbidden, Invalid, Unavailable
from intelligences import i01_intake
from ledger import derived_id, payload_sha256
from reasons import R


def _rsha(ref: str) -> str:
    return hashlib.sha256(ref.encode("utf-8")).hexdigest()


def payout_sha256(p: dict) -> str:
    """What Andre approves for a payout held by the per-person rule (the payee by its SHA-256 only)."""
    return payload_sha256({"payout_id": p["payout_id"], "deal_id": p["deal_id"], "influencer_id": p["influencer_id"],
                           "amount": p["amount"], "currency": p["currency"], "content_ids": p["content_ids"],
                           "payee_ref_sha256": _rsha(p["payee_ref"])})


class PayoutsMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _apply_tax(self, inf: dict, d: dict, at: str) -> None:
        """A confirmed (and, for a verified payee, Andre-approved) tax reference takes effect."""
        inf["tax"] = {k: d[k] for k in ("tax_form", "tax_ref", "tax_ref_sha256", "legal_form", "country")}
        inf["tax"]["recorded_at"] = at
        inf["payee"] = {"payee_ref": None, "status": "none", "checked_at": None}     # a new reference: verify again
        inf["updated_at"] = at

    @staticmethod
    def _tax_evidence(iid: str, p: dict, actor: str, rk: str):
        return ("tax_profile_recorded", f"influencer:{iid}",
                {"influencer_id": iid, **{k: p[k] for k in ("tax_form", "tax_ref_sha256", "legal_form", "country")}},
                (actor, rk))

    def _a_payee_checked(self, d, at):
        inf = self.influencers[d["influencer_id"]]
        inf["payee"] = {"payee_ref": d["payee_ref"], "status": d["status"], "checked_at": at}

    def _a_payout_requested(self, d, at):
        self.payouts[d["payout_id"]] = {**{k: d[k] for k in ("payout_id", "deal_id", "influencer_id", "amount",
                                                              "currency", "brand", "content_ids", "payee_ref")},
                                        "status": d.get("status", "pending_finance"), "finance_ref": None,
                                        "reason": None, "needs_andre": d.get("needs_andre", []),
                                        "content_sha256": d.get("content_sha256"), "requested_at": at,
                                        "updated_at": at, "requested_by": d["actor"]}
        deal = self.deals[d["deal_id"]]
        deal["requested"] = money.fmt(money.total([deal["requested"], d["amount"]]))

    def _a_payout_approved(self, d, at):
        self.payouts[d["payout_id"]].update(status="pending_finance", approved_by="andre", updated_at=at)

    def _a_payout_result(self, d, at):
        p = self.payouts[d["payout_id"]]
        p.update(status=d["status"], finance_ref=d.get("finance_ref"), reason=d.get("reason"), updated_at=at)
        deal = self.deals[p["deal_id"]]
        if d["status"] in ("refused_by_finance", "cancelled"):
            deal["requested"] = money.fmt(money.total([deal["requested"]]) - money.D(p["amount"]))
        submitted = money.total([x["amount"] for x in self.payouts.values()
                                 if x["deal_id"] == deal["deal_id"] and x["status"] == "submitted"])
        if deal["status"] == "contracted" and money.D(deal["fee"]) > 0 and submitted == money.D(deal["fee"]):
            deal.update(status="completed", updated_at=at)

    # ------------------------------------------------------------------------------------------------ tax profile

    def record_tax_profile(self, caller: str, body: dict) -> dict:
        """A tax REFERENCE never takes effect on the caller's word (AEGIS R1-M2): it is mailed for confirmation to the
        address on the record, and a change for a creator whose payee is already verified then also needs Andre's
        approval of its hash. Answers the pending confirmation."""
        with self.lock:
            self._gate()
            rk = self._rk("tax_profile", body["influencer_id"], body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.confirmation_view(self.confirmations[prev[1]])
            inf = self._get(self.influencers, body["influencer_id"], "INFLUENCER_NOT_FOUND")
            problem = i01_intake.contractable(inf)
            if problem:
                raise Forbidden(R(problem))
            # W-9 for a US person; W-8BEN for a foreign individual; W-8BEN-E for a foreign entity
            us, w9 = body["country"] == "US", body["tax_form"] == "w9"
            if us != w9 or (not w9 and (body["tax_form"] == "w8bene") != (body["legal_form"] == "entity")):
                raise Invalid(R("TAX_FORM_MISMATCH"))
            payload = {"tax_form": body["tax_form"], "tax_ref": body["tax_ref"], "tax_ref_sha256": _rsha(body["tax_ref"]),
                       "legal_form": body["legal_form"], "country": body["country"]}
            conf, msg, ev = self._confirmation(caller, rk, inf["influencer_id"], "tax_profile", inf["email"],
                                               inf["email_hash"], payload)
            self._commit("confirmation_requested", self._req({"confirmation": conf, "message": msg}, caller, rk, body,
                                                             conf["conf_id"]), caller, evidence=ev)
            return self.confirmation_view(self.confirmations[conf["conf_id"]])

    # ------------------------------------------------------------------------------------------------ verification

    def verify_payee(self, caller: str, influencer_id: str, body: dict) -> dict:
        """Register the payee at Finance (once per tax reference) and read its verification. Finance unavailable:
        503 ``FINANCE_UNAVAILABLE`` and nothing recorded. Finance is never called with the lock held."""
        rk = self._rk("payee_verify", influencer_id, body)
        with self.lock:
            self._gate()
            prev = self._idem(caller, rk, body)
            if prev:
                return self.influencer_view(self.influencers[influencer_id])
            inf = self._get(self.influencers, influencer_id, "INFLUENCER_NOT_FOUND")
            problem = i01_intake.contractable(inf)
            if problem:
                raise Forbidden(R(problem))
            if not inf.get("tax"):
                raise Conflict(R("TAX_PROFILE_REQUIRED"))
            tax = dict(inf["tax"])
            payee_ref = inf["payee"]["payee_ref"]
            payee_id = derived_id("pye", influencer_id, tax["tax_ref_sha256"])
            identity_ref = self.identity_ref(inf)
        self._begin(rk)
        try:
            fin = self.ports.finance
            try:
                if payee_ref is None:
                    reg = fin.register_payee(payee_id, tax["tax_ref"], tax["tax_form"], tax["legal_form"],
                                             tax["country"], identity_ref)
                    if reg.status == "unavailable":
                        raise Unavailable(R("FINANCE_UNAVAILABLE"))
                    if reg.status != "registered" or not isinstance(reg.payee_ref, str) \
                            or not 1 <= len(reg.payee_ref) <= 128:
                        raise Conflict(R("PAYEE_REFUSED"))
                    payee_ref = reg.payee_ref
                st = fin.payee_status(payee_ref)
            except (Unavailable, Conflict):
                raise
            except Exception:  # noqa: BLE001 - a port that raises is unavailable; its text is dropped
                raise Unavailable(R("FINANCE_UNAVAILABLE")) from None
            if st.status == "unavailable":
                raise Unavailable(R("FINANCE_UNAVAILABLE"))
            status = st.status if st.status in ("verified", "pending", "refused") else "refused"
            with self.lock:
                self._gate()
                inf = self.influencers[influencer_id]
                if not inf.get("tax") or inf["tax"]["tax_ref_sha256"] != tax["tax_ref_sha256"]:
                    raise Conflict(R("STATE_CHANGED"))
                problem = i01_intake.contractable(inf)
                if problem:
                    raise Forbidden(R(problem))
                self._commit("payee_checked", self._req({"influencer_id": influencer_id, "payee_ref": payee_ref,
                                                         "status": status}, caller, rk, body, influencer_id), caller,
                             evidence=("payee_status_recorded", f"influencer:{influencer_id}",
                                       {"influencer_id": influencer_id, "status": status,
                                        "payee_ref_sha256": _rsha(payee_ref)}, (caller, rk)))
                return self.influencer_view(inf)
        finally:
            self._end(rk)

    @staticmethod
    def identity_ref(inf: dict) -> str:
        """The creator's CONFIRMED identity for Finance to match the payee's KYC against (AEGIS R1-M2): our record id
        and the SHA-256 of the confirmed canonical address (Finance can hash the address Stripe verified)."""
        from intelligences import i02_identity
        return f"{inf['influencer_id']}:" + hashlib.sha256(i02_identity.canonical(inf["email"]).encode()).hexdigest()

    # ------------------------------------------------------------------------------------------------ payouts

    def payout_view(self, p: dict) -> dict:
        return {k: v for k, v in p.items() if k != "payee_ref"}

    def payouts_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [self.payout_view(p) for p in self.payouts.values()
                    if status is None or p["status"] == status][:1000]

    def payout(self, payout_id: str) -> dict:
        with self.lock:
            return self.payout_view(self._get(self.payouts, payout_id, "PAYOUT_NOT_FOUND"))

    def _payout_check(self, body: dict, amount) -> tuple[dict, dict]:
        """(deal, influencer) when a payout of ``amount`` for these contents may be requested now; else raises."""
        d = self._get(self.deals, body["deal_id"], "DEAL_NOT_FOUND")
        inf = self.influencers[d["influencer_id"]]
        problem = i01_intake.contractable(inf)
        if problem:
            raise Forbidden(R(problem))
        if d["status"] != "contracted":
            raise Conflict(R("CONTRACT_NOT_IN_FORCE"))
        if len(set(body["content_ids"])) != len(body["content_ids"]):
            raise Invalid(R("CONTENT_REPEATED"))
        paid = {c for p in self.payouts.values() if p["status"] not in ("refused_by_finance", "cancelled")
                for c in p["content_ids"]}
        for cid in body["content_ids"]:
            c = self.contents.get(cid)
            if c is None or c["deal_id"] != d["deal_id"]:
                raise Invalid(R("CONTENT_NOT_IN_DEAL"))
            if c["status"] != "live":
                raise Conflict(R("CONTENT_NOT_LIVE"))
            if cid in paid:
                raise Conflict(R("CONTENT_ALREADY_PAID"))
        if money.total([d["requested"], amount]) > money.D(d["fee"]):
            raise Conflict(R("PAYOUT_OVER_DEAL"))
        if inf["payee"]["status"] != "verified" or not inf["payee"]["payee_ref"]:
            raise Forbidden(R("PAYEE_NOT_VERIFIED"))
        return d, inf

    def request_payout(self, caller: str, body: dict) -> dict:
        rk = self._rk("payout", body["deal_id"], body)
        try:
            amount = money.parse(body["amount"], positive=True)
        except money.MoneyError:
            raise Invalid(R("MONEY_INVALID")) from None
        with self.lock:
            self._gate()
            prev = self._idem(caller, rk, body)
            if prev:
                return self.payout_view(self.payouts[prev[1]])
            d, inf = self._payout_check(body, amount)
            payee_ref = inf["payee"]["payee_ref"]
        self._begin(rk)
        try:
            try:
                st = self.ports.finance.payee_status(payee_ref)     # verified NOW, at Finance, not only when last read
            except Exception:  # noqa: BLE001
                raise Unavailable(R("FINANCE_UNAVAILABLE")) from None
            if st.status == "unavailable":
                raise Unavailable(R("FINANCE_UNAVAILABLE"))
            if st.status != "verified":
                raise Forbidden(R("PAYEE_NOT_VERIFIED"))
            with self.lock:
                self._gate()
                prev = self._idem(caller, rk, body)
                if prev:
                    return self.payout_view(self.payouts[prev[1]])
                d, inf = self._payout_check(body, amount)
                if inf["payee"]["payee_ref"] != payee_ref:
                    raise Conflict(R("STATE_CHANGED"))
                pid = derived_id("pay", caller, rk)
                data = {"payout_id": pid, "deal_id": d["deal_id"], "influencer_id": inf["influencer_id"],
                        "amount": money.fmt(amount), "currency": d["currency"], "brand": d["brand"],
                        "content_ids": list(body["content_ids"]), "payee_ref": payee_ref}
                # AEGIS R1-M5: the $5,000 rule again, keyed on the PERSON being paid — every deal of every record
                # sharing this payee or tax reference, lifetime. Over it, a deal Andre did not approve himself is paid
                # only after Andre approves this payout by its hash.
                person_total = self._person_total(inf)
                needs = ["PAYEE_TOTAL_OVER_LIMIT"] if (person_total > self.settings.auto_approve_max
                                                        and d.get("approved_by") != "andre") else []
                data.update(status="pending_andre" if needs else "pending_finance", needs_andre=needs,
                            content_sha256=payout_sha256(data))
                self._commit("payout_requested", self._req(data, caller, rk, body, pid), caller,
                             evidence=("payout_requested", f"payout:{pid}",
                                       {"payout_id": pid, "deal_id": d["deal_id"],
                                        "influencer_id": inf["influencer_id"], "amount": data["amount"],
                                        "currency": data["currency"], "content_ids": data["content_ids"],
                                        "payee_ref_sha256": _rsha(payee_ref), "needs_andre": needs,
                                        "content_sha256": data["content_sha256"]}, (caller, rk)))
            if not needs:
                self._hand_to_finance(pid)
            with self.lock:
                return self.payout_view(self.payouts[pid])
        finally:
            self._end(rk)

    def decide_payout(self, payout_id: str, body: dict, approve: bool) -> dict:
        """Andre on a payout held by the per-person $5,000 rule: approve binds its hash and hands it to Finance."""
        with self.lock:
            self._gate()
            rk = self._rk("payout_approve" if approve else "payout_reject", payout_id, body)
            if self._idem("andre", rk, body):
                return self.payout_view(self.payouts[payout_id])
            p = self._get(self.payouts, payout_id, "PAYOUT_NOT_FOUND")
            if p["status"] != "pending_andre":
                raise Conflict(R("PAYOUT_NOT_PENDING"))
            if not approve:
                self._commit("payout_result", self._req({"payout_id": payout_id, "status": "cancelled",
                                                         "reason": "REJECTED_BY_ANDRE"}, "andre", rk, body, payout_id),
                             "andre", evidence=("payout_result", f"payout:{payout_id}",
                                                {"payout_id": payout_id, "status": "cancelled",
                                                 "reason": "REJECTED_BY_ANDRE"}, ("andre", rk)))
                return self.payout_view(p)
            if body["content_sha256"] != p["content_sha256"] or payout_sha256(p) != p["content_sha256"]:
                raise Conflict(R("CONTENT_HASH_MISMATCH"))
            stale = self._stale_payout(p)
            if stale:
                raise Conflict(R("STATE_CHANGED"))
            self._commit("payout_approved", self._req({"payout_id": payout_id}, "andre", rk, body, payout_id),
                         "andre", evidence=("payout_approved", f"payout:{payout_id}",
                                            {"payout_id": payout_id, "content_sha256": p["content_sha256"]},
                                            ("andre", rk)))
        self._hand_to_finance(payout_id)
        with self.lock:
            return self.payout_view(self.payouts[payout_id])

    def _stale_payout(self, p: dict) -> Optional[str]:
        """A payout still waiting for Finance is never handed over once the payee it was requested for is no longer
        the influencer's verified payee (a new tax reference, a failed re-check) or the influencer is blocked."""
        inf = self.influencers[p["influencer_id"]]
        if i01_intake.contractable(inf):
            return "INFLUENCER_BLOCKED"
        if inf["payee"]["payee_ref"] != p["payee_ref"] or inf["payee"]["status"] != "verified":
            return "PAYEE_CHANGED"
        return None

    def _hand_to_finance(self, pid: str) -> str:
        """Hand one recorded payout to Finance (outside the lock) and record its answer. One hand-over per payout at a
        time (AEGIS R1-L3: the request and the retry job never race); Finance's verification of the payee is read
        again right before every hand-over. ``unavailable`` leaves it ``pending_finance`` for ``payout-retry``; the
        adapter is idempotent on ``payout_id``. A payout whose payee changed or is no longer verified, or whose
        influencer is blocked, is cancelled instead (its amount released), never handed over."""
        with self.lock:
            if pid in self._payout_inflight:
                return "in_flight"
            p = dict(self.payouts[pid])
            if p["status"] != "pending_finance" or self._closed:
                return p["status"]
            self._payout_inflight.add(pid)
        try:
            stale = None
            try:
                st = self.ports.finance.payee_status(p["payee_ref"])
            except Exception:  # noqa: BLE001
                st = None
            if st is None or st.status == "unavailable":
                return "pending_finance"
            if st.status != "verified":
                stale = "PAYEE_NOT_VERIFIED"
            with self.lock:
                if self.payouts[pid]["status"] != "pending_finance" or self._closed:
                    return self.payouts[pid]["status"]
                stale = stale or self._stale_payout(p)
                if stale:
                    try:
                        self._commit("payout_result", {"payout_id": pid, "status": "cancelled", "reason": stale},
                                     INTERNAL_ACTOR, evidence=("payout_result", f"payout:{pid}",
                                                               {"payout_id": pid, "status": "cancelled",
                                                                "reason": stale}, (pid, "cancelled")))
                    except Unavailable:
                        return "pending_finance"
                    return "cancelled"
            try:
                ans = self.ports.finance.request_payout(pid, p["payee_ref"], p["amount"], p["currency"], p["brand"],
                                                        p["deal_id"])
            except Exception:  # noqa: BLE001
                return "pending_finance"
            if ans.status not in ("accepted", "refused"):
                return "pending_finance"
            status = "submitted" if ans.status == "accepted" else "refused_by_finance"
            ref = ans.finance_ref if isinstance(ans.finance_ref, str) and 1 <= len(ans.finance_ref) <= 128 else None
            with self.lock:
                if self.payouts[pid]["status"] != "pending_finance":
                    return self.payouts[pid]["status"]
                try:
                    self._commit("payout_result", {"payout_id": pid, "status": status, "finance_ref": ref},
                                 INTERNAL_ACTOR, evidence=("payout_result", f"payout:{pid}",
                                                           {"payout_id": pid, "status": status,
                                                            "finance_ref_sha256": _rsha(ref) if ref else None},
                                                           (pid, status)))
                except Unavailable:
                    return "pending_finance"            # Finance holds it (idempotent); recorded at the next retry
                return status
        finally:
            with self.lock:
                self._payout_inflight.discard(pid)

    def payout_retry(self, body: dict) -> dict:
        """The ``payout-retry`` job: every payout still ``pending_finance`` is handed to Finance again."""
        with self.lock:
            self._gate()
            rk = self._rk("job", "payout-retry", body)
            prev = self._idem("scheduler", rk, body)
            if prev:
                return {"job": "payout-retry", "already_ran": True, **(prev[1] or {})}
            ids = [p["payout_id"] for p in self.payouts.values() if p["status"] == "pending_finance"]
        out = {"submitted": 0, "refused_by_finance": 0, "pending_finance": 0, "cancelled": 0, "in_flight": 0}
        for pid in ids:
            st = self._hand_to_finance(pid)
            out[st] = out.get(st, 0) + 1
        with self.lock:
            self._commit("job_ran", self._req({"job": "payout-retry"}, "scheduler", rk, body, out), "scheduler")
        return {"job": "payout-retry", **out}


INTERNAL_ACTOR = "influencer"
