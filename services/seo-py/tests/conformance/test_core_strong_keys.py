"""core-spec/1 section 5: strong keys, token / digit / e-mail shapes, normalisation and caps (K-1..K-4, N-1, N-2), frozen
at 186471eb behaviour. The generated leak corpus (``tests/leak_corpus_v2.py``: every family of ADR 0050 v2 2a-2f x
every disguise, synthetic values made at run time) is withheld by the strict lane, in full; the client view masks every
strong-key family. Counts only."""
import collections

import pytest

import leak_corpus_v2 as lc

pytestmark = pytest.mark.conformance
FAMILIES = ("aws_access_key_id", "openai_anthropic", "sk_generic", "github", "stripe", "slack", "google", "perplexity",
            "gitlab", "npm_pypi", "huggingface", "sendgrid", "jwt", "shopify", "amazon_lwa", "azure", "square", "vault",
            "discord", "mapbox", "twilio", "mailgun", "digitalocean", "misc_saas", "db_uri", "pem_pgp")
PERSONAL = frozenset(("card_visa", "card_amex", "ssn", "phone_us", "phone_intl", "email"))
# (families with no strong prefix: the token rule (d), strict only: the client view shows them, R-p)
NO_PREFIX = frozenset(("aws_secret",))


@pytest.fixture(scope="module")
def sg():
    from agents import secret_guard
    return secret_guard


def test_core_K_1_the_26_strong_key_families(sg):
    """K-1: the prefix families are exactly the 26 named."""
    assert tuple(n for n, _rx in sg.PROVIDERS) == FAMILIES


def test_core_K_1_K_2_K_3_K_4_N_1_every_case_withheld_by_the_strict_lane(sg):
    """K-1..K-4, N-1: every case of the generated leak corpus (families x disguises x pairs) is withheld."""
    cases = lc.corpus()
    missed = collections.Counter((c.family, c.disguise) for c in cases if sg.check(c.carrier) is None)
    print(f"K: cases={len(cases)} families={len({c.family for c in cases})} missed={sum(missed.values())}")
    assert not missed, sorted(missed)[:20]


def test_core_K_1_the_client_view_masks_every_strong_key(sg):
    """K-1 (CV ``strong_key``): every unlabelled strong-key case is masked by the client view."""
    cases = [c for c in lc.corpus() if c.family not in PERSONAL and c.family != "labelled"
             and not c.family.endswith("_unlabelled") and c.family not in NO_PREFIX]
    missed = collections.Counter((c.family, c.disguise) for c in cases if not sg.client_view_findings(c.carrier))
    print(f"K-1 CV: cases={len(cases)} shown={sum(missed.values())}")
    assert not missed, sorted(missed)[:20]


def test_core_N_2_caps(sg):
    """N-2: the caps of the spec, and what happens past them (withheld unread / masked / fail closed)."""
    assert (sg.MAX_INPUT, sg.URL_MAX_INPUT, sg.URL_TEXT_MAX, sg.URL_PARTS_MAX) == (65536, 8192, 64, 1024)
    assert (sg.GUARD_MAX_DEPTH, sg.GUARD_MAX_NODES) == (16, 200000)
    assert (sg.PERCENT_ROUNDS, sg.DECODE_TRIM, sg.DECODE_RUNS_MAX, sg.DECODE_RUN_MAX) == (3, 6, 64, 4096)
    big = "ordinary words " * (sg.MAX_INPUT // 15 + 2)
    assert sg.check(big) is not None and sg.client_view_findings(big) == ["cap"]
    deep: dict = {}
    node = deep
    for _ in range(sg.GUARD_MAX_DEPTH + 4):
        node["k"] = {}
        node = node["k"]
    assert sg.guard_record(deep) != deep


def test_core_N_2_guard_record_fails_closed(sg, monkeypatch):
    """N-2: ``guard_record`` fails closed on an internal error (the whole record withheld)."""
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(sg, "_guard", boom)
    assert sg.guard_record({"a": "ordinary"}) == sg.WITHHELD
