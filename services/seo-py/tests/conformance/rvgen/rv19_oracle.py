"""Reviewer's independent oracle for OQ-2: rv-priya18 rv/oracle.py (digit stream, spelled, vanity, '@' e-mail)
plus the ADR 0047 address rule written out here from the ADR's prose (not from search_performance)."""
# Ported into the permanent conformance suite (priya-spec/1.1 C-5) from review rv-priya19, file h/oracle19.py: the reviewer's
# generator, seeded; synthetic values only, made at run time. Changes: non-ASCII literals written as escapes,
# imports from the package, the command-line driver replaced by a function. The random call order is unchanged,
# so the corpus is the review's own (sha256 pinned in data/manifest.json).
import re

from rvgen import rv18_oracle as rv

STRONG = ("street avenue boulevard st ave av rd blvd ln dr ct pl ter cir hwy pkwy trl cres aly plz rte fwy expy "
          "tpke").split()
ORDINARY = "road lane drive court way terrace place loop plaza".split()
# stop / quantity words a street name token may not be (ADR: "none a stop or quantity word such as near, in, best,
# hour, bedroom, lane, way")
STOP = set(("a an the of for in on at to and or my your with near by from best top cheap cheapest hour hours hr hrs "
            "day days week weeks month months year years minute minutes bedroom bedrooms bed beds bath baths car cars "
            "star stars person people room rooms story stories way ways things tips step steps lane lanes door doors "
            "inch inches ft foot feet mile miles unit units flat apt suite ste places easy simple quick great ton "
            "gallon season stroke percent sq").split()) | set(STRONG) | set(ORDINARY)
DIRS = set("n s e w ne nw se sw north south east west n. s. e. w.".split())
STATE_CODES = set(("al ak az ar ca co ct de dc fl ga hi id il ia ks ky la ma md mi mn ms mo mt ne nv nh nj nm ny nc nd "
                   "oh pa ri sc sd tn tx ut vt va wa wv wi wy").split())
STATE_NAMES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
               "illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
               "mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
               "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
               "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming")
UNIT_WORDS = ("btu btus miles mile mi km kms dollars dollar usd jobs job sq sqft ft feet watts watt lumens rpm lbs lb "
              "pounds people units gallons gal views followers zip code codes population calories psi cfm").split()
UK_AREAS = set(("ab al ba bb bd bh bl bn br bs bt b ca cb cf ch cm co cr ct cv cw da dd de dg dh dl dn dt dy ec eh en "
                "ex e fk fy gl gu gy g ha hd hg hp hr hs hu hx ig im ip iv je ka kt kw ky la ld le ll ln ls lu l me mk "
                "ml m ne ng nn np nr nw n ol ox pa pe ph pl po pr rg rh rm sa se sg sk sl sm sn so sp sr ss st sw sy s "
                "ta td tf tn tq tr ts tw ub wa wc wd wf wn wr ws wv w yo ze").split())
UK_NOT_INWARD = set("st nd rd th tb gb kb pb hp lb ft hz dr bd".split())


def _tokens(t):
    return [re.sub(r"^[^a-z0-9#]+|[^a-z0-9]+$", "", w) for w in t.split()]


def street(t):
    toks = _tokens(t)
    raw = t.split()
    for i, w in enumerate(toks):
        if not re.fullmatch(r"\d{1,6}[a-z]?", w):
            continue
        if i > 0 and toks[i - 1] in ("top", "best"):
            continue
        j = i + 1
        if j < len(toks) and toks[j] in DIRS:
            j += 1
        adj = j < len(toks) and toks[j] in ("old", "new", "good")
        k0 = j + 1 if adj else j
        for n in (1, 2, 3):
            names = toks[k0:k0 + n]
            if len(names) < n or not all(re.fullmatch(r"[a-z][a-z'.-]*|\d{1,4}(st|nd|rd|th)", x) and x not in STOP
                                         for x in names):
                break
            s = k0 + n
            if s < len(toks) and toks[s] in STRONG + ORDINARY:
                return True
        if adj:          # 'old' / 'new' / 'good' as the whole name: the suffix ends the query (or unit/state/zip)
            s = j + 1
            if s < len(toks) and toks[s] in STRONG + ORDINARY and s == len(toks) - 1:
                return True
    _ = raw
    return False


def state_zip(t):
    for m in re.finditer(r"(?:^|[\s,])(" + STATE_NAMES + r"|[a-z]{2})(\.?\s*,?\s*)(\d{5})(?!\d)", t):
        st, sep = m.group(1), m.group(2)
        if len(st) == 2 and (st not in STATE_CODES or sep == ""):
            continue
        rest = t[m.end():].lstrip()
        if any(rest.startswith(u) and (len(rest) == len(u) or not rest[len(u)].isalnum()) for u in UNIT_WORDS):
            continue
        return True
    return False


def other_forms(t):
    if re.search(r"(?<!\w)p\.?\s*o\.?\s*box\s*#?\s*\d+", t) or re.search(r"(?<!\w)pmb\s*#?\s*\d+", t):
        return True
    if re.search(r"(?<!\w)(rr|hc|rural route)\s*#?\s*\d+\s*,?\s*box\s*#?\s*\d+", t):
        return True
    if re.search(r"(?<!\d)\d{5}\s*-\s*\d{4}(?!\d)", t):                       # ZIP+4
        return True
    for m in re.finditer(r"(?<![a-z0-9])([a-z]{1,2})(\d[a-z\d]?)\s+(\d)([a-z]{2})(?![a-z0-9])", t):
        if m.group(1) in UK_AREAS and not set(m.group(4)) & set("cikmov") and m.group(4) not in UK_NOT_INWARD:
            return True
    if re.search(r"(?<![a-z0-9])[abceghj-nprstvxy]\d[a-z]\s?\d[a-z]\d(?![a-z0-9])", t):
        return True
    if re.search(r"(?<![a-z0-9])\d{1,5}\s+(rue|chemin|impasse|all[e\u00e9]e|quai)\s+[a-z']", t):
        return True
    if re.search(r"(?<![a-z0-9])\d{1,5}\s+(place|route|avenue|av|boulevard|bd)\s+(de\s+la|du|des|de|d')\s*[a-z]", t):
        return True
    return False


def address(t):
    t = rv.norm(t)
    return street(t) or state_zip(t) or other_forms(t)


def mandated(q):
    m = rv.mandated(q)
    if m:
        return m
    return "address" if address(q) else None
