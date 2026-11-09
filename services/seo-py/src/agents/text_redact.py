"""
Credential redaction for engine-answer text before ANY of it is stored (ADR 0049, review round 9). Shared plumbing,
not an agent. Theo keeps a bounded excerpt of each answer as drill-down evidence; an answer can quote a key a page
leaked, so the RAW answer text goes through ``redact`` first, every rule always, no exemptions, before the excerpt is
cut. (To be consolidated with ``agents/secret_guard.py`` once PR #51's rework of that module lands; it is not edited
here.)

Rules (each replaces its match, or for the two labelled rules only the value after the label, with
``[REDACTED:<rule>]``):
  aws_access_key_id   AKIA / ASIA / AGPA / AIDA / AROA / AIPA / ANPA / ANVA / ABIA / ACCA + 16 upper-case letters or
                      digits, also when single whitespace characters are put between its characters
  aws_secret_key      a 40-character base64-like run labelled with ``aws``/``secret``/``key`` within 40
                      characters before it
                      (a trailing ``/`` is allowed, so a key inside a URL path is caught)
  jwt                 ``eyJ<header>.<payload>.<signature>`` (three base64url parts)
  anthropic_key       ``sk-ant-`` + 10 or more key characters
  openai_key          ``sk-`` / ``sk-proj-`` + 20 or more key characters
  github_token        ``ghp_ gho_ ghu_ ghs_ ghr_`` + 20 or more, ``github_pat_`` + 20 or more
  stripe_key          ``sk_live_ sk_test_ rk_live_ rk_test_ pk_live_ whsec_`` + 10 or more
  slack_token         ``xoxb- xoxa- xoxp- xoxr- xoxs-`` + 10 or more
  google_api_key      ``AIza`` + 35
  gitlab_token        ``glpat-`` + 20
  private_key         a PEM ``-----BEGIN … PRIVATE KEY-----`` block (to its END line; a longer body is consumed
                      in bounded steps)
  bearer_token        ``Bearer`` + 16 or more token characters
  labelled_secret     a value after a credential label (password, passwd, pwd, secret, api key, access key,
                      token, auth token, client secret, private key, credentials) and ``:`` or ``=``
Zero-width and bidi-control characters are removed from the text first, so a key split by them is caught by every
rule. Whitespace inside a key is caught only by the AWS access-key-id rule (as stated above); other keys split by
spaces are NOT claimed to be caught. Lexical, and bounded: every repetition has an upper bound.
"""

from __future__ import annotations

import re

VERSION = "text-redact/2026-11-08.1"
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
_KEYCH = r"[A-Za-z0-9_\-]"
_PEM_TAIL = re.compile(r"\[REDACTED:private_key\](?:[A-Za-z0-9+/=\r\n]{1,4096}|-----END[ A-Z]{0,40}PRIVATE KEY-----)")
RULES = [
    ("private_key", re.compile(r"-----BEGIN[ A-Z]{0,40}PRIVATE KEY-----(?:[\s\S]{0,4096}?-----END"
                               r"[ A-Z]{0,40}PRIVATE KEY-----|[\s\S]{0,4096})")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{5,4096}\.[A-Za-z0-9_\-]{2,4096}\.[A-Za-z0-9_\-]{2,4096}")),
    ("anthropic_key", re.compile(r"sk-ant-" + _KEYCH + r"{10,512}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?" + _KEYCH + r"{20,512}")),
    ("github_token", re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})")),
    ("stripe_key", re.compile(r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,255}|whsec_[A-Za-z0-9]{10,255}")),
    ("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,255}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_\-]{20,64}")),
    ("aws_access_key_id", re.compile(r"(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ABIA|ACCA)(?:\s?[A-Z0-9]){16}"
                                     r"(?![A-Z0-9])")),
    ("aws_secret_key", re.compile(r"(?i)(?<=aws|ret|key)(?:[^A-Za-z0-9/+]{0,40}?)([A-Za-z0-9/+]{40})"
                                  r"(?![A-Za-z0-9+])")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s{1,4}[A-Za-z0-9._~+/\-]{16,4096}={0,2}")),
    ("labelled_secret", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[ _\-]?key|access[ _\-]?key|"
                                   r"auth[ _\-]?token|token|client[ _\-]?secret|private[ _\-]?key|credentials?)"
                                   r"[\"']?\s{0,4}[:=]\s{0,4}[\"']?([^\s\"'<>]{6,512})")),
]


def redact(text: str) -> tuple:
    """(the text with every credential replaced, the rule names that fired). Never raises on a str."""
    t = _INVISIBLE.sub("", text or "")
    fired = []
    for name, rx in RULES:
        if name in ("aws_secret_key", "labelled_secret"):
            def repl(m, name=name):
                return m.group(0)[:m.start(1) - m.start(0)] + f"[REDACTED:{name}]"
        else:
            def repl(m, name=name):
                return f"[REDACTED:{name}]"
        t, n = rx.subn(repl, t)
        if name == "private_key" and n:          # a body longer than one bounded match: eat the rest, in steps
            for _ in range(len(t) // 4096 + 2):
                t, more = _PEM_TAIL.subn("[REDACTED:private_key]", t)
                if not more:
                    break
        if n:
            fired.append(name)
    return t, fired
