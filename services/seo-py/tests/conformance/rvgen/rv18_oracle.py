"""Independent reference of the CLOSED list's digit / vanity / e-mail rules (reviewer's own reading of ADR 0047), used
only to decide whether a DROPPED ordinary query is list-mandated. Address: the ADR's address rule is the builder's
own definition, so it is checked with HEAD's ``looks_like_address`` (passed in)."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya18, file rv/oracle.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import re
import unicodedata

EN = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
ES = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"]
NW = {w: str(i) for i, w in enumerate(EN)} | {w: str(i) for i, w in enumerate(ES)} | {"oh": "0", "una": "1"}
IIN = re.compile(r"4|5[0-8]|2(?:2[2-9]|[3-6]\d|7[01])\d|2720|3[46789]|30[0-5]|35(?:2[89]|[3-8]\d)|6\d|8[12]|220[0-4]|9792")


def norm(q):
    t = unicodedata.normalize("NFKC", q)
    out = []
    for c in t:
        cat = unicodedata.category(c)
        if cat == "Cf":
            continue
        if cat == "Nd" and not c.isascii():
            c = str(unicodedata.digit(c))
        out.append(c)
    return "".join(out).lower()


def luhn(d):
    s = 0
    for i, ch in enumerate(reversed(d)):
        x = int(ch)
        if i % 2:
            x = x * 2 - 9 if x > 4 else x * 2
        s += x
    return s % 10 == 0


def ssn(d):
    return len(d) == 9 and d[:3] not in ("000", "666") and d[0] != "9" and d[3:5] != "00" and d[5:] != "0000"


def nanp(d):
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return len(d) == 10 and d[0] in "23456789" and d[3] in "23456789"


def card(d):
    return 13 <= len(d) <= 19 and (IIN.match(d) is not None or (len(d) == 15 and d[0] == "1")) and luhn(d)


def stream(t):
    """Returns the first rule hit: windows of whole consecutive digit groups whose gaps are <= 3 characters."""
    gs = [(m.start(), m.end(), m.group()) for m in re.finditer(r"\d+", t)]
    for a in range(len(gs)):
        plus = "+" in t[max(0, gs[a][0] - 3):gs[a][0]]
        d = ""
        for b in range(a, len(gs)):
            if b > a and gs[b][0] - gs[b - 1][1] > 3:
                break
            d += gs[b][2]
            if len(d) > 19:
                break
            if ssn(d):
                return "ssn"
            if len(d) in (10, 11) and nanp(d):
                return "nanp"
            if card(d):
                return "card"
            if plus and 8 <= len(d) <= 15:
                return "e164"
    return None


def spelled(t):
    toks = re.split(r"[^a-z0-9]+", t)
    if sum(1 for w in toks if w in NW) < 2:
        return ""
    return " ".join(NW.get(w, w) for w in toks)


VAN = re.compile(r"(?<![a-z0-9])(?:1[^a-z0-9]{0,3})?8[^a-z0-9]{0,3}(?:0[^a-z0-9]{0,3}0|3[^a-z0-9]{0,3}3|4[^a-z0-9]{0,3}4|"
                 r"5[^a-z0-9]{0,3}5|6[^a-z0-9]{0,3}6|7[^a-z0-9]{0,3}7|8[^a-z0-9]{0,3}8)[^a-z0-9]{0,3}"
                 r"([a-z0-9](?:[^a-z0-9]{0,3}[a-z0-9]){6})(?![a-z0-9])")


def vanity(t):
    return any(any(c.isalpha() for c in m.group(1)) for m in VAN.finditer(t))


def mandated(q, looks_like_address=None):
    t = norm(q)
    s = stream(t)
    if s:
        return "stream_" + s
    sp = spelled(t)
    if sp:
        s = stream(sp)
        if s:
            return "spelled_" + s
    if vanity(t):
        return "vanity"
    if re.search(r"[a-z0-9._%+\-]@[a-z0-9\-]+\.[a-z]{2,}", t):
        return "email"
    if looks_like_address is not None and looks_like_address(t):
        return "address"
    return None
