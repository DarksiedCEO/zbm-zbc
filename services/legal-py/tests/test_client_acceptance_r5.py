"""AEGIS r4 on ADR 0028 (aegis-client-acceptance-r4.md @05832146): the Legal side.

- N4-L1: a link token has exactly one valid spelling (base64url re-encodes to itself); the 15 other spellings of the
  same bytes are refused.
- N4-L2: on the portal's private calls, a 404 says whether it is final (unknown, used, expired, revoked:
  ``"final": true``) or not (the link's documents cannot be served right now); a blob read error is a 503, never a
  404.
- N4-L3: Legal's private /health counts tokens of another link format version that reached it.
"""

from __future__ import annotations

import base64
import errno

import pytest

import link_tag as LT
import store as store_mod
from helpers import LINK_TAG_KEY, Harness, rid
from test_client_acceptance_portal import accept, determine_cq19, docs_of, invite, msa, resolve, sow_fill


@pytest.fixture
def pair():
    x = Harness()
    x.approve_rules()
    determine_cq19(x)
    m, s = msa(x), sow_fill(x)
    return x, m, s, docs_of(m, s)


def spellings(token: str) -> list:
    """Every 66-character string that decodes to the same 49 bytes (the last character carries 4 unused bits)."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    raw = base64.urlsafe_b64decode(token + "==")
    out = []
    for ch in alphabet:
        t = token[:-1] + ch
        try:
            if base64.urlsafe_b64decode(t + "==") == raw:
                out.append(t)
        except ValueError:
            pass
    return out


# --- N4-L1 ---------------------------------------------------------------------------------------------------------

def test_a_token_has_one_valid_spelling(pair):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    alts = spellings(tok)
    assert len(alts) == 16 and tok in alts
    assert [t for t in alts if LT.valid(t, LINK_TAG_KEY.encode())] == [tok]
    assert all(LT.version_of(t) is None for t in alts if t != tok)
    for t in alts:
        if t != tok:
            r = resolve(x, t)
            assert r.status_code == 404 and r.json()["final"] is True


# --- N4-L2 ---------------------------------------------------------------------------------------------------------

def test_final_404s_say_so(pair):
    x, m, s, docs = pair
    unknown = LT.mint(LINK_TAG_KEY.encode())
    r = resolve(x, unknown)
    assert r.status_code == 404 and r.json() == {"detail": "not found", "final": True}
    forged = "A" * 66
    assert resolve(x, forged).json() == {"detail": "not found", "final": True}
    used = invite(x, docs, party="client:acme-1")
    assert resolve(x, used["token"]).status_code == 200
    # revoked
    x.ok(x.apost(f"/legal/v1/client-acceptance-invitations/{used['invitation_id']}/revoke", {"request_id": rid()}))
    assert resolve(x, used["token"]).json() == {"detail": "not found", "final": True}


def test_a_used_and_an_expired_link_are_final(pair):
    x, m, s, docs = pair
    out, out2 = invite(x, docs), invite(x, docs)
    x.svc.client_invitations[out2["invitation_id"]]["expires_at"] = "2000-01-01T00:00:00Z"
    assert resolve(x, out2["token"]).json() == {"detail": "not found", "final": True}
    assert accept(x, out["token"], docs).status_code == 201
    assert resolve(x, out["token"]).json() == {"detail": "not found", "final": True}


def test_a_link_whose_documents_cannot_be_served_now_is_not_final(pair, monkeypatch):
    x, m, s, docs = pair
    out = invite(x, docs)
    real = x.svc.blobs.get
    monkeypatch.setattr(x.svc.blobs, "get", lambda sha: None)        # a blob missing (not a read error)
    r = resolve(x, out["token"])
    assert r.status_code == 404 and r.json() == {"detail": "not found"} and "final" not in r.json()
    monkeypatch.setattr(x.svc.blobs, "get", real)
    assert resolve(x, out["token"]).status_code == 200


def test_a_blob_read_error_is_a_503_never_a_404(pair, monkeypatch):
    x, m, s, docs = pair
    out = invite(x, docs)
    real = x.svc.blobs.get

    def eio(sha):
        raise store_mod.StoreReadError("blob read failed: OSError")
    monkeypatch.setattr(x.svc.blobs, "get", eio)
    r = resolve(x, out["token"])
    assert r.status_code == 503 and "final" not in r.json()
    assert accept(x, out["token"], docs).status_code == 503
    monkeypatch.setattr(x.svc.blobs, "get", real)
    assert resolve(x, out["token"]).status_code == 200                  # the fault cleared: the link still works


def test_the_blob_store_raises_on_an_os_read_error_and_returns_none_for_a_missing_blob(tmp_path, monkeypatch):
    bs = store_mod.BlobStore(str(tmp_path))
    sha = bs.put(b"hello")
    assert bs.get(sha) == b"hello"
    assert bs.get("0" * 64) is None                                    # missing: not a fault

    def broken_open(*a, **k):
        raise OSError(errno.EIO, "I/O error")
    monkeypatch.setattr(store_mod, "open", broken_open, raising=False)
    with pytest.raises(store_mod.StoreReadError):
        bs.get(sha)


# --- N4-L3 ---------------------------------------------------------------------------------------------------------

def test_legals_private_health_counts_tokens_of_another_format_version(pair):
    x, m, s, docs = pair
    tok = invite(x, docs)["token"]
    raw = bytearray(base64.urlsafe_b64decode(tok + "=="))
    raw[0] = 2
    other = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    assert resolve(x, other).status_code == 404
    h = x.client.get("/health").json()["link_tags"]
    assert h["status"] == "ok" and h["foreign_versions_seen"] == {"2": 1}


# --- AEGIS r5 N5-L2 ------------------------------------------------------------------------------------------------

def test_an_unreadable_blob_is_its_own_red_line_in_the_integrity_report(tmp_path, monkeypatch):
    h = Harness(data_dir=str(tmp_path / "data"))
    h.approve_rules()
    m = msa(h)
    assert h.ok(h.get("/legal/v1/integrity"))["status"] == "green"
    real = h.svc.blobs.get

    def flaky(sha):
        if sha == m["sha256"]:
            raise store_mod.StoreReadError("blob read failed: OSError")
        return real(sha)
    monkeypatch.setattr(h.svc.blobs, "get", flaky)
    r = h.get("/legal/v1/integrity")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["status"] == "red"
    assert "1 blob(s) unreadable (read error)" in rep["problems"]
    assert not any("missing or not matching" in p for p in rep["problems"])
