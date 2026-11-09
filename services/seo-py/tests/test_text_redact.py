"""Credential redaction before Theo stores any answer text (ADR 0049, review round 9; agents/text_redact.py).

Property test: every fake key x every position (start / middle / end, inside a URL query and path, quoted, in a
code span, split by zero-width characters, and — only for the AWS access-key-id rule, which claims it — split by
whitespace) is never stored in a Theo envelope. Every key below is FAKE and built from parts at import time, so no
literal credential is committed (and the secret scanner has nothing to find)."""

from __future__ import annotations

import json

import pytest

from agents import text_redact, theo
from test_theo import CTX, PS, Clock, NativePort, Port, ans, topics

KEYS = {
    "aws_access_key_id": "AK" + "IA" + "IOSFODNN7" + "FAKEKEY"[:7],
    "jwt": "ey" + "JhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJ4In0" + "." + "c2lnbmF0dXJl",
    "anthropic_key": "sk-" + "ant-" + "api03-" + "x" * 40,
    "openai_key": "sk-" + "proj-" + "Z" * 40,
    "github_token": "gh" + "p_" + "B" * 36,
    "github_pat": "github" + "_pat_" + "C" * 40,
    "stripe_key": "sk" + "_live_" + "D" * 24,
    "stripe_test": "rk" + "_test_" + "E" * 24,
    "slack_token": "xo" + "xb-" + "1234567890-" + "F" * 12,
    "google_api_key": "AI" + "za" + "G" * 35,
    "gitlab_token": "gl" + "pat-" + "H" * 20,
    "bearer": "Bearer " + "tok" + "I" * 30,
    "labelled_password": "password" + "=" + "hunter2" + "J" * 6,
    "aws_secret": "aws_secret_access_key = " + "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "K" * 10,
}
INVISIBLE = "​"


def placements(key: str, name: str):
    yield "start", f"{key} is what the page shows."
    yield "middle", f"The docs mention {key} in passing, then move on."
    yield "end", f"The page ends with {key}"
    yield "url_query", f"See https://docs.example.test/page?key={key}&x=1 for details."
    yield "url_path", f"See https://docs.example.test/{key}/readme."
    yield "double_quoted", f'The config says "{key}".'
    yield "single_quoted", f"The config says '{key}'."
    yield "code_span", f"Run `{key}` to start."
    if name not in ("bearer", "labelled_password", "aws_secret"):        # values after a label keep their label
        yield "zero_width_split", "Key: " + INVISIBLE.join(key) + " here."
    if name == "aws_access_key_id":                                       # the one rule that claims it
        yield "whitespace_split", "Key " + " ".join(key[i:i + 4] for i in range(0, len(key), 4)) + " here."


def secret_part(name: str, key: str) -> str:
    """What must never be stored: the credential itself (for labelled forms, the value after the label)."""
    if name == "bearer":
        return key.split(" ", 1)[1]
    if name == "labelled_password":
        return key.split("=", 1)[1]
    if name == "aws_secret":
        return key.split("= ", 1)[1]
    return key


CASES = [(n, k, where, text) for n, k in KEYS.items() for where, text in placements(k, n)]


@pytest.mark.parametrize("name,key,where,text", CASES, ids=[f"{c[0]}-{c[2]}" for c in CASES])
def test_a_credential_is_never_stored_wherever_it_sits(name, key, where, text):
    secret = secret_part(name, key)
    clean, fired = text_redact.redact(text)
    assert secret not in clean and secret.replace(" ", "") not in clean.replace(" ", ""), (where, clean)
    assert fired
    # through Theo's store point: the excerpt in the envelope rows
    t, plans = topics()
    a1 = ans("Z Best Media: " + text)
    port = Port({1: a1, 2: ans("RivalCo too."), 3: ans("I recommend Z Best Media.")})
    res = theo.run_chains(t, plans, {"openai": port},
                          ["openai"], 1, lambda **k: None, None, theo.CallCap(100), CTX, Clock())
    env = theo.envelope_for("OK", res, theo.output("zbm", "seo-cnv-" + "0" * 40, t, plans, res, 1), t, plans)
    blob = json.dumps(env, ensure_ascii=False)
    for frag in (secret, secret.replace(" ", ""), secret[:16]):
        assert frag not in blob.replace(INVISIBLE, ""), (where, frag)


def test_native_chains_never_store_a_credential_either():
    key = KEYS["anthropic_key"]
    t, plans = topics()
    p = NativePort({1: ans(f"Z Best Media docs leak {key} here."), 2: ans("RivalCo too."),
                    3: ans("I recommend Z Best Media.")})
    res = theo.run_chains(t, plans, {"openai": p}, ["openai"], 1, lambda **k: None, None, theo.CallCap(100), CTX,
                          Clock())
    row = [r for r in res["rows"] if r["turn"] == 1][0]
    assert key not in json.dumps(res) and row["evidence"]["redacted"] == ["anthropic_key"]


def test_ordinary_text_is_left_alone():
    for text in ("Z Best Media recovers reimbursements.", "Ask for the token count in your invoice.",
                 "The password reset page is at /reset.", "Skip-level managers approve refunds."):
        clean, fired = text_redact.redact(text)
        assert clean == text and fired == [], text


def test_every_redaction_regex_is_bounded():
    from test_theo import _bad_repetition
    for name, rx in text_redact.RULES:
        bad = [b for b in _bad_repetition(rx) if b[0] == "unbounded"]
        assert not bad, (name, bad)


def test_the_fixture_brand_is_the_one_the_excerpt_keeps():
    assert PS["brand_terms"] == ["Z Best Media"]


def test_a_private_key_longer_than_one_bounded_match_is_still_removed_whole():
    body = ("Q" * 64 + "\n") * 150                                    # ~9,750 chars, beyond one 4,096 match
    pem = "-----BEGIN RSA " + "PRIVATE KEY-----\n" + body + "-----END RSA " + "PRIVATE KEY-----"
    clean, fired = text_redact.redact("before " + pem + " after")
    assert clean == "before [REDACTED:private_key] after" and fired == ["private_key"]
    assert not [b for b in __import__("test_theo")._bad_repetition(text_redact._PEM_TAIL) if b[0] == "unbounded"]
