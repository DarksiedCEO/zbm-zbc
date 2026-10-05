"""The vault: store, use, rotate, destroy, access control, sealing, and what never leaks."""

from __future__ import annotations

import base64
import json
import os

import pytest

import crypto
from helpers import CALLERS, Harness, rid

REF = "vault:finance_31.stripe_secret"


def store_finance(hk, **kw):
    return hk.andre_store("finance_31", "stripe_secret", readers=["finance_31"], purposes=["stripe_api"], **kw)


def test_andre_store_then_reader_uses_for_declared_purpose(hk):
    v = store_finance(hk)
    assert v["ref"] == REF and v["version"] == 1 and "value" not in v
    r = hk.ok(hk.use("finance_31", REF, "stripe_api"))
    assert r["value"] == "sk_value_123" and r["version"] == 1
    # recorded on the ledger BEFORE it was returned, and the ledger never holds the value
    rel = hk.ledger.of_type("secret_released")
    assert len(rel) == 1 and rel[0]["event_id"] == r["ledger_event_id"]
    assert "sk_value_123" not in json.dumps(hk.ledger.events)


def test_ref_without_prefix_is_the_same_secret(hk):
    store_finance(hk)
    assert hk.ok(hk.use("finance_31", "finance_31.stripe_secret", "stripe_api"))["value"] == "sk_value_123"


def test_non_reader_gets_not_found_never_confirmation(hk):
    store_finance(hk)
    for caller in ("legal_37", "onboarding", "delivery_28"):
        r = hk.use(caller, REF, "stripe_api")
        assert r.status_code == 404 and r.json() == {"detail": "SECRET_NOT_FOUND"}
    assert hk.get(f"/sec/v1/secrets/{REF}", caller="legal_37").status_code == 404


def test_wrong_purpose_is_refused(hk):
    store_finance(hk)
    r = hk.use("finance_31", REF, "something_else")
    assert r.status_code == 403 and r.json()["detail"] == "PURPOSE_NOT_ALLOWED"


def test_dashboard_can_never_read_a_value(hk):
    store_finance(hk)
    r = hk.use("dashboard", REF, "stripe_api")
    assert r.status_code == 403
    assert "value" not in hk.ok(hk.get(f"/sec/v1/secrets/{REF}"))


def test_service_stores_its_own_secret_write_only(hk):
    body = {"request_id": rid(), "name": "client-c1-shopify", "kind": "oauth_token", "value": "tok",
            "client_id": "c1"}
    v = hk.ok(hk.post("/sec/v1/secrets", body, caller="onboarding"), 201)
    assert v["readers"] == [] and v["owner"] == "onboarding"
    # nobody, the owner included, can read a write-only secret back
    assert hk.use("onboarding", v["ref"], "x").status_code == 404


def test_service_cannot_grant_another_department(hk):
    body = {"request_id": rid(), "name": "k", "kind": "api_key", "value": "v", "readers": ["finance_31"],
            "purposes": ["p"]}
    r = hk.post("/sec/v1/secrets", body, caller="onboarding")
    assert r.status_code == 403 and r.json()["detail"] == "READERS_NOT_ALLOWED"


def test_service_may_be_its_own_reader(hk):
    body = {"request_id": rid(), "name": "llm_key", "kind": "api_key", "value": "v1", "readers": ["delivery_28"],
            "purposes": ["model_call"]}
    hk.ok(hk.post("/sec/v1/secrets", body, caller="delivery_28"), 201)
    assert hk.ok(hk.use("delivery_28", "vault:delivery_28.llm_key", "model_call"))["value"] == "v1"


def test_readers_without_purposes_refused(hk):
    body = {"request_id": rid(), "name": "k", "kind": "api_key", "value": "v", "readers": ["onboarding"]}
    assert hk.post("/sec/v1/secrets", body, caller="onboarding").status_code == 422


def test_dashboard_cannot_store_as_a_service(hk):
    body = {"request_id": rid(), "name": "k", "kind": "api_key", "value": "v"}
    assert hk.post("/sec/v1/secrets", body, caller="dashboard").status_code == 403


def test_duplicate_name_refused_and_request_id_idempotent(hk):
    body = {"request_id": rid(), "name": "k", "kind": "api_key", "value": "v"}
    a = hk.ok(hk.post("/sec/v1/secrets", body, caller="onboarding"), 201)
    b = hk.ok(hk.post("/sec/v1/secrets", body, caller="onboarding"), 201)
    assert a == b
    r = hk.post("/sec/v1/secrets", {**body, "value": "other"}, caller="onboarding")
    assert r.status_code == 409 and r.json()["detail"] == "REQUEST_ID_REUSED"
    r = hk.post("/sec/v1/secrets", {**body, "request_id": rid()}, caller="onboarding")
    assert r.status_code == 409 and r.json()["detail"] == "SECRET_EXISTS"


def test_base64_values_round_trip(hk):
    raw = os.urandom(40)
    hk.andre_store("verification_integrity", "bin", value=base64.b64encode(raw).decode(), encoding="base64",
                   readers=["verification_integrity"], purposes=["p"])
    r = hk.ok(hk.use("verification_integrity", "vault:verification_integrity.bin", "p"))
    assert base64.b64decode(r["value_b64"]) == raw and "value" not in r


def test_hmac_key_is_generated_inside_and_32_bytes(hk):
    v = hk.andre_store("finance_31", "identity_hmac_key", kind="hmac_key", value=None, readers=["finance_31"],
                       purposes=["payout_identity"])
    r = hk.ok(hk.use("finance_31", v["ref"], "payout_identity"))
    assert len(base64.b64decode(r["value_b64"])) == 32


def test_hmac_key_cannot_be_supplied(hk):
    body = {"request_id": rid(), "owner": "finance_31", "name": "k", "kind": "hmac_key", "value": "chosen"}
    r = hk.post("/sec/v1/secrets/andre", hk.approved("SECRET_STORE", "vault:finance_31.k", body))
    assert r.status_code == 422 and r.json()["detail"] == "VALUE_NOT_ALLOWED"


def test_rotate_by_owner_and_old_version_is_gone(hk):
    store_finance(hk)
    v = hk.ok(hk.post(f"/sec/v1/secrets/{REF}/rotate", {"request_id": rid(), "value": "sk_new"}, caller="finance_31"))
    assert v["version"] == 2
    r = hk.ok(hk.use("finance_31", REF, "stripe_api"))
    assert r["value"] == "sk_new" and r["version"] == 2
    sid = hk.svc.by_ref[REF]
    assert hk.svc.sealed.get(sid, 1) is None and hk.svc.sealed.get(sid, 2) is not None


def test_rotate_by_a_reader_who_is_not_owner_refused(hk):
    hk.andre_store("onboarding", "shared", readers=["finance_31"], purposes=["p"])
    r = hk.post("/sec/v1/secrets/vault:onboarding.shared/rotate", {"request_id": rid(), "value": "x"},
                caller="finance_31")
    assert r.status_code == 404


def test_service_cannot_pass_an_approval_field(hk):
    store_finance(hk)
    body = {"request_id": rid(), "value": "x", "approval": {"challenge_id": "c", "credential_id": "AAAA",
                                                            "client_data_json": "AAAA", "authenticator_data": "AAAA",
                                                            "signature": "AAAA"}}
    assert hk.post(f"/sec/v1/secrets/{REF}/rotate", body, caller="finance_31").status_code == 422


def test_andre_rotate_needs_passkey(hk):
    store_finance(hk)
    r = hk.post(f"/sec/v1/secrets/{REF}/rotate", {"request_id": rid(), "value": "x"})
    assert r.status_code == 403 and r.json()["detail"] == "APPROVAL_REQUIRED"
    body = {"request_id": rid(), "value": "sk_andre"}
    hk.ok(hk.post(f"/sec/v1/secrets/{REF}/rotate", hk.approved("SECRET_ROTATE", REF, body)))
    assert hk.ok(hk.use("finance_31", REF, "stripe_api"))["value"] == "sk_andre"


def test_destroy_makes_it_unrecoverable(hk):
    store_finance(hk)
    sid = hk.svc.by_ref[REF]
    hk.ok(hk.post(f"/sec/v1/secrets/{REF}/destroy", {"request_id": rid()}, caller="finance_31"))
    assert hk.use("finance_31", REF, "stripe_api").status_code == 404
    assert hk.svc.sealed.get(sid, 1) is None
    # the name may be reused; it is a NEW secret (new id), the old one stays destroyed
    v = store_finance(hk)
    assert hk.svc.by_ref[REF] != sid and v["version"] == 1


def test_destroy_client_wipes_only_that_clients_secrets_of_that_owner(hk):
    for name, client in (("a", "c1"), ("b", "c1"), ("c", "c2")):
        hk.ok(hk.post("/sec/v1/secrets", {"request_id": rid(), "name": name, "kind": "oauth_token", "value": "v",
                                          "client_id": client}, caller="onboarding"), 201)
    hk.ok(hk.post("/sec/v1/secrets", {"request_id": rid(), "name": "d", "kind": "api_key", "value": "v",
                                      "client_id": "c1"}, caller="fulfillment"), 201)
    r = hk.ok(hk.post("/sec/v1/clients/c1/destroy", {"request_id": rid()}, caller="onboarding"))
    assert r == {"client_id": "c1", "destroyed": 2, "held": 0}
    left = {s["ref"]: s["status"] for s in hk.svc.secrets.values()}
    assert left["vault:onboarding.c"] == "active" and left["vault:fulfillment.d"] == "active"


def test_set_access_grants_and_revokes(hk):
    hk.andre_store("verification_integrity", "yt_client", readers=[], purposes=[])
    ref = "vault:verification_integrity.yt_client"
    assert hk.use("verification_integrity", ref, "oauth").status_code == 404
    body = {"request_id": rid(), "readers": ["verification_integrity"], "purposes": ["oauth"]}
    hk.ok(hk.post(f"/sec/v1/secrets/{ref}/access", hk.approved("SECRET_ACCESS", ref, body)))
    assert hk.use("verification_integrity", ref, "oauth").status_code == 200
    body = {"request_id": rid(), "readers": [], "purposes": []}
    hk.ok(hk.post(f"/sec/v1/secrets/{ref}/access", hk.approved("SECRET_ACCESS", ref, body)))
    assert hk.use("verification_integrity", ref, "oauth").status_code == 404


def test_signing_key_is_never_releasable_or_listed(hk):
    kid = hk.svc.health()["signing_key"]
    assert kid
    assert all(s["owner"] != "cybersecurity" for s in hk.ok(hk.get("/sec/v1/secrets")))
    for caller in ("finance_31", "orchestrator"):
        assert hk.use(caller, f"vault:cybersecurity.signing-key-{kid}", "x").status_code in (403, 404, 422)


def test_tampered_sealed_file_is_refused_and_opens_incident(hk):
    store_finance(hk)
    sid = hk.svc.by_ref[REF]
    env = json.loads(hk.svc.sealed.get(sid, 1))
    ct = bytearray(base64.b64decode(env["ciphertext"]))
    ct[0] ^= 1
    env["ciphertext"] = base64.b64encode(bytes(ct)).decode()
    hk.svc.sealed.put(sid, 1, json.dumps(env, sort_keys=True, separators=(",", ":")).encode())
    r = hk.use("finance_31", REF, "stripe_api")
    assert r.status_code == 503 and r.json()["detail"] == "SEAL_BROKEN"
    assert any(i["code"] == "SEALED_SECRET_TAMPERED" for i in hk.svc.incidents.values())


def test_sealed_file_moved_to_another_secret_does_not_open(hk):
    """Context binding: a sealed value copied under another secret's id fails authentication."""
    store_finance(hk)
    hk.andre_store("legal_37", "other", value="legal_value", readers=["legal_37"], purposes=["p"])
    a, b = hk.svc.by_ref[REF], hk.svc.by_ref["vault:legal_37.other"]
    s = hk.svc.secrets[b]
    s["versions"]["1"]["envelope_sha256"] = __import__("hashlib").sha256(hk.svc.sealed.get(a, 1)).hexdigest()
    hk.svc.sealed.put(b, 1, hk.svc.sealed.get(a, 1))
    assert hk.use("legal_37", "vault:legal_37.other", "p").status_code == 503


def test_crypto_context_binding_directly():
    kms = crypto.LocalFileKeyService(os.urandom(32))
    ctx = {"secret_id": "a", "version": "1", "owner": "o", "kind": "k"}
    env = crypto.seal(kms, b"hello", ctx)
    assert crypto.open_sealed(kms, env, ctx) == b"hello"
    for k in ctx:
        with pytest.raises(crypto.SealBroken):
            crypto.open_sealed(kms, env, {**ctx, k: "x"})
    with pytest.raises(crypto.SealBroken):
        crypto.open_sealed(crypto.LocalFileKeyService(os.urandom(32)), env, ctx)
    with pytest.raises(crypto.SealBroken):
        crypto.Envelope.from_bytes(b'{"v":1}')


def test_no_key_service_means_vault_unavailable(tmp_path):
    h = Harness(tmp_path, kms=crypto.NotWiredKeyService())
    h.enroll()
    body = {"request_id": rid(), "owner": "finance_31", "name": "k", "kind": "api_key", "value": "v"}
    r = h.post("/sec/v1/secrets/andre", h.approved("SECRET_STORE", "vault:finance_31.k", body))
    assert r.status_code == 503 and r.json()["detail"] == "VAULT_UNAVAILABLE"
    assert h.ok(h.get("/health"))["vault_available"] is False


def test_ledger_down_means_no_value_is_released(hk):
    store_finance(hk)
    hk.ledger.fail_types.add("secret_released")
    r = hk.use("finance_31", REF, "stripe_api")
    assert r.status_code == 503 and "sk_value_123" not in r.text


def test_ledger_down_means_nothing_is_stored(hk):
    n, sealed = len(hk.svc.log), set(hk.svc.sealed.names())
    hk.ledger.fail = True
    r = hk.post("/sec/v1/secrets", {"request_id": rid(), "name": "k", "kind": "api_key", "value": "v"},
                caller="onboarding")
    assert r.status_code == 503 and len(hk.svc.log) == n
    assert set(hk.svc.sealed.names()) == sealed          # the sealed file written first was removed again
    assert "vault:onboarding.k" not in hk.svc.by_ref


def test_release_rate_limit(tmp_path):
    h = Harness(tmp_path, SEC_RELEASE_RATE_PER_MIN="3")
    h.enroll()
    store_finance(h)
    codes = [h.use("finance_31", REF, "stripe_api").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


def test_values_never_appear_in_log_errors_or_audit(hk):
    store_finance(hk, value="SUPERSECRET_xyz_123")
    hk.use("finance_31", REF, "stripe_api")
    hk.use("finance_31", REF, "bad_purpose")
    blob = json.dumps(hk.svc.log.records) + json.dumps(hk.ledger.events)
    blob += hk.get("/sec/v1/audit/events").text + hk.get("/sec/v1/audit/access").text + hk.get("/sec/v1/status").text
    assert "SUPERSECRET" not in blob


def test_error_bodies_do_not_echo_input(hk):
    r = hk.post("/sec/v1/secrets", {"request_id": "bad id!", "name": "SECRET_ECHO", "kind": "api_key",
                                    "value": "VALUE_ECHO"}, caller="onboarding")
    assert r.status_code == 422 and "VALUE_ECHO" not in r.text


def test_unknown_caller_and_bad_bearer(hk):
    r = hk.client.post("/sec/v1/secrets/x.y/use", json={"purpose": "p"},
                       headers={"Authorization": "Bearer wrong-token-0123456789abcdef0123456789"})
    assert r.status_code == 401
    r = hk.client.post("/sec/v1/secrets/x.y/use", json={"purpose": "p"},
                       headers={**hk.headers(None), "X-SEC-Caller-Token": "nope" * 10})
    assert r.status_code == 403
    r = hk.client.get("/sec/v1/status", headers={"Authorization": ("Bearer \xe9" + "x" * 40).encode("latin-1")})
    assert r.status_code == 401


def test_every_response_is_no_store(hk):
    store_finance(hk)
    r = hk.use("finance_31", REF, "stripe_api")
    assert r.headers["cache-control"] == "no-store" and r.headers["pragma"] == "no-cache"


def test_caller_token_map_validation(tmp_path):
    import config
    from helpers import base_env
    env = base_env(tmp_path)
    with pytest.raises(RuntimeError):
        config.load({**env, "SEC_CALLER_TOKENS": json.dumps({"stranger": "x" * 40})})
    with pytest.raises(RuntimeError):
        config.load({**env, "SEC_CALLER_TOKENS": json.dumps({"finance_31": "short"})})
    same = "s" * 40
    with pytest.raises(RuntimeError):
        config.load({**env, "SEC_CALLER_TOKENS": json.dumps({"finance_31": same, "legal_37": same})})
    with pytest.raises(RuntimeError):
        config.load({**env, "SEC_CALLER_TOKENS": json.dumps({"finance_31": env["SEC_SERVICE_TOKEN"]})})
    assert set(config.load(env).caller_tokens) == set(CALLERS)
