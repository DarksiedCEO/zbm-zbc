"""
Money: the shared wire-format vectors (fixtures/money_vectors.json, BUILD_CONTRACTS §1 / ADR 0003 §1a), the one
rounding rule (R1: exact views x rate / 1000, ONE half-up quantize per payable), and a balance-to-zero property over
random postings (FIN-02, G9).
"""

from __future__ import annotations

import json
import random
from decimal import Decimal
from pathlib import Path

import pytest

import money as M
from intelligences import i01_journal as J

VECTORS = json.loads((Path(__file__).resolve().parents[3] / "fixtures" / "money_vectors.json").read_text("utf-8"))
STRINGS = VECTORS["string_vectors"]
JSONS = VECTORS["json_vectors"]


def test_contract_block_matches():
    assert VECTORS["contract"]["pattern"] == M.WIRE_PATTERN.pattern
    assert VECTORS["contract"]["max"] == M.fmt(M.MAX_MONEY)


@pytest.mark.parametrize("vec", STRINGS, ids=[f"{i}" for i in range(len(STRINGS))])
def test_string_vector_verdicts(vec):
    s = vec["input"]
    for field, positive in (("money", False), ("positive_money", True)):
        if vec[field] == "accept":
            assert M.fmt(M.parse(s, positive=positive)) == s
        else:
            with pytest.raises(ValueError):
                M.parse(s, positive=positive)


def test_string_vectors_over_http(hr):
    for i, vec in enumerate(STRINGS):
        r = hr.post("/fin/v1/treasury/sweeps", {"request_id": f"mv-{i}", "amount": vec["input"]},
                    caller="scheduler")
        assert r.status_code != 500, vec
        if vec["positive_money"] == "accept":
            assert r.status_code != 422, (vec, r.text)
        else:
            assert r.status_code in (413, 422), vec          # an oversized vector is refused by the body cap first


@pytest.mark.parametrize("vec", JSONS, ids=[v["json"][:20] for v in JSONS])
def test_json_vectors_over_http(hr, vec):
    raw = '{"request_id":"jv-%d","amount":%s}' % (JSONS.index(vec), vec["json"])
    r = hr.client.post("/fin/v1/treasury/sweeps", content=raw.encode(),
                       headers={**hr.headers("scheduler"), "Content-Type": "application/json"})
    assert r.status_code != 500
    assert (r.status_code != 422) == (vec["verdict"] == "accept"), (vec, r.status_code)


@pytest.mark.parametrize("views,rate,want", [
    (2500, "0.01", "0.03"),          # R1 / S14: half-up once per payable; half-even would give 0.02
    (12345, "2.35", "29.01"), (12345, "4.00", "49.38"), (1, "0.01", "0.00"), (50, "0.01", "0.00"),
    (500, "0.01", "0.01"), (1500, "0.01", "0.02"), (999999999, "999.99", "999989999.00"), (0, "5.00", "0.00"),
    (893617, "2.35", "2100.00"), (17021, "2.35", "40.00"), (51064, "2.35", "120.00")])
def test_payable_amount_vectors(views, rate, want):
    assert M.fmt(M.payable_amount(views, Decimal(rate))) == want


def test_backup_withholding_rounding():
    assert M.fmt(M.pct(Decimal("29.01"), 24)) == "6.96"
    assert M.fmt(M.pct(Decimal("0.02"), 24)) == "0.00"
    assert M.fmt(M.pct(Decimal("0.03"), 24)) == "0.01"      # 0.0072 -> 0.01 half-up


def test_never_decimal_from_float():
    with pytest.raises(M.MoneyError):
        M.D(1.5)
    with pytest.raises(ValueError):
        M.parse(1.5)
    with pytest.raises(M.MoneyError):
        M.payable_amount(1.0, Decimal("1.00"))


def test_balance_to_zero_property_over_random_postings():
    """Random candidate entries: only balanced, well-formed ones validate; after every accepted append the trial
    balance of each entity is exactly 0.00."""
    rnd = random.Random(31)
    balances: dict = {}
    by_id: dict = {}
    accepted = refused = 0
    accts = {"zbm": ["1010", "3000", "5020", "5030", "1300"], "zbc": ["1010", "3000", "5020", "5030", "1300"]}
    for n in range(4000):
        ent = rnd.choice(["zbc", "zbm"])
        k = rnd.randrange(2, 6)
        lines = []
        for _ in range(k):
            amt = Decimal(rnd.randrange(1, 10**7)) / 100
            side = rnd.random() < 0.5
            lines.append(J.line(rnd.choice(accts[ent]), None, amt if side else M.ZERO, M.ZERO if side else amt))
        if rnd.random() < 0.5:                                   # make half of them balance
            d, c = M.total(l["debit"] for l in lines), M.total(l["credit"] for l in lines)
            diff = M.q(d - c)
            if diff > 0:
                lines.append(J.line("3000", None, M.ZERO, diff))
            elif diff < 0:
                lines.append(J.line("3000", None, -diff, M.ZERO))
        e = J.build(f"e{n}", ent, "2026-10-02", "2026-10-02T00:00:00Z", lines, "correction", {"kind": "t", "id": str(n)},
                    f"k{n}", None, f"fin-je-{n}")
        problems = J.validate(e, set(), by_id)
        d, c = J.totals(e)
        if d == c and d > 0 and len(e["lines"]) >= 2:
            assert not problems, problems
            J.apply_balances(balances, e)
            by_id[e["entry_id"]] = e
            accepted += 1
        else:
            assert problems
            refused += 1
        for ent_ in ("zbc", "zbm"):
            assert J.trial_balance(balances, ent_)["difference"] == "0.00"
    assert accepted > 1000 and refused > 1000
