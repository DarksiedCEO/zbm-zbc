"""rv-priya20 boundary set (enumerated, M-2 + v1.1 C-1/C-2 edges). iter_cases() -> (meta, q)."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya20, file h/g20bnd.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver removed. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import base64
import random

from rvgen import rv20_p20 as P
from rvgen import rv20_p20b as Q

LATIN_W = ["merci", "danke", "thanks", "gracias"]
CJK_W = ["\u3067\u3059", "\u8c22\u8c22", "\uac10\uc0ac"]
CYR_W = ["\u0441\u043f\u0430\u0441\u0438\u0431\u043e"]


def styles(w, k):
    return [w, w.upper(), w.title(), "".join(c.upper() if i % 2 else c for i, c in enumerate(w)),
            "".join(c.upper() if i % 2 == 0 else c for i, c in enumerate(w))][k]


def b_atdot():
    r = random.Random("b20-atdot")
    for a in P.AT_WORDS:
        for d in P.DOT_WORDS:
            for k in range(5):
                loc, doms, tld = P.email_parts(r)
                q = loc + " " + styles(a, k) + " " + doms[0] + " " + styles(d, k) + " " + tld
                yield {"g": "atdot", "s": "alone"}, q
            for gi, g in enumerate(P.GLUE):
                loc, doms, tld = P.email_parts(r)
                q = loc + " " + styles(a, gi % 5) + " " + doms[0] + " " + styles(d, gi % 5) + " " + tld
                yield {"g": "atdot", "s": "glue_latin", "u": "%04X" % ord(g)}, q + g + r.choice(LATIN_W)
                yield {"g": "atdot", "s": "glue_cjk", "u": "%04X" % ord(g)}, r.choice(CJK_W) + g + q + g + r.choice(CJK_W)
            # TLD equal to a keyword, label equal to a keyword
            loc, doms, tld = P.email_parts(r)
            yield {"g": "atdot", "s": "tld_kw"}, loc + " " + a + " " + doms[0] + " " + d + " " + r.choice(P.AT_WORDS + P.DOT_WORDS).replace("-", "")
            yield {"g": "atdot", "s": "label_kw"}, loc + " " + a + " " + r.choice(P.DOT_WORDS) + " " + d + " " + tld


def b_glue_addr():
    r = random.Random("b20-addr")
    shapes = ["strong", "ordinary", "state", "pobox", "rr", "zip4", "uk", "ca", "fr"]
    for sh in shapes:
        for g in P.GLUE:
            for side in ("after", "before", "both"):
                for wk, words in (("latin", LATIN_W), ("cjk", CJK_W), ("cyr", CYR_W)):
                    _, a = Q.address(r, sh)
                    w = r.choice(words)
                    q = {"after": a + g + w, "before": w + g + a, "both": w + g + a + g + w}[side]
                    yield {"g": "addr_glue", "s": sh + "/" + side + "/" + wk, "u": "%04X" % ord(g)}, q


def b_glue_email():
    r = random.Random("b20-mailglue")
    for form in ("plain", "spelled", "bracket", "under", "enc"):
        for g in P.GLUE:
            for wk, words in (("latin", LATIN_W), ("cjk", CJK_W)):
                e = P.email(r)
                v = {"plain": e, "spelled": Q.spelled_email(r), "bracket": Q.bracket_email(r),
                     "under": Q.underscore_email(r), "enc": Q.encode_email(r, e)}[form]
                w = r.choice(words)
                yield {"g": "mail_glue", "s": form + "/" + wk, "u": "%04X" % ord(g)}, w + g + v + g + w


def b_zw():
    r = random.Random("b20-zw")
    vals = [("nanp", "(415) 555-0134".replace("555-0134", "%03d-%04d" % (r.randint(200, 999), r.randint(0, 9999)))),
            ("ssn", P.ssn(r)), ("card", P.card(r)), ("email", P.email(r)), ("addr", Q.address(r, "strong")[1]),
            ("e164", "+" + P.e164(r)), ("vanity", "1-800-" + "".join(r.choice("abcdefgh") for _ in range(7)))]
    for name, v in vals:
        for z in P.ZW:
            for i in range(len(v) + 1):
                yield {"g": "zw", "s": name, "u": "%04X" % ord(z)}, v[:i] + z + v[i:]


def b_nd():
    r = random.Random("b20-nd")
    for z in P.ND_ZEROS:
        for name, v in (("card", P.card(r)), ("nanp", P.nanp(r)), ("e164", "+" + P.e164(r)), ("ssn", P.ssn(r)),
                        ("vanity", "1 877 " + r.choice(P.VANITY_WORDS)), ("addr", Q.address(r, "state")[1])):
            yield {"g": "nd", "s": name, "u": "%04X" % z}, P.to_block(v, z)
            yield {"g": "nd", "s": name + "/ctx", "u": "%04X" % z}, "ref " + P.to_block(v, z) + " ok"


PLUSES = ["+", "\uff0b", "\ufe62", "\u207a", "\u208a"]


def b_e164():
    r = random.Random("b20-e164")
    gaps = ["", " ", "  ", "   ", "x", "ab", "abc", "-", " - ", "(", "\u00a0", "\u200b", "\u2009\u2009", "a b",
            "\u0436", "\u3000"]
    for n in (8, 9, 15):
        for pl in PLUSES:
            for g in gaps:
                d = str(r.randint(1, 9)) + P.rdig(r, n - 1)
                yield {"g": "e164", "s": "n%d" % n, "u": "%04X" % ord(pl)}, pl + g + d
                yield {"g": "e164", "s": "n%d/ctx" % n, "u": "%04X" % ord(pl)}, "tel " + pl + g + d + " ok"


def b_sep_every():
    """Every separator character of P*/S*/Z* (non-ASCII and ASCII) as a single gap and as a 3-gap in NANP and SSN."""
    r = random.Random("b20-sep")
    for c in P.SEPS:
        d = P.nanp(r)[-10:]
        yield {"g": "sep1", "s": "nanp", "u": "%04X" % ord(c)}, d[:3] + c + d[3:6] + c + d[6:]
        s = P.ssn(r)
        yield {"g": "sep3", "s": "ssn", "u": "%04X" % ord(c)}, s[:3] + c * 3 + s[3:5] + c * 3 + s[5:]


def b_letters_every():
    r = random.Random("b20-let")
    for c in P.OTHER_L + P.NFKC_L:
        d = P.nanp(r)[-10:]
        yield {"g": "let", "s": "nanp", "u": "%04X" % ord(c)}, "".join(x + (c if i in (2, 5) else "") for i, x in enumerate(d))
        s = P.card(r)
        yield {"g": "let3", "s": "card", "u": "%04X" % ord(c)}, "".join(x + (c * 3 if i % 4 == 3 and i < len(s) - 1 else "") for i, x in enumerate(s))


def b_enc_chunks():
    r = random.Random("b20-enc")
    for _ in range(6):
        e = P.email(r)
        b = e.encode()
        encs = {"b64": base64.b64encode(b).decode(), "b64np": base64.b64encode(b).decode().rstrip("="),
                "b64u": base64.urlsafe_b64encode(b).decode(), "b32": base64.b32encode(b).decode(),
                "b32l": base64.b32encode(b).decode().lower().rstrip("="), "hex": b.hex(),
                "pct": "".join("%%%02X" % x for x in b)}
        for k, s in encs.items():
            for i in range(1, len(s)):
                yield {"g": "enc_chunk", "s": k}, s[:i] + " " + s[i:]
                yield {"g": "enc_chunk", "s": k + "/ctx"}, "mail " + s[:i] + " " + s[i:] + " pls"


def b_rot_rev():
    r = random.Random("b20-rot")
    for _ in range(150):
        lang = r.choice(Q.LANGS)
        q = Q.in_context(r, lang, P.email(r))
        toks = q.split(" ")
        yield {"g": "rotrev", "s": "rot13"}, Q.rot13(q)
        yield {"g": "rotrev", "s": "rev"}, q[::-1]
        yield {"g": "rotrev", "s": "wordrev"}, " ".join(t[::-1] for t in toks)
        for j in range(len(toks)):
            yield {"g": "rotrev", "s": "tok_rot"}, " ".join(Q.rot13(t) if i == j else t for i, t in enumerate(toks))
            yield {"g": "rotrev", "s": "tok_rev"}, " ".join(t[::-1] if i == j else t for i, t in enumerate(toks))


def b_cards():
    r = random.Random("b20-cards")
    for pre, n in (("1", 15), ("36", 14), ("300", 14), ("305", 14), ("2200", 16), ("62", 19), ("3528", 16)):
        for _ in range(20):
            d = P.luhn_fix(pre + P.rdig(r, n - 1 - len(pre)))
            for f in (d, " ".join(d[i:i + 4] for i in range(0, n, 4)), d[:4] + " " + d[4:10] + " " + d[10:],
                      "-".join(d[i:i + 4] for i in range(0, n, 4))):
                yield {"g": "card", "s": pre}, f


def b_vanity():
    r = random.Random("b20-van")
    for code in P.VANITY_CODES:
        for one in ("", "1", "1-", "1 ", "+1 "):
            for f in ("{o}{c}-{s}", "{o}({c}) {s}", "{o}{c}{s}", "{o}{c} {s}", "{o}{c}.{s}", "{o}{c}-{a}-{b}",
                      "{o}{c} {sp}"):
                s = r.choice(P.VANITY_WORDS)
                yield {"g": "vanity", "s": code}, f.format(o=one, c=code, s=s.upper() if r.random() < .5 else s,
                                                           a=s[:3], b=s[3:], sp=" ".join(s))


def b_spelled():
    r = random.Random("b20-spell")
    for lang in ("en", "es"):
        for cls in ("nanp", "ssn", "card", "e164"):
            for _ in range(40):
                d = {"nanp": P.nanp, "ssn": P.ssn, "card": P.card, "e164": P.e164}[cls](r)
                pre = "+" if cls == "e164" else ""
                for j in ("-", " ", "", ", "):
                    yield {"g": "spell", "s": cls + "/" + lang}, pre + P.spell(r, d, lang, joiner=j)
                yield {"g": "spell1", "s": cls + "/" + lang}, pre + P.spell_mixed(r, d, lang, one=True)


GROUPS = [b_atdot, b_glue_addr, b_glue_email, b_zw, b_nd, b_e164, b_sep_every, b_letters_every, b_enc_chunks,
          b_rot_rev, b_cards, b_vanity, b_spelled]


def iter_cases():
    for g in GROUPS:
        yield from g()
