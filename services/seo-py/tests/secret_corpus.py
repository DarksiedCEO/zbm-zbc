"""A deterministic corpus of SYNTHETIC strong-key and labelled-credential cases for the shared redaction core
(ADR 0050). Every value here is FAKE, assembled from fragments at run time with a seeded RNG, so no real credential
literal lives in the source and the secret scanner has nothing to find. The corpus covers every entry on the ADR's
closed threat list, placed in free text and in every URL part, and crossed with each obfuscation and pairs of them.

Each case is ``(name, carrier, secret, kind)``: ``carrier`` is the text or URL as it would appear, ``secret`` is the
raw value that must never survive, ``kind`` is ``"text"`` or ``"url"``."""

from __future__ import annotations

import base64
import random
import string
from urllib.parse import quote

_B62 = string.ascii_letters + string.digits
_HEX = "0123456789abcdef"
_EX = "EXAMPLE"


def _body(rng, n, alphabet=_B62):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _jwt(rng):
    def part(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")
    return ".".join(part(x) for x in (b'{"alg":"HS256"}', b'{"s":"' + _body(rng, 6).encode() + b'"}',
                                      _body(rng, 20).encode()))


def strong_keys(rng):
    """One fake value per strong-key family on the threat list (no-digit AWS secret included)."""
    def _secret40(with_digit):
        alpha = string.ascii_letters + ("0123456789" if with_digit else "") + "/+"
        while True:
            s = "".join(rng.choice(alpha) for _ in range(40))
            if any(c.islower() for c in s) and any(c.isupper() for c in s) \
                    and (not with_digit or any(c.isdigit() for c in s)) \
                    and (with_digit or not any(c.isdigit() for c in s)):
                return s
    no_digit_secret = _secret40(False)
    mixed_secret = _secret40(True)
    return {
        "aws_akia": "AKIA" + "IOSFODNN7" + _EX,
        "aws_asia": "ASIA" + _body(rng, 12, string.ascii_uppercase + string.digits) + "ABCD",
        "aws_aroa": "AROA" + "IOSFODNN7" + _EX,
        "aws_secret_mixed": mixed_secret,
        "aws_secret_nodigit_labelled": "aws_secret_access_key=" + no_digit_secret,
        "slack_bot": "xoxb-" + "11111111-" + _body(rng, 12),
        "slack_user": "xoxp-" + "22222222-" + _body(rng, 12),
        "slack_config": "xoxc-" + _body(rng, 14),
        "slack_refresh_dot": "xoxe.xoxp-" + "1-" + _body(rng, 14),
        "slack_refresh_num": "xoxe-1-" + _body(rng, 14),
        "github_pat_classic": "ghp_" + _body(rng, 36),
        "github_oauth": "gho_" + _body(rng, 36),
        "github_pat_fine": "github_pat_" + "11" + _body(rng, 40, _B62 + "_"),
        "openai_proj": "sk-proj-" + _EX + _body(rng, 24),
        "anthropic": "sk-ant-api03-" + _EX + _body(rng, 24),
        "perplexity": "pplx-" + _body(rng, 24),
        "stripe_live": "sk_live_" + _body(rng, 24),
        "stripe_whsec": "whsec_" + _body(rng, 28),
        "google_api": "AIza" + "Sy" + _body(rng, 33, _B62 + "-_"),
        "gitlab": "glpat-" + _body(rng, 22, _B62 + "-_"),
        "npm": "npm_" + _body(rng, 36),
        "huggingface": "hf_" + _body(rng, 34),
        "sendgrid": "SG." + _body(rng, 20, _B62 + "-_") + "." + _body(rng, 30, _B62 + "-_"),
        "jwt": _jwt(rng),
    }


_LABELS = ["password", "passcode", "passwd", "pwd", "PIN", "secret", "api_key", "apikey", "api-key", "x-api-key",
           "access_token", "refresh_token", "client_secret", "auth", "authorization", "token", "session_id",
           "api key", "access code", "contraseña", "mot de passe", "schlüssel", "senha", "clé"]
_SEPS = [": ", "=", " = ", ":", " -> ", " → ", " is ", " - ", '": "']


def labelled(rng):
    """Labelled-credential cases: every label x a spread of separators, a >=6 value (some no-digit, some short)."""
    out = []
    values = [_EX + _body(rng, 6), "hunter2" + _body(rng, 4), "Consistency", _body(rng, 8, string.ascii_lowercase),
              _body(rng, 9, string.ascii_lowercase), "s3cr3t-" + _body(rng, 6)]
    for lab in _LABELS:
        sep = rng.choice(_SEPS)
        val = rng.choice(values)
        close = '"' if sep == '": "' else ""
        text = f"config {lab}{sep}{val}{close} applied."
        out.append(("labelled_" + lab.replace(" ", "_"), text, val, "text"))
    return out


def _obfuscate(secret, how, rng):
    """Return an obfuscated rendering of ``secret`` (text form) for the given technique."""
    if how == "plain":
        return secret
    if how == "glued":
        return "internalconfig" + secret + "END"
    if how == "fullwidth":
        return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in secret)
    if how == "invisible":
        return "​".join(secret)
    if how == "linebreak":
        mid = len(secret) // 2
        return secret[:mid] + "\n" + secret[mid:]
    if how == "pct1":
        return quote(secret, safe="")
    if how == "pct2":
        return quote(quote(secret, safe=""), safe="")
    if how == "glued_fullwidth":
        return "pre" + "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in secret)
    return secret


_TEXT_OBF = ["plain", "glued", "fullwidth", "invisible", "linebreak", "pct1", "pct2", "glued_fullwidth"]
_URL_PARTS = ["path", "query", "fragment", "userinfo", "hostlabel", "deep_path"]


def _url(secret, part, rng):
    q = quote(secret, safe="")
    if part == "path":
        return f"https://site.test/a/{q}/b"
    if part == "query":
        return f"https://site.test/p?x=1&t={q}"
    if part == "fragment":
        return f"https://site.test/p#tok={q}"
    if part == "userinfo":
        return f"https://user:{q}@site.test/p"
    if part == "hostlabel":
        # the authority preserves case; a key as a subdomain label must still be swept out of the stored string.
        # Only DNS-label-safe characters can sit in a host label, so other keys fall back to the path.
        if all(c.isalnum() or c == "-" for c in secret):
            return f"https://{secret}.site.test/p"
        return f"https://site.test/host/{q}"
    if part == "deep_path":
        return f"https://docs.google.com/d/{q}/edit?v=1#{q}"
    return f"https://site.test/{q}"


def corpus(seed=5050):
    """>= 1,000 cases: every strong-key family x text obfuscations x a pair; every family x URL parts; labelled creds."""
    rng = random.Random(seed)
    cases = []
    keys = strong_keys(rng)
    for name, secret in keys.items():
        # a bare, shapeless 40-char AWS secret (no prefix, no label) glued INTO the middle of a word with no delimiter
        # is indistinguishable from random base64 and is documented residual risk (ADR 0050), not a covered case.
        obf = _TEXT_OBF if name != "aws_secret_mixed" else [o for o in _TEXT_OBF if "glued" not in o]
        for how in obf:
            cases.append((f"{name}:{how}", f"Note: {_obfuscate(secret, how, rng)} done.", secret, "text"))
        for part in _URL_PARTS:
            cases.append((f"{name}:url:{part}", _url(secret, part, rng), secret, "url"))
    # a few more seeds for volume and variety (keeps the corpus well over 1,000 cases)
    for s in range(1, 28):
        r2 = random.Random(seed + s)
        for name, secret in strong_keys(r2).items():
            cases.append((f"{name}:v{s}", f"value {secret} end", secret, "text"))
    cases += labelled(rng)
    return cases
