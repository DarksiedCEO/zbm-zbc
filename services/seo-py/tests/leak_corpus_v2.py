"""A deterministic GENERATED leak corpus for the shared redaction core v2 (ADR 0050 v2). Every value is SYNTHETIC,
assembled from fragments with a seeded RNG at run time: no real credential, card number, SSN, phone number or
e-mail literal lives in this file.

``corpus()`` -> list of ``Case(name, family, disguise, carrier, probes)``: ``carrier`` is the text as an answer
engine could write it; ``probes`` are the substrings that must never appear in anything the core lets through (the
raw value and its head / tail, plus each label-free value fragment). Families cover every entry of ADR 0050 v2 2a-2f;
disguises cover every Unicode / encoding / markdown / label-form trick on the ADR's list, and pairs of them."""

from __future__ import annotations

import base64
import html
import random
import string
from dataclasses import dataclass
from urllib.parse import quote

B62 = string.ascii_letters + string.digits
HEX = "0123456789abcdef"
UP = string.ascii_uppercase + string.digits


@dataclass(frozen=True)
class Case:
    name: str
    family: str
    disguise: str
    carrier: str
    probes: tuple
    value: str


def _r(rng, n, alphabet=B62):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _mixed(rng, n, alphabet=B62):
    while True:
        s = _r(rng, n, alphabet)
        if any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s):
            return s


def _luhn_complete(rng, prefix, length):
    body = prefix + "".join(rng.choice(string.digits) for _ in range(length - len(prefix) - 1))
    total = 0
    for i, c in enumerate(reversed(body)):
        d = int(c)
        if i % 2 == 0:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return body + str((10 - total % 10) % 10)


def _b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def values(rng) -> dict:
    """family -> (value, kind). kind: "key" (no label needed), "labelled" (value only meaningful with its label in
    the carrier), "pii"."""
    dash = "-" * 5
    pem_kind = "PRIVATE" + " KEY"
    v = {
        "aws_access_key_id": "AK" + "IA" + _r(rng, 16, UP),
        "aws_session_id": "AS" + "IA" + _r(rng, 16, UP),
        "aws_secret": _mixed(rng, 40, B62 + "/+"),
        "openai_proj": "sk-" + "proj-" + _mixed(rng, 48, B62 + "_-"),
        "anthropic": "sk-" + "ant-" + "api03-" + _mixed(rng, 60, B62 + "_-"),
        "github_classic": "gh" + "p_" + _mixed(rng, 36),
        "github_fine": "github" + "_pat_" + "11" + _mixed(rng, 59, B62 + "_"),
        "stripe_live": "sk" + "_live_" + _mixed(rng, 24),
        "stripe_whsec": "wh" + "sec_" + _mixed(rng, 32),
        "slack_bot": "xo" + "xb-" + _r(rng, 12, string.digits) + "-" + _r(rng, 12, string.digits) + "-" + _mixed(rng, 24),
        "slack_refresh": "xo" + "xe.xoxp-" + "1-" + _mixed(rng, 40),
        "google_api": "AI" + "za" + _mixed(rng, 35, B62 + "_-"),
        "google_oauth": "ya" + "29." + _mixed(rng, 60, B62 + "_-"),
        "google_client_secret": "GOC" + "SPX-" + _mixed(rng, 28, B62 + "_-"),
        "shopify_access": "shp" + "at_" + _r(rng, 32, HEX),
        "shopify_shared": "shp" + "ss_" + _r(rng, 32, HEX),
        "amazon_lwa": "amzn1" + ".oa2-cs.v1." + _r(rng, 64, HEX),
        "azure_account_key": "AccountKey=" + base64.b64encode(rng.randbytes(64)).decode(),
        "azure_sas": "sv=2022-11-02&ss=b&sig=" + quote(base64.b64encode(rng.randbytes(32)).decode(), safe=""),
        "square": "sq0" + "atp-" + _mixed(rng, 22, B62 + "_-"),
        "vault": "hv" + "s." + _mixed(rng, 90, B62 + "_-"),
        "discord_bot": "M" + _mixed(rng, 25, B62) + "." + _r(rng, 6, B62) + "." + _mixed(rng, 38, B62 + "_-"),
        "mapbox": "sk." + "eyJ" + _mixed(rng, 60, B62) + "." + _mixed(rng, 22, B62),
        "jwt": ".".join(_b64url(x) for x in (b'{"alg":"HS256","typ":"JWT"}', b'{"sub":"' + _r(rng, 8).encode() + b'"}',
                                             rng.randbytes(32))),
        "sendgrid": "SG" + "." + _mixed(rng, 22, B62 + "_-") + "." + _mixed(rng, 43, B62 + "_-"),
        "npm": "np" + "m_" + _mixed(rng, 36),
        "huggingface": "h" + "f_" + _mixed(rng, 34),
        "perplexity": "pp" + "lx-" + _mixed(rng, 48),
        "gitlab": "gl" + "pat-" + _mixed(rng, 20, B62 + "_-"),
        "twilio": "S" + "K" + _r(rng, 32, HEX),
        "pem": dash + "BEGIN RSA " + pem_kind + dash + "\n" + _mixed(rng, 64, B62 + "+/") + "\n" + _mixed(rng, 64, B62 + "+/")
               + "\n" + dash + "END RSA " + pem_kind + dash,
        "db_postgres": "postgres" + "://app:" + _mixed(rng, 14) + "@db." + _r(rng, 6, string.ascii_lowercase) + ".test:5432/main",
        "db_mongo": "mongodb" + "+srv://svc:" + _mixed(rng, 14) + "@cluster0." + _r(rng, 5, string.ascii_lowercase) + ".test/app",
        "db_redis": "redis" + "://:" + _mixed(rng, 16) + "@cache.test:6379/0",
        "url_userinfo": "https" + "://admin:" + _mixed(rng, 12) + "@portal." + _r(rng, 6, string.ascii_lowercase) + ".test/",
        "hex32_unlabelled": _r(rng, 32, HEX),
        "hex64_unlabelled": _r(rng, 64, HEX),
        "b64_unlabelled": _mixed(rng, 40, B62),
        "card_visa": _luhn_complete(rng, "4", 16),
        "card_amex": _luhn_complete(rng, "37", 15),
        "ssn": f"{rng.randint(100, 665)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}",
        "phone_us": f"({rng.randint(201, 989)}) {rng.randint(200, 999)}-{rng.randint(1000, 9999)}",
        "phone_intl": f"+44 20 {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}",
        "email": _r(rng, 7, string.ascii_lowercase) + "." + _r(rng, 5, string.ascii_lowercase) + "@" + "example-"
                 + _r(rng, 4, string.ascii_lowercase) + ".com",
    }
    out = {k: (x, "pii" if k in ("card_visa", "card_amex", "ssn", "phone_us", "phone_intl", "email") else "key")
           for k, x in v.items()}
    # labelled credentials: short, word-like values that only a label makes secret
    for name in ("short_password", "pin", "passphrase_words", "natural_language", "camel_label", "snake_label",
                 "non_english"):
        out[name] = ({"pin": str(rng.randint(1000, 9999)),
                      "passphrase_words": "-".join(rng.choice(("river", "candle", "orbit", "maple", "violet", "copper"))
                                                   for _ in range(4))}.get(name, _r(rng, 5, string.ascii_lowercase)
                                                                           + str(rng.randint(10, 99))), "labelled")
    return out


LABELS = ["password", "Password", "PASSWORD", "passwd", "pwd", "pass", "passcode", "passphrase", "PIN", "secret",
          "token", "api key", "API_KEY", "apikey", "access key", "authorization", "auth", "bearer", "credentials",
          "cookie", "session id", "sid", "seed phrase", "recovery phrase", "mnemonic", "private key", "client secret",
          "connection string", "account key", "authToken", "userPassword", "DB_PASS", "x-api-key",
          "contrase\u00f1a", "mot de passe", "Passwort", "senha", "\u043f\u0430\u0440\u043e\u043b\u044c",
          "\u5bc6\u7801", "\u30d1\u30b9\u30ef\u30fc\u30c9", "\ube44\ubc00\ubc88\ud638", "wachtwoord",
          "l\u00f6senord", "has\u0142o", "\u015fifre", "kata sandi", "m\u1eadt kh\u1ea9u"]


def label_forms(label, value):
    """The label-form disguises (ADR 0050 v2 / review findings: cut at & , ; ' < >, many spaces / newlines after the
    separator, markdown / XML / table / JSON forms, natural language)."""
    return {
        "colon": f"{label}: {value}", "equals": f"{label}={value}", "arrow": f"{label} -> {value}",
        "is": f"the {label} is {value}", "would_be": f"for the admin the {label} would be {value} today",
        "remains": f"{label} remains {value} after the reset", "quoted": f"\"{label}\": \"{value}\"",
        "amp": f"{label}={value}&next=1", "comma": f"{label}: {value}, then", "semi": f"{label}={value};x",
        "apos": f"{label}='{value}'", "angle": f"{label}=<{value}>", "spaces": f"{label}:      {value}",
        "newlines": f"{label}:\n\n\n\n\n\n{value}", "md_bold": f"**{label}**: `{value}`",
        "md_table": f"| field | value |\n|---|---|\n| {label} | {value} |", "xml": f"<{label}>{value}</{label}>",
        "json": "{\"" + label + "\": \"" + value + "\"}", "dash_sep": f"{label} - {value}",
    }


def cased_verb_forms(label, value):
    """Review 98b76cde MUST 2: the verb between a label and its value in any case (the client view matched it in
    lower case only). Their cases are appended at the END of ``corpus()``, so that no earlier case moves."""
    return {
        "is_title": f"the {label} Is {value}", "is_upper": f"the {label} IS {value}", "is_mixed": f"the {label} iS {value}",
        "was_title": f"the {label} Was {value} then", "was_upper": f"the {label} WAS {value} then",
        "are_title": f"the {label} Are {value} now", "are_upper": f"the {label} ARE {value} now",
        "would_be_upper": f"for the admin the {label} WOULD BE {value} today",
        "will_be_title": f"the {label} Will Be {value} tomorrow", "remains_upper": f"{label} REMAINS {value} after the reset",
    }


def _zw(s, ch):
    return ch.join(s)


def char_disguises(value):
    """value -> disguised value, per disguise (Unicode / split / encoding)."""
    fw = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in value)
    half = len(value) // 2
    return {
        "plain": value, "fullwidth": fw,
        "zwsp": _zw(value, "\u200b"), "wj": _zw(value, "\u2060"), "bom": _zw(value, "\ufeff"),
        "soft_hyphen": _zw(value, "\u00ad"), "cgj": _zw(value, "\u034f"), "vs16": _zw(value, "\ufe0f"),
        "tag_chars": _zw(value, "\U000e0020"), "hangul_filler": _zw(value, "\u3164"),
        "bidi_wrap": "\u202e" + value + "\u202c", "nbsp_split": value[:half] + "\u00a0" + value[half:],
        "ls_split": value[:half] + "\u2028" + value[half:], "nel_split": value[:half] + "\u0085" + value[half:],
        "newline_split": value[:half] + "\n" + value[half:], "spaced_chars": " ".join(value),
        "groups_of_4": " ".join(value[i:i + 4] for i in range(0, len(value), 4)),
        "dash_groups": "-".join(value[i:i + 5] for i in range(0, len(value), 5)),
        "dot_groups": ".".join(value[i:i + 6] for i in range(0, len(value), 6)),
        "pct_once": quote(value, safe=""), "pct_twice": quote(quote(value, safe=""), safe=""),
        "pct_all": "".join(f"%{ord(c):02X}" if ord(c) < 128 else quote(c) for c in value),
        "html_dec": "".join(f"&#{ord(c)};" for c in value), "html_hex": "".join(f"&#x{ord(c):x};" for c in value),
        "html_named": html.escape(value, quote=True),
        "b64": base64.b64encode(value.encode()).decode(), "hex": value.encode().hex(),
        "md_code": f"`{value}`", "md_bold": f"**{value}**", "md_strike": f"~~{value}~~", "html_code": f"<code>{value}</code>",
        "u_escape": "".join(f"\\u{ord(c):04x}" for c in value),
        "md_interleave": "**".join(value[i:i + 4] for i in range(0, len(value), 4)),
        "private_use": _zw(value, "\ue000"), "unassigned": _zw(value, "\u0378"),
        "enclosing_mark": _zw(value, "\u20dd"), "double_spaced": "  ".join(value),
    }


PAIRS = [("zwsp", "fullwidth"), ("nbsp_split", "zwsp"), ("pct_once", "zwsp"), ("html_dec", "fullwidth"),
         ("groups_of_4", "zwsp"), ("newline_split", "cgj"), ("md_code", "vs16"), ("b64", "pct_once"),
         ("dash_groups", "fullwidth"), ("tag_chars", "nbsp_split"), ("hangul_filler", "ls_split"),
         ("spaced_chars", "soft_hyphen"), ("bidi_wrap", "groups_of_4"), ("md_bold", "html_dec")]


def _probes(value):
    v = value.replace("\n", "")
    ps = {v}
    if len(v) >= 16:
        ps |= {v[:12], v[-12:]}
    if "=" in v and v.split("=", 1)[1]:
        ps.add(v.split("=", 1)[1][:12])
    if "://" in v and "@" in v:
        ps.add(v.split("://", 1)[1].split("@", 1)[0].split(":")[-1])     # the password part of user info
    if "\n" in value:
        ps |= {line[:12] for line in value.split("\n") if len(line) >= 12 and "-----" not in line}
    return tuple(sorted(p for p in ps if len(p) >= 4))


def corpus(seed: int = 7) -> list:
    rng = random.Random(seed)
    vals = values(rng)
    cases: list = []
    for fam, (value, kind) in vals.items():
        if kind == "labelled":
            continue
        for dname, dv in char_disguises(value).items():
            carrier = f"Z Best Media note: {dv} end."
            cases.append(Case(f"{fam}/{dname}", fam, dname, carrier, _probes(value), value))
        for a, b in PAIRS:
            dv = char_disguises(char_disguises(value)[a])[b]
            cases.append(Case(f"{fam}/{a}+{b}", fam, f"{a}+{b}", f"Z Best Media: {dv} ok", _probes(value), value))
    # labelled credentials: every label x every label form, the value plain and under a char disguise
    lrng = random.Random(seed + 1)
    for i, label in enumerate(LABELS):
        for fname, text in label_forms(label, "VALUEPLACEHOLDER").items():
            value = _r(lrng, 6, string.ascii_lowercase) + str(lrng.randint(10, 99))
            cases.append(Case(f"label/{label}/{fname}", "labelled", fname, "Z Best Media. " + text.replace(
                "VALUEPLACEHOLDER", value), (value,), value))
        for dname in ("zwsp", "fullwidth", "nbsp_split", "html_dec", "pct_once"):
            value = _r(lrng, 6, string.ascii_lowercase) + str(lrng.randint(10, 99))
            dl = char_disguises(label)[dname] if len(label) > 1 else label
            cases.append(Case(f"label/{label}/disguised_label_{dname}", "labelled", f"label_{dname}",
                              f"Z Best Media. {dl}: {value}", (value,), value))
    # a label hidden by a partial escape or a short base64 run (the value itself is word-like, so only the decoded
    # label withholds it); the base64 carrier itself is the probe
    for i in range(40):
        value = "".join(lrng.choice(string.ascii_lowercase) for _ in range(lrng.randint(6, 11)))
        cases.append(Case(f"label/escaped/{i}", "labelled", "label_u_escape",
                          "Z Best Media pa\\u0073sword: " + value, (value,), value))
        enc = base64.b64encode(("pwd=" + value).encode()).decode().rstrip("=")
        cases.append(Case(f"label/b64/{i}", "labelled", "label_b64", "Z Best Media blob " + enc, (enc,), enc))
    for name in ("pin", "passphrase_words"):
        value = vals[name][0]
        for fname, text in label_forms("PIN" if name == "pin" else "seed phrase", value).items():
            cases.append(Case(f"{name}/{fname}", name, fname, "Z Best Media. " + text, (value,), value))
    # homoglyph labels: Cyrillic a / o inside the label
    for label in ("p\u0430ssword", "t\u043eken", "s\u0435cret", "\u0430pi key"):
        value = _r(lrng, 7, string.ascii_lowercase) + str(lrng.randint(10, 99))
        cases.append(Case(f"homoglyph/{label}", "labelled", "homoglyph_label", f"Z Best Media. {label}: {value}",
                          (value,), value))
    # review 98b76cde MUST 2: every label x every cased verb form (appended last: see ``cased_verb_forms``)
    crng = random.Random(seed + 2)
    for label in LABELS:
        for fname, text in cased_verb_forms(label, "VALUEPLACEHOLDER").items():
            value = _r(crng, 6, string.ascii_lowercase) + str(crng.randint(10, 99))
            cases.append(Case(f"label/{label}/{fname}", "labelled", fname, "Z Best Media. " + text.replace(
                "VALUEPLACEHOLDER", value), (value,), value))
    return cases


def url_cases(seed: int = 7) -> list:
    """URL placements of every no-label family: path segment, split across segments, query value, tracking
    parameter, fragment, user info, host label (when the value is a valid label)."""
    vals = values(random.Random(seed))
    out = []
    for fam, (value, kind) in vals.items():
        if kind == "labelled" or "\n" in value:
            continue
        q = quote(value, safe="")
        places = {"path": f"https://site.test/a/{q}/b", "query": f"https://site.test/p?x={q}",
                  "tracking": f"https://site.test/p?gclid={q}", "fragment": f"https://site.test/p#{q}",
                  "double_pct": f"https://site.test/p?x={quote(q, safe='')}",
                  "split_path": "https://site.test/" + "/".join(quote(value[i:i + 7], safe="")
                                                                for i in range(0, len(value), 7)),
                  "docs_position": f"https://docs.google.com/document/d/{q}/edit",
                  "code_position": f"https://github.com/o/r/commit/{q}"}
        if value.replace("-", "").isalnum() and len(value) <= 63:
            places["host_label"] = f"https://{value.lower()}.site.test/p"
        places["hex_label_path"] = "https://site.test/p/" + ("pwd=" + value[:6]).encode().hex()
        for pname, u in places.items():
            probes = _probes(value) + (tuple(x.lower() for x in _probes(value)) if pname == "host_label" else ())
            out.append(Case(f"url/{fam}/{pname}", fam, pname, u, probes, value))
    return out
