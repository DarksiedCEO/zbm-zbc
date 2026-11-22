"""core-spec/2 AL-1 / AL-2 (card C2-15; owner default Q2-7, TI-1): per-tenant catalog allowlist for the client view.

Synthetic values only. Pass (AL-2): 2 tenants x 1,000 catalog tokens: 100% shown for the owner, 0 for the other
tenant; 0 K-1 / K-5 values shown even when listed. Also: codes only (no raw copy), cap 50,000 (50,001 refused,
set unchanged), every change attempt audited first in typed fields, a failed audit leaves the set unchanged."""
from __future__ import annotations

import random
import string

import pytest

import sink_fields
import sink_guard
import vault
from agents import cv_allowlist as al
from agents import cv_shape
from agents import secret_guard as sg

MASK = "[hidden: looks like a credential]"
TA, TB = "tenant-alpha", "tenant-beta"
AT = "2026-11-14T17:30:00Z"
K1 = ("".join(("AKIAQX", "7RT2ZK", "9LMNB4", "PW")), "".join(("ghp_aB", "3dE5fG", "7hJ9kL", "1mN3pQ", "5rS7tU", "9vW1xY", "3zA5")), "".join(("sk_liv", "e_Zq8R", "t2Vb6N", "m4Kx9L", "p3Wd7H", "s1")),
      "xoxb-1234567890-abcdefABCDEF")
K5 = ("4111111111111111", "4111-1111-1111-1111", "123-45-6789", "+14155550134")


def _tokens(seed: int, n: int = 1000) -> list:
    """SKU-like synthetic tokens: 10 characters, upper-case letters + 2..4 digits (valueish by CV-3, not K-1/K-5)."""
    rng, out = random.Random(seed), set()
    while len(out) < n:
        d = rng.randint(2, 4)
        cs = [rng.choice(string.ascii_uppercase) for _ in range(10 - d)] + [rng.choice(string.digits) for _ in range(d)]
        rng.shuffle(cs)
        if cs[0].isalpha():
            out.add("".join(cs))
    return sorted(out)


def _store():
    key = vault.ephemeral_vault_key()
    coders = {t: vault.Coder(key, t) for t in (TA, TB)}
    rows: list = []
    return al.CatalogAllowlist(coders.__getitem__, lambda kind, row: rows.append((kind, dict(row)))), rows


def _shown(tok: str) -> bool:
    rendered, _spans = cv_shape.display(f"Order {tok} ships today.", MASK)
    return tok in rendered


@pytest.fixture(scope="module", autouse=True)
def _registered_tenants():
    """C2-6h (ruling 02:15 PT F6f3-1; T-5 / T-7): the service syncs an allowlist only for an EXISTING tenant
    (svc_allowlist TENANT_NOT_FOUND), registered before any sink write names it. The store-level tests register TA / TB
    the same way, fixture-local (the guard's registry is restored after the module). The tenant_id check is unchanged."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sink_guard, "_REGISTERED", set(sink_guard._REGISTERED))
        sink_guard.register_tenant(TA)
        sink_guard.register_tenant(TB)
        yield


@pytest.fixture(scope="module")
def two():
    store, rows = _store()
    a, b = _tokens(20261114), _tokens(51)
    assert not set(a) & set(b)
    store.replace(TA, a, actor="seo", source="shopify", integration_tenant=TA, at=AT)
    store.replace(TB, b, actor="seo", source="shopify", integration_tenant=TB, at=AT)
    return store, rows, a, b


def test_al2_baseline_every_token_is_masked_without_the_allowlist(two):
    _store_, _rows, a, b = two
    assert sum(_shown(t) for t in a + b) == 0          # the tokens are CV-3 valueish: the allowlist is what shows them


def test_al2_owner_sees_100_percent_and_the_other_tenant_0(two):
    store, _rows, a, b = two
    with al.scope(store, TA):
        assert sum(_shown(t) for t in a) == 1000
        assert sum(_shown(t) for t in b) == 0
        assert sg.client_view_findings(f"Order {a[0]} ships today.") == []
    with al.scope(store, TB):
        assert sum(_shown(t) for t in b) == 1000
        assert sum(_shown(t) for t in a) == 0
    assert sum(_shown(t) for t in a[:50]) == 0          # outside any scope nothing is shown


def test_al1_exact_token_only_and_a_merged_span_stays_masked(two):
    store, _rows, a, _b = two
    with al.scope(store, TA):
        assert not _shown(a[0].lower()) and not _shown(a[0] + "X")
        rendered, _ = cv_shape.display(f"Ref {a[0]}-{a[1]}Q9 end", MASK)
        assert a[0] not in rendered                     # a span that is not exactly a listed token is masked


def test_al2_k1_and_k5_never_shown_even_when_listed():
    store, _rows = _store()
    store.replace(TA, K1 + K5, actor="seo", source="shopify", integration_tenant=TA, at=AT)
    assert store.count(TA) == len(K1 + K5)
    with al.scope(store, TA):
        assert [v for v in K1 + K5 if _shown(v)] == []
        assert all(sg.client_view_findings(f"Ref {v} ok") for v in K1 + K5)


def test_al1_codes_only_no_raw_copy(two):
    store, rows, a, b = two
    held = store.codes_of(TA) | store.codes_of(TB)
    assert len(held) == 2000 and all(sink_fields.REF_RX.match(c[1:-1]) and c.startswith("{c:text:") for c in held)
    blob = repr(sorted(held)) + repr(rows)
    assert [t for t in a + b if t in blob] == []
    assert store.codes_of(TA).isdisjoint(store.codes_of(TB))


def test_al1_cap_50000_accepted_50001_refused_set_unchanged():
    store, rows = _store()
    toks = [f"QZ{i:06d}K" for i in range(al.CAP + 1)]
    assert store.replace(TA, toks[:al.CAP], actor="seo", source="shopify", integration_tenant=TA, at=AT) == al.CAP
    before = store.codes_of(TA)
    with pytest.raises(al.AllowlistRefused) as e:
        store.replace(TA, toks, actor="seo", source="shopify", integration_tenant=TA, at=AT)
    assert e.value.outcome == "refused_cap" and store.codes_of(TA) is before
    assert rows[-1][1]["outcome"] == "refused_cap" and rows[-1][1]["requested_count"] == al.CAP + 1


def test_al1_sources_own_integration_only():
    store, rows = _store()
    for src, integ in (("shopify", TB), ("upload", TA)):
        with pytest.raises(al.AllowlistRefused) as e:
            store.replace(TA, ["AB12CD34EF"], actor="seo", source=src, integration_tenant=integ, at=AT)
        assert e.value.outcome == "refused_source"
    assert store.count(TA) == 0 and [r["outcome"] for _k, r in rows] == ["refused_source"] * 2


def test_al1_every_change_audited_first_in_typed_fields(two):
    _store_, rows, _a, _b = two
    assert [(k, r["tenant_id"], r["count"], r["outcome"]) for k, r in rows] == [
        (al.AUDIT_KIND, TA, 1000, "applied"), (al.AUDIT_KIND, TB, 1000, "applied")]
    for kind, row in rows:
        assert sink_guard.violations(kind, row) == []
        assert set(row) == {"tenant_id", "actor", "at", "source", "count", "previous_count", "requested_count",
                            "outcome"}
    store, _ = _store()

    def boom(kind, row):
        raise OSError("audit sink down")
    store._audit = boom
    with pytest.raises(OSError):
        store.replace(TA, ["AB12CD34EF"], actor="seo", source="shopify", integration_tenant=TA, at=AT)
    assert store.count(TA) == 0                         # audit failed -> the set is unchanged
    with pytest.raises(al.AllowlistRefused):
        store.replace(TA, ["AB12CD34EF"], actor="not an actor", source="shopify", integration_tenant=TA, at=AT)


def test_al1_audit_enums_equal_the_module():
    assert sink_fields.CV_ALLOWLIST_ENUMS["/source"] == frozenset(al.SOURCES) | {"unknown"}
    assert sink_fields.CV_ALLOWLIST_ENUMS["/outcome"] == frozenset(al.OUTCOMES)
    row = {"tenant_id": TA, "actor": "seo", "at": AT, "source": "shopify", "count": 1, "previous_count": 0,
           "requested_count": 1, "outcome": "applied", "token": "AB12CD34EF"}
    assert ("/token", "untyped") in sink_guard.violations(al.AUDIT_KIND, row)


def test_al2_never_reads_each_token_of_a_merged_span(two):
    """AL-1 (C2-R2, mutant al_never_whole_span_only): a listed merged span is never exempt when ONE of its token cores
    is K-5, even where the whole view text reads as no K-5 (the comma and the trailing digits bridge the SSN into a
    longer number there). A listed merged span of plain tokens is still shown (control)."""
    store, _rows = _store()
    ssn = "-".join(("123", "45", "6789"))
    merged = ssn + ", 12"
    plain = "AB12CD34, EF56GH78"
    store.replace(TA, [merged, plain], actor="seo", source="shopify", integration_tenant=TA, at=AT)
    with al.scope(store, TA):
        assert al.shown(plain, plain) is True
        assert al.shown(merged, merged) is False
        assert al.shown(ssn, ssn) is False
