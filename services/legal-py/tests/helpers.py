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
           "LEGAL_CALLER_TOKENS": json.dumps(CALLERS)}
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
        body.update(over)
        return self.ok(self.post("/legal/v1/acceptances", body, caller=caller), code)

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
