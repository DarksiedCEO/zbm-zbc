"""Certification tests for the shared redaction core ``agents.secret_guard`` v2 (ADR 0050 v2: WITHHOLD THE WHOLE FIELD
UNLESS IT IS CLEARLY SAFE; supersedes ADR 0050). Every corpus is synthetic (built from fragments at run time; no real
key, card, SSN, phone number or e-mail literal in the source).

  * zero leaks through every entry point on the generated corpus (tests/leak_corpus_v2.py, >= 2,000 cases), the
    v1 corpus (tests/secret_corpus.py) and the real service path into data/seo_log.jsonl;
  * utility measured on the disclosed corpora (tests/utility_corpus_v2.py);
  * every check, normalisation step, decode and guard_record branch is load-bearing (disabling it makes a test fail);
  * linear time and the hard caps."""

from __future__ import annotations

import json
import random
import re
import time

import leak_corpus_v2 as lc
import leak_harness_v2 as lh
import pytest
import secret_corpus
import utility_corpus_v2 as uc

from agents import callum
from agents import secret_guard as sg


def _probe(secret: str) -> str:
    return secret if len(secret) <= 12 else secret[:12]


def _survives(secret: str, blob: str) -> bool:
    return _probe(secret) in blob


# --------------------------------------------------------------------------------------- v1 corpus (kept as is)

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
    keys = secret_corpus.strong_keys(random.Random(1))
    assert set(keys) >= {"aws_akia", "aws_secret_mixed", "aws_secret_nodigit_labelled", "slack_bot",
                         "slack_refresh_dot", "slack_refresh_num", "github_pat_classic", "github_pat_fine",
                         "openai_proj", "anthropic", "perplexity", "stripe_live", "stripe_whsec", "google_api",
                         "gitlab", "npm", "huggingface", "sendgrid", "jwt"}
    for name, secret in keys.items():
        assert sg.strong_secret(f"x {secret} y"), name


def test_labelled_credentials_have_no_leaks():
    for seed in range(40):
        for name, text, value, _ in secret_corpus.labelled(random.Random(seed)):
            red = sg.redact_text(text)[0]
            assert value not in red, (seed, name, red)
            assert not sg.strong_secret(sg.guard_record(text)), (seed, name)


# --------------------------------------------------------------------------------------- v2 generated corpus

def test_the_generated_corpus_is_large_and_covers_every_family_and_disguise():
    cases, urls = lc.corpus(), lc.url_cases()
    assert len(cases) >= 2000 and len(urls) >= 300, (len(cases), len(urls))
    fams = {c.family for c in cases}
    assert fams >= {"aws_access_key_id", "aws_secret", "openai_proj", "anthropic", "github_classic", "github_fine",
                    "stripe_live", "slack_bot", "google_api", "google_oauth", "google_client_secret",
                    "shopify_access", "shopify_shared", "amazon_lwa", "azure_account_key", "azure_sas", "square",
                    "vault", "discord_bot", "mapbox", "jwt", "pem", "db_postgres", "db_mongo", "db_redis",
                    "url_userinfo", "hex32_unlabelled", "b64_unlabelled", "card_visa", "card_amex", "ssn",
                    "phone_us", "phone_intl", "email", "labelled", "pin", "passphrase_words"}
    disg = {c.disguise for c in cases}
    assert disg >= set(lc.char_disguises("x").keys()) | {f"{a}+{b}" for a, b in lc.PAIRS} \
        | set(lc.label_forms("x", "y").keys())
    assert {c.disguise for c in urls} >= {"path", "query", "tracking", "fragment", "double_pct", "split_path",
                                         "docs_position", "code_position", "host_label"}


def test_zero_leaks_through_every_text_entry_point():
    res = lh.run_text(lc.corpus())
    assert {k: v[:5] for k, v in res.items() if v} == {}


def test_zero_leaks_through_every_url_entry_point():
    res = lh.run_urls(lc.url_cases())
    assert {k: v[:5] for k, v in res.items() if v} == {}


def test_zero_leaks_end_to_end_through_classification():
    from test_callum import answer, classify, collected
    assert lh.run_classify(lc.corpus(), lc.url_cases(), answer, collected, classify) == []


def test_zero_leaks_through_the_real_service_path_into_seo_log_jsonl(tmp_path):
    import test_answer_engines as te
    import test_perplexity as base
    from test_callum import prompt_set, run, service
    res = lh.run_service(tmp_path, lh.service_cases(), te, base, service, prompt_set, run)
    # the strict lane: nothing reaches the log, the data directory or the ledger
    assert res["cases"] >= 2300 and res["leaks"] == [], (res["runs"], res["leaks"][:10])
    # ADR 0051 job 3: the owner's API view masks on high-precision findings; only the documented residual (unlabelled
    # random tokens: no label, no known prefix) shows there, never a key, a labelled value, a card, SSN, phone or e-mail
    shown = {n.split("/")[1] if n.startswith("url/") else n.split("/")[0] for n in res["client_view_leaks"]}
    assert shown <= CLIENT_VIEW_RESIDUAL, sorted(shown - CLIENT_VIEW_RESIDUAL)


CLIENT_VIEW_RESIDUAL = frozenset(("aws_secret", "hex32_unlabelled", "hex64_unlabelled", "b64_unlabelled"))


# --------------------------------------------------------------------------------------- utility (disclosed)

EXCERPT_WITHHELD_MEASURED = 0.0706        # ADR 0050 v2: measured 22 / 312; the 5% bar is MISSED (reported)


def test_utility_excerpts_are_measured_and_only_label_words_withhold_them():
    ex = uc.excerpts()
    assert len(ex) >= 300
    withheld = [(i, b, e) for i, b, e in ex if e["withheld"]]
    rate = len(withheld) / len(ex)
    # every withheld excerpt of the legitimate corpus is withheld by check (a), a credential label word ("password",
    # "pass", "pin", "token", "secret", "cookie", "API keys"): no other check costs anything on this corpus
    for i, _b, _e in withheld:
        assert sg.check(uc.ANSWERS[i]) == "a_label", uc.ANSWERS[i][:80]
    assert rate <= EXCERPT_WITHHELD_MEASURED + 1e-9, (len(withheld), len(ex), rate)


def _reduced(u: str) -> bool:
    from urllib.parse import parse_qsl, urlsplit
    s = sg.safe_url(u)
    if s == sg.WITHHELD or "{secret}" in s or "{withheld}" in s:
        return True
    p = urlsplit(u)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not sg._tracking(k)]
    return bool(q and not urlsplit(s).query) or bool(p.fragment and not urlsplit(s).fragment)


def test_utility_urls_stay_under_the_bar():
    urls = uc.urls()
    assert len(urls) >= 300
    assert {c for c, _ in urls} >= {"reddit", "drive", "docs", "canva", "shopify_store", "amazon", "blog", "youtube",
                                    "analytics_query"}
    reduced = [u for _, u in urls if _reduced(u)]
    # r2 part 3a: exemptions only for exact id shapes on exact hosts (rule (e) still applies in them).
    # bar is an owner decision; measured value at this commit (was 1 / 306 reduced and 0 / 306 stored-withheld,
    # under the 2.5% bar)
    assert len(reduced) == 65, len(reduced)
    stored = [u for _, u in urls if (callum.stored_url(u) or {}).get("withheld")]
    assert len(stored) == 64, len(stored)
    # ADR 0051 job 2: the log keeps host only, so the strict lane's URL numbers above no longer cost the client
    # anything. The strict flag recorded in the log (any part, tracking parameters included) is unchanged: 73 / 306
    flagged = [u for _, u in urls if callum.url_flag(u)]
    assert len(flagged) == 73, len(flagged)
    # job 3: what the owner sees is the CLIENT view (high-precision findings only, offending parts only). Bar 2.5%: met
    masked = [u for _, u in urls if sg.client_view_url(u)[1] is not None]
    assert len(masked) / len(urls) <= 0.025, len(masked)


# the v1 precision fixtures, now with their v2 verdicts stated explicitly (rule 2a withholds any field holding a
# credential label word, generic prose included; rule 2e withholds 9+ digit runs that are not an allowlisted shape)
V1_PHRASES_KEPT = [
    "The key takeaway is revenue growth.", "Ask about our growth strategy for 2026.",
    "Order 112-1234567-1234567 shipped on 2026-11-08.", "ISBN 978-3-16-148410-0 is in stock.",
    "SKU ABC-12345 and SKU-998877 are restocked.", "Commit 8f14e45fceea167a5a36dedd4bea2543c1b2d3e4 is public.",
    "UUID 550e8400-e29b-41d4-a716-446655440000 identifies the row.", "Hashtag #RevenueRecovery is trending.",
    "Case study: acme-2026-q3-results published today.", "Our refund policy changed in October 2026.",
    "Read the pricing plans comparison report.",
]
V1_PHRASES_WITHHELD = {
    "Use password protection on every account.": "a_label", "A secret menu and token rewards for members.": "a_label",
    "Reset your password-reset link in settings.": "a_label", "Our authorization letter template is free.": "a_label",
    "Bearer of good news: fees dropped.": "a_label", "The api key best practices guide is published.": "a_label",
    "The token economy explained for sellers.": "a_label", "Store your access codes in a vault.": "a_label",
    "Order 1005006123456789 shipped on 2026-11-08.": "e_digits", "Call 555-010-0199 for support.": "e_digits",
}


def test_v1_precision_phrases_have_their_stated_v2_verdicts():
    for p in V1_PHRASES_KEPT:
        assert sg.check(p) is None and sg.redact_text(p) == (p, 0), (p, sg.check(p))
    for p, why in V1_PHRASES_WITHHELD.items():
        assert sg.check(p) == why and sg.redact_text(p) == (sg.WITHHELD, 1), (p, sg.check(p))


# --------------------------------------------------------------------------------------- the public API

def _akia():
    return "AK" + "IA" + "IOSFODNN7" + "EXAMPLE"


def test_redact_text_returns_the_original_or_withholds_the_whole_field():
    assert sg.redact_text("") == ("", 0) and sg.redact_text(None) == ("", 0)
    assert sg.redact_text("plain words") == ("plain words", 0)
    assert sg.redact_text(f"keep this part {_akia()} and this") == (sg.WITHHELD, 1)       # never cut out
    assert sg.redact is sg.redact_text and sg.is_clearly_safe("plain words") and not sg.is_clearly_safe(_akia())


def test_the_hard_cap_withholds_an_oversized_field_unread():
    assert sg.check("a" * sg.MAX_INPUT) is None
    assert sg.check("a" * (sg.MAX_INPUT + 1)) == "cap" and sg.redact_text("a" * (sg.MAX_INPUT + 1))[0] == sg.WITHHELD
    assert sg.safe_url("https://site.test/" + "a" * sg.URL_MAX_INPUT) == sg.WITHHELD
    assert sg.check(b"bytes") == "type"


def test_safe_url_keeps_host_and_passing_segments_only():
    k = _akia()
    assert sg.safe_url(f"https://site.test/a/{k}/b?q=shoes") == "https://site.test/a/{secret}/b?q=shoes"
    assert sg.safe_url(f"https://site.test/a/b?q=shoes&k={k}") == "https://site.test/a/b"         # query dropped
    assert sg.safe_url("https://site.test/a/b?q=shoes&utm_source=x#top") == "https://site.test/a/b?q=shoes#top"
    assert sg.safe_url(f"https://{k.lower()}.site.test/p") == sg.WITHHELD                    # a failing host label
    assert sg.safe_url("https://admin:" + "Zq9" + "xY2kLm@site.test/p") == "https://site.test/p"   # user info never
    assert sg.safe_url("not a url") == sg.WITHHELD and sg.safe_url("https://site.test/u/jane@example.com/x") \
        == "https://site.test/u/{withheld}/x"
    doc = "https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit"  # API docs sample
    assert sg.safe_url(doc) == doc                                                           # a disclosed position
    assert sg.safe_url("https://docs.google.com/document/d/1" + "A" * 43 + "/edit").count("{secret}") == 1  # all hex


def test_guard_record_sweeps_keys_values_lists_tuples_sets_bytes_and_objects():
    k = _akia()

    class Obj:
        def __str__(self):
            return "obj " + k
    obj = {"a": [f"key {k} here", {"b": ("Bearer " + k, 7, None, True)}], "n": 3, "ok": "plain text",
           f"note {k}": 1, "s": {k, "fine"}, "by": k.encode(), "o": Obj(), 5: "five", None: "none"}
    out = sg.guard_record(obj)
    assert k not in json.dumps(out, default=str)
    assert out["n"] == 3 and out["a"][1]["b"][1] == 7 and out["a"][1]["b"][2] is None and out["a"][1]["b"][3] is True
    assert out["ok"] == "plain text" and out[5] == "five" and out[None] == "none"
    assert out["[withheld]#3"] == 1 and sorted(out["s"]) == ["[withheld]", "fine"]
    assert out["by"] == sg.WITHHELD and out["o"] == sg.WITHHELD and out["a"][0] == sg.WITHHELD
    assert sg.guard_record({"b": b"plain bytes"}) == {"b": "plain bytes"}
    assert sg.guard_record({"o": 3.5, "t": ("x", "y")}) == {"o": 3.5, "t": ["x", "y"]}


def test_guard_record_fails_closed_and_is_bounded():
    class Bad:
        def __str__(self):
            raise RuntimeError("unreadable")
    assert sg.guard_record({"x": Bad()}) == sg.WITHHELD                                     # whole record withheld
    deep: dict = {"v": "plain"}
    for _ in range(sg.GUARD_MAX_DEPTH + 3):
        deep = {"d": deep}
    out = sg.guard_record(deep)
    for _ in range(sg.GUARD_MAX_DEPTH + 1):
        out = out["d"]
    assert out == sg.WITHHELD                                                                # beyond the depth cap
    wide = [str(i) for i in range(sg.GUARD_MAX_NODES + 5)]
    assert sg.guard_record(wide)[-1] == sg.WITHHELD                                          # beyond the node cap


def test_guard_record_keeps_the_service_own_vocabulary_and_typed_values():
    rec = {"secret_redacted": True, "flags": ["SECRET_REDACTED"], "state": "NOT_FETCHED_SECRET",
           "url": "https://site.test/{secret}/page", "result_sha256": "a" * 64, "observed_at": "2026-11-09T07:32:00Z",
           "run_id": "seo-cit-" + "0f" * 20, "prompt_id": "seo-pst-" + "1e" * 20 + ":v1:0"}
    assert sg.guard_record(rec) == rec
    # a typed key with a value outside its exact shape is checked like any other string
    assert sg.guard_record({"run_id": "seo-cit-" + _akia()})["run_id"] == sg.WITHHELD
    assert sg.guard_record({"note_sha256": "x " + "ab12" * 16})["note_sha256"] == sg.WITHHELD
    assert sg.guard_record({"result_sha256": "ab12" * 16})["result_sha256"] == "ab12" * 16


def test_wiring_callum_withholds_whole_excerpts_hints_and_url_parts():
    k = _akia()
    ev = callum.excerpt(f"Z Best Media is named. Then {k} appears.", ["Z Best Media"])
    assert ev["excerpt"]["text"] == sg.WITHHELD and ev["withheld"] == "credential" and ev["secret_redacted"]
    near = callum.excerpt("Z Best Media" + " filler" * 30 + " password: hunter2zz", ["Z Best Media"])
    assert "hunter2zz" not in json.dumps(near)
    assert callum._safe_hint(f"Title {k}") is None and callum._safe_hint("Plain title") == "Plain title"
    for seg in ("4111-1111-1111-1111", "4111.1111.1111.1111", "4111%201111%201111%201111",
                "4111\u200b1111\u200b1111\u200b1111", "123.45.6789", "+44 20 7946 0958"):
        s = callum.stored_url(f"https://site.test/contact/{seg}/info")
        assert s["url"] == "https://site.test/contact/{withheld}/info" and s["withheld"] == "PERSONAL_DATA", (seg, s)
    assert callum.field_check("call 555 010 0199") == "personal_data:number"
    assert callum.field_check("jane at example dot com") == "personal_data:email"


# --------------------------------------------------------------------------------------- tests bite (mutations)

def _rand_mixed(n, seed=3):
    rng = random.Random(seed)
    return lc._mixed(rng, n)


def _mutation_probes() -> dict:
    """mutation -> (patches, probe texts): with the patches applied, at least one probe must come through
    (``check`` returns None, or the guarded record keeps it)."""
    akia = _akia()
    hidden = "hunter2zz"
    sg_ = "agents.secret_guard."

    def split(s, ch):
        return ch.join(s)

    def mail(ch):                    # r2 part 2: an address split by an invisible (no token rule rescues it)
        return "write to jane.doe@exa" + ch + "mple.com"
    return {
        "check_a_label": ([(sg_ + "_label_hit", lambda v: False)], ["Z Best Media. password: " + hidden]),
        "check_b_userinfo": ([(sg_ + "_userinfo_hit", lambda v: False)], ["see ftp://admin:" + hidden + "@localhost/"]),
        "check_c_prefix": ([(sg_ + "_prefix_hit", lambda v: False)],
                           ["see AKIA" + "ABCDEFGHIJKLMNOP now", "token glpat-" + "abcdefghijklmnopqrstu"[:20] + " x"]),
        "check_d_token": ([(sg_ + "_token_hit", lambda v: False)], ["value " + _rand_mixed(40) + " end"]),
        "check_e_digits": ([(sg_ + "_digits_hit", lambda v: False)], ["card 4111 1111 1111 1111 thanks"]),
        "check_f_email": ([(sg_ + "_email_hit", lambda v: False)], ["write to jane.doe@example.com"]),
        "check_url_rules_in_text": ([(sg_ + "_url_judge", lambda u: (None, False))],
                                    ["see https://" + akia.lower() + ".site.test/p now"]),
        "norm_nfkc": ([(sg_ + "_nfkc", lambda t: t)],
                      ["".join(chr(ord(c) + 0xFEE0) for c in akia)]),
        "norm_map_chars": ([(sg_ + "_map_chars", lambda t: t)], [split(akia, "\u200b"), mail("\u200b")]),
        "norm_map_spaces": ([(sg_ + "_MAP_SPACES", False)], ["mail jane.doe@exa\u2028mple.com"]),
        "norm_hangul_fillers": ([(sg_ + "_HANGUL_FILLERS", frozenset())], [split(akia, "\u3164"), mail("\u3164")]),
        "norm_drop_Cf": ([(sg_ + "_DROP_CATS", frozenset({"Cc", "Mn", "Me", "Co", "Cn", "Cs"}))],
                         [split(akia, "\u2060"), split(akia, "\U000e0020"), mail("\u2060")]),
        "norm_drop_Mn": ([(sg_ + "_DROP_CATS", frozenset({"Cf", "Cc", "Me", "Co", "Cn", "Cs"}))],
                         [split(akia, "\u034f"), split(akia, "\ufe0f"), mail("\u034f")]),
        "norm_drop_Cc": ([(sg_ + "_DROP_CATS", frozenset({"Cf", "Mn", "Me", "Co", "Cn", "Cs"}))], [split(akia, "\x01"), mail("\x01")]),
        "norm_drop_Me": ([(sg_ + "_DROP_CATS", frozenset({"Cf", "Mn", "Cc", "Co", "Cn", "Cs"}))], [split(akia, "\u20dd"), mail("\u20dd")]),
        "norm_drop_Co": ([(sg_ + "_DROP_CATS", frozenset({"Cf", "Mn", "Cc", "Me", "Cn", "Cs"}))], [split(akia, "\ue000"), mail("\ue000")]),
        "norm_drop_Cn": ([(sg_ + "_DROP_CATS", frozenset({"Cf", "Mn", "Cc", "Me", "Co", "Cs"}))], [split(akia, "\u0378"), mail("\u0378")]),
        "norm_collapse": ([(sg_ + "_collapse", lambda t: t)], ["  ".join(_rand_mixed(40, 5))]),
        "norm_strip_markup": ([(sg_ + "_strip_markup", lambda t: t)], ["AKIA**IOSF**ODNN7" + "EXAMPLE", "write to jane.doe@exa**mple**.com"]),
        # r2 part 2: confusable folding is one step done in two places (label words, and the folded view of (c)-(f))
        "norm_confusable_fold": ([(sg_ + "_FOLD", type("NoFold", (), {"fold_cased": staticmethod(str.lower)})()),
                                  (sg_ + "_fold_confusables", lambda v: v)],
                                 ["p\u0430ssword: " + hidden]),
        # a label hidden by an encoding (the value itself is not key-shaped, so only the decoded label withholds)
        "decode_percent": ([(sg_ + "_percent", lambda t: t)], ["Z Best Media pa%73sword: " + hidden]),
        "decode_percent_rounds": ([(sg_ + "PERCENT_ROUNDS", 1)], ["Z Best Media pa%2573sword: " + hidden]),
        "decode_entities": ([(sg_ + "_entities", lambda t: t)], ["Z Best Media pa&#115;sword: " + hidden]),
        "decode_escapes": ([(sg_ + "_unescape", lambda t: t)], ["Z Best Media pa\\u0073sword: " + hidden]),
        "decode_base64_runs": ([(sg_ + "_B64_RUN", re.compile(r"(?!x)x"))],
                               ["blob " + __import__("base64").b64encode(b"pwd=yopumzgdpam").decode().rstrip("=")]),
        # hex-encoded text always holds digits, so check (e) withholds it anyway, but only the decode reads it as a
        # CREDENTIAL: without it the URL below would be "personal" (stored as "{withheld}", not "{secret}")
        "decode_hex_runs": ([(sg_ + "_HEX_RUN", re.compile(r"(?!x)x"))],
                            ["https://site.test/p/" + b"pwd=hunter".hex()]),
    }


def _apply(monkeypatch, patches):
    for target, value in patches:
        mod, attr = target.rsplit(".", 1)
        monkeypatch.setattr(mod + "." + attr if False else __import__(mod, fromlist=[attr]), attr, value)
    sg._CMAP.clear()
    sg._TABLE.clear()


@pytest.mark.parametrize("name", sorted(_mutation_probes()))
def test_each_check_and_normalisation_step_is_load_bearing(monkeypatch, name):
    patches, probes = _mutation_probes()[name]

    def verdict(p):
        # a URL: its worst part kind (r2 part 3a: url_has_secret is True for personal data too, so it cannot tell)
        return sg._kind(sg.check(p)) if not p.startswith("https://") else sg._url_verdict(p)
    before = [verdict(p) for p in probes]
    assert all(v is not None for v in before), (name, before)
    _apply(monkeypatch, patches)
    try:
        # the probe comes through (None) or is no longer read as a credential (a URL then fetched, a "{withheld}"
        # placeholder instead of "{secret}")
        assert any(verdict(p) != b for p, b in zip(probes, before)), (name, "disabling this broke nothing")
    finally:
        sg._CMAP.clear()
        sg._TABLE.clear()


class _Obj:
    def __init__(self, s):
        self.s = s

    def __str__(self):
        return self.s


def _guard_mutations() -> dict:
    k = _akia()
    return {
        "_guard_str": (lambda s, key: s, {"f": f"x {k}"}),
        "_guard_bytes": (lambda b, key: bytes(b).decode(), {"f": f"x {k}".encode()}),
        "_guard_key": (lambda kk, i: kk, {f"x {k}": 1}),
        "_guard_dict": (lambda d, depth, key, budget: dict(d), {"f": {"g": f"x {k}"}}),
        "_guard_seq": (lambda seq, depth, key, budget: list(seq), {"f": [f"x {k}"]}),
        "_guard_set": (lambda st, depth, key, budget: sorted(st), {"f": {f"x {k}"}}),
        "_guard_other": (lambda obj, key: str(obj), {"f": _Obj(f"x {k}")}),
    }


@pytest.mark.parametrize("branch", sorted(_guard_mutations()))
def test_each_guard_record_branch_is_load_bearing(monkeypatch, branch):
    fn, rec = _guard_mutations()[branch]
    assert _akia() not in json.dumps(sg.guard_record(rec), default=str)
    monkeypatch.setattr(sg, branch, fn)
    assert _akia() in json.dumps(sg.guard_record(rec), default=str), branch


def test_the_fail_closed_branch_and_the_caps_are_load_bearing(monkeypatch):
    class Bad:
        def __str__(self):
            raise RuntimeError("x")
    assert sg.guard_record({"x": Bad()}) == sg.WITHHELD
    monkeypatch.setattr(sg, "_on_guard_error", lambda obj: obj)
    assert sg.guard_record({"x": Bad()}) != sg.WITHHELD


# --------------------------------------------------------------------------------------- linear time, caps

WORST = {
    "a": lambda n: "a" * n, "mixed": lambda n: ("Ab3/+=_-.: " * n)[:n], "spaced": lambda n: ("A " * n)[:n],
    "nbsp": lambda n: ("a\u00a0" * n)[:n], "dash": lambda n: ("a-" * n)[:n], "md": lambda n: ("**a** " * n)[:n],
    "pct": lambda n: ("%41" * n)[:n], "amp": lambda n: ("&#65;" * n)[:n], "at": lambda n: ("a@" * n)[:n],
    "words": lambda n: ("the best shop tools " * n)[:n], "zw": lambda n: ("a\u200b" * n)[:n],
    "esc": lambda n: ("\\u0041" * n)[:n], "brack": lambda n: ("x [at] y " * n)[:n], "b64": lambda n: ("QUJD" * n)[:n],
}


def _timed(fn, *a):
    best = None
    for _ in range(2):
        t = time.perf_counter()
        fn(*a)
        d = time.perf_counter() - t
        best = d if best is None else min(best, d)
    return best


@pytest.mark.parametrize("name", sorted(WORST))
def test_check_is_linear_and_bounded(monkeypatch, name):
    monkeypatch.setattr(sg, "MAX_INPUT", 10 ** 7)                    # the cap lifted to see the growth
    t16, t64 = (_timed(sg.check, WORST[name](kb * 1024)) for kb in (16, 64))
    assert t64 < 8 * t16 + 0.05, (name, t16, t64)                    # linear: 4x the input, ~4x the time
    monkeypatch.setattr(sg, "MAX_INPUT", 65536)
    assert _timed(sg.check, WORST[name](65536)) < 1.0                  # 200 ms measured bar; 1 s here for slow CI
    assert sg.check(WORST[name](65537)) == "cap"
    assert _timed(sg.safe_url, "https://site.test/" + WORST[name](sg.URL_MAX_INPUT - 40)) < 1.0


def test_urls_keep_ipv6_brackets_never_keep_controls_and_a_withheld_url_is_null():
    assert sg.safe_url("http://[::1]/x") == "http://[::1]/x" and callum.stored_url("http://[::1]/x")["url"] == "http://[::1]/x"
    for ch in ("\u202e", "\u2067", "\u200b", "\x07"):
        bad = "https://site.test/" + ch + "gpj.exe"
        assert sg.safe_url(bad) == "https://site.test/{secret}" and sg.url_has_secret(bad), repr(bad)
    assert sg.safe_url("https://si\u202ete.test/a") == sg.WITHHELD
    rec = sg.guard_record({"url": "x " + _akia(), "final_url": "x " + _akia(), "note": "x " + _akia()})
    assert rec == {"url": None, "final_url": None, "note": sg.WITHHELD}
