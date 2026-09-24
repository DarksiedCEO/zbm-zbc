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
    return None


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
    if find_credential(out.replace(REDACTED, " ")):
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


class ScrubbingFilter(logging.Filter):
    """Logging filter: rewrites every record's message with ``scrub``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        record.msg = scrub(msg)
        record.args = None
        return True


_factory_installed = False


def install_log_scrubbing() -> None:
    """Scrub EVERY log record in the process, whatever logger emits it.

    Logger-level filters are not inherited by child loggers (a filter on
    "onboarding" does not see records from "onboarding.guardrails"), so the
    WIP version could miss records. Wrapping the record factory scrubs the
    message of every record at creation, including uvicorn's access log
    (whose request path could carry an identifier). Exception tracebacks
    are handled separately: the API never lets an exception message that
    could contain input reach a log (see api.py's generic handler)."""
    global _factory_installed
    if _factory_installed:
        return
    old = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = old(*args, **kwargs)
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        record.msg = scrub(msg)
        record.args = None
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True
