"""AEGIS round 7 on 09030da (Oct 6 2026, NOT BLOCKING; cleared for wiring).

Low  ``audit_evidence`` parsed the whole log under the service lock. Now only the raw lines not yet seen are copied
     under it; they are parsed and hashed outside it and cached by log length, so a page costs only the new lines.
"""

from __future__ import annotations

import threading

import service as service_mod
from helpers import Harness, wired_ports


def test_low_evidence_view_parses_only_new_lines_across_pages(tmp_path, monkeypatch):
    h = Harness(tmp_path, ports=wired_ports())
    h.won_deal()
    parsed = []
    real = getattr(service_mod, "_parse_line", None)
    assert real is not None, "no separate parse step: the view parses inside its own loop"
    monkeypatch.setattr(service_mod, "_parse_line", lambda raw: parsed.append(raw) or real(raw))
    p1 = h.ok(h.get("/audit/evidence?limit=2&offset=0", caller="compliance_38"))
    first = len(parsed)
    assert first == len(h.svc.log)                                      # every line once, the first time
    p2 = h.ok(h.get("/audit/evidence?limit=2&offset=2", caller="compliance_38"))
    assert len(parsed) == first                                          # page two: no line parsed again
    assert p1["total"] == p2["total"] and p1["evidence"] != p2["evidence"]
    h.contact(email="later@x.test")                                      # new lines
    grown = len(h.svc.log) - first
    assert grown >= 1
    h.ok(h.get("/audit/evidence?limit=2&offset=0", caller="compliance_38"))
    assert len(parsed) == first + grown                                  # only the new lines
    assert h.ok(h.get("/audit/evidence?limit=1000", caller="compliance_38"))["attempted"] == 0


def test_low_evidence_view_parses_outside_the_service_lock(tmp_path, monkeypatch):
    h = Harness(tmp_path, ports=wired_ports())
    h.won_deal()
    held = []
    real = getattr(service_mod, "_parse_line", None)
    assert real is not None

    def probe(raw):                                                     # the service lock is an RLock, so ask
        free = []                                                        # from another thread whether it is free
        t = threading.Thread(target=lambda: free.append(h.svc.lock.acquire(blocking=False)
                                                        and (h.svc.lock.release() or True)))
        t.start()
        t.join()
        held.append(not free[0])
        return real(raw)

    monkeypatch.setattr(service_mod, "_parse_line", probe)
    out = h.ok(h.get("/audit/evidence", caller="compliance_38"))
    assert held and not any(held)                                        # the lock was free at every parse
    assert out["consistency"] == "eventual; re-read to settle"
