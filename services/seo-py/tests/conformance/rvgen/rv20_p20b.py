"""rv-priya20 contexts, e-mail transforms and addresses (own word choices)."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya20, file h/p20b.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver removed. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import base64
import string
from urllib.parse import quote, quote_plus

from rvgen.rv20_p20 import AT_WORDS, DOT_WORDS, GLUE, rcase, rdig, label, email_parts

# 12 context languages: (before phrases, after phrases); X1..X12 = en es fr de pt it ja zh ko ru ar hi
CTX = {
    "en": (["please reach me via", "my old details:", "contact info", "send it to"], ["asap thanks", "for the refund",
                                                                                     "ok?", "before friday"]),
    "es": (["mis datos son", "escribeme a", "el contacto es"], ["gracias de antemano", "por favor", "urgente"]),
    "fr": (["mes coordonnees", "ecrivez moi a", "voici le contact"], ["merci beaucoup", "svp", "avant lundi"]),
    "de": (["meine daten lauten", "schreib an", "kontakt bitte"], ["danke schoen", "bis morgen", "dringend"]),
    "pt": (["meus dados sao", "fale comigo em", "contato"], ["obrigada", "por favor", "ate amanha"]),
    "it": (["i miei dati", "scrivimi a", "recapito"], ["grazie mille", "per favore", "entro domani"]),
    "ja": (["\u9023\u7d61\u5148\u306f", "\u79c1\u306e\u756a\u53f7"], ["\u3067\u3059", "\u307e\u3067\u304a\u9858\u3044"]),
    "zh": (["\u6211\u7684\u8054\u7cfb\u65b9\u5f0f", "\u8bf7\u53d1\u5230"], ["\u8c22\u8c22", "\u5c3d\u5feb"]),
    "ko": (["\uc5f0\ub77d\ucc98\ub294", "\ub0b4 \ubc88\ud638"], ["\uc785\ub2c8\ub2e4", "\uac10\uc0ac\ud569\ub2c8\ub2e4"]),
    "ru": (["\u043c\u043e\u0439 \u043a\u043e\u043d\u0442\u0430\u043a\u0442", "\u043f\u0438\u0448\u0438\u0442\u0435"],
           ["\u0441\u043f\u0430\u0441\u0438\u0431\u043e", "\u0441\u0440\u043e\u0447\u043d\u043e"]),
    "ar": (["\u0631\u0642\u0645\u064a \u0647\u0648", "\u0631\u0627\u0633\u0644\u0646\u064a"],
           ["\u0634\u0643\u0631\u0627", "\u0628\u0633\u0631\u0639\u0629"]),
    "hi": (["\u092e\u0947\u0930\u093e \u0928\u0902\u092c\u0930", "\u0938\u0902\u092a\u0930\u094d\u0915"],
           ["\u0927\u0928\u094d\u092f\u0935\u093e\u0926", "\u091c\u0932\u094d\u0926\u0940"]),
}
LANGS = ["en", "es", "fr", "de", "pt", "it", "ja", "zh", "ko", "ru", "ar", "hi"]
CJK = {"ja", "zh", "ko"}
EMOJI = ["\U0001f4de", "\U0001f600", "\u2709\ufe0f", "\U0001f4cd", "\U0001f525"]
CJK_PUNCT = ["\u3001", "\u3002", "\uff1a", "\u300c", "\u300d", "\uff08", "\uff09", "\u30fb"]


def joiner(r, lang, side):
    k = r.random()
    if k < 0.35:
        return " "
    if k < 0.45:
        return "\t"
    if k < 0.55:
        return " " + r.choice(EMOJI) + " "
    if k < 0.65:
        return r.choice(CJK_PUNCT)
    if k < 0.85:                                     # C-2: fullwidth / ideographic glue, no space
        return r.choice(GLUE)
    if lang in CJK or lang in ("ru", "ar", "hi"):
        return ""                                    # glued without spaces (C-2: CJK context words)
    return " " + r.choice(GLUE) + " "


def in_context(r, lang, value):
    before, after = CTX[lang]
    pos = r.choice(["before", "after", "around"])
    b = rcase(r, r.choice(before)) if pos in ("before", "around") else ""
    a = rcase(r, r.choice(after)) if pos in ("after", "around") else ""
    out = value
    if b:
        out = b + joiner(r, lang, "b") + out
    if a:
        out = out + joiner(r, lang, "a") + a
    return out


ROT = str.maketrans(string.ascii_lowercase + string.ascii_uppercase,
                    string.ascii_lowercase[13:] + string.ascii_lowercase[:13]
                    + string.ascii_uppercase[13:] + string.ascii_uppercase[:13])


def rot13(s):
    return s.translate(ROT)


def spelled_email(r, at_w=None, dot_w=None, style=None):
    loc, doms, tld = email_parts(r)
    at_w = at_w or r.choice(AT_WORDS)
    style = style or r.choice(["space", "space", "upper", "title", "random"])

    def kw(w):
        if style == "upper":
            return w.upper()
        if style == "title":
            return w.title()
        if style == "random":
            return "".join(c.upper() if r.random() < 0.5 else c for c in w)
        return w
    parts = [loc, " " + kw(at_w) + " "]
    labs = doms + [tld]
    for i, lab in enumerate(labs):
        parts.append(lab)
        if i < len(labs) - 1:
            parts.append(" " + kw(dot_w or r.choice(DOT_WORDS)) + " ")
    return "".join(parts)


BR_AT = ["[at]", "(at)", "{at}", "<at>", "[@]", "( at )", "[ AT ]", "(AT)"]
BR_DOT = ["[.]", "(dot)", "[dot]", "{dot}", "<dot>", "( . )", "[ DOT ]", "(Dot)"]


def bracket_email(r):
    loc, doms, tld = email_parts(r)
    sp = r.choice(["", " "])
    labs = doms + [tld]
    s = loc + sp + r.choice(BR_AT) + sp + labs[0]
    for lab in labs[1:]:
        s += sp + r.choice(BR_DOT) + sp + lab
    return s


def underscore_email(r):
    loc, doms, tld = email_parts(r)
    at = r.choice(["_at_", "-at-", " _at_ ", " -at- ", "_AT_", "-At-"])
    s = loc + at + doms[0]
    for lab in doms[1:] + [tld]:
        s += r.choice(["_dot_", "-dot-", " _dot_ ", " -dot- ", "_DOT_", "."]) + lab
    return s


def encode_email(r, e):
    b = e.encode()
    k = r.randrange(11)
    if k == 0:
        return base64.b64encode(b).decode()
    if k == 1:
        return base64.b64encode(b).decode().rstrip("=")
    if k == 2:
        return base64.urlsafe_b64encode(b).decode()
    if k == 3:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")
    if k == 4:
        return base64.b32encode(b).decode()
    if k == 5:
        return base64.b32encode(b).decode().rstrip("=").lower()
    if k == 6:
        return base64.b32encode(b).decode().lower()
    if k == 7:
        return b.hex() if r.random() < 0.5 else b.hex().upper()
    if k == 8:
        return "".join("%%%02X" % x for x in b)
    if k == 9:
        return quote(e, safe="")
    i = r.randrange(len(e))
    return e[:i] + "".join("%%%02x" % x for x in e[i:].encode()) if r.random() < 0.5 else quote_plus(e)


def chunk(r, s):
    if len(s) < 2:
        return s
    cuts = sorted(r.sample(range(1, len(s)), r.randint(1, min(4, len(s) - 1))))
    return " ".join(s[a:b] for a, b in zip([0] + cuts, cuts + [len(s)], strict=True))


# addresses (C7): own street names, suffixes, units, states
NAMES = ["birch", "quail ridge", "san pablo", "harbor view", "juniper", "old orchard", "w 14th", "mill creek",
         "dogwood", "las palmas", "sycamore", "cherry hill", "foxglove", "e 3rd", "willow bend", "seaview"]
STRONG = ["st", "ave", "blvd", "rd", "dr", "ln", "ct", "pl", "ter", "cir", "hwy", "pkwy", "street", "avenue",
          "boulevard"]
ORDINARY = ["road", "lane", "drive", "court", "way", "terrace", "place", "loop", "plaza"]
DIRS = ["", "", "n ", "s ", "e ", "w ", "ne ", "sw "]
STATES = ["ca 90012", "ny 10027", "tx 78701", "wa 98104", "fl 33130", "il 60614", "az 85004", "ga 30303"]
UK = ["sw1a 1aa", "ec2m 7pp", "m1 1ae", "b33 8hq", "ls1 4dy", "cf10 1ep", "g2 3wt", "eh1 1yz"]
CA = ["m5v 3l9", "h3z 2y7", "v6b 4y8", "t2p 1j9", "k1a 0b1"]
FR = ["rue de la gare", "chemin des vignes", "avenue de la republique", "boulevard du port", "impasse des lilas",
      "quai de la seine", "place de la mairie"]


def address(r, kind=None):
    kind = kind or r.choice(["strong", "ordinary", "state", "pobox", "pmb", "rr", "zip4", "uk", "ca", "fr"])
    hn = str(r.randint(1, 98999))
    if kind == "strong":
        s = hn + " " + r.choice(DIRS) + r.choice(NAMES) + " " + r.choice(STRONG)
        if r.random() < 0.3:
            s += r.choice([" apt ", " suite ", " unit ", " #"]) + str(r.randint(1, 990))
        return kind, s
    if kind == "ordinary":
        return kind, hn + " " + r.choice(DIRS) + r.choice(NAMES) + " " + r.choice(ORDINARY)
    if kind == "state":
        return kind, hn + " " + r.choice(NAMES) + " " + r.choice(STRONG) + " " + r.choice(STATES)
    if kind == "pobox":
        return kind, r.choice(["po box ", "p.o. box ", "PO Box "]) + str(r.randint(1, 99999))
    if kind == "pmb":
        return kind, "pmb " + str(r.randint(1, 9999))
    if kind == "rr":
        return kind, r.choice(["rr ", "hc "]) + str(r.randint(1, 30)) + " box " + str(r.randint(1, 999))
    if kind == "zip4":
        return kind, "%05d-%04d" % (r.randint(10000, 99950), r.randint(1, 9999))
    if kind == "uk":
        return kind, r.choice(UK)
    if kind == "ca":
        return kind, r.choice(CA)
    return kind, str(r.randint(1, 400)) + " " + r.choice(FR)


ADDR_CTX = ["is my home", "pick me up at", "deliver to", "visit us", "open now", "near me", "parking", "hours today",
            "the blue house", "map", "rent", "for sale by owner"]


def address_in_text(r, s, ntok):
    """D24: the address followed by 0..8 tokens of context (and maybe some before)."""
    words = " ".join(r.choice(" ".join(ADDR_CTX).split()) for _ in range(ntok))
    out = s + (" " + words if words else "")
    if r.random() < 0.4:
        out = r.choice(ADDR_CTX) + " " + out
    return out


__all__ = ["rdig", "label"]
