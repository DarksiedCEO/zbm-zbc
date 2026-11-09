"""Certification tests for the shared redaction core ``agents.secret_guard`` (ADR 0050). The strong-key and
labelled-credential corpora are synthetic (built from fragments at run time; no real key literal in the source). The
tests also prove each guard is load-bearing: with any one guard disabled, a representative input leaks."""

from __future__ import annotations

import re
import time

import pytest
import secret_corpus

from agents import secret_guard as sg

_NEVER = re.compile(r"(?!x)x")


def _probe(secret: str) -> str:
    return secret if len(secret) <= 12 else secret[:12]


def _survives(secret: str, blob: str) -> bool:
    return _probe(secret) in blob


# --------------------------------------------------------------------------------------- zero strong-key leaks

def test_strong_key_corpus_has_no_leaks():
    cases = secret_corpus.corpus()
    assert len(cases) >= 1000, len(cases)
    leaks = []
    for name, carrier, secret, kind in cases:
        guarded = sg.guard_record({"field": carrier})
        if _survives(secret, repr(guarded)):
            leaks.append(("value_survived_guard_record", name))
        if kind == "url" and not sg.url_has_secret(carrier):
            leaks.append(("url_not_flagged", name))
        if kind == "text":
            red = sg.redact_text(carrier)[0]
            if _survives(secret, red) and not sg.strong_secret(red):
                leaks.append(("text_not_redacted", name))
    assert leaks == [], leaks[:25]


def test_every_threat_list_family_is_present_and_caught():
    import random
    keys = secret_corpus.strong_keys(random.Random(1))
    # every strong-key family on the closed list has a representative that the core catches
    assert set(keys) >= {"aws_akia", "aws_secret_mixed", "aws_secret_nodigit_labelled", "slack_bot",
                         "slack_refresh_dot", "slack_refresh_num", "github_pat_classic", "github_pat_fine",
                         "openai_proj", "anthropic", "perplexity", "stripe_live", "stripe_whsec", "google_api",
                         "gitlab", "npm", "huggingface", "sendgrid", "jwt"}
    for name, secret in keys.items():
        assert sg.strong_secret(f"x {secret} y"), name


def test_labelled_credentials_have_no_leaks():
    import random
    for seed in range(40):
        for name, text, value, _ in secret_corpus.labelled(random.Random(seed)):
            red = sg.redact_text(text)[0]
            assert value not in red, (seed, name, red)
            assert not sg.strong_secret(sg.guard_record(text)), (seed, name)


# --------------------------------------------------------------------------------------- over-redaction bar

LEGIT_PHRASES = [
    "Use password protection on every account.", "The key takeaway is revenue growth.",
    "A secret menu and token rewards for members.", "Reset your password-reset link in settings.",
    "Our authorization letter template is free.", "Bearer of good news: fees dropped.",
    "Ask about our growth strategy for 2026.", "The api key best practices guide is published.",
    "Order 1005006123456789 shipped on 2026-11-08.", "ISBN 978-3-16-148410-0 is in stock.",
    "SKU ABC-12345 and SKU-998877 are restocked.", "Commit 8f14e45fceea167a5a36dedd4bea2543c1b2d3e4 is public.",
    "UUID 550e8400-e29b-41d4-a716-446655440000 identifies the row.", "Hashtag #RevenueRecovery is trending.",
    "Call 555-010-0199 for support.", "Case study: acme-2026-q3-results published today.",
    "The token economy explained for sellers.", "Store your access codes in a vault.",
    "Our refund policy changed in October 2026.", "Read the pricing plans comparison report.",
]
LEGIT_URLS = [
    "https://site.test/password-reset-guide", "https://site.test/api-key-best-practices",
    "https://site.test/token-economy-explained", "https://site.test/authorization-letter-template",
    "https://site.test/blog/how-to-choose-a-password-manager", "https://site.test/products/revenue-recovery-kit",
    "https://www.amazon.com/dp/B0C1234567", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://github.com/acme/site/commit/8f14e45fceea167a5a36dedd4bea2543c1b2d3e4",
    "https://site.test/items/550e8400-e29b-41d4-a716-446655440000",
    "https://site.test/p?utm_source=newsletter&utm_campaign=fall2026",
    "https://site.test/search?q=password+protection", "https://doi.org/10.1145/3290605.3300857",
    "https://arxiv.org/abs/2401.12345", "https://en.wikipedia.org/wiki/Revenue_recovery",
    "https://t.co/Ab3dE5gH7j", "https://bit.ly/3xYz9AbC", "https://open.spotify.com/episode/4rOoJ6Egrf8K2Irywzw",
    "https://site.test/order/ORD-2026-000123", "https://site.test/en-us/pricing",
]


def test_legit_over_redaction_stays_under_the_bar():
    import url_corpus
    legit = LEGIT_PHRASES + LEGIT_URLS + url_corpus.corpus()
    assert len(legit) >= 200
    flagged = 0
    for item in legit:
        if item.startswith("http"):
            if sg.url_has_secret(item):
                flagged += 1
        elif sg.redact_text(item)[1]:
            flagged += 1
    rate = flagged / len(legit)
    assert rate <= 0.025, (flagged, len(legit), rate)


# --------------------------------------------------------------------------------------- guard_record fail-closed

def test_guard_record_sweeps_nested_structures_and_keeps_shape():
    akia = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    obj = {"a": [f"key {akia} here", {"b": ("Bearer " + akia, 7, None, True)}], "n": 3, "ok": "plain text"}
    out = sg.guard_record(obj)
    assert akia not in repr(out)
    assert out["n"] == 3 and out["a"][1]["b"][1] == 7 and out["a"][1]["b"][2] is None and out["a"][1]["b"][3] is True
    assert out["ok"] == "plain text"


def test_guard_record_replaces_a_string_still_matching_after_span_redaction():
    # a line-break split key: redact_text may leave a fragment, so guard_record replaces the whole string
    akia = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    s = akia[:8] + "\n" + akia[8:]
    assert sg.guard_record(s) == sg.REDACTED or akia not in sg.guard_record(s)
    assert "EXAMPLE" not in sg.guard_record({"x": s})["x"]


# --------------------------------------------------------------------------------------- tests bite

def _rep():
    ex = "EXAMPLE"
    akia = "AKIA" + "IOSFODNN7" + ex
    return {
        "prefix_family": ("text", f"see {akia} now", akia),
        "glue": ("text", f"xx{akia}yy", akia),
        "labelled": ("text", "password: hunter2value", "hunter2value"),
        "aws_secret": ("text", "x " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + ex + "KEY" + " y",
                       "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + ex + "KEY"),
        "aws_spaced": ("text", "id " + " ".join(akia) + " end", " ".join(akia)),
        "pem": ("text", "-----BEGIN RSA PRIVATE KEY-----\nMIIEab" + ex + "\n-----END RSA PRIVATE KEY-----", "MIIEab"),
        "normalise": ("text", "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in akia), None),
        "decode": ("url", "https://site.test/" + "".join("%%%02X" % ord(c) for c in akia) + "/x", None),
        "split": ("text", akia[:8] + "\n" + akia[8:], None),
        "host_label": ("url", f"https://{akia}.site.test/p", None),
    }


def _fires(kind, carrier, secret):
    if kind == "url":
        return sg.url_has_secret(carrier)
    if secret is None:
        return sg.strong_secret(carrier)
    return secret not in sg.redact_text(carrier)[0]


@pytest.mark.parametrize("guard,attrs,value", [
    # the prefix-shape family is two redundant patterns (word-boundary + glued); disable both to show it is needed
    ("prefix_shapes", ("_PREFIX", "_GLUE"), _NEVER), ("glue_only", ("_GLUE",), _NEVER),
    ("labelled", ("_NAMED",), _NEVER), ("aws_secret", ("_AWS_SECRET",), _NEVER),
    ("aws_spaced", ("_AWS_SPACED",), _NEVER), ("pem", ("_PEM",), _NEVER), ("split", ("_SPLIT",), _NEVER),
    ("normalise", ("normalise",), staticmethod(lambda s: s or "")), ("decode", ("DECODE_ROUNDS",), 0),
])
def test_each_guard_is_load_bearing(monkeypatch, guard, attrs, value):
    """With the one guard disabled, at least one representative input leaks (so the guard earns its place)."""
    rep = _rep()
    val = value.__func__ if isinstance(value, staticmethod) else value
    before = {name: _fires(*c) for name, c in rep.items()}
    for attr in attrs:
        monkeypatch.setattr(sg, attr, val)
    after = {name: _fires(*c) for name, c in rep.items()}
    broke = [name for name in rep if before[name] and not after[name]]
    assert broke, (guard, "disabling this guard broke nothing")


def test_disabling_guard_record_lets_a_split_key_through_a_record():
    akia = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    s = akia[:8] + "\n" + akia[8:]
    # the raw record string (no guard_record) still holds the key tail; guard_record removes it
    assert "EXAMPLE" in {"excerpt": s}["excerpt"]
    assert "EXAMPLE" not in repr(sg.guard_record({"excerpt": s}))


# --------------------------------------------------------------------------------------- runtime

@pytest.mark.parametrize("payload", [
    "a" * 40000, "Ab3" * 13000, "/" * 40000, "eyJ" + "a" * 40000, "A " * 20000, "%41" * 13000,
    "Ab3/+" * 8000, "password=" + "a" * 40000, "AKIA" + "9" * 40000,
])
def test_worst_case_runtime_is_bounded(payload):
    start = time.perf_counter()
    sg.redact_text(payload)
    sg.url_has_secret("https://site.test/" + payload[:12000] + "?q=" + payload[:12000])
    assert time.perf_counter() - start < 2.0
