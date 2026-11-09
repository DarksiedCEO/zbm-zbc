"""Redaction of keys, tokens and personal data in provider text before any excerpt of it is stored or emitted (ADR 0048,
war room WR-F031). Local to seo-py: it consolidates with the shared ``secret_guard`` once #51 / #53 land.

``secrets(text)`` ALWAYS runs on the raw text (before folding, windowing or any other reading), with no exemption:

  documented key prefixes   AWS access key ids (AKIA / ASIA / AGPA / AIDA / AROA / ANPA + 16) and a 40-character
                            secret beside them, ``sk-`` / ``sk-proj-`` / ``sk-ant-``, ``ghp_`` / ``gho_`` / ``ghu_`` /
                            ``ghs_`` / ``ghr_`` / ``github_pat_``, ``sk_live_`` / ``sk_test_`` / ``rk_live_`` /
                            ``rk_test_`` / ``pk_live_``, Slack ``xox?-`` / ``xapp-``, Google ``AIza``, ``pplx-``
  labelled values           ``key=`` / ``token=`` / ``secret=`` / ``password=`` / ``api_key:`` ... (``=``: always;
                            ``:``: when the value looks like a token), ``Authorization:`` / ``Bearer`` values
  JWTs                      three base64url parts, the first two starting ``eyJ``
  long opaque tokens        32+ hex characters; 32+ letters-and-digits with both; 32+ base64 / base64url with
                            upper, lower and digits (an AWS secret access key among them)

``personal(text)`` redacts e-mail addresses, card numbers (Luhn-valid, 13-19 digits), US SSNs and phone numbers.
``redact(text)`` is both. Placeholders hold no digit and no key-shaped text, so redacting twice is the same as once.
Every pattern has bounded repetition only (no nested quantifier); linear in the text.
"""

from __future__ import annotations

import re

SECRET = "[redacted:secret]"
EMAIL = "[redacted:email]"
CARD = "[redacted:card]"
SSN = "[redacted:ssn]"
PHONE = "[redacted:phone]"

_B = r"(?<![A-Za-z0-9_])"            # a token boundary on the left

_PREFIXED = re.compile("|".join([
    r"(?<![A-Z0-9])(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[A-Z0-9]{16}",
    _B + r"sk-(?:ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{16,400}",
    _B + r"gh[pousr]_[A-Za-z0-9]{20,255}",
    _B + r"github_pat_[A-Za-z0-9_]{20,255}",
    _B + r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{8,255}",
    _B + r"rk_[A-Za-z0-9]{16,255}",
    _B + r"xox[abposr]-[A-Za-z0-9\-]{8,255}",
    _B + r"xapp-[A-Za-z0-9\-]{8,255}",
    _B + r"AIza[0-9A-Za-z_\-]{30,60}",
    _B + r"pplx-[A-Za-z0-9_\-]{16,255}",
    _B + r"eyJ[A-Za-z0-9_\-]{4,2000}\.(?:eyJ)?[A-Za-z0-9_\-]{4,4000}\.[A-Za-z0-9_\-]{4,2000}",
]))
# an AWS secret access key (40 base64 characters, redacted wherever it stands) and other long base64 tokens; never
# starting right after a dot, so a URL path in the text ("site.com/Claims/...") is left
_B64 = re.compile(r"(?<![A-Za-z0-9/+=.])[A-Za-z0-9/+]{32,4000}={0,2}(?![A-Za-z0-9/+=])")
_AUTH = re.compile(r"(?i)\b((?:proxy-)?authorization[\"']?\s{0,4}[:=]\s{0,4}[\"']?(?:(?:bearer|basic|token|digest|"
                   r"apikey)\s{1,4})?)([^\s\"',;]{4,4000})")
_BEARER = re.compile(r"(?i)\b(bearer\s{1,4})([A-Za-z0-9._~+/=\-]{8,4000})")
_LABEL = r"(?:[A-Za-z0-9]{0,20}[_.\-])?(?:api[_\-]?key|apikey|access[_\-]?key|secret[_\-]?key|private[_\-]?key|" \
         r"client[_\-]?secret|secret|token|access[_\-]?token|auth[_\-]?token|refresh[_\-]?token|session[_\-]?token|" \
         r"password|passwd|pwd|passphrase|credential|credentials|key)"
_LABELLED_EQ = re.compile(r"(?i)(?<![A-Za-z0-9])(" + _LABEL + r"[\"']?\s{0,4}=\s{0,4}[\"']?)([^\s\"'&,;<>]{1,4000})")
_LABELLED_COLON = re.compile(r"(?i)(?<![A-Za-z0-9])(" + _LABEL + r"[\"']?\s{0,4}:\s{0,4}[\"']?)([^\s\"'&,;<>]{8,4000})")
_HEX = re.compile(r"(?<![A-Za-z0-9])[0-9A-Fa-f]{32,4000}(?![A-Za-z0-9])")
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,4000}(?![A-Za-z0-9_\-])")

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,8}\.[A-Za-z]{2,24}")
_SSN = re.compile(r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)\d(?:[ \-]?\d){12,18}(?!\d)")
_PHONE = re.compile(r"(?<![\w+])(?:\+?\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)")


def _tokenish(v: str) -> bool:
    """A labelled ``:`` value that looks like a credential (prose such as ``Key: Refund Rangers`` is left)."""
    return len(v) >= 20 or (any(c.isdigit() for c in v) and any(c.isalpha() for c in v))


def _opaque(m: re.Match) -> str:
    v = m.group(0)
    digit = any(c.isdigit() for c in v)
    # a 32+ letters-and-digits piece anywhere in the run (``test-only-<hex>``, a token glued to the next word)
    if digit and any(len(p) >= 32 and any(c.isdigit() for c in p) and any(c.isalpha() for c in p)
                     for p in re.split(r"[_\-]", v)):
        return SECRET
    if digit and any(c.isupper() for c in v) and any(c.islower() for c in v):
        return SECRET
    return v


def _b64(m: re.Match) -> str:
    v = m.group(0).rstrip("=")
    kinds = sum((any(c.isupper() for c in v), any(c.islower() for c in v), any(c.isdigit() for c in v)))
    return SECRET if kinds == 3 or (len(v) == 40 and kinds == 2 and ("/" in v or "+" in v)) else m.group(0)


def secrets(text: str) -> str:
    """``text`` with every key, token and credential replaced by ``[redacted:secret]`` (module docstring)."""
    if not text:
        return text or ""
    t = _PREFIXED.sub(SECRET, text)
    t = _AUTH.sub(lambda m: m.group(1) + SECRET, t)
    t = _BEARER.sub(lambda m: m.group(1) + SECRET, t)
    t = _LABELLED_EQ.sub(lambda m: m.group(1) + SECRET, t)
    t = _LABELLED_COLON.sub(lambda m: m.group(1) + (SECRET if _tokenish(m.group(2)) else m.group(2)), t)
    t = _HEX.sub(SECRET, t)
    t = _B64.sub(_b64, t)
    t = _OPAQUE.sub(_opaque, t)
    return t


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _card(m: re.Match) -> str:
    d = re.sub(r"\D", "", m.group(0))
    return CARD if 13 <= len(d) <= 19 and _luhn(d) else m.group(0)


def personal(text: str) -> str:
    """``text`` with e-mail addresses, Luhn-valid card numbers, SSNs and phone numbers redacted."""
    if not text:
        return text or ""
    t = _EMAIL.sub(EMAIL, text)
    t = _CARD.sub(_card, t)
    t = _SSN.sub(SSN, t)
    return _PHONE.sub(PHONE, t)


def redact(text: str) -> str:
    return personal(secrets(text))
