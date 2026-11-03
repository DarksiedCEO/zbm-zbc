"""Client-supplied canonical facts through the actual founder-authorized API; no provider calls."""

import copy
import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest
import helpers as H
from ledger import LedgerNotRecorded, LedgerRecordError


@pytest.fixture
def owned(tmp_path, monkeypatch):
    for name in ("SERVICE_TOKEN", "ANDRE"):
        monkeypatch.setattr(H, name, secrets.token_hex(32))
    for name in ("CALLERS", "TENANT_TOKENS"):
        monkeypatch.setattr(
            H, name, {k: secrets.token_hex(32) for k in getattr(H, name)}
        )
    h = H.Harness(
        tmp_path,
        data_dir=str(tmp_path / "data"),
        real_compliance=True,
        SEO_INVOICE_VERIFICATION=None,
    )
    h.tenant("acme")
    try:
        yield h
    finally:
        h.svc.close()


def body():
    return {
        "request_id": H.rid(),
        "fields": [
            {
                "field": k,
                "value": v,
                "source": "owned_fixture",
                "provenance": "Explicit synthetic facts, not client data",
                "authority": "first_party",
            }
            for k, v in {
                "name": "Owned Example",
                "street_address": "100 Example Street",
                "locality": "Example City",
                "region": "CA",
                "telephone": "202-555-0100",
            }.items()
        ],
    }


def create(h, b=None, **kw):
    return h.post("/tenants/acme/entity", body() if b is None else b, andre=True, **kw)


def test_create_all_five_facts_and_current_view_replay_after_update_restart(owned):
    h = owned
    b = body()
    e = h.ok(create(h, b), 201)
    assert (
        e["tenant_id"] == "acme" and e["entity_id"] == "ent-acme" and e["version"] == 1
    )
    assert set(e["fields"]) == {v["field"] for v in b["fields"]}
    for supplied in b["fields"]:
        k = supplied["field"]
        v = {x: y for x, y in supplied.items() if x != "field"}
        assert all(e["fields"][k][x] == y for x, y in v.items())
        assert e["fields"][k]["history"] == []
    before = len(h.ledger.events)
    assert h.ok(create(h, b), 201) == e
    assert len(h.ledger.events) == before
    update = {
        "request_id": H.rid(),
        "field": "name",
        **b["fields"][0],
        "value": "Owned Updated",
        "expected_version": 1,
    }
    changed = h.ok(h.post("/entities/ent-acme/fields", update, andre=True))
    assert changed["version"] == 2 and len(changed["fields"]["name"]["history"]) == 1
    assert h.ok(create(h, b), 201) == changed
    h2 = h.restart()
    try:
        assert h2.ok(create(h2, b), 201) == changed
        assert h2.ok(h2.get("/tenants/acme/entity")) == changed
        assert len(h2.ledger.of_type("entity_created")) == 2  # own seed plus client
    finally:
        h2.svc.close()


def test_subset_omissions_are_not_inferred(owned):
    b = body()
    b["fields"] = [b["fields"][-1]]
    e = owned.ok(create(owned, b), 201)
    assert set(e["fields"]) == {"telephone"}
    assert owned.svc.tenants["acme"]["finance_client_id"] is None


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "unknown",
        "value_bool",
        "value_control",
        "source",
        "authority",
        "provenance",
        "phone",
        "extra_field",
        "tenant_id",
        "entity_id",
    ],
)
def test_strict_supplied_values_and_ownership(owned, change):
    b = body()
    if change == "empty":
        b["fields"] = []
    elif change == "unknown":
        b["fields"][0]["field"] = "country"
    elif change == "value_bool":
        b["fields"][0]["value"] = True
    elif change == "value_control":
        b["fields"][0]["value"] = "owned\nprivate"
    elif change == "source":
        b["fields"][0]["source"] = "Mixed Case"
    elif change == "authority":
        b["fields"][0]["authority"] = "verified_by_provider"
    elif change == "provenance":
        b["fields"][0]["provenance"] = ""
    elif change == "phone":
        b["fields"][-1]["value"] = "20255501009"
    elif change == "extra_field":
        b["fields"][0]["private_extra"] = "x"
    else:
        b[change] = "zbm"
    assert create(owned, b).status_code == 422
    assert owned.svc.entity_for_tenant("acme") is None


def test_request_conflict_duplicate_and_own_tenant_refusal(owned):
    b = body()
    owned.ok(create(owned, b), 201)
    changed = copy.deepcopy(b)
    changed["fields"][0]["value"] = "Different"
    owned.refused(create(owned, changed), 409, "REQUEST_ID_REUSED")
    owned.refused(create(owned), 409, "ENTITY_EXISTS")
    owned.refused(
        owned.post("/tenants/zbm/entity", body(), andre=True),
        422,
        "ENTITY_TENANT_KIND_INVALID",
    )
    owned.refused(
        owned.post("/tenants/missing/entity", body(), andre=True),
        404,
        "TENANT_NOT_FOUND",
    )


@pytest.mark.parametrize(
    "caller,founder,tenant,status",
    [
        ("dashboard", False, None, 403),
        ("seo_agent", False, None, 403),
        ("hub", True, "acme", 403),
        ("hub", True, "globex", 403),
        (None, True, None, 403),
    ],
)
def test_founder_only_create(owned, caller, founder, tenant, status):
    # Direct TestClient avoids Harness's convenience rewrite from seo_agent to dashboard.
    r = owned.client.post(
        "/seo/v1/tenants/acme/entity",
        json=body(),
        headers=owned.headers(caller, founder, tenant),
    )
    assert r.status_code == status
    assert owned.svc.entity_for_tenant("acme") is None


@pytest.mark.parametrize(
    "switch", ["global", "write", "tenant:acme", "capability:entity_write"]
)
def test_creation_respects_existing_kills(owned, switch):
    owned.ok(owned.switch(switch))
    assert create(owned).status_code == 403
    assert owned.svc.entity_for_tenant("acme") is None


@pytest.mark.parametrize("committed", [False, True])
def test_unconfirmed_evidence_never_publishes_and_identical_retry_recovers(
    owned, committed
):
    b = body()
    original = owned.ledger.record_event

    def unavailable(*a, **kw):
        event_type = kw.get("event_type", a[2] if len(a) > 2 else None)
        if event_type == "entity_created":
            if committed:
                original(*a, **kw)
            raise (
                LedgerRecordError("owned ambiguous failure")
                if committed
                else LedgerNotRecorded("owned refusal")
            )
        return original(*a, **kw)

    owned.ledger.record_event = unavailable
    assert create(owned, b).status_code == 503
    assert owned.svc.entity_for_tenant("acme") is None
    owned.ledger.record_event = original
    recovered = owned.restart()
    try:
        assert recovered.ok(create(recovered, b), 201)["tenant_id"] == "acme"
        assert len(recovered.ledger.of_type("entity_created")) == 2
    finally:
        recovered.svc.close()


def test_concurrent_creation_has_one_binding(owned):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda b: create(owned, b), [body(), body()]))
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert (
        len([e for e in owned.svc.entities.values() if e["tenant_id"] == "acme"]) == 1
    )


def test_scoped_reader_cannot_see_other_tenant_entity(owned):
    owned.tenant("globex")
    owned.ok(create(owned), 201)
    owned.refused(
        owned.get("/tenants/acme/entity", caller="hub", tenant="globex"),
        404,
        "TENANT_NOT_FOUND",
    )
    assert (
        owned.ok(owned.get("/tenants/acme/entity", caller="hub", tenant="acme"))[
            "tenant_id"
        ]
        == "acme"
    )


def test_duplicate_fields_and_reordered_same_key_refused(owned):
    b = body()
    b["fields"] = [b["fields"][0], dict(b["fields"][0])]
    assert create(owned, b).status_code == 422
    assert owned.svc.entity_for_tenant("acme") is None
    b = body()
    owned.ok(create(owned, b), 201)
    reordered = {**b, "fields": list(reversed(b["fields"]))}
    owned.refused(create(owned, reordered), 409, "REQUEST_ID_REUSED")


def test_service_auth_and_bad_founder_token_and_privacy_keys(owned):
    path = "/seo/v1/tenants/acme/entity"
    assert owned.client.post(path, json=body()).status_code == 401
    headers = owned.headers("dashboard", True)
    headers["X-Andre-Approval-Token"] = secrets.token_hex(32)
    owned.refused(
        owned.client.post(path, json=body(), headers=headers),
        403,
        "ANDRE_APPROVAL_INVALID",
    )
    b = body()
    b["fields"][0]["telephone"] = "private-shaped-extra"
    r = create(owned, b)
    assert r.status_code == 422 and "forbidden_field" in r.text
    assert owned.svc.entity_for_tenant("acme") is None


def test_entity_id_collision_does_not_reassign_other_tenant(owned):
    # Synthetic hash/name collision guard, not canonical onboarding by state injection.
    other = copy.deepcopy(owned.svc.entities["ent-zbm"])
    other.update(entity_id="ent-acme", tenant_id="globex")
    owned.svc.entities["ent-acme"] = other
    owned.refused(create(owned), 409, "ENTITY_EXISTS")
    assert owned.svc.entities["ent-acme"] == other
    assert owned.svc.entity_for_tenant("acme") is None


@pytest.mark.parametrize("boundary", ["anchor", "append"])
def test_anchor_or_local_append_uncertainty_rolls_forward_without_reset(
    owned, boundary
):
    from store import StoreWriteError

    b = body()
    record = owned.ledger.record_event
    append = owned.svc.log.append_prepared

    def lost(*a, **kw):
        event_type = kw.get("event_type", a[2] if len(a) > 2 else None)
        if event_type == "log_anchor":
            record(*a, **kw)
            raise LedgerRecordError("owned anchor response loss")
        return record(*a, **kw)

    def failed(rec, line):
        raise StoreWriteError("owned append fault")

    if boundary == "anchor":
        owned.ledger.record_event = lost
    else:
        owned.svc.log.append_prepared = failed
    r = create(owned, b)
    assert r.status_code == 503
    assert owned.svc.entity_for_tenant("acme") is None
    owned.ledger.record_event = record
    owned.svc.log.append_prepared = append
    recovered = owned.restart()
    try:
        e = recovered.ok(create(recovered, b), 201)
        assert e["version"] == 1 and e["tenant_id"] == "acme"
        assert len(recovered.ledger.of_type("entity_created")) == 2
    finally:
        recovered.svc.close()


def test_closed_service_and_integrity_gate_refuse_create(owned):
    owned.svc.integrity = {
        "ok": False,
        "checked_at": owned.svc.integrity["checked_at"],
        "problem": "owned unverified",
    }
    owned.ledger.fail_reads = True
    assert create(owned).status_code == 503
    assert owned.svc.entity_for_tenant("acme") is None
    owned.ledger.fail_reads = False
    owned.svc.close()
    assert create(owned).status_code == 503


def test_tenant_id_max_bound_and_exact_route_auth_inventory(owned):
    tid = "a" * 40
    owned.tenant(tid)
    e = owned.ok(owned.post("/tenants/" + tid + "/entity", body(), andre=True), 201)
    assert e["entity_id"] == "ent-" + tid and len(e["entity_id"]) == 44
    routes = [
        r
        for r in owned.client.app.app.app.routes
        if getattr(r, "path", None) == "/seo/v1/tenants/{tid}/entity"
    ]
    assert {method for r in routes for method in r.methods} == {"GET", "POST"}
    post = next(r for r in routes if "POST" in r.methods)
    names = {d.call.__name__ for d in post.dependant.dependencies}
    assert "andre" in names
