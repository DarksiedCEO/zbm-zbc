"""
Credential handling — defense in depth for "no credential ever appears in
an API response, log line, error message or ledger payload".

Three layers (ADR 0004, decision 6; fix wave 1 finding F10):

1. STRUCTURAL: no model has a secret field, inbound models forbid unknown
   fields, validation errors never echo input, the vault stand-in refuses
   to hold anything.
2. REFUSED AT INTAKE: ``find_credential`` looks at every inbound string
   after NORMALIZING it (NFKC, so fullwidth ``ｐａｓｓｗｏｒｄ`` is
   ``password``; format/zero-width characters removed; letters separated by
   spaces/dots/dashes collapsed, so ``p a s s w o r d`` is ``password``;
   casefolded; a leetspeak variant for keywords) and with multilingual
   credential keywords. A credential-shaped value — a password/PIN/OTP/key
   after its label, a ``user / secret`` pair after a login/creds word, a
   password-like token next to a login word, a Luhn-valid card number, an
   API-key/token shape — is REJECTED (422, ``CREDENTIAL_REFUSAL``) rather
   than stored and scrubbed later. The only place credentials may ever go is
   the vault path, and it refuses today.
3. OUTPUT SCRUB: ``scrub`` still replaces anything credential-like with
   ``[REDACTED]`` in every response, log record, ledger payload/summary and
   memory write; if the normalized text is still credential-shaped after
   the pattern replacements, the whole string is replaced.

Honest limit (ADR 0004 gap): a password that is an ordinary word, typed
with no cue ("tangerine"), cannot be recognised by any pattern. The
structural layer still holds for every field built to carry access.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

REDACTED = "[REDACTED]"
# What an already-redacted span counts as when a text is re-checked: a
# non-alphanumeric mark, so "password=[REDACTED] HTTP/1.1" is not re-read as
# "password= HTTP/1.1" (which made the whole access-log path [REDACTED]).
_INERT = "\u2022"

CREDENTIAL_REFUSAL = (
    "this looks like a password, PIN, card number, key or other credential, so it was not accepted. "
    "Please never send credentials to us here; we will never ask for them. Access is granted through "
    "the secure access link instead"
)

# key/cue followed by a value: "password: x", "my pw is x", "api key = x"
_CUE_STRONG = re.compile(
    r"(?i)\b(password|passwd|passcode|pwd|pw)\b"
    r"(\s*(?:is|was|=|:|->)\s*|\s+)(\"[^\"]*\"|'[^']*'|\S+)"
)
_CUE = re.compile(
    r"(?i)\b(pass|secret|token|api[\s_-]?key|access[\s_-]?token|"
    r"refresh[\s_-]?token|client[\s_-]?secret|private[\s_-]?key|2fa(?:\s+code)?|otp|login)"
    r"(\s*(?:is|was|=|:|->)\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)
# well-known credential prefixes
_PREFIXED = re.compile(
    r"\b(sk_(?:live|test)_[A-Za-z0-9]{6,}|rk_(?:live|test)_[A-Za-z0-9]{6,}|shp(?:at|ss|ca|pa)_[A-Za-z0-9]{8,}|"
    r"EAA[A-Za-z0-9]{20,}|ya29\.[A-Za-z0-9_\-.]{10,}|1//[A-Za-z0-9_\-]{10,}|gh[pousr]_[A-Za-z0-9]{10,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_\-]{16,}|AIza[A-Za-z0-9_\-]{30,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
# high-entropy token: >=20 chars, upper+lower+digit, token charset, no dots
_TOKENISH = re.compile(r"(?<![A-Za-z0-9_\-+/=])[A-Za-z0-9_\-+/=]{20,}(?![A-Za-z0-9_\-+/=])")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")

_SECRET_KEYS = {
    "password", "passwd", "pwd", "secret", "token", "access_token", "refresh_token", "id_token",
    "client_secret", "api_key", "apikey", "private_key", "authorization", "credential", "credentials",
    "otp", "passcode", "pin", "cvv", "card_number",
}

# --- normalization ---------------------------------------------------------------

_SEPARATED = re.compile(r"(?<![^\W_])((?:[^\W_][ .\-_*·]){3,}[^\W_])(?![^\W_])")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})


def _strip_format(text: str) -> str:
    # NFKC folds compatibility forms (fullwidth letters, ligatures, circled
    # letters); Cf removes zero-width space/joiners, soft hyphen, BOM, bidi
    # controls — characters that hide a keyword without changing how it looks.
    t = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in t if unicodedata.category(ch) != "Cf")


def _collapse_separated(text: str) -> str:
    return _SEPARATED.sub(lambda m: re.sub(r"[ .\-_*·]", "", m.group(1)), text)


def normalize(text: str) -> str:
    """The form keyword rules run on: NFKC, format characters removed,
    separated single letters collapsed, casefolded, underscores as spaces."""
    t = _collapse_separated(_strip_format(text)).casefold()
    return _collapse_separated(t.replace("_", " "))


# --- keywords (normalized, casefolded) ------------------------------------------------

_PASSWORD_WORDS = (
    r"passwords?|passwd|pass\s?word|passwort|kennwort|passcode|passphrase|pwd|pw|contraseñas?|contrasenas?|clave|"
    r"mot\s+de\s+passe|mdp|senhas?|wachtwoord|lösenord|losenord|salasana|hasło|haslo|şifre|sifre|heslo|jelszó|jelszo|"
    r"passord|adgangskode|lozinka|geslo|parolă|parola\s+d'ordine|пароль|парол|密码|密碼|パスワード|비밀번호|סיסמה|"
    r"كلمة\s+المرور|κωδικός"
)
_SECRET_WORDS = (
    r"secret|client\s+secret|api\s*-?\s*key|apikey|access\s+token|refresh\s+token|auth\s+token|token|private\s+key|"
    r"secret\s+key|signing\s+key"
)
_PIN_WORDS = (
    r"pin(?:\s*(?:code|number|no|nr|#))?|pincode|passcode|cvv2?|cvc|otp|2fa(?:\s*code)?|mfa(?:\s*code)?|"
    r"security\s+code|verification\s+code|one[\s-]time\s+(?:code|password)|código|codigo|geheimzahl|code\s+pin"
)
_LOGIN_WORDS = (
    r"login|log\s?in|sign\s?in|signin|username|user\s?name|user|usuario|benutzer(?:name)?|utilisateur|identifiant|"
    r"account|acct|email|e-mail"
)
_CREDS_WORDS = r"creds?|credentials?|credenciales|zugangsdaten|identifiants|login\s+details|login\s+info"

_SEP_EXPLICIT = r"\s*(?::=|=>|->|=|:|：)\s*"
_SEP_VERB = r"\s+(?:is|was|es|ist|est|é|är|er|on|jest|je)\s+"

_BENIGN_AFTER_IS = frozenset(
    "wrong right correct incorrect required needed missing expired reset changed same different weak strong long short "
    "case sensitive forgotten lost stored saved set ok okay fine working broken being still also now not the a an too "
    "very really blank empty invalid valid locked unlocked shared private safe secure in on with for at of to my our "
    "your their his her its this that it no yes managed handled kept protected hidden encrypted".split()
)

_PW_EXPLICIT = re.compile(rf"(?<![^\W_])(?:{_PASSWORD_WORDS})(?![^\W_]){_SEP_EXPLICIT}([^\s]+)")
_PW_VERB = re.compile(rf"(?<![^\W_])(?:{_PASSWORD_WORDS})(?![^\W_]){_SEP_VERB}([^\s]+)")
_PW_SPACE = re.compile(rf"(?<![^\W_])(?:{_PASSWORD_WORDS})(?![^\W_])\s+([^\s]+)")
_SECRET_VALUE = re.compile(rf"(?<![^\W_])(?:{_SECRET_WORDS})(?![^\W_])(?:{_SEP_EXPLICIT}|{_SEP_VERB}|\s+)([^\s]+)")
_PIN_VALUE = re.compile(rf"(?<![^\W_])(?:{_PIN_WORDS})(?![^\W_])(?:{_SEP_EXPLICIT}|{_SEP_VERB}|\s+)?#?\s*(\d[\d\s-]{{1,10}}\d)(?!\d)")
_LOGIN_PAIR = re.compile(rf"(?<![^\W_])(?:{_LOGIN_WORDS})(?![^\W_])(?:{_SEP_EXPLICIT}|{_SEP_VERB}|\s+)"
                         r"(\S+?)\s*[/|\\]\s*(\S+)")
_CREDS_PAIR = re.compile(rf"(?<![^\W_])(?:{_CREDS_WORDS})(?![^\W_])(?:{_SEP_EXPLICIT}|{_SEP_VERB}|\s+)"
                         r"(\S+?)\s*[/|\\:]\s*(\S+)")
_CREDS_VALUE = re.compile(rf"(?<![^\W_])(?:{_CREDS_WORDS})(?![^\W_]){_SEP_EXPLICIT}(\S+)")
_EMAIL_PAIR = re.compile(r"[^\s@/|]+@[^\s@/|]+\s*[/|\\:]\s*(\S+)")
_NEAR_WORDS = (
    r"login|log\s?in|sign\s?in|signin|username|user\s?name|user|usuario|benutzer(?:name)?|utilisateur|identifiant"
)
_LOGIN_NEAR = re.compile(rf"(?<![^\W_])(?:{_NEAR_WORDS}|{_CREDS_WORDS}|{_PASSWORD_WORDS})(?![^\W_])")
# A card number stands alone: not embedded in an identifier or hex string
# (digits glued to letters, '-', '_' or '.' are part of something else).
_CARD = re.compile(r"(?<![\w\-])(?<!\w\.)(?:\d[ -]?){12,18}\d(?![\w\-])(?!\.\w)")
_WORDISH = re.compile(r"\S+")


def _trim(v: str) -> str:
    return v.strip(".,;!?)(\"'[]{}<>“”‘’")


def _has_alnum(v: str) -> bool:
    return any(ch.isalnum() for ch in v)


def _password_like(v: str) -> bool:
    """Looks like something someone chose as a secret: has a digit, or mixes
    letters with symbols; at least 4 characters."""
    v = _trim(v)
    if len(v) < 4 or not _has_alnum(v) or "://" in v:
        return False
    has_digit = any(ch.isdigit() for ch in v)
    has_alpha = any(ch.isalpha() for ch in v)
    has_symbol = any(not ch.isalnum() for ch in v)
    return has_digit or (has_alpha and has_symbol)


def _high_entropy(tok: str) -> bool:
    """Password-shaped token: 8+ chars and at least three of lower / upper /
    digit / symbol."""
    tok = _trim(tok)
    if len(tok) < 8 or "://" in tok or "@" in tok:
        return False
    classes = sum((any(c.islower() for c in tok), any(c.isupper() for c in tok), any(c.isdigit() for c in tok),
                   any(not c.isalnum() for c in tok)))
    return classes >= 3


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _looks_high_entropy(tok: str) -> bool:
    return (
        any(c.isupper() for c in tok)
        and any(c.islower() for c in tok)
        and any(c.isdigit() for c in tok)
    )


# --- fix wave 3 (N5): the AEGIS round-2 shapes --------------------------------------

# URL with a password in its userinfo: https://lee:<pw>@shop.example/admin
_URL_USERINFO_SECRET = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/@:]+:[^\s/@]+@")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@]+@")
# "you can get in with lee and <pw>", "log in using admin / <pw>"
_GET_IN_WITH = re.compile(r"(?<![^\W_])(?:get|log|sign)\s*(?:in|on)(?:to)?(?:\s+\S+){0,3}?\s+(?:with|using|via)\s+"
                          r"(\S+)\s+(?:and|&|\+|/|,|y|und|et|e)\s+(\S+)")
# "use <pw> to log in", "enter <pw> to sign in"
_USE_TO_LOGIN = re.compile(r"(?<![^\W_])(?:use|try|enter|type|with)\s+(\S+)\s+(?:to|for)\s+(?:log|sign|get)\s*(?:in|on)(?![^\W_])")
# leetspeak password words: p@ss, p4ss, pa$$, p4ssw0rd (compared on the leet-folded token)
_LEET_PASS_WORDS = frozenset({"pass", "passw", "passwd", "password", "pwd", "pw", "pword", "passwort", "passcode"})
_PASS_EXPLICIT = re.compile(rf"(?<![^\W_])pass(?![^\W_]){_SEP_EXPLICIT}(\S+)")
# US social security number
_SSN = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
_SSN_WORDS = r"ssn|social\s+security(?:\s+(?:number|no|nr|#))?|itin|tax\s+id"
_SSN_VALUE = re.compile(rf"(?<![^\W_])(?:{_SSN_WORDS})(?![^\W_])[^\d\n]{{0,20}}(\d[\d\s-]{{7,12}}\d)(?!\d)")
# bank account / routing numbers after a bank word; IBAN (mod-97 checked)
_BANK_WORDS = (
    r"routing(?:\s*(?:number|no|nr|#))?|aba|iban|swift|bic|sort\s*code|bank\s*account|account\s*(?:number|no|nr|#)|"
    r"acct(?:\s*(?:number|no|nr|#))?|a/c|checking|savings|clabe|cuenta|konto(?:nummer)?|compte|conta|rekening"
)
_BANK_VALUE = re.compile(rf"(?<![^\W_])(?:{_BANK_WORDS})(?![^\W_])[^\d\n]{{0,20}}(\d(?:[\d ]*\d)?)")
_ACCOUNT_LONG = re.compile(r"(?<![^\W_])account(?![^\W_])[^\d\n]{0,20}(\d{12,17})(?!\d)")
_IBAN = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30})(?![A-Za-z0-9])")
# "admin / <pw>", "lee | <pw>": a pair whose second half is password-shaped
# (8+ chars with upper case, lower case AND a digit — so "America/New_York",
# "sales/returns" and "compliance_15/p1_wording" are not pairs)
_SLASH_PAIR = re.compile(r"(\S+)\s*[/|]\s*(\S+)")


def _iban_valid(s: str) -> bool:
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    try:
        return int("".join(str(int(ch, 36)) for ch in moved)) % 97 == 1
    except ValueError:
        return False


def _digit_count(s: str) -> int:
    return sum(ch.isdigit() for ch in s)


def _wave3_shape(raw: str, n: str, leet: str) -> str | None:
    """The shapes AEGIS round 2 got through intake (fix wave 3, N5)."""
    if _URL_USERINFO_SECRET.search(raw):
        return "url_userinfo_password"
    for m in _GET_IN_WITH.finditer(n):
        if _password_like(m.group(2)) or _password_like(m.group(1)):
            return "login_secret_pair"
    for m in _USE_TO_LOGIN.finditer(n):
        if _password_like(m.group(1)):
            return "password_for_login"
    toks = raw.split()
    for i, tok in enumerate(toks):
        base = _trim(tok).casefold()
        folded = base.translate(_LEET)
        if folded == base:
            continue
        word, sep, rest = folded.partition(":") if ":" in folded else folded.partition("=")
        if word in _LEET_PASS_WORDS:
            value = rest if sep else (toks[i + 1] if i + 1 < len(toks) else "")
            if _has_alnum(value) and len(_trim(value)) >= 4:
                return "password_after_label"
    if _PASS_EXPLICIT.search(leet) and not _PASS_EXPLICIT.search(n):
        return "password_after_label"
    if _SSN.search(raw):
        return "ssn"
    for m in _SSN_VALUE.finditer(n):
        if _digit_count(m.group(1)) == 9:
            return "ssn"
    for m in _BANK_VALUE.finditer(n):
        if _digit_count(m.group(1)) >= 6:
            return "bank_account_number"
    if _ACCOUNT_LONG.search(n):
        return "bank_account_number"
    for m in _IBAN.finditer(raw):
        if _iban_valid(m.group(1)):
            return "bank_account_number"
    for m in _SLASH_PAIR.finditer(raw):
        v = _trim(m.group(2))
        if _high_entropy(v) and _looks_high_entropy(v) and "://" not in m.group(0):
            return "login_secret_pair"
    return None


def find_credential(text: str) -> str | None:
    """Return the NAME of the first credential rule the text trips (never
    the value), or None. Runs on normalized forms; see module docstring."""
    if not isinstance(text, str) or not text:
        return None
    raw = _strip_format(text)  # case kept: key prefixes are case-sensitive
    n = normalize(text)
    leet = n.translate(_LEET)
    if _BEARER.search(raw):
        return "bearer_token"
    if _PREFIXED.search(raw) or _PREFIXED.search(_collapse_separated(raw)):
        return "api_key_shape"
    for m in _TOKENISH.finditer(raw):
        if _looks_high_entropy(m.group(0)):
            return "token_shape"
    for form in (n, leet):
        for m in _PW_EXPLICIT.finditer(form):
            if _has_alnum(m.group(1)):
                return "password_after_label"
        for m in _PW_VERB.finditer(form):
            if _trim(m.group(1)) not in _BENIGN_AFTER_IS and _has_alnum(m.group(1)):
                return "password_after_label"
        for m in _PW_SPACE.finditer(form):
            if _password_like(m.group(1)):
                return "password_after_label"
    for m in _SECRET_VALUE.finditer(n):
        if _password_like(m.group(1)) or len(_trim(m.group(1))) >= 12:
            return "secret_after_label"
    if _PIN_VALUE.search(n):
        return "pin_after_label"
    for m in _LOGIN_PAIR.finditer(n):
        if _password_like(m.group(2)):
            return "login_secret_pair"
    for m in _CREDS_PAIR.finditer(n):
        if _has_alnum(m.group(1)) and _has_alnum(m.group(2)):
            return "login_secret_pair"
    for m in _CREDS_VALUE.finditer(n):
        if _has_alnum(m.group(1)):
            return "credentials_after_label"
    for m in _EMAIL_PAIR.finditer(n):
        if _password_like(m.group(1)):
            return "login_secret_pair"
    # a password-shaped token within a few words after a login / password word
    for m in _LOGIN_NEAR.finditer(n):
        for w in _WORDISH.findall(_nearby_raw(raw, n, m.end())):
            if _high_entropy(w):
                return "password_near_login_word"
    for m in _CARD.finditer(raw):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits) and len(set(digits)) > 1:
            return "card_number"
    return _wave3_shape(raw, n, leet)


def _nearby_raw(raw: str, n: str, n_end: int) -> str:
    """The ~6 words of the case-preserving text that follow a keyword found
    at ``n_end`` in the normalized text. Normalization can shorten the text
    (collapsed letters), so the position is mapped by word count."""
    words_before = len(n[:n_end].split())
    raw_words = raw.split()
    return " ".join(raw_words[max(0, words_before - 1): words_before + 6])


def contains_credential(text: str) -> bool:
    return find_credential(text) is not None


def refuse_credentials(obj: Any) -> None:
    """Raise ValueError(CREDENTIAL_REFUSAL) if any string in ``obj`` (dict
    keys included) is credential-shaped. The message never repeats input."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and (k.lower() in _SECRET_KEYS or find_credential(k)):
                raise ValueError(CREDENTIAL_REFUSAL)
            refuse_credentials(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            refuse_credentials(v)
    elif isinstance(obj, str) and find_credential(obj):
        raise ValueError(CREDENTIAL_REFUSAL)


def scrub(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    out = _BEARER.sub(f"Bearer {REDACTED}", text)
    out = _CUE_STRONG.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _CUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _PREFIXED.sub(REDACTED, out)
    out = _TOKENISH.sub(lambda m: REDACTED if _looks_high_entropy(m.group(0)) else m.group(0), out)
    # Second pass on the normalized form: anything the plain patterns missed
    # (other languages, spaced or fullwidth letters, zero-width characters,
    # pairs, PINs, card numbers) redacts the whole string.
    if find_credential(out.replace(REDACTED, _INERT)):
        return REDACTED
    return out


def scrub_obj(obj: Any) -> Any:
    """Recursively scrub strings; drop values under secret-named keys."""
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                clean[k] = REDACTED
            else:
                clean[k] = scrub_obj(v)
        return clean
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v) for v in obj]
    if isinstance(obj, str):
        return scrub(obj)
    return obj


# --- stored copies of client free text (fix wave 3, N5) ------------------------------
#
# Raw client free text (messages, documents, fact values, a clipper's bio)
# is never stored or served. What is kept is ``redact_text(text)``: every
# detector miss would otherwise become a stored secret, so this is
# deliberately aggressive — it replaces with [REDACTED]:
#   - URL userinfo (``https://user:pw@host`` -> ``https://[REDACTED]@host``)
#     and secret-ish or password-shaped URL path segments / query values;
#   - SSNs, bank account / routing numbers (after a bank word, or any run of
#     9-19 digits), IBANs, Luhn-valid card numbers;
#   - every high-entropy token of 10+ characters that mixes letters and
#     digits (with mixed case or a symbol, or 3+ of each);
#   - everything following a login / password / PIN / secret / credentials /
#     bank word in any supported language (to the end of that sentence, at
#     most 12 words), and the few words before "to log in";
#   - the cue patterns of ``scrub``.
# If the result still trips ``find_credential``, the whole text is replaced.
# Emails and plain URLs are kept.

_KEYWORD = re.compile(
    rf"(?:{_PASSWORD_WORDS}|{_SECRET_WORDS}|{_PIN_WORDS}|{_CREDS_WORDS}|{_NEAR_WORDS}|{_BANK_WORDS}|{_SSN_WORDS}|"
    r"pass|passe|passw|pword|get\s+in|log\s*on|sign\s*on|security\s+(?:question|answer))"
)
_TO_LOGIN = re.compile(r"(?:to|for)\s+(?:log|sign|get)\s*(?:in|on)")
_LONG_DIGITS = re.compile(r"(?<![\d.,])\d{9,19}(?!\d|[.,]\d)")
_EMAIL_TOKEN = re.compile(r"[^\s@/:]+@[^\s@/]+\.[A-Za-z]{2,}")
_URL_TOKEN = re.compile(r"(?i)^(?:[a-z][a-z0-9+.\-]*://|www\.)")
_URL_PART = re.compile(r"[^/?&=#;]+")
_QUERY_PAIR = re.compile(r"([?&;#])([^=&#;?]+)=([^&#;]*)")
_TRIM_KW = ".,;!?)(\"'[]{}<>:=“”‘’"
_REDACTED_RUN = re.compile(r"\[REDACTED\](?:[\s,;:/|]*\[REDACTED\])+")


def _entropic(tok: str) -> bool:
    """A token that looks chosen as a secret: 10+ characters mixing letters
    and digits, with mixed case or a symbol, or 3+ letters and 3+ digits."""
    t = _trim(tok)
    if len(t) < 10:
        return False
    letters = sum(ch.isalpha() for ch in t)
    digits = sum(ch.isdigit() for ch in t)
    if not letters or not digits:
        return False
    mixed_case = any(ch.isupper() for ch in t) and any(ch.islower() for ch in t)
    symbol = any(not ch.isalnum() for ch in t)
    return mixed_case or symbol or (letters >= 3 and digits >= 3)


def _secret_key(k: str) -> bool:
    k = normalize(k)
    return k in _SECRET_KEYS or _KEYWORD.fullmatch(k) is not None or k in {"key", "sig", "signature", "auth", "code", "session"}


def redact_url(url: str) -> str:
    """Userinfo, secret-named query values and password-shaped path segments."""
    from urllib.parse import unquote

    u = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}@", url)
    m = re.match(r"(?i)^((?:[a-z][a-z0-9+.\-]*://)?[^/?#]*)(.*)$", u)
    head, rest = (m.group(1), m.group(2)) if m else ("", u)
    rest = _QUERY_PAIR.sub(lambda q: f"{q.group(1)}{q.group(2)}={REDACTED}" if _secret_key(unquote(q.group(2))) else q.group(0), rest)
    rest = _URL_PART.sub(lambda p: REDACTED if p.group(0) != REDACTED and _entropic(unquote(p.group(0))) else p.group(0), rest)
    return head + rest


def _pattern_redactions(t: str) -> str:
    t = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}@", t)
    t = _BEARER.sub(f"Bearer {REDACTED}", t)
    t = _CUE_STRONG.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", t)
    t = _CUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", t)
    t = _PREFIXED.sub(REDACTED, t)
    t = _SSN.sub(REDACTED, t)
    for rx in (_SSN_VALUE, _BANK_VALUE, _ACCOUNT_LONG):
        # these match on case-folded text; the value is digits, so positions
        # are found on a case-folded copy of the same length
        low = t.casefold() if len(t.casefold()) == len(t) else None
        if low is None:
            continue
        spans = [m.span(1) for m in rx.finditer(low) if _digit_count(m.group(1)) >= 4]
        for a, b in reversed(spans):
            t = t[:a] + REDACTED + t[b:]
    t = _IBAN.sub(lambda m: REDACTED if _iban_valid(m.group(1)) else m.group(0), t)

    def card(m):
        digits = re.sub(r"\D", "", m.group(0))
        return REDACTED if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    t = _CARD.sub(card, t)
    t = _LONG_DIGITS.sub(REDACTED, t)
    return t


def redact_text(text: str) -> str:
    """The only form in which client free text is kept (see above)."""
    if not isinstance(text, str) or not text:
        return text
    t = _pattern_redactions(_strip_format(text))
    parts = re.split(r"(\s+)", t)
    idx = [i for i, p in enumerate(parts) if p and not p.isspace()]
    words = [normalize(parts[i]).strip(_TRIM_KW) for i in idx]
    leet = [w.translate(_LEET) for w in words]
    redact = [False] * len(idx)

    def follow(start: int) -> None:
        for j in range(start, min(start + 12, len(idx))):
            redact[j] = True
            gap = parts[idx[j] + 1] if idx[j] + 1 < len(parts) else ""
            if parts[idx[j]].rstrip("\"')]}").endswith((".", "!", "?")) or "\n" in gap:
                break

    for i in range(len(idx)):
        for k in (3, 2, 1):
            if i + k > len(idx):
                continue
            for form in (words, leet):
                gram = " ".join(form[i:i + k])
                if _KEYWORD.fullmatch(gram):
                    follow(i + k)
                    break
                if _TO_LOGIN.fullmatch(gram):
                    for j in range(max(0, i - 3), i):
                        redact[j] = True
                    break
            else:
                continue
            break
        tok = parts[idx[i]]
        for sep in (":", "="):
            left, found, right = tok.partition(sep)
            if found and right and "://" not in tok:
                lw = normalize(left).strip(_TRIM_KW)
                if _KEYWORD.fullmatch(lw) or _KEYWORD.fullmatch(lw.translate(_LEET)):
                    parts[idx[i]] = f"{left}{sep}{REDACTED}"
                    follow(i + 1)
                break
    for n, i in enumerate(idx):
        tok = parts[i]
        if redact[n]:
            parts[i] = REDACTED
        elif _URL_TOKEN.match(tok):
            parts[i] = redact_url(tok)
        elif _EMAIL_TOKEN.fullmatch(_trim(tok)):
            continue
        elif _entropic(tok) or (_high_entropy(tok) and _looks_high_entropy(tok)):
            parts[i] = REDACTED
    out = _REDACTED_RUN.sub(REDACTED, "".join(parts))
    if find_credential(out.replace(REDACTED, _INERT)):
        return REDACTED
    return out


def redact_obj(obj: Any) -> Any:
    """``redact_text`` over every string of a JSON-like value."""
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def _scrub_arg(a: Any) -> Any:
    return scrub(a) if isinstance(a, str) else a


def scrub_log_record(record: logging.LogRecord) -> None:
    """Scrub one log record IN A FORM EVERY FORMATTER CAN STILL USE (fix wave
    3, D1). The old code set ``record.args = None``; uvicorn's access
    formatter unpacks ``record.args`` as a 5-tuple (client, method, path,
    http version, status), so every request raised ``TypeError`` ("---
    Logging error ---") and no access line was written.

    - String args are scrubbed element-wise; non-strings (ints, the status
      code) are kept, so the args keep their shape.
    - If the formatted message is still credential-shaped (a secret made of
      message + args together), uvicorn access records get the request path
      replaced (every string arg if that is not enough); any other record is
      rewritten as its scrubbed, formatted message with an EMPTY TUPLE of
      args (never None).
    - A message without args is scrubbed directly."""
    args = record.args
    if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
        # The request path can carry a secret anywhere (a path segment, a
        # query value, percent-encoded): URL-aware redaction first.
        args = (args[0], args[1], redact_url(args[2]), args[3], args[4])
    if isinstance(args, tuple):
        record.args = tuple(_scrub_arg(a) for a in args)
    elif isinstance(args, dict):
        record.args = {k: _scrub_arg(v) for k, v in args.items()}
    if not record.args:
        record.msg = scrub(record.msg if isinstance(record.msg, str) else str(record.msg))
        record.args = ()
        return
    try:
        formatted = record.getMessage()
    except Exception:  # noqa: BLE001  (a malformed record: keep only its scrubbed text)
        record.msg, record.args = scrub(str(record.msg)), ()
        return
    clean = scrub(formatted)
    if clean == formatted:
        return
    if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
        a = list(record.args)
        a[2] = REDACTED  # the request path + query string, if still credential-shaped
        record.args = tuple(a)
        if scrub(record.getMessage()) != record.getMessage():
            record.args = tuple(REDACTED if isinstance(x, str) else x for x in a)
        return
    record.msg, record.args = clean, ()


class ScrubbingFilter(logging.Filter):
    """Logging filter: scrubs every record (see ``scrub_log_record``)."""

    def filter(self, record: logging.LogRecord) -> bool:
        scrub_log_record(record)
        return True


_factory_installed = False


def install_log_scrubbing() -> None:
    """Scrub EVERY log record in the process, whatever logger emits it.

    Logger-level filters are not inherited by child loggers (a filter on
    "onboarding" does not see records from "onboarding.guardrails"), so the
    record factory is wrapped instead: every record is scrubbed at creation,
    including uvicorn's access log (whose request path could carry an
    identifier or a secret in the query string). Exception tracebacks are
    handled separately: the API never lets an exception message that could
    contain input reach a log (see api.py's generic handler)."""
    global _factory_installed
    if _factory_installed:
        return
    old = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = old(*args, **kwargs)
        scrub_log_record(record)
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True
