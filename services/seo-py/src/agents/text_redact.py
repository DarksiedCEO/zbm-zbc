"""
Credential redaction for engine-answer text before ANY of it is stored (ADR 0049, review rounds 9-10). Shared
plumbing, not an agent: a THIN wrapper over ``agents/secret_guard.py`` (PR #51), so there is one rule set, not two.

``redact(text)`` does, in order:
  1. normalises: zero-width / bidi characters removed and percent-decoding repeated until the text is stable (at most
     ``MAX_DECODE`` passes), so a fully or partly encoded key (``sk%2Dant%2D…``, ``%2573k-…``) is matched decoded;
  2. runs secret_guard's STRONG detectors (documented prefixes incl. Perplexity ``pplx-``, labelled credentials, JWT,
     the AWS secret format) on the normalised text: every match is replaced (``[REDACTED:secret_guard]``);
  3. runs the DELTA rules below, which secret_guard does not have yet. They are to be upstreamed into secret_guard
     (Callum's builder owns that file; it is not edited here), after which this module only does steps 1-2.
The STORED text is the normalised, redacted text.

Delta rules (each replaces its match — for the labelled rules only the value — with ``[REDACTED:<rule>]``):
  private_key          a PEM ``-----BEGIN … PRIVATE KEY-----`` block (to its END line; a longer body is consumed
                       in bounded steps)
  aws_access_key_id    AKIA / ASIA / AGPA / AIDA / AROA / AIPA / ANPA / ANVA / ABIA / ACCA + 16, with up to one
                       whitespace character between ANY two characters, the prefix included (``A K I A …``); a
                       spaced match must hold a digit (``ASIA PACIFIC REGION OFFICE`` is prose)
  aws_secret_key       a 40-character ``[A-Za-z0-9/+]`` run labelled with ``aws`` / ``secret`` / ``key`` within 40
                       characters before it (a trailing ``/`` allowed: a key in a URL path)
  stripe_webhook       ``whsec_`` + 10 or more
  gitlab_token         ``glpat-`` + 20 or more
  bearer_token         ``Bearer`` + whitespace + 16 or more token characters (no digit needed)
  labelled_credential  ANY label naming a credential — an identifier holding key / apikey / token / secret / password
                       / passwd / passphrase / pwd / auth / authorization / bearer / credential(s) / session id /
                       cookie (``access_token``, ``auth``, ``key``, ``api-key``, ``x-api-key``, ``client_secret`` …)
                       — then ``:`` or ``=`` and a value of 6+ characters that is token-shaped (a digit, a symbol
                       other than ``-`` / ``.``, mixed case at 10+, or 20+ characters): ``key: consistency`` is prose
  spaced_key           single characters separated by single whitespace (``s k - a n t - …``) whose collapsed form any
                       rule above (or secret_guard) matches
Every delta regex is bounded (no repetition above 4,096). Whitespace inside a key is caught only by
aws_access_key_id and spaced_key, as stated.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from agents import secret_guard

VERSION = "text-redact/2026-11-08.2"
MAX_DECODE = 6
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
_AWS_PREFIXES = ("AKIA", "ASIA", "AGPA", "AIDA", "AROA", "AIPA", "ANPA", "ANVA", "ABIA", "ACCA")
_AWS_ID = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(r"\s?".join(p) for p in _AWS_PREFIXES) + r")"
                     r"(?:\s?[A-Z0-9]){16}(?![A-Za-z0-9])")
_PEM_TAIL = re.compile(r"\[REDACTED:private_key\](?:[A-Za-z0-9+/=\r\n]{1,4096}|-----END[ A-Z]{0,40}PRIVATE KEY-----)")
_CRED_WORD = (r"(?:api[_\-]?key|apikey|key|token|secret|password|passwd|passphrase|pwd|auth|authorization|bearer"
              r"|credentials?|session[_\-]?id|cookie)")
_LABEL = (r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,16}[_\-.]){0,3}" + _CRED_WORD + r"(?:[_\-.][A-Za-z0-9]{1,16}){0,2}"
          r"(?![A-Za-z0-9])")
RULES = [
    ("private_key", re.compile(r"-----BEGIN[ A-Z]{0,40}PRIVATE KEY-----(?:[\s\S]{0,4096}?-----END"
                               r"[ A-Z]{0,40}PRIVATE KEY-----|[\s\S]{0,4096})")),
    ("aws_access_key_id", _AWS_ID),
    ("aws_secret_key", re.compile(r"(?i)(?<=aws|ret|key)(?:[^A-Za-z0-9/+]{0,40}?)([A-Za-z0-9/+]{40})"
                                  r"(?![A-Za-z0-9+])")),
    ("stripe_webhook", re.compile(r"whsec_[A-Za-z0-9]{10,255}")),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_\-]{20,64}")),
    ("bearer_token", re.compile(r"(?i)(?<![A-Za-z0-9])bearer\s{1,4}([A-Za-z0-9._~+/\-]{16,4096}={0,2})")),
    ("labelled_credential", re.compile(_LABEL + r"[\"']?\s{0,4}[:=]\s{0,4}[\"']?(?:(?i:basic|bearer|token|digest)"
                                       r"\s{1,4})?([^\s\"'<>&,;\[\]]{6,512})")),
]
_GROUP_RULES = ("aws_secret_key", "bearer_token", "labelled_credential")
_SPACED = re.compile(r"(?<!\S)(?:\S\s){7,300}\S(?!\S)")


def normalise(text: str) -> str:
    """Invisible characters removed and percent-decoding repeated until stable (bounded)."""
    t = _INVISIBLE.sub("", text or "")
    for _ in range(MAX_DECODE):
        d = _INVISIBLE.sub("", unquote(t)) if "%" in t else t
        if d == t:
            break
        t = d
    return t


def _token_shaped(v: str) -> bool:
    return any(c.isdigit() for c in v) or any(not c.isalnum() and c not in "-.'" for c in v) or len(v) >= 20 \
        or (len(v) >= 10 and any(c.isupper() for c in v) and any(c.islower() for c in v))


def _strong(text: str) -> list:
    return secret_guard._strong_spans(text)       # PR #51's strong detectors (a public text entry point is requested)


def _apply(t: str, fired: list) -> str:
    spans = sorted(_strong(t))
    if spans:
        merged: list = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        out, at = [], 0
        for a, b in merged:
            out += [t[at:a], "[REDACTED:secret_guard]"]
            at = b
        t = "".join(out + [t[at:]])
        fired.append("secret_guard")
    for name, rx in RULES:
        def repl(m, name=name):
            if name == "aws_access_key_id" and re.search(r"\s", m.group(0)) and not re.search(r"\d", m.group(0)):
                return m.group(0)                     # spaced capitals without a digit: prose
            if name in _GROUP_RULES:
                v = m.group(1)
                if v.startswith("[REDACTED") or (name == "labelled_credential" and not _token_shaped(v)):
                    return m.group(0)
                return m.group(0)[:m.start(1) - m.start(0)] + f"[REDACTED:{name}]" + m.group(0)[m.end(1) - m.start(0):]
            return f"[REDACTED:{name}]"
        new = rx.sub(repl, t)
        if new != t:
            fired.append(name)
            t = new
        if name == "private_key" and "private_key" in fired:
            for _ in range(len(t) // 4096 + 2):       # a body longer than one bounded match: eat the rest, in steps
                t, more = _PEM_TAIL.subn("[REDACTED:private_key]", t)
                if not more:
                    break
    return t


def _hit(collapsed: str) -> bool:
    probe: list = []
    return _apply(collapsed, probe) != collapsed


def redact(text: str) -> tuple:
    """(the normalised text with every credential replaced, the rule names that fired). Never raises on a str."""
    t = normalise(text)
    fired: list = []

    def spaced(m):
        collapsed = re.sub(r"\s", "", m.group(0))
        if _hit(normalise(collapsed)):
            fired.append("spaced_key")
            return "[REDACTED:spaced_key]"
        return m.group(0)
    t = _SPACED.sub(spaced, t)
    t = _apply(t, fired)
    return t, sorted(set(fired), key=fired.index)
