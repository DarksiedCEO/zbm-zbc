"""Test harness (no network: ports are fakes from fakes.py or the production stand-ins; the ledger is FakeLedgerClient)."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi.testclient import TestClient

import api
import config as config_mod
from clock import FixedClock
from fakes import FakeCompliance, FakeCounsel, FakeCyber, FakeDept, FakeESign, FakeLedgerClient
from ports import Ports

# 17:00 UTC = 10:00 in Los Angeles (LEGAL_BUSINESS_TZ): the calendar date is the same in both.
NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)
SERVICE_TOKEN = "test-legal-service-token-do-not-use-0123"
ANDRE_TOKEN = "test-andre-approval-token-legal-do-not-use-01"
CALLERS = {n: f"test-legal-caller-{n}-0123456789abcdefghij" for n in config_mod.CALLER_NAMES}
LINK_TAG_KEY = "test-link-tag-key-shared-with-the-portal-do-not-use"   # ADR 0028, AEGIS r2 N-M1
COUNSEL_REF = "eng-counsel-1"
_ids = itertools.count(1)


def rid(prefix: str = "r") -> str:
    return f"{prefix}-{next(_ids)}"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def base_env(**over) -> dict:
    env = {"LEGAL_SERVICE_TOKEN": SERVICE_TOKEN, "LEGAL_ANDRE_APPROVAL_TOKEN": ANDRE_TOKEN,
           "LEGAL_CALLER_TOKENS": json.dumps(CALLERS), "LEGAL_LINK_TAG_KEY": LINK_TAG_KEY}
    env.update({k: v for k, v in over.items() if v is not None})
    return {k: v for k, v in env.items() if v != "__unset__"}


_LIVE: dict = {}      # realpath(data dir) -> the Harness whose service holds it (bug sweep E)


class Harness:
    def __init__(self, env: Optional[dict] = None, data_dir: Optional[str] = None, clock: Optional[FixedClock] = None,
                 ledger: Optional[FakeLedgerClient] = None, wired: bool = False, ports: Optional[Ports] = None):
        self.clock = clock or FixedClock(NOW)
        self.ledger = ledger if ledger is not None else FakeLedgerClient()
        if ports is None:
            ports = Ports(compliance=FakeCompliance())
            if wired:
                ports.counsel = FakeCounsel()
                ports.cybersecurity_22 = FakeCyber()
                ports.esign = FakeESign()
                for n in ("people_43", "clipper_network", "creative_production", "finance_31", "onboarding",
                          "verification_integrity", "push"):
                    setattr(ports, n, FakeDept(n))
        self.ports = ports
        self.compliance = ports.compliance
        e = base_env(**(env or {}))
        if data_dir:
            e["LEGAL_DATA_DIR"] = data_dir
        self.env = e
        if data_dir:
            # bug sweep E: one service instance per data directory (DataDirLock claim). A new Harness on the same
            # directory is a restart: the old instance is closed first (as bizdev-py's Harness.restart does).
            old = _LIVE.pop(os.path.realpath(data_dir), None)
            if old is not None:
                old.svc.close()
        self.settings = config_mod.load(e)
        self.svc = api.build_service(self.settings, self.clock, ports, self.ledger)
        if data_dir:
            _LIVE[os.path.realpath(data_dir)] = self
        self.app = api.create_app(self.svc, self.settings)
        self.client = TestClient(self.app)

    # --- raw HTTP ---------------------------------------------------------------------------------------------
    def headers(self, caller=None, andre=None, bearer=SERVICE_TOKEN):
        hd = {}
        if bearer is not None:
            hd["Authorization"] = f"Bearer {bearer}"
        if caller:
            hd["X-LEGAL-Caller-Token"] = CALLERS.get(caller, caller)
        if andre is not None:
            hd["X-Andre-Approval-Token"] = andre
        return hd

    def post(self, path, body, caller=None, andre=None, bearer=SERVICE_TOKEN):
        return self.client.post(path, json=body, headers=self.headers(caller, andre, bearer))

    def put(self, path, body, caller=None, andre=None):
        return self.client.put(path, json=body, headers=self.headers(caller, andre))

    def apost(self, path, body):
        return self.post(path, body, andre=ANDRE_TOKEN)

    def get(self, path, caller="scheduler", **params):
        return self.client.get(path, headers=self.headers(caller), params=params)

    def ok(self, r, code=200):
        assert r.status_code == code, (r.status_code, r.text[:3000])
        return r.json()

    # --- rules -------------------------------------------------------------------------------------------------
    def rules(self):
        return self.ok(self.get("/legal/v1/rules"))

    def approve_rules(self):
        seed = [p for p in self.rules()["open_proposals"] if p["kind"] == "seed"][0]
        return self.ok(self.apost("/legal/v1/rules/decisions", {"request_id": rid("dec"), "decisions": [
            {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]}))

    # --- documents -----------------------------------------------------------------------------------------------
    def upload(self, doc_id, text, version="1.0", entity="zbc", clause_ids=(), template_variables=None, code=201,
               party_ref=None):
        """Andre's upload. Legal assigns the number (AEGIS N17-5): ``version`` is what the test EXPECTS; a new
        major number is asked for with ``bump: major``; the assigned number is asserted."""
        body = {"request_id": rid("up"), "entity": entity, "text": text,
                "clause_ids": [{"clause_id": c, "position": p} for c, p in clause_ids]}
        if version.endswith(".0") and version != "1.0":
            body["bump"] = "major"
        if template_variables is not None:
            body["template_variables"] = template_variables
        if party_ref is not None:
            body["party_ref"] = party_ref
        out = self.ok(self.apost(f"/legal/v1/documents/{doc_id}/versions", body), code)
        if code == 201:
            assert out["version"] == version, (out["version"], version)
        return out

    def fill(self, doc_id, variables, party_ref, entity="zbm", expect=None, code=201, caller="scheduler"):
        """A scheduler fill of the current template, bound to ``party_ref`` (AEGIS N17-4)."""
        out = self.ok(self.post(f"/legal/v1/documents/{doc_id}/versions",
                                {"request_id": rid("fill"), "entity": entity, "variables": variables,
                                 "party_ref": party_ref}, caller=caller), code)
        if expect is not None and code == 201:
            assert out["version"] == expect, (out["version"], expect)
        return out

    def engage(self, counsel_ref=COUNSEL_REF, version="1.0"):
        """Engagement letter with the AI clause: countersigned by counsel, approved by Andre (LG-17)."""
        v = self.upload("engagement_letter", f"Engagement letter {counsel_ref} v{version}", version, "zbm",
                        [("ENG-AI-01", "standard")])
        self.ok(self.apost(f"/legal/v1/documents/engagement_letter/versions/{version}/counsel-signoff",
                           {"request_id": rid("cs"), "counsel_ref": counsel_ref, "signed_on": "2026-10-01",
                            "doc_sha256": v["sha256"], "countersignature_b64": b64(b"countersigned " + counsel_ref.encode())}))
        return self.ok(self.apost(f"/legal/v1/documents/engagement_letter/versions/{version}/decision",
                                  {"request_id": rid("ap"), "decision": "approve", "version_sha256": v["sha256"]}))

    def memo(self, content=None, counsel_ref=COUNSEL_REF, memo_date="2026-10-01", code=201, **parts):
        cites = {"cq_ids": [], "obligation_ids": [], "doc_versions": [], "clause_ids": [], "retention_classes": [],
                 "signoff_topics": []}
        cites.update(parts.pop("cites", {}))
        body = {"request_id": rid("memo"), "counsel_ref": counsel_ref, "memo_date": memo_date,
                "content_b64": b64(content or f"memo {next(_ids)}".encode()), "cites": cites,
                "answers": parts.pop("answers", []),
                "retention_periods": parts.pop("retention_periods", {}), "signoff_scopes": parts.pop("signoff_scopes", {})}
        assert not parts, parts
        return self.ok(self.apost("/legal/v1/memos", body), code)

    def verify_cq(self, *cq_ids):
        return self.memo(cites={"cq_ids": list(cq_ids)},
                         answers=[{"cq_id": c, "resolution": "verified_rule"} for c in cq_ids])

    def memo_proposals(self, memo_id, proposals, code=201):
        """Step 2 (AEGIS N17-8): Compliance proposals backed by an already-filed memo."""
        return self.ok(self.apost(f"/legal/v1/memos/{memo_id}/compliance-proposals",
                                  {"request_id": rid("mpr"), "proposals": proposals}), code)

    def approve_doc(self, doc_id, text, version="1.0", entity="zbc", clause_ids=(), template_variables=None,
                    effective_at=None, party_ref=None):
        v = self.upload(doc_id, text, version, entity, clause_ids, template_variables, party_ref=party_ref)
        d = self.ok(self.get(f"/legal/v1/documents/{doc_id}"))
        if d["counsel_required"]:
            memo = self.memo(cites={"doc_versions": [f"{doc_id}@{version}"],
                                    "clause_ids": sorted({c for c, _ in clause_ids})})
            self.ok(self.apost(f"/legal/v1/documents/{doc_id}/versions/{version}/counsel-signoff",
                               {"request_id": rid("so"), "counsel_ref": COUNSEL_REF, "signed_on": "2026-10-01",
                                "doc_sha256": v["sha256"], "memo_id": memo["memo_id"], "memo_sha256": memo["memo_sha256"]}))
        body = {"request_id": rid("ap"), "decision": "approve", "version_sha256": v["sha256"]}
        if effective_at:
            body["effective_at"] = effective_at
        self.ok(self.apost(f"/legal/v1/documents/{doc_id}/versions/{version}/decision", body))
        return v

    def clickwrap(self, doc_id, version, doc_sha, party="clipper:cn-clp-1", caller="clipper_network", code=201, **over):
        body = {"request_id": rid("acc"), "party_ref": party, "signer_identity_ref": party.split(":", 1)[1],
                "doc_id": doc_id, "version": version, "doc_sha256": doc_sha, "presented_sha256": doc_sha,
                "method": "clickwrap_unticked_box", "presentation": "scroll_to_accept", "affirmative_act": True}
        if doc_id == "clipper_agreement":
            body["esign_consent"] = {"disclosure_version": "1.0", "consented_at": "2026-10-01T16:59:00Z",
                                     "access_demonstrated": True}
        # AEGIS L-5: no silent rerouting. A client's acceptance is recorded only through the client acceptance page;
        # a test that means the page calls client_page_accept, and a test of a refusal names another caller
        assert not (caller == "onboarding" and party.startswith("client:")), \
            "a client's acceptance goes through the page: call client_page_accept"
        body.update(over)
        return self.ok(self.post("/legal/v1/acceptances", body, caller=caller), code)

    COMPANION_SOW = "Statement of Work (test companion): no fees, no fields."
    COMPANION_MSA = "Z Best Media Master Services Agreement (test companion): the same terms for every client."

    def _companion(self, doc_id, text):
        """A standard form (no party, no fields) the page needs next to the document under test: the page is an MSA
        with a SOW (clickwrap text v1 Part B), so a test about one of them pairs it with a standard other half."""
        cur = self.svc._current(doc_id)
        if cur is not None and not cur.get("template_variables"):
            return {"doc_id": doc_id, "version": cur["version"], "doc_sha256": cur["sha256"]}
        v = self.ok(self.apost(f"/legal/v1/documents/{doc_id}/versions",
                               {"request_id": rid("cmp"), "entity": "zbm", "text": text, "bump": "major"
                                if self.svc._versions_of(doc_id) else "minor"}), 201)
        self.ok(self.apost(f"/legal/v1/documents/{doc_id}/versions/{v['version']}/founder-approval",
                           {"request_id": rid("cfap"), "version_sha256": v["sha256"], "founder_confirms": True,
                            "rationale": "Test companion standard form, approved by me as founder."}))
        return {"doc_id": doc_id, "version": v["version"], "doc_sha256": v["sha256"]}

    def client_page_accept(self, doc_id, version, doc_sha, party, code=201, companion=None, **over):
        """ADR 0028: a client's acceptance through the client acceptance page, explicitly: Andre's link for the client
        (the document under test, paired with a standard companion so the link is the page Legal renders: an MSA with a
        SOW), the page text Legal renders, the client-portal caller's recording. A refused link answers its refusal.
        Returns the acceptance of the document under test."""
        target = {"doc_id": doc_id, "version": version, "doc_sha256": doc_sha}
        docs = [target]
        companion = code == 201 if companion is None else companion     # a refusal test checks the document alone
        if companion and doc_id == "client_msa":
            docs.append(self._companion("sow", self.COMPANION_SOW))
        elif companion and doc_id in ("sow", "sow_seo_audit", "sow_revenue_recovery"):
            docs.insert(0, self._companion("client_msa", self.COMPANION_MSA))
        inv = self.apost("/legal/v1/client-acceptance-invitations", {
            "request_id": rid("cinv"), "party_ref": party, "documents": docs,
            "display": {"client_legal_name": "Client LLC", "notice_email": "legal@client.example",
                        "sow_number": "SOW-1", "sow_title": "Statement of Work"}})
        if inv.status_code != 201:
            return self.ok(inv, code)
        import clickwrap_page as CP
        res = self.post("/legal/v1/portal/invitations/resolve", {"token": inv.json()["token"]}, caller="client_portal")
        bl = res.json().get("blocks") if res.status_code == 200 else None
        page = (CP.page_text(bl) if bl else f"acceptance page for {doc_id} {version}\n").encode("utf-8")
        presented, act = over.pop("presented_sha256", doc_sha), over.pop("affirmative_act", True)
        over.pop("signer_identity_ref", None)
        assert not over, f"client_page_accept: no page field for {sorted(over)}"
        body = {"request_id": rid("pacc"), "token": inv.json()["token"],
                "documents": [{**d, "presented_sha256": presented if d is target else d["doc_sha256"],
                               "affirmative_act": act if d is target else True} for d in docs],
                "presentation": "inline", "page_sha256": sha(page), "page_text_b64": b64(page),
                "form_sha256": sha("form " + inv.json()["token"]),
                "signatory": {"full_name": "Sam Signer", "title": "Owner", "company": "Client LLC"},
                "ip_hmac_sha256": "e" * 64, "user_agent_hmac_sha256": "f" * 64, "hmac_key_id": "0" * 16}
        r = self.ok(self.post("/legal/v1/portal/acceptances", body, caller="client_portal"), code)
        if code != 201:
            return r
        mine = next(a for a in r["acceptances"] if a["doc_id"] == doc_id)
        return {**mine, "obligation_ids": r["obligation_ids"], "reasons": [], "reason_lines": [],
                "request_id": r["request_id"], "invitation_id": r["invitation_id"]}

    def job(self, name):
        return self.ok(self.post(f"/legal/v1/jobs/{name}/run", {"request_id": rid("job")}, caller="scheduler"))

    def all_text(self) -> str:
        """Everything Legal wrote outside its blob store: ledger events (with payloads), local log, audit export."""
        parts = [json.dumps(self.ledger.events, default=str)]
        parts += [json.dumps(r) for r in self.svc.log.records]
        cur = 0
        while cur is not None:
            page = self.ok(self.get("/legal/v1/audit/export", cursor=cur))
            parts.append(json.dumps(page))
            cur = page["next_cursor"]
        return "\n".join(parts)
