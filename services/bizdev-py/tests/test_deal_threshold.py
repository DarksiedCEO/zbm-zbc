"""Andre approves any deal over $10,000, aggregated per counterparty group so splitting cannot get around it."""

from datetime import timedelta

from helpers import T0, Harness, rid, wired_ports


def _ready(h, p):
    h.bid(p["pursuit_id"])
    return h.ready_response(p["pursuit_id"])


def test_boundary_10000_00_is_not_over(h):
    p = h.pursuit(value="10000.00")
    assert p["deal_gate"]["needs_andre"] is False and p["deal_gate"]["approved"] is True
    p = h.pursuit(value="10000.01", ref="org:big", name="Big Co", domain="big.test")
    assert p["deal_gate"]["needs_andre"] is True and p["deal_gate"]["approved"] is False


def test_over_threshold_needs_andre_before_submission(h):
    p = h.pursuit(value="25000.00")
    r = _ready(h, p)
    h.refused(h.submit(r), 409, "DEAL_APPROVAL_REQUIRED")
    g = h.ok(h.get(f"/pursuits/{p['pursuit_id']}"))["deal_gate"]
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/deal-approval", {"request_id": rid(),
                                                                    "binding_sha256": g["binding_sha256"]}), 403)
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/deal-approval", {"request_id": rid(), "binding_sha256": "0" * 64},
                     andre=True), 409, "DEAL_APPROVAL_STALE")
    g = h.ok(h.post(f"/pursuits/{p['pursuit_id']}/deal-approval",
                    {"request_id": rid(), "binding_sha256": g["binding_sha256"]}, andre=True))
    assert g["approved"] is True
    assert h.ok(h.submit(r), 201)


def test_not_needed_is_refused(h):
    p = h.pursuit(value="100.00")
    g = p["deal_gate"]
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/deal-approval",
                     {"request_id": rid(), "binding_sha256": g["binding_sha256"]}, andre=True), 409,
              "DEAL_APPROVAL_NOT_NEEDED")


def test_split_deals_are_aggregated_and_reblock_the_first(h):
    a = h.pursuit(value="6000.00")
    ra = _ready(h, a)
    b = h.pursuit(value="6000.00", ref="org:acme-2", name="ACME, Incorporated", domain="www.acme.test")
    assert b["deal_gate"]["aggregate"] == "12000.00" and b["deal_gate"]["needs_andre"]
    assert sorted(b["deal_gate"]["members"]) == sorted([a["pursuit_id"], b["pursuit_id"]])
    h.refused(h.submit(ra), 409, "DEAL_APPROVAL_REQUIRED")          # the earlier one is re-blocked at the gate


def test_grouping_is_transitive_over_any_shared_key(h):
    a = h.pursuit(value="4000.00", ref="org:a", name="Alpha Co", domain="alpha.test")
    h.pursuit(value="4000.00", ref="org:b", name="Alpha", domain="beta.test")              # shares the name with a
    c = h.pursuit(value="4000.00", ref="org:c", name="Gamma", domain="beta.test")         # shares the domain with b
    assert c["deal_gate"]["aggregate"] == "12000.00"
    assert h.ok(h.get(f"/pursuits/{a['pursuit_id']}"))["deal_gate"]["needs_andre"] is True


def test_subdomain_and_two_level_suffix(h):
    h.pursuit(value="6000.00", ref="org:uk1", name="Brit One", domain="sales.brit.co.uk")
    b = h.pursuit(value="6000.00", ref="org:uk2", name="Brit Two", domain="www.brit.co.uk")
    c = h.pursuit(value="6000.00", ref="org:uk3", name="Other Three", domain="other.co.uk")
    assert b["deal_gate"]["aggregate"] == "12000.00" and c["deal_gate"]["aggregate"] == "6000.00"


def test_partner_deals_and_pursuits_aggregate_together(h):
    h.pursuit(value="6000.00", ref="org:client1", name="Client One", domain="clientone.test")
    p = h.partner()
    d = h.deal(p["partner_id"], value="5000.00")
    assert d["deal_gate"]["aggregate"] == "11000.00" and d["deal_gate"]["needs_andre"] is True


def test_approval_goes_stale_when_value_or_group_changes(h):
    p = h.pursuit(value="20000.00")
    g = p["deal_gate"]
    h.ok(h.post(f"/pursuits/{p['pursuit_id']}/deal-approval", {"request_id": rid(),
                                                               "binding_sha256": g["binding_sha256"]}, andre=True))
    assert h.ok(h.get(f"/pursuits/{p['pursuit_id']}"))["deal_gate"]["approved"] is True
    h.ok(h.post(f"/pursuits/{p['pursuit_id']}/value", {"request_id": rid(), "value": "21000.00"}))
    assert h.ok(h.get(f"/pursuits/{p['pursuit_id']}"))["deal_gate"]["approved"] is False
    g = h.ok(h.get(f"/pursuits/{p['pursuit_id']}"))["deal_gate"]
    h.ok(h.post(f"/pursuits/{p['pursuit_id']}/deal-approval", {"request_id": rid(),
                                                               "binding_sha256": g["binding_sha256"]}, andre=True))
    h.pursuit(value="1.00", ref="org:acme-sibling", name="Acme", domain="acme-other.test")
    assert h.ok(h.get(f"/pursuits/{p['pursuit_id']}"))["deal_gate"]["approved"] is False


def test_lost_sibling_no_longer_counts(h):
    a = h.pursuit(value="6000.00")
    h.bid(a["pursuit_id"])
    b = h.pursuit(value="6000.00", ref="org:acme-b", name="Acme", domain="acme-b.test")
    assert b["deal_gate"]["needs_andre"]
    h.ok(h.post(f"/pursuits/{a['pursuit_id']}/lost", {"request_id": rid(), "reason_code": "scope"}))
    assert h.ok(h.get(f"/pursuits/{b['pursuit_id']}"))["deal_gate"]["needs_andre"] is False


def test_window_applies_to_closed_deals_only_and_uses_the_injected_clock(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    h.pursuit(value="6000.00", deadline=None, kind="formal_pitch")              # open: counts whatever its age
    won = h.won_deal(value="3000.00")                                          # closed (won), Acme's group below
    h.ok(h.post("/partner-deals", {"request_id": rid(), "partner_id": won["partner_id"], "brand": "zbm",
                                   "counterparty": {"ref": "org:acme-p", "name": "Acme", "domain": "acmep.test"},
                                   "deal_value": "1.00"}), 201)
    h.clock.at = T0 + timedelta(days=366)
    b = h.pursuit(value="6000.00", ref="org:acme-later", name="Acme", domain="acme-later.test", deadline=None,
                  kind="formal_pitch")
    assert b["deal_gate"]["aggregate"] == "12001.00"                          # the old open pitch still counts


def test_threshold_setting_can_only_be_lowered(tmp_path):
    import pytest
    with pytest.raises(RuntimeError):
        Harness(tmp_path, NBD_DEAL_APPROVAL_THRESHOLD="10000.01")
    h = Harness(tmp_path, NBD_DEAL_APPROVAL_THRESHOLD="500.00")
    assert h.pursuit(value="500.01")["deal_gate"]["needs_andre"] is True


def test_win_rechecks_gate(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    p = h.pursuit(value="9000.00")
    h.ok(h.submit(_ready(h, p)), 201)
    h.ok(h.job("submission-queue"))
    h.pursuit(value="2000.00", ref="org:acme-x", name="Acme", domain="acme-x.test")
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/won", {"request_id": rid()}, andre=True), 409,
              "DEAL_APPROVAL_REQUIRED")
    g = h.ok(h.get(f"/pursuits/{p['pursuit_id']}"))["deal_gate"]
    h.ok(h.post(f"/pursuits/{p['pursuit_id']}/deal-approval", {"request_id": rid(),
                                                               "binding_sha256": g["binding_sha256"]}, andre=True))
    assert h.ok(h.post(f"/pursuits/{p['pursuit_id']}/won", {"request_id": rid()}, andre=True))["stage"] == "won"


def test_queued_submission_held_not_cancelled_when_gate_reopens(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    p = h.pursuit(value="9000.00")
    h.ok(h.submit(_ready(h, p)), 201)
    h.pursuit(value="2000.00", ref="org:acme-y", name="Acme", domain="acme-y.test")
    assert h.ok(h.job("submission-queue"))["held"] == 1
    assert h.ports.submission.calls == []
    assert h.ok(h.get("/submissions"))[0]["status"] == "queued"
