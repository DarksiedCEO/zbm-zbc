"""Redaction of keys, tokens and personal data in provider text before any of it is stored, logged or emitted (ADR 0048,
war room WR-F031; review of 0110a226 M1 / M3 / M4 / S3). Local to seo-py: it consolidates with the shared
``secret_guard`` once #51 / #53 land.

Every entry point first NORMALISES the text (``normalise``: NFKC, then every format character — zero-width, bidi
controls, soft hyphen — and every default-ignorable code point removed), so a key written in full-width letters or
with invisible characters inside is read as the key. Spans are collected over the normalised text by every rule and
merged, then replaced, so the rules never see each other's placeholders.

``secrets(text)`` — STRONG rules (never exempted):

  documented key prefixes   AWS access key ids (AKIA / ASIA / AGPA / AIDA / AROA / AIPA / ANPA / ANVA / APKA / ABIA /
                            ACCA + 16, matched even glued to a word), ``sk-`` / ``sk-proj-`` / ``sk-ant-...``,
                            ``gh?_`` / ``github_pat_``, Stripe ``sk|rk|pk_live|test_`` / ``whsec_``, Slack ``xox?-`` /
                            ``xoxe.xox?-`` / ``xoxe-`` / ``xapp-``, Google ``AIza`` / ``ya29.`` / ``GOCSPX-``, ``pplx-``,
                            ``glpat-``, ``npm_``, ``hf_``, SendGrid ``SG.``, Shopify ``shp??_``, DigitalOcean ``dop_v1_``,
                            ``xai-``, ``gsk_``, ``r8_``, Telegram bot tokens, JWTs, PEM private-key blocks. A prefix
                            glued to a letter or digit counts when the rest looks random (a digit or an upper-case
                            letter in it), so ``task-force-...`` prose is left.
  labelled credentials      a credential LABEL (password / passwd / pwd / passcode / passphrase / PIN / access code /
                            auth / api key / token / secret / client secret / key ... and common non-English labels:
                            contraseña, mot de passe, passwort, senha, wachtwoord, пароль, 密码, パスワード, 비밀번호 ...;
                            ``_LABELS``), then ANY separator (``:`` ``=`` ``->`` ``→`` `` is `` `` - `` or whitespace,
                            up to three in a row), then the value: 6+ characters, or 3+ with a digit, is redacted. No
                            "looks like a token" gate. A password-family label with an explicit separator redacts the
                            value up to the end of its line or sentence (a passphrase has spaces); a quoted value is
                            redacted to its closing quote. Bare ``key`` counts with ``:`` / ``=`` only.
  URL userinfo              ``scheme://user:password@host`` and ``user:password@host``: the user part is redacted
  Authorization / Bearer    the header value / the bearer token
  encoded                   a word that holds ``%xx`` or an HTML entity is decoded (up to ``DECODE_ROUNDS`` rounds);
                            a strong secret in any decoding redacts the whole word
  broken / spaced           a key broken over lines (the break and indentation removed) or written with a space
                            between every character is matched as one token

WEAK heuristics (``secrets`` too; exemptions apply ONLY to these): 32+ hex characters; 32+ letters-and-digits with
both; 32+ base64 / base64url with upper, lower and digits (an AWS secret access key among them, 40 characters with
``/`` or ``+``). Exempt (S3): runs made only of words and short numbers (blog slugs, CamelCase hashtags, paths,
``How-To-Claim-FBA-Reimbursements-2026``), and a 40- or 64-character lower-case hex commit id in a URL on a code host
or after ``/commit/`` / ``/tree/`` / ``/blob/`` ... (``_code_sha``).

``personal(text)`` redacts e-mail addresses (also ``name [at] host [dot] com`` and percent-encoded forms), card
numbers (Luhn-valid, 13-19 digits; an ISBN-13, a UUID group or an Amazon order id ``123-1234567-1234567`` is not a
card), US SSNs (separated, or 9 digits after an SSN label) and phone numbers (with separators, parentheses or a
leading ``+``, or 10+ digits after a phone label: a plain 10-digit number is left). ``redact(text)`` is both.
Placeholders hold no digit and no key-shaped text, so redacting twice is the same as once.

``strong_secret`` / ``personal_hit`` / ``sensitive`` answer yes / no on the text and each of its decodings (the final
guard, ``scrub``). ``scrub(obj)`` redacts every free-text string (and dict key) of a record before it is recorded,
then re-checks every field and replaces any field a detector still fires on with ``[redacted:withheld]`` (M4: fail
closed). Every pattern has bounded repetition only (no nested unbounded quantifier); linear in the text.
"""

from __future__ import annotations

import functools
import hashlib
import html
import re
import unicodedata
from urllib.parse import unquote

import lookalikes

SECRET = "[redacted:secret]"
EMAIL = "[redacted:email]"
CARD = "[redacted:card]"
SSN = "[redacted:ssn]"
PHONE = "[redacted:phone]"
WITHHELD = "[redacted:withheld]"
PLACEHOLDERS = (SECRET, EMAIL, CARD, SSN, PHONE, WITHHELD)
DECODE_ROUNDS = 4


def normalise(text: str) -> str:
    """NFKC, then format characters (zero-width, bidi controls, soft hyphen, ...) and default-ignorables removed."""
    if not text:
        return text or ""
    t = unicodedata.normalize("NFKC", lookalikes.strip_invisible(text))
    return lookalikes.strip_invisible(t)


def decodings(text: str) -> list:
    """``text``, then each round of percent- and HTML-entity decoding until stable (at most ``DECODE_ROUNDS``)."""
    out, cur = [text], text
    for _ in range(DECODE_ROUNDS):
        nxt = html.unescape(unquote(cur)) if ("%" in cur or "&" in cur) else cur
        if nxt == cur:
            break
        out.append(nxt)
        cur = nxt
    return out


def decode(text: str) -> str:
    """The last of ``decodings`` (bounded): what a reader of the decoded text sees."""
    return decodings(text or "")[-1]


# ---------------------------------------------------------------------------------------------- strong: prefixes

# (regex, glued_needs_random): when the match is glued to a letter or digit on its left, a prefix whose start is a
# short or common letter sequence counts only when the body looks random (a digit or an upper-case letter in it)
_PREFIXES = (
    (r"(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA|ABIA|ACCA)[A-Z0-9]{16,64}", False),
    (r"sk-(?:ant-(?:api|admin|sid)\d\d-|ant-|proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{16,400}", True),
    (r"gh[pousr]_[A-Za-z0-9]{20,255}", True),
    (r"github_pat_[A-Za-z0-9_]{20,255}", False),
    (r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{8,255}", False),
    (r"whsec_[A-Za-z0-9]{20,255}", False),
    (r"rk_[A-Za-z0-9]{16,255}", True),
    (r"xoxe\.xox[a-z]-[A-Za-z0-9\-]{8,255}", False),
    (r"xox[a-z]-[A-Za-z0-9\-]{8,255}", False),
    (r"xapp-[A-Za-z0-9\-]{8,255}", False),
    (r"AIza[0-9A-Za-z_\-]{30,60}", False),
    (r"ya29\.[0-9A-Za-z_\-]{20,2000}", False),
    (r"GOCSPX-[0-9A-Za-z_\-]{16,255}", False),
    (r"pplx-[A-Za-z0-9_\-]{16,255}", False),
    (r"glpat-[A-Za-z0-9_\-]{20,255}", False),
    (r"npm_[A-Za-z0-9]{30,255}", True),
    (r"hf_[A-Za-z0-9]{30,255}", True),
    (r"SG\.[A-Za-z0-9_\-]{16,255}\.[A-Za-z0-9_\-]{16,255}", False),
    (r"shp(?:at|ss|ca|pa)_[A-Fa-f0-9]{32,64}", False),
    (r"dop_v1_[a-f0-9]{64}", False),
    (r"xai-[A-Za-z0-9]{20,255}", True),
    (r"gsk_[A-Za-z0-9]{20,255}", True),
    (r"r8_[A-Za-z0-9]{20,255}", True),
    (r"\d{8,10}:AA[A-Za-z0-9_\-]{30,40}", True),
    (r"eyJ[A-Za-z0-9_\-]{4,2000}\.(?:eyJ)?[A-Za-z0-9_\-]{4,4000}\.[A-Za-z0-9_\-]{4,2000}", False),
)
_PREFIX = re.compile("|".join(f"(?P<p{i}>{rx})" for i, (rx, _g) in enumerate(_PREFIXES)))
_PEM = re.compile(r"-----BEGIN (?:[A-Z0-9]{1,20} ){0,4}PRIVATE KEY-----(?:[\s\S]{0,20000}?-----END (?:[A-Z0-9]{1,20} )"
                  r"{0,4}PRIVATE KEY-----|[A-Za-z0-9+/=\s]{0,20000})")
_AWS_SECRET = re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])")


def _randomish(body: str) -> bool:
    return any(c.isdigit() for c in body) or any(c.isupper() for c in body)


def _prefix_spans(t: str) -> list:
    out = []
    for m in _PREFIX.finditer(t):
        v = m.group(0)
        if v.startswith("sk-") and not _randomish(v[3:]):
            continue                                   # ``sk-ii-facial-essence``: a slug, not a key
        glued = m.start() > 0 and t[m.start() - 1].isalnum()
        if glued and _PREFIXES[int(m.lastgroup[1:])][1] and not (
                any(c.isdigit() for c in v[3:]) and any(c.isalpha() for c in v[3:])):
            continue                                   # ``task-force-...``: prose glued to a short prefix
        out.append(m.span())
    return out


# ---------------------------------------------------------------------------------------------- strong: labelled

_PASSWORD_LABELS = (
    r"pass(?:word|wd|code|phrase|w)s?|pwd|pass[ \-]word|"
    r"contrase(?:ñ|n)as?|clave(?: de acceso)?|mot[ \-]de[ \-]passe|motdepasse|mdp|passwort|kennwort|senhas?|"
    r"palavra[ \-]passe|wachtwoord|l(?:ö|o)senord|salasana|has(?:ł|l)o|parol(?:a|ă)|heslo|jelsz(?:ó|o)|(?:ş|s)ifre|"
    r"adgangskode|passord|lozinka|geslo|lösen|пароль|密码|密碼|口令|パスワード|暗証番号|비밀번호|암호|كلمة المرور|كلمة السر|"
    r"סיסמה|סיסמא|m(?:ậ|a)t kh(?:ẩ|a)u|kata sandi|kata laluan|รหัสผ่าน|पासवर्ड|κωδικός(?: πρόσβασης)?")
_OTHER_LABELS = (
    r"pin[ \-]?(?:code|number|no)|access[ \-]?codes?|security[ \-]?code|verification[ \-]?code|one[ \-]time[ \-]code|"
    r"otp|2fa[ \-]?code|recovery[ \-]?codes?|backup[ \-]?codes?|licen[cs]e[ \-]?key|serial[ \-]?key|"
    r"api[ _\-]?(?:keys?|secrets?|tokens?)|apikeys?|x-api-key|x-goog-api-key|access[ _\-]?(?:keys?|tokens?)|"
    r"secret[ _\-]?(?:keys?|access[ _\-]?key)|private[ _\-]?keys?|client[ _\-]?secrets?|"
    r"(?:auth|bearer|refresh|session|id|oauth|csrf|xsrf)[ _\-]?tokens?|session[ _\-]?(?:ids?|keys?|cookies?)|"
    r"credentials?|auth|tokens?|secrets?|"
    r"c(?:ó|o)digo de acceso|code d'acc(?:è|e)s|token de acceso|jetons?|chave(?: de)? api|cl(?:é|e)(?: d')? ?api|"
    r"zugangscode|pin-code|código pin|codigo pin|ключ api|токен|令牌|トークン|토큰")
# a composite name around the label (``OPENAI_API_KEY``, ``db.password``, ``x_auth_token_value``)
_PRE = r"(?:[A-Za-z0-9]{1,24}[_.\-]){0,4}"
_POST = r"(?:[_.\-][A-Za-z0-9]{1,24}){0,2}"
_SEP = (r"(?:\s{0,4}(?:[:=]{1,2}|=>|-->|->|→|⇒|⟶|➜|➔|➡)\s{0,4}|\s{1,4}[\-\u2013\u2014]{1,2}\s{0,4}|[\u2013\u2014]\s{0,4}"
        r"|\s{1,4}(?:is|was|are|equals|reads|set to)(?![A-Za-z])\s{0,4}|\s{1,4}){1,3}")
_Q = r"[\"'\u201c\u201d\u2018\u2019«»`]"
_VALUE = r"(?!\[redacted:)(?:(?P<qv>" + _Q + r")(?P<qval>[^\"'\u201c\u201d\u2018\u2019«»`\n]{3,200})" + _Q + \
         r"|" + _Q + r"?(?P<val>[^\s\"'\u201c\u201d\u2018\u2019«»`<>]{3,4000}))"
_LABELLED = re.compile(r"(?i)(?<![A-Za-z0-9])(?P<label>" + _PRE + r"(?:(?P<pw>" + _PASSWORD_LABELS + r")|" + _OTHER_LABELS
                       + r")" + _POST + r")" + _Q + r"?(?P<sep>" + _SEP + r")" + _VALUE)
# ``PIN`` in capitals (``pin`` is an ordinary verb), ``key`` with an explicit ``:`` / ``=``
_PIN = re.compile(r"(?<![A-Za-z0-9])(?P<label>PINs?|Pin)" + _Q + r"?(?P<sep>" + _SEP + r")" + _VALUE)
_KEY = re.compile(r"(?i)(?<![A-Za-z0-9])(?P<label>" + _PRE + r"keys?" + _POST + r")" + _Q +
                  r"?(?P<sep>\s{0,4}[:=]{1,2}\s{0,4})" + _VALUE)
_EXPLICIT = re.compile(r"[:=→⇒⟶➜➔➡\-\u2013\u2014]|\b(?:is|was|are|equals|reads|set to)\b")
_CLAUSE_END = re.compile(r"\n|[.!?;](?:\s|$)")
_AUTH = re.compile(r"(?i)\b((?:proxy-)?authorization[\"']?\s{0,4}[:=]\s{0,4}[\"']?(?:(?:bearer|basic|token|digest|"
                   r"apikey)\s{1,4})?)([^\s\"',;]{4,4000})")
_BEARER = re.compile(r"(?i)\b(bearer\s{1,4})([A-Za-z0-9._~+/=\-]{8,4000})")
_USERINFO = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.\-]{1,20}:)?//([^\s/?#@\[\]]{1,256})@(?=[^\s@])")
_USERPASS = re.compile(r"(?<![\w.%+\-/:@])([^\s/:@\[\]]{1,64}:[^\s/@\[\]]{1,128})@(?=[A-Za-z0-9])")


def _value_ok(v: str) -> bool:
    return len(v) >= 6 or (len(v) >= 3 and any(c.isdigit() for c in v))


def _labelled_spans(t: str) -> list:
    out = []
    for rx in (_LABELLED, _PIN, _KEY):
        for m in rx.finditer(t):
            if m.group("qval") is not None:
                v, (a, b) = m.group("qval"), m.span("qval")
                if not _value_ok(v.strip()):
                    continue
                out.append((a, b))
                continue
            v, (a, b) = m.group("val"), m.span("val")
            if not _value_ok(v.rstrip(".,;:!?)]}")):
                continue
            explicit = bool(_EXPLICIT.search(m.group("sep")))
            if rx is _LABELLED and m.group("pw") is not None and explicit:
                # a passphrase has spaces: the value runs to the end of its line or sentence (200 characters at most)
                e = _CLAUSE_END.search(t, max(a, b - 1), max(b, min(len(t), a + 200)))
                b = max(b, e.start() if e is not None else min(len(t), a + 200))
            out.append((a, b))
    for m in _AUTH.finditer(t):
        out.append(m.span(2))
    for m in _BEARER.finditer(t):
        out.append(m.span(2))
    for rx in (_USERINFO, _USERPASS):
        for m in rx.finditer(t):
            out.append(m.span(1))
    return out


# ---------------------------------------------------------------------------------------------- weak heuristics

_HEX = re.compile(r"(?<![A-Za-z0-9])[0-9A-Fa-f]{32,4000}(?![A-Za-z0-9])")
_B64 = re.compile(r"(?<![A-Za-z0-9/+.])[A-Za-z0-9/+]{32,4000}={0,2}(?![A-Za-z0-9/+=])")
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,4000}(?![A-Za-z0-9_\-])")
_PIECES = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_VOWEL = re.compile(r"[aeiouyAEIOUY]")
CODE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "sourceforge.net", "googlesource.com",
              "gitee.com", "sr.ht", "kernel.org", "gitea.com", "sourcehut.org", "huggingface.co")
_CODE_REF = re.compile(r"/(?:commit|commits|tree|blob|raw|compare|blame|archive|rev|revision|changeset|src|-)/$")
_URL_START = re.compile(r"(?i)(?:[a-z][a-z0-9+.\-]{1,20}://|www\.)([^\s/?#]{1,253})")


_CAMEL = re.compile(r"[a-z][A-Z]")
_CONSONANTS = re.compile(r"[b-df-hj-np-tv-xzB-DF-HJ-NP-TV-XZ]{6,}")


def _wordy(v: str) -> bool:
    """A run made only of words and short numbers (S3): a slug, a hashtag, a path, CamelCase — not a random token.
    It has STRUCTURE (``-`` / ``_`` / ``/`` / ``.`` between parts, or CamelCase), every letter piece is word-like
    (``_pronounceable``), the letter pieces average 4+ letters, and it holds at most three numbers of at most four
    digits. A random token almost never passes all of these (measured in the tests)."""
    if not (re.search(r"[\-_/.]", v) or len(_CAMEL.findall(v)) >= 2):
        return False
    pieces = _PIECES.findall(v)
    letters = [p for p in pieces if not p.isdigit()]
    digits = [p for p in pieces if p.isdigit()]
    if len(letters) < 2 or len(digits) > 3 or any(len(p) > 4 for p in digits):
        return False
    if sum(len(p) for p in letters) < 4 * len(letters):
        return False
    return all(_pronounceable(p) for p in letters) and sum(len(p) for p in pieces) * 10 >= len(v) * 8


def _pronounceable(p: str) -> bool:
    """A word-like piece: up to three letters, an all-capitals acronym of up to five, or one vowel in five letters
    and no run of six consonants."""
    if len(p) <= 3 or (p.isupper() and len(p) <= 5):
        return True
    return len(_VOWEL.findall(p)) * 5 >= len(p) and _CONSONANTS.search(p) is None


def _code_sha(t: str, a: int, b: int) -> bool:
    """A 40- / 64-character lower-case hex commit id in a URL on a code host or after ``/commit/`` and the like."""
    v = t[a:b]
    if len(v) not in (40, 64) or v != v.lower():
        return False
    lo = max(t.rfind(" ", 0, a), t.rfind("\n", 0, a), t.rfind("\t", 0, a)) + 1
    url = t[lo:a]
    if _CODE_REF.search(url) is not None:
        return True
    m = _URL_START.search(url)
    if m is None:
        return False
    host = m.group(1).lower().rsplit("@", 1)[-1].split(":", 1)[0]
    return any(host == h or host.endswith("." + h) for h in CODE_HOSTS)


def _weak_spans(t: str) -> list:
    out = []
    for m in _HEX.finditer(t):
        if not _code_sha(t, *m.span()):
            out.append(m.span())
    for m in _B64.finditer(t):
        v = m.group(0).rstrip("=")
        kinds = sum((any(c.isupper() for c in v), any(c.islower() for c in v), any(c.isdigit() for c in v)))
        if (kinds == 3 or (len(v) == 40 and kinds == 2 and ("/" in v or "+" in v))) and not _wordy(v):
            out.append(m.span())
    for m in _OPAQUE.finditer(t):
        v = m.group(0)
        if not any(c.isdigit() for c in v) or _wordy(v) or _code_sha(t, *m.span()):
            continue
        if any(len(p) >= 32 and any(c.isdigit() for c in p) and any(c.isalpha() for c in p)
               for p in re.split(r"[_\-]", v)) or (any(c.isupper() for c in v) and any(c.islower() for c in v)):
            out.append(m.span())
    return out


# ---------------------------------------------------------------------------------------------- broken / spaced / encoded

_BREAK = re.compile(r"(?<=[A-Za-z0-9_\-+/=.])[ \t]{0,40}\r?\n[ \t]{0,40}(?=[A-Za-z0-9_\-+/=])")
_SPACED = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z0-9_\-.+/=][ \t]){15,600}[A-Za-z0-9_\-.+/=](?![A-Za-z0-9])")
_WORDS = re.compile(r"\S{1,8000}")
_ENCODED = re.compile(r"%[0-9A-Fa-f]{2}|&#?[A-Za-z0-9]{1,10};")


def _encoded_words(t: str):
    if "%" not in t and "&" not in t:
        return
    for m in _WORDS.finditer(t):
        if _ENCODED.search(m.group(0)):
            yield m


def _strong_one(t: str) -> list:
    spans = _prefix_spans(t) + [m.span() for m in _PEM.finditer(t)]
    spans += [m.span() for m in _AWS_SECRET.finditer(t)
              if _aws_shape(m.group(0)) and not _wordy(m.group(0))]
    return spans


def _aws_shape(v: str) -> bool:
    return any(c.isdigit() for c in v) and any(c.isupper() for c in v) and any(c.islower() for c in v)


def _joined_spans(t: str) -> list:
    """Strong matches across line breaks (``sk-proj-abc`` / ``def...`` on the next line): breaks removed, matched,
    mapped back; only a match that crosses a removed break is new."""
    if "\n" not in t:
        return []
    parts, pos, cuts, last = [], [], set(), 0
    for m in _BREAK.finditer(t):
        parts.append(t[last:m.start()])
        pos.extend(range(last, m.start()))
        cuts.add(len(pos))
        last = m.end()
    if not cuts:
        return []
    parts.append(t[last:])
    pos.extend(range(last, len(t)))
    j = "".join(parts)
    out = []
    for a, b in _strong_one(j) + [s for s in _labelled_spans(j)]:
        if any(a < c < b for c in cuts) and b > a:
            out.append((pos[a], pos[b - 1] + 1))
    return out


def _spaced_spans(t: str) -> list:
    out = []
    for m in _SPACED.finditer(t):
        j = re.sub(r"[ \t]", "", m.group(0))
        if _strong_one(j) or _weak_spans(j):
            out.append(m.span())
    return out


def _encoded_spans(t: str) -> list:
    out = []
    for m in _encoded_words(t):
        for d in decodings(m.group(0))[1:]:
            d = normalise(d)
            if _strong_one(d) or _labelled_spans(d):
                out.append(m.span())
                break
    return out


def _merge(spans: list) -> list:
    merged: list = []
    for a, b, *rest in sorted(spans):
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b, rest[0] if rest else SECRET])
    return merged


def _apply(t: str, spans: list) -> str:
    merged = _merge(spans)
    if not merged:
        return t
    out, at = [], 0
    for a, b, ph in merged:
        out.append(t[at:a])
        out.append(ph)
        at = b
    out.append(t[at:])
    return "".join(out)


def _protect(t: str, spans: list) -> list:
    """Spans that lie inside a placeholder already written are dropped (redacting twice is the same as once)."""
    if "[redacted:" not in t:
        return spans
    marks = [(m.start(), m.end()) for m in re.finditer(r"\[redacted:[a-z]{1,10}\]", t)]
    return [s for s in spans if not any(ma <= s[0] and s[1] <= mb for ma, mb in marks)]


def _secret_spans(t: str) -> list:
    return (_strong_one(t) + _labelled_spans(t) + _weak_spans(t) + _joined_spans(t) + _spaced_spans(t)
            + _encoded_spans(t))


def secrets(text: str) -> str:
    """``text`` (normalised) with every key, token and credential replaced by ``[redacted:secret]``."""
    if not text:
        return text or ""
    t = normalise(text)
    return _apply(t, _protect(t, _secret_spans(t)))


# ---------------------------------------------------------------------------------------------- personal data

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,8}\.[A-Za-z]{2,24}")
_AT_WORD = r"(?:\s{0,3}[\[({<]\s{0,2}(?:at|@)\s{0,2}[\])}>]\s{0,3})"
_DOT_WORD = r"(?:\s{0,3}[\[({<]\s{0,2}(?:dot|\.)\s{0,2}[\])}>]\s{0,3}|\s{1,3}dot\s{1,3}|\.)"
_EMAIL_OBF = re.compile(r"(?i)(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}" + _AT_WORD + r"[A-Za-z0-9\-]{1,63}(?:"
                        + _DOT_WORD + r"[A-Za-z0-9\-]{1,63}){0,6}" + _DOT_WORD + r"[A-Za-z]{2,24}\b")
_EMAIL_WORDS = re.compile(r"(?i)(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}\s{1,3}at\s{1,3}[A-Za-z0-9\-]{1,63}"
                          r"(?:\s{1,3}dot\s{1,3}[A-Za-z0-9\-]{1,63}){1,6}\b")
_SSN = re.compile(r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)")
_SSN_LABEL = re.compile(r"(?i)\b(?:ssn|social security(?: number| no\.?)?)\W{0,4}(\d{9})(?!\d)")
_CARD = re.compile(r"(?<!\d)\d(?:[ \-]?\d){12,18}(?!\d)")
_UUID = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_AMAZON_ORDER = re.compile(r"(?<!\d)\d{3}-\d{7}-\d{7}(?!\d)")
_PHONE_NANP = re.compile(r"(?<![\w+])(?:\+?1[\s.\-])?(?:\(\d{3}\)[\s.\-]?|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}(?!\d)")
_PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}(?:[\s.\-]?\(?\d{1,5}\)?){1,6}(?!\d)")
_PHONE_LABEL = re.compile(r"(?i)\b(?:phone|tel|telephone|call|text|sms|mobile|cell|whatsapp|fax|ph|contact)\.?"
                          r"(?:\s{0,3}(?:no\.?|number|#))?\s{0,3}[:#]?\s{0,3}(\(?\+?\d[\d\s.\-()]{8,18}\d)")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _isbn13(d: str) -> bool:
    return len(d) == 13 and d[:3] in ("978", "979") and sum(int(c) * (1 if i % 2 == 0 else 3)
                                                             for i, c in enumerate(d)) % 10 == 0


def _personal_spans(t: str) -> list:
    out = [(m.start(), m.end(), EMAIL) for rx in (_EMAIL, _EMAIL_OBF, _EMAIL_WORDS) for m in rx.finditer(t)]
    safe = [m.span() for rx in (_UUID, _AMAZON_ORDER) for m in rx.finditer(t)]
    for m in _CARD.finditer(t):
        a, b = m.span()
        d = re.sub(r"\D", "", m.group(0))
        if not (13 <= len(d) <= 19 and _luhn(d)) or any(sa <= a and b <= sb for sa, sb in safe):
            continue
        if _isbn13(d):
            continue                                   # an ISBN-13 (978 / 979, its own check digit) is not a card
        out.append((a, b, CARD))
    out += [(m.start(), m.end(), SSN) for m in _SSN.finditer(t)]
    out += [(m.start(1), m.end(1), SSN) for m in _SSN_LABEL.finditer(t)]
    for rx in (_PHONE_NANP, _PHONE_INTL):
        for m in rx.finditer(t):
            n = sum(c.isdigit() for c in m.group(0))
            if 10 <= n <= 15 or (rx is _PHONE_INTL and 8 <= n <= 15):
                if not any(sa <= m.start() and m.end() <= sb for sa, sb in safe):
                    out.append((m.start(), m.end(), PHONE))
    for m in _PHONE_LABEL.finditer(t):
        if 10 <= sum(c.isdigit() for c in m.group(1)) <= 15:
            out.append((m.start(1), m.end(1), PHONE))
    for m in _encoded_words(t):
        for d in decodings(m.group(0))[1:]:
            if _EMAIL.search(d) or _EMAIL_OBF.search(d):
                out.append((m.start(), m.end(), EMAIL))
                break
    return out


def personal(text: str) -> str:
    """``text`` (normalised) with e-mail addresses, Luhn-valid card numbers, SSNs and phone numbers redacted."""
    if not text:
        return text or ""
    t = normalise(text)
    return _apply(t, _protect(t, _personal_spans(t)))


def redact(text: str) -> str:
    """Personal data, then keys and tokens (an e-mail address is one placeholder, never split by a token rule)."""
    return secrets(personal(text))


_HSPACE = re.compile(r"[^\S\n]{2,}")
_BLANKS = re.compile(r"\n[^\S\n]*(?:\n[^\S\n]*){2,}")


def prepare(text: str) -> str:
    """Provider text made safe to READ (M2, idris.observe): normalised; percent- and HTML-entity-decoded (bounded,
    ``DECODE_ROUNDS``) so nothing is read in its encoded form; whitespace runs on a line read as one space and blank-
    line runs as one blank line (a credential's label and value are never kept apart by padding); URL user parts
    (``//user:password@``) removed; then personal data and keys redacted (``redact``)."""
    if not text:
        return text or ""
    t = normalise(decode(normalise(text)))
    t = _BLANKS.sub("\n\n", _HSPACE.sub(" ", t))
    t = _USERINFO.sub(lambda m: m.group(0)[:m.start(1) - m.start(0)], t)
    t = _USERPASS.sub("", t)
    return redact(t)


# ---------------------------------------------------------------------------------------------- the final guard

def strong_secret(text: str) -> bool:
    """A strong rule (prefix, PEM, AWS secret, labelled credential, broken / spaced / encoded key) fires on ``text``
    or on any of its decodings. Never exempted."""
    if not text:
        return False
    for d in _views(text):
        if _protect(d, _strong_one(d) + _labelled_spans(d) + _joined_spans(d) + _spaced_spans(d)):
            return True
    return False


def _views(text: str) -> list:
    """The normalised text, each of its decodings, and the same with every whitespace run read as one space."""
    out = []
    for d in decodings(normalise(text)):
        d = normalise(d)
        out.append(d)
        c = " ".join(d.split())
        if c != d:
            out.append(c)
    return out


def personal_hit(text: str) -> bool:
    """A personal-data rule fires on ``text`` or any of its decodings."""
    if not text:
        return False
    return any(_protect(d, _personal_spans(d)) for d in _views(text))


def sensitive(text: str) -> bool:
    """Any rule (strong, weak or personal) fires: the text is not safe to show as it is."""
    if not text:
        return False
    t = normalise(text)
    return strong_secret(t) or personal_hit(t) or bool(_protect(t, _weak_spans(t)))


# keys whose values are ids, hashes, codes, enums or times written by the service (never provider text)
_STRUCTURAL = frozenset({"engine", "engines", "model", "model_version", "provider", "class", "reason", "status",
                         "outcome", "state", "kind", "basis", "rules", "refresh_tier", "version", "contract",
                         "agent", "task", "severity", "decision", "data_class", "code", "origin", "source",
                         "confidence", "signal", "excluded", "withheld", "capability", "effect_class"})


def _structural(key) -> bool:
    if not isinstance(key, str):
        return False
    k = key.lower()
    return k in _STRUCTURAL or k == "id" or k.endswith(("_id", "_ids", "sha256", "_hash", "hash", "_at", "_due")) \
        or k in ("at", "seq")


@functools.lru_cache(maxsize=16384)
def _clean_one(s: str) -> tuple:
    r = redact(s)
    out = s if r == normalise(s) else r
    if strong_secret(out) or personal_hit(out):
        return WITHHELD, out is not s, True              # M4: a detector still fires after redaction: fail closed
    return out, out is not s, False


def _clean(s: str, stats: dict) -> str:
    out, changed, withheld = _clean_one(s)
    if changed:
        stats["redacted"] = stats.get("redacted", 0) + 1
    if withheld:
        stats["withheld"] = stats.get("withheld", 0) + 1
    return out


@functools.lru_cache(maxsize=16384)
def _unsafe(s: str) -> bool:
    return strong_secret(s) or personal_hit(s)


def _walk(obj, stats: dict, key=None):
    if isinstance(obj, str):
        if not _structural(key):
            return _clean(obj, stats)
        if _unsafe(obj):                                  # an id / code field is never redacted, only withheld
            stats["withheld"] = stats.get("withheld", 0) + 1
            return WITHHELD
        return obj
    if isinstance(obj, list):
        return [_walk(x, stats, key) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(x, stats, key) for x in obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nk = k if not isinstance(k, str) or _structural(key) else _clean(k, stats)
            if nk in out and nk != k:
                nk = f"{nk}#{len(out)}"
            out[nk] = _walk(v, stats, k)
        if out.get("untrusted") is True and isinstance(out.get("text"), str) and out["text"] != obj.get("text"):
            out["sha256"] = hashlib.sha256(out["text"].encode("utf-8", "surrogatepass")).hexdigest()
        return out
    return obj


def scrub(obj) -> tuple:
    """``(obj', stats)``: every free-text string and dict key of a record redacted (``redact``), then re-checked: a
    field any strong or personal detector still fires on is replaced by ``[redacted:withheld]``. Ids, hashes, codes
    and times (``_structural`` keys) are never rewritten, only checked: one a detector fires on is withheld too. An
    observed-text blob (``{"untrusted": true, "text", "sha256"}``) gets
    the SHA-256 of its redacted text. Never raises: on an internal error every string is withheld (fail closed)."""
    stats: dict = {}
    try:
        return _walk(obj, stats), stats
    except Exception:  # fail closed: nothing un-redacted is recorded
        def blank(o, key=None):
            if isinstance(o, str):
                return o if _structural(key) else WITHHELD
            if isinstance(o, (list, tuple)):
                return [blank(x, key) for x in o]
            if isinstance(o, dict):
                return {k if not isinstance(k, str) or re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", k) else f"{WITHHELD}#{i}":
                        blank(v, k) for i, (k, v) in enumerate(o.items())}
            return o
        return blank(obj), {"failed": True}
