"""OFAC owner screening and the 50 Percent Rule at client activation (ADR 0027 decision 4; MSA 1.5(b), (c), (e); red team
OPS-H4, OPS-M13).

Andre records who owns the client (PUT /compliance/v1/clients/{id}/ownership: owners, percentages, chains, the
client's attestation, name HASHES only). Each disclosed owner needs Andre's OFAC search on record (the same attestation
route as the client, with ``owner_id``), bound to that owner's name hash, fresh, no match. Any hit opens a permanent
hold on the client. An entity client without its ownership attestation, or with a 25%+ entity owner whose own owners
are not disclosed, is refused. Entities owned 50% or more in aggregate, directly or indirectly, by blocked persons are
blocked (sanctions-grounded). Owners are re-screened with the client every 90 days."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest

from clock import iso
from helpers import ANDRE_TOKEN, rid, unmet_codes
from test_service_area_gate import OCT30, attest, harness, status, web_client

CLIENT = "client-own"


def nh(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def owner(oid, pct, kind="person", parent=None, country="US", **over):
    return {"owner_id": oid, "kind": kind, "parent_owner_id": parent, "ownership_bp": int(pct * 100),
            "name_sha256": nh(f"name-{oid}"), "country": country, **over}


def attestation(at=None):
    return {"attested_at": iso(at or OCT30), "method": "signed_form", "evidence_sha256": "c" * 64,
            "attestation_ref": "ownership-form-2026-10-30"}


def put_ownership(h, owners, client=CLIENT, kind="entity", attested=True, no_owner_25=False, lane="client",
                  andre=ANDRE_TOKEN, **over):
    body = {"request_id": rid("own"), "lane": lane, "client_kind": kind, "owners": owners,
            "no_owner_25": no_owner_25, "client_attestation": attestation() if attested else None, **over}
    hd = h.headers(None, andre) if andre else h.headers("onboarding")
    return h.client.put(f"/compliance/v1/clients/{client}/ownership", json=body, headers=hd)


def attest_owner(h, oid, result="no_match", client=CLIENT, at=None, name=None, lane="client"):
    body = {"request_id": rid("att"), "subject_id": client, "owner_id": oid, "lane": lane,
            "searched_at": iso(at or h.clock.at), "list_version": "SDN list updated 2026-10-29",
            "lists": ["SDN", "Consolidated"], "tool": "ofac_sanctions_list_search", "result": result,
            "evidence_sha256": hashlib.sha256(f"ev-{oid}".encode()).hexdigest(),
            "searched_name_sha256": nh(name or f"name-{oid}")}
    r = h.post("/compliance/v1/sanctions/attestations", body, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    return r.json()


def rule(h, client=CLIENT, **kw):
    r = h.rule(client, "client", web_client(h, client, **kw))
    assert r.status_code == 200, r.text
    return r.json()


def owner_lines(j) -> list:
    return [line for line in j["unmet_lines"] if "owner" in line.lower()]


TREE = [owner("own-a", 60), owner("own-b", 40, kind="entity"), owner("own-c", 100, parent="own-b")]


def screened_tree(h):
    assert put_ownership(h, TREE).status_code == 200
    for o in TREE:
        attest_owner(h, o["owner_id"])


# --------------------------------------------------------------------------------------------- the happy paths

def test_an_entity_with_every_owner_screened_is_allowed_and_the_status_is_current():
    h = harness()
    screened_tree(h)
    j = rule(h)
    assert j["allowed"] is True, j["unmet_lines"]
    s = status(h, CLIENT)
    assert s["decision"] == "ALLOW" and s["valid_until"] == iso(OCT30 + timedelta(days=90))


def test_a_sole_proprietor_needs_no_owner_list_and_no_ownership_attestation():
    h = harness()
    assert put_ownership(h, [], kind="sole_proprietor", attested=False).status_code == 200
    assert rule(h)["allowed"] is True


def test_a_widely_held_entity_attests_that_nobody_owns_25_percent():
    h = harness()
    assert put_ownership(h, [], no_owner_25=True).status_code == 200
    assert rule(h)["allowed"] is True


# --------------------------------------------------------------------------------------------- refusals

def test_no_ownership_record_refuses():
    h = harness()
    j = rule(h, ownership=False)
    assert j["allowed"] is False and ("HR-14", "owner_screening") in unmet_codes(j)
    assert any("ownership" in line for line in owner_lines(j) + j["unmet_lines"])


def test_an_entity_without_its_ownership_attestation_is_refused():
    h = harness()
    assert put_ownership(h, TREE, attested=False).status_code == 200
    for o in TREE:
        attest_owner(h, o["owner_id"])
    j = rule(h)
    assert j["allowed"] is False and any("attestation" in line for line in j["unmet_lines"])


def test_an_entity_with_no_owners_and_no_attestation_that_nobody_owns_25_percent_is_refused():
    h = harness()
    assert put_ownership(h, []).status_code == 200
    assert rule(h)["allowed"] is False


def test_an_owner_without_a_recorded_search_is_refused():
    h = harness()
    assert put_ownership(h, TREE).status_code == 200
    attest_owner(h, "own-a")
    attest_owner(h, "own-b")
    j = rule(h)
    assert j["allowed"] is False and any("own-c" in line for line in j["unmet_lines"])


def test_a_search_for_another_name_does_not_screen_the_owner():
    h = harness()
    assert put_ownership(h, TREE).status_code == 200
    attest_owner(h, "own-a", name="someone else")
    attest_owner(h, "own-b")
    attest_owner(h, "own-c")
    j = rule(h)
    assert j["allowed"] is False and any("own-a" in line and "another name" in line for line in j["unmet_lines"])


def test_a_25_percent_entity_owner_must_disclose_its_own_owners():
    h = harness()
    owners = [owner("own-a", 70), owner("own-h", 30, kind="entity")]
    assert put_ownership(h, owners).status_code == 200
    for o in owners:
        attest_owner(h, o["owner_id"])
    j = rule(h)
    assert j["allowed"] is False and any("own-h" in line and "25%" in line for line in j["unmet_lines"])
    owners[1]["no_further_owners_25"] = True
    assert put_ownership(h, owners).status_code == 200
    assert rule(h)["allowed"] is True


def test_an_owner_search_older_than_30_days_cannot_activate():
    h = harness()
    assert put_ownership(h, TREE).status_code == 200
    attest_owner(h, "own-a", at=OCT30 - timedelta(days=31))
    attest_owner(h, "own-b")
    attest_owner(h, "own-c")
    j = rule(h)
    assert j["allowed"] is False and any("own-a" in line and "older" in line for line in j["unmet_lines"])


def test_an_owners_search_never_stands_in_for_the_clients_own():
    h = harness()
    screened_tree(h)
    a = attest_owner(h, "own-a")
    j = rule(h, sanctions_attestation_id=a["attestation_id"])
    assert j["allowed"] is False and ("HR-14", "client_sanctions_attested") in unmet_codes(j)


# --------------------------------------------------------------------------------------------- hits and the 50% rule

def test_any_owner_hit_holds_the_client_permanently_and_only_the_api_refuses_release():
    h = harness()
    assert put_ownership(h, [owner("own-a", 90), owner("own-s", 10)]).status_code == 200
    attest_owner(h, "own-a")
    out = attest_owner(h, "own-s", result="potential_match")
    assert out["hold_id"]
    hold = next(x for x in h.svc.holds.values() if x["hold_id"] == out["hold_id"])
    assert hold["subject_id"] == CLIENT and hold["releasable"] is False and hold["kind"] == "sanctions_owner_match"
    r = h.post(f"/compliance/v1/holds/{out['hold_id']}/release", {"request_id": rid(), "reason": "looks fine"},
               andre=ANDRE_TOKEN)
    assert r.status_code == 409
    j = rule(h)
    assert j["allowed"] is False
    s = status(h, CLIENT)
    # a 10% owner: the client is refused but is not itself blocked property under the 50 Percent Rule
    assert s["decision"] == "REFUSE" and s["sanctions_grounded"] is False


def test_blocked_owners_at_50_percent_in_aggregate_make_the_client_blocked():
    h = harness()
    assert put_ownership(h, [owner("own-x", 25), owner("own-y", 25), owner("own-z", 50)]).status_code == 200
    first = attest_owner(h, "own-x", result="match")
    second = attest_owner(h, "own-y", result="match")
    kinds = {h.svc.holds[first["hold_id"]]["kind"], h.svc.holds[second["hold_id"]]["kind"]}
    assert kinds == {"sanctions_owner_match", "sanctions_match"}     # the second one reaches 50%
    s = status(h, CLIENT)
    assert s["sanctions_grounded"] is True and s["sanctions_ground"] == "match"


def test_indirect_ownership_through_a_blocked_entity_counts():
    h = harness()
    # own-x (blocked) owns 50% of entity own-b; own-b owns 50% of the client: own-b is blocked (50% rule), and its
    # 50% of the client makes the client blocked, although own-b's own name is on no list
    owners = [owner("own-a", 50), owner("own-b", 50, kind="entity"), owner("own-x", 50, parent="own-b"),
              owner("own-q", 50, parent="own-b")]
    assert put_ownership(h, owners).status_code == 200
    for o in ("own-a", "own-b", "own-q"):
        attest_owner(h, o)
    attest_owner(h, "own-x", result="match")
    s = status(h, CLIENT)
    assert s["sanctions_grounded"] is True
    v = h.client.get(f"/compliance/v1/clients/{CLIENT}/ownership", headers=h.headers(None, ANDRE_TOKEN)).json()
    assert v["blocked_bp"] == 5000 and v["blocked"] is True


def test_below_50_percent_in_aggregate_is_refused_but_not_blocked():
    h = harness()
    owners = [owner("own-a", 60), owner("own-b", 40, kind="entity"), owner("own-x", 49, parent="own-b"),
              owner("own-q", 51, parent="own-b")]
    assert put_ownership(h, owners).status_code == 200
    for o in ("own-a", "own-b", "own-q"):
        attest_owner(h, o)
    attest_owner(h, "own-x", result="match")
    s = status(h, CLIENT)
    assert s["decision"] == "REFUSE" and s["sanctions_grounded"] is False


# --------------------------------------------------------------------------------------------- the 90-day re-screen

def test_owners_are_rescreened_every_90_days_with_the_client():
    h = harness()
    screened_tree(h)
    assert rule(h)["allowed"] is True
    h.clock.at = OCT30 + timedelta(days=60)
    attest_owner(h, "own-a")             # re-screened early: the ACTIVATION still names the first search
    h.clock.at = OCT30 + timedelta(days=91)
    s = status(h, CLIENT)
    assert s["decision"] == "REFUSE"
    for oid in ("own-a", "own-b", "own-c"):
        assert any(f"owner {oid}" in x and "re-screen" in x for x in s["reasons"]), (oid, s["reasons"])


def test_a_re_activation_takes_the_owners_new_searches():
    h = harness()
    screened_tree(h)
    assert rule(h)["allowed"] is True
    h.clock.at = OCT30 + timedelta(days=50)
    h.run_controls()                      # (controls go stale; they run every 6 hours in production)
    for o in TREE:
        attest_owner(h, o["owner_id"])
    j = rule(h)
    assert j["allowed"] is True, j["unmet_lines"]
    s = status(h, CLIENT)
    assert s["decision"] == "ALLOW" and s["valid_until"] == iso(OCT30 + timedelta(days=50 + 90))
    h.clock.at = OCT30 + timedelta(days=91)            # past the FIRST searches' 90 days, inside the new ones'
    assert not [x for x in status(h, CLIENT)["reasons"] if "owner" in x]


def test_valid_until_is_the_earliest_of_the_client_and_its_owners():
    h = harness()
    assert put_ownership(h, TREE).status_code == 200
    attest_owner(h, "own-a", at=OCT30 - timedelta(days=20))
    attest_owner(h, "own-b")
    attest_owner(h, "own-c")
    assert rule(h)["allowed"] is True
    assert status(h, CLIENT)["valid_until"] == iso(OCT30 - timedelta(days=20) + timedelta(days=90))


def test_an_ownership_change_after_activation_needs_a_new_activation():
    h = harness()
    screened_tree(h)
    assert rule(h)["allowed"] is True
    changed = [owner("own-a", 55), TREE[1], TREE[2], owner("own-d", 5)]
    assert put_ownership(h, changed).status_code == 200
    s = status(h, CLIENT)
    assert s["decision"] == "REFUSE" and any("ownership" in x for x in s["reasons"])


# --------------------------------------------------------------------------------------------- the record itself

@pytest.mark.parametrize("owners,why", [
    ([owner("own-a", 60), owner("own-a", 40)], "twice"),
    ([owner("own-a", 60), owner("own-b", 50)], "100%"),
    ([owner("own-a", 100), owner("own-c", 50, parent="own-a")], "entity"),
    ([owner("own-b", 100, kind="entity"), owner("own-c", 50, parent="own-zz")], "parent"),
    ([owner("own-b", 50, kind="entity", parent="own-c"), owner("own-c", 50, kind="entity", parent="own-b")], "cycle"),
])
def test_an_inconsistent_ownership_record_is_refused(owners, why):
    h = harness()
    r = put_ownership(h, owners)
    assert r.status_code == 422 and why in r.text, r.text


def test_a_sole_proprietor_with_owners_is_refused():
    h = harness()
    assert put_ownership(h, [owner("own-a", 100)], kind="sole_proprietor").status_code == 422


def test_only_andre_records_ownership_and_no_name_is_ever_accepted_or_stored():
    h = harness()
    assert put_ownership(h, TREE, andre=None).status_code == 403
    bad = [dict(TREE[0], legal_name="Jane Doe")]
    assert put_ownership(h, bad).status_code == 422
    assert put_ownership(h, TREE).status_code == 200
    blob = json.dumps([r for r in h.svc.log.records]) + json.dumps(h.ledger.events)
    for o in TREE:
        assert f"name-{o['owner_id']}" not in blob


def test_ownership_is_recorded_on_the_ledger_first_and_survives_a_restart(tmp_path):
    from helpers import Harness
    import helpers as H
    from fakes import FakeLedgerClient, PassingLegal
    from clock import FixedClock
    ledger = FakeLedgerClient()
    clock = FixedClock(OCT30)
    ports = H.passing_ports(clock)
    ports.legal = PassingLegal()
    h = Harness(ports=ports, clock=clock, ledger=ledger, data_dir=str(tmp_path))
    h.approve_seed()
    h.run_controls()
    screened_tree(h)
    assert any(e["event_type"] == "client_ownership_recorded" for e in ledger.events)
    ports2 = H.passing_ports(clock)
    ports2.legal = PassingLegal()
    h2 = Harness(ports=ports2, clock=clock, ledger=ledger, data_dir=str(tmp_path))
    h2.run_controls()
    assert rule(h2)["allowed"] is True
