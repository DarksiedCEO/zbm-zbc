"""Andre's passkey: enrolment, approvals bound to exactly one action, single use, clone detection, recovery."""

from __future__ import annotations

import base64
import json

import pytest

import cbor
import webauthn
from helpers import ENROLL_TOKEN, ORIGIN, RP_ID, Authenticator, Harness, b64u, cbor_dumps, rid

REF = "vault:finance_31.k"


def store_body(**kw):
    return {"request_id": rid(), "owner": "finance_31", "name": "k", "kind": "api_key", "value": "v", **kw}


@pytest.mark.parametrize("alg", [-7, -8, -257])
def test_enrol_and_approve_with_each_algorithm(h, alg):
    a = h.enroll(alg=alg)
    h.ok(h.post("/sec/v1/secrets/andre", h.approved("SECRET_STORE", REF, store_body(), key=a)), 201)
    assert h.ok(h.get("/sec/v1/passkeys"))[0]["alg"] == alg


def test_first_enrolment_needs_the_enroll_token(h):
    r = h.post("/sec/v1/passkeys/enroll/options", {"enroll_token": "x" * 40})
    assert r.status_code == 403 and r.json()["detail"] == "ENROLL_TOKEN_REFUSED"
    assert h.post("/sec/v1/passkeys/enroll/options", {}).status_code == 403


def test_enroll_token_works_once(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"))
    h.enroll()
    h2 = h.restart()
    r = h2.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN})
    assert r.status_code == 403          # a passkey exists: the token is no longer a way in


def test_a_lost_log_cannot_reopen_enrolment(h):
    """In memory, a restart loses the log; the ledger still holds its anchors, so nothing works (fail closed)
    instead of the empty log letting someone enrol a passkey with the old token."""
    h.enroll()
    h2 = h.restart()
    r = h2.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN})
    assert r.status_code == 503 and r.json()["detail"] == "INTEGRITY_UNVERIFIED"


def test_enroll_token_cannot_be_reused_after_revocation_dance(h):
    a = h.enroll()
    b = h.enroll()
    h.ok(h.post(f"/sec/v1/passkeys/{a.credential_id}/revoke",
                h.approved("PASSKEY_REVOKE", a.credential_id, {"request_id": rid()}, key=b)))
    assert h.post(f"/sec/v1/passkeys/{b.credential_id}/revoke",
                  h.approved("PASSKEY_REVOKE", b.credential_id, {"request_id": rid()}, key=b)).json()["detail"] \
        == "LAST_PASSKEY"


def test_second_passkey_needs_an_approval_from_the_first(hk):
    r = hk.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN})
    assert r.status_code == 403 and r.json()["detail"] == "ENROLL_NEEDS_APPROVAL"
    hk.enroll()
    assert len(hk.ok(hk.get("/sec/v1/passkeys"))) == 2
    assert hk.ok(hk.get("/sec/v1/status"))["passkey_warning"] is None


def test_health_warns_until_two_passkeys(h):
    assert "no passkey" in h.ok(h.get("/sec/v1/status"))["passkey_warning"]
    h.enroll()
    assert "only one" in h.ok(h.get("/sec/v1/status"))["passkey_warning"]


def test_no_approval_no_action(hk):
    r = hk.post("/sec/v1/secrets/andre", store_body())
    assert r.status_code == 422           # the approval field is required by the model
    r = hk.post("/sec/v1/freezes", {"request_id": rid(), "target_kind": "caller", "target_id": "finance_31",
                                    "reason_code": "TEST"})
    assert r.status_code == 422


def test_approval_bound_to_exactly_the_body(hk):
    body = store_body()
    approved = hk.approved("SECRET_STORE", REF, body)
    tampered = {**approved, "value": "attacker_value"}
    r = hk.post("/sec/v1/secrets/andre", tampered)
    assert r.status_code == 403 and r.json()["detail"] == "APPROVAL_ACTION_MISMATCH"


def test_approval_bound_to_the_action(hk):
    body = store_body()
    ch = hk.challenge("SECRET_STORE", REF, body)
    hk.ok(hk.post("/sec/v1/secrets/andre", {**body, "approval": hk.keys[0].assert_(ch)}), 201)
    fbody = {"request_id": rid(), "target_kind": "caller", "target_id": "finance_31", "reason_code": "TEST"}
    ch2 = hk.challenge("SECRET_STORE", REF, store_body())
    r = hk.post("/sec/v1/freezes", {**fbody, "approval": hk.keys[0].assert_(ch2)})
    assert r.status_code == 403 and r.json()["detail"] == "APPROVAL_ACTION_MISMATCH"


def test_approval_is_single_use(hk):
    body = store_body()
    approved = hk.approved("SECRET_STORE", REF, body)
    hk.ok(hk.post("/sec/v1/secrets/andre", approved), 201)
    again = {**approved, "request_id": rid()}
    r = hk.post("/sec/v1/secrets/andre", again)
    assert r.status_code == 403 and r.json()["detail"] in ("APPROVAL_CHALLENGE_USED", "APPROVAL_ACTION_MISMATCH")
    # same request id: idempotent replay of the original answer, nothing new
    assert hk.post("/sec/v1/secrets/andre", approved).status_code == 201


def test_a_failed_attempt_burns_the_challenge(hk):
    body = store_body()
    ch = hk.challenge("SECRET_STORE", REF, body)
    bad = hk.keys[0].assert_(ch)
    bad["signature"] = b64u(b"\x30" + b"\x00" * 70)
    assert hk.post("/sec/v1/secrets/andre", {**body, "approval": bad}).status_code == 403
    good = hk.keys[0].assert_(ch)
    r = hk.post("/sec/v1/secrets/andre", {**body, "approval": good})
    assert r.status_code == 403 and r.json()["detail"] == "APPROVAL_CHALLENGE_USED"


def test_expired_challenge(hk):
    body = store_body()
    ch = hk.challenge("SECRET_STORE", REF, body)
    hk.svc.challenges[ch["challenge_id"]]["expires"] = 0
    r = hk.post("/sec/v1/secrets/andre", {**body, "approval": hk.keys[0].assert_(ch)})
    assert r.json()["detail"] == "APPROVAL_CHALLENGE_EXPIRED"


@pytest.mark.parametrize("over,code", [
    ({"cd_over": {"origin": "https://evil.example"}}, "PASSKEY_ORIGIN_REFUSED"),
    ({"cd_over": {"crossOrigin": True}}, "PASSKEY_CROSS_ORIGIN_REFUSED"),
    ({"cd_over": {"type": "webauthn.create"}}, "PASSKEY_WRONG_CEREMONY"),
    ({"cd_over": {"challenge": b64u(b"x" * 32)}}, "PASSKEY_CHALLENGE_MISMATCH"),
    ({"flags": 0x01}, "PASSKEY_USER_NOT_VERIFIED"),
    ({"flags": 0x04}, "PASSKEY_USER_NOT_PRESENT"),
    ({"rp_id": "evil.example"}, "PASSKEY_RP_MISMATCH"),
])
def test_assertion_checks(hk, over, code):
    body = store_body()
    ch = hk.challenge("SECRET_STORE", REF, body)
    r = hk.post("/sec/v1/secrets/andre", {**body, "approval": hk.keys[0].assert_(ch, **over)})
    assert r.status_code == 403 and r.json()["detail"] == code


def test_unknown_credential(hk):
    other = Authenticator()
    body = store_body()
    ch = hk.challenge("SECRET_STORE", REF, body)
    r = hk.post("/sec/v1/secrets/andre", {**body, "approval": other.assert_(ch)})
    assert r.json()["detail"] == "APPROVAL_CREDENTIAL_UNKNOWN"


def test_counter_regression_suspends_the_key_and_raises_sev1(hk):
    a = hk.keys[0]
    hk.ok(hk.post("/sec/v1/secrets/andre", hk.approved("SECRET_STORE", REF, store_body())), 201)
    body = store_body(name="k2")
    ch = hk.challenge("SECRET_STORE", "vault:finance_31.k2", body)
    r = hk.post("/sec/v1/secrets/andre", {**body, "approval": a.assert_(ch, count=1)})
    assert r.json()["detail"] == "PASSKEY_COUNTER_REGRESSION"
    assert hk.ok(hk.get("/sec/v1/passkeys"))[0]["status"] == "suspended"
    assert any(i["code"] == "PASSKEY_CLONE_SUSPECTED" and i["severity"] == "sev1" for i in hk.svc.incidents.values())
    r = hk.post("/sec/v1/secrets/andre", hk.approved("SECRET_STORE", "vault:finance_31.k2", store_body(name="k2")))
    assert r.json()["detail"] == "PASSKEY_SUSPENDED"


def test_zero_counter_passkeys_are_accepted(h):
    a = h.enroll(counter_step=0)
    for i in range(3):
        h.ok(h.post("/sec/v1/secrets/andre", h.approved("SECRET_STORE", f"vault:finance_31.k{i}",
                                                         store_body(name=f"k{i}"), key=a)), 201)


def test_sign_count_survives_restart(tmp_path):
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    a = h.enroll()
    h.ok(h.post("/sec/v1/secrets/andre", h.approved("SECRET_STORE", REF, store_body())), 201)
    h.svc.log  # noqa: B018
    h2 = h.restart()
    body = store_body(name="k2")
    ch = h2.challenge("SECRET_STORE", "vault:finance_31.k2", body)
    r = h2.post("/sec/v1/secrets/andre", {**body, "approval": a.assert_(ch, count=1)})
    assert r.json()["detail"] == "PASSKEY_COUNTER_REGRESSION"


def test_registration_refuses_attestation_formats_and_missing_uv(h):
    opts = h.ok(h.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN}))
    a = Authenticator()
    r = h.post("/sec/v1/passkeys/enroll", a.register(opts, fmt="packed"))
    assert r.json()["detail"] == "PASSKEY_ATTESTATION_FORMAT_REFUSED"
    opts = h.ok(h.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN}))
    r = h.post("/sec/v1/passkeys/enroll", a.register(opts, flags=0x41))
    assert r.json()["detail"] == "PASSKEY_USER_NOT_VERIFIED"


def test_registration_challenge_is_single_use(h):
    opts = h.ok(h.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN}))
    a = Authenticator()
    h.ok(h.post("/sec/v1/passkeys/enroll", a.register(opts)), 201)
    r = h.post("/sec/v1/passkeys/enroll", Authenticator().register(opts))
    assert r.json()["detail"] == "APPROVAL_CHALLENGE_USED"


def test_an_approval_challenge_cannot_be_used_to_enrol(hk):
    ch = hk.challenge("SECRET_STORE", REF, store_body())
    r = hk.post("/sec/v1/passkeys/enroll", Authenticator().register(ch))
    assert r.json()["detail"] == "APPROVAL_CHALLENGE_UNKNOWN"


def test_challenge_for_unknown_action_refused(hk):
    r = hk.post("/sec/v1/approvals/challenges", {"action": "MAKE_ME_ADMIN", "target": "", "body": {}})
    assert r.status_code == 422


def test_challenges_only_through_the_dashboard(hk):
    r = hk.post("/sec/v1/approvals/challenges", {"action": "FREEZE", "target": "", "body": {}}, caller="finance_31")
    assert r.status_code == 403


def test_passkeys_not_configured_means_nothing_approvable(tmp_path):
    h = Harness(tmp_path, SEC_WEBAUTHN_RP_ID=None, SEC_WEBAUTHN_ORIGINS=None)
    r = h.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN})
    assert r.status_code == 503 and r.json()["detail"] == "PASSKEYS_NOT_CONFIGURED"


def test_repeated_approval_failures_open_an_incident(hk):
    for _ in range(3):
        body = store_body()
        ch = hk.challenge("SECRET_STORE", REF, body)
        bad = hk.keys[0].assert_(ch, cd_over={"origin": "https://evil.example"})
        hk.post("/sec/v1/secrets/andre", {**body, "approval": bad})
    assert any(i["code"] == "APPROVAL_FAILURES" for i in hk.svc.incidents.values())


def test_recovery_revokes_every_passkey_and_reopens_enrolment(tmp_path):
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    h.enroll()
    new_token = "recovery-token-0123456789abcdefghijklmnopq"
    from helpers import secret_file
    f = secret_file(tmp_path, "recovery.token", new_token.encode())
    h2 = h.restart(SEC_ANDRE_ENROLL_TOKEN_FILE=f, SEC_PASSKEY_RECOVERY="1")
    assert all(p["status"] == "revoked" for p in h2.ok(h2.get("/sec/v1/passkeys")))
    assert any(i["code"] == "PASSKEY_RECOVERY_STARTED" for i in h2.svc.incidents.values())
    opts = h2.ok(h2.post("/sec/v1/passkeys/enroll/options", {"enroll_token": new_token}))
    h2.ok(h2.post("/sec/v1/passkeys/enroll", Authenticator().register(opts)), 201)
    # restarting with the same flag does not reset again, and the used token is spent
    h3 = h2.restart(SEC_ANDRE_ENROLL_TOKEN_FILE=f, SEC_PASSKEY_RECOVERY="1")
    assert sum(p["status"] == "active" for p in h3.ok(h3.get("/sec/v1/passkeys"))) == 1


# --- verifier unit tests ------------------------------------------------------------------------------------

def test_b64url_decoding_is_strict():
    for bad in ("a+b", "a/b", "ab==", "", "a" * 50_000):
        with pytest.raises(webauthn.WebAuthnError):
            webauthn.b64url_decode(bad, 1000, "X")
    with pytest.raises(webauthn.WebAuthnError):
        webauthn.b64url_decode("AB", 10, "X")     # non-canonical: trailing bits set


def test_cose_keys_refused():
    with pytest.raises(webauthn.WebAuthnError):
        webauthn.cose_public_key({1: 2, 3: -35, -1: 2})                     # ES384 not accepted
    with pytest.raises(webauthn.WebAuthnError):
        webauthn.cose_public_key({1: 2, 3: -7, -1: 1, -2: b"\0" * 32, -3: b"\0" * 32})   # not on the curve
    with pytest.raises(webauthn.WebAuthnError):
        webauthn.cose_public_key({1: 3, 3: -257, -1: b"\x01" * 128, -2: b"\x01\x00\x01"})  # 1024-bit RSA


def test_auth_data_trailing_bytes_refused():
    a = Authenticator()
    ad = a.auth_data(0x45, attested=True) + b"\x00"
    with pytest.raises(webauthn.WebAuthnError):
        webauthn.parse_auth_data(ad)


def test_cbor_strictness():
    for bad in (b"\x9f\x01\xff", b"\x18\x01", b"\xc0\x00", b"\xa2\x01\x01\x01\x02", b"\xf9\x00\x00", b"\x01\x02",
                b"\x81" * 20 + b"\x01"):
        with pytest.raises(cbor.CBORError):
            cbor.loads(bad)
    assert cbor.loads(cbor_dumps({"a": [1, -2, b"x", "y", True, None]})) == {"a": [1, -2, b"x", "y", True, None]}


def test_origins_must_sit_under_the_rp_id():
    assert webauthn.origins_match_rp((ORIGIN,), RP_ID)
    assert not webauthn.origins_match_rp(("https://zbm.test.evil.example",), RP_ID)
    assert not webauthn.origins_match_rp(("https://notzbm.test",), RP_ID)
    with pytest.raises(ValueError):
        webauthn.allowed_origins("https://x.test/path")


def test_client_data_must_be_json_object():
    rp = webauthn.Relying(RP_ID, (ORIGIN,))
    cred = webauthn.Credential("AAAA", -7, base64.b64encode(b"").decode(), 0, "", False)
    with pytest.raises(webauthn.WebAuthnError):
        webauthn.verify_assertion(cred, b64u(json.dumps([1]).encode()), "AAAA", "AAAA", b"x" * 32, rp)
