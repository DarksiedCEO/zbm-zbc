"""ADR 0051 (number to be confirmed at merge), job 1: the client citation vault (``src/vault.py``).

Every key here is generated at run time (``os.urandom``); no key literal is committed. The ``check_*`` helpers hold
the properties the mutation tests turn off: each mutation must make one of them fail.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import pickle
import secrets
import threading

import pytest

import vault
from vault import (MAX_RECORD_BYTES, VAULT_NAME, CitationVault, VaultCorrupt, VaultKeyInvalid, VaultNotFound,
                   VaultRecordInvalid, VaultUnavailable, load_vault_key, parse_vault_key)

TENANT_A = "acme-corp"
TENANT_B = "globex-co"


def key_text() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def new_key(kid: str = "k1", previous: tuple | None = None):
    text = key_text()
    if previous:
        return parse_vault_key(text, kid, previous[0], previous[1]), text
    return parse_vault_key(text, kid), text


def marker() -> str:
    return "ZQXV-" + secrets.token_hex(12) + "-VXQZ"


def citation(m: str) -> dict:
    return {"url": f"https://news.example/{m}/story", "excerpt": f"the answer cited {m} as the best plumber",
            "title": f"Title {m}", "engine": "fixture", "position": 1}


def lines(data_dir) -> list[bytes]:
    p = os.path.join(str(data_dir), VAULT_NAME)
    if not os.path.exists(p):
        return []
    with open(p, "rb") as fh:
        return fh.read().split(b"\n")[:-1]


def rewrite(data_dir, index: int, change) -> None:
    p = os.path.join(str(data_dir), VAULT_NAME)
    ls = lines(data_dir)
    obj = json.loads(ls[index])
    change(obj)
    ls[index] = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("ascii")
    with open(p, "wb") as fh:
        fh.write(b"".join(ln + b"\n" for ln in ls))


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def flip(text: str, at: int = 0) -> str:
    raw = bytearray(unb64(text))
    raw[at] ^= 0x01
    return b64(bytes(raw))


def encodings(secret: bytes) -> list[bytes]:
    """The secret, its hex, and its base64 (both alphabets) at every alignment, as substrings that do not depend on
    the surrounding bytes."""
    out = [secret, secret.hex().encode(), secret.hex().upper().encode()]
    for shift in range(3):
        for enc in (base64.b64encode, base64.urlsafe_b64encode):
            full = enc(b"\x00" * shift + secret + b"\x00\x00\x00")
            lead = (shift * 4 + 2) // 3 + 1
            core = full[lead:lead + (len(secret) * 4) // 3 - 4]
            assert len(core) >= 16
            out.append(core)
    return out


def all_bytes_under(root) -> list[tuple[str, bytes]]:
    out = []
    for dirpath, _dirs, files in os.walk(str(root)):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out.append((p, fh.read()))
    return out


def not_found(fn) -> VaultNotFound:
    try:
        got = fn()
    except VaultNotFound as exc:
        e = exc
    else:
        raise AssertionError(f"expected VaultNotFound, got a record ({type(got).__name__})")
    assert type(e) is VaultNotFound and str(e) == vault.NOT_FOUND and e.args == (vault.NOT_FOUND,)
    assert e.__cause__ is None and e.__context__ is None
    return e


class CountingAead:
    def __init__(self, inner):
        self.inner = inner
        self.decrypts = 0

    def encrypt(self, *a):
        return self.inner.encrypt(*a)

    def decrypt(self, *a):
        self.decrypts += 1
        return self.inner.decrypt(*a)


# --- properties the mutation tests turn off -------------------------------------------------------------------------

def check_nonce_unique(tmp_path) -> None:
    key, _ = new_key()
    v = CitationVault(str(tmp_path / "nonce"), key)
    rec = citation(marker())
    for _ in range(64):
        v.put(TENANT_A, rec)
    stored = [json.loads(ln) for ln in lines(tmp_path / "nonce")]
    nonces = [s["n"] for s in stored]
    assert len(set(nonces)) == len(nonces) == 64, "a nonce was reused"
    assert len({s["c"] for s in stored}) == 64, "the same record encrypted twice gave the same ciphertext"
    assert all(len(unb64(n)) == 12 for n in nonces)


def check_moved_tenant_fails(tmp_path) -> None:
    """A line rewritten to carry tenant B's tag is not readable by B: the tenant is bound in the associated data."""
    key, _ = new_key()
    d = tmp_path / "moved"
    vid = CitationVault(str(d), key).put(TENANT_A, citation(marker()))
    rewrite(d, 0, lambda o: o.__setitem__("t", b64(key._cur.tenant_tag(TENANT_B))))
    v = CitationVault(str(d), key)
    not_found(lambda: v.get(TENANT_B, vid))
    not_found(lambda: v.get(TENANT_A, vid))


def check_moved_id_fails(tmp_path) -> None:
    """A line copied under another record id does not decrypt: the id is bound in the associated data."""
    key, _ = new_key()
    d = tmp_path / "moved-id"
    CitationVault(str(d), key).put(TENANT_A, citation(marker()))
    other = secrets.token_bytes(16)
    p = os.path.join(str(d), VAULT_NAME)
    obj = json.loads(lines(d)[0])
    obj["id"] = b64(other)
    with open(p, "ab") as fh:
        fh.write(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    v = CitationVault(str(d), key)
    assert len(v) == 2
    not_found(lambda: v.get(TENANT_A, other.hex()))


def check_cross_tenant_never_decrypts(tmp_path) -> None:
    """A wrong tenant is refused on the keyed tenant tag, before the key is used to decrypt anything."""
    key, _ = new_key()
    v = CitationVault(str(tmp_path / "xt"), key)
    vid = v.put(TENANT_A, citation(marker()))
    counter = CountingAead(key._cur._aead)
    key._cur._aead = counter
    try:
        not_found(lambda: v.get(TENANT_B, vid))
        assert counter.decrypts == 0, "a wrong tenant reached decryption"
        assert v.get(TENANT_A, vid)["engine"] == "fixture"
        assert counter.decrypts == 1
    finally:
        key._cur._aead = counter.inner


# --- tests ----------------------------------------------------------------------------------------------------------

def test_round_trip_and_restart(tmp_path):
    key, _ = new_key()
    d = str(tmp_path / "data")
    v = CitationVault(d, key)
    rec = citation(marker())
    rec["nested"] = {"list": [1, 2.5, None, True, "\u00e9\u4e2d"], "empty": {}}
    vid = v.put(TENANT_A, rec)
    assert vault.VAULT_ID.fullmatch(vid)
    assert v.get(TENANT_A, vid) == rec
    v2 = CitationVault(d, key)                      # a restart reads it back from disk
    assert v2.get(TENANT_A, vid) == rec and len(v2) == 1
    assert v2.put(TENANT_B, {"x": 1}) != vid
    assert oct(os.stat(os.path.join(d, VAULT_NAME)).st_mode & 0o777) == "0o600"
    assert oct(os.stat(d).st_mode & 0o777) == "0o700"


def test_line_format_has_only_base64_fields(tmp_path):
    key, _ = new_key("key-2026-11")
    d = tmp_path / "data"
    vid = CitationVault(str(d), key).put(TENANT_A, citation(marker()))
    (ln,) = lines(d)
    obj = json.loads(ln)
    assert set(obj) == {"v", "id", "t", "k", "n", "c"} and obj["v"] == 1
    assert unb64(obj["id"]).hex() == vid
    assert unb64(obj["k"]) == b"key-2026-11"
    assert len(unb64(obj["t"])) == 32 and len(unb64(obj["n"])) == 12
    for f in ("id", "t", "k", "n", "c"):
        assert set(obj[f]) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_vault_ids_are_random_128_bit(tmp_path):
    key, _ = new_key()
    v = CitationVault(str(tmp_path / "data"), key)
    ids = {v.put(TENANT_A, {"i": i}) for i in range(200)}
    assert len(ids) == 200 and all(len(i) == 32 for i in ids)


def test_nonce_unique_per_record(tmp_path):
    check_nonce_unique(tmp_path)


def test_no_plaintext_anywhere_under_data_dir(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    key, ktext = new_key()
    d = tmp_path / "data"
    v = CitationVault(str(d), key)
    m = marker()
    ids = [v.put(TENANT_A, citation(m)) for _ in range(5)] + [v.put(TENANT_B, {"title": m * 50})]
    assert v.get(TENANT_A, ids[0])["title"] == f"Title {m}"
    files = all_bytes_under(tmp_path)
    assert files and any(p.endswith(VAULT_NAME) for p, _ in files)
    needles = encodings(m.encode()) + encodings(unb64(ktext)) + [ktext.encode(), ktext.rstrip("=").encode(),
                                                                 TENANT_A.encode(), TENANT_B.encode()]
    for path, data in files:
        for needle in needles:
            assert needle not in data, f"plaintext-derived bytes found in {os.path.basename(path)}"
    assert not any(m in r.getMessage() for r in caplog.records)
    assert not any(ktext in r.getMessage() for r in caplog.records)


def test_cross_tenant_get_fails_exactly_like_missing(tmp_path):
    key, _ = new_key()
    v = CitationVault(str(tmp_path / "data"), key)
    vid = v.put(TENANT_A, citation(marker()))
    errors = [not_found(lambda: v.get(TENANT_B, vid)),                 # another tenant
              not_found(lambda: v.get(TENANT_A, secrets.token_hex(16))),  # unknown id
              not_found(lambda: v.get(TENANT_A, vid.upper())),          # malformed id
              not_found(lambda: v.get(TENANT_A, vid[:-1])),
              not_found(lambda: v.get("Not A Tenant", vid)),
              not_found(lambda: v.get(TENANT_A, None))]
    assert len({(type(e), str(e), e.args) for e in errors}) == 1
    assert v.get(TENANT_A, vid)["engine"] == "fixture"


def test_cross_tenant_never_reaches_decryption(tmp_path):
    check_cross_tenant_never_decrypts(tmp_path)


def test_tampered_ciphertext_fails_closed(tmp_path):
    key, _ = new_key()
    d = tmp_path / "data"
    vid = CitationVault(str(d), key).put(TENANT_A, citation(marker()))
    rewrite(d, 0, lambda o: o.__setitem__("c", flip(o["c"], 3)))
    not_found(lambda: CitationVault(str(d), key).get(TENANT_A, vid))


def test_tampered_tag_bytes_fail_closed(tmp_path):
    key, _ = new_key()
    d = tmp_path / "data"
    vid = CitationVault(str(d), key).put(TENANT_A, citation(marker()))
    rewrite(d, 0, lambda o: o.__setitem__("c", flip(o["c"], len(unb64(o["c"])) - 1)))
    not_found(lambda: CitationVault(str(d), key).get(TENANT_A, vid))


def test_tampered_nonce_fails_closed(tmp_path):
    key, _ = new_key()
    d = tmp_path / "data"
    vid = CitationVault(str(d), key).put(TENANT_A, citation(marker()))
    rewrite(d, 0, lambda o: o.__setitem__("n", flip(o["n"], 0)))
    not_found(lambda: CitationVault(str(d), key).get(TENANT_A, vid))


def test_tampered_tenant_tag_fails_closed(tmp_path):
    key, _ = new_key()
    d = tmp_path / "data"
    vid = CitationVault(str(d), key).put(TENANT_A, citation(marker()))
    rewrite(d, 0, lambda o: o.__setitem__("t", flip(o["t"], 5)))
    not_found(lambda: CitationVault(str(d), key).get(TENANT_A, vid))


def test_line_moved_to_another_tenant_fails_closed(tmp_path):
    check_moved_tenant_fails(tmp_path)


def test_line_moved_to_another_id_fails_closed(tmp_path):
    check_moved_id_fails(tmp_path)


def test_tampered_key_id_fails_closed(tmp_path):
    old, old_text = new_key("old")
    d = tmp_path / "data"
    vid = CitationVault(str(d), old).put(TENANT_A, citation(marker()))
    cur, _ = new_key("cur", previous=(old_text, "old"))
    for kid in (b"cur", b"nope", b"OLD"):
        rewrite(d, 0, lambda o, kid=kid: o.__setitem__("k", b64(kid)))
        not_found(lambda: CitationVault(str(d), cur).get(TENANT_A, vid))
    rewrite(d, 0, lambda o: o.__setitem__("k", b64(b"old")))
    assert CitationVault(str(d), cur).get(TENANT_A, vid)["engine"] == "fixture"


@pytest.mark.parametrize("change", [
    lambda o: o.__setitem__("v", 2),
    lambda o: o.__setitem__("v", True),
    lambda o: o.__setitem__("extra", "x"),
    lambda o: o.pop("t"),
    lambda o: o.__setitem__("c", o["c"][:20]),
    lambda o: o.__setitem__("n", o["n"] + "AA"),
    lambda o: o.__setitem__("c", o["c"] + "="),
    lambda o: o.__setitem__("k", b64(b"bad id!")),
], ids=["version", "bool-version", "extra-field", "missing-field", "short-ct", "long-nonce", "padding", "bad-kid"])
def test_malformed_lines_are_not_served(tmp_path, change):
    key, _ = new_key()
    d = tmp_path / "data"
    vid = CitationVault(str(d), key).put(TENANT_A, citation(marker()))
    rewrite(d, 0, change)
    v = CitationVault(str(d), key)
    not_found(lambda: v.get(TENANT_A, vid))


def test_duplicate_json_field_is_not_served(tmp_path):
    key, _ = new_key()
    d = tmp_path / "data"
    vid = CitationVault(str(d), key).put(TENANT_A, citation(marker()))
    p = os.path.join(str(d), VAULT_NAME)
    ln = lines(d)[0]
    with open(p, "wb") as fh:
        fh.write(ln[:-1] + b',"t":"AAAA"}\n')
    v = CitationVault(str(d), key)
    assert v.bad_lines == 1
    not_found(lambda: v.get(TENANT_A, vid))


def test_in_place_edit_after_open_is_caught_on_read(tmp_path):
    key, _ = new_key()
    d = tmp_path / "data"
    v = CitationVault(str(d), key)
    vid = v.put(TENANT_A, citation(marker()))
    rewrite(d, 0, lambda o: o.__setitem__("c", flip(o["c"], 1)))   # same length: the offset still matches
    not_found(lambda: v.get(TENANT_A, vid))


def test_wrong_key_fails(tmp_path):
    key, _ = new_key("k1")
    d = tmp_path / "data"
    vid = CitationVault(str(d), key).put(TENANT_A, citation(marker()))
    other, _ = new_key("k1")                          # same id, different bytes
    not_found(lambda: CitationVault(str(d), other).get(TENANT_A, vid))
    other2, _ = new_key("k2")
    not_found(lambda: CitationVault(str(d), other2).get(TENANT_A, vid))


def test_previous_key_decrypts_and_new_writes_use_current(tmp_path):
    old, old_text = new_key("k-2026-10")
    d = tmp_path / "data"
    rec = citation(marker())
    old_id = CitationVault(str(d), old).put(TENANT_A, rec)
    cur, cur_text = new_key("k-2026-11", previous=(old_text, "k-2026-10"))
    assert cur.key_id == "k-2026-11" and cur.previous_key_id == "k-2026-10"
    v = CitationVault(str(d), cur)
    assert v.get(TENANT_A, old_id) == rec
    not_found(lambda: v.get(TENANT_B, old_id))
    new_id = v.put(TENANT_A, rec)
    stored = [json.loads(ln) for ln in lines(d)]
    assert [unb64(s["k"]) for s in stored] == [b"k-2026-10", b"k-2026-11"]
    assert v.get(TENANT_A, new_id) == rec
    # after the rotation finishes (previous key removed), the old record is gone and the new one still reads
    only_cur = parse_vault_key(cur_text, "k-2026-11")
    v2 = CitationVault(str(d), only_cur)
    not_found(lambda: v2.get(TENANT_A, old_id))
    assert v2.get(TENANT_A, new_id) == rec
    # the previous key alone cannot read what the current key wrote
    not_found(lambda: CitationVault(str(d), old).get(TENANT_A, new_id))


def test_no_key_put_refuses_and_writes_nothing(tmp_path):
    d = tmp_path / "data"
    v = CitationVault(str(d), None)
    assert not v.key_configured
    with pytest.raises(VaultUnavailable):
        v.put(TENANT_A, citation(marker()))
    with pytest.raises(VaultUnavailable):
        v.get(TENANT_A, secrets.token_hex(16))
    assert os.listdir(str(d)) == []
    assert load_vault_key({}) is None
    assert load_vault_key({"SEO_VAULT_KEY": "  ", "SEO_VAULT_KEY_ID": ""}) is None
    with pytest.raises(TypeError):
        CitationVault(str(d), "not-a-key")
    assert os.listdir(str(d)) == []


def test_load_vault_key_from_env(tmp_path):
    cur, prev = key_text(), key_text()
    k = load_vault_key({"SEO_VAULT_KEY": cur, "SEO_VAULT_KEY_ID": "k2", "SEO_VAULT_KEY_PREVIOUS": prev,
                        "SEO_VAULT_KEY_PREVIOUS_ID": "k1"})
    assert (k.key_id, k.previous_key_id) == ("k2", "k1")
    k = load_vault_key({"SEO_VAULT_KEY": cur.rstrip("=") + "\n", "SEO_VAULT_KEY_ID": "k2"})
    assert k.previous_key_id is None
    vid = CitationVault(str(tmp_path / "d"), k).put(TENANT_A, {"a": 1})
    assert CitationVault(str(tmp_path / "d"), load_vault_key({"SEO_VAULT_KEY": cur, "SEO_VAULT_KEY_ID": "k2"})) \
        .get(TENANT_A, vid) == {"a": 1}


def _bad_envs():
    good = key_text()
    raw = os.urandom(32)
    std = base64.b64encode(raw).decode()
    while "+" not in std and "/" not in std:
        raw = os.urandom(32)
        std = base64.b64encode(raw).decode()
    noncanon = good.rstrip("=")
    last = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".index(noncanon[-1])
    noncanon = noncanon[:-1] + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"[last ^ 1]
    return {
        "short": {"SEO_VAULT_KEY": base64.urlsafe_b64encode(os.urandom(16)).decode(), "SEO_VAULT_KEY_ID": "k"},
        "long": {"SEO_VAULT_KEY": base64.urlsafe_b64encode(os.urandom(33)).decode(), "SEO_VAULT_KEY_ID": "k"},
        "hex": {"SEO_VAULT_KEY": os.urandom(32).hex(), "SEO_VAULT_KEY_ID": "k"},
        "std-alphabet": {"SEO_VAULT_KEY": std, "SEO_VAULT_KEY_ID": "k"},
        "zeros": {"SEO_VAULT_KEY": base64.urlsafe_b64encode(b"\x00" * 32).decode(), "SEO_VAULT_KEY_ID": "k"},
        "repetitive": {"SEO_VAULT_KEY": base64.urlsafe_b64encode(os.urandom(4) * 8).decode(),
                       "SEO_VAULT_KEY_ID": "k"},
        "non-canonical": {"SEO_VAULT_KEY": noncanon, "SEO_VAULT_KEY_ID": "k"},
        "inner-space": {"SEO_VAULT_KEY": good[:10] + " " + good[10:], "SEO_VAULT_KEY_ID": "k"},
        "no-id": {"SEO_VAULT_KEY": good},
        "id-only": {"SEO_VAULT_KEY_ID": "k"},
        "bad-id": {"SEO_VAULT_KEY": good, "SEO_VAULT_KEY_ID": "Key One"},
        "long-id": {"SEO_VAULT_KEY": good, "SEO_VAULT_KEY_ID": "k" * 33},
        "previous-only": {"SEO_VAULT_KEY_PREVIOUS": good, "SEO_VAULT_KEY_PREVIOUS_ID": "old"},
        "previous-no-id": {"SEO_VAULT_KEY": good, "SEO_VAULT_KEY_ID": "k", "SEO_VAULT_KEY_PREVIOUS": key_text()},
        "previous-id-no-key": {"SEO_VAULT_KEY": good, "SEO_VAULT_KEY_ID": "k", "SEO_VAULT_KEY_PREVIOUS_ID": "old"},
        "previous-same-bytes": {"SEO_VAULT_KEY": good, "SEO_VAULT_KEY_ID": "k", "SEO_VAULT_KEY_PREVIOUS": good,
                                "SEO_VAULT_KEY_PREVIOUS_ID": "old"},
        "previous-same-id": {"SEO_VAULT_KEY": good, "SEO_VAULT_KEY_ID": "k", "SEO_VAULT_KEY_PREVIOUS": key_text(),
                             "SEO_VAULT_KEY_PREVIOUS_ID": "k"},
        "previous-bad": {"SEO_VAULT_KEY": good, "SEO_VAULT_KEY_ID": "k", "SEO_VAULT_KEY_PREVIOUS": good[:-5],
                         "SEO_VAULT_KEY_PREVIOUS_ID": "old"},
    }


@pytest.mark.parametrize("case", sorted(_bad_envs()))
def test_invalid_key_settings_refuse_without_quoting_and_write_nothing(tmp_path, case):
    env = _bad_envs()[case]
    with pytest.raises(VaultKeyInvalid) as ei:
        load_vault_key(env)
    msg = str(ei.value)
    for name, value in env.items():
        if name.endswith("_ID"):
            continue
        assert value.strip() not in msg and value.strip()[:12] not in msg
    assert ei.value.__context__ is None or not any(v in str(ei.value.__context__) for v in env.values())
    assert not (tmp_path / "data").exists()


def test_key_objects_are_never_printed_pickled_or_copied(tmp_path):
    prev_text = key_text()
    key, text = new_key("k2", previous=(prev_text, "k1"))
    raw, prev_raw = unb64(text), unb64(prev_text)
    forbidden = [text, text.rstrip("="), prev_text.rstrip("="), raw.hex(), prev_raw.hex(), repr(raw), repr(prev_raw)]
    shown = [repr(key), str(key), f"{key}", f"{key!r}", repr(key._cur), str(key._prev), repr(key.__class__)]
    v = CitationVault(str(tmp_path / "data"), key)
    shown += [repr(v), str(v)]
    for s in shown:
        assert not any(f in s for f in forbidden)
    assert "redacted" in repr(key) and "k2" in repr(key)
    for obj in (key, key._cur, key._prev, v):
        for proto in range(pickle.HIGHEST_PROTOCOL + 1):
            with pytest.raises(TypeError):
                pickle.dumps(obj, protocol=proto)
        with pytest.raises(TypeError):
            copy.copy(obj)
        with pytest.raises(TypeError):
            copy.deepcopy(obj)
    for obj in (key, key._cur):
        assert not hasattr(obj, "__dict__")
    with pytest.raises(TypeError):
        vault.VaultKey("raw", None)


def test_error_messages_never_carry_key_or_plaintext(tmp_path):
    key, text = new_key()
    m = marker()
    d = str(tmp_path / "data")
    v = CitationVault(d, key)
    vid = v.put(TENANT_A, citation(m))
    errors = []
    for call in (lambda: v.put(TENANT_A, {m: object()}),
                 lambda: v.put(TENANT_A, {"x": float("nan"), "m": m}),
                 lambda: v.put(TENANT_A, [m]),
                 lambda: v.put(m, citation(m)),
                 lambda: v.put(TENANT_A, {"x": m * (MAX_RECORD_BYTES // len(m) + 1)}),
                 lambda: v.get(TENANT_B, vid),
                 lambda: v.get(m, vid),
                 lambda: CitationVault(d, None).put(TENANT_A, citation(m)),
                 lambda: parse_vault_key(text[:-3] + m[:3], "k"),
                 lambda: parse_vault_key(text, m)):
        with pytest.raises(vault.VaultError) as ei:
            call()
        errors.append(ei.value)
    for e in errors:
        msg = " ".join([str(e), repr(e)] + [str(a) for a in e.args])
        assert m not in msg and text not in msg and text[:16] not in msg and TENANT_A not in msg
        assert e.__cause__ is None
        assert e.__context__ is None or m not in str(e.__context__)
    assert len(lines(d)) == 1


def test_oversized_record_refused_and_nothing_written(tmp_path):
    key, _ = new_key()
    d = tmp_path / "data"
    v = CitationVault(str(d), key)
    overhead = len(json.dumps({"x": ""}, separators=(",", ":")))
    vid = v.put(TENANT_A, {"x": "a" * (MAX_RECORD_BYTES - overhead)})   # exactly at the cap: kept
    assert len(v.get(TENANT_A, vid)["x"]) == MAX_RECORD_BYTES - overhead
    before = lines(d)
    with pytest.raises(VaultRecordInvalid):
        v.put(TENANT_A, {"x": "a" * (MAX_RECORD_BYTES - overhead + 1)})
    with pytest.raises(VaultRecordInvalid):
        v.put(TENANT_A, {"x": "\u00e9" * (MAX_RECORD_BYTES // 2)})       # counted in UTF-8 bytes
    assert lines(d) == before and len(v) == 1


def test_concurrent_puts_produce_valid_lines(tmp_path):
    key, _ = new_key()
    d = tmp_path / "data"
    v = CitationVault(str(d), key)
    out: list = []
    errors: list = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker(n: int) -> None:
        try:
            start.wait()
            for i in range(25):
                tenant = TENANT_A if (n + i) % 2 else TENANT_B
                vid = v.put(tenant, {"worker": n, "i": i, "pad": "p" * (i * 97)})
                with lock:
                    out.append((tenant, vid, n, i))
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(out) == 200 and len({o[1] for o in out}) == 200
    ls = lines(d)
    assert len(ls) == 200 and all(vault._parse_line(ln) is not None for ln in ls)
    fresh = CitationVault(str(d), key)
    assert fresh.bad_lines == 0 and len(fresh) == 200
    for tenant, vid, n, i in out:
        assert fresh.get(tenant, vid)["worker"] == n and fresh.get(tenant, vid)["i"] == i


def test_torn_tail_symlink_and_open_permissions_refuse_open(tmp_path):
    key, _ = new_key()
    d = tmp_path / "data"
    vid = CitationVault(str(d), key).put(TENANT_A, {"a": 1})
    p = os.path.join(str(d), VAULT_NAME)
    with open(p, "ab") as fh:
        fh.write(b'{"v":1,"id"')
    # review 601e328a SHOULD 3 (was: refused): ONLY a torn final line is cut at open and counted; the observe-only
    # start (repair=False) still refuses it; a torn line with another bad line refuses (tests/test_review_601e328a.py)
    with pytest.raises(VaultCorrupt):
        CitationVault(str(d), key, repair=False)
    v = CitationVault(str(d), key)
    rep = dict(v.tail_repair)
    assert len(rep.pop("repair_id")) == 32                             # review 08eea6e1 SHOULD 1: counts + a hash id
    assert rep == {"torn_lines": 1, "bytes_removed": 11, "records_kept": 1, "records_removed": 1, "newline_added": 0,
                   "quarantined": 1}
    assert v.get(TENANT_A, vid) == {"a": 1} and open(p, "rb").read().endswith(b"\n")
    d2 = tmp_path / "data2"
    os.makedirs(str(d2))
    os.symlink(p, os.path.join(str(d2), VAULT_NAME))
    with pytest.raises(VaultCorrupt):
        CitationVault(str(d2), key)
    d3 = tmp_path / "data3"
    CitationVault(str(d3), key).put(TENANT_A, {"a": 1})
    os.chmod(os.path.join(str(d3), VAULT_NAME), 0o644)
    with pytest.raises(VaultCorrupt):
        CitationVault(str(d3), key)


def test_write_refused_when_file_changed_outside_the_process(tmp_path):
    key, _ = new_key()
    d = tmp_path / "data"
    v = CitationVault(str(d), key)
    v.put(TENANT_A, {"a": 1})
    with open(os.path.join(str(d), VAULT_NAME), "ab") as fh:
        fh.write(b"x\n")
    with pytest.raises(vault.VaultWriteError):
        v.put(TENANT_A, {"a": 2})
    assert len(lines(d)) == 2


def test_relative_data_dir_refused(tmp_path):
    key, _ = new_key()
    with pytest.raises(ValueError):
        CitationVault("relative/dir", key)


# --- mutation bite: each mutation must make a property check fail ----------------------------------------------------

def test_mutation_empty_associated_data_is_caught(tmp_path, monkeypatch):
    check_moved_tenant_fails(tmp_path / "control")
    monkeypatch.setattr(vault, "_aad", lambda tenant_id, vault_id, key_id: b"")
    with pytest.raises(AssertionError):
        check_moved_tenant_fails(tmp_path / "mutant")
    with pytest.raises(AssertionError):
        check_moved_id_fails(tmp_path / "mutant2")


def test_mutation_constant_nonce_is_caught(tmp_path, monkeypatch):
    check_nonce_unique(tmp_path / "control")
    monkeypatch.setattr(vault, "_new_nonce", lambda: b"\x00" * 12)
    with pytest.raises(AssertionError):
        check_nonce_unique(tmp_path / "mutant")


def test_mutation_tenant_check_off_is_caught(tmp_path, monkeypatch):
    check_cross_tenant_never_decrypts(tmp_path / "control")
    monkeypatch.setattr(vault, "_tenant_matches", lambda material, tenant_id, tag: True)
    with pytest.raises(AssertionError):
        check_cross_tenant_never_decrypts(tmp_path / "mutant")
