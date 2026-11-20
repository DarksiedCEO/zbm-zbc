"""priya-spec/2 section 5 CODES (a)-(e) (Wave-1 B-CD-01/02 analogs; G-9 CODES (b)); routes: test_priya_codes_g9.py.
Card P2-8. CI-lane draws only; the full-corpus counts (every frozen corpus) are the lead job
scratchpad script priya-v2/p28/codes_full.py (counts only, never text). Synthetic tenants and keys only."""

from __future__ import annotations

import base64
from collections import defaultdict

import pytest
from corpora import ORDINARY, ci_rows
from test_priya_withheld import CODE_RE, bing, gsc

import vault
from agents import priya
from primitives import codes

pytestmark = pytest.mark.conformance

# synthetic keys made up here (high-variety bytes); never a real key
KEY_A = base64.urlsafe_b64encode(bytes((i * 53 + 7) % 256 for i in range(32))).decode()
KEY_B = base64.urlsafe_b64encode(
    bytes((i * 29 + 101) % 256 for i in range(32))
).decode()
ENV_A = {"SEO_VAULT_KEY": KEY_A, "SEO_VAULT_KEY_ID": "k-p28-a"}
ENV_ROT = {
    "SEO_VAULT_KEY": KEY_B,
    "SEO_VAULT_KEY_ID": "k-p28-b",
    "SEO_VAULT_KEY_PREVIOUS": KEY_A,
    "SEO_VAULT_KEY_PREVIOUS_ID": "k-p28-a",
}
CI_PERSONAL = (
    "boundary",
    "matrix5",
    "attack676",
    "rv21_short",
    "rv21_d21",
    "rv21_b21",
    "rv18_p1",
    "rv20_m20",
)
WITHHELD_Q = ("zzqxvbt kitten", "mail me@example", "qvzxkwj plumber")


def _coder(env=ENV_A, tenant="acme"):
    return vault.Coder(vault.load_vault_key(env), tenant)


def code_of(coder, q: str) -> str:
    """Exactly priya._withheld_row's coding of a key: codes.code("query", normalize(q)) under this tenant's coder."""
    with codes.coding(coder, own=(), evidence=None):
        return codes.code("query", priya.normalize(q))


def _oq_queries():
    for name in ORDINARY:
        for _i, _k, q in ci_rows(name):
            yield q


def _build(rows, coder):
    return priya.build(
        "acme" if coder.tenant_id == "acme" else coder.tenant_id,
        "run-p28",
        "2026-10-29T00:00:00Z",
        [],
        gsc(rows),
        bing([]),
        coder=coder,
    )


def _rows():
    return [(q, 1, 20 + i, 0.05, 4.0) for i, q in enumerate(WITHHELD_Q)] + [
        ("kitten food", 9, 400, 0.02, 3.0)
    ]


# ---------------------------------------------------------------- (a) same key, tenant and key id -> same code
def test_codes_a_same_key_tenant_and_key_id_give_the_same_code_in_two_runs():
    l1, l2 = _build(_rows(), _coder()), _build(_rows(), _coder())
    w1, w2 = l1["withheld_rows"], l2["withheld_rows"]
    assert len(w1) == len(WITHHELD_Q) and [r["code"] for r in w1] == [
        r["code"] for r in w2
    ]
    assert all(CODE_RE.match(r["code"]) for r in w1)
    assert l1["gate"]["codes_key_id"] == l2["gate"]["codes_key_id"] == "k-p28-a"
    c1, c2 = _coder(), _coder()  # two independent coders = two service runs
    n = same = 0
    for q in _oq_queries():
        n += 1
        same += code_of(c1, q) == code_of(c2, q)
    assert n > 1000 and same == n  # 100%


# ---------------------------------------------------------------- (b) 2 tenants x every OQ query -> 0 equal codes
def test_codes_b_two_tenants_never_share_a_code_on_the_oq_corpora():
    ca, cg = _coder(tenant="acme"), _coder(tenant="globex")
    keys, equal = set(), 0
    for q in _oq_queries():
        k = priya.normalize(q)
        if k in keys:
            continue
        keys.add(k)
        equal += code_of(ca, q) == code_of(cg, q)
    assert len(keys) > 1000 and equal == 0
    la, lg = _build(_rows(), ca), _build(_rows(), cg)
    assert not {r["code"] for r in la["withheld_rows"]} & {
        r["code"] for r in lg["withheld_rows"]
    }


# ---------------------------------------------------------------- (c) 0 collisions between distinct keys of a tenant
# ---------------------------------------------------------------- (d) grouping by code == grouping by key
def _partition(coder, names):
    by_code, by_key = defaultdict(set), defaultdict(set)
    for name in names:
        for i, _k, q in ci_rows(name):
            ident = (name, i)
            by_code[code_of(coder, q)].add(ident)
            by_key[priya.normalize(q)].add(ident)
    return by_code, by_key


def test_codes_c_d_no_collisions_and_grouping_by_code_equals_grouping_by_key_on_ci_draws():
    c = _coder()
    by_code, by_key = _partition(c, CI_PERSONAL + tuple(ORDINARY))
    n_cases = sum(len(v) for v in by_key.values())
    assert n_cases > 10000 and len(by_key) > 5000
    # (c) a code is shared only by cases with one key: number of distinct codes == number of distinct keys
    collisions = len(by_key) - len(by_code)
    assert collisions == 0
    # (d) the two partitions of the cases are identical: 0 differences
    diffs = {frozenset(v) for v in by_code.values()} ^ {
        frozenset(v) for v in by_key.values()
    }
    assert len(diffs) == 0


def test_codes_d_spellings_of_one_key_are_one_coded_row_with_summed_metrics():
    rows = [
        ("Zzqxvbt  Kitten", 1, 20, 0.05, 4.0),
        ("zzqxvbt kitten", 2, 30, 0.07, 5.0),
        ("qvzxkwj plumber", 1, 25, 0.04, 6.0),
    ]
    led = _build(rows, _coder())
    w = led["withheld_rows"]
    assert len(w) == 2 and len({r["code"] for r in w}) == 2
    want = code_of(_coder(), "zzqxvbt kitten")
    row = next(r for r in w if r["code"] == want)
    assert row["metrics"]["search_console"]["clicks"] == 3
    assert row["metrics"]["search_console"]["impressions"] == 50


# ---------------------------------------------------------------- (e) key rotation (SEO_VAULT_KEY_PREVIOUS)
def test_codes_e_rotation_changes_every_code_and_records_the_key_id():
    """Precondition of (e): after a rotation every code moves and the run records the new codes_key_id, so a
    comparer can only see the ruler move. The R2 classification itself: see HANDOFF P2-8 (e) BLOCKED."""
    before, after = _coder(ENV_A), _coder(ENV_ROT)
    assert vault.load_vault_key(ENV_ROT).previous_key_id == "k-p28-a"
    moved = n = 0
    for q in _oq_queries():
        n += 1
        moved += code_of(before, q) != code_of(after, q)
    assert n > 1000 and moved == n
    l1, l2 = _build(_rows(), before), _build(_rows(), after)
    assert (l1["gate"]["codes_key_id"], l2["gate"]["codes_key_id"]) == (
        "k-p28-a",
        "k-p28-b",
    )
    assert not {r["code"] for r in l1["withheld_rows"]} & {
        r["code"] for r in l2["withheld_rows"]
    }
    assert [r["reason"] for r in l1["withheld_rows"]] != [] and len(
        l1["withheld_rows"]
    ) == len(l2["withheld_rows"])
