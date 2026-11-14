"""Reviewer generator part 3: contexts X0-X12 and the matrix M-1 driver.
g3.py OUT.jsonl N SEED  -> one JSON line per case {c, d, x, y, s, q}."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya19, file h/g3.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
from rvgen import rv19_g1 as g1
from rvgen import rv19_g2 as g2

LANGS = ["en", "es", "fr", "de", "pt", "it", "ja", "zh", "ko", "ru", "ar", "hi"]
CTX = {
    "en": ["call", "contact", "my number", "please help", "info", "send to", "reach", "support", "details", "urgent"],
    "es": ["llamar", "contacto", "mi n\u00famero", "por favor", "ayuda", "tel\u00e9fono", "correo", "enviar"],
    "fr": ["appeler", "contact", "mon num\u00e9ro", "s'il vous pla\u00eet", "aide", "t\u00e9l\u00e9phone", "courriel"],
    "de": ["anrufen", "Kontakt", "meine Nummer", "bitte", "Hilfe", "Telefon", "schicken"],
    "pt": ["ligar", "contato", "meu n\u00famero", "por favor", "ajuda", "telefone"],
    "it": ["chiamare", "contatto", "mio numero", "per favore", "aiuto", "telefono"],
    "ja": ["\u96fb\u8a71", "\u756a\u53f7", "\u9023\u7d61", "\u304a\u9858\u3044\u3057\u307e\u3059", "\u554f\u3044\u5408\u308f\u305b"],
    "zh": ["\u7535\u8bdd", "\u53f7\u7801", "\u8054\u7cfb", "\u8bf7", "\u5e2e\u52a9"],
    "ko": ["\uc804\ud654", "\ubc88\ud638", "\uc5f0\ub77d", "\uc8fc\uc138\uc694", "\ub3c4\uc6c0"],
    "ru": ["\u043f\u043e\u0437\u0432\u043e\u043d\u0438\u0442\u044c", "\u043d\u043e\u043c\u0435\u0440",
           "\u043a\u043e\u043d\u0442\u0430\u043a\u0442", "\u043f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430"],
    "ar": ["\u0627\u062a\u0635\u0644", "\u0631\u0642\u0645", "\u062a\u0648\u0627\u0635\u0644",
           "\u0645\u0646 \u0641\u0636\u0644\u0643"],
    "hi": ["\u092b\u093c\u094b\u0928", "\u0928\u0902\u092c\u0930", "\u0938\u0902\u092a\u0930\u094d\u0915",
           "\u0915\u0943\u092a\u092f\u093e"],
}
ADDR_CTX = {"en": ["directions to", "deliver to", "hours", "pizza near", "apartment for rent", "open now", "parking"],
            "es": ["direcci\u00f3n", "c\u00f3mo llegar a", "horario"], "fr": ["adresse", "itin\u00e9raire vers"],
            "de": ["Adresse", "Weg zu"], "pt": ["endere\u00e7o", "como chegar"], "it": ["indirizzo", "come arrivare"],
            "ja": ["\u4f4f\u6240", "\u884c\u304d\u65b9"], "zh": ["\u5730\u5740", "\u600e\u4e48\u53bb"],
            "ko": ["\uc8fc\uc18c", "\uac00\ub294 \uae38"], "ru": ["\u0430\u0434\u0440\u0435\u0441"],
            "ar": ["\u0639\u0646\u0648\u0627\u0646"], "hi": ["\u092a\u0924\u093e"]}
JOIN = [" ", "\t", " \U0001f600 ", "\U0001f4de", "\u3001", "\u300c", "\u3002", "\uff1a", " - ", ": "]


def contextual(r, value, x, addr=False):
    if x == 0:
        return value
    lang = LANGS[x - 1]
    pool = (ADDR_CTX if addr else CTX)[lang]
    w1, w2 = r.choice(pool), r.choice(pool)
    j1, j2 = r.choice(JOIN), r.choice(JOIN)
    if addr:            # the address rule's head needs a space (or start) before the house number
        j1 = r.choice([" ", "\t", " \U0001f600 ", "\u3001", "\u300c", ": "])
    pos = r.choice(["before", "after", "around"])
    if pos == "before":
        return w1 + j1 + value
    if pos == "after":
        return value + j2 + w2
    return w1 + j1 + value + j2 + w2


DIGIT_D = ["D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10", "D11", "D12", "D13", "D14"]
GAPK = {"D02": "ascii", "D03": "three", "D04": "nfkc", "D05": "other", "D06": "sep", "D07": "mixed"}
VAL = {"card": g1.card, "nanp": g1.nanp, "e164": g1.e164, "ssn": g1.ssn}


def digit_case(r, c, D, y):
    d = VAL[c](r)
    if D == "D01":
        return "", g1.fmt_digits(r, c, d)
    if D in GAPK:
        if D == "D03":
            every = True
        elif D == "D07":
            every = r.random() < 0.5
        else:
            every = r.random() < 0.5
        s = g1.gapped(r, d, GAPK[D], every)
        return ("every" if every else "some"), ("+" + s if c == "e164" else s)
    if D == "D08":
        z = r.choice(g1.ND_BLOCKS_NONASCII)
        return "%05x" % z, g1.to_block(g1.fmt_digits(r, c, d), z)
    if D == "D09":
        f = g1.fmt_digits(r, c, d)
        if r.random() < 0.5:
            return "fwdigits", g1.fw_digits(f)
        return "fwall", g1.fullwidth(f)
    if D == "D10":
        return "", g1.insert_zw(r, g1.fmt_digits(r, c, d))
    plus = (r.choice(["+", "+ "]) if c == "e164" else "")
    if D in ("D11", "D12"):
        return "", plus + g2.spelled(r, d, y, "space")
    if D == "D13":
        m = r.choice(["hyphen", "run", "upper"])
        return m, plus + g2.spelled(r, d, y, m)
    if D == "D14":
        one = r.random() < 0.5
        return ("one" if one else "many"), plus + g2.spelled_mixed(r, d, y, one)
    raise ValueError(D)


VAN_D = DIGIT_D + ["D15"]


def vanity_case(r, D, y):
    one, code, kp = g2.vanity_parts(r)
    digits = one + code
    if D == "D01":
        return "", g2.vanity_join(r, one, code, kp)
    if D in GAPK:
        g = g1.gapped(r, digits, GAPK[D], D == "D03" or r.random() < 0.5)
        return "", g + r.choice(["-", " "]) + kp
    if D == "D08":
        z = r.choice(g1.ND_BLOCKS_NONASCII)
        return "%05x" % z, g1.to_block(g2.vanity_join(r, one, code, kp), z)
    if D == "D09":
        f = g2.vanity_join(r, one, code, kp)
        return ("fwall", g1.fullwidth(f)) if r.random() < 0.5 else ("fwdigits", g1.fw_digits(f))
    if D == "D10":
        return "", g1.insert_zw(r, g2.vanity_join(r, one, code, kp))
    if D in ("D11", "D12"):
        return "", g2.spelled(r, digits, y, "space") + " " + kp
    if D == "D13":
        m = r.choice(["hyphen", "run", "upper"])
        return m, g2.spelled(r, digits, y, m) + r.choice([" ", "-"]) + kp
    if D == "D14":
        return "one", g2.spelled_mixed(r, digits, y, True) + " " + kp
    if D == "D15":
        if r.random() < 0.5:
            return "kpspaced", g2.vanity_join(r, one, code, " ".join(kp))
        return "codegap", g1.gapped(r, digits, "ascii", r.random() < 0.5) + " " + kp
    raise ValueError(D)
