"""Test harness: an in-process service over the real ASGI stack, a fake ledger with ledger-rust's contract (seo-py's
FakeLedger), a settable fixed clock, and builders that walk a campaign to any point of the lifecycle through the API
only. Every token is DERIVED (sha256 of a test-only label), never written as a secret-shaped literal. Nothing here is
importable from src/."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.testclient import TestClient

import api
import config as config_mod
from clock import FixedClock, iso
from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, LedgerRecordError, Recorder, payload_sha256
from service import OohService
from store import RecordLog


def derived(label: str) -> str:
    return hashlib.sha256(f"ooh-py test-only {label}".encode()).hexdigest()


SERVICE_TOKEN = derived("service token")
ANDRE = derived("andre approval token")
CALLERS = {c: derived(f"caller token {c}") for c in config_mod.KNOWN_CALLERS}
TENANT_TOKENS = {t: derived(f"tenant token {t}") for t in ("acme", "globex")}
T0 = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)
FLIGHT = ("2026-11-01", "2026-11-30")
VENDORS = {"lamar": "Lamar Advertising Company", "outfront": "Outfront Media", "clear-channel": "Clear Channel Outdoor"}


def policy_formats() -> tuple:
    import policy
    return policy.FORMATS


def rid() -> str:
    return str(uuid.uuid4())


def at(**kw) -> str:
    return iso(T0 + timedelta(**kw))


class FakeLedger:
    """ledger-rust's contract: idempotent on identical content, 409 on a different record under the same id.
    ``lose_anchors``: that many ``log_anchor`` records ARE recorded but their answers are lost (the client sees an
    unknown outcome: a timeout, which is never a failure)."""

    def __init__(self):
        self.events: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.fail = False
        self.fail_reads = False
        self.lose_anchors = 0
        self.verify_result = True

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        if self.fail:
            raise LedgerNotRecorded("fake ledger down")
        e = {"kind": "event", "seq": len(self.events) + 1, "event_id": event_id, "department": department,
             "event_type": event_type, "actor": actor, "subject_id": subject_id,
             "payload_sha256": payload_sha256(payload), "summary": summary, "_payload": payload}
        prev = self.by_id.get(event_id)
        if prev is not None:
            if {k: prev[k] for k in e if k not in ("seq", "_payload")} != \
                    {k: e[k] for k in e if k not in ("seq", "_payload")}:
                raise LedgerConflict("409")
        else:
            self.events.append(e)
            self.by_id[event_id] = e
        if event_type == "log_anchor" and self.lose_anchors:
            self.lose_anchors -= 1
            raise LedgerRecordError("answer lost (the ledger recorded it)")

    def entries(self) -> list[dict]:
        if self.fail_reads:
            raise LedgerQueryFailed("fake ledger unreadable")
        return [{k: v for k, v in e.items() if k != "_payload"} for e in self.events]

    def verify(self) -> bool:
        return self.verify_result and not self.fail_reads

    def of_type(self, t: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == t]


def base_env(**over) -> dict:
    env = {"OOH_SERVICE_TOKEN": SERVICE_TOKEN, "OOH_NON_PRODUCTION": "1", "OOH_ANDRE_APPROVAL_TOKEN": ANDRE,
           "OOH_CALLER_TOKENS": json.dumps(CALLERS), "OOH_TENANT_TOKENS": json.dumps(TENANT_TOKENS)}
    for k, v in over.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


class Harness:
    def __init__(self, tmp, data_dir: Optional[str] = None, ledger: Optional[FakeLedger] = None,
                 clock: Optional[FixedClock] = None, **env_over):
        self.tmp = tmp
        self.env = base_env(OOH_DATA_DIR=data_dir, **env_over)
        self.settings = config_mod.load(self.env)
        self.ledger = ledger or FakeLedger()
        self.clock = clock or FixedClock(T0)
        lock = self.settings.data_dir_lock
        token = lock.claim() if lock is not None else None
        try:
            self.svc = OohService(self.settings, Recorder(self.ledger), RecordLog(self.settings.data_dir), self.clock,
                                  lock_token=token)
        except BaseException:
            if lock is not None:
                lock.release_claim(token)
            raise
        self.client = TestClient(api._wrap(api.create_app(self.svc, self.settings)), raise_server_exceptions=False)

    def restart(self, **env_over) -> "Harness":
        self.svc.close()
        return Harness(self.tmp, self.settings.data_dir, self.ledger, self.clock, **env_over)

    # ------------------------------------------------------------------ raw calls

    def headers(self, caller: Optional[str] = "dashboard", andre: bool = False, tenant: Optional[str] = None) -> dict:
        h = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        if caller:
            h[api.CALLER_HEADER] = CALLERS[caller]
        if andre:
            h["X-Andre-Approval-Token"] = ANDRE
        if tenant:
            h[api.TENANT_HEADER] = TENANT_TOKENS[tenant]
        return h

    def post(self, path: str, body: Optional[dict] = None, caller: Optional[str] = "ooh_agent", andre: bool = False,
             tenant: Optional[str] = None):
        if andre and caller == "ooh_agent":
            caller = "dashboard"
        return self.client.post("/ooh/v1" + path, json=body if body is not None else {},
                                headers=self.headers(caller, andre, tenant))

    def get(self, path: str, caller: Optional[str] = "dashboard", tenant: Optional[str] = None, **kw):
        return self.client.get("/ooh/v1" + path, headers=self.headers(caller, tenant=tenant), **kw)

    @staticmethod
    def ok(resp, code: int = 200):
        assert resp.status_code == code, (resp.status_code, resp.text)
        return resp.json()

    @staticmethod
    def refused(resp, code: int, detail: Optional[str] = None):
        assert resp.status_code == code, (resp.status_code, resp.text)
        if detail is not None:
            assert resp.json()["detail"] == detail, resp.text
        return resp.json()

    def switch(self, name: str, engaged: bool = True, andre: bool = False, caller: str = "dashboard"):
        return self.post("/kill-switches", {"request_id": rid(), "switch": name, "engaged": engaged},
                         caller=caller, andre=andre)

    # ------------------------------------------------------------------ builders (through the API only)

    def tenant(self, tid: str = "acme") -> dict:
        t = self.ok(self.post("/tenants", {"request_id": rid(), "tenant_id": tid}, andre=True), 201)
        if self.svc.pa_determination is None:      # the suites buy as PRINCIPAL: counsel's (test) confirmation first
            self.determine("PRINCIPAL")
        if not self.svc.flag_met("los-angeles", "EXECUTION_AUTHORIZED"):   # the suites plan and buy in Los Angeles
            self.ready("los-angeles", "PLANNING_COVERAGE", "EXECUTION_AUTHORIZED")
        for vid, name in VENDORS.items():           # Andre's vendor registry (global): the vendors the suites use
            if vid not in self.svc.vendors:
                self.vendor(vid, name)
        return t

    def determine(self, determination: str, confirmed_by: str = "COUNSEL", ref: str = "counsel-memo-test-1"):
        return self.ok(self.post("/principal-agent-determinations",
                                 {"request_id": rid(), "determination": determination, "confirmed_by": confirmed_by,
                                  "confirmation_ref": ref}, andre=True))

    def ready(self, market: str, *flags: str, value: str = "MET", andre: bool = True, caller: str = "dashboard"):
        out = None
        for f in flags:
            out = self.post(f"/markets/{market}/readiness", {"request_id": rid(), "flag": f, "value": value,
                                                            "evidence_ref": f"readiness-{market}-{f.lower()}"},
                            caller=caller, andre=andre)
            if out.status_code != 200:
                return out
        return out

    def source(self, vendor: str = "lamar", kind: str = "RATE_CARD", dated: Optional[str] = None,
               caller: str = "ooh_agent", doc: Optional[str] = None) -> str:
        r = self.post("/sources", {"request_id": rid(), "kind": kind, "vendor": vendor,
                                   "doc_ref": doc or f"{vendor}-ratecard-2026", "dated": dated or at(days=-1)},
                      caller=caller)
        return self.ok(r, 201)["source_id"]

    def face(self, vendor: str = "lamar", ref: Optional[str] = None, fmt: str = "STATIC_BILLBOARD",
             market: str = "los-angeles") -> str:
        """The registry face for (vendor, ref): registered from a rate card if new, else the existing one (a respelt
        reference finds the same face)."""
        ref = ref or f"face-{uuid.uuid4().hex[:8]}"
        r = self.post("/faces", {"request_id": rid(), "source_id": self.source(vendor), "vendor_face_ref": ref,
                                 "format": fmt, "market": market})
        if r.status_code == 409 and r.json().get("detail") == "FACE_EXISTS":
            found = self.ok(self.get("/faces", params={"vendor": vendor, "vendor_face_ref": ref}))
            return found[0]["face_id"]
        return self.ok(r, 201)["face_id"]

    def vendor(self, vid: str, name: str):
        return self.ok(self.post("/vendors", {"request_id": rid(), "vendor_id": vid, "legal_name": name}, andre=True),
                       201)

    def campaign(self, tid: str = "acme") -> str:
        c = self.ok(self.post(f"/tenants/{tid}/campaigns", {"request_id": rid(), "name": "Fall Billboards",
                                                            "markets": ["los-angeles"]}), 201)
        self.ok(self.brief(tid, c["campaign_id"]))          # OOH-3: every suite plans against a client brief
        return c["campaign_id"]

    def brief(self, tid: str, cid: str, **over):
        c = self.view(tid, cid)
        body = {"request_id": rid(), "expected_version": c["version"], "budget_cap": "50000.00", "currency": "USD",
                "formats": list(policy_formats()), "flight_window_start": "2026-10-16",
                "flight_window_end": "2027-12-31", "brief_doc_ref": "acme-brief-1", **over}
        return self.post(f"/tenants/{tid}/campaigns/{cid}/brief", body)

    def markup_evidence(self, tid: str, cid: str, kind: str = "COMPARABLE_DEALS") -> str:
        c = self.ok(self.post(f"/tenants/{tid}/campaigns/{cid}/markup-evidence",
                              {"request_id": rid(), "kind": kind, "doc_ref": "comparable-deals-2026"}), 201)
        return c["markup_evidence"][-1]["evidence_id"]

    def view(self, tid: str, cid: str) -> dict:
        return self.ok(self.get(f"/tenants/{tid}/campaigns/{cid}"))

    def go(self, tid: str, cid: str, to: str, andre: bool = False, reason: str = "TEST_STEP"):
        c = self.view(tid, cid)
        return self.post(f"/tenants/{tid}/campaigns/{cid}/transitions",
                         {"request_id": rid(), "to": to, "expected_state": c["state"],
                          "expected_version": c["version"], "reason": reason}, andre=andre)

    def plan(self, tid: str, cid: str) -> None:
        """INTAKE -> BRIEF_VALIDATED -> PLAN_DRAFTED -> PLAN_APPROVED (Andre approves the plan)."""
        for to in ("BRIEF_VALIDATED", "PLAN_DRAFTED"):
            self.ok(self.go(tid, cid, to))
        self.ok(self.go(tid, cid, "PLAN_APPROVED", andre=True))

    def unit(self, tid: str, cid: str, vendor: str = "lamar", text: Optional[str] = None,
             ref: Optional[str] = None, flight: Optional[tuple] = FLIGHT) -> str:
        body = {"request_id": rid(), "face_id": self.face(vendor, ref)}
        if flight is not None:
            body.update(flight_start=flight[0], flight_end=flight[1])
        if text is not None:
            body["vendor_text"] = text
        return self.ok(self.post(f"/tenants/{tid}/campaigns/{cid}/inventory", body), 201)["unit_id"]

    def move(self, tid: str, cid: str, uid: str, to: str, **extra):
        u = self.ok(self.get(f"/tenants/{tid}/campaigns/{cid}/inventory/{uid}"))
        return self.post(f"/tenants/{tid}/campaigns/{cid}/inventory/{uid}/availability",
                         {"request_id": rid(), "to": to, "expected_state": u["state"],
                          "expected_version": u["version"], **extra})

    def unit_get(self, tid: str, cid: str, uid: str) -> dict:
        return self.ok(self.get(f"/tenants/{tid}/campaigns/{cid}/inventory/{uid}"))

    def hold_evidence(self, tid: str, cid: str, uid: str, caller: str = "ooh_agent", vendor_ref: str = "lamar-hold-77",
                      expires: Optional[str] = None, doc: str = "lamar-email-hold-77", **extra):
        """An agent attaches the vendor's hold evidence for Andre's decision."""
        u = self.unit_get(tid, cid, uid)
        body = {"request_id": rid(), "expected_version": u["version"], "vendor_ref": vendor_ref,
                "hold_expires_at": expires or at(days=7), "evidence_doc_ref": doc, **extra}
        return self.post(f"/tenants/{tid}/campaigns/{cid}/inventory/{uid}/hold-evidence", body, caller=caller)

    def hold_decide(self, tid: str, cid: str, uid: str, evidence_id: Optional[str] = None, decision: str = "CONFIRM",
                    andre: bool = True, caller: str = "dashboard", **extra):
        """Andre's decision on a hold-evidence record (the unit's latest one unless named)."""
        u = self.unit_get(tid, cid, uid)
        eid = evidence_id or u["hold_evidence"][-1]["evidence_id"]
        body = {"request_id": rid(), "expected_version": u["version"], "evidence_id": eid, "decision": decision,
                **extra}
        return self.post(f"/tenants/{tid}/campaigns/{cid}/inventory/{uid}/hold-decisions", body, caller=caller,
                         andre=andre)

    def confirm_hold(self, tid: str, cid: str, uid: str, vendor_ref: str = "lamar-hold-77",
                     expires: Optional[str] = None) -> dict:
        """Evidence by the agent, then Andre's confirmation: the one way to HOLD_CONFIRMED."""
        self.ok(self.hold_evidence(tid, cid, uid, vendor_ref=vendor_ref, expires=expires), 201)
        return self.ok(self.hold_decide(tid, cid, uid))

    def price(self, tid: str, cid: str, price_type: str, amount: str, andre: bool = False, **extra):
        body = {"request_id": rid(), "price_type": price_type, "amount": amount, "currency": "USD", **extra}
        return self.post(f"/tenants/{tid}/campaigns/{cid}/prices", body, andre=andre)

    def quote(self, tid: str, cid: str, uids: list, amount: str = "5000.00", vendor: str = "lamar",
              valid_until: Optional[str] = None, **extra) -> str:
        r = self.price(tid, cid, "VENDOR_QUOTE", amount, vendor=vendor, inventory_ids=uids,
                       valid_until=valid_until or at(days=10), source_ref="lamar-q-1", **extra)
        return self.ok(r, 201)["price_id"]

    def commercial(self, tid: str, cid: str, markup: str = "20.00", pa: str = "PRINCIPAL", **over):
        c = self.view(tid, cid)
        prior = self.view(tid, cid)["markup_evidence"]
        basis = over.pop("markup_basis_ref", None) or (prior[-1]["evidence_id"] if prior
                                                       else self.markup_evidence(tid, cid))
        c = self.view(tid, cid)
        body = {"request_id": rid(), "expected_version": c["version"], "principal_agent_state": pa,
                "pricing_method": "DISCLOSED_MARKUP", "markup_pct": markup, "markup_disclosed": True,
                "markup_basis_ref": basis, "presentation": "COST_AND_FEE_BROKEN_OUT",
                "cancellation_terms_ref": "lamar-cxl-30d", **over}
        return self.post(f"/tenants/{tid}/campaigns/{cid}/commercial-model", body, andre=True)

    def held_unit(self, tid: str, cid: str, vendor: str = "lamar", amount: str = "5000.00",
                  valid_until: Optional[str] = None, ref: Optional[str] = None,
                  flight: tuple = FLIGHT) -> tuple[str, str]:
        """A unit walked IDENTIFIED -> HOLD_CONFIRMED with its vendor quote; returns (unit id, quote id)."""
        uid = self.unit(tid, cid, vendor, ref=ref, flight=flight)
        for to in ("VERIFIED", "AVAILABILITY_REQUESTED"):
            self.ok(self.move(tid, cid, uid, to))
        qid = self.quote(tid, cid, [uid], amount, vendor, valid_until)
        for to in ("QUOTED", "HOLD_REQUESTED"):
            self.ok(self.move(tid, cid, uid, to))
        self.confirm_hold(tid, cid, uid)
        return uid, qid

    def to_proposal_sent(self, tid: str = "acme", amount: str = "5000.00", markup: str = "20.00",
                         valid_until: Optional[str] = None, ref: Optional[str] = None,
                         flight: tuple = FLIGHT) -> dict:
        """A campaign at PROPOSAL_SENT with one held unit, its quote, the commercial model and the sell price."""
        cid = self.campaign(tid)
        self.plan(tid, cid)
        uid, qid = self.held_unit(tid, cid, amount=amount, valid_until=valid_until, ref=ref, flight=flight)
        self.ok(self.go(tid, cid, "AVAILS_REQUESTED"))
        self.ok(self.commercial(tid, cid, markup))
        from decimal import Decimal

        import policy
        sell = policy.fmt(policy.apply_markup(Decimal(amount), Decimal(markup)))
        sp = self.ok(self.price(tid, cid, "CLIENT_SELL_PRICE", sell, andre=True, quote_refs=[qid]), 201)
        self.ok(self.price(tid, cid, "APPROVED_BUY_LIMIT", amount, andre=True), 201)
        self.ok(self.go(tid, cid, "QUOTES_VALIDATED"))
        self.ok(self.go(tid, cid, "PROPOSAL_SENT", andre=True))
        return {"tid": tid, "cid": cid, "uid": uid, "qid": qid, "sell_id": sp["price_id"], "sell": sell,
                "amount": amount}

    def approve(self, ctx: dict, **over):
        c = self.view(ctx["tid"], ctx["cid"])
        body = {"request_id": rid(), "expected_version": c["version"], "sell_price_id": ctx["sell_id"],
                "client_approval_doc_ref": "io-acme-signed-1", **over}
        return self.post(f"/tenants/{ctx['tid']}/campaigns/{ctx['cid']}/client-approval", body, andre=True)

    def funds(self, ctx: dict, amount: str, ref: Optional[str] = None, **over):
        c = self.view(ctx["tid"], ctx["cid"])
        body = {"request_id": rid(), "expected_version": c["version"], "amount": amount, "currency": "USD",
                "method": "ach", "evidence_ref": ref or f"boa-ach-{uuid.uuid4().hex[:10]}", "cleared": True, **over}
        return self.post(f"/tenants/{ctx['tid']}/campaigns/{ctx['cid']}/funds-verifications", body, andre=True)

    def funded(self, tid: str = "acme", amount: str = "5000.00", valid_until: Optional[str] = None,
               ref: Optional[str] = None, flight: tuple = FLIGHT) -> dict:
        """A campaign at FUNDS_VERIFIED: approved by the client and 100% prepaid."""
        ctx = self.to_proposal_sent(tid, amount, valid_until=valid_until, ref=ref, flight=flight)
        self.ok(self.approve(ctx))
        self.ok(self.funds(ctx, ctx["sell"]))
        c = self.view(tid, ctx["cid"])
        assert c["state"] == "FUNDS_VERIFIED", c["state"]
        ctx.update(approval_id=c["client_approval"]["approval_id"], terms_id=c["commercial"]["terms_id"])
        return ctx

    def auth_body(self, ctx: dict, **over) -> dict:
        c = self.view(ctx["tid"], ctx["cid"])
        body = {"request_id": rid(), "expected_version": c["version"], "inventory_ids": [ctx["uid"]],
                "vendor": "lamar", "exact_quote_refs": [ctx["qid"]], "authorized_amount": ctx["amount"],
                "authorized_currency": "USD", "client_approval_ref": ctx["approval_id"],
                "commercial_terms_ref": ctx["terms_id"], "cancellation_terms_ref": "lamar-cxl-30d",
                "signer": "andre", "authority_scope": "OOH_PURCHASE", "expires_at": at(days=2)}
        body.update(over)
        return body

    def authorize(self, ctx: dict, **over):
        return self.post(f"/tenants/{ctx['tid']}/campaigns/{ctx['cid']}/authorizations", self.auth_body(ctx, **over),
                         andre=True)

    def authorized(self, tid: str = "acme", amount: str = "5000.00", valid_until: Optional[str] = None,
                   ref: Optional[str] = None, flight: tuple = FLIGHT) -> dict:
        ctx = self.funded(tid, amount, valid_until, ref=ref, flight=flight)
        a = self.ok(self.authorize(ctx), 201)
        ctx.update(aid=a["authorization_id"], auth=a)
        return ctx

    def submit_body(self, ctx: dict, **over) -> dict:
        a = self.ok(self.get(f"/tenants/{ctx['tid']}/campaigns/{ctx['cid']}/authorizations/{ctx['aid']}"))
        body = {"request_id": rid(), "vendor": a["vendor"], "amount": a["authorized_amount"],
                "currency": a["authorized_currency"], "quote_refs": list(a["exact_quote_refs"]),
                "campaign_version": a["campaign_version"], "channel": "MANUAL_BY_ANDRE"}
        body.update(over)
        return body

    def submit(self, ctx: dict, body: Optional[dict] = None, **over):
        return self.post(f"/tenants/{ctx['tid']}/campaigns/{ctx['cid']}/authorizations/{ctx['aid']}/submit",
                         body or self.submit_body(ctx, **over), andre=True)

    def outcome(self, ctx: dict, outcome: str = "ACCEPTED", **over):
        body = {"request_id": rid(), "outcome": outcome, "vendor_ref": "lamar-io-9001", **over}
        return self.post(f"/tenants/{ctx['tid']}/campaigns/{ctx['cid']}/authorizations/{ctx['aid']}/outcome", body,
                         andre=True)
