"""Property test from the AEGIS round 3 fuzz probe (scratchpad aegis-nbd/test_r3b.py): with a Finance port that
answers at random (delivered, refused, unknown, raising after or before taking the request) and Andre reconciling,
the commission money never drifts: settled equals the live payouts, the shortfall is always max(0, settled -
accrued), and a payout Finance holds is never queued again or cancelled. It held on 63bef23 too; it guards the
round-3 changes to reconcile."""

from __future__ import annotations

import random
from decimal import Decimal

from helpers import Harness, fin_id, rid, wired_ports
from ports import Delivery



class RandPayouts:
    wired = True

    def __init__(self, rng):
        self.rng, self.calls, self.held = rng, [], {}

    def request_payout(self, pid, payload):
        self.calls.append((pid, payload["amount"]))
        x = self.rng.choice(["delivered", "refused", "unknown", "raise"])
        if x == "delivered":
            self.held[pid] = payload["amount"]
            return Delivery("delivered", fin_id("pay"))
        if x == "raise":
            if self.rng.random() < .5:
                self.held[pid] = payload["amount"]
            raise TimeoutError()
        return Delivery(x)

    def payout_status(self, pid):
        if pid in self.held:
            return Delivery("with_finance", fin_id("pay"))
        return Delivery(self.rng.choice(["refused", "unknown"]))


def test_fuzz_money_invariants_hold(tmp_path):
    for seed in range(12):
        rng = random.Random(seed)
        port = RandPayouts(rng)
        h = Harness(tmp_path / str(seed), ports=wired_ports(payouts=port))
        did = h.won_deal(value="8000.00", rate="10.00")["deal_id"]
        for _ in range(30):
            a = rng.random()
            if a < .3:
                h.money_event(did, "payment", f"{rng.randint(1, 4000)}.{rng.randint(0, 99):02d}")
            elif a < .45:
                h.money_event(did, rng.choice(["refund", "chargeback"]), f"{rng.randint(1, 3000)}.00")
            elif a < .85:
                h.job("payout-request")
            else:
                for p in h.ok(h.get("/payouts")):
                    if p["status"] in ("sending", "held"):
                        out = "paid" if p["payout_id"] in port.held else "not_paid"
                        h.post(f"/payouts/{p['payout_id']}/reconcile", {"request_id": rid(), "outcome": out,
                                                                         "state_sha256": p["state_sha256"]},
                               andre=True)
            c = h.svc.partner_deals[did]["commission"]
            live = sum(Decimal(p["amount"]) for p in h.svc.payouts.values() if p["status"] != "cancelled")
            assert Decimal(c["settled"]) == live
            assert Decimal(c["shortfall"]) == max(Decimal(0), Decimal(c["settled"]) - Decimal(c["accrued"]))
            for pid in port.held:
                assert h.svc.payouts[pid]["status"] not in ("queued", "cancelled"), (seed, pid)
