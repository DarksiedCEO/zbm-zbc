"""core-spec/2 AL-1 on merged spans (card C2-13e; findings F13c-4 / F13d-1).

The union mask (C2-13d) merges a VS token with an adjacent CV-4 run, so a listed catalog token can sit INSIDE a larger
masked span whose text is not the token. AL-1 must judge each listed token inside such a span: the token is shown,
the rest of the span stays masked. Never exempts K-1 / K-5 (whole span or token), never crosses tenants, never shows a
token glued into a larger CV-2 token (test_cv_allowlist keeps that case).

Plants (synthetic, seeded; counts only, a value is never printed): listed tokens of 10 / 14 / 30 code points (30 > the
24-cp piece limit: merged only by the union) next to a short fragment, a valueish token or both, separated by one
space, NBSP or tab, on either side. Measured: listed tokens masked (must be 0), neighbours shown (must be 0), the other
tenant's tokens shown (must be 0), listed tokens next to a K-1 / K-5 value shown (must be 0)."""
from __future__ import annotations

import random
import string

import pytest

import sink_guard
import vault
from agents import cv_allowlist as al
from agents import cv_shape

MASK = "[hidden: looks like a credential]"
TA, TB = "tenant-alpha", "tenant-beta"
AT = "2026-11-14T17:30:00Z"
SEPS = (" ", "\u00a0", "\t")
LENS = (10, 14, 30)
N = 200                                                  # tokens per length per tenant
K5 = ("4111111111111111", "123-45-6789", "+14155550134")


def _rand(rng, n: int, digits: int) -> str:
    while True:
        cs = [rng.choice(string.ascii_uppercase) for _ in range(n - digits)] + \
             [rng.choice(string.digits) for _ in range(digits)]
        rng.shuffle(cs)
        t = "".join(cs)
        if t[0].isalpha() and not al._never(t):
            return t


def _listed(seed: int) -> list:
    rng, out = random.Random(seed), []
    for n in LENS:
        seen: set = set()
        while len(seen) < N:
            seen.add(_rand(rng, n, rng.randint(2, 4)))
        out += sorted(seen)
    return out


def _neighbours(rng) -> list:
    """(kind, text): a short fragment, a valueish token, and both (fragment + space + valueish)."""
    frag = rng.choice(string.ascii_lowercase) + rng.choice(string.digits) + rng.choice(string.ascii_lowercase)
    vtok = _rand(rng, 10, 3)
    return [("frag", frag), ("valueish", vtok), ("both", frag + " " + vtok)]


@pytest.fixture(scope="module")
def two():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sink_guard, "_REGISTERED", set(sink_guard._REGISTERED))
        sink_guard.register_tenant(TA)
        sink_guard.register_tenant(TB)
        key = vault.ephemeral_vault_key()
        coders = {t: vault.Coder(key, t) for t in (TA, TB)}
        store = al.CatalogAllowlist(coders.__getitem__, lambda kind, row: None)
        a, b = _listed(20261115), _listed(1315)
        assert not set(a) & set(b)
        store.replace(TA, a, actor="seo", source="shopify", integration_tenant=TA, at=AT)
        store.replace(TB, b, actor="seo", source="shopify", integration_tenant=TB, at=AT)
        yield _Plant((store, a, b))


class _Plant(tuple):
    """The fixture value; its repr carries counts only, so a failing traceback never prints a token."""

    def __repr__(self) -> str:
        return f"<plant {len(self[1])} + {len(self[2])} tokens>"


def _cases(toks: list, seed: int):
    rng = random.Random(seed)
    for t in toks:
        for kind, nb in _neighbours(rng):
            sep = rng.choice(SEPS)
            for side in ("after", "before"):
                body = t + sep + nb if side == "after" else nb + sep + t
                yield t, kind, nb, f"Order {body} ships today."


def _view_merged(text: str, t: str) -> bool:
    """True when the token's VS span sits inside a larger merged view span (the F13c-4 situation)."""
    v, _src = cv_shape.canonical(text)
    k = v.find(t)
    return any(a <= k and k + len(t) <= b and (a, b) != (k, k + len(t)) for a, b in cv_shape.view_spans(v))


def test_al1_listed_token_inside_a_merged_span_is_shown_rest_masked(two):
    store, a, b = two
    counts = {"cases": 0, "merged": 0, "listed_masked": 0, "neighbour_shown": 0, "other_shown": 0}
    by: dict = {}
    with al.scope(store, TA):
        for t, kind, nb, text in _cases(a, 7):
            rendered, _ = cv_shape.display(text, MASK)
            counts["cases"] += 1
            counts["merged"] += _view_merged(text, t)
            if t not in rendered:
                counts["listed_masked"] += 1
                key = f"{len(t)}/{kind}"
                by[key] = by.get(key, 0) + 1
            counts["neighbour_shown"] += any(p in rendered for p in nb.split(" "))
        for t, _kind, _nb, text in _cases(b, 8):
            counts["other_shown"] += t in cv_shape.display(text, MASK)[0]
    assert counts["merged"] > 0, counts                  # the plant reaches the merged-span case
    assert counts["listed_masked"] == 0 and counts["neighbour_shown"] == 0 and counts["other_shown"] == 0, \
        (counts, by)


def test_al1_merged_span_with_a_k_value_never_exempt(two):
    store, a, _b = two
    rng, shown_k, merged, shown_t = random.Random(9), 0, 0, 0
    with al.scope(store, TA):
        for t in a:
            k = rng.choice(K5)
            sep = rng.choice(SEPS)
            text = f"Ref {t}{sep}{k} ok"
            rendered, _ = cv_shape.display(text, MASK)
            shown_k += k in rendered
            v, _src = cv_shape.canonical(text)
            i, j = v.find(t), v.find(k)
            if any(x <= i and j + len(k) <= y for x, y in cv_shape.view_spans(v)):
                merged += 1                              # one merged span holds the listed token AND the K-5 value
                shown_t += t in rendered
    assert shown_k == 0, shown_k                         # K-5 never shown
    assert merged > 0 and shown_t == 0, (merged, shown_t)  # a span holding a K-5 value exempts nothing


def test_al1_out_of_scope_nothing_shown(two):
    _store, a, _b = two
    shown = sum(t in cv_shape.display(text, MASK)[0] for t, _k, _n, text in _cases(a[:60], 7))
    assert shown == 0, shown
