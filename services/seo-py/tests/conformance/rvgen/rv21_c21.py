"""rv-priya21 contexts (12 languages, own phrases), e-mail transforms and addresses (own word choices)."""
# Ported into the permanent conformance suite (priya-spec/2 FG-3, CAL-2) from review rv-priya21, file h/c21.py
# (sha256 fd57e568ccaf39b79f65fab0686241317a69e2b4da96d2b3afad796cf580aa37): the reviewer's generator, seeded; synthetic values only, made at run time. Changes: non-ASCII
# literals written as escapes, imports from the package, the command-line driver and environment overrides
# removed (N / PER fixed at the review's defaults). The random call order is unchanged, so the corpus is the
# review's own (sha256 pinned in data/manifest.json).
import base64
import re
import string
from urllib.parse import quote, quote_plus

from rvgen.rv21_p21 import AT_WORDS, DOT_WORDS, GLUE, rcase, email_parts

LANGS = ["en", "es", "fr", "de", "pt", "it", "ja", "zh", "ko", "ru", "ar", "hi"]
# (before, after) phrases; non-Latin as escapes
CTX = {
    "en": (["the flyer said", "text my cousin on", "is this legit", "found in old notes"],
           ["is it still valid", "lol", "from the receipt", "call back tmrw"]),
    "es": (["encontre esto", "le escribo a", "mi tia dejo"], ["sigue activo", "gracias", "para la cita"]),
    "fr": (["trouve sur le mur", "appelle le", "note pour moi"], ["toujours valide", "merci", "demain matin"]),
    "de": (["auf dem zettel stand", "ruf bitte", "notiz"], ["noch gueltig", "danke", "heute abend"]),
    "pt": (["achei isso", "liga pro", "anotei"], ["ainda vale", "valeu", "amanha cedo"]),
    "it": (["ho trovato", "chiama il", "appunto"], ["ancora valido", "grazie", "stasera"]),
    "ja": (["\u30e1\u30e2", "\u96fb\u8a71\u3057\u3066"], ["\u3067\u3059\u304b", "\u3088\u308d\u3057\u304f"]),
    "zh": (["\u5907\u5fd8", "\u6253\u7ed9"], ["\u53ef\u4ee5\u5417", "\u591a\u8c22"]),
    "ko": (["\uba54\ubaa8", "\uc804\ud654"], ["\ub9de\ub098\uc694", "\uace0\ub9c8\uc6cc"]),
    "ru": (["\u0437\u0430\u043f\u0438\u0441\u043a\u0430", "\u043f\u043e\u0437\u0432\u043e\u043d\u0438"],
           ["\u0434\u0435\u0439\u0441\u0442\u0432\u0443\u0435\u0442", "\u0441\u043f\u0430\u0441\u0438\u0431\u043e"]),
    "ar": (["\u0645\u0644\u0627\u062d\u0638\u0629", "\u0627\u062a\u0635\u0644"],
           ["\u0645\u0645\u0643\u0646", "\u0645\u0639 \u0627\u0644\u0634\u0643\u0631"]),
    "hi": (["\u0928\u094b\u091f", "\u092b\u093c\u094b\u0928 \u0915\u0930\u094b"],
           ["\u0938\u0939\u0940 \u0939\u0948", "\u0936\u0941\u0915\u094d\u0930\u093f\u092f\u093e"]),
}
CJK = {"ja", "zh", "ko"}
EMOJI = ["\U0001f9fe", "\U0001f4f1", "\U0001f4e7", "\U0001f3e0", "\u2764\ufe0f", "\U0001f44d\U0001f3fd"]
CJK_PUNCT = ["\u300c", "\u300d", "\u3010", "\u3011", "\u300a", "\u300b", "\u30fb", "\u3000"]


def joiner(r, lang):
    k = r.random()
    if k < 0.3:
        return " "
    if k < 0.38:
        return "  \t"
    if k < 0.46:
        return r.choice(EMOJI)                        # emoji glued, no space
    if k < 0.56:
        return " " + r.choice(CJK_PUNCT) + " "
    if k < 0.66:
        return "\u00a0"                               # no-break space
    if k < 0.86:
        g = r.choice(GLUE)                            # C-2 glue, maybe doubled, maybe with a space on one side
        return r.choice([g, g + g, g + " ", " " + g])
    if lang in CJK or lang in ("ru", "ar", "hi"):
        return ""                                     # glued without spaces
    return ' "'


def in_context(r, lang, value, pos=None):
    before, after = CTX[lang]
    pos = pos or r.choice(["before", "after", "around", "around"])
    out = value
    if pos in ("before", "around"):
        out = rcase(r, r.choice(before)) + joiner(r, lang) + out
    if pos in ("after", "around"):
        out = out + joiner(r, lang) + rcase(r, r.choice(after))
    return out


ROT = str.maketrans(string.ascii_lowercase + string.ascii_uppercase,
                    string.ascii_lowercase[13:] + string.ascii_lowercase[:13]
                    + string.ascii_uppercase[13:] + string.ascii_uppercase[:13])


def rot13(s):
    return s.translate(ROT)


def kwcase(r, w, style):
    if style == "upper":
        return w.upper()
    if style == "title":
        return w.title()
    if style == "alt":
        return "".join(c.upper() if i % 2 == 0 else c for i, c in enumerate(w))
    return w


def kwsep(r):
    """the space around a spelled keyword: spaces / tabs / no-break / C-2 glue (v1.1 C-2) / parentheses."""
    return r.choice([(" ", " "), ("  ", " "), ("\t", "\t"), ("\u00a0", "\u00a0"), (" (", ") "),
                     (r.choice(GLUE), r.choice(GLUE)), (" ", r.choice(GLUE)), ("\u3000", "\u3000")])


def spelled_email(r, at_w=None, dot_w=None, style=None, short=False):
    loc, doms, tld = email_parts(r, short)
    at_w = at_w or r.choice(AT_WORDS)
    style = style or r.choice(["lower", "upper", "title", "alt"])
    a, b = kwsep(r)
    parts = [loc, a + kwcase(r, at_w, style) + b]
    labs = doms + [tld]
    for i, lab in enumerate(labs):
        parts.append(lab)
        if i < len(labs) - 1:
            a, b = kwsep(r)
            parts.append(a + kwcase(r, dot_w or r.choice(DOT_WORDS), style) + b)
    return "".join(parts)


BR_AT = ["[at]", "(at)", "{at}", "<at>", "[@]", "[ at ]", "{ AT }", "<At>", "(@)"]
BR_DOT = ["[.]", "(dot)", "[dot]", "{dot}", "<dot>", "[ . ]", "{ DOT }", "<Dot>", "(.)"]


def bracket_email(r, short=False):
    loc, doms, tld = email_parts(r, short)
    sp = r.choice(["", " ", "  "])
    labs = doms + [tld]
    s = loc + sp + r.choice(BR_AT) + sp + labs[0]
    for lab in labs[1:]:
        s += sp + r.choice(BR_DOT) + sp + lab
    return s


def underscore_email(r, short=False):
    loc, doms, tld = email_parts(r, short)
    at = r.choice(["_at_", "-at-", " _at_ ", "-AT-", "_At_", " -at- ", "__at__"[1:-1]])
    s = loc + at + doms[0]
    for lab in doms[1:] + [tld]:
        s += r.choice(["_dot_", "-dot-", "-DOT-", "_Dot_", " _dot_ ", "."]) + lab
    return s


ENC = ["b64", "b64np", "b64u", "b64unp", "b32", "b32np", "b32l", "b32lnp", "hex", "HEX", "pct", "pctpart", "qplus"]


def encode(r, e, k=None):
    b = e.encode()
    k = k or r.choice(ENC)
    if k == "b64":
        return base64.b64encode(b).decode()
    if k == "b64np":
        return base64.b64encode(b).decode().rstrip("=")
    if k == "b64u":
        return base64.urlsafe_b64encode(b).decode()
    if k == "b64unp":
        return base64.urlsafe_b64encode(b).decode().rstrip("=")
    if k == "b32":
        return base64.b32encode(b).decode()
    if k == "b32np":
        return base64.b32encode(b).decode().rstrip("=")
    if k == "b32l":
        return base64.b32encode(b).decode().lower()
    if k == "b32lnp":
        return base64.b32encode(b).decode().rstrip("=").lower()
    if k == "hex":
        return b.hex()
    if k == "HEX":
        return b.hex().upper()
    if k == "pct":
        return "".join("%%%02x" % x for x in b)
    if k == "pctpart":
        i = e.index("@") if "@" in e else len(e) // 2
        return e[:i] + quote(e[i:], safe="")
    return quote_plus(e, safe="")


def chunk(r, s, size=None):
    """pieces of 1..5 characters (or ``size``) set apart by spaces (D21)."""
    out, i = [], 0
    while i < len(s):
        n = size or r.choice([1, 1, 2, 3, 4, 5])
        out.append(s[i:i + n])
        i += n
    return r.choice([" ", " ", "  "]).join(out)


def wordrev(s):
    return re.sub(r"[^@.\s]+", lambda m: m.group()[::-1], s)


# addresses (C7): own names, suffixes, units, states, postcodes
NAMES = ["cottonwood", "bluebonnet", "tamarack", "vista del mar", "e 52nd", "pine needle", "ridgecrest",
         "stone fence", "marigold", "hawthorne", "el camino", "w 7th", "magnolia", "ocean breeze", "red fox"]
STRONG = ["St", "st.", "Ave.", "av", "blvd.", "RD", "dr.", "Ln", "ct", "pl.", "ter", "cir", "hwy", "pkwy", "trl",
          "cres", "aly", "plz", "rte", "fwy", "expy", "tpke", "Street", "AVENUE", "boulevard"]
ORDINARY = ["road", "lane", "drive", "court", "way", "terrace", "place", "loop", "plaza"]
DIRS = ["", "", "", "N ", "S. ", "e ", "w. ", "NE ", "sw ", "north ", "West "]
STATES = ["CA 94110", "Nevada 89501", "or, 97035"[4:], "co 80202", "ma 02139", "ohio 43215", "tn 37203", "nj 07030",
          "mn 55401", "ut 84101"]
UK = ["sw1a 2aa", "w1d 3qu", "nw1 6xe", "e1 6an", "ox1 3bg", "bs1 4dj", "l1 8jq", "ne1 7ru", "ab10 1xg", "cb2 1tn"]
CA = ["k2p 1l4", "v5k 0a1", "h2x 1y4", "t5j 0n3", "r3c 4t3", "s4p 3y2", "e1c 4m3"]
FR = ["rue des acacias", "chemin du moulin", "impasse de la fontaine", "avenue des tilleuls", "boulevard de la mer",
      "quai du rhone", "allee des pins", "place de la liberte", "route du lac"]
UNITS = [" apt ", " Apt. ", " suite ", " ste ", " unit ", " #", ", #", " apartment "]


def address(r, kind=None):
    kind = kind or r.choice(["strong", "strong", "ordinary", "ordinary", "state", "pobox", "pmb", "rr", "zip4", "uk",
                             "ca", "fr"])
    hn = str(r.randint(1, 999999)) + (r.choice(["a", "b", ""]) if r.random() < 0.1 else "")
    nm = r.choice(NAMES)
    if kind == "strong":
        s = hn + " " + r.choice(DIRS) + nm + " " + r.choice(STRONG)
        if r.random() < 0.35:
            s += r.choice(UNITS) + str(r.randint(1, 4999))
        return kind, s
    if kind == "ordinary":
        s = hn + " " + r.choice(DIRS) + nm + " " + r.choice(ORDINARY)
        if r.random() < 0.25:
            s += r.choice(UNITS) + str(r.randint(1, 999))
        return kind, s
    if kind == "state":
        return kind, hn + " " + nm + " " + r.choice(STRONG) + r.choice([" ", ", "]) + r.choice(STATES)
    if kind == "pobox":
        return kind, r.choice(["po box ", "P.O. Box ", "p o box ", "pobox #"]) + str(r.randint(1, 999999))
    if kind == "pmb":
        return kind, r.choice(["pmb ", "PMB #"]) + str(r.randint(1, 99999))
    if kind == "rr":
        return kind, r.choice(["rr ", "RR ", "hc ", "rural route "]) + str(r.randint(1, 60)) + r.choice(
            [" box ", ", box ", " Box #"]) + str(r.randint(1, 9999))
    if kind == "zip4":
        return kind, "%05d%s%04d" % (r.randint(10000, 99950), r.choice(["-", " - ", "\u2013"]), r.randint(1, 9999))
    if kind == "uk":
        return kind, r.choice(UK)
    if kind == "ca":
        return kind, r.choice(CA)
    return kind, str(r.randint(1, 900)) + r.choice(["", " bis", " ter"]) + r.choice([" ", ", "]) + r.choice(FR)


ADDR_CTX = ["meet", "outside", "tonight", "gate", "code", "garage", "leave", "box", "porch", "thanks", "ring",
            "bell", "after", "six", "blue", "door", "dog", "inside", "upstairs", "please"]


def address_in_text(r, s, ntok, before=True):
    """D24: the address followed by 0..8 context tokens (and maybe a phrase before)."""
    words = " ".join(r.choice(ADDR_CTX) for _ in range(ntok))
    out = s + (" " + words if words else "")
    if before and r.random() < 0.5:
        out = " ".join(r.choice(ADDR_CTX) for _ in range(r.randint(1, 3))) + " " + out
    return out

