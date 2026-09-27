"""
Spec §F property (required): NO RELEASE WHILE ANY GATE INPUT IS ABSENT OR ANSWERED BY A STAND-IN.

For every non-empty subset of the 11 gate inputs {V&I, Compliance ruling, Compliance holds, sanctions status, tax
agent, rail status, bank feed, reconciliation, treasury (rail balance), vault, rules version (the Compliance rows the
rules rely on)} — all 2^11 - 1 = 2047 of them — made absent/stale while the fakes otherwise pass, the release route
submits NOTHING to the rail, and every affected item carries a reason naming each absent input.

Each subset starts from the same approved, funded batch: the golden state (local log lines, ledger events, fakes and
clock) is built once and copied per subset; the service is rebuilt by replaying the copied log.
"""

from __future__ import annotations

import copy
import itertools

import pytest

from helpers import Harness, rid
from store import RecordLog

INPUTS = ("vi", "ruling", "holds", "sanctions", "tax", "rail", "bank", "recon", "treasury", "vault", "rules")
# what the reason message names, per input
NAMES = {"vi": "V&I certification", "ruling": "Compliance ruling", "holds": "Compliance holds",
         "sanctions": "sanctions status", "tax": "tax agent", "rail": "rail account status", "bank": "bank feed",
         "recon": "reconciliation run is stale", "treasury": "rail balance unavailable", "vault": "vault",
         "rules": "Compliance row"}


def _absent(f, name):
    if name == "vi":
        f["vi"].available = False
    elif name == "ruling":
        f["compliance"].rulings_available = False
    elif name == "holds":
        f["compliance"].holds_available = False
    elif name == "sanctions":
        f["compliance"].sanctions_available = False
    elif name == "tax":
        f["tax"].available = False
    elif name == "rail":
        f["rails"]["stripe"].status_available = False
    elif name == "bank":
        f["bank"].available = False
    elif name == "treasury":
        f["rails"]["stripe"].balance_available = False
    elif name == "vault":
        f["vault"].available = False
    elif name == "rules":
        f["compliance"].rows_available = False
    # "recon": handled by the clock (the latest reconciliation run goes stale: > 26 h)


@pytest.fixture(scope="module")
def golden():
    g = Harness().ready()
    g.fund_campaign()
    g.payee()
    g.accrue()
    g.recon()
    b = g.run()["batch"]
    g.approve(b)
    g.fund_batch(b["batch_id"])
    return g, b


def _fork(g):
    w = copy.deepcopy({"f": g.f, "clock": g.clock})
    log = RecordLog(None)
    log._lines = list(g.svc.log._lines)
    return Harness(ledger=g.ledger.copy(), clock=w["clock"], fakes=w["f"], log=log)


class _Proxy:
    """One FastAPI app for all 2,047 forks (building an app costs ~0.1 s): its routes call whichever forked service
    is current."""

    def __init__(self):
        self.target = None

    def __getattr__(self, name):
        return getattr(self.target, name)


def _fork_service(g, proxy):
    """A fork that reuses one app: a fresh Service replayed from the golden log, with copied fakes and clock."""
    from ledger import Recorder
    from ports import Ports
    from service import Service
    import config as config_mod
    w = copy.deepcopy({"f": g.f, "clock": g.clock})
    log = RecordLog(None)
    log._lines = list(g.svc.log._lines)
    f = w["f"]
    ports = Ports(vi=f["vi"], compliance=f["compliance"], cn=f["cn"], legal=f["legal"], rails=f["rails"],
                  bank=f["bank"], tax=f["tax"], gl=f["gl"], vault=f["vault"], people=f["people"], push=f["push"])
    seed = open(g.settings.seed_path, "rb").read()
    proxy.target = Service(g.settings, Recorder(g.ledger.copy()), log, seed, config_mod.PINNED_SEED_SHA256, ports,
                           w["clock"], config_mod.PINNED_SEED_SHA256)
    return f, w["clock"]


def test_golden_state_releases_when_every_input_is_present(golden):
    g, b = golden
    x = _fork(g)
    x.clock.advance(hours=13)
    rel = x.release(b["batch_id"])
    assert rel["batch"]["items"][0]["status"] == "submitted"
    assert len([c for c in x.stripe.calls if c[0] == "submit"]) == 1


SUBSETS = [s for n in range(1, len(INPUTS) + 1) for s in itertools.combinations(INPUTS, n)]


def test_no_release_while_any_gate_input_is_absent(golden):
    assert len(SUBSETS) == 2 ** 11 - 1
    g, b = golden
    import api
    from fastapi.testclient import TestClient
    proxy = _Proxy()
    client = TestClient(api.create_app(proxy, g.settings), raise_server_exceptions=False)
    failures = []
    for subset in SUBSETS:
        f, clock = _fork_service(g, proxy)
        for name in subset:
            _absent(f, name)
        clock.advance(hours=27 if "recon" in subset else 13)
        r = client.post(f"/fin/v1/payout-batches/{b['batch_id']}/release", json={"request_id": rid()},
                        headers=g.headers("scheduler"))
        submits = [c for c in f["rails"]["stripe"].calls if c[0] == "submit"]
        body = r.json()
        if r.status_code == 409:
            items = body.get("items") or []
        elif r.status_code == 200:
            items = [i for i in body["results"] if i.get("status") == "excluded_at_release"]
        else:
            failures.append((subset, f"HTTP {r.status_code}"))
            continue
        if submits or len(items) != 1:
            failures.append((subset, f"submits={len(submits)} items={len(items)}"))
            continue
        messages = " | ".join(x_["message"] for x_ in items[0]["reasons"])
        missing = [n for n in subset if NAMES[n] not in messages]
        if missing:
            failures.append((subset, f"no reason names {missing}"))
    assert not failures, failures[:10]
