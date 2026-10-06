"""Price books, proposals and the won-deal hand-offs (ADR 0013 decisions 14-15 and 19). A mixin of SalesService.

Every service line exists with NO price; nothing is quoted until Andre approves a price for that line (versioned;
the approval binds line id, version and price). Agents may build and send a proposal at approved list prices only
when it is at most SALES_AUTO_APPROVE_MAX, has no media-buy line, no discount and no custom term; anything else waits
for Andre's approval of that exact proposal (its content SHA-256). Before ``sent`` a Legal (37) contract in force is
required (stand-in: refused, LEGAL_UNAVAILABLE). A won deal hands off to Onboarding and Finance through ports
(stand-ins: ``pending_delivery``, retried by ``handoff-retry``)."""

from __future__ import annotations

from datetime import timedelta

import money
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from intelligences import i11_pricing
from ledger import derived_id
from reasons import R

PROPOSAL_VALID_DAYS = 30


class DealsMixin:
    def _init_pricebook(self) -> None:
        for lid, (brand, title, kind, line, unit) in i11_pricing.CATALOG.items():
            self.pricebook[lid] = {"line_id": lid, "brand": brand, "title": title, "kind": kind, "product_line": line,
                                   "unit": unit, "version": 0, "approved": None, "history": []}

    # ------------------------------------------------------------------------------------------------ replay

    def _a_price_approved(self, d, at):
        x = self.pricebook[d["line_id"]]
        appr = {"version": d["version"], "price": d.get("price"), "markup_pct": d.get("markup_pct"),
                "binding_sha256": d["binding_sha256"], "approved_at": at}
        x.update(version=d["version"], approved=appr)
        x["history"] = (x["history"] + [{"event": "approved", **appr}])[-50:]

    def _a_price_withdrawn(self, d, at):
        x = self.pricebook[d["line_id"]]
        x["approved"] = None
        x["history"] = (x["history"] + [{"event": "withdrawn", "version": d["version"], "at": at}])[-50:]

    def _a_proposal_created(self, d, at):
        p = {k: d[k] for k in ("proposal_id", "brand", "opportunity_id", "account_id", "lines", "subtotal", "discount",
                               "total", "custom_terms", "payment_methods", "valid_until", "content_sha256",
                               "needs_andre")}
        p.update(status=d["status"], approved_by=d.get("approved_by"), created_at=at, updated_at=at, sent_at=None,
                 contract=None, created_by=d["actor"], closed_reason=None)
        self.proposals[d["proposal_id"]] = p
        self.opps[d["opportunity_id"]]["proposal_ids"].append(d["proposal_id"])

    def _a_proposal_approved(self, d, at):
        self.proposals[d["proposal_id"]].update(status="approved", approved_by="andre", updated_at=at)

    def _a_proposal_sent(self, d, at):
        p = self.proposals[d["proposal_id"]]
        p.update(status="sent", sent_at=at, updated_at=at,
                 contract={"kind": d["contract_kind"], "ref": d["contract_ref"]})
        o = self.opps[p["opportunity_id"]]
        if o["stage"] not in ("closed_won", "closed_lost"):
            o["stage"], o["updated_at"] = "proposal", at

    def _a_proposal_won(self, d, at):
        p = self.proposals[d["proposal_id"]]
        p.update(status="won", updated_at=at)
        o = self.opps[p["opportunity_id"]]
        o["stage"], o["updated_at"] = "closed_won", at
        for h in d["handoffs"]:
            self.handoffs[h["handoff_id"]] = {**h, "status": "pending_delivery", "created_at": at,
                                              "delivered_at": None, "reference": None}

    def _a_proposal_lost(self, d, at):
        self.proposals[d["proposal_id"]].update(status="lost", closed_reason=d["reason_code"], updated_at=at)

    def _a_handoff_delivered(self, d, at):
        self.handoffs[d["handoff_id"]].update(status="delivered", delivered_at=at, reference=d.get("reference"))

    # ------------------------------------------------------------------------------------------------ price book

    def pricebook_view(self, brand: str) -> list[dict]:
        with self.lock:
            return [dict(self.pricebook[lid]) for lid in i11_pricing.lines_of(brand)]

    def approve_price(self, brand: str, line_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"price_approve|{line_id}|{body['request_id']}"
            if self._idem("andre", rk, body):
                return dict(self.pricebook[line_id])
            x = self.pricebook.get(line_id)
            if x is None or x["brand"] != brand:
                raise NotFound(R("LINE_UNKNOWN"))
            if body["version"] != x["version"] + 1:
                raise Conflict(R("PRICE_VERSION_STALE"))
            price = markup = None
            try:
                if x["kind"] == "media_buy":
                    if body.get("price") is not None or body.get("markup_pct") is None:
                        raise Invalid(R("MARKUP_REQUIRED"))
                    markup = f"{money.parse_pct(body['markup_pct']):f}"
                else:
                    if body.get("markup_pct") is not None or body.get("price") is None:
                        raise Invalid(R("PRICE_REQUIRED"))
                    price = money.fmt(money.parse(body["price"], positive=True))
            except money.MoneyError:
                raise Invalid(R("MONEY_INVALID")) from None
            binding = i11_pricing.approval_binding(line_id, body["version"], price, markup)
            data = {"line_id": line_id, "version": body["version"], "price": price, "markup_pct": markup,
                    "binding_sha256": binding}
            self._commit("price_approved", self._req(data, "andre", rk, body, line_id), "andre",
                         evidence=("price_approved", f"line:{line_id}", data, ("andre", rk)))
            return dict(x)

    def withdraw_price(self, brand: str, line_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"price_withdraw|{line_id}|{body['request_id']}"
            if self._idem("andre", rk, body):
                return dict(self.pricebook[line_id])
            x = self.pricebook.get(line_id)
            if x is None or x["brand"] != brand:
                raise NotFound(R("LINE_UNKNOWN"))
            if x["approved"] is None or x["approved"]["version"] != body["version"]:
                raise Conflict(R("PRICE_VERSION_STALE"))
            data = {"line_id": line_id, "version": body["version"]}
            self._commit("price_withdrawn", self._req(data, "andre", rk, body, line_id), "andre",
                         evidence=("price_withdrawn", f"line:{line_id}", data, ("andre", rk)))
            return dict(x)

    # ------------------------------------------------------------------------------------------------ proposals

    def proposal(self, pid: str) -> dict:
        with self.lock:
            return dict(self._get(self.proposals, pid, "PROPOSAL_NOT_FOUND"))

    def proposals_view(self, status) -> list[dict]:
        with self.lock:
            return [dict(p) for p in self.proposals.values() if status is None or p["status"] == status][:1000]

    @staticmethod
    def _proposal_doc(p: dict) -> dict:
        return {k: p[k] for k in ("proposal_id", "brand", "opportunity_id", "account_id", "lines", "subtotal",
                                  "discount", "total", "custom_terms", "payment_methods", "valid_until")}

    def _prices_current(self, p: dict) -> bool:
        for line in p["lines"]:
            appr = self.pricebook[line["line_id"]]["approved"]
            if appr is None or appr["version"] != line["price_version"]:
                return False
        return True

    def create_proposal(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"proposal_create|{body['opportunity_id']}|{body['request_id']}"
            prev = self._idem(caller, rk, body)
            if prev:
                return dict(self.proposals[prev[1]])
            o = self._get(self.opps, body["opportunity_id"], "OPPORTUNITY_NOT_FOUND")
            if o["stage"] in ("closed_won", "closed_lost"):
                raise Conflict(R("OPPORTUNITY_CLOSED"))
            try:
                committed = money.total(p["total"] for p in self.proposals.values()
                                        if p["opportunity_id"] == o["opportunity_id"]
                                        and p["status"] in ("approved", "sent", "won"))
                q = i11_pricing.compute(o["brand"], [dict(x) for x in body["lines"]], self.pricebook,
                                        body.get("discount", "0.00"), body.get("custom_terms"),
                                        self.settings.auto_approve_max, committed)
            except i11_pricing.QuoteProblem as exc:
                raise (Conflict if exc.code == "PRICE_NOT_APPROVED" else Invalid)(R(exc.code)) from None
            except money.MoneyError:
                raise Invalid(R("MONEY_INVALID")) from None
            pid = derived_id("prp", caller, rk)
            doc = {"proposal_id": pid, "brand": o["brand"], "opportunity_id": o["opportunity_id"],
                   "account_id": o["account_id"], "lines": q["lines"], "subtotal": q["subtotal"],
                   "discount": q["discount"], "total": q["total"], "custom_terms": body.get("custom_terms"),
                   "payment_methods": q["payment_methods"],
                   "valid_until": (self.now().date() + timedelta(days=PROPOSAL_VALID_DAYS)).isoformat()}
            sha = i11_pricing.proposal_sha256(doc)
            auto = not q["needs_andre"]
            data = {**doc, "content_sha256": sha, "needs_andre": q["needs_andre"],
                    "status": "approved" if auto else "pending_andre", "approved_by": "auto_rule" if auto else None}
            ev = ("proposal_approved", f"proposal:{pid}", {"proposal_id": pid, "content_sha256": sha,
                                                          "total": q["total"], "by": "auto_rule"}, (caller, rk)) \
                if auto else None
            self._commit("proposal_created", self._req(data, caller, rk, body, pid), caller, evidence=ev)
            return dict(self.proposals[pid])

    def approve_proposal(self, pid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"proposal_approve|{pid}|{body['request_id']}"
            if self._idem("andre", rk, body):
                return dict(self.proposals[pid])
            p = self._get(self.proposals, pid, "PROPOSAL_NOT_FOUND")
            if p["status"] != "pending_andre":
                raise Conflict(R("PROPOSAL_NOT_PENDING"))
            if body["content_sha256"] != p["content_sha256"] or \
                    i11_pricing.proposal_sha256(self._proposal_doc(p)) != p["content_sha256"]:
                raise Conflict(R("PROPOSAL_HASH_MISMATCH"))
            if not self._prices_current(p):
                raise Conflict(R("PRICE_CHANGED"))
            data = {"proposal_id": pid, "content_sha256": p["content_sha256"]}
            self._commit("proposal_approved", self._req(data, "andre", rk, body, pid), "andre",
                         evidence=("proposal_approved", f"proposal:{pid}", {**data, "total": p["total"], "by": "andre"},
                                   ("andre", rk)))
            return dict(p)

    def _sendable(self, p: dict) -> None:
        if p["status"] != "approved":
            raise Conflict(R("PROPOSAL_NOT_APPROVED"))
        if p["valid_until"] < self.today():
            raise Conflict(R("PROPOSAL_EXPIRED"))
        if i11_pricing.proposal_sha256(self._proposal_doc(p)) != p["content_sha256"]:
            raise Conflict(R("PROPOSAL_HASH_MISMATCH"))
        if not self._prices_current(p):
            raise Conflict(R("PRICE_CHANGED"))

    def send_proposal(self, caller: str, pid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"proposal_send|{pid}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return dict(self.proposals[pid])
            p = self._get(self.proposals, pid, "PROPOSAL_NOT_FOUND")
            self._sendable(p)
            sha, account_id = p["content_sha256"], p["account_id"]
        check = self.ports.legal.in_force(account_id, body["contract_kind"], body["contract_ref"])   # outside the lock
        if check.status == "unavailable":
            raise Unavailable(R("LEGAL_UNAVAILABLE"))
        if check.status != "in_force":
            raise Forbidden(R("CONTRACT_NOT_IN_FORCE"))
        with self.lock:
            self._gate()
            if self._idem(caller, rk, body):
                return dict(self.proposals[pid])
            self._sendable(p)
            if p["content_sha256"] != sha:
                raise Conflict(R("PROPOSAL_HASH_MISMATCH"))
            data = {"proposal_id": pid, "content_sha256": sha, "contract_kind": body["contract_kind"],
                    "contract_ref": body["contract_ref"]}
            self._commit("proposal_sent", self._req(data, caller, rk, body, pid), caller,
                         evidence=("proposal_sent", f"proposal:{pid}", data, (caller, rk)))
            return dict(p)

    def proposal_won(self, caller: str, pid: str, body: dict) -> dict:
        """AEGIS S1-L1: a won deal creates a client and an invoice draft, so it needs Andre (``caller == "andre"``,
        the FounderGate) or the client's acceptance of THIS proposal confirmed by Legal (stand-in: refused)."""
        rk = f"proposal_won|{pid}|{body['request_id']}"
        with self.lock:
            self._gate()
            if self._idem(caller, rk, body):
                return {**self.proposals[pid], "handoffs": self._handoffs_of(pid)}
            p = self._get(self.proposals, pid, "PROPOSAL_NOT_FOUND")
            if p["status"] != "sent":
                raise Conflict(R("PROPOSAL_NOT_SENT"))
            sha, account_id = p["content_sha256"], p["account_id"]
        acc = body.get("acceptance")
        if caller != "andre":
            if acc is None:
                raise Forbidden(R("ACCEPTANCE_REQUIRED"))
            check = self.ports.legal.accepted(account_id, pid, sha, acc["kind"], acc["ref"])   # outside the lock
            if check.status == "unavailable":
                raise Unavailable(R("LEGAL_UNAVAILABLE"))
            if check.status != "accepted":
                raise Forbidden(R("ACCEPTANCE_NOT_CONFIRMED"))
        with self.lock:
            self._gate()
            if self._idem(caller, rk, body):
                return {**self.proposals[pid], "handoffs": self._handoffs_of(pid)}
            if p["status"] != "sent" or p["content_sha256"] != sha:
                raise Conflict(R("PROPOSAL_NOT_SENT"))
            o = self.opps[p["opportunity_id"]]
            account = self.accounts.get(p["account_id"]) or {}       # S3-M2: never shadows the acceptance
            common = {"proposal_id": pid, "brand": p["brand"], "account_id": p["account_id"],
                      "contact_id": o["contact_id"], "content_sha256": p["content_sha256"]}
            handoffs = [
                {"handoff_id": derived_id("hof", "onboarding", pid), "kind": "onboarding_create_client",
                 "payload": {**common, "account_name": account.get("name"), "product_lines": o["product_lines"],
                             "contract": p["contract"]}},
                {"handoff_id": derived_id("hof", "finance", pid), "kind": "finance_invoice_draft",
                 "payload": {**common, "currency": "USD", "lines": [{"line_id": x["line_id"], "amount": x["amount"]}
                                                                    for x in p["lines"]],
                             "discount": p["discount"], "total": p["total"],
                             "payment_methods": p["payment_methods"]}}]
            won = {"proposal_id": pid, "handoffs": handoffs, "by": caller, "acceptance": acc}
            self._commit("proposal_won", self._req(won, caller, rk, body, pid), caller,
                         evidence=("proposal_won", f"proposal:{pid}", {"proposal_id": pid, "content_sha256": sha,
                                                                       "by": caller, "acceptance": acc}, (caller, rk)))
            ids = [h["handoff_id"] for h in handoffs]
        for hid in ids:
            self._deliver(hid)
        with self.lock:
            return {**self.proposals[pid], "handoffs": self._handoffs_of(pid)}

    def proposal_lost(self, caller: str, pid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"proposal_lost|{pid}|{body['request_id']}"
            if self._idem(caller, rk, body):
                return dict(self.proposals[pid])
            p = self._get(self.proposals, pid, "PROPOSAL_NOT_FOUND")
            if p["status"] not in ("pending_andre", "approved", "sent"):
                raise Conflict(R("PROPOSAL_CLOSED"))
            self._commit("proposal_lost", self._req({"proposal_id": pid, "reason_code": body["reason_code"]}, caller,
                                                    rk, body, pid), caller)
            return dict(p)

    # ------------------------------------------------------------------------------------------------ hand-offs

    def _handoffs_of(self, pid: str) -> list[dict]:
        return [self._handoff_view(h) for h in self.handoffs.values() if h["payload"]["proposal_id"] == pid]

    def _handoff_view(self, h: dict) -> dict:
        return {**{k: h[k] for k in ("handoff_id", "kind", "status", "created_at", "delivered_at", "reference")},
                "proposal_id": h["payload"]["proposal_id"], **self.handoff_attempts.get(h["handoff_id"], {})}

    def handoffs_view(self) -> list[dict]:
        with self.lock:
            return [self._handoff_view(h) for h in self.handoffs.values()]

    def _deliver(self, hid: str) -> str:
        with self.lock:
            h = self.handoffs[hid]
            if h["status"] != "pending_delivery":
                return h["status"]
            port = self.ports.onboarding if h["kind"] == "onboarding_create_client" else self.ports.finance
            payload = dict(h["payload"])
        try:
            res = port.deliver(hid, payload)
            status, ref = res.status, res.reference
        except Exception:          # an adapter error is an undelivered hand-off, retried later
            status, ref = "unavailable", None
        with self.lock:
            att = self.handoff_attempts.setdefault(hid, {"attempts": 0, "last_result": None})
            att["attempts"] += 1
            att["last_result"] = status
            if status == "delivered":
                try:
                    self._commit("handoff_delivered", {"handoff_id": hid, "reference": ref}, "sales")
                except Unavailable:
                    return "pending_delivery"     # the adapter is idempotent on handoff_id: the retry re-delivers
            return self.handoffs[hid]["status"]

    def retry_handoffs(self, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = f"job|handoff-retry|{body['request_id']}"
            prev = self._idem("scheduler", rk, body)
            if prev:
                return {"job": "handoff-retry", "already_ran": True, **(prev[1] or {})}
            ids = sorted(h["handoff_id"] for h in self.handoffs.values() if h["status"] == "pending_delivery")
        results = {hid: self._deliver(hid) for hid in ids}
        summary = {"delivered": sum(1 for v in results.values() if v == "delivered"),
                   "pending_delivery": sum(1 for v in results.values() if v == "pending_delivery")}
        with self.lock:
            self._commit("job_ran", self._req({"job": "handoff-retry"}, "scheduler", rk, body, summary), "scheduler")
        return {"job": "handoff-retry", **summary}
