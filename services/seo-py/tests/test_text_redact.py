"""Credential redaction and the separator personal-data screen before Theo stores any answer text (ADR 0049, review
rounds 9-10; agents/text_redact.py over agents/secret_guard.py, agents/separator_screen.py).

Property tests: every fake key x every position (start / middle / end, URL query and path, quoted, code span, JSON
value, zero-width split, fully / partly / doubly percent-encoded, single spaces between every character, and AWS ids
in spaced groups) is never stored in a Theo envelope; every phone / card / SSN written with separators generated from
the Unicode P*/S*/Z* categories (runs of 1-3) is withheld. Every key below is FAKE and built from parts at import
time, so no literal credential is committed (and the secret scanner has nothing to find)."""

from __future__ import annotations

import json
import sys
import unicodedata
from urllib.parse import quote

import pytest

from agents import separator_screen, text_redact, theo
from test_theo import CTX, PS, Clock, NativePort, Port, ans, topics

INVISIBLE = "​"
# (name, the text as it would appear, the part that must never be stored)
_V = "a8F3kq9Z" + "p2Lm" + "W7xQ"
KEYS = [
    ("aws_access_key_id", "AK" + "IA" + "IOSFODNN7" + "FAKEKEY"[:7], None),
    ("aws_asia", "AS" + "IA" + "Q3EGDQ7K" + "PZ4MN2XA", None),
    ("jwt", "ey" + "JhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJ4In0" + "." + "c2lnbmF0dXJl", None),
    ("anthropic_key", "sk-" + "ant-" + "api03-" + "x" * 40, None),
    ("openai_key", "sk-" + "proj-" + "Z" * 40, None),
    ("perplexity_key", "pp" + "lx-" + "a1B2c3D4" * 4, None),
    ("github_token", "gh" + "p_" + "B" * 36, None),
    ("github_pat", "github" + "_pat_" + "C" * 40, None),
    ("stripe_key", "sk" + "_live_" + "D" * 24, None),
    ("stripe_test", "rk" + "_test_" + "E" * 24, None),
    ("stripe_webhook", "wh" + "sec_" + "F" * 24, None),
    ("slack_token", "xo" + "xb-" + "1234567890-" + "F" * 12, None),
    ("google_api_key", "AI" + "za" + "G" * 35, None),
    ("gitlab_token", "gl" + "pat-" + "H" * 20, None),
    ("bearer", "Bearer " + "tok" + "I" * 30, "tok" + "I" * 30),
    ("authorization", "Authorization: Bearer " + _V + "x9", _V + "x9"),
    ("password", "password" + "=" + "hunter2" + "J" * 6, "hunter2" + "J" * 6),
    ("access_token", "access_token" + "=" + _V, _V),
    ("auth", "auth" + "=" + _V + "1", _V + "1"),
    ("bare_key", "key" + "=" + _V + "2", _V + "2"),
    ("api_dash_key", "api-key: " + _V + "3", _V + "3"),
    ("apikey", "apikey" + "=" + _V + "4", _V + "4"),
    ("x_api_key", "x-api-key: " + _V + "5", _V + "5"),
    ("client_secret", "client_secret" + "=" + _V + "6", _V + "6"),
    ("aws_secret", "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "K" * 10,
     "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "K" * 10),
]


def placements(key: str, name: str):
    yield "start", f"{key} is what the page shows."
    yield "middle", f"The docs mention {key} in passing, then move on."
    yield "end", f"The page ends with {key}"
    yield "url_query", f"See https://docs.example.test/page?q={key}&x=1 for details."
    yield "url_path", f"See https://docs.example.test/{key}/readme."
    yield "double_quoted", f'The config says "{key}".'
    yield "single_quoted", f"The config says '{key}'."
    yield "code_span", f"Run `{key}` to start."
    yield "json_value", '{"value": ' + json.dumps(key) + "}"
    yield "zero_width_split", "Key: " + INVISIBLE.join(key) + " here."
    yield "percent_full", "Value " + "".join(f"%{b:02X}" for b in key.encode()) + " here."
    yield "percent_partial", "Value " + "".join(c if c.isalnum() else f"%{ord(c):02X}" for c in key) + " here."
    yield "percent_double", "Value " + quote(quote(key, safe=""), safe="") + " here."
    if " " not in key:
        yield "spaced_chars", "Key " + " ".join(key) + " here."
    if name.startswith("aws_a"):
        yield "spaced_groups", "Key " + " ".join(key[i:i + 4] for i in range(0, len(key), 4)) + " here."


CASES = [(n, k, s or k, where, text) for n, k, s in KEYS for where, text in placements(k, n)]


def _stored(text: str) -> str:
    t, plans = topics()
    port = Port({1: ans("Z Best Media: " + text), 2: ans("RivalCo too."), 3: ans("I recommend Z Best Media.")})
    res = theo.run_chains(t, plans, {"openai": port}, ["openai"], 1, lambda **k: None, None, theo.CallCap(100), CTX,
                          Clock())
    env = theo.envelope_for("OK", res, theo.output("zbm", "seo-cnv-" + "0" * 40, t, plans, res, 1), t, plans)
    return json.dumps(env, ensure_ascii=False).replace(INVISIBLE, "")


@pytest.mark.parametrize("name,key,secret,where,text", CASES, ids=[f"{c[0]}-{c[3]}" for c in CASES])
def test_a_credential_is_never_stored_wherever_it_sits(name, key, secret, where, text):
    clean, fired = text_redact.redact(text)
    for frag in (secret, secret[:16], secret[-16:]):
        assert frag not in clean and frag not in clean.replace(" ", ""), (where, clean)
    assert fired, (where, clean)
    blob = _stored(text)
    for frag in (secret, secret[:16], secret[-16:], quote(secret, safe="")[:16]):
        assert frag not in blob and frag not in blob.replace(" ", ""), (where, frag)


def test_native_chains_and_model_labels_never_store_a_credential_either():
    key = KEYS[3][1]
    t, plans = topics()
    p = NativePort({1: ans(f"Z Best Media docs leak {key} here."), 2: ans("RivalCo too."),
                    3: ans("I recommend Z Best Media.")})
    res = theo.run_chains(t, plans, {"openai": p}, ["openai"], 1, lambda **k: None, None, theo.CallCap(100), CTX,
                          Clock())
    row = [r for r in res["rows"] if r["turn"] == 1][0]
    assert key not in json.dumps(res) and row["evidence"]["redacted"] == ["secret_guard"]
    assert theo._clean_label("model-" + key) == "model-[REDACTED:secret_guard]"
    assert theo._clean_label("claude-x-2026") == "claude-x-2026" and theo._clean_label(None) is None


def test_a_model_label_carrying_a_key_is_redacted_in_the_rows():
    key = KEYS[5][1]
    t, plans = topics()
    a1 = ans("Z Best Media is fine.")
    a1.model, a1.model_version = "m " + key, "v " + key
    res = theo.run_chains(t, plans, {"openai": Port({1: a1, 2: ans("RivalCo."), 3: ans("I recommend Z Best Media.")})},
                          ["openai"], 1, lambda **k: None, None, theo.CallCap(100), CTX, Clock())
    assert key not in json.dumps(res)


ORDINARY = ("Z Best Media recovers reimbursements.", "Ask for the token count in your invoice.",
            "The password reset page is at /reset.", "Skip-level managers approve refunds.",
            "Key: consistency matters most.", "ASIA PACIFIC REGION OFFICE HOURS ARE LONG",
            "ASIA PACIFIC REGIONS HQ.", "The token: a unit of text.",
            "cookie: chocolate-chip", "Get 50% off this week.", "see /password-reset-guide for help",
            "Authorization is required before refunds.", "monkey=banana", "a b c d e f g h i j",
            "Plans • Pricing • FAQ", "sort_key=price")


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_text_is_left_alone(text):
    clean, fired = text_redact.redact(text)
    assert clean == text and fired == [], text


def test_every_delta_regex_is_bounded():
    from test_theo import _bad_repetition
    for name, rx in text_redact.RULES + [("pem_tail", text_redact._PEM_TAIL), ("spaced", text_redact._SPACED)]:
        bad = [b for b in _bad_repetition(rx) if b[0] == "unbounded"]
        assert not bad, (name, bad)


def test_percent_decoding_is_bounded_and_stops_when_stable():
    deep = "sk-ant-" + "y" * 30
    for _ in range(text_redact.MAX_DECODE):
        deep = quote(deep, safe="")
    assert "y" * 30 not in text_redact.redact(deep)[0]
    assert text_redact.normalise("100% sure, 50%% off") == "100% sure, 50%% off"


def test_a_private_key_longer_than_one_bounded_match_is_still_removed_whole():
    body = ("Q" * 64 + "\n") * 150                                    # ~9,750 chars, beyond one 4,096 match
    pem = "-----BEGIN RSA " + "PRIVATE KEY-----\n" + body + "-----END RSA " + "PRIVATE KEY-----"
    clean, fired = text_redact.redact("before " + pem + " after")
    assert clean == "before [REDACTED:private_key] after" and fired == ["private_key"]


def test_the_fixture_brand_is_the_one_the_excerpt_keeps():
    assert PS["brand_terms"] == ["Z Best Media"]


# ---------------------------------------------------------------------------------------------- separators (round 10)

def _separator_chars():
    """Every P*/S*/Z* character the base screen does not already read, sampled across the whole code space, plus the
    ones the reviewer named. Generated from unicodedata, not written out."""
    pool = [chr(cp) for cp in range(0x21, sys.maxunicode + 1)
            if unicodedata.category(chr(cp))[0] in "PSZ"
            and chr(cp) not in separator_screen.BASE_SEPARATORS and unicodedata.category(chr(cp)) not in ("Pd", "Pc")]
    named = [c for c in "•·∙・~|:*#" if c in pool]
    zs = [c for c in pool if unicodedata.category(c)[0] == "Z"]                 # every Z* (all whitespace)
    return sorted(set(named + zs + pool[::40]))


SEPS = _separator_chars()
SHAPES = (("phone", ("555", "123", "4567")), ("card", ("4111", "1111", "1111", "1111")),
          ("ssn", ("123", "45", "6789")))


def test_the_generated_separator_set_covers_every_category_family():
    cats = {unicodedata.category(c)[0] for c in SEPS}
    assert cats == {"P", "S", "Z"} and len(SEPS) > 100


@pytest.mark.parametrize("shape,groups", SHAPES, ids=[s[0] for s in SHAPES])
def test_personal_numbers_with_any_unicode_separator_are_withheld(shape, groups):
    missed = []
    for sep in SEPS:
        for run in (1, 3):
            text = f"Call {(sep * run).join(groups)} today."
            if separator_screen.personal(text) is None:
                missed.append((hex(ord(sep)), run))
        if separator_screen.personal(f"Call {'x'.join(groups)} today.") is None:
            missed.append(("x", 1))
    assert not missed, missed[:10]


def test_a_separated_number_never_reaches_a_theo_envelope():
    for sep in ("•", "·", "∙", "・", "~", "|", ":", "*", "#", "x"):
        number = sep.join(("555", "123", "4567"))
        blob = _stored(f"call {number} for a refund")
        assert number not in blob and "4567" not in blob, sep


def test_separator_screen_keeps_the_base_allowances():
    for text in ("Founded 2019·2020.", "Over 1•000•000 orders.", "Plans • Pricing • FAQ",
                 "Version v2.1.3 ships today.", "Open 9:30 to 5:00."):
        assert separator_screen.personal(text) is None, text
