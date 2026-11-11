"""
Review of 2b26d980 (the sink guard refused LEGITIMATE values: availability). Every item of the review, with corpora
generated at run time (never a literal secret, e-mail address or number) and full flows.

MUST 1  Compliance ruling ids (``cmp-rul-`` + 26 base32) are a closed class; any other accepted shape is written as
        ``RULING_ID_HASHED`` + a keyed HMAC. Every externally issued identifier that reaches the log or the ledger
        is generated in its real format (10,000+ each) and none is refused.
MUST 2  Vault key ids (date-stamped ones too) are a closed class under ``key_id``; a key-shaped id refuses start.
SHOULD  real-world host names (punycode, platform hash labels, credential words), the Finance client id, the
        idempotent retry across the deploy, the operator vault's torn-tail repair, the export of old-format lines,
        a schedule naming a lost prompt set, the identifier mutation, "@"-less e-mail forms and folded prose.
NITs    nan / inf, ``dop_v1_``, logging ``extra=``.

Failures print names, classes and counts: never a value.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import json
import logging
import os
import random
import re
import secrets
import string
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

import config as config_mod
import fixture_server
import helpers
import log_sink
import models
import sink_guard as sg
import svc_tenants
import test_logs as tl
import test_sink_audits as sa
import vault as vault_mod
from agents import secret_guard
from clock import iso
from fixture_server import home_html
from gsc_fakes import FakeGoogle, write_key_file
from helpers import base_env, invoice_id, rid
from ledger import derived_id, payload_sha256
from ports import Ports, gsc_from_settings
from service import SeoService

SEED = 20261110
N_IDS = 10_000
SERVICES = Path(__file__).resolve().parents[2]            # services/ (other services are read for id formats only)
HEX = "0123456789abcdef"
ALNUM = string.ascii_lowercase + string.digits
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def clear_guard_caches() -> None:
    sg._CACHE.clear()
    sg._HOST_CACHE.clear()
    sg._KEYS.clear()


@pytest.fixture(autouse=True)
def _fresh_guard_caches():
    """A mutation test changes a rule: no verdict of another rule set may be remembered across it."""
    clear_guard_caches()
    yield
    clear_guard_caches()


# ================================================================================================ issuing formats

def compliance_b32(s: str, n: int = 26) -> str:
    """compliance-py ``service._b32``, to the letter."""
    return base64.b32encode(hashlib.sha256(s.encode("utf-8", "surrogatepass")).digest()).decode("ascii").lower()[:n]


def ruling_id(rng) -> str:
    """compliance-py ``service.py``: ``"cmp-rul-" + _b32(f"{principal}|{request_id}[|{outcome_sha}]")``."""
    parts = [rng.choice(("finance_31", "seo_2", "dashboard")), str(uuid.UUID(int=rng.getrandbits(128)))]
    if rng.random() < 0.5:
        parts.append(hashlib.sha256(str(rng.random()).encode()).hexdigest())
    return "cmp-rul-" + compliance_b32("|".join(parts))


def finance_rid(prefix: str, rng) -> str:
    """finance-py ``service.rid``: ``fin-<prefix>-`` + 26 Crockford base32 characters of a SHA-256."""
    d = hashlib.sha256(str(rng.random()).encode()).digest()
    n = int.from_bytes(d[:17], "big")
    out = []
    for _ in range(26):
        out.append(CROCKFORD[n & 31])
        n >>= 5
    return f"fin-{prefix}-" + "".join(out)


FINANCE_RID_PREFIXES = ("bat", "brk", "cb", "chk", "contact", "crc", "dec", "dec2", "dsp", "exc", "hof", "int", "inv",
                        "itm", "je", "mail", "mb", "off", "pay", "prop", "rc", "rcp", "rct", "rec", "review", "rfd",
                        "sft", "trx")
FINANCE_DERIVED_PREFIXES = ("aud", "bap2", "brk", "crcc", "hold", "invvoid", "rfdpd", "trset", "x", "wo")
JURISDICTIONS = ["US-" + a + b for a in string.ascii_uppercase for b in string.ascii_uppercase] + \
    ["PR", "GU", "VI", "AS", "MP"]


def hexs(rng, n: int) -> str:
    return "".join(rng.choice(HEX) for _ in range(n))


def digit_hex(rng, n: int) -> str:
    """A hex value that happens to hold digits only (what one SHA-256 prefix in about 1,800 is at 16 characters)."""
    return "".join(rng.choice(string.digits) for _ in range(n))


def service_id(rng) -> str:
    return derived_id(rng.choice(sorted(sg.SERVICE_ID_PREFIXES)), rng.random())


def request_key(rng) -> str:
    op = rng.choice(("audit", "schedule", "citation_run", "client_report", "log_chunk", "finance_client", "domains"))
    target = rng.choice(("acme", "zbm", "acme/" + derived_id("aud", rng.random()), derived_id("lgi", rng.random()),
                         "acme-corp"))
    return f"{op}|{target}|{uuid.UUID(int=rng.getrandbits(128))}"


def timestamp(rng) -> str:
    from datetime import datetime, timedelta, timezone
    return iso(datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=rng.randrange(0, 400_000_000),
                                                                     microseconds=rng.randrange(0, 1_000_000)))


def spend(rng) -> str:
    return str((Decimal(rng.randrange(0, 10 ** 12)) / Decimal(10 ** 6)).quantize(Decimal("0.000001")))


def client_reference(rng) -> str:
    """Client references as the other departments write them (finance-py's tests and fixtures: ``zbm-client-1``,
    ``client-7``, ``acme-party-1``): a slug with a short serial."""
    w = rng.choice(NEUTRAL_WORDS)
    k = rng.random()
    if k < 0.3:
        return f"zbm-client-{rng.randrange(1, 100000)}"
    if k < 0.5:
        return f"client-{rng.randrange(1, 100000)}"
    if k < 0.7:
        return f"{w}-party-{rng.randrange(1, 1000)}"
    if k < 0.85:
        return f"{w.capitalize()}_{rng.choice(NEUTRAL_WORDS).capitalize()}.{rng.randrange(1, 100)}"
    return f"{w}-{rng.choice(NEUTRAL_WORDS)}"


# name -> (issuer, format, the field it is written under, generator(rng), exhaustive values or None)
EXTERNAL = {
    "compliance_ruling_id": ("compliance-py service.py _b32 (lines ~84, 901, 946)", "cmp-rul- + 26 base32 [a-z2-7]",
                             "ruling_id", ruling_id, None),
    "compliance_jurisdiction": ("compliance-py activation_status (seo-py compliance_client.JURISDICTION)",
                                "US-[A-Z]{2} | PR | GU | VI | AS | MP", "jurisdiction", None, JURISDICTIONS),
    "finance_invoice_id": ("finance-py service.rid('inv', ...)", "fin-inv- + 26 Crockford base32", "invoice_id",
                           lambda r: finance_rid("inv", r), None),
    "finance_record_ids": ("finance-py service.rid (28 prefixes)", "fin-<prefix>- + 26 Crockford base32", "ref",
                           lambda r: finance_rid(r.choice(FINANCE_RID_PREFIXES), r), None),
    "finance_event_ids": ("finance-py ledger.derived_id", "fin-<prefix>- + 40 hex", "ref",
                          lambda r: f"fin-{r.choice(FINANCE_DERIVED_PREFIXES)}-" + hexs(r, 40), None),
    "finance_client_reference": ("not issued by Finance: a reference it accepts (models.CLIENT_ID_RE)",
                                 "[A-Za-z0-9._-]{1,100}; in use: <slug>-<serial>", "finance_client_id",
                                 client_reference, None),
    "ledger_event_id": ("seo-py ledger.derived_id (the ids ledger-rust stores)", "seo-evd|anc- + 40 hex", "event_id",
                        lambda r: derived_id(r.choice(("evd", "anc")), r.random()), None),
    "ledger_payload_sha256": ("ledger-rust / seo-py payload_sha256", "64 hex", "payload_sha256",
                              lambda r: hexs(r, 64), None),
    "service_id": ("seo-py ledger.derived_id (11 prefixes)", "seo-<prefix>- + 40 hex", "audit_id", service_id, None),
    "request_id": ("callers (models.REQUEST_ID)", "lower-case UUID", "request",
                   lambda r: str(uuid.UUID(int=r.getrandbits(128))), None),
    "request_key": ("seo-py SeoService.rk", "<op>|<target>|<uuid>", "request_id", request_key, None),
    "log_epoch": ("seo-py store (first 16 hex of a line SHA-256)", "16 hex, digits-only included", "epoch",
                  lambda r: digit_hex(r, 16) if r.random() < 0.5 else hexs(r, 16), None),
    "log_key_fingerprint": ("seo-py svc_logs (first 16 hex of the key's SHA-256)", "16 hex, digits-only included",
                            "key_fp", lambda r: digit_hex(r, 16) if r.random() < 0.5 else hexs(r, 16), None),
    "vault_record_id": ("seo-py vault (128 random bits)", "32 hex, digits-only included", "vault_id",
                        lambda r: digit_hex(r, 32) if r.random() < 0.3 else hexs(r, 32), None),
    "vault_repair_id": ("seo-py vault (32 hex of a SHA-256)", "32 hex, digits-only included", "repair_id",
                        lambda r: digit_hex(r, 32) if r.random() < 0.3 else hexs(r, 32), None),
    "vault_ref": ("seo-py vault.ref / blob_ref", "seo-vlt|blb- + 32 hex + 8 hex check", "content_ref",
                  lambda r: (vault_mod.ref if r.random() < 0.5 else vault_mod.blob_ref)(hexs(r, 32)), None),
    "keyed_hmac": ("seo-py vault.field_hash", "64 hex", "report_hmac", lambda r: hexs(r, 64), None),
    "timestamp": ("seo-py clock.iso", "YYYY-MM-DDTHH:MM:SS[.f]Z", "at", timestamp, None),
    "gsc_property_host": ("Search Console property (gsc_client.property_host)", "a host name", "property_host",
                          lambda r: neutral_host(r), None),
    "gsc_window_date": ("Search Console read window", "YYYY-MM-DD", "window_start",
                        lambda r: timestamp(r)[:10], None),
    "engine_model_id": ("engine adapters' allowlists (sink_guard.MODEL_IDS)", "a closed set", "model", None,
                        sorted(sg.MODEL_IDS)),
    "engine_spend": ("seo-py svc_engines (str of a Decimal)", "N.NNNNNN", "spend_usd", spend, None),
}


# ================================================================================================ host corpora

NEUTRAL_WORDS = """acme north south river stone bright cloud forest harbor summit valley maple cedar orchid falcon tiger
pixel quantum rocket garden kitchen coffee bakery studio design market trade supply solar wind ocean coast metro urban
rural happy lucky prime alpha beta gamma delta omega nova stellar lunar cosmic digital media labs works group global
local direct express smart swift rapid fresh green blue red gold silver iron steel copper glass paper print book story
travel hotel pizza burger sushi taco yoga fitness health dental legal tax bank loan insure auto motor bike cycle shoe
shirt dress jewel watch phone laptop cable router server host web net app data code craft build repair clean plumbing
roof paint floor door window home house villa estate realty farm ranch vine wine beer brew tea spice salt sugar
honey""".split()
TLDS = ("com net org io co us uk de fr ca au in jp es it nl shop store app dev ai tech online site info biz xyz me tv "
        "agency studio design cloud digital media").split()
SECOND_LEVEL = ("co.uk", "com.au", "co.jp", "com.br", "co.nz", "com.mx")
SCRIPTS = {
    "german": "abcdefghiklmnoprstuw\u00e4\u00f6\u00fc", "french": "abcdefgilmnoprstuv\u00e9\u00e8\u00ea\u00e0\u00e7",
    "spanish": "abcdefgilmnoprstuv\u00f1\u00e1\u00e9\u00ed\u00f3\u00fa",
    "polish": "abcdefgijklmnoprstuwyz\u0105\u0107\u0119\u0142\u0144\u00f3\u015b\u017a\u017c",
    "turkish": "abcdefghijklmnoprstuvyz\u00e7\u011f\u00f6\u015f\u00fc",
    "vietnamese": "abcdeghiklmnopqrstuvxy\u0103\u00e2\u0111\u00ea\u00f4\u01a1\u01b0",
    "russian": "".join(chr(c) for c in range(0x430, 0x450)), "greek": "".join(chr(c) for c in range(0x3b1, 0x3c2)),
    "hebrew": "".join(chr(c) for c in range(0x5d0, 0x5eb)), "arabic": "".join(chr(c) for c in range(0x627, 0x63b)),
    "hindi": "".join(chr(c) for c in range(0x915, 0x93a)), "thai": "".join(chr(c) for c in range(0xe01, 0xe2f)),
    "korean": "".join(chr(c) for c in range(0xac00, 0xac00 + 400, 7)),
    "japanese": "".join(chr(c) for c in range(0x3042, 0x3094)),
    "chinese": "".join(chr(c) for c in range(0x4e00, 0x4e00 + 600, 5)),
}
IDN_TLDS = ["com", "de", "net", "org", "ru", "jp", "cn", "kr", "fr", "es"] + \
    ["xn--" + t.encode("punycode").decode("ascii") for t in ("\u0440\u0444", "\u4e2d\u56fd", "\u0443\u043a\u0440",
                                                             "\u0441\u0440\u0431", "\u0431\u0435\u043b",
                                                             "\ud55c\uad6d", "\u03b5\u03bb", "\u0440\u0443\u0441")]
PLATFORMS = ("webflow.io", "ngrok.io", "ngrok-free.app", "mybigcommerce.com", "vercel.app", "netlify.app",
             "herokuapp.com", "pages.dev", "cloudfront.net", "amplifyapp.com", "azurewebsites.net", "web.app",
             "onrender.com", "fly.dev", "myshopify.com", "github.dev", "repl.co", "trycloudflare.com", "workers.dev",
             "up.railway.app")
LABEL_WORDS = ("password", "passwd", "pass", "pin", "secret", "secrets", "token", "tokens", "auth", "apikey", "key",
               "cookie", "cookies", "credential", "credentials", "bearer", "otp", "passcode", "passphrase", "sid",
               "pwd", "mnemonic", "privatekey", "accesstoken", "passwort", "clave")


def neutral_host(r) -> str:
    k = r.random()
    w = r.sample(NEUTRAL_WORDS, 3)
    if k < .35:
        name = w[0] + w[1]
    elif k < .6:
        name = w[0] + "-" + w[1]
    elif k < .7:
        name = w[0] + str(r.randint(1, 9999))
    elif k < .78:
        name = str(r.randint(1, 999)) + w[0]
    elif k < .86:
        name = "-".join(w)
    elif k < .92:
        name = w[0] + w[1] + str(r.choice((24, 247, 365, 2024, 2025, 2026, 101, 360)))
    else:
        name = w[0]
    sub = r.choice(("", "", "", "www.", "shop.", "blog.", "app.", "m.", "store.", "en.", "us.", "my.", "api."))
    tld = r.choice(SECOND_LEVEL) if r.random() < .08 else r.choice(TLDS)
    return sub + name + "." + tld


def idn_host(r) -> str:
    script = r.choice(sorted(SCRIPTS))
    letters = SCRIPTS[script]
    n = r.randint(2, 8) if script in ("chinese", "japanese", "korean") else r.randint(3, 14)
    u = "".join(r.choice(letters) for _ in range(n))
    if u.isascii():
        u += r.choice([c for c in letters if not c.isascii()])
    if r.random() < .15:
        u += "-" + "".join(r.choice(letters) for _ in range(r.randint(2, 6)))
    label = "xn--" + u.encode("punycode").decode("ascii")
    return r.choice(("", "", "www.", "shop.")) + label + "." + r.choice(IDN_TLDS)


def platform_host(r) -> str:
    alphabet = HEX if r.random() < .5 else ALNUM
    h = "".join(r.choice(alphabet) for _ in range(r.randint(6, 20)))
    k = r.random()
    if k < .4:
        label = h
    elif k < .7:
        label = r.choice(NEUTRAL_WORDS) + "-" + h
    elif k < .85:
        label = r.choice(NEUTRAL_WORDS) + "-" + r.choice(NEUTRAL_WORDS) + "-" + h
    else:
        label = h + "-" + r.choice(NEUTRAL_WORDS)
    return label + "." + r.choice(PLATFORMS)


def label_word_host(r) -> str:
    w, o, k = r.choice(LABEL_WORDS), r.choice(NEUTRAL_WORDS), r.random()
    if k < .2:
        label = w
    elif k < .4:
        label = w + o
    elif k < .55:
        label = o + w
    elif k < .7:
        label = w + "-" + o
    elif k < .8:
        label = o + "-" + w
    elif k < .9:
        label = str(r.randint(1, 9)) + w
    else:
        label = w + str(r.choice((0, 1, 2, 4, 24, 360, 2049)))
    return r.choice(("", "", "www.", "app.", "my.")) + label + "." + r.choice(TLDS)


def named_label_word_hosts() -> list:
    """The hosts the review names, built from their parts (a credential word and a neutral word)."""
    return ["1" + "pass" + "word.com", "pin.it", "pass" + "word-manager.com", "sec" + "ret-sale.shop",
            "pass" + "wordstate.com", "api" + "key.dev", "tok" + "enmetrics.io"]


def digits(n: int) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(n))


def token(n: int, alphabet: str = ALNUM) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


def mixed_token(n: int) -> str:
    """A random letters-and-digits token: a letter beyond ``f`` (not a hex run), 4..10 digits, interleaved."""
    while True:
        t = token(n)
        if any(c in "ghijklmnopqrstuvwxyz" for c in t) and 4 <= sum(c.isdigit() for c in t) <= 10 \
                and sg._changes(t) >= 4 and not any(t[i:i + 9].isdigit() for i in range(len(t) - 8)):
            return t


def letters(n: int) -> str:
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(n))


SECRET_HOSTS = {
    # strong-key prefixes as labels
    "prefix_openai_project": lambda: "sk-proj-" + token(24) + ".example.com",
    "prefix_github": lambda: "ghp" + token(36) + ".example.com",
    "prefix_aws_id": lambda: "akia" + token(16) + ".example.com",
    "prefix_slack": lambda: "xoxb-" + token(20) + ".example.com",
    "prefix_perplexity": lambda: "pplx-" + token(24) + ".example.com",
    "prefix_gitlab": lambda: "glpat-" + token(20) + ".example.com",
    # hex labels
    "hex_32": lambda: secrets.token_hex(16) + ".example.com",
    "hex_40": lambda: secrets.token_hex(20) + ".example.com",
    "hex_63_of_64": lambda: secrets.token_hex(32)[:63] + ".example.com",
    "hex_64_over_two_labels": lambda: secrets.token_hex(16) + "." + secrets.token_hex(16) + ".example.com",
    "hex_32_after_a_word": lambda: "key-" + secrets.token_hex(16) + ".example.com",
    # long random tokens
    "token_32": lambda: mixed_token(32) + ".example.com",
    "token_40": lambda: mixed_token(40) + ".example.com",
    # card / SSN / phone digit labels
    "card_16": lambda: "4" + digits(15) + ".example.com",
    "card_dashed": lambda: "-".join(digits(4) for _ in range(4)) + ".example.com",
    "card_dotted": lambda: ".".join(digits(4) for _ in range(4)) + ".example.com",
    "ssn_9": lambda: digits(9) + ".example.com",
    "ssn_dashed": lambda: "-".join((digits(3), digits(2), digits(4))) + ".example.com",
    "phone_10": lambda: digits(10) + ".example.com",
    "phone_dashed": lambda: "-".join((digits(3), digits(3), digits(4))) + ".example.com",
    "phone_after_a_word": lambda: "tel" + digits(10) + ".example.com",
    # e-mail forms without "@"
    "email_at_label": lambda: ".".join((letters(5), letters(6), "at", letters(7), secrets.choice(("com", "org")))),
    "email_mailbox_provider": lambda: ".".join((letters(5), letters(6), secrets.choice(sorted(sg.MAILBOX_PROVIDERS)))),
    # the value of a credential label
    "label_value_in_label": lambda: "pass" + "word-" + letters(4) + digits(3) + ".example.com",
    "label_value_next_label": lambda: "tok" + "en." + letters(4) + digits(4) + ".shop.example.com",
}


def undecodable_label() -> str:
    """An ``xn--`` label of letters that is not valid punycode for a label of letters."""
    while True:
        label = "xn--" + letters(secrets.choice((3, 5, 8)))
        if not sg._idna_label_ok(label):
            return label


def refusal_rate(gen, n: int, seed: int) -> tuple:
    r = random.Random(seed)
    reasons = collections.Counter()
    for _ in range(n):
        reason = sg.host_refusal(gen(r))
        if reason is not None:
            reasons[reason] += 1
    return sum(reasons.values()), dict(reasons)


# ================================================================================================ MUST 1: the audit

def test_the_issuing_formats_are_the_other_services_own():
    """The formats above are read from the issuing services (read-only): if one changes, this fails."""
    cmp_src = SERVICES / "compliance-py" / "src" / "service.py"
    fin_src = SERVICES / "finance-py" / "src" / "service.py"
    fin_models = SERVICES / "finance-py" / "src" / "models.py"
    if not (cmp_src.exists() and fin_src.exists() and fin_models.exists()):
        pytest.skip("the issuing services' sources are not next to this one (a single-service checkout)")
    text = cmp_src.read_text(encoding="utf-8")
    assert 'return base64.b32encode(hashlib.sha256(s.encode("utf-8", "surrogatepass")).digest()).decode("ascii")' \
        '.lower()[:n]' in text and "def _b32(s: str, n: int = 26) -> str:" in text
    issued = re.findall(r'"cmp-rul-" \+ _b32\(', text)
    assert len(issued) >= 3 and len(re.findall(r'"cmp-rul-', text)) == len(issued)      # no other ruling id shape
    text = fin_src.read_text(encoding="utf-8")
    assert 'return f"fin-{prefix}-" + "".join(out)' in text and "for _ in range(26):" in text
    assert set(re.findall(r'rid\("([a-z0-9]+)"', text)) <= set(FINANCE_RID_PREFIXES) | {"x"}
    text = fin_models.read_text(encoding="utf-8")
    assert 'OWN_ID_RE = re.compile(r"fin-[a-z][a-z0-9]{0,7}-(?:[0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{40})")' in text
    assert 'CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")' in text


@pytest.mark.parametrize("name", sorted(EXTERNAL))
def test_no_externally_issued_identifier_is_refused(name):
    issuer, fmt, field, gen, closed = EXTERNAL[name]
    rng = random.Random(f"{SEED}:{name}")
    values = list(closed) if closed is not None else [gen(rng) for _ in range(N_IDS)]
    assert closed is not None or len(set(values)) >= N_IDS * 0.9 or name in ("gsc_window_date",)
    refused = collections.Counter()
    for v in values:
        for sink in ("audit_requested", sg.LEDGER + "audit_requested"):
            found = sg.violations(sink, {field: v, "nested": {"rows": [{field: v}]}})
            if found:
                refused[found[0][1]] += 1
                break
    print(f"external-id {name}: issuer={issuer!r} format={fmt!r} generated={len(values)} refused={sum(refused.values())}")
    assert not refused, (name, len(values), dict(refused))


def test_the_ruling_id_class_is_exactly_the_issued_shape():
    rng = random.Random(SEED)
    ids = [ruling_id(rng) for _ in range(N_IDS)]
    assert all(sg.classify(x) == "ruling_id" for x in ids)
    # the availability bug: the strict check refuses the ones holding 9+ scattered digits (about 1 in 80)
    strict = sum(1 for x in ids if secret_guard.check(x) is not None)
    print(f"ruling ids: generated={len(ids)} refused_by_the_strict_check={strict} refused_by_the_guard=0")
    assert strict > 0
    body = compliance_b32("x")
    near = ["cmp-rul-" + body[:25], "cmp-rul-" + body + "a", "cmp-rul-" + body.upper(), "cmp-rul-" + body[:25] + "1",
            "cmp-rul-" + body[:25] + "8", "cmp-rul-" + body[:25] + "0", "cmp-rul_" + body, "cmp-rux-" + body,
            "Cmp-rul-" + body, "cmp-rul-" + digits(26).replace("2", "9") + "", "xcmp-rul-" + body]
    assert all(sg.classify(x) != "ruling_id" for x in near)
    # a digit-heavy id of another shape is not a machine value at all
    heavy = "cmp-rul-" + "".join(secrets.choice("01899") for _ in range(26))
    assert sg.classify(heavy) is None and sg.violations("job_ran", {"ruling_id": heavy})


def test_the_finance_id_class_is_exactly_what_finance_issues():
    rng = random.Random(SEED + 1)
    assert all(sg.classify(finance_rid("inv", rng)) == "invoice_id" for _ in range(2000))
    for p in FINANCE_RID_PREFIXES:
        if p != "inv":
            assert sg.classify(finance_rid(p, rng)) == "finance_id", p
    body = finance_rid("crc", rng)[8:]
    for bad in ("fin-crc-" + body[:25], "fin-crc-" + body.lower(), "fin-CRC-" + body, "fin-toolongpre-" + body,
                "fin--" + body, "fin-1ab-" + body, "fin-crc-" + body[:25] + "I", "fin-crc-" + hexs(rng, 39),
                "fin-crc-" + hexs(rng, 40).upper(), "fan-crc-" + body):
        assert sg.classify(bad) not in ("finance_id", "invoice_id"), len(bad)
    assert sg.classify("invoice:" + finance_rid("inv", rng)) == "scoped_id"


CLASS_MUTATIONS = {
    # name -> (the mutation, a value of the class holding 9+ digits or a credential word, its field)
    "ruling_id": (lambda m: m.setattr(sg, "_is_ruling_id", lambda s: False), None, "ruling_id"),
    "finance_id": (lambda m: m.setattr(sg, "_is_finance_id", lambda s: False), None, "ref"),
    "jurisdiction": (lambda m: m.setattr(sg, "_is_jurisdiction", lambda s: False), "US-PW", "jurisdiction"),
    "amount": (lambda m: m.setattr(sg, "_is_amount", lambda s: False), "100.250000", "spend_usd"),
    "key_fp": (lambda m: m.setattr(sg, "DIGIT_HEX_KEYS", sg.DIGIT_HEX_KEYS - {"key_fp"}), None, "key_fp"),
}


def _class_value(name: str) -> str:
    rng = random.Random(secrets.randbits(64))
    if name == "ruling_id":
        return digit_heavy_ruling_id()
    if name == "finance_id":
        while True:
            x = finance_rid("crc", rng)
            if secret_guard.check(x) is not None:
                return x
    if name == "key_fp":
        return digit_hex(rng, 16)
    return CLASS_MUTATIONS[name][1]


@pytest.mark.parametrize("name", sorted(CLASS_MUTATIONS))
def test_each_new_class_bites(name, monkeypatch):
    """Each closed class is what lets its value through: without it the value is refused (the review's finding)."""
    mutate, _fixed, field = CLASS_MUTATIONS[name]
    value = _class_value(name)
    for sink in ("engine_run_settled", sg.LEDGER + "engine_run_settled"):
        assert sg.violations(sink, {field: value}) == [], name
    with monkeypatch.context() as m:
        mutate(m)
        clear_guard_caches()
        assert [p for p, _r in sg.violations("engine_run_settled", {field: value})] == ["/" + field], name
    clear_guard_caches()
    assert sg.violations("engine_run_settled", {field: value}) == [], name


def test_the_amount_and_jurisdiction_classes_are_exact():
    for ok in ("0.000000", "100.000000", "999999.999999", "12.500000"):
        assert sg.classify(ok) == "amount", ok
    assert sg.violations("engine_run_settled", {"spend_usd": "100.000000", "reserved_usd": "2500.500000",
                                                "days": {"2026-10-09": {"calls": 3, "spend_usd": "250.000001"}}}) == []
    for bad in ("1000000.000000", "100.0000000", "100.00000", ".000000", "100,000000", "1e3.000000",
                "-100.000000", "100.000000.1", digits(4) + "." + digits(4) + "." + digits(4)):
        assert sg.classify(bad) != "amount", len(bad)
    assert sg.classify(digits(10)) is None and sg.classify(digits(6) + "." + digits(7)) is None
    assert all(sg.classify(j) in ("jurisdiction", "code") for j in JURISDICTIONS)
    for bad in ("US-pw", "us-PW", "US-P", "US-PWX", "US_PW", "UK-PW", "US-P1"):
        assert sg.classify(bad) != "jurisdiction", bad


# ================================================================================================ worlds

class Compliance(helpers.PassingCompliance):
    """Compliance answering ONE ruling id for every client (the id under test)."""

    def __init__(self, ruling: str):
        super().__init__()
        self.ruling = ruling

    def activation(self, client_id):
        out = super().activation(client_id)
        return {**out, "ruling_id": self.ruling}


def digit_heavy_ruling_id() -> str:
    """A real-format ruling id holding 9+ digits that the strict check refuses (``e_digits``): what one id in about
    80 looks like, found by drawing ids as Compliance derives them."""
    rng = random.Random(secrets.randbits(64))
    for _ in range(200_000):
        x = ruling_id(rng)
        if sum(c.isdigit() for c in x) >= 9 and secret_guard.check(x) == "e_digits":
            return x
    raise AssertionError("no digit-heavy ruling id in 200,000 draws")


def idn_label(rng, script: str = "german") -> str:
    lettersx = SCRIPTS[script]
    u = "".join(rng.choice(lettersx) for _ in range(8)) + [c for c in lettersx if not c.isascii()][0]
    return "xn--" + u.encode("punycode").decode("ascii")


def site(srv, host: str) -> None:
    srv.text(host, "/robots.txt", f"User-agent: *\nAllow: /\nSitemap: http://{host}/sitemap.xml\n")
    srv.text(host, "/sitemap.xml", "<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/"
             f"schemas/sitemap/0.9'><url><loc>http://{host}/</loc></url><url><loc>http://{host}/about</loc></url>"
             "</urlset>", ctype="application/xml")
    srv.text(host, "/llms.txt", f"# Z Best Media\n\n> Revenue recovery.\n\n## Pages\n- [Home](http://{host}/): start\n")
    srv.html(host, "/", home_html(canonical=f"http://{host}/"))
    srv.html(host, "/about", home_html(title="About Z Best Media", canonical=f"http://{host}/about"))


def fetcher_for(srv, hosts):
    names = set(hosts) | set(fixture_server.NAMES)
    fetch_mod = fixture_server.fetch_mod

    def allow(host: str, ip: str, port: int) -> bool:
        if host in names and ip == "127.0.0.1" and port in (srv.port, 80):
            return True
        return fetch_mod.default_policy(host, ip, port)
    return srv.fetcher(transport=fixture_server.PortRewrite(srv.port), policy=allow,
                       resolver=srv.resolver(extra={n: ["127.0.0.1"] for n in names}))


def accepted_host(make) -> str:
    """A generated host the host rule takes (the measured false-refusal rate of each corpus is asserted on its own:
    a flow must not fail one run in a hundred on it)."""
    for _ in range(100):
        host = make()
        if sg.host_refusal(host) is None:
            return host
    raise AssertionError("no accepted host in 100 draws")


class World:
    """One durable service whose tenant ``acme`` owns an IDN domain, a platform-hash domain and a label-word domain,
    with Compliance issuing a digit-heavy ruling id and a date-stamped vault key id."""

    def __init__(self, tmp_path, srv, key_id: str = "2026-11", ruling: str | None = None):
        rng = random.Random(secrets.randbits(64))
        self.idn = accepted_host(lambda: idn_label(rng) + ".test")
        self.hashed = accepted_host(lambda: rng.choice(NEUTRAL_WORDS) + "-" + hexs(rng, 12) + ".platform.test")
        self.worded = "1" + "pass" + "word-" + rng.choice(NEUTRAL_WORDS) + ".test"
        self.domains = [self.idn, self.hashed, self.worded]
        self.competitors = [accepted_host(lambda: idn_label(rng, "russian") + ".example"),
                            accepted_host(lambda: "shop-" + hexs(rng, 10) + ".webflow.io"),
                            "pass" + "word-manager.example"]
        for d in self.domains:
            site(srv, d)
        self.ruling = ruling or digit_heavy_ruling_id()
        tmp_path.mkdir(parents=True, exist_ok=True)
        keyfile = write_key_file(tmp_path / "sa.json")
        self.fake = FakeGoogle(prop="sc-domain:" + self.idn)
        self.fake.pages = [(f"http://{self.idn}/", 30, 300, 4.0), (f"http://{self.idn}/about", 10, 200, 9.0)]
        self.fake.queries = [("revenue recovery", 20, 100, 2.0), ("marketplace fees", 10, 150, 8.0)]
        settings = config_mod.load(base_env(SEO_GSC_SERVICE_ACCOUNT_FILE=keyfile))
        ports = Ports.default(settings)
        ports.fetcher = fetcher_for(srv, self.domains)
        ports.engines["openai"] = sa.Engine()
        ports.compliance = Compliance(self.ruling)
        fc = self.fake.clock
        ports.first_party["search_console"] = gsc_from_settings(settings, transport=self.fake.transport(),
                                                                monotonic=fc.mono, wall=fc.wall, sleep=fc.sleep)
        self.vault_file = helpers.write_vault_key(tmp_path / "vault.key")
        self.h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data"), ports=ports,
                                 SEO_GSC_SERVICE_ACCOUNT_FILE=keyfile, SEO_PROBE_SAMPLES="3",
                                 SEO_VAULT_KEY_FILE=self.vault_file, SEO_VAULT_KEY_ID=key_id,
                                 SEO_GSC_READS_PER_DAY="48")
        self.srv = srv
        self.ids: dict = {}

    def setup(self) -> None:
        h = self.h
        h.tenant("acme", "client", tuple(self.domains))
        h.ok(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": "acme-party-1"},
                    andre=True))
        ps = h.ok(h.post("/tenants/acme/prompt-sets", {
            "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"],
            "competitor_domains": self.competitors, "prompts": ["who recovers marketplace revenue"],
            "engines": ["openai"]}, caller="dashboard"), 201)
        self.ids["psid"] = ps["prompt_set_id"]
        fields = [{"field": "name", "value": "Acme Rug Care", "source": "founder_statement",
                   "provenance": "stated by the founder", "authority": "first_party"},
                  {"field": "telephone", "value": "(562) 555-0142", "source": "founder_statement",
                   "provenance": "stated", "authority": "first_party"}]
        self.ids["eid"] = h.ok(h.post("/tenants/acme/entity", {"request_id": rid(), "fields": fields},
                                      andre=True), 201)["entity_id"]

    def audit(self, domain: str, n: int, code: int = 201):
        h = self.h
        return h.post("/tenants/acme/audits", {
            "request_id": rid(), "domain": domain, "scheme": "http", "paths": ["/", "/about"],
            "invoice_id": invoice_id(n), "prompt_set_id": self.ids["psid"]}, caller="dashboard", andre=True)

    def flow(self) -> None:
        """tenant -> domains -> prompt set -> entity -> audit -> client report -> citation run -> schedule and
        ticks -> log ingest -> Search Console."""
        h, ids = self.h, self.ids
        self.setup()
        h.ok(h.post("/tenants/acme/gsc-property", {"request_id": rid(), "property": "sc-domain:" + self.idn},
                    andre=True))
        a = h.ok(self.audit(self.idn, 1), 201)
        assert a["status"] == "completed", a["status"]
        ids["aid"] = a["audit_id"]
        h.ok(sa.client_report(h, "acme", ids["aid"]))
        run = h.ok(h.post("/tenants/acme/citation-runs", {"request_id": rid(), "domain": self.worded,
                                                          "prompt_set_id": ids["psid"], "engines": ["openai"]},
                          caller="dashboard", andre=True), 201)
        assert run["status"] == "completed", run["status"]
        ids["cid"] = run["citation_run_id"]
        s = h.ok(h.post("/tenants/acme/schedules", {
            "request_id": rid(), "domain": self.hashed, "scheme": "http", "every_days": 7,
            "invoice_id": invoice_id(2), "paths": ["/", "/about"], "prompt_set_id": ids["psid"]},
            caller="dashboard", andre=True), 201)
        ids["sid"] = s["schedule_id"]
        t = sa.tick(h)
        assert t["ran"] == 1, {k: v for k, v in t.items() if isinstance(v, int)}
        h.clock.advance(days=7)
        self.srv.html(self.hashed, "/", home_html(canonical=None))
        t = sa.tick(h)
        assert t["ran"] == 1 and t["drift_recorded"] == 1, {k: v for k, v in t.items() if isinstance(v, int)}
        iid = h.ok(tl.ingest(h, tid="acme", caller="dashboard", domain=self.hashed), 201)["ingest_id"]
        lines = [f'66.249.66.{i} - - [09/Oct/2026:17:00:0{i} +0000] "GET /about HTTP/1.1" 200 5 "-" '
                 f'"Mozilla/5.0 (compatible; Googlebot/2.1)"' for i in range(1, 6)]
        h.ok(tl.send(h, iid, 1, base64.b64encode(("\n".join(lines) + "\n").encode()).decode(), tid="acme",
                     caller="dashboard"))
        fin = h.ok(h.post(f"/tenants/acme/log-ingests/{iid}/finish", {"request_id": rid()}, caller="dashboard"))
        assert fin["status"] == "finished" and fin["report"] is not None
        ids["iid"] = iid


def log_records(h) -> list:
    return list(h.svc.log.iter_records())


def all_strings(x, out=None) -> list:
    out = [] if out is None else out
    if isinstance(x, str):
        out.append(x)
    elif isinstance(x, dict):
        for k, v in x.items():
            all_strings(k, out)
            all_strings(v, out)
    elif isinstance(x, (list, tuple)):
        for v in x:
            all_strings(v, out)
    return out


def stub_key_ids(h) -> set:
    out = set()

    def walk(x):
        if isinstance(x, dict):
            if set(x) >= {"vault_id", "key_id", "hmac"}:
                out.add(x["key_id"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for r in log_records(h):
        walk(r["data"])
    return out


# ================================================================================================ the full flow

@pytest.mark.local_http
def test_the_full_flow_with_a_digit_heavy_ruling_id_date_stamped_key_ids_and_real_world_domains(tmp_path, srv,
                                                                                               caplog):
    w = World(tmp_path, srv, key_id="2026-11")
    assert sum(c.isdigit() for c in w.ruling) >= 9 and secret_guard.check(w.ruling) == "e_digits"
    assert sg.classify(w.idn) == sg.classify(w.hashed) == sg.classify(w.worded) == "hostname"
    w.flow()
    h = w.h
    # the ruling id is in the log and on the ledger as Compliance issued it
    lines = log_records(h)
    strings = all_strings([r["data"] for r in lines])
    assert strings.count(w.ruling) >= 4 and sg.RULING_ID_HASHED not in strings
    confirmed = sa.ledger_payloads(h, "compliance_activation_confirmed")
    assert len(confirmed) >= 3 and all(p["ruling_id"] == w.ruling and "ruling_id_hmac" not in p for p in confirmed)
    # every line and every ledger payload holds machine values only, and the stubs carry the date-stamped key id
    assert all(sg.violations(r["kind"], r["data"]) == [] for r in lines)
    assert all(sg.violations(sg.LEDGER + e["event_type"], e["_payload"]) == [] for e in h.ledger.events)
    assert stub_key_ids(h) == {"2026-11"}
    kinds = {r["kind"] for r in lines}
    assert {"audit_requested", "audit_completed", "citation_run_requested", "citation_run_recorded",
            "schedule_created", "drift_recorded", "log_ingest_finished", "gsc_read_started",
            "client_report_rendered", "tenant_gsc_property_set", "entity_created", "prompt_set_version"} <= kinds, \
        sorted(kinds)
    assert not [r for r in caplog.records if "sink guard" in str(r.msg)]
    health = h.svc.health()
    assert health["legacy_prose_lines"] == 0 and health["status"] == "ok"
    # the rotation: a key id carrying a full date and a serial, the old one as the previous key
    new_file = helpers.write_vault_key(tmp_path / "vault2.key")
    h2 = h.restart(SEO_VAULT_KEY_FILE=new_file, SEO_VAULT_KEY_ID="k-20261110-01",
                   SEO_VAULT_KEY_PREVIOUS_FILE=w.vault_file, SEO_VAULT_KEY_PREVIOUS_ID="2026-11")
    w.h = h2
    assert h2.svc.integrity["ok"] and h2.svc.health()["legacy_prose_lines"] == 0
    a2 = h2.ok(w.audit(w.worded, 3), 201)
    assert a2["status"] == "completed"
    assert h2.ok(h2.get(f"/tenants/acme/audits/{w.ids['aid']}"))["report"] is not None       # read under the old key
    h2.ok(h2.post("/tenants/acme/schedules", {
        "request_id": rid(), "domain": w.idn, "scheme": "http", "every_days": 30, "invoice_id": invoice_id(4),
        "paths": ["/"], "prompt_set_id": w.ids["psid"]}, caller="dashboard", andre=True), 201)
    assert stub_key_ids(h2) == {"2026-11", "k-20261110-01"}
    assert all(sg.violations(r["kind"], r["data"]) == [] for r in log_records(h2))
    export = h2.ok(h2.get("/audit/export", caller="compliance_38", params={"since": 1, "limit": 1000}))
    assert export["withheld"] == 0 and len(export["events"]) == len(log_records(h2))
    served = all_strings(export["events"])
    assert w.ruling in served and "2026-11" in served and "k-20261110-01" in served and sg.WITHHELD not in served
    h3 = h2.restart(SEO_VAULT_KEY_FILE=new_file, SEO_VAULT_KEY_ID="k-20261110-01",
                    SEO_VAULT_KEY_PREVIOUS_FILE=w.vault_file, SEO_VAULT_KEY_PREVIOUS_ID="2026-11")
    assert h3.svc.integrity["ok"] and len(h3.svc.audits) == len(h2.svc.audits)
    assert h3.ok(h3.get(f"/tenants/acme/audits/{a2['audit_id']}"))["report"] is not None


# ================================================================================================ MUST 1: ruling ids

def _client_world(tmp_path, srv, ruling: str, **env) -> helpers.Harness:
    ports = Ports.default()
    ports.fetcher = srv.site_fetcher()
    ports.engines["openai"] = sa.Engine()
    ports.compliance = Compliance(ruling)
    tmp_path.mkdir(parents=True, exist_ok=True)
    fixture_server.install_site(srv)
    h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data"), ports=ports, SEO_PROBE_SAMPLES="3", **env)
    h.tenant("acme", "client", (sa.DOMAIN,))
    h.ok(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": "acme-party-1"},
                andre=True))
    return h


def _paid_audit(h, n: int = 1):
    return h.post("/tenants/acme/audits", {"request_id": rid(), "domain": sa.DOMAIN, "scheme": "http",
                                           "paths": ["/"], "invoice_id": invoice_id(n)},
                  caller="dashboard", andre=True)


@pytest.mark.local_http
def test_a_ruling_id_of_another_accepted_shape_is_written_as_a_code_and_a_keyed_hmac(tmp_path, srv):
    import compliance_client
    other = "RUL." + digits(12) + ":" + letters(6) + "-" + digits(4)           # RULING_ID takes it; nobody issues it
    assert compliance_client.RULING_ID.fullmatch(other) and sg.classify(other) is None
    h = _client_world(tmp_path, srv, other)
    a = h.ok(_paid_audit(h), 201)
    assert a["status"] == "completed"
    run_body = {"request_id": rid(), "domain": sa.DOMAIN, "scheme": "http", "every_days": 7, "paths": ["/"],
                "invoice_id": invoice_id(2)}
    h.ok(h.post("/tenants/acme/schedules", run_body, caller="dashboard", andre=True), 201)
    assert sa.tick(h)["ran"] == 1
    mac = h.svc.vault.field_hash("acme", "compliance_ruling_id", other)[1]
    payloads = sa.ledger_payloads(h, "compliance_activation_confirmed")
    assert len(payloads) >= 3
    assert all(p["ruling_id"] == sg.RULING_ID_HASHED and p["ruling_id_hmac"] == mac for p in payloads)
    blob = sa.written(h)
    assert other not in blob and other.lower() not in blob.lower()
    assert hashlib.sha256(other.encode()).hexdigest() not in blob                # keyed: never a plain hash of it
    cmp_ = [r["data"]["invoice_verification"]["compliance"] for r in sa.records(h, "audit_requested")]
    assert cmp_ and all(c["ruling_id"] == sg.RULING_ID_HASHED and c["ruling_id_hmac"] == mac for c in cmp_)
    # the same id for another tenant hashes differently (bound to the tenant), and the same id again hashes the same
    assert h.svc._ruling_fields({"ruling_id": other}, "zbm")["ruling_id_hmac"] != mac
    assert h.svc._ruling_fields({"ruling_id": other}, "acme")["ruling_id_hmac"] == mac
    assert h.svc._ruling_fields({"ruling_id": None}, "acme") == {"ruling_id": None}


@pytest.mark.local_http
def test_mutation_without_the_ruling_class_the_digit_heavy_id_stops_paid_runs(tmp_path, srv, monkeypatch):
    """The bug the review found, reproduced by removing the fix: the id is refused at the sink (503), the audit and
    the schedule are not created. With the class in place the same calls answer 201."""
    ruling = digit_heavy_ruling_id()
    h = _client_world(tmp_path, srv, ruling)
    with monkeypatch.context() as m:
        m.setattr(sg, "_is_ruling_id", lambda s: False)
        m.setattr(SeoService, "_ruling_fields", lambda self, cmp_, tid: cmp_)
        clear_guard_caches()
        assert sg.classify(ruling) is None
        h.refused(_paid_audit(h, 1), 503, "SINK_REFUSED")
        h.refused(h.post("/tenants/acme/schedules", {"request_id": rid(), "domain": sa.DOMAIN, "scheme": "http",
                                                     "every_days": 7, "paths": ["/"], "invoice_id": invoice_id(2)},
                         caller="dashboard", andre=True), 503, "SINK_REFUSED")
    clear_guard_caches()
    with monkeypatch.context() as m:                         # the class alone removed: hashed, never refused
        m.setattr(sg, "_is_ruling_id", lambda s: False)
        clear_guard_caches()
        a = h.ok(_paid_audit(h, 3), 201)
        (c,) = [r["data"]["invoice_verification"]["compliance"] for r in sa.records(h, "audit_requested")
                if r["data"]["audit_id"] == a["audit_id"]]
        assert c["ruling_id"] == sg.RULING_ID_HASHED
    clear_guard_caches()
    a = h.ok(_paid_audit(h, 1), 201)
    (c,) = [r["data"]["invoice_verification"]["compliance"] for r in sa.records(h, "audit_requested")
            if r["data"]["audit_id"] == a["audit_id"]]
    assert c["ruling_id"] == ruling and "ruling_id_hmac" not in c


# ================================================================================================ MUST 2: key ids

DATE_STAMPED = ("2026-11", "k-20261110-01", "2026-11-10", "prod-2026q4", "k-20261110-0001", "v7", "2026")


def test_date_stamped_key_ids_are_accepted_only_as_a_configured_key_id_under_key_id():
    for kid in DATE_STAMPED:
        assert sg.key_id_refusal(kid) is None, len(kid)
    fresh = "k-" + digits(8) + "-" + digits(2)               # a well-formed key id nobody configured
    assert sg.key_id_refusal(fresh) is None and fresh not in sg.KEY_IDS
    stub = {"vault_id": secrets.token_hex(16), "i": 0, "key_id": fresh, "hmac": secrets.token_hex(32), "flag": None}
    assert sg.violations("audit_requested", {"paths": [stub]}) == [("/paths[]/key_id", "secret")]
    sg.register_key_ids([fresh, None])
    try:
        assert sg.violations("audit_requested", {"paths": [stub]}) == []
        # only under ``key_id``: the same value in any other field, in a list or as a key is refused
        assert sg.violations("audit_requested", {"note": fresh}) == [("/note", "secret")]
        assert sg.violations("audit_requested", {"key_ids": [fresh]}) == [("/key_ids[]", "secret")]
        assert sg.violations("audit_requested", {fresh: 1}) == [("/{key}", "secret")]
        assert sg.classify(fresh) is None and sg.scrub(fresh) == sg.WITHHELD
        # a line READ BACK may carry a retired key's id (well-formed); a write may not
        other = "k-" + digits(8) + "-" + digits(2)
        assert sg.violations("audit_requested", {"key_id": other}) != []
        assert sg.violations("audit_requested", {"key_id": other}, stored=True) == []
    finally:
        sg.KEY_IDS.discard(fresh)


def _keyish_ids() -> dict:
    return {"hex": secrets.token_hex(16), "token": mixed_token(28).replace("-", ""), "digits": "k-" + digits(13),
            "prefix": "sk-proj-" + token(20), "label": "pass" + "word-" + letters(8)}


def test_a_key_shaped_key_id_refuses_start_with_a_closed_configuration_error(tmp_path):
    for name, kid in sorted(_keyish_ids().items()):
        assert sg.key_id_refusal(kid) is not None, name
        with pytest.raises(ValueError) as e:
            sg.register_key_ids([kid])
        assert kid not in str(e.value) and kid not in sg.KEY_IDS, name
        d = tmp_path / name
        d.mkdir()
        with pytest.raises(RuntimeError) as e:
            helpers.Harness(d, data_dir=str(d / "data"), SEO_VAULT_KEY_FILE=helpers.write_vault_key(d / "v.key"),
                            SEO_VAULT_KEY_ID=kid)
        assert "SEO_VAULT_KEY_ID" in str(e.value) and kid not in str(e.value), name
        with pytest.raises(RuntimeError) as e:               # the previous key's id, the same
            helpers.Harness(d, data_dir=str(d / "data2"), SEO_VAULT_KEY_FILE=helpers.write_vault_key(d / "v.key"),
                            SEO_VAULT_KEY_ID="k1", SEO_VAULT_KEY_PREVIOUS_FILE=helpers.write_vault_key(d / "p.key"),
                            SEO_VAULT_KEY_PREVIOUS_ID=kid)
        assert "SEO_VAULT_KEY_PREVIOUS_ID" in str(e.value) and kid not in str(e.value), name


def test_the_service_checks_the_key_ids_itself_when_configuration_did_not():
    """A Settings object built past ``config._vault`` (a test, an embedder): the service's own start still refuses."""
    from clock import FixedClock
    from ledger import Recorder
    from store import RecordLog
    kid = secrets.token_hex(16)
    settings = config_mod.load(base_env())
    settings.vault_key = vault_mod.parse_vault_key(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"), kid)
    with pytest.raises(RuntimeError) as e:
        SeoService(settings, Recorder(helpers.FakeLedger()), RecordLog(None), Ports.default(settings),
                   FixedClock(helpers.T0))
    assert "SEO_VAULT_KEY_ID" in str(e.value) and kid not in str(e.value) and kid not in sg.KEY_IDS


@pytest.mark.local_http
@pytest.mark.parametrize("kid", ["2026-11", "k-20261110-01"])
def test_a_date_stamped_key_id_runs_audits_and_schedules(tmp_path, srv, kid):
    h = sa.harness(tmp_path, srv, SEO_VAULT_KEY_FILE=helpers.write_vault_key(tmp_path / "v.key"),
                   SEO_VAULT_KEY_ID=kid)
    a = h.ok(sa.audit(h), 201)
    assert a["status"] == "completed"
    sa.schedule(h)
    assert sa.tick(h)["ran"] == 1
    assert stub_key_ids(h) == {kid}
    h2 = h.restart(SEO_VAULT_KEY_FILE=h.env["SEO_VAULT_KEY_FILE"], SEO_VAULT_KEY_ID=kid)
    assert h2.ok(h2.get(f"/tenants/zbm/audits/{a['audit_id']}"))["report"] is not None
    assert sa.tick(h2)["already_done"] == 1 and h2.svc.health()["legacy_prose_lines"] == 0


@pytest.mark.local_http
def test_mutation_without_the_key_id_class_a_date_stamped_key_id_breaks_audits(tmp_path, srv, monkeypatch):
    kid = "2026-11"
    h = sa.harness(tmp_path, srv, SEO_VAULT_KEY_FILE=helpers.write_vault_key(tmp_path / "v.key"),
                   SEO_VAULT_KEY_ID=kid)
    monkeypatch.setattr(sg, "KEY_IDS", set())                # the closed set emptied: the review's finding
    h.refused(sa.audit(h), 503, "SINK_REFUSED")
    h.refused(h.post("/tenants/zbm/schedules", {"request_id": rid(), "domain": sa.DOMAIN, "scheme": "http",
                                                "paths": ["/", "/about"], "every_days": 7}), 503, "SINK_REFUSED")
    monkeypatch.undo()
    assert h.ok(sa.audit(h), 201)["status"] == "completed"


# ================================================================================================ SHOULD 1: hosts

HOST_CORPORA = {"neutral": (neutral_host, 4000), "idn_punycode": (idn_host, 600), "platform_hash": (platform_host, 1200),
                "label_word": (label_word_host, 400)}
FALSE_REFUSAL_MAX = 0.01


@pytest.mark.parametrize("name", sorted(HOST_CORPORA))
def test_real_world_host_names_are_accepted(name):
    gen, n = HOST_CORPORA[name]
    refused, reasons = refusal_rate(gen, n, SEED)
    print(f"host false refusals {name}: generated={n} refused={refused} rate={refused / n:.4%} reasons={reasons}")
    assert refused / n <= FALSE_REFUSAL_MAX, (name, refused, reasons)
    # and each is a machine value of the ``hostname`` class wherever the service writes a host
    r = random.Random(SEED)
    sample = [h for h in (gen(r) for _ in range(300)) if sg.host_refusal(h) is None]
    assert all(sg.classify(h) == "hostname" and not svc_tenants.domain_refused(h) for h in sample)
    assert sg.violations("tenant_domains_set", {"domains": sample, "property_host": sample[0]}) == []


def test_the_hosts_the_review_names_are_accepted():
    hosts = named_label_word_hosts()
    assert [h for h in hosts if sg.classify(h) != "hostname" or svc_tenants.domain_refused(h)] == []
    idn = ["xn--" + u.encode("punycode").decode("ascii") + ".de" for u in ("m\u00fcnchen", "b\u00fccher")] + \
        ["xn--" + "\u043f\u0440\u0438\u043c\u0435\u0440".encode("punycode").decode("ascii") + "." +
         "xn--" + "\u0440\u0444".encode("punycode").decode("ascii")]
    assert [h for h in idn if sg.classify(h) != "hostname" or not svc_tenants.valid_domain(h)] == []


@pytest.mark.parametrize("name", sorted(SECRET_HOSTS))
def test_secret_carrying_hosts_stay_refused(name):
    gen = SECRET_HOSTS[name]
    n = 2000
    accepted = sum(1 for _ in range(n) if sg.host_refusal(gen()) is None)
    print(f"secret host {name}: generated={n} accepted={accepted}")
    assert accepted == 0, (name, accepted)
    h = gen()
    assert sg.classify(h) is None and svc_tenants.domain_refused(h)
    assert sg.violations("tenant_domains_set", {"domains": [h]}) == [("/domains[]", "host")]


def test_a_24_character_random_token_label_is_refused_with_a_measured_miss_rate():
    """A random letters-and-digits label of 24 characters: refused unless it holds almost no digits (a label of
    letters only cannot be told from a name). The miss rate is measured and bounded."""
    n = 20000
    accepted = sum(1 for _ in range(n) if sg.host_refusal(token(24) + ".example.com") is None)
    print(f"secret host token_24 (uniform a-z0-9): generated={n} accepted={accepted} rate={accepted / n:.4%}")
    assert accepted / n <= 0.005


HOST_MUTATIONS = {
    "prefix": (lambda m: (m.setattr(secret_guard, "_prefix_found", lambda *a, **k: False),
                          m.setattr(secret_guard, "_JOINED_PREFIX", re.compile(r"(?!)")),
                          m.setattr(secret_guard, "_AWS_JOINED", re.compile(r"(?!)"))),
               lambda: "sk-proj-" + letters(14) + ".example.com"),
    "hex": (lambda m: m.setattr(sg, "HOST_HEX_MIN", 10 ** 6),
            lambda: secrets.token_hex(8) + "." + secrets.token_hex(8) + ".example.com"),
    "token": (lambda m: m.setattr(sg, "HOST_TOKEN_MIN", 10 ** 6), lambda: mixed_token(26) + ".example.com"),
    "digit_run": (lambda m: m.setattr(sg, "HOST_DIGIT_RUN", 10 ** 6), lambda: digits(10) + ".example.com"),
    "digit_total": (lambda m: m.setattr(sg, "HOST_DIGITS_TOTAL", 10 ** 6),
                    lambda: "a" + digits(4) + "." + digits(4) + ".b." + digits(4) + ".c." + digits(4) + ".example.com"),
    "hash_run_cap": (lambda m: m.setattr(sg, "HOST_HASH_RUN_MAX", 10 ** 6),
                     lambda: "ab" + digits(14) + "cd.example.com"),
    "email_at": (lambda m: m.setattr(sg, "HOST_AT_LABEL", "\x00"),
                 lambda: ".".join((letters(5), letters(6), "at", letters(7), "com"))),
    "email_provider": (lambda m: m.setattr(sg, "MAILBOX_PROVIDERS", frozenset()),
                       lambda: ".".join((letters(5), letters(6), sorted(secret_guard.MAILBOX_PROVIDERS)[0]))),
    "label_value": (lambda m: m.setattr(sg, "HOST_VALUE_MIN", 10 ** 6),
                    lambda: "pass" + "word-" + letters(4) + digits(3) + ".example.com"),
    "idna": (lambda m: m.setattr(sg, "_idna_label_ok", lambda label: True), lambda: undecodable_label() + ".example.com"),
}


@pytest.mark.parametrize("name", sorted(HOST_MUTATIONS))
def test_each_host_rule_bites(name, monkeypatch):
    mutate, gen = HOST_MUTATIONS[name]
    hosts = [gen() for _ in range(50)]
    assert all(sg.host_refusal(h) is not None for h in hosts), name
    with monkeypatch.context() as m:
        mutate(m)
        clear_guard_caches()
        got = sum(1 for h in hosts if sg.host_refusal(h) is None)
    clear_guard_caches()
    assert got > 0, f"removing the {name} rule changed nothing"
    assert all(sg.host_refusal(h) is not None for h in hosts), name


def test_every_lane_judges_a_host_alike():
    """The guard's ``hostname`` class, the strict check and the strict URL inspection agree on every host: one the
    sink takes is kept in a URL (a tenant's own site is not withheld), one it refuses is withheld."""
    from agents import callum
    r = random.Random(SEED + 3)
    hosts = [gen(r) for gen, _n in HOST_CORPORA.values() for _ in range(250)] + named_label_word_hosts() + \
        [gen() for gen in SECRET_HOSTS.values() for _ in range(40)] + [undecodable_label() + ".com" for _ in range(40)]
    split = collections.Counter()
    for h in hosts:
        ok = sg.classify(h) == "hostname"
        split[ok] += 1
        u = secret_guard.inspect_url(f"https://{h}/about?x=1")
        assert (secret_guard.check(h) is None) == ok, (ok, sg.host_refusal(h))
        assert (u["host_kind"] is None) == ok, (ok, sg.host_refusal(h))
        assert (secret_guard.safe_url(f"https://{h}/about") == f"https://{h}/about") == ok, (ok, sg.host_refusal(h))
        assert (callum.checked_host(h)["host"] == h) == ok, (ok, sg.host_refusal(h))
    assert split[True] >= 1000 and split[False] >= 1000, dict(split)
    for h in ("EXAMPLE.com", "Shop.Example.COM"):            # a URL's host is read in lower case
        assert secret_guard.inspect_url(f"https://{h}/")["host_kind"] is None


def test_the_host_rule_edges():
    ok = ["a.io", "x" * 63 + ".com", "a-1.b-2.example.org", "blog-2026-11-10.example.com", "1a2b3c4d5e6f.example.io",
          "shop.at", "at.example.com", "mail.yahoo.com", "www.gmail.com", "tok" + "en2049.com", "auth0.com",
          "b" * 23 + "7.example.com", "v1-2-3.example.com"]
    assert [h for h in ok if sg.host_refusal(h) is not None] == []
    bad = {"x" * 64 + ".com": "syntax", "-a.example.com": "syntax", "a..example.com": "syntax", "example.c": "tld",
           "example.123": "tld", "example." + "a" * 25: "tld", "xn--.com": "syntax",
           undecodable_label() + ".com": "idna",
           "example.xn--zz": "tld", "a" * 250 + ".com": "syntax", "EXAMPLE.com": "syntax", "exa mple.com": "syntax",
           "2026-11-10-1.example.com": "digits", "noat.com": None}
    for host, reason in bad.items():
        assert sg.host_refusal(host) == reason, (len(host), reason)
    # a host-shaped value is a host name or nothing: a refused one is never read as a code instead
    for name in ("email_at_label", "hex_32", "ssn_dashed"):
        assert sg._judge(SECRET_HOSTS[name]()) == (None, "host")


def test_host_judgement_is_linear_and_cached():
    import time
    long = ".".join(["a1" * 15] * 8) + ".example.com"
    t = time.perf_counter()
    for i in range(2000):
        sg.host_refusal(f"{i}{long}"[:253])
    assert time.perf_counter() - t < 2.0


# ================================================================================================ SHOULD 1: the routes

def test_idn_platform_and_label_word_domains_are_accepted_by_every_domain_field(tmp_path):
    rng = random.Random(secrets.randbits(64))
    h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data"))
    idn = accepted_host(lambda: idn_label(rng) + "." + "xn--" + "\u0440\u0444".encode("punycode").decode("ascii"))
    hashed = accepted_host(lambda: "store-" + hexs(rng, 12) + ".mybigcommerce.com")
    worded = "sec" + "ret-sale.shop"
    h.tenant("acme", "client", (idn, hashed, worded))
    h.ok(h.post("/tenants/acme/gsc-property", {"request_id": rid(), "property": "sc-domain:" + idn}, andre=True))
    ps = h.ok(h.post("/tenants/acme/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Acme"], "prompts": ["who cleans rugs"],
        "competitor_domains": named_label_word_hosts() + [
            accepted_host(lambda: idn_label(rng, "greek") + ".example"),
            accepted_host(lambda: hexs(rng, 12) + ".ngrok.io")], "engines": ["openai"]},
                    caller="dashboard"), 201)
    assert len(ps["versions"]["1"]["competitor_domains"]) == 9
    assert all(sg.violations(r["kind"], r["data"]) == [] for r in log_records(h))
    # a secret-carrying host is refused by its route (422), never at the sink (503)
    n = len(h.svc.log)
    for name in sorted(SECRET_HOSTS):
        bad = SECRET_HOSTS[name]()
        h.refused(h.post("/tenants/acme/domains", {"request_id": rid(), "domains": [bad]}, andre=True), 422)
        h.refused(h.post("/tenants/acme/prompt-sets", {
            "request_id": rid(), "name": "n" + letters(6), "brand_terms": ["Acme"], "prompts": ["who cleans rugs"],
            "competitor_domains": [bad], "engines": ["openai"]}, caller="dashboard"), 422)
    assert len(h.svc.log) == n


# ================================================================================================ SHOULD 2: finance id

def test_the_finance_client_id_pattern_is_what_the_guard_can_write_and_never_ends_in_503(tmp_path):
    assert models.FINANCE_CLIENT == r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
    rx = re.compile(models.FINANCE_CLIENT)
    rng = random.Random(SEED + 7)
    alphabet = string.ascii_letters + string.digits + "._-"
    corpus = [client_reference(rng) for _ in range(N_IDS // 2)]
    while len(corpus) < N_IDS:
        x = rng.choice(string.ascii_letters + string.digits) + "".join(
            rng.choice(alphabet) for _ in range(rng.choice((1, 3, 6, 10, 16, 24, 40, 63))))
        corpus.append(x)
    assert all(rx.fullmatch(x) for x in corpus)
    accepted = [x for x in corpus if not svc_tenants.identifier_refused(x, prose=True)]
    # whatever validation accepts, both sinks take: a 422 or a write, never a 503
    stuck = [x for x in accepted
             if sg.violations("tenant_finance_client_set", {"tenant_id": "acme", "finance_client_id": x})
             or sg.violations(sg.LEDGER + "tenant_finance_client_set", {"finance_client_id": x})]
    print(f"finance client ids: generated={len(corpus)} accepted={len(accepted)} "
          f"refused_422={len(corpus) - len(accepted)} accepted_then_refused_at_the_sink={len(stuck)}")
    assert stuck == [] and len(accepted) >= N_IDS * 0.45
    h = helpers.Harness(tmp_path)
    h.tenant("acme", "client", ("acme.test",))
    codes = collections.Counter()
    for x in rng.sample(corpus, 300) + ["a" * 65, "." + "abc", "_x", "", "a" * 101]:
        r = h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": x}, andre=True)
        codes[r.status_code] += 1
    assert set(codes) <= {200, 422}, dict(codes)
    assert codes[200] > 100 and codes[422] >= 5


def test_a_key_typed_as_the_finance_client_id_is_422_and_the_identifier_check_is_load_bearing(tmp_path, monkeypatch):
    """SHOULD 7: a 32-hex key IS a machine value for the sink (the ``hash`` class), so only
    ``secret_guard.identifier_refusal`` in ``svc_tenants.identifier_refused`` keeps it out of the log."""
    h = helpers.Harness(tmp_path)
    h.tenant("acme", "client", ("acme.test",))
    key = "a" + secrets.token_hex(16)[1:]
    assert sg.classify(key) == "hash" and re.fullmatch(models.FINANCE_CLIENT, key)
    n = len(h.svc.log)
    h.refused(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": key}, andre=True),
              422, "IDENTIFIER_WITHHELD")
    h.refused(h.post("/tenants", {"request_id": rid(), "tenant_id": key, "kind": "client"}, andre=True), 422,
              "IDENTIFIER_WITHHELD")
    assert len(h.svc.log) == n and key not in json.dumps([r["data"] for r in log_records(h)])
    with monkeypatch.context() as m:                         # the mutation: the check removed
        m.setattr(secret_guard, "identifier_refusal", lambda text: None)
        assert not svc_tenants.identifier_refused(key)
        other = "b" + secrets.token_hex(16)[1:]
        assert h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": other},
                      andre=True).status_code == 200
    assert svc_tenants.identifier_refused(key)


# ================================================================================================ SHOULD 3: retry

def test_a_request_recorded_with_the_legacy_hash_is_idempotent_after_the_deploy(tmp_path, monkeypatch):
    h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data"))
    words = " ".join(letters(7) for _ in range(4))
    body = {"request_id": rid(), "name": "core", "brand_terms": ["Z Best Media", words.title()],
            "competitor_domains": ["competitor.test"], "prompts": [f"what about {words}", "who recovers revenue"],
            "engines": ["openai"]}
    seen = []
    real_idem = SeoService._idem

    def spy(self, actor, rk, b):
        seen.append(json.loads(json.dumps(b)))
        return real_idem(self, actor, rk, b)
    with monkeypatch.context() as m:                         # the OLD release: the plain hash of the body
        m.setattr(SeoService, "_machine_body", lambda self, b: b)
        m.setattr(SeoService, "_idem", spy)
        first = h.ok(h.post("/tenants/zbm/prompt-sets", body), 201)
    (line,) = [r["data"] for r in sa.records(h, "prompt_set_version")]
    assert line["request_sha"] == payload_sha256(seen[-1])                       # an old-format request hash
    assert line["request_sha"] != payload_sha256(h.svc._machine_body(seen[-1]))  # ... not what is computed now
    h2 = h.restart()
    assert h2.ok(h2.post("/tenants/zbm/prompt-sets", body), 201) == first        # the retry: the first answer
    assert len(sa.records(h2, "prompt_set_version")) == 1
    h2.refused(h2.post("/tenants/zbm/prompt-sets", {**body, "prompts": ["a different question"]}), 409,
               "REQUEST_ID_REUSED")
    h2.refused(h2.post("/tenants/zbm/prompt-sets", {**body, "brand_terms": ["Z Best Media"]}), 409,
               "REQUEST_ID_REUSED")
    monkeypatch.setattr(SeoService, "_legacy_request_sha", lambda self, b: None)  # the mutation: no legacy compare
    h2.refused(h2.post("/tenants/zbm/prompt-sets", body), 409, "REQUEST_ID_REUSED")
    monkeypatch.undo()
    # a request recorded NOW keeps the keyed form, and its retry answers the same
    body2 = {**body, "request_id": rid(), "name": "second"}
    second = h2.ok(h2.post("/tenants/zbm/prompt-sets", body2), 201)
    assert h2.ok(h2.post("/tenants/zbm/prompt-sets", body2), 201) == second


# ================================================================================================ SHOULD 4: repair

def _torn_operator_vault(tmp_path, srv):
    h = sa.harness(tmp_path, srv)
    h.ok(sa.audit(h), 201)                                   # the audited paths are operator text: two stubs
    v = h.svc.optext_vault
    n = len(v)
    assert n >= 1 and v.path.endswith(vault_mod.OPERATOR_VAULT_NAME)
    frag = b'{"v":1,"id":"' + letters(11).upper().encode()
    with open(v.path, "ab") as fh:
        fh.write(frag)
    return h, n, frag


def _side(d: str) -> list:
    return sorted(os.listdir(d))


@pytest.mark.local_http
def test_the_operator_vault_torn_tail_is_recorded_cleared_and_shown_in_health(tmp_path, srv):
    h, n, frag = _torn_operator_vault(tmp_path, srv)
    h2 = h.restart()
    health = h2.svc.health()
    st = health["operator_vault"]["tail_repair"]
    assert st["records_removed"] == 1 and st["records_kept"] == n and st["quarantined"] == 1 and st["recorded"]
    assert st["bytes_removed"] == len(frag) and health["citation_vault"]["tail_repair"] is None
    assert h2.ok(h2.get("/status"))["operator_vault"]["tail_repair"]["recorded"] is True
    assert h2.ok(h2.client.get("/health")) == {"status": "ok"}                   # unauthenticated: the status only
    evs = h2.ledger.of_type("operator_vault_tail_repaired")
    assert len(evs) == 1 and set(evs[0]["_payload"]) == set(vault_mod.REPAIR_FIELDS)
    assert evs[0]["subject_id"] == "operator_vault" and h2.ledger.of_type("citation_vault_tail_repaired") == []
    assert frag.decode()[13:] not in json.dumps(evs)                             # counts, never content
    d = h2.settings.data_dir
    assert not any(f.endswith(vault_mod.REPAIR_MARKER_SUFFIX) for f in _side(d))  # the marker is gone once recorded
    assert open(h2.svc.optext_vault.path, "rb").read().endswith(b"\n") and len(h2.svc.optext_vault) == n
    h3 = h2.restart()
    assert len(h3.ledger.of_type("operator_vault_tail_repaired")) == 1 and h3.svc.optext_vault.tail_repair is None
    assert h3.svc.health()["operator_vault"]["tail_repair"] is None


@pytest.mark.local_http
def test_an_operator_vault_repair_the_ledger_cannot_take_stays_visible_and_is_recorded_later(tmp_path, srv,
                                                                                            monkeypatch):
    h, n, _frag = _torn_operator_vault(tmp_path, srv)
    h.svc.close()
    real = SeoService._record_twice

    def failing(self, eid, event_type, *a, **k):
        if event_type == "operator_vault_tail_repaired":
            from ledger import LedgerNotRecorded
            raise LedgerNotRecorded("down")
        return real(self, eid, event_type, *a, **k)
    with monkeypatch.context() as m:
        m.setattr(SeoService, "_record_twice", failing)
        h2 = helpers.Harness(h.tmp, h.settings.data_dir, h.ledger, h.clock, h.ports)
        st = h2.svc.health()["operator_vault"]["tail_repair"]
        assert st["recorded"] is False and st["records_kept"] == n
        assert any(f.endswith(vault_mod.REPAIR_MARKER_SUFFIX) for f in _side(h2.settings.data_dir))
        assert h2.ledger.of_type("operator_vault_tail_repaired") == []
        h2.svc.close()
    h3 = helpers.Harness(h.tmp, h.settings.data_dir, h.ledger, h.clock, h.ports)
    assert len(h3.ledger.of_type("operator_vault_tail_repaired")) == 1
    assert h3.svc.health()["operator_vault"]["tail_repair"]["recorded"] is True
    assert not any(f.endswith(vault_mod.REPAIR_MARKER_SUFFIX) for f in _side(h3.settings.data_dir))


@pytest.mark.local_http
def test_mutation_recording_only_the_citation_vault_loses_the_operator_repair(tmp_path, srv, monkeypatch):
    h, _n, _frag = _torn_operator_vault(tmp_path, srv)
    real = SeoService._record_one_vault_repair
    monkeypatch.setattr(SeoService, "_record_one_vault_repair",
                        lambda self, vault, event_type, subject: real(self, vault, event_type, subject)
                        if subject == "citation_vault" else None)                # what 2b26d980 did
    h2 = h.restart()
    assert h2.ledger.of_type("operator_vault_tail_repaired") == []
    assert "recorded" not in h2.svc.health()["operator_vault"]["tail_repair"]


# ================================================================================================ SHOULD 5: export

def _old_format_directory(tmp_path, srv):
    """A data directory as a release up to 8bd9e29e left it: an audit whose line carries the whole report, a prompt
    set with its content inline, a log ingest with robots.txt inline, an entity with plain values. Returns the
    harness (restarted) and the prose markers those lines hold."""
    h = sa.harness(tmp_path, srv)
    marks = {k: f"zq{letters(8)} wv{letters(8)}" for k in ("prompt", "brand", "robots", "entity", "note")}
    aid, report = sa.old_format_audit(h, pytest.MonkeyPatch())
    content = {"name": "old", "brand_terms": [marks["brand"]], "competitor_domains": [],
               "prompts": [f"what about {marks['prompt']}"], "engines": ["openai"]}
    psid = derived_id("pst", "zbm", "old")
    sa.raw_commit(h.svc, "prompt_set_version", {"prompt_set_id": psid, "tenant_id": "zbm", "version": 1,
                                                "content": content, "sha256": payload_sha256(content)}, "dashboard")
    sa.raw_commit(h.svc, "job_ran", {"job": "integrity", "note": marks["note"], "at_host": "site.test",
                                     marks["entity"]: 1}, "scheduler")
    return h, marks, aid, report


@pytest.mark.local_http
def test_old_format_lines_are_counted_at_start_and_their_prose_is_never_served(tmp_path, srv, monkeypatch):
    h, marks, aid, report = _old_format_directory(tmp_path, srv)
    titles = [s for s in all_strings(report) if " " in s and len(s) > 12][:20]
    assert len(titles) >= 5
    old_lines = [r for r in log_records(h) if sg.violations(r["kind"], r["data"], stored=True)]
    assert len(old_lines) == 3
    h2 = h.restart()
    assert h2.svc.legacy_prose_lines == 3 and h2.svc.health()["legacy_prose_lines"] == 3
    assert h2.ok(h2.get("/status"))["legacy_prose_lines"] == 3
    r = h2.get("/audit/export", caller="compliance_38", params={"since": 1, "limit": 1000})
    page = h2.ok(r)
    assert page["withheld"] >= len(titles) and len(page["events"]) == len(log_records(h2))
    text = r.text
    assert [k for k, v in marks.items() if v in text or v.split()[0] in text] == []
    assert [i for i, s in enumerate(titles) if s in text] == []
    # every served line is machine values only; ids, codes, hashes and counts are still there
    for ev in page["events"]:
        assert sg.violations(ev["kind"], ev["data"], stored=True) == [], ev["kind"]
    by_kind = collections.defaultdict(list)
    for ev in page["events"]:
        by_kind[ev["kind"]].append(ev["data"])
    (done,) = [d for d in by_kind["audit_completed"] if d["audit_id"] == aid]
    (raw_done,) = [x["data"] for x in log_records(h2) if x["kind"] == "audit_completed" and x["data"]["audit_id"] == aid]
    assert done["report_sha256"] == raw_done["report_sha256"] and sg.WITHHELD in all_strings(done["report"])
    assert raw_done["report"] == report                                         # the line itself is untouched
    (ps,) = by_kind["prompt_set_version"]
    assert ps["content"]["prompts"] == [sg.WITHHELD] and ps["content"]["brand_terms"] == [sg.WITHHELD]
    assert ps["content"]["name"] == "old" and ps["content"]["engines"] == ["openai"] and ps["version"] == 1
    (job,) = [d for d in by_kind["job_ran"] if "at_host" in d]
    assert job["note"] == sg.WITHHELD and job["at_host"] == "site.test"
    assert job[sg.WITHHELD] == 1 and marks["entity"] not in job
    # a line that held machine values only is served exactly as it was written
    for ev, rec in zip(page["events"], log_records(h2)):
        if not sg.violations(rec["kind"], rec["data"], stored=True):
            assert ev["data"] == {k: v for k, v in rec["data"].items() if k not in ("_obj", "request_sha")}
    # the mutation: without the filter the prose is served
    monkeypatch.setattr(sg, "withhold", lambda d, *a, **k: (d, 0))
    leaked = h2.get("/audit/export", caller="compliance_38", params={"since": 1, "limit": 1000}).text
    assert marks["prompt"] in leaked and marks["note"] in leaked
    monkeypatch.undo()
    # paging keeps the filter
    one = h2.ok(h2.get("/audit/export", caller="compliance_38", params={"since": 1, "limit": 2}))
    assert len(one["events"]) == 2


def test_withhold_keeps_machine_values_and_replaces_everything_else():
    kid = "k-" + digits(8)
    data = {"audit_id": derived_id("aud", 1), "n": 3, "ok": True, "none": None, "ratio": 0.5, "bad": float("inf"),
            "note": "two words", "rows": [{"host": "example.com", "text": "more than a word"}, "x y", 7],
            "epoch": digits(16), "other": digits(16), "key_id": kid, "raw": b"bytes", "two words": {"a": "b c"},
            "three words here": 1, "at": "2026-10-09T18:00:00Z"}
    out, n = sg.withhold(data)
    W = sg.WITHHELD
    assert out == {"audit_id": data["audit_id"], "n": 3, "ok": True, "none": None, "ratio": 0.5, "bad": W, "note": W,
                   "rows": [{"host": "example.com", "text": W}, W, 7], "epoch": data["epoch"], "other": W,
                   "key_id": kid, "raw": W, W: {"a": W}, W + ".2": 1, "at": data["at"]}
    assert n == 9 and sg.violations("job_ran", out, stored=True) == []
    clean = {"a": derived_id("aud", 2), "b": [1, "COMPLETED", {"c": "example.org"}]}
    assert sg.withhold(clean) == (clean, 0)
    deep: dict = {}
    cur = deep
    for _ in range(sg.MAX_DEPTH + 5):
        cur["d"] = {}
        cur = cur["d"]
    assert sg.withhold(deep)[1] == 1


# ================================================================================================ SHOULD 6: schedule

@pytest.mark.local_http
def test_a_schedule_naming_a_prompt_set_whose_blob_is_lost_is_503(tmp_path, srv):
    h = sa.harness(tmp_path, srv, own=False)
    h.tenant("acme", domains=(sa.DOMAIN,))
    h.ok(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": "acme-party-1"},
                andre=True))
    ps = h.ok(h.post("/tenants/acme/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"], "prompts": ["who recovers revenue"],
        "engines": ["openai"]}, caller="dashboard"), 201)
    body = {"request_id": rid(), "domain": sa.DOMAIN, "scheme": "http", "every_days": 7, "paths": ["/"],
            "invoice_id": invoice_id(1), "prompt_set_id": ps["prompt_set_id"]}
    os.remove(os.path.join(h.settings.data_dir, vault_mod.OPERATOR_VAULT_NAME))
    h2 = h.restart()
    assert h2.svc.prompt_sets[ps["prompt_set_id"]]["versions"]["1"]["vault_unavailable"] is True
    n = len(h2.svc.log)
    h2.refused(h2.post("/tenants/acme/schedules", body, caller="dashboard", andre=True), 503, "VAULT_UNAVAILABLE")
    assert len(h2.svc.log) == n and h2.svc.schedules == {}
    # as an audit and a citation run already answered
    h2.refused(h2.post("/tenants/acme/audits", {k: v for k, v in body.items() if k != "every_days"},
                       caller="dashboard", andre=True), 503, "VAULT_UNAVAILABLE")
    # without the prompt set the schedule is created
    h2.ok(h2.post("/tenants/acme/schedules", {k: v for k, v in body.items() if k != "prompt_set_id"},
                  caller="dashboard", andre=True), 201)


# ================================================================================================ SHOULD 9: identifiers

def atless_forms() -> dict:
    a, b, d = letters(5), letters(6), letters(7)
    provider = secrets.choice(sorted(secret_guard.MAILBOX_PROVIDERS))
    return {"at_dotted": f"{a}.{b}.at.{d}.com", "at_dashed": f"{a}-{b}-at-{d}-com", "at_underscored": f"{a}_at_{d}_org",
            "provider_dotted": f"{a}.{b}.{provider}", "provider_dashed": f"{a}-{b}-" + provider.replace(".", "-"),
            "provider_underscored": f"{a}_{b}_" + provider.replace(".", "_").replace("-", "_"),
            "mailto_dash": f"mailto-{a}-{b}", "mailto_underscored": f"mailto_{a}_{b}",
            "mailto_colon": f"mailto:{a}.{b}", "email_colon": f"email:{a}.{b}", "email_equals": f"e-mail={a}"}


def test_atless_email_forms_and_folded_prose_are_recognised():
    forms = atless_forms()
    assert [k for k, v in forms.items() if not secret_guard.atless_email(v)] == []
    assert [k for k, v in forms.items() if secret_guard.identifier_refusal(v) is None] == []
    fine = ["acme-corp", "email-campaigns", "look-at-me", "at-home", "gmail-com", "mail-order-rugs", "client-7",
            "acme.co", "great-at-work", "emailer", "mailtools", "team.outlook", "a-b-c"]
    assert [x for x in fine if secret_guard.atless_email(x)] == []
    words = [letters(n) + "a" for n in (4, 5, 3, 6, 4, 5)]
    assert secret_guard.folded_prose("-".join(words)) and secret_guard.folded_prose(".".join(words))
    assert secret_guard.folded_prose("_".join(words + ["7"]))
    assert not secret_guard.folded_prose("-".join(words[:5]))                    # five segments: an id
    assert not secret_guard.folded_prose("acme-co-uk-retail-west")
    assert not secret_guard.folded_prose("a1-b2-c3-d4-e5-f6-g7")                 # serials, not words
    assert not secret_guard.folded_prose("zbm-client-2026-10-07-a")


def test_identifier_fields_refuse_atless_email_forms_and_folded_prose(tmp_path):
    h = helpers.Harness(tmp_path)
    h.tenant("acme", "client", ("acme.test",))
    n = len(h.svc.log)
    forms = atless_forms()
    prose = "-".join(letters(k) + "e" for k in (3, 5, 4, 6, 3, 5))
    tenant_rx, name_rx = re.compile(models.TENANT), re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
    tried = collections.Counter()
    for key, v in sorted(forms.items()) + [("prose", prose)]:
        if tenant_rx.fullmatch(v):
            tried["tenant_id"] += 1
            h.refused(h.post("/tenants", {"request_id": rid(), "tenant_id": v, "kind": "client"}, andre=True), 422,
                      "IDENTIFIER_WITHHELD")
        if re.fullmatch(models.FINANCE_CLIENT, v):
            tried["finance_client_id"] += 1
            h.refused(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": v},
                             andre=True), 422, "IDENTIFIER_WITHHELD")
        if key != "prose" and re.fullmatch(models.DOMAIN, v) and svc_tenants.valid_domain(v):
            tried["domains"] += 1
            h.refused(h.post("/tenants/acme/domains", {"request_id": rid(), "domains": [v]}, andre=True), 422,
                      "IDENTIFIER_WITHHELD")
            tried["competitor_domains"] += 1
            h.refused(h.post("/tenants/acme/prompt-sets", {
                "request_id": rid(), "name": "n" + letters(5), "brand_terms": ["Acme"], "prompts": ["who cleans"],
                "competitor_domains": [v], "engines": ["openai"]}, caller="dashboard"), 422, "IDENTIFIER_WITHHELD")
        if key != "prose" and name_rx.fullmatch(v):
            tried["prompt_set_name"] += 1
            h.refused(h.post("/tenants/acme/prompt-sets", {
                "request_id": rid(), "name": v, "brand_terms": ["Acme"], "prompts": ["who cleans"],
                "engines": ["openai"]}, caller="dashboard"), 422, "IDENTIFIER_WITHHELD")
        if key != "prose" and len(v) <= 40:
            import svc_entity
            r = h.post("/tenants/acme/entity", {"request_id": rid(), "fields": [
                {"field": "name", "value": "Acme", "source": v, "provenance": "stated",
                 "authority": "first_party"}]}, andre=True)
            if svc_entity.SOURCE.fullmatch(v):             # a source of the field's own shape: the identifier rule
                tried["entity_source"] += 1
                h.refused(r, 422, "IDENTIFIER_WITHHELD")
            else:                                          # any other shape: the field's format refuses it first
                h.refused(r, 422, "ENTITY_VALUE_INVALID")
    assert len(h.svc.log) == n
    assert min(tried[k] for k in ("tenant_id", "finance_client_id", "domains", "competitor_domains",
                                  "prompt_set_name", "entity_source")) >= 2, dict(tried)
    # what remains accepted (ADR 0051's known limit): a short token, a five-segment id, a hyphenated "at" in a host
    h.ok(h.post("/tenants", {"request_id": rid(), "tenant_id": "acme-co-uk-retail-west", "kind": "client"},
                andre=True), 201)
    h.ok(h.post("/tenants/acme/domains", {"request_id": rid(), "domains": ["acme.test", "great-at-work.test"]},
                andre=True))


def test_mutation_without_the_atless_rule_the_forms_are_accepted(tmp_path, monkeypatch):
    h = helpers.Harness(tmp_path)
    h.tenant("acme", "client", ("acme.test",))
    v = atless_forms()["provider_dashed"]
    assert re.fullmatch(models.TENANT, v)
    monkeypatch.setattr(secret_guard, "atless_email", lambda text: False)
    h.ok(h.post("/tenants", {"request_id": rid(), "tenant_id": v, "kind": "client"}, andre=True), 201)
    monkeypatch.setattr(secret_guard, "folded_prose", lambda text: False)
    prose = "-".join(letters(k) + "e" for k in (3, 5, 4, 6, 3, 5))
    h.ok(h.post("/tenants", {"request_id": rid(), "tenant_id": prose, "kind": "client"}, andre=True), 201)


# ================================================================================================ NITs

def test_a_non_finite_float_is_not_a_machine_value():
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert sg.violations("job_ran", {"x": bad}) == [("/x", "float")]
        assert sg.violations("job_ran", {"rows": [1.5, bad]}) == [("/rows[]", "float")]
        assert sg.violations("job_ran", bad) == [("", "float")]
        assert sg.scrub(bad) == sg.WITHHELD
        with pytest.raises(sg.SinkViolation):
            sg.enforce("job_ran", {"x": {"y": bad}})
    assert sg.violations("job_ran", {"x": 0.25, "y": -1e300, "z": 0.0, "n": 7}) == []
    assert sg.scrub(0.25) == 0.25


def test_a_non_finite_float_refuses_the_commit(tmp_path):
    h = helpers.Harness(tmp_path)
    n, m = len(h.svc.log), len(h.ledger.events)
    with pytest.raises(Exception) as e:
        h.svc._commit("job_ran", {"job": "integrity", "ratio": float("nan")}, "scheduler")
    assert getattr(e.value, "reason", None) == "SINK_REFUSED"
    assert len(h.svc.log) == n and len(h.ledger.events) == m


def test_the_dop_v1_prefix_is_a_strong_key_in_both_lanes():
    for prefix in ("dop_v1_", "doo_v1_", "dor_v1_"):
        for body in (letters(12), token(40), secrets.token_hex(32), "x" * 9):
            key = prefix + body
            assert secret_guard.check(key) == "c_prefix", (prefix, len(body))
            assert secret_guard.operator_refusal(f"use {key} for the droplet") == "c_prefix"
            assert "strong_key" in secret_guard.client_view_findings(f"the token is {key} today")
            assert sg.classify(key) is None
    assert secret_guard.check("dop_v2_" + letters(12)) is None                   # the family, not every look-alike
    host = "dopv1" + token(20) + ".example.com"
    assert sg.host_refusal(host) == "prefix"


def test_logging_extra_attributes_are_scrubbed(tmp_path, caplog):
    helpers.Harness(tmp_path)
    assert log_sink.installed()
    caplog.set_level(logging.INFO)
    prose = f"zq{letters(8)} wv{letters(8)}"
    key = "sk" + "-" + token(40, string.ascii_letters + string.digits)
    aid = derived_id("aud", 9)
    logging.getLogger("seo.test").info("audit %s recorded", aid, extra={
        "client_note": prose, "audit": aid, "extra_value": key, "count": 3, "ratio": float("nan"),
        "detail": {"host": "example.com", "text": prose, "n": 2}, "rows": [prose, "COMPLETED"],
        "obj": object(), "two words": prose})
    (rec,) = [r for r in caplog.records if r.name == "seo.test"]
    d = rec.__dict__
    assert d["client_note"] == sg.WITHHELD and d["extra_value"] == sg.WITHHELD and d["obj"] == sg.WITHHELD
    assert d["audit"] == aid and d["count"] == 3 and d["ratio"] == sg.WITHHELD
    assert d["detail"] == {"host": "example.com", "text": sg.WITHHELD, "n": 2}
    assert d["rows"] == [sg.WITHHELD, "COMPLETED"] and "two words" not in d
    assert rec.getMessage() == f"audit {aid} recorded"
    formatted = logging.Formatter("%(message)s %(client_note)s %(detail)s").format(rec) + json.dumps(
        {k: v for k, v in d.items() if isinstance(v, (str, int, float, dict, list))}, default=str)
    assert prose not in formatted and key not in formatted
    # a record made by hand (no factory) with extras is scrubbed when a handler takes it
    made = logging.makeLogRecord({"name": "seo.test", "msg": "x", "client_note": prose, "levelno": logging.INFO})
    log_sink.FILTER.filter(made)
    assert made.__dict__["client_note"] == sg.WITHHELD


def test_mutation_without_the_extra_scrub_the_attribute_is_kept(tmp_path, caplog, monkeypatch):
    helpers.Harness(tmp_path)
    caplog.set_level(logging.INFO)
    prose = f"zq{letters(8)} wv{letters(8)}"
    monkeypatch.setattr(log_sink, "_scrub_extra", lambda record: None)
    logging.getLogger("seo.test").info("noted", extra={"client_note": prose})
    (rec,) = [r for r in caplog.records if r.name == "seo.test"]
    assert rec.__dict__["client_note"] == prose


# ================================================================================================ SHOULD 8: test order

def test_no_test_leaves_a_mixin_method_on_the_service_class():
    """A test that patches ``SeoService`` by assignment and assigns the original back leaves the MIXIN's function
    in ``SeoService.__dict__``, where it shadows the mixin for every later test (tests/test_sink_citations.py did;
    tests/test_review_adacb2ca.py then failed). Whatever ran before this test, the class holds only its own."""
    leaked = sorted(name for name, f in vars(SeoService).items()
                    if callable(f) and not getattr(f, "__qualname__", "SeoService.").startswith("SeoService."))
    assert leaked == [], leaked
