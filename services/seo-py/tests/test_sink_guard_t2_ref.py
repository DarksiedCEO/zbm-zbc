"""core-spec/2 T-2 / T-5, lead ruling 21:45 PT (card C2-14b, F14-1): a brace-less B-CD-01 code in a field DECLARED a
T-2 vault reference is accepted by sink_guard without the strict secret heuristic; the same string in any other field
keeps the strict check; a secret-shaped string in a T-2 field that does not match the grammar is refused.
Synthetic values only."""
from __future__ import annotations

import random

import pytest

import sink_fields as sf
import sink_guard as sg
from primitives import codes

T0 = "2026-11-14T00:00:00Z"
TID = "acme"


def _hexes(seed: int, n: int) -> list:
    rnd = random.Random(seed)
    out = []
    while len(out) < n:
        h = "".join(rnd.choice("0123456789abcdef") for _ in range(16))
        if any(c.isalpha() for c in h) and h not in out:
            out.append(h)
    return out


def _strict_refused(v: str) -> bool:
    return sg._judge(v)[1] is not None


def _log(span) -> dict:
    return {"tenant_id": TID, "span": span, "surface": "citation_run", "outcome": "revealed", "actor": "hub"}


def _row(span) -> dict:
    return {"tenant_id": TID, "actor": "hub", "at": T0, "span": span, "surface": "citation_run",
            "outcome": "revealed"}


REFS = [f"c:{k}:{h}" for k, h in zip(list(codes.KINDS) * 20, _hexes(2141, 20 * len(codes.KINDS)))]
STRICT = [r for r in REFS if _strict_refused(r)]


def test_the_samples_are_ones_the_strict_check_refuses():
    # without the T-2 branch these would be refused (F14-1); the test is only meaningful if many are
    assert len(STRICT) >= len(REFS) // 4, (len(STRICT), len(REFS))


def test_declared_t2_fields_are_exactly_the_ref_entries():
    ref_paths = sorted(k for k, f in sf.SINK_FIELDS.items() if f.ref)
    assert ref_paths, "no T-2 field declared"
    assert all(sf.SINK_FIELDS[k].types == {"code"} for k in ref_paths)


@pytest.mark.parametrize("kind,record", [("span_reveal", _log), (sg.LEDGER + "span_revealed", _row)])
def test_t2_field_accepts_every_braceless_code(kind, record):
    bad = [r for r in REFS if sg.violations(kind, record(r)) != []]
    assert bad == []
    assert [r for r in REFS if sg.refused(kind, record(r))] == []


@pytest.mark.parametrize("kind,record", [("span_reveal", _log), (sg.LEDGER + "span_revealed", _row)])
def test_same_string_in_a_non_ref_field_keeps_the_strict_check(kind, record):
    for r in STRICT[:60]:
        for field in ("tenant_id", "surface", "outcome"):
            d = record(r)
            d[field] = r
            reasons = {why for p, why in sg.violations(kind, d) if p == "/" + field}
            assert reasons - {"field_type", "untyped"}, (field, reasons)    # the strict class verdict, not only T-5
        d = {**record(r), "note": r}                                        # an undeclared field of the same record
        assert {why for p, why in sg.violations(kind, d) if p == "/note"} - {"field_type", "untyped"}


def test_same_string_in_a_kind_without_t2_fields_keeps_the_strict_check():
    for r in STRICT[:60]:
        assert ("/span", sg._judge(r)[1]) in sg.violations("not_a_typed_kind", {"span": r})
        assert "/span" in sg.refused("not_a_typed_kind", {"span": r})
        assert sg.violations("", r) != []                                   # a bare value: no field, no waiver


def _planted(seed: int) -> list:
    rnd = random.Random(seed)
    out = []
    for h in _hexes(seed, 40):
        tok = "".join(rnd.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(24))
        out += [
            "{c:text:" + h + "}",                 # Wave-1 D-02a braced form stays refused
            "c:text:" + h + "0",                  # 17 hex
            "c:text:" + h[:15],                   # 15 hex
            "c:text:" + h.upper(),                # upper hex
            "c:nokind:" + h,                      # kind outside codes.KINDS
            "c:text:" + h + " ",                  # trailing whitespace
            "c:text:" + h + "\n",
            "x c:text:" + h,                      # prefix
            "c:text:" + h + tok,                  # code + key-shaped tail
            "sk_live_" + tok,                     # synthetic key-shaped token
            "c:text:" + tok,                      # grammar shape, non-hex body
            h,                                    # bare 16 hex
        ]
    return out


@pytest.mark.parametrize("kind,record", [("span_reveal", _log), (sg.LEDGER + "span_revealed", _row)])
def test_secret_shaped_string_in_a_t2_field_not_matching_the_grammar_is_refused(kind, record):
    planted = _planted(2142)
    assert [p for p in planted if sf.is_ref(p)] == []
    taken = [p for p in planted if "/span" not in sg.refused(kind, record(p))]
    assert taken == []
