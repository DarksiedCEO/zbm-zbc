"""Review of c0c09bd1 (shared redaction core, both lanes; operator text): the design change "Option B for operator text"
(prompt-set free text, entity values / provenance, audit / schedule paths, URL-prefix Search Console properties are
VAULTED, not refused; refused only for a strong key / PEM / user info / credential query key), identifier validation
(tenant id, domains, Finance client id, prompt-set name, competitor domains, entity source), log-ingest label segments
kept only when the label is alone, new e-mail spellings ("at symbol", "at at", e-mail emoji), base64 with a glued 6-8
character suffix, the S1 price exemption scoped to the plain spelled " at " after a real price, the wrap join scoped
to at / dot variant words (and wrapped bracketed forms in the client view), client-view number words and labelled
phones, multi-word labels with apostrophes / hyphens, the entity telephone waiver, the value after a label path
segment, decision 22 for prose only, and codes / model numbers before " at <domain>" in the client view.

Every sensitive-looking value here is GENERATED at run time from fragments (nothing credential-shaped or e-mail-shaped
is written in this file); failures report counts and case names only."""

from __future__ import annotations

import base64
import json
import random
import re
import string
import time
import urllib.parse

import helpers
import pytest
import test_review_601e328a as r6
import test_review_c13529af as c13
import test_vault_wiring as vw

import reasons
import svc_audits
import svc_entity
import svc_optext
from agents import callum, logs
from agents import secret_guard as sg
from helpers import rid

SEED = 909001
AT = chr(64)
LOW = string.ascii_lowercase
ALNUM = string.ascii_letters + string.digits
EMOJI = ("\U0001F4E7", "\u2709", "\U0001F4E9")
NEVER = re.compile(r"(?!x)x")


def _r(rng, alphabet, n):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _gh(rng) -> str:
    return "gh" + "p_" + _r(rng, ALNUM, 36)


def _aws(rng) -> str:
    return "AK" + "IA" + _r(rng, string.ascii_uppercase + "234567", 16)


def _pem() -> str:
    return "-----" + "BEGIN " + "RSA PRIV" + "ATE KEY-----"


def _userinfo(rng) -> str:
    return "https://" + _r(rng, LOW, 6) + ":" + _r(rng, ALNUM, 12) + AT + "host.test/x"


def _mail(rng) -> tuple:
    return r6._mail(rng)


def _email(rng) -> str:
    loc, dom = _mail(rng)
    return f"{loc}{AT}{dom}.com"


def _card(rng) -> str:
    return r6._card(rng, "4", 16)


def _ssn(rng) -> str:
    return "-".join(r6._ssn(rng))


def _phone(rng) -> str:
    a, b, c = r6._nanp(rng)
    return f"({a}) {b}-{c}"


def _secret_word(rng) -> str:
    """A credential VALUE that no strong-key rule knows (it is only a secret because of its label)."""
    return _r(rng, LOW, 5) + _r(rng, string.digits, 3) + _r(rng, LOW, 4)


DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven",
               "8": "eight", "9": "nine"}


def _words(d: str, sep: str = " ") -> str:
    return sep.join(DIGIT_WORDS[c] for c in d)


# ================================================================================================ MUST 2: log ingests

def _label_value_segments(rng) -> dict:
    out = {}
    for i in range(25):
        v = _secret_word(rng) if i % 2 else _r(rng, LOW, 9)
        out[f"token_dash_{i}"] = (f"token-{v}", v)
        out[f"md_bold_{i}"] = (urllib.parse.quote(f"**Password:** {v}"), v)
        out[f"is_{i}"] = (urllib.parse.quote(f"password is {v}"), v)
        out[f"md_table_{i}"] = (urllib.parse.quote(f"| password | {v} |"), v)
        out[f"after_{i}"] = (f"token/{v}", v)                  # the value in the next segment (SHOULD)
    return out


def test_a_log_segment_holding_a_label_and_its_value_is_never_kept():
    rng = random.Random(SEED + 1)
    cases = _label_value_segments(rng)
    leaked = [n for n, (seg, v) in cases.items() if v in (logs.template_path(f"/a/{seg}") or "")]
    assert leaked == []
    # a label word alone is kept (its value, if any, is another segment)
    for p in ("/reset-password", "/account/api-keys", "/forgot-password/confirm", "/mot-de-passe"):
        assert logs.template_path(p) == p, p


def test_the_label_alone_rule_is_load_bearing(monkeypatch):
    rng = random.Random(SEED + 2)
    cases = _label_value_segments(rng)
    monkeypatch.setattr(sg, "label_only", lambda seg, *a: True)             # mutation: every label segment kept
    leaked = [n for n, (seg, v) in cases.items() if v in (logs.template_path(f"/a/{seg}") or "")]
    assert len(leaked) > 0


def test_the_after_label_rule_is_load_bearing_in_log_templates(monkeypatch):
    rng = random.Random(SEED + 3)
    vals = [_r(rng, LOW, 9) for _ in range(10)]
    assert all(v not in logs.template_path(f"/token/{v}") for v in vals)
    monkeypatch.setattr(sg, "after_label", lambda segs, i: False)       # mutation
    assert all(v in logs.template_path(f"/token/{v}") for v in vals)


# ================================================================================================ MUST 3: spellings

def _spelled(rng) -> dict:
    out = {}
    for i in range(10):
        loc, dom = _mail(rng)
        out[f"symbol_dash_{i}"] = f"write {loc} at-symbol {dom}.com today"
        out[f"symbol_space_{i}"] = f"write {loc} at symbol {dom}.com today"
        out[f"at_at_{i}"] = f"write {loc} at at {dom}.com today"
        for j, e in enumerate(EMOJI):
            out[f"emoji{j}_{i}"] = f"write {loc}{e}{dom}.com today"
            out[f"emoji{j}_spaced_{i}"] = f"write {loc} {e} {dom}.com today"
    return out


def test_new_e_mail_spellings_are_found_in_both_lanes():
    cases = _spelled(random.Random(SEED + 4))
    strict = [n for n, t in cases.items() if sg.check(t) != "f_email"]
    client = [n for n, t in cases.items() if "email" not in sg.client_view_findings(t)]
    assert strict == [] and client == []


def test_the_spelling_rules_are_load_bearing(monkeypatch):
    cases = _spelled(random.Random(SEED + 5))
    old = re.compile(r"(?i)(?<=[a-z0-9])[-_]%s[-_](?=[a-z0-9])|(?<![^ ])_%s_(?![^ ])|(?<![a-z])(?:at[- ]sign|arroba"
                     r"|arobase)(?![a-z])| ?@ ?" % ((r"(?:at[- ]?sign|at|arroba|arobase)",) * 2))
    monkeypatch.setattr(sg, "_VAR_AT", old)                             # mutation: the c0c09bd1 spellings removed
    assert sum(1 for t in cases.values() if sg.check(t) != "f_email") > 0
    assert sum(1 for t in cases.values() if "email" not in sg.client_view_findings(t)) > 0


# ================================================================================================ MUST 4: base64 suffix

def _b64_glued(rng) -> dict:
    out = {}
    for i in range(12):
        loc, dom = _mail(rng)
        for k in (6, 7, 8):
            p = base64.b64encode(f"contact {loc}{AT}{dom}.com now".encode()).decode().rstrip("=")
            out[f"suffix{k}_{i}"] = "ref " + p + _r(rng, ALNUM, k) + " end"
            out[f"prefix{k}_{i}"] = "ref " + _r(rng, ALNUM, k) + p + " end"
            card = _card(rng)
            q = base64.b64encode(f"card {card} ok".encode()).decode().rstrip("=")
            out[f"card_suffix{k}_{i}"] = "ref " + q + _r(rng, ALNUM, k) + " end"
    return out


def test_base64_with_a_glued_suffix_of_six_to_eight_characters_is_read_in_both_lanes():
    cases = _b64_glued(random.Random(SEED + 6))
    assert [n for n, t in cases.items() if sg.check(t) is None] == []
    assert [n for n, t in cases.items() if not sg.client_view_findings(t)] == []


def test_the_six_byte_trim_is_load_bearing(monkeypatch):
    cases = _b64_glued(random.Random(SEED + 7))
    monkeypatch.setattr(sg, "DECODE_TRIM", 3)                           # mutation: the c0c09bd1 trim undone
    missed = [n for n, t in cases.items() if not sg.client_view_findings(t)]
    assert len(missed) > 0


# ================================================================================================ MUST 5: price scope

def _price_forms(rng) -> tuple:
    shown, masked = [], []
    for _ in range(20):
        dom = _mail(rng)[1]
        p = f"{rng.randint(5, 999)}.{rng.randint(10, 99)}"
        shown += [f"It costs ${p} at {dom}.com.", f"Price: $1,{rng.randint(100, 999)}.99 at {dom}.com.",
                  f"Now {p} at {dom}.com only.", f"Only \u20ac{rng.randint(5, 99)} at {dom}.com."]
        masked += [f"The bundle is {p} at {dom} dot com.", f"Grab 2 at {dom}.com.", f"Now {p}{AT}{dom}.com only.",
                   f"Now ${p} [at] {dom}.com only.", f"Now ${p} (at) {dom}.com only.",
                   f"Now ${p} at sign {dom}.com only.", f"Ref {rng.randint(10000, 99999)} at {dom}.com.",
                   f"Now {p} at {dom}[.]com only."]
    return shown, masked


def test_the_price_exemption_covers_only_a_real_price_before_the_plain_spelled_at():
    shown, masked = _price_forms(random.Random(SEED + 8))
    assert sum(1 for t in shown if sg.client_view_findings(t)) == 0
    assert sum(1 for t in masked if "email" not in sg.client_view_findings(t)) == 0
    assert all(sg.check(t) is not None for t in shown + masked)        # the strict lane withholds every one


def test_the_price_scope_is_load_bearing(monkeypatch):
    shown, masked = _price_forms(random.Random(SEED + 9))
    monkeypatch.setattr(sg, "_cv_real_price", lambda v, s0, e0: True)   # mutation: any digits read as a price
    assert sum(1 for t in masked if "email" not in sg.client_view_findings(t)) > 0
    monkeypatch.undo()
    monkeypatch.setattr(sg, "_cv_real_price", lambda v, s0, e0: False)  # mutation: no price exempt
    assert sum(1 for t in shown if sg.client_view_findings(t)) > 0


# ================================================================================================ MUST 6: wrap join scope

def _wrap_prose(rng) -> list:
    out = []
    for _ in range(20):
        b = _r(rng, LOW, 7)
        out += [f"Visit us - shop at {b}.com - today", f"Great value - we sell at {b}.com - fast shipping",
                f"Order now - available at {b}.com - while stocks last"]
    return out


def _wrap_variants(rng) -> dict:
    out = {}
    for i in range(10):
        loc, dom = _mail(rng)
        out[f"hyphen_{i}"] = f"{loc}-\nat-\n{dom}-\ndot-\ncom"
        out[f"under_{i}"] = f"{loc}_\nat_\n{dom}_\ndot_\ncom"
        out[f"sign_{i}"] = f"{loc} at-\nsign {dom}.com"
        out[f"bracket_mid_{i}"] = f"{loc} [at] {dom[:3]}\n{dom[3:]} [dot] com"
        out[f"bracket_nbsp_{i}"] = f"{loc} [a\u00a0t] {dom}\u00a0[dot]\u00a0com"
        out[f"paren_mid_{i}"] = f"{loc} (a\nt) {dom[:4]}\n{dom[4:]} (dot) com"
        out[f"brace_mid_{i}"] = f"{loc} {{at}} {dom} {{d\not}} c\nom"
    return out


def test_the_wrap_join_only_touches_at_and_dot_variant_words():
    rng = random.Random(SEED + 10)
    prose, variants = _wrap_prose(rng), _wrap_variants(rng)
    assert sum(1 for t in prose if sg.client_view_findings(t)) == 0
    assert [n for n, t in variants.items() if sg.check(t) != "f_email"] == []
    assert [n for n, t in variants.items() if "email" not in sg.client_view_findings(t)] == []


def test_the_wrap_join_never_glues_prose_neighbours():
    rng = random.Random(SEED + 44)
    for _ in range(20):
        a, b, c = _r(rng, LOW, 6), _r(rng, LOW, 7), _r(rng, LOW, 5)
        t = f"Great {a} - visit {b} - our {c} period sale"             # a variant hint ("period") elsewhere
        assert f"{a} - visit {b} - our" in sg._spelled_variants(t)
        assert sg._wrap_join(f"{a}-\nat-\n{b}".replace("\n", " ")) == f"{a}-at-{b}"


def test_the_wrap_scope_is_load_bearing(monkeypatch):
    rng = random.Random(SEED + 11)
    variants = _wrap_variants(rng)
    monkeypatch.setattr(sg, "_WRAP_WORDS", frozenset(("sign", "symbol")))   # mutation: no at / dot
    assert sum(1 for n, t in variants.items() if n.startswith(("hyphen", "under"))
               and "email" not in sg.client_view_findings(t)) > 0
    monkeypatch.undo()
    monkeypatch.setattr(sg, "_wrap_join", lambda v: sg._WRAP_SEP.sub(r"\1", v))   # mutation: the old unscoped join
    a, b = _r(rng, LOW, 6), _r(rng, LOW, 7)
    assert f"{a} - visit {b} - our" not in sg._spelled_variants(f"Great {a} - visit {b} - our x period sale")
    monkeypatch.undo()
    monkeypatch.setattr(sg, "_cv_wrapped_domain", lambda tail: False)   # mutation: no wrapped bracketed domain
    assert sum(1 for n, t in variants.items() if n.startswith(("bracket_mid", "paren_mid"))
               and "email" not in sg.client_view_findings(t)) > 0


# ================================================================================================ MUST 7 / 8: numbers

def _number_word_cases(rng) -> dict:
    out = {}
    for i in range(15):
        a, g, s = r6._ssn(rng)
        out[f"ssn_words_{i}"] = ("ssn", f"my number is {_words(a)}, {_words(g)}, {_words(s)} thanks")
        out[f"ssn_words_dash_{i}"] = ("ssn", f"it is {_words(a)} - {_words(g)} - {_words(s)}")
        a3, b3, c4 = r6._nanp(rng)
        out[f"phone_words_{i}"] = ("phone", f"reach me on {_words(a3)}, {_words(b3)}, {_words(c4)} please")
        out[f"phone_words_flat_{i}"] = ("phone", f"reach me on {_words(a3 + b3 + c4)} please")
        out[f"phone_words_one_{i}"] = ("phone", f"dial {_words('1' + a3 + b3 + c4)} now")
    return out


def _labelled_phones(rng) -> dict:
    out = {}
    for i in range(6):
        for lab in ("call", "tel:", "phone", "mobile", "cell", "whatsapp", "text", "sms"):
            a, b, c = r6._nanp(rng)
            n = rng.choice((10, 11, 12, 13))
            digits = (a + b + c + _r(rng, string.digits, 5))[:n]
            out[f"{lab}_{i}"] = f"please {lab} {digits} after five"
    return out


def test_the_client_view_reads_number_words_for_ssns_and_phones():
    cases = _number_word_cases(random.Random(SEED + 12))
    assert [n for n, (k, t) in cases.items() if k not in sg.client_view_findings(t)] == []
    assert [n for n, (k, t) in cases.items() if sg.check(t) is None] == []


def test_the_number_word_rule_is_load_bearing(monkeypatch):
    cases = _number_word_cases(random.Random(SEED + 13))
    monkeypatch.setattr(sg, "_cv_number_words", lambda v: set())      # mutation
    assert sum(1 for k, t in cases.values() if k not in sg.client_view_findings(t)) > 0


def test_labelled_unformatted_phones_are_masked_in_the_client_view():
    rng = random.Random(SEED + 14)
    cases = _labelled_phones(rng)
    assert [n for n, t in cases.items() if "phone" not in sg.client_view_findings(t)] == []
    # an unlabelled order / SKU number is not a phone
    plain = [f"order {_r(rng, '123456789', 1)}{_r(rng, string.digits, 11)} shipped" for _ in range(30)]
    assert sum(1 for t in plain if "phone" in sg.client_view_findings(t)) == 0


def test_the_labelled_phone_rule_is_load_bearing(monkeypatch):
    cases = _labelled_phones(random.Random(SEED + 15))
    monkeypatch.setattr(sg, "_cv_labelled_phone", lambda m: False)     # mutation
    assert sum(1 for t in cases.values() if "phone" not in sg.client_view_findings(t)) > 0


# ================================================================================================ MUST 9: labels

LABELS = ("parola d'ordine", "parola d\u2019ordine", "mot de passe", "contrase\u00f1a", "senha", "kata sandi",
          "m\u1eadt kh\u1ea9u", "palavra-passe", "contrasenya")


def _label_forms(rng) -> dict:
    out = {}
    for lab in LABELS:
        for i in range(3):
            v = _secret_word(rng)
            spaced = " ".join(lab)
            out[f"{lab}|plain|{i}"] = f"{lab}: {v}"
            out[f"{lab}|spaced|{i}"] = f"{spaced} : {v}"
            tag = re.sub(r"[ '\u2019\-]", "_", lab)
            out[f"{lab}|xml|{i}"] = f"<{tag}>{v}</{tag}>"
            out[f"{lab}|json|{i}"] = json.dumps({lab: v}, ensure_ascii=False)
    return out


def test_multi_word_labels_with_apostrophes_and_hyphens_are_read_in_both_lanes():
    cases = _label_forms(random.Random(SEED + 16))
    assert [n.split("|")[1] for n, t in cases.items() if sg.check(t) is None] == []
    assert [n.split("|")[1] for n, t in cases.items() if "label_value" not in sg.client_view_findings(t)] == []


def test_the_new_labels_are_load_bearing(monkeypatch):
    cases = {n: t for n, t in _label_forms(random.Random(SEED + 17)).items() if n.startswith("parola")}
    monkeypatch.setattr(sg, "_JOINED_RX", re.compile("|".join(w for w in sg._JOINED if w != "paroladordine")))
    assert sum(1 for t in cases.values() if sg.check(t) is None) > 0   # the letter-spaced form needs the entry


# ================================================================================================ SHOULD: URL path labels

def test_the_value_after_a_label_segment_is_withheld_and_masked():
    rng = random.Random(SEED + 18)
    for _ in range(20):
        v = _r(rng, LOW, 9)
        for path in (f"/account/password/{v}", f"/token/{v}", f"/api-key/{v}", f"/reset-password/{v}"):
            url = "https://shop.test" + path
            assert v not in sg.safe_url(url) and v not in (callum.stored_url(url) or {}).get("url", "")
            shown, kind = sg.client_view_url(url)
            assert kind == sg.CREDENTIAL and v not in shown
    # the label word alone is kept, and an action word after it too; the fetch decision is unchanged
    assert sg.safe_url("https://shop.test/reset-password/confirm") == "https://shop.test/reset-password/confirm"
    assert sg.client_view_url("https://shop.test/account/api-keys")[1] is None


def test_decision_22_is_for_prose_only_never_a_url_path():
    rng = random.Random(SEED + 19)
    for _ in range(20):
        loc, dom = _mail(rng)
        url = f"https://competitor.test/u/{loc}%20at%20{dom}.com/x"
        shown, kind = sg.client_view_url(url)
        assert kind == sg.PERSONAL and loc not in shown
        assert sg.client_view_findings(f"Visit {loc} at {dom}.com today") == []      # prose: shown (decision 22)


# ================================================================================================ SHOULD: codes / models

def _codes(rng) -> tuple:
    shown, masked = [], []
    for _ in range(20):
        b = _r(rng, LOW, 7)
        code = rng.choice(("SAVE", "PURR", "WELCOME", "X", "K", "Z")) + str(rng.randint(10, 990))
        shown += [f"Use code {code} at {b}.com for 15% off.", f"The {code} at amazon.com is cheaper."]
        masked += [f"Write {_r(rng, LOW, 5)}{rng.randint(10, 99)} at {b}.com today.",
                   f"Write {code.lower()} at {b}.com today.", f"Write {code}{AT}{b}.com today."]
    return shown, masked


def test_a_code_or_model_number_before_the_plain_at_is_prose_in_the_client_view():
    shown, masked = _codes(random.Random(SEED + 20))
    assert sum(1 for t in shown if sg.client_view_findings(t)) == 0
    assert sum(1 for t in masked if "email" not in sg.client_view_findings(t)) == 0


def test_the_code_rule_is_load_bearing(monkeypatch):
    shown, _ = _codes(random.Random(SEED + 21))
    monkeypatch.setattr(sg, "_CV_CODE", NEVER)                          # mutation
    assert sum(1 for t in shown if sg.client_view_findings(t)) == len(shown)


def _code_corpus(seed: int = SEED + 22) -> list:
    """A fresh e-commerce excerpt corpus (>= 520) where about 1 in 12 excerpts puts a code / model number directly
    before " at <domain>"."""
    r = random.Random(seed)
    answers, _ = c13.shop_corpus(seed)
    out = list(answers)
    i = 0
    while len(out) < 560:
        b = r.choice(c13.BRANDS)
        if i % 12 == 0:
            code = r.choice(("SAVE10", "PURR15", "WELCOME20", "X200", "K450", "ZX9", "B2G1"))
            t = r.choice((f"Use code {code} at {b}.com for {r.choice((10, 15, 20))}% off.",
                          f"The {code} at {r.choice(c13.RETAILERS)} costs less this week.",
                          f"Model {code} at {b}.com ships in {r.randint(2, 6)} days."))
        else:
            t = r.choice(c13.TEMPLATES).format(
                b=b, B=b.capitalize(), p=r.choice(c13.PRODUCTS), day="Friday", n=r.randint(2, 9), m=r.randint(2, 6),
                pr=f"{r.randint(5, 399)}.{r.randint(10, 99)}", r=r.choice(c13.RETAILERS), r2=r.choice(c13.RETAILERS),
                rt="4.5", color="grey", size="small", sku=f"PA-{r.randint(1000, 9999)}", model=f"X{r.randint(100, 990)}",
                model2=f"K{r.randint(100, 990)}", pct=15, code="SAVE10", w=r.randint(2, 30), a=r.randint(10, 40),
                c=r.randint(10, 40), k=r.randint(20, 900), year=2026, month="summer", where=f"{b}.com",
                store=r.choice(c13.RETAILERS))
        out.append(f"{b.capitalize()} answer: {t} Many buyers agree.")
        i += 1
    return out


CV_UTILITY_BAR = 0.05


def test_client_view_excerpt_utility_with_codes_is_under_the_bar():
    corpus = _code_corpus()
    assert len(corpus) >= 520
    coded = sum(1 for a in corpus if re.search(r"[A-Z0-9]*[0-9][A-Z0-9]* at ", a))
    assert coded >= len(corpus) // 20
    masked = sum(1 for a in corpus if c13._excerpt_masked(a))
    assert masked / len(corpus) <= CV_UTILITY_BAR, masked


# ================================================================================================ operator refusal rules

def test_operator_refusal_refuses_only_keys_pem_and_user_info():
    rng = random.Random(SEED + 23)
    refused = [f"compare tools {_gh(rng)}", f"tools {_aws(rng)} setup", f"key {_pem()} body", f"see {_userinfo(rng)}"]
    assert all(sg.operator_refusal(t) is not None for t in refused)
    accepted = [f"how to reset my password on {b}" for b in ("shopify", "amazon", "etsy")] + \
               ["api key setup for zapier", f"order {_card(rng)}", f"mail {_email(rng)}", f"ssn {_ssn(rng)}"]
    assert all(sg.operator_refusal(t) is None for t in accepted)


def test_operator_url_refusal_adds_credential_query_keys_by_exact_name():
    rng = random.Random(SEED + 24)
    for k in sg.OPERATOR_QUERY_KEYS:
        assert sg.operator_url_refusal(f"https://site.test/p?{k}={_r(rng, ALNUM, 10)}") == "url_credential", k
    for k in ("variant", "page", "q", "utm_source", "_sid", "id", "keyword", "token_count"):
        assert sg.operator_url_refusal(f"https://site.test/p?{k}={_r(rng, string.digits, 14)}") is None, k


# ================================================================================================ legitimate corpora

PLATFORMS = ("shopify", "amazon seller central", "etsy", "tiktok shop", "walmart marketplace", "ebay", "woocommerce",
             "bigcommerce", "google merchant center", "meta commerce manager", "klaviyo", "zapier", "quickbooks",
             "stripe", "paypal", "square", "wix", "squarespace", "gmail", "notion")
PRODUCTS = ("cat tree", "dog harness", "heated pet bed", "lip gloss", "magnesium glycinate", "sleep mask",
            "water fountain", "litter mat", "travel carrier", "grooming glove", "standing desk", "phone case")
CITIES = ("Long Beach", "Los Angeles", "Torrance", "Carson", "Irvine", "San Diego", "Austin", "Denver", "Miami")
PROMPT_TEMPLATES = (
    "how to reset my password on {p}", "api key setup for {p}", "best {x} for small apartments",
    "where to buy a {x} near {c}", "is {p} worth it for a small brand", "how do I recover a suspended {p} account",
    "{p} two factor authentication setup guide", "compare {p} and {p2} fees", "what is the token limit on {p}",
    "how to find my {p} access token settings page", "best {x} under 50 dollars", "who sells the {x} in {c}",
    "how to change the login password for {p}", "{p} secret shopper program reviews", "how to get a {p} api key",
    "recover revenue from {p} reimbursements", "who audits {p} listings in {c}", "best agency for {p} ads in {c}",
    "how long does {p} take to pay out", "is the {x} safe for kittens", "how to authenticate a {p} store",
    "where is the {p} password reset link", "how do coupon codes work at checkout on {p}",
    "what is a session id on {p}", "{p} pin verification letter help", "how to rotate my {p} credentials safely",
)
CONTEXT_TERMS = ("pet supplies", "password manager", "api", "cat tree", "SKU", "Long Beach", "token rewards",
                 "secret sauce", "login page", "pin badge", "auth flow", "marketplace", "reimbursement", "FBA",
                 "lip gloss", "sleep stack", "access code promo", "session", "cookie policy", "bearer bond")


def legit_prompts(n: int = 260, seed: int = SEED + 30) -> list:
    r = random.Random(seed)
    out: list = []
    while len(out) < n:
        t = r.choice(PROMPT_TEMPLATES).format(p=r.choice(PLATFORMS), p2=r.choice(PLATFORMS), x=r.choice(PRODUCTS),
                                              c=r.choice(CITIES))
        t = f"{t} {r.choice(('', 'in 2026', 'for beginners', 'step by step', 'quickly', 'today'))}".strip()
        if t not in out:
            out.append(t)
    return out


def legit_terms(n: int = 240, seed: int = SEED + 31) -> list:
    r = random.Random(seed)
    out: list = []
    while len(out) < n:
        t = f"{r.choice(CONTEXT_TERMS)} {r.choice(('', 'store', 'deal', 'kit', 'guide', 'ca', 'usa', '2026'))}".strip()
        t = t if t not in out else f"{t} {r.choice(PRODUCTS)}"
        if t not in out and len(t) <= 80:
            out.append(t)
    return out


def legit_paths(n: int = 240, seed: int = SEED + 32) -> list:
    r = random.Random(seed)
    out: list = []
    while len(out) < n:
        slug = "-".join(r.choice(PRODUCTS).split())
        t = r.choice((
            "/reset-password", "/account/login", f"/products/{slug}", f"/products/{slug}?variant={r.randint(10**13, 10**14 - 1)}",
            f"/cart?variant={r.randint(10**13, 10**14 - 1)}", f"/dp/B0{_r(r, string.ascii_uppercase + string.digits, 8)}",
            f"/collections/{slug}?page={r.randint(2, 9)}", f"/blog/{r.randint(2021, 2026)}/{r.randint(1, 12):02d}/{slug}",
            f"/search?q={slug.replace('-', '+')}", "/api-key-setup-guide", f"/p/{r.randint(10**11, 10**12 - 1)}",
            f"/forgot-password?return_to=/products/{slug}", f"/pages/{slug}-faq", "/account/api-keys",
            f"/itm/{r.randint(10**11, 10**12 - 1)}", f"/listing/{r.randint(10**9, 10**10 - 1)}/{slug}",
            f"/products/{slug}?utm_source=newsletter&utm_campaign={slug}", f"/{slug}/reviews?sort=newest",
            f"/order-status?order={r.randint(1000, 99999)}", f"/sitemap_products_{r.randint(1, 9)}.xml",
        ))
        if t not in out:
            out.append(t)
    return out


def legit_gsc(n: int = 240, seed: int = SEED + 45) -> list:
    """URL-prefix Search Console properties on the tenant's domain (no query: Google's URL-prefix form)."""
    r = random.Random(seed)
    out: list = []
    while len(out) < n:
        slug = "-".join(r.choice(PRODUCTS).split())
        t = r.choice((f"/{slug}/", f"/blog/{r.randint(2021, 2026)}/", f"/collections/{slug}/", f"/en-{r.choice(('us', 'ca', 'gb'))}/",
                      f"/products/{slug}-{r.randint(2, 99)}/", f"/dp/B0{_r(r, string.ascii_uppercase + string.digits, 8)}/",
                      "/reset-password/", "/account/", f"/shop/{r.choice(CITIES).lower().replace(' ', '-')}/{slug}/"))
        if t not in out:
            out.append(t)
    return out


def legit_entity_values(seed: int = SEED + 33) -> list:
    r = random.Random(seed)
    streets = ("East 2nd Street", "Pine Avenue", "Ocean Boulevard", "Atlantic Ave", "Main St", "Broadway",
               "Pacific Coast Hwy", "Long Beach Blvd", "Bellflower Blvd", "Anaheim St")
    out = []
    for i in range(240):
        k = i % 4
        if k == 0:
            out.append(("name", f"{r.choice(c13.BRANDS).capitalize()} {r.choice(('Media', 'Pets', 'Labs', 'Studio'))}"))
        elif k == 1:
            out.append(("street_address", f"{r.randint(10, 9999)} {r.choice(streets)}"
                                          f"{r.choice(('', ' Suite ' + str(r.randint(100, 900)), ' Unit B'))}"))
        elif k == 2:
            out.append(("locality", r.choice(CITIES)))
        else:
            a, b, c = r6._nanp(r)
            out.append(("telephone", f"({a}) {b}-{c}"))
    return out


def legit_provenance(n: int = 240, seed: int = SEED + 34) -> list:
    r = random.Random(seed)
    tmpl = ("confirmed on the Google Business Profile {d}", "from the owner's call on {d}", "invoice letterhead {d}",
            "password reset email footer, {d}", "Yelp listing checked {d}", "account settings page, {d}",
            "Andre re-confirmed on the dashboard {d}", "state filing {d}", "website contact page {d}",
            "API key setup screen of the listing tool, {d}")
    return [r.choice(tmpl).format(d=f"2026-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}") for _ in range(n)]


def legit_identifiers(seed: int = SEED + 35) -> dict:
    r = random.Random(seed)
    words = ("acme", "bright", "paws", "kitty", "haven", "maple", "river", "sunny", "glow", "zbest", "tail", "purr",
             "fur", "cat", "corner", "north", "south", "west", "labs", "media", "goods", "supply", "co", "shop")
    tlds = ("com", "co", "net", "io", "shop", "store", "us", "co.uk", "ca", "com.au")
    tenants, domains, finance = set(), set(), set()
    while len(tenants) < 240:
        tenants.add(f"{r.choice(words)}-{r.choice(words)}" + (f"-{r.randint(1, 99)}" if r.random() < 0.3 else ""))
    while len(domains) < 240:
        domains.add(f"{r.choice(words)}{r.choice(words)}{r.choice(('', str(r.randint(1, 99))))}.{r.choice(tlds)}")
    while len(finance) < 240:
        finance.add(r.choice((f"zbm-client-{r.randint(1, 999)}", f"{r.choice(words)}-party-{r.randint(1, 99)}",
                              f"ZBM.{r.choice(words).upper()}.{r.randint(1, 9999)}", f"cl_{r.choice(words)}_{r.randint(2024, 2026)}")))
    return {"tenant_id": sorted(tenants), "domain": sorted(domains), "finance_client_id": sorted(finance)}


FALSE_REFUSAL_BAR = 0.01


def _rate(n: int, d: int) -> float:
    return n / d if d else 0.0


def test_operator_free_text_false_refusals_per_field_are_under_one_percent():
    prompts, terms, paths = legit_prompts(), legit_terms(), legit_paths()
    ents, prov = legit_entity_values(), legit_provenance()
    assert min(len(prompts), len(terms), len(paths), len(ents), len(prov)) >= 220
    rates = {
        "prompt": _rate(sum(1 for t in prompts if sg.operator_refusal(t) is not None), len(prompts)),
        "context_term": _rate(sum(1 for t in terms if sg.operator_refusal(t) is not None), len(terms)),
        "audit_path": _rate(sum(1 for p in paths if svc_audits.path_refused("https", "site.test", p)), len(paths)),
        "entity_value": _rate(sum(1 for _f, v in ents if sg.operator_refusal(v) is not None), len(ents)),
        "provenance": _rate(sum(1 for t in prov if sg.operator_refusal(t) is not None), len(prov)),
        "gsc_property": _rate(sum(1 for p in legit_gsc() if sg.operator_url_refusal(f"https://site.test{p}") is not None),
                              len(legit_gsc())),
    }
    assert all(v <= FALSE_REFUSAL_BAR for v in rates.values()), {k: round(v, 4) for k, v in rates.items()}
    # the strict check alone (last round's rule) refused far more of the same legitimate input
    old = sum(1 for t in prompts if sg.check(t) is not None) / len(prompts)
    assert old > 0.05, old


def test_identifier_false_refusals_are_near_zero():
    ids = legit_identifiers()
    rates = {k: _rate(sum(1 for x in v if sg.identifier_refusal(x) is not None), len(v)) for k, v in ids.items()}
    assert all(len(v) >= 220 for v in ids.values())
    assert all(v <= FALSE_REFUSAL_BAR for v in rates.values()), {k: round(v, 4) for k, v in rates.items()}


def test_identifiers_with_a_key_digits_or_an_e_mail_are_refused():
    rng = random.Random(SEED + 36)
    bad = [f"acme-{_r(rng, string.digits, 9)}", f"x{_r(rng, string.digits, 12)}", _email(rng), _gh(rng),
           f"acme-{_aws(rng)}", f"password-{_secret_word(rng)}"]
    assert all(sg.identifier_refusal(x) is not None for x in bad)


def test_identifier_rules_are_load_bearing(monkeypatch):
    rng = random.Random(SEED + 37)
    digits = f"acme-{_r(rng, string.digits, 9)}"
    assert sg.identifier_refusal(digits) is not None
    monkeypatch.setattr(sg, "_ID_DIGITS", NEVER)
    monkeypatch.setattr(sg, "check", lambda t, waive=frozenset(): None)
    assert sg.identifier_refusal(digits) is None                       # mutation: the 9+ digit rule removed


# ================================================================================================ service: endpoints

class _Recording:
    """The fixture site's fetcher, recording every URL it is asked to fetch (in memory only)."""

    def __init__(self, inner):
        self.inner, self.fetched = inner, []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def fetch(self, url, *a, **kw):
        self.fetched.append(url)
        return self.inner.fetch(url, *a, **kw)


def _harness(tmp_path, data=True, srv=None):
    ports = None
    if srv is not None:
        from fixture_server import install_site
        from ports import Ports
        install_site(srv)
        ports = Ports.default()
        ports.fetcher = _Recording(srv.site_fetcher())
    h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data") if data else None, SEO_PROBE_SAMPLES="3",
                        ports=ports)
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [vw.DOMAIN]}, andre=True))
    return h


def _ps(name: str, prompts: list, rules=None) -> dict:
    b = {"request_id": rid(), "name": name, "brand_terms": ["Z Best Media"], "competitor_domains": ["competitor.test"],
         "prompts": prompts, "engines": ["openai"]}
    if rules is not None:
        b["brand_term_rules"] = rules
    return b


def test_every_operator_endpoint_accepts_the_legitimate_corpora(tmp_path):
    h = _harness(tmp_path, data=False)
    refused: dict = {}
    prompts = legit_prompts()
    ok = 0
    for i in range(0, len(prompts), 50):
        r = h.post("/tenants/zbm/prompt-sets", _ps(f"set-{i}", prompts[i:i + 50]))
        ok += len(prompts[i:i + 50]) if r.status_code == 201 else 0
    refused["prompt"] = len(prompts) - ok
    terms = legit_terms()
    ok = 0
    for i in range(0, len(terms), 10):
        r = h.post("/tenants/zbm/prompt-sets", _ps(f"rules-{i}", ["who recovers marketplace revenue"],
                                                   [{"term": "Z Best Media", "context_terms": terms[i:i + 5],
                                                     "negative_terms": terms[i + 5:i + 10]}]))
        ok += len(terms[i:i + 10]) if r.status_code == 201 else 0
    refused["context_term"] = len(terms) - ok
    paths = legit_paths()
    ok = 0
    m = h.settings.audit_max_pages
    for i in range(0, len(paths), m):
        r = h.post("/tenants/zbm/schedules", {"request_id": rid(), "domain": vw.DOMAIN, "paths": paths[i:i + m],
                                              "every_days": 7})
        ok += len(paths[i:i + m]) if r.status_code == 201 else 0
    refused["schedule_path"] = len(paths) - ok
    refused["audit_path"] = 0
    for p in paths:
        try:
            svc_audits._check_paths([p], 25, "https", vw.DOMAIN)
        except Exception:  # noqa: BLE001
            refused["audit_path"] += 1
    h.tenant("acme", domains=("acme.test",))
    ver = 0
    refused["entity_value"] = refused["provenance"] = 0
    h.ok(h.post("/tenants/acme/entity", {"request_id": rid(), "fields": [
        {"field": "name", "value": "Acme", "source": "founder_statement", "provenance": "stated", "authority": "first_party"}]},
        andre=True), 201)
    ver = 1
    for (f, v), pv in zip(legit_entity_values(), legit_provenance()):
        r = h.post("/entities/ent-acme/fields", {"request_id": rid(), "field": f, "value": v, "source": "dashboard",
                                                 "provenance": pv, "authority": "first_party",
                                                 "expected_version": ver}, andre=True)
        if r.status_code == 200:
            ver += 1
        else:
            refused["entity_value"] += 1
    gsc = legit_gsc()
    assert len(gsc) >= 220
    refused["gsc_property"] = 0
    for p in gsc:
        try:
            h.svc.set_gsc_property("zbm", {"request_id": rid(), "property": f"https://{vw.DOMAIN}{p}"})
        except Exception:  # noqa: BLE001
            refused["gsc_property"] += 1
    total = {"prompt": len(prompts), "context_term": len(terms), "schedule_path": len(paths), "audit_path": len(paths),
             "entity_value": 240, "provenance": 240, "gsc_property": len(gsc)}
    assert all(_rate(refused[k], total[k]) <= FALSE_REFUSAL_BAR for k in total), refused


def test_identifier_endpoints_accept_realistic_identifiers(tmp_path):
    h = _harness(tmp_path, data=False)
    ids = legit_identifiers()
    bad = {"tenant_id": 0, "domain": 0, "finance_client_id": 0}
    for t in ids["tenant_id"]:
        bad["tenant_id"] += h.post("/tenants", {"request_id": rid(), "tenant_id": t, "kind": "client"},
                                   andre=True).status_code != 201
    doms = ids["domain"]
    tids = ids["tenant_id"]
    for i in range(0, len(doms), 20):
        r = h.post(f"/tenants/{tids[i // 20]}/domains", {"request_id": rid(), "domains": doms[i:i + 20]}, andre=True)
        if r.status_code != 200:
            bad["domain"] += sum(1 for d in doms[i:i + 20] if sg.identifier_refusal(d) is not None
                                 or r.json().get("detail") != "DOMAIN_TAKEN")
    for f in ids["finance_client_id"]:
        bad["finance_client_id"] += h.post(f"/tenants/{tids[0]}/finance-client",
                                           {"request_id": rid(), "finance_client_id": f}, andre=True).status_code != 200
    assert all(_rate(v, 240) <= FALSE_REFUSAL_BAR for v in bad.values()), bad


def test_identifier_endpoints_refuse_keys_digits_and_e_mails(tmp_path):
    rng = random.Random(SEED + 38)
    h = _harness(tmp_path, data=False)
    n = len(h.svc.log)
    h.refused(h.post("/tenants", {"request_id": rid(), "tenant_id": f"acme-{_r(rng, string.digits, 9)}",
                                  "kind": "client"}, andre=True), 422, "IDENTIFIER_WITHHELD")
    h.tenant("acme")
    h.refused(h.post("/tenants/acme/domains", {"request_id": rid(), "domains": [f"x{_r(rng, string.digits, 10)}.com"]},
                     andre=True), 422, "IDENTIFIER_WITHHELD")
    for f in (_email(rng), f"cl-{_r(rng, string.digits, 11)}", "acme-" + _gh(rng)):
        h.refused(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": f}, andre=True),
                  422)
    h.refused(h.post("/tenants/zbm/prompt-sets", _ps("set-" + _r(rng, string.digits, 10), ["who recovers revenue"])),
              422)
    assert len(h.svc.log) == n + 1                                      # only the tenant "acme"


def test_identifier_validation_is_load_bearing_at_the_endpoint(tmp_path, monkeypatch):
    import svc_tenants
    rng = random.Random(SEED + 39)
    h = _harness(tmp_path, data=False)
    import sink_guard
    monkeypatch.setattr(svc_tenants, "identifier_refused", lambda x: False)   # mutation
    tid = f"acme-{_r(rng, string.digits, 9)}"
    # sink job A: with the endpoint's check removed the sink guard still refuses the id (nothing is written) ...
    h.refused(h.post("/tenants", {"request_id": rid(), "tenant_id": tid, "kind": "client"}, andre=True), 503,
              "SINK_REFUSED")
    assert tid not in h.svc.tenants
    monkeypatch.setattr(sink_guard, "enforce", lambda kind, data: None)        # ... and with both removed it gets in
    h.ok(h.post("/tenants", {"request_id": rid(), "tenant_id": tid, "kind": "client"}, andre=True), 201)


# ================================================================================================ the real service run

def _injected(rng) -> dict:
    """Secrets / personal data in every closed-list form that operator text may now carry (strong keys are refused;
    these are accepted and vaulted)."""
    a3, b3, c4 = r6._nanp(rng)
    s1, s2, s3 = r6._ssn(rng)
    loc, dom = _mail(rng)
    return {
        "email": _email(rng), "card": _card(rng), "ssn": _ssn(rng), "phone": _phone(rng),
        "spelled_local": loc, "label_value": _secret_word(rng), "digits": _r(rng, string.digits, 14),
        "ssn_words": f"{_words(s1)} {_words(s2)} {_words(s3)}", "phone_words": _words(a3 + b3 + c4),
        "token_value": _r(rng, ALNUM, 24), "spelled_dom": dom,
    }


def _scan(h, caplog, values: dict, extra: str = "") -> list:
    blob = vw.written(h, caplog) + extra
    return vw.found(blob, values)


@pytest.mark.local_http
def test_a_real_service_run_keeps_no_operator_text_on_disk_and_the_owner_reads_decrypt(tmp_path, caplog, srv):
    import logging
    caplog.set_level(logging.DEBUG)
    rng = random.Random(SEED + 40)
    v = _injected(rng)
    h = _harness(tmp_path, srv=srv)
    fetcher = h.ports.fetcher
    h.ports.engines["openai"] = vw.Port(vw.answers(vw.synthetic()))
    errors = ""
    # prompts (W4-3 still refuses an "@" address and digit runs: the forms it lets through are vaulted)
    prompts = [f"how to reset it, my password is {v['label_value']}", f"reach {v['spelled_local']} at {v['spelled_dom']}.com",
               f"verify {v['ssn_words']}", f"call {v['phone_words']}", "who recovers marketplace revenue"]
    rules = [{"term": "Z Best Media", "context_terms": [v["email"], v["card"], f"password is {v['label_value']}"],
              "negative_terms": [v["ssn"], v["phone"], v["digits"]]}]
    ps = h.ok(h.post("/tenants/zbm/prompt-sets", _ps("core", prompts, rules)), 201)
    # an entity on a client tenant: a card in the name, PII in the provenance
    h.tenant("acme", domains=("acme.test",))
    h.ok(h.post("/tenants/acme/entity", {"request_id": rid(), "fields": [
        {"field": "name", "value": f"Acme {v['card']}", "source": "founder_statement",
         "provenance": f"from {v['email']} on {v['phone']}", "authority": "first_party"}]}, andre=True), 201)
    h.ok(h.post("/entities/ent-acme/fields", {"request_id": rid(), "field": "street_address",
                                              "value": f"{v['ssn']} Main St", "source": "dashboard",
                                              "provenance": f"password is {v['label_value']}",
                                              "authority": "first_party", "expected_version": 1}, andre=True))
    # audit and schedule paths with every form; an audit runs (the fetcher answers DNS_FAILED)
    q = urllib.parse.quote
    paths = [f"/u/{q(v['email'])}/profile", f"/order/{v['card']}", f"/c/{v['phone'].replace(' ', '')}",
             f"/s/{v['ssn']}", f"/token/{v['token_value']}", f"/cart?variant={v['digits']}",
             f"/reset-password?email={q(v['email'])}", "/reset-password", "/"]
    audit = h.ok(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": vw.DOMAIN, "scheme": "http",
                                                 "paths": paths}), 201)
    assert audit["status"] == "completed"
    sched = h.ok(h.post("/tenants/zbm/schedules", {"request_id": rid(), "domain": vw.DOMAIN, "paths": paths,
                                                    "every_days": 7}), 201)
    # a URL-prefix Search Console property with personal data in its path
    h.svc.set_gsc_property("zbm", {"request_id": rid(), "property": f"https://{vw.DOMAIN}/u/{v['card']}/"})
    # a log ingest whose request paths carry a label and its value
    import test_logs as tl
    g = h.ok(tl.ingest(h), 201)
    lines = [tl.clf("66.249.66.1", f"/a/token-{v['token_value']}", 200, tl.GOOGLE_UA),
             tl.clf("66.249.66.1", "/a/" + q(f"password is {v['label_value']}"), 200, tl.GOOGLE_UA)]
    h.ok(tl.send(h, g["ingest_id"], 1, tl.b64(lines)))
    h.ok(h.post(f"/tenants/zbm/log-ingests/{g['ingest_id']}/finish", {"request_id": rid()}))
    # a citation run over the prompt set (the MNC list's prompt field passes the strict check or is None)
    h.ok(h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": vw.DOMAIN,
                                               "prompt_set_id": ps["prompt_set_id"]}), 201)
    # refusals: strong keys / PEM / user info / credential query keys: nothing written, the error carries nothing
    keys = {"gh": _gh(rng), "aws": _aws(rng)}
    for body in (_ps("bad", [f"tools {keys['gh']}"]), _ps("bad2", ["ok prompt"], [{"term": "Z Best Media",
                                                                                   "context_terms": [keys["aws"]]}])):
        r = h.post("/tenants/zbm/prompt-sets", body)
        errors += r.text
        assert r.status_code == 422 and r.json()["detail"] == "PROMPT_SECRET"
    r = h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": vw.DOMAIN, "paths": [f"/p?token={keys['gh']}"]})
    errors += r.text
    assert r.status_code == 422 and r.json()["detail"] == "PATH_WITHHELD"
    r = h.post("/tenants/zbm/schedules", {"request_id": rid(), "domain": vw.DOMAIN, "every_days": 7,
                                          "paths": [f"/p?api_key={_r(rng, ALNUM, 12)}"]})
    errors += r.text
    assert r.status_code == 422 and r.json()["detail"] == "PATH_WITHHELD"
    r = h.post("/entities/ent-acme/fields", {"request_id": rid(), "field": "name", "value": f"Acme {keys['aws']}",
                                             "source": "dashboard", "provenance": "x", "authority": "first_party",
                                             "expected_version": 2}, andre=True)
    errors += r.text
    assert r.status_code == 422 and r.json()["detail"] == "ENTITY_VALUE_WITHHELD"
    with pytest.raises(Exception) as ei:
        h.svc.set_gsc_property("zbm", {"request_id": rid(), "property": f"https://{vw.DOMAIN}/x/?key={keys['gh']}"})
    assert getattr(ei.value, "reason", None) == "GSC_PROPERTY_INVALID"
    # the scan: every file under the data directory, the ledger, the captured logs, every error response
    assert _scan(h, caplog, {**v, **keys}, errors) == []
    # the owner's reads decrypt and mask (a PII text is masked, an ordinary one shown as typed)
    got = h.ok(h.get(f"/tenants/zbm/prompt-sets/{ps['prompt_set_id']}"))
    cur = got["versions"][str(got["current"])]
    assert cur["prompts"][4] == "who recovers marketplace revenue"
    assert set(cur.get("masked", [])) >= {0, 2, 3}
    assert all(x not in json.dumps(got) for x in (v["label_value"], v["ssn_words"], v["phone_words"]))
    ent = h.ok(h.get("/tenants/acme/entity", caller="dashboard"))
    assert v["card"] not in json.dumps(ent) and v["email"] not in json.dumps(ent)
    assert ent["fields"]["name"].get("value_masked") and ent["fields"]["name"]["provenance_masked"]
    a = h.ok(h.get(f"/tenants/zbm/audits/{audit['audit_id']}"))
    assert "/reset-password" in a["paths"] and "/" in a["paths"]
    assert all(x not in json.dumps(a) for x in (v["email"], v["card"], v["ssn"], v["token_value"]))
    s = h.ok(h.get(f"/tenants/zbm/schedules/{sched['schedule_id']}"))
    assert all(x not in json.dumps(s) for x in (v["email"], v["card"], v["ssn"], v["token_value"]))
    t = h.ok(h.get("/tenants/zbm"))
    assert v["card"] not in json.dumps(t) and t.get("gsc_property_masked")
    # in memory the service has the text (prompts as typed go to the engine; paths are fetched)
    assert any(v["token_value"] in u for u in fetcher.fetched)
    # a restart replays the log: the text is opened from the vault again
    h2 = h.restart()
    p2 = h2.svc.prompt_sets[ps["prompt_set_id"]]["versions"]["1"]
    assert p2["prompts"] == [" ".join(p.split()) for p in prompts] and not p2.get("vault_unavailable")
    assert h2.svc.entities["ent-acme"]["fields"]["name"]["value"] == f"Acme {v['card']}"


def test_operator_vaulting_is_load_bearing(tmp_path, monkeypatch, caplog):
    """Mutation: the seal writes the text itself into the log record: the scan finds it."""
    rng = random.Random(SEED + 41)
    v = {"label_value": _secret_word(rng), "email": _email(rng)}
    monkeypatch.setattr(svc_optext.OperatorTextMixin, "_optext_seal",
                        lambda self, tid, field, texts, flags=None: list(texts))
    h = _harness(tmp_path)
    h.ok(h.post("/tenants/zbm/prompt-sets", _ps("core", [f"how to reset my password {v['label_value']}"],
                                                [{"term": "Z Best Media", "context_terms": [v["email"]]}])), 201)
    assert _scan(h, caplog, v) != []


def test_a_prompt_set_is_refused_without_a_vault_key_and_nothing_is_written(tmp_path):
    h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data"), SEO_VAULT_KEY_FILE=None)
    n = len(h.svc.log)
    h.refused(h.post("/tenants/zbm/prompt-sets", _ps("core", ["who recovers marketplace revenue"])), 503,
              "VAULT_UNAVAILABLE")
    assert len(h.svc.log) == n


def test_a_lost_vault_record_leaves_the_prompt_set_unusable(tmp_path):
    h = _harness(tmp_path)
    ps = h.ok(h.post("/tenants/zbm/prompt-sets", _ps("core", ["who recovers marketplace revenue"])), 201)
    import os
    os.remove(os.path.join(h.settings.data_dir, "operator_text_vault.jsonl"))
    h2 = h.restart()
    assert h2.svc.prompt_sets[ps["prompt_set_id"]]["versions"]["1"].get("vault_unavailable")
    h2.refused(h2.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": vw.DOMAIN,
                                                      "prompt_set_id": ps["prompt_set_id"]}), 503, "VAULT_UNAVAILABLE")


def test_idempotency_and_the_content_hash_use_the_hmac(tmp_path):
    h = _harness(tmp_path)
    body = _ps("core", ["how to reset my password on shopify"])
    a = h.ok(h.post("/tenants/zbm/prompt-sets", body), 201)
    b = h.ok(h.post("/tenants/zbm/prompt-sets", body), 201)                 # the same request: the same answer
    assert a == b
    h.refused(h.post("/tenants/zbm/prompt-sets", {**body, "request_id": rid()}), 409, "PROMPT_SET_EXISTS")
    rec = [r for r in h.svc.log.records if r["kind"] == "prompt_set_version"][-1]
    assert "how to reset" not in json.dumps(rec)
    assert all(svc_optext.is_stub(x) for x in rec["data"]["content"]["prompts"])


# ================================================================================================ MUST 10: entity waiver

def test_the_telephone_waiver_applies_to_the_telephone_field_only(tmp_path):
    rng = random.Random(SEED + 42)
    h = _harness(tmp_path)
    card, ssn = _card(rng), _ssn(rng)
    h.tenant("acme", domains=("acme.test",))
    h.ok(h.post("/tenants/acme/entity", {"request_id": rid(), "fields": [
        {"field": "name", "value": f"Acme {card}", "source": "founder_statement", "provenance": "stated",
         "authority": "first_party"},
        {"field": "street_address", "value": f"{ssn} Main St", "source": "founder_statement", "provenance": "stated",
         "authority": "first_party"},
        {"field": "telephone", "value": "(562) 248-6617", "source": "founder_statement", "provenance": "stated",
         "authority": "first_party"}]}, andre=True), 201)
    rec = [r for r in h.svc.log.records if r["kind"] == "entity_created"][-1]["data"]["fields"]
    assert rec["name"]["value"]["flag"] == "e_digits" and rec["street_address"]["value"]["flag"] == "e_digits"
    assert rec["telephone"]["value"]["flag"] is None
    assert svc_entity.value_flag("locality", card) == "e_digits" and svc_entity.value_flag("telephone", "5622486617") \
        is None


def test_the_telephone_waiver_is_load_bearing(monkeypatch):
    rng = random.Random(SEED + 43)
    card = _card(rng)
    assert svc_entity.value_flag("name", card) == "e_digits"
    monkeypatch.setattr(svc_entity, "_waiver", lambda field: frozenset(("e_digits",)))   # mutation: every field
    assert svc_entity.value_flag("name", card) is None


# ================================================================================================ timing

def _timed(fn, arg) -> float:
    best = None
    for _ in range(2):
        t = time.perf_counter()
        fn(arg)
        d = time.perf_counter() - t
        best = d if best is None else min(best, d)
    return best


ADVERSARIAL = {
    "wrap_prose": "a - b - ", "wrap_at": "x - at - ", "price": "$1,299.99 at ", "code": "SAVE10 at ",
    "num_words": "one two three, ", "phone_label": "call 5551234 ", "emoji_at": "x\u2709y ", "at_at": "x at at ",
    "bracket_wrap": "x [at] a b c ", "b64_glued": "QUJDREVGR0hJSktM" + "x1y2z3w4 ",
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_both_lanes_stay_linear_on_the_new_rules(name):
    unit = ADVERSARIAL[name]
    for fn in (sg.check, sg.client_view_findings, sg.operator_refusal):
        t16 = _timed(fn, c13.t8._fill(unit, 16 * 1024))
        t64 = _timed(fn, c13.t8._fill(unit, 64 * 1024))
        assert t64 < 1.0, (name, fn.__name__)
        assert t64 < 8 * t16 + 0.05, (name, fn.__name__, t16, t64)


def test_the_new_reason_is_in_the_closed_catalogue():
    assert reasons.R("IDENTIFIER_WITHHELD") == "IDENTIFIER_WITHHELD"


# ================================================================================================ every entry point

def _text_cases() -> dict:
    """class -> texts every strict entry point must withhold and the client view must mask (its required class)."""
    rng = random.Random(SEED + 50)
    return {
        "spellings": list(_spelled(rng).values()),
        "b64_glued": list(_b64_glued(rng).values()),
        "wrap_variants": list(_wrap_variants(rng).values()),
        "number_words": [t for _k, t in _number_word_cases(rng).values()],
        "labelled_phones": list(_labelled_phones(rng).values()),
        "labels": list(_label_forms(rng).values()),
        "prices_masked": _price_forms(rng)[1],
    }


def test_every_strict_entry_point_withholds_the_new_cases():
    misses: dict = {}
    for cls, texts in _text_cases().items():
        for t in texts:
            bad = []
            if sg.check(t) is None:
                bad.append("check")
            if sg.redact_text(t)[0] != sg.WITHHELD:
                bad.append("redact_text")
            g = sg.guard_record({"note": t, t: "x"})
            if t in json.dumps(g):
                bad.append("guard_record")
            if callum.field_check(t) is None:
                bad.append("field_check")
            e = callum.excerpt("Z Best Media " + t, ["Z Best Media"])
            if t in json.dumps(e):
                bad.append("excerpt")
            u = "https://competitor.test/p/" + urllib.parse.quote(t, safe="")
            if urllib.parse.quote(t, safe="") in sg.safe_url(u) or urllib.parse.quote(t, safe="") in json.dumps(
                    callum.stored_url(u)):
                bad.append("url")
            for b in bad:
                misses[(cls, b)] = misses.get((cls, b), 0) + 1
    assert misses == {}


def recall_table() -> dict:
    """class -> (samples, strict misses, client misses of the required class)."""
    out = {}
    for cls, texts in _text_cases().items():
        out[cls] = (len(texts), sum(1 for t in texts if sg.check(t) is None),
                    sum(1 for t in texts if not sg.client_view_findings(t)))
    return out


def test_the_recall_table_is_complete():
    table = recall_table()
    assert all(s >= 20 and strict == 0 and client == 0 for s, strict, client in table.values()), table
