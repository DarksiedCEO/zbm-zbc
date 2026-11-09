"""Idris round 2 of the 0110a226 certifying review (ADR 0048 "Review of 0110a226"): every finding fixed as a CLASS,
with generated cases. Every key-shaped value here is DERIVED at run time from a hash (``tok``): there is no
secret-shaped literal in this file, and nothing here is a real credential.

  M1  labelled credentials, any separator, no token gate, non-English labels
  M2  candidate names never carry a secret or personal datum (URL user parts, percent-encoding, URL / e-mail words)
  M3  token shapes: Slack, glued AWS ids, NFKC / invisible characters, tokens broken over lines
  M4  nothing un-redacted reaches data/seo_log.jsonl through the rows this service records (final guard, fail closed)
  M5  multi-letter lookalikes (``cl`` / ``d``, ``rn`` / ``m``, ``vv`` / ``w``, ``ii`` ``il`` ``li`` / ``u``)
  M6  the client's brand in ANY host label is a client lookalike; confirm and the competitor set refuse it
  S1  a retry whose citation run was lost (PROCESS_LOST) is refused 409, nothing stored under the id
  S2  ``_excerpt``'s two redactions are each needed
  S3  over-redaction measured on a corpus of legitimate text
  S4  two-edit typos and word-order swaps of long client names
"""

from __future__ import annotations

import hashlib
import json
import random

import pytest
import test_idris as ti
from helpers import rid

from agents import idris, text_redact

ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
HEXL = "0123456789abcdef"


def tok(label: str, n: int, alphabet: str = ALNUM) -> str:
    """A key-shaped test value derived at run time (no secret-shaped literal in the source)."""
    out, i = "", 0
    while len(out) < n:
        for byte in hashlib.sha256(f"seo-py idris r2 test-only {label} {i}".encode()).digest():
            out += alphabet[byte % len(alphabet)]
        i += 1
    return out[:n]


def short(s: str) -> str:
    """A credential-shaped value truncated for assertion messages (never printed whole)."""
    return s[:4] + "..." if len(s) > 4 else "..."


def strong_keys() -> dict:
    """Documented provider shapes, built at run time."""
    return {
        "aws_id": "AK" + "IA" + tok("aws", 16, UPPER),
        "aws_secret": tok("aws s", 19) + "/" + tok("aws s2", 19) + "+",
        "openai": "sk-" + "proj-" + tok("oa", 40),
        "anthropic": "sk-" + "ant-api03-" + tok("an", 40),
        "github": "gh" + "p_" + tok("gh", 36),
        "github_pat": "github" + "_pat_" + tok("ghpat", 40),
        "stripe": "sk" + "_live_" + tok("st", 24),
        "slack_bot": "xo" + "xb-" + tok("sl", 12, "0123456789") + "-" + tok("sl2", 24),
        "slack_user": "xo" + "xp-" + tok("su", 12, "0123456789") + "-" + tok("su2", 24),
        "slack_e": "xo" + "xe.xo" + "xp-1-" + tok("se", 40),
        "slack_x": "xo" + "xz-" + tok("sx", 30),
        "google": "AI" + "za" + tok("go", 35),
        "perplexity": "pp" + "lx-" + tok("pp", 48),
        "gitlab": "gl" + "pat-" + tok("gl", 20),
        "hf": "h" + "f_" + tok("hf", 34),
        "jwt": "ey" + "J" + tok("j1", 17) + ".ey" + "J" + tok("j2", 30) + "." + tok("j3", 43),
    }


def leaked(secret: str, out: str) -> bool:
    """Any 12-character piece of the secret (past its public prefix) still in ``out``."""
    body = secret[-24:]
    return any(body[i:i + 12] in out for i in range(0, len(body) - 11))


# ---------------------------------------------------------------------------------------------- M1 labelled values

LABELS = ["password", "Password", "PASSWORD", "passwd", "pwd", "passcode", "passphrase", "PIN", "pin code", "PIN number",
          "access code", "auth", "api key", "api-key", "API Key", "apikey", "api_key", "token", "Token", "auth token",
          "access token", "secret", "client secret", "client_secret", "secret key", "private key", "credentials",
          "OPENAI_API_KEY", "db.password", "x-api-key", "session token", "contraseña", "Contraseña", "mot de passe",
          "Mot de passe", "passwort", "Passwort", "Kennwort", "senha", "Senha", "wachtwoord", "lösenord", "hasło",
          "пароль", "密码", "パスワード", "비밀번호", "clave", "código de acceso", "jeton", "токен"]
SEPS = [": ", ":", "=", " = ", " is ", " - ", " → ", " -> ", " ", "  ", "\t", ":\n", " is: ", " => ", "\uff1a", "=\"", " — "]


def _values(rnd: random.Random) -> list:
    return [tok(f"v{rnd.random()}", rnd.randint(6, 30)),
            tok(f"w{rnd.random()}", 8, "abcdefghijklmnopqrstuvwxyz"),          # letters only: no token gate
            tok(f"x{rnd.random()}", 7, "abcdefghijklmnopqrstuvwxyz") + "!",
            "Hunter" + tok(f"d{rnd.random()}", 3, "0123456789")]


@pytest.mark.parametrize("label", LABELS)
def test_m1_a_labelled_value_is_redacted_for_every_separator(label):
    rnd = random.Random(f"m1 {label}")
    for sep in SEPS:
        for v in _values(rnd):
            text = f"Refund Rangers note. {label}{sep}{v} and more."
            if sep == "=\"":
                text = f"Refund Rangers note. {label}=\"{v}\" and more."
            out = text_redact.secrets(text)
            assert v not in out, (label, sep, short(v), out[:60])
            assert text_redact.SECRET in out and "Refund Rangers" in out


def test_m1_passphrases_with_spaces_and_quoted_values():
    words = " ".join(tok(f"pp{i}", 6, "abcdefghijklmnopqrstuvwxyz") for i in range(4))
    for label in ("password", "passphrase", "contraseña", "mot de passe", "Passwort"):
        for sep in (": ", " is ", " = ", " - ", " → "):
            out = text_redact.secrets(f"{label}{sep}{words}. Next sentence here.")
            assert not any(w in out for w in words.split()), (label, sep)
            assert "Next sentence here." in out
    v = tok("q1", 10) + " " + tok("q2", 10)
    out = text_redact.secrets(f'Use token "{v}" for it.')
    assert v.split()[0] not in out and v.split()[1] not in out


def test_m1_an_opening_quote_without_a_closing_one_is_still_redacted():
    rnd = random.Random(48036)
    for label in ("api key", "PIN", "password", "jeton", "wachtwoord"):
        for sep in ('="', " = '", ': "', "=\u201c"):
            v = tok(f"oq{rnd.random()}", 12)
            assert v not in text_redact.secrets(f"{label}{sep}{v} rest"), (label, sep)


def test_m1_short_values_and_labels_inside_words_are_left():
    for t in ("The PIN is ok", "A token of thanks.", "auth flow", "Spinach and pineapple", "authentic brands",
              "Tokenomics matter.", "the secret to winning the Buy Box"):
        assert text_redact.secrets(t) == t, t


# ---------------------------------------------------------------------------------------------- M3 shapes

@pytest.mark.parametrize("kind", sorted(strong_keys()))
def test_m3_every_strong_shape_survives_no_disguise(kind):
    secret = strong_keys()[kind]
    disguises = {
        "plain": f"see {secret} now",
        "glued_word": f"see token{secret} now" if not secret[0].isalnum() else f"see x{secret} now",
        "glued_digit": f"see 9{secret} now",
        "fullwidth": "see " + "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in secret) + " now",
        "zero_width": "see " + secret[:6] + "​" + secret[6:12] + "‍" + secret[12:] + " now",
        "bidi": "see ‮" + secret[:8] + "⁦" + secret[8:] + "⁩ now",
        "soft_hyphen": "see " + secret[:10] + "­" + secret[10:] + " now",
        "word_joiner": "see " + secret[:5] + "⁠" + secret[5:] + " now",
        "line_break": "see " + secret[:9] + "\n" + secret[9:] + " now",
        "line_break_indent": "see " + secret[:14] + "\n    " + secret[14:] + " now",
        "crlf": "see " + secret[:11] + "\r\n" + secret[11:] + " now",
        "spaced": "see " + " ".join(secret) + " now",
        "percent": "see " + "".join(f"%{ord(c):02X}" if i % 3 == 0 else c for i, c in enumerate(secret)) + " now",
        "entity": "see " + "".join(f"&#{ord(c)};" if i % 4 == 0 else c for i, c in enumerate(secret)) + " now",
    }
    for name, text in disguises.items():
        out = text_redact.redact(text)
        assert not leaked(secret, out), (kind, name, out[:20])
        if not (kind == "aws_secret" and name.startswith("glued")):       # glued, it is a weak (base64) match
            assert text_redact.strong_secret(text), (kind, name)


def test_m3_glued_aws_access_key_id_is_matched():
    k = "AK" + "IA" + tok("glue", 16, UPPER)
    for left in ("x", "AWS", "id", "9", "_", "key"):
        assert k not in text_redact.secrets(left + k), left


def test_m3_strong_key_bar_zero_leaks_property():
    """The department-wide bar: zero strong-key leaks over seeded random contexts and disguises."""
    rnd = random.Random(480303)
    keys = strong_keys()
    for _ in range(600):
        secret = keys[rnd.choice(sorted(keys))]
        pieces = list(secret)
        for _j in range(rnd.randint(0, 3)):
            i = rnd.randrange(1, len(pieces))
            pieces.insert(i, rnd.choice(["​", "‌", "‍", "﻿", "­", "⁠", "‬"]))
        s = "".join(pieces)
        if rnd.random() < 0.3:
            s = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" and rnd.random() < 0.5 else c for c in s)
        left = rnd.choice(["", " ", "(", "\n", "**", ": ", "=", "'", '"', "x", "9", "token ", "key="])
        right = rnd.choice(["", " ", ")", "\n", "**", ".", ",", "x"])
        text = "Refund Rangers helps. " + left + s + right + " Z Best Media too."
        assert not leaked(secret, text_redact.redact(text)), short(secret)


# ---------------------------------------------------------------------------------------------- M2 candidate names

def _observe_text(text, cites=("https://www.refundrangers.com/",)):
    e = ti.engines(openai=[(0, ti.ans(text, list(cites)))] * 3,
                   anthropic=[(0, ti.ans(text, list(cites), provider="anthropic"))] * 3)
    obs = ti.observe(e)
    return obs, ti.compute(obs)


def _dump(*objs) -> str:
    return "".join(json.dumps(o, ensure_ascii=False) for o in objs)


def test_m2_url_user_parts_and_encoded_secrets_never_become_a_candidate():
    pw = tok("userinfo", 14)
    k = strong_keys()["github"]
    enc = "".join(f"%{ord(c):02X}" for c in k)
    texts = [f"Options:\n1. **Refund Rangers** at https://admin:{pw}@portal.example.com/login - a claims agency.",
             f"Options:\n1. **Refund Rangers** - see ftp://{pw}@files.example.net/ for the list.",
             f"Options:\n1. **Refund Rangers** - login admin:{pw}@portal.example.org for it.",
             f"Options:\n1. **{enc}** - a claims agency.\n2. **Refund Rangers** helps.",
             f"Options:\n1. **Refund Rangers** - a claims agency, {enc}%2520 here.",
             f"Options:\n1. **Refund Rangers** - https://x.example.com/?q=%25{enc[3:]}"]
    for text in texts:
        obs, env = _observe_text(text)
        out = _dump(obs, env)
        assert pw not in out and not leaked(k, out), text[:40]
        for c in obs["candidates"]:
            for d in c["display"]:
                assert not text_redact.sensitive(d), d


def test_m2_words_inside_a_url_or_email_address_are_never_names():
    texts = ["Options:\n1. **Refund Rangers** - read https://blog.example.com/Quillmark-Velotrax-Review/ today.",
             "Options:\n1. **Refund Rangers** - read www.example.com/Brandoria/Zentrix for it.",
             "Options:\n1. **Refund Rangers** - read example.com/Orvalink?ref=Pexoria today.",
             "Options:\n1. **Refund Rangers** - write to Kalvenor.Smith@Mailoria.example.org now.",
             "Options:\n1. **Refund Rangers** - mailto:Trivexa@example.com for it."]
    banned = ("quillmark", "velotrax", "brandoria", "zentrix", "orvalink", "pexoria", "kalvenor", "mailoria", "trivexa")
    for text in texts:
        obs, _env = _observe_text(text)
        keys = {c["key"] for c in obs["candidates"]}
        assert not any(b in k for b in banned for k in keys), (text[:50], keys)
        assert "refundrangers" in keys


def test_m2_personal_data_is_read_from_the_raw_text_and_never_a_name():
    phone = "(" + tok("ph", 3, "23456789") + ") " + tok("ph2", 3, "23456789") + "-" + tok("ph3", 4, "0123456789")
    email = tok("em", 8, "abcdefghijklmnopqrstuvwxyz") + "@example.org"
    obf = tok("ob", 6, "abcdefghijklmnopqrstuvwxyz") + " [at] example [dot] org"
    pct = tok("pc", 6, "abcdefghijklmnopqrstuvwxyz") + "%40example.org"
    for pd in (phone, email, obf, pct, email.replace("@", "​@")):
        text = f"Options:\n1. **Refund Rangers** - call {pd} today.\n2. **Getida** helps."
        obs, env = _observe_text(text)
        out = _dump(obs, env)
        assert pd not in out and pd.split("@")[0].split(" ")[0][:6] not in out.replace("Refund", ""), pd[:4]


def test_m2_the_extractor_refuses_a_sensitive_display():
    """``candidates`` is public: called on raw text, a key-shaped or token-shaped word is never a name."""
    for k in (strong_keys()["aws_id"], tok("opaque", 36), "Brand" + tok("weak", 30)):
        ex: dict = {}
        found = idris.candidates(f"Options:\n1. **{k}** - a claims agency.\n2. **Getida** helps.", ti.CTX, ex)
        assert [f["key"] for f in found] == ["getida"] and ex.get("SENSITIVE") == 1, short(k)


def test_m2_prepare_decodes_collapses_and_strips_user_parts():
    pw = tok("prep", 12)
    t = text_redact.prepare(f"See https://admin:{pw}@portal.example.com/x and %4A%61%6E%65 then  password" + " " * 30
                            + tok("prep2", 9, "abcdefghij") + " ok. Mail " + tok("pm", 6, "abcdefgh") + "%40example.org")
    assert pw not in t and "//portal.example.com/x" in t and "Jane" in t
    assert "password [redacted:secret]" in t and text_redact.EMAIL in t and "%40" not in t
    assert text_redact.prepare("a\n\n\n\n\nb   c") == "a\n\nb c"


def test_m2_a_value_padded_away_from_its_label_never_becomes_a_name():
    """prepare() collapses whitespace before Idris reads the text, so a label and its value are read together."""
    v = "Hunter" + tok("pad", 4, "0123456789") + "Zq"
    for pad in (" " * 30, "\t" * 20, " \u00a0" * 12):
        text = f"Options:\n1. **Refund Rangers** - a claims agency. password{pad}{v} for the portal.\n2. Getida."
        obs, env = _observe_text(text)
        assert v not in _dump(obs, env), repr(pad[:2])


def test_m3_the_whole_slack_refresh_token_is_one_redaction():
    k = strong_keys()["slack_e"]
    out = text_redact.secrets(f"see {k} now")
    assert out == f"see {text_redact.SECRET} now"


def test_m2_secrets_strips_url_user_parts_by_itself():
    pw = tok("ui", 12)
    for t in (f"https://admin:{pw}@host.example.com/", f"ftp://{pw}@files.example.net/", f"use admin:{pw}@host.example.org"):
        assert pw not in text_redact.secrets(t), t[:8]


# ---------------------------------------------------------------------------------------------- M4 storage

def _log_text(h) -> str:
    import pathlib
    return (pathlib.Path(h.settings.data_dir) / "seo_log.jsonl").read_text(encoding="utf-8")


def test_m4_callum_rows_recorded_through_idris_carry_no_secret_to_the_log(tmp_path):
    keys = strong_keys()
    pw = tok("m4pw", 12)
    lines = [f"Z Best Media {name}: {k}" for name, k in sorted(keys.items())]
    text = ("Z Best Media helps sellers. password: " + pw + " now.\n" + "\n".join(lines)
            + "\nZ Best Media again, Z Best Media.")
    a = ti.ans(text, ["https://site.test/x", f"https://user:{pw}@site.test/y", "https://www.refundrangers.com/"])
    h, _fakes, psid = ti.svc(tmp_path, data_dir=str(tmp_path / "data"), openai=[a], anthropic=[a])
    h.ok(ti.comp_set(h), 201)
    v = h.ok(ti.vis(h, psid), 201)
    log = _log_text(h)
    assert "citation_run_recorded" in log and "competitor_observations_recorded" in log
    cr = h.ok(h.get(f"/tenants/zbm/citation-runs/{v['citation_run_id']}"))
    for name, k in keys.items():
        assert not leaked(k, log), name
        assert not leaked(k, json.dumps(cr)), name
    assert pw not in log
    assert text_redact.SECRET in log                               # the excerpts were kept, redacted
    for row in cr["result"]["facts"]["rows"]:
        ex = ((row.get("evidence") or {}).get("excerpt") or {})
        if ex.get("text"):
            assert not text_redact.strong_secret(ex["text"]) and not text_redact.personal_hit(ex["text"])
            assert ex["sha256"] == hashlib.sha256(ex["text"].encode()).hexdigest()


def test_m4_the_final_guard_fails_closed_when_redaction_misses(monkeypatch):
    k = strong_keys()["openai"]
    monkeypatch.setattr(text_redact, "redact", lambda s: s)               # a redaction that misses everything
    text_redact._clean_one.cache_clear()
    try:
        out, stats = text_redact.scrub({"rows": [{"evidence": {"excerpt": {"untrusted": True, "text": f"see {k}",
                                                                          "sha256": "0" * 64}}}],
                                        "model": f"m {k}", "note": "Refund Rangers"})
    finally:
        text_redact._clean_one.cache_clear()
    blob = json.dumps(out)
    assert k not in blob and stats["withheld"] == 2
    assert out["rows"][0]["evidence"]["excerpt"]["text"] == text_redact.WITHHELD
    assert out["note"] == "Refund Rangers"


def test_m4_the_guard_fails_closed_on_an_internal_error(monkeypatch):
    def boom(_s):
        raise RuntimeError("x")
    monkeypatch.setattr(text_redact, "_clean_one", boom)
    out, stats = text_redact.scrub({"a": ["Refund Rangers", {"b": "text"}], "citation_run_id": "seo-cit-1"})
    assert stats == {"failed": True} and out["a"][0] == text_redact.WITHHELD and out["citation_run_id"] == "seo-cit-1"


def test_m4_scrub_leaves_ids_hashes_and_times():
    rec = {"citation_run_id": "seo-cit-" + "1" * 40, "answer_sha256": "a" * 64, "observed_at": "2026-10-09T18:00:00Z",
           "engine": "openai", "text": "Refund Rangers helps."}
    out, stats = text_redact.scrub(rec)
    assert out == rec and stats == {}


# ---------------------------------------------------------------------------------------------- M5 / S4 / M6 lookalikes

@pytest.mark.parametrize("brand,spoof", [
    ("Cloudtrack", "dloudtrack"), ("Delta Claims", "Clelta Claims"), ("Z Best Media", "zbestrnedia"),
    ("Quillbrook Labs", "Qulbrook Labs"), ("Quillbrook Labs", "Quiibrook Labs"), ("Quillbrook Labs", "Quilibrook"),
    ("Willow Recovery", "VVillow Recovery"), ("Seller Snap", "SeIIer Snap"), ("Seller Snap", "Se11er Snap"),
    ("Mediaworks", "rnediavvorks"), ("Dunmore", "clunrnore"), ("Fiona Labs", "ﬁona labs"),
    ("Refund Rangers", "Refund Rangers̲"), ("Duluth", "duIuth"),
])
def test_m5_multi_letter_lookalikes_are_the_clients(brand, spoof):
    assert idris.client_like(spoof, [brand], None) == "CLIENT_LOOKALIKE", (brand, spoof)
    assert idris.client_like_host(spoof.replace(" ", "").lower() + ".com", [brand], None) is not None


@pytest.mark.parametrize("brand,spoof", [
    ("Z Best Media", "Z Bset Meida"), ("Z Best Media", "Zbedtmedai"), ("Refund Rangers", "Refnud Rnagers"),
    ("Quillbrook Labs", "Quilbrok Labs"), ("Refund Rangers", "Refind Rangres"), ("Z Best Media", "Media Best Z"),
    ("Z Best Media", "Best Z Media"), ("Refund Rangers", "Rangers Refund"), ("Quillbrook Labs", "Labs Quillbrook"),
])
def test_s4_two_edit_typos_and_word_order_swaps(brand, spoof):
    assert idris.client_like(spoof, [brand], None) == "CLIENT_LOOKALIKE", (brand, spoof)


@pytest.mark.parametrize("brand,other", [
    ("Seller Snap", "Seller Scan"), ("BigCommerce", "Bold Commerce"), ("Refund Rangers", "Refund Masters"),
    ("Jungle Scout", "Jungle Sound"), ("Z Best Media", "Best Buy"), ("Sellics", "Sellix"), ("Helium 10", "Lithium 10"),
])
def test_s4_distinct_brands_stay_distinct(brand, other):
    assert idris.client_like(other, [brand], None) != "CLIENT_LOOKALIKE", (brand, other)


@pytest.mark.parametrize("host", ["zbestmedia.attacker.com", "login.zbest-media.secure-host.net",
                                  "z.best.media.example.com", "www.zbestrnedia.evil.io", "zbestmedia.example.co.uk",
                                  "pay.xn--zbstmedia-h0h.example.com", "media.zbest.example.com",
                                  "a.b.c.d.zbestmedia.e.f.example.com", "site.attacker.com"])
def test_m6_the_clients_brand_in_any_host_label_is_a_lookalike(host):
    assert idris.client_like_host(host, ["Z Best Media"], "site") is not None, host


def test_m6_a_label_containing_the_brand_is_a_lookalike_too():
    import codecs
    idn = "xn--" + codecs.encode("zb\u0435stmedia-portal", "punycode").decode()
    for host in (f"login.{idn}.attacker.com", "zbestmedia-portal.example.net", "secure.zbestmedia-login.x.github.io"):
        assert idris.client_like_host(host, ["Z Best Media"], "site") == "CLIENT_LOOKALIKE", host


@pytest.mark.parametrize("host", ["refundrangers.com", "blog.refundrangers.com", "mediabest.example.com",
                                  "www.acme.test", "best.example.com", "media.example.com"])
def test_m6_other_hosts_are_not(host):
    assert idris.client_like_host(host, ["Z Best Media"], "site") is None, host


def test_m6_a_subdomain_lookalike_is_never_a_competitor_end_to_end(tmp_path):
    text = ("Options:\n1. **Brightvale** - a claims agency.\n2. Z Best Media helps.\n"
            "See zbestmedia.brightvale.com for details.")
    a = ti.ans(text, ["https://site.test/x", "https://zbestmedia.brightvale.com/claims",
                      "https://www.refundrangers.com/"])
    h, _fakes, psid = ti.svc(tmp_path, openai=[a], anthropic=[a])
    h.ok(ti.comp_set(h), 201)
    for _ in range(2):
        f = h.ok(ti.vis(h, psid, max_age_minutes=0), 201)["result"]["facts"]
    em = f["emerging"]
    listed = {c["key"] for c in em["items"] + em["watch"] + em["confirmed"]}
    assert "brightvale" not in listed, listed
    obs = [o["observations"] for o in h.svc.competitor_observations.values()]
    assert not any("zbestmedia.brightvale.com" in c["domain_hints"] and c["key"] != "brightvale"
                   for o in obs for c in o["candidates"])
    assert any("zbestmedia.brightvale.com" in c["domain_hints"] for o in obs for c in o["candidates"]
               if c["key"] == "brightvale")                       # the name stands for the impersonating host
    assert "brightvale" in {x["key"] for x in em["client_lookalikes"]}
    for caller, tenant in (("dashboard", None), ("hub", "zbm"), ("seo_agent", None)):
        h.refused(h.post("/tenants/zbm/competitor-set/confirmations", {"request_id": rid(), "key": "brightvale"},
                         caller=caller, tenant=tenant), 409, "CANDIDATE_IS_CLIENT_LOOKALIKE")
    for d in ("zbestmedia.brightvale.com", "login.z-best-media.example.net"):
        h.refused(ti.comp_set(h, competitors=[{"name": "Brightvale", "domains": [d]}]), 422, "COMPETITOR_IS_CLIENT")


def test_m5_the_competitor_set_refuses_multi_letter_lookalikes(tmp_path):
    h, _fakes, _psid = ti.svc(tmp_path)
    for name in ("Z Best rnedia", "Z Bset Meida", "Media Best Z"):
        h.refused(ti.comp_set(h, competitors=[{"name": name, "domains": ["other.test"]}]), 422, "COMPETITOR_IS_CLIENT")
    for d in ("zbestrnedia.com", "zbestmeclia.net", "zbestmedia.example.com"):
        h.refused(ti.comp_set(h, competitors=[{"name": "Brightvale", "domains": [d]}]), 422, "COMPETITOR_IS_CLIENT")


def test_typo_distance_is_banded_and_linear():
    assert idris._typo_distance("zbestmedia", "zbestmedia") == 0
    assert idris._typo_distance("zbestmedia", "zbsetmedia") == 1
    assert idris._typo_distance("zbestmedia", "zbstmqdia") == 2
    assert not idris._two_typos("blgcommerce", "boldcommerce")             # two edits side by side: another word
    assert idris._two_typos("zbestmedia", "zbstmqdia")                     # two separate slips
    assert idris._typo_distance("a" * 5000, "a" * 4999 + "b") == 3         # beyond MAX_TYPO_LEN: not compared


# ---------------------------------------------------------------------------------------------- S1 crash retry

class Crash(BaseException):
    """The process dying mid-run (not an Exception: nothing catches it)."""


def test_s1_a_retry_after_the_citation_run_was_lost_is_409_and_stores_nothing(tmp_path):
    h, fakes, psid = ti.svc(tmp_path)
    h.ok(ti.comp_set(h), 201)
    body = {"request_id": rid(), "domain": ti.DOMAIN, "prompt_set_id": psid}
    fakes["openai"].script = [Crash()]
    with pytest.raises(Crash):
        h.svc.request_visibility_run("seo_agent", "zbm", dict(body), False)
    assert h.svc.mark_interrupted_citation_runs() == 1
    assert [r["interrupted_reason"] for r in h.svc.citation_runs.values()] == ["PROCESS_LOST"]
    fakes["openai"].script = [ti.ANSWER]
    for _ in range(2):
        h.refused(h.post("/tenants/zbm/competitor-visibility-runs", body), 409, "VISIBILITY_RUN_NEEDS_NEW_REQUEST_ID")
    assert h.svc.visibility_runs == {}
    v = h.ok(h.post("/tenants/zbm/competitor-visibility-runs", {**body, "request_id": rid()}), 201)
    assert v["result"]["outcome"] == "OK" and v["source"] == "fresh"


# ---------------------------------------------------------------------------------------------- S2 _excerpt

def test_s2_the_whole_text_redaction_catches_a_token_cut_by_the_window():
    """The window starts 20 characters before the key's end: only the whole-text redaction sees the whole key."""
    for kind in ("openai", "github", "google", "perplexity"):
        k = strong_keys()[kind]
        head = "Intro words here. " + k + " "
        text = head + "Refund Rangers is a claims agency. " * 3
        at = len(head) - 21 + 60
        ex = idris._excerpt(text, at)["excerpt"]["text"]
        assert k[-20:-8] in text[at - 60:] and not leaked(k, ex), kind


def test_s2_the_window_redaction_joins_a_label_and_value_kept_apart_by_whitespace():
    v = tok("s2", 12)
    text = "Refund Rangers is a claims agency. password" + " " * 30 + v + " ok."
    ex = idris._excerpt(text, 0)["excerpt"]["text"]
    assert v not in ex and text_redact.SECRET in ex


def test_s2_personal_data_in_the_window_is_withheld():
    got = idris._excerpt("Refund Rangers: write to " + tok("s2e", 6, "abcdefghij") + "@example.org today.", 0)
    assert got["excerpt"] is None and got["withheld"].startswith("personal_data:")


# ---------------------------------------------------------------------------------------------- S3 over-redaction

def legit_corpus() -> list:
    """>= 200 legitimate phrases of the kind AI answers about agencies and tools hold (S3). Built at run time; the
    ADR reports the measured rate."""
    rnd = random.Random(48033)
    brands = ["Refund Rangers", "Getida", "Seller Snap", "Jungle Scout", "Helium 10", "Carbon6", "SellerApp",
              "Teikametrics", "Pacvue", "Z Best Media", "Acme Recovery", "Quillbrook Labs", "Sellerboard"]
    prose = [
        "{b} recovers FBA reimbursements for Amazon sellers.", "{b} charges 15% of what it recovers.",
        "Pricing for {b} starts at $49 per month.", "{b} was founded in 2016 and is based in Austin.",
        "Many sellers compare {b} with other agencies.", "{b} offers a free audit of the last 18 months.",
        "Reviews of {b} on Trustpilot average 4.6 stars.", "{b} handles lost and damaged inventory claims.",
        "According to Marketplace Pulse, {b} is growing.", "{b} integrates with Seller Central via SP-API.",
        "The key takeaway: {b} focuses on FBA fees.", "{b}'s dashboard shows open cases by status.",
        "{b} supports US, UK, DE and JP marketplaces.", "Contact {b} through the form on its website.",
        "{b} files cases for inbound shipment discrepancies.", "{b} has no long-term contract.",
        "Customers say {b} found money they had missed.", "{b} reports every recovered dollar in a monthly summary.",
    ]
    out = [p.format(b=b) for p in prose for b in rnd.sample(brands, 5)]
    out += [
        "https://example.com/blog/how-to-recover-amazon-fba-reimbursements-in-2026",
        "https://example.com/blog/The-Best-Amazon-Seller-Tools-For-2026-Ranked",
        "https://example.com/guides/strengths-and-weaknesses-of-jungle-scout-2026/",
        "Read /resources/fba-inventory-reimbursement-policy-changes-explained for details.",
        "#SmallBusinessSaturday #AmazonSellerTips #FBAReimbursement2026",
        "#RevenueRecoveryForMarketplaceSellers is trending.",
        "ISBN 978-1-4028-9462-6 is the reference book.", "ISBN 9781402894626 (paperback).",
        "ISBN-13: 978-0-306-40615-7 covers it.", "Order 112-3456789-1234567 was reimbursed.",
        "Amazon order 405-1234567-7654321 shows the refund.", "Ticket 4415982736 was closed.",
        "Case ID 9876543210 is open.", "Invoice 2026103145 lists 12 units.", "SKU 1234567890 restocked.",
        "Use the UUID 123e4567-e89b-12d3-a456-426614174000 in the request.",
        "Shipment FBA17ABCD123 arrived at PHX7.", "The ASIN B08N5WRWNW is affected.",
        "Version 3.14.159 shipped on 2026-10-09.", "The report covers 2024-01-01 to 2026-09-30.",
        "Fees rose 3.5% in Q3 2026.", "Over 1,250,000 sellers use FBA.", "Call volume fell 12% year over year.",
        "The token economy is not relevant here.", "Use the Pin tool to save listings.",
        "Our secret sauce is speed.", "Secure your account with two-factor authentication.",
        "Password managers such as 1Password help teams.", "The API documentation is public.",
        "Their access policy is documented.", "A strong password policy matters.",
        "Keys to success: patience and data.", "Authentication happens through Amazon.",
        "https://example.com/p/refund-rangers-review?utm_source=newsletter&utm_medium=email",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://maps.example.com/place/Austin+TX",
        "Austin, TX 78701", "Suite 400, 123 Main Street", "Open Monday to Friday, 9am to 5pm.",
        "The FAQ page answers 25 common questions.", "Section 4.2 of the policy covers removals.",
        "It costs $1,299 per year or $129 per month.", "Over 10,000 claims were filed in 2025.",
        "The GS1 barcode 0123456789012 identifies the product.",
        "Most agencies need read-only access to Seller Central.", "Claims must be filed within 60 days.",
        "Fulfillment centers such as ONT8 and LGB8 serve Los Angeles.", "Expect payouts in 5 to 7 business days.",
        "The FBA fee table changed on January 15, 2026.", "Inbound placement fees start at $0.21 per unit.",
        "A 2025 survey of 1,200 sellers found similar results.", "Their support team replies within 24 hours.",
        "Check the Inventory Ledger report weekly.", "Removal orders cost $1.04 per unit.",
        "Long-term storage fees apply after 365 days.", "The reimbursement policy changed in October 2024.",
        "Some agencies take 25% while others take 10%.", "Monthly plans range from $29 to $199.",
        "Seller Central's Case Log shows every open case.", "Amazon issues a reimbursement ID for each payout.",
        "Brand Registry is required for some features.", "Use the Manage FBA Inventory page to check.",
        "Q4 is the busiest season for most sellers.", "The Buy Box rewards competitive pricing.",
    ]
    for i in range(20):
        sha_ = tok(f"sha{i}", 40, HEXL)
        out.append(f"https://github.com/example/repo/commit/{sha_}")
        out.append(f"See https://gitlab.com/group/project/-/tree/{sha_} for the fix.")
    for i in range(10):
        out.append(f"https://example.com/blog/{'-'.join(rnd.sample(['amazon', 'seller', 'fba', 'refund', 'guide', 'claims', 'tips', 'how', 'to', 'recover', 'lost', 'inventory'], 7))}-{2020 + i}")
    return out


def test_s3_over_redaction_on_legitimate_text_is_within_the_bar():
    corpus = legit_corpus()
    assert len(corpus) >= 200
    changed = [t for t in corpus if text_redact.redact(t) != t]
    rate = len(changed) / len(corpus)
    assert rate <= 0.025, (round(rate * 100, 2), changed)


@pytest.mark.parametrize("text", [
    "https://example.com/blog/how-to-recover-amazon-fba-reimbursements-in-2026",
    "https://example.com/blog/The-Best-Amazon-Seller-Tools-For-2026-Ranked",
    "#SmallBusinessSaturdayDeals2026AmazonSellers", "/guides/How-To-Claim-FBA-Reimbursements-2026-Edition",
    "#SmallBusinessSaturday #AmazonSellerTips", "ISBN 978-1-4028-9462-6", "ISBN 9781402894626",
    "Ticket 4415982736 was closed.", "Order 112-3456789-1234567 was reimbursed.",
    "123e4567-e89b-12d3-a456-426614174000",
])
def test_s3_named_legitimate_shapes_are_left(text):
    assert text_redact.redact(text) == text


def test_s3_a_commit_sha_in_a_url_is_left_but_a_bare_hex_token_is_not():
    sha_ = tok("bare", 40, HEXL)
    assert text_redact.redact(f"https://github.com/a/b/commit/{sha_}") == f"https://github.com/a/b/commit/{sha_}"
    assert sha_ not in text_redact.redact(f"the value {sha_} here")


def test_s3_the_wordy_exemption_rarely_exempts_a_random_token():
    rnd = random.Random(48034)
    for alphabet in (ALNUM, ALNUM + "-_", "abcdefghijklmnopqrstuvwxyz0123456789"):
        n = sum(text_redact._wordy("".join(rnd.choice(alphabet) for _ in range(32))) for _ in range(5000))
        assert n <= 10, (alphabet[-3:], n)                               # <= 0.2% (weak heuristics only)


def test_redaction_is_idempotent_and_linear():
    rnd = random.Random(48035)
    for _ in range(200):
        k = strong_keys()[rnd.choice(sorted(strong_keys()))]
        t = f"Refund Rangers password: {tok(str(rnd.random()), 10)} and {k}; call (415) 555-0134."
        once = text_redact.redact(t)
        assert text_redact.redact(once) == once
    big = "password: " * 20000 + "x " * 20000 + "%41" * 10000
    assert len(text_redact.redact(big)) > 0
