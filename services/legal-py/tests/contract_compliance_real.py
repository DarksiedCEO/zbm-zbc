"""
Contract check (AEGIS N17-8): Legal's REAL thin client (``compliance_client.HttpCompliance``) against the REAL
compliance-py app, in one process (compliance-py's ASGI app served through its TestClient transport; no network).
Run as a script by ``test_aegis_r17.py::test_n17_8_memo_proposal_end_to_end_against_the_real_compliance_py`` in a
subprocess, because both services have modules with the same names (api, service, models ...). Prints one JSON
object; exit code 0 = the path is proven end to end.

Path proven: Legal files a counsel memo answering CQ-11 -> the memo id exists -> Andre proposes the superseding row
with ``source_url: urn:legal37:memos:<memo_id>`` -> Legal delivers it to compliance-py (201, a real proposal id) ->
Andre approves it AT COMPLIANCE -> Legal's proposal-delivery job reads the replacement row back through the real
client and only then marks CQ-11 verified.
"""

from __future__ import annotations

import json
import os
import socket
import sys

sys.dont_write_bytecode = True   # wave 26b (XS-PYC): it imports compliance-py; it runs outside pytest, so the suite's conftest does not reach it
from datetime import datetime, timezone
from pathlib import Path

LEGAL = Path(__file__).resolve().parents[1]
COMPLIANCE = LEGAL.parent / "compliance-py"
CLASH = ("api", "config", "service", "models", "ports", "ledger", "store", "clock", "errors", "founder", "reasons",
         "rules", "textguard", "serve", "helpers", "fakes", "intelligences", "compliance_client", "advice", "bizdays",
         "builders", "controls", "sidestore", "contract_maps", "platforms", "watcher", "adapters", "fetcher", "register",
         "answers")


def _refuse(*a, **k):
    raise RuntimeError("network access attempted (not allowed)")


socket.socket.connect = _refuse
socket.create_connection = _refuse


def purge() -> None:
    for k in list(sys.modules):
        if k.split(".")[0] in CLASH:
            del sys.modules[k]


def main() -> int:
    out: dict = {}
    # 1) the real compliance-py
    sys.path[:0] = [str(COMPLIANCE / "src"), str(COMPLIANCE / "tests")]
    os.environ["COMPLIANCE_SERVICE_TOKEN"] = "test-compliance-service-token-do-not-use"
    import helpers as CH                                        # noqa: E402  (compliance-py's test harness)
    from clock import FixedClock                                # noqa: E402
    cx = CH.Harness(clock=FixedClock(datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)))
    cx.approve_seed()
    transport = cx.client._transport
    seed = next(r for r in CH.SEED_ROWS if r["id"] == "CQ-11")
    csvc, ccall = CH.SERVICE_TOKEN, CH.CALLERS["legal_37"]
    purge()
    sys.path = [p for p in sys.path if "compliance-py" not in p]
    # 2) legal-py with its real thin client, pointed at that app
    sys.path[:0] = [str(LEGAL / "src"), str(LEGAL / "tests")]
    os.environ["LEGAL_SERVICE_TOKEN"] = "test-legal-service-token-do-not-use-0123"
    import helpers as LH                                        # noqa: E402  (legal-py's test harness)
    from compliance_client import HttpCompliance                # noqa: E402
    from fakes import FakeCounsel, FakeCyber, FakeDept, FakeESign   # noqa: E402
    from ports import Ports                                     # noqa: E402
    ports = Ports(compliance=HttpCompliance("http://testserver", csvc, ccall, transport=transport))
    ports.counsel, ports.cybersecurity_22, ports.esign = FakeCounsel(), FakeCyber(), FakeESign()
    for n in ("people_43", "clipper_network", "creative_production", "finance_31", "onboarding",
              "verification_integrity", "push"):
        setattr(ports, n, FakeDept(n))
    x = LH.Harness(ports=ports, clock=cx.clock)
    x.approve_rules()
    x.engage()
    m = x.memo(cites={"cq_ids": ["CQ-11"]}, answers=[{"cq_id": "CQ-11", "resolution": "verified_rule",
                                                      "quoted_excerpt": "Counsel answers CQ-11."}])
    out["memo_id"] = m["memo_id"]
    row = dict(seed)
    row.pop("in_force", None)
    row.update(id="CQ-11-MEMO-1", status="verified", source_kind="guidance", source_quality="primary",
               verified_at="2026-10-01", source_url=f"urn:legal37:memos:{m['memo_id']}")
    mp = x.memo_proposals(m["memo_id"], [{"kind": "supersede", "target_id": "CQ-11", "proposed_row": row,
                                          "quoted_excerpt": "Counsel answers CQ-11."}])
    p = mp["proposals"][0]
    out["legal_proposal"] = {k: p[k] for k in ("status", "compliance_proposal_id")}
    out["cq11_before"] = x.ok(x.get("/legal/v1/register/CQ-11"))["status"]
    # 3) Andre approves AT COMPLIANCE (compliance-py's own harness again, same process, same app)
    purge()
    sys.path = [q for q in sys.path if "legal-py" not in q]
    sys.path[:0] = [str(COMPLIANCE / "src"), str(COMPLIANCE / "tests")]
    inbox = [q for q in cx.inbox() if q["proposal_id"] == p["compliance_proposal_id"]]
    out["at_compliance"] = [{"kind": q["kind"], "target_id": q["target_id"],
                             "source_url": q["proposed_row"]["source_url"]} for q in inbox]
    cx.approve(*inbox)
    x.clock.advance(days=1)
    out["delivery_job"] = x.job("proposal-delivery")["summary"]
    out["cq11_after"] = x.ok(x.get("/legal/v1/register/CQ-11"))["status"]
    print(json.dumps(out, sort_keys=True))
    ok = (out["legal_proposal"]["status"] == "delivered" and out["legal_proposal"]["compliance_proposal_id"]
          and out["at_compliance"] and out["at_compliance"][0]["source_url"] == f"urn:legal37:memos:{m['memo_id']}"
          and out["cq11_before"] == "unverified" and out["cq11_after"] == "verified")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
