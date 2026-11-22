"""The fixed ordinary corpora of core-spec/1 O-4 as the CI lane draws them (seeded; a pure function of the seed and
the size), and the lanes each corpus is read in. Nothing here holds a credential: values are made at run time.

  neutral   O-4 N   sentences of the embedded ordinary words (13 languages) and English answer prose, 4 casings
  fuzz      O-4 F   the dec 72 style generator: words, digits, punctuation, invisible / space characters; 35% hold a
                    label token (a form of SP-01..05)
  hosts     O-4 H   generated host families (words, hyphenated, IDN punycode, hash-like, numeric) + real hosts
  urls      O-4 U   ordinary-word URLs per language (path / slug / query / fragment / sub-domain), raw and encoded
  machine   O-4 MV  machine values of the 19 sink classes' shapes
The lane functions take the modules of ONE tree (``agents.secret_guard``, ``sink_guard``, ``svc_tenants``,
``svc_audits``, ``agents.callum``) so the calibration tool runs them on ffd28ca8 / cbdf5e78 / 2b26d980 and the tests
on the build."""
import base64
import hashlib
import json
import random
import string
import uuid
from pathlib import Path
from urllib.parse import quote

SEED = 51
DATA = Path(__file__).resolve().parent / "data"
WORDS = json.loads((DATA / "words.json").read_text(encoding="ascii"))["words"]
LANGS = tuple(sorted(WORDS))
LOWER = string.ascii_lowercase
EN_WORDS = ("revenue recovery agency marketplace seller refund guide best tips how to choose small business audit "
            "inventory fees storage returns report update policy change review compare pricing plans case study growth "
            "strategy brand retail shipping order customer service product price delivery store online account "
            "support help team market sales season holiday campaign email newsletter page site traffic search "
            "ranking content article blog post video image photo design style color size model brand new used "
            "today tomorrow week month year morning evening city country street house garden kitchen coffee "
            "water music movie book school teacher student doctor travel hotel flight ticket weather").split()


def _rng(*parts) -> random.Random:
    return random.Random(":".join(str(p) for p in (SEED,) + parts))


# ------------------------------------------------------------------------------------------------ neutral (N)

CASINGS = ("as_is", "lower", "upper", "title")


def _case(s: str, c: str) -> str:
    return {"as_is": s, "lower": s.lower(), "upper": s.upper(), "title": s.title()}[c]


def neutral(per_lang: int = 140) -> list:
    """(lang, casing, text): ``per_lang`` sentences of 5-10 ordinary words per language (and English), 4 casings."""
    out = []
    for lang in ("en",) + LANGS:
        words = EN_WORDS if lang == "en" else WORDS[lang]
        joiner = "" if lang in ("ja", "zh", "th") else " "
        r = _rng("neutral", lang)
        for _ in range(per_lang):
            s = joiner.join(r.choice(words) for _ in range(r.randint(5, 10)))
            s += r.choice((".", "!", "?", "", ","))
            for c in CASINGS:
                out.append((lang, c, _case(s, c)))
    return out


def lanes_text(sg, t: str) -> tuple:
    """(strict reads, client view masks)."""
    return (sg.check(t) is not None, bool(sg.client_view_findings(t)))


# ------------------------------------------------------------------------------------------------ fuzz (F)

_NOISE = ("\u00a0", "\u200b", "\u2009", "\u3000", "\t", "\n", "\u2013", "\u00b7", "\u2022", "\u060c", "\u3001")
_LABEL_TOKENS = ("password", "Password", "PASSWORD", "api_key", "apiKey", "token", "secret", "PIN", "passwd",
                 "\u043f\u0430\u0440\u043e\u043b\u044c", "\ube44\ubc00\ubc88\ud638", "\u5bc6\u7801",
                 "\u30d1\u30b9\u30ef\u30fc\u30c9", "\u05e1\u05d9\u05e1\u05de\u05d4",
                 "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2 \u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2")


def fuzz(n: int = 5000) -> list:
    """(has_label_token, text): mixed-script word salad with digits, punctuation, spaces and invisible characters;
    35% hold one label token."""
    r = _rng("fuzz")
    out = []
    for i in range(n):
        lang = r.choice(LANGS + ("en",))
        words = EN_WORDS if lang == "en" else WORDS[lang]
        toks = []
        for _ in range(r.randint(3, 14)):
            k = r.random()
            if k < .6:
                toks.append(r.choice(words))
            elif k < .72:
                toks.append(str(r.randint(0, 10 ** r.randint(1, 6))))
            elif k < .82:
                toks.append(r.choice(".,;:!?-/()[]\"'#%&*+=<>|~"))
            elif k < .9:
                toks.append(r.choice(_NOISE))
            else:
                toks.append(r.choice(WORDS[r.choice(LANGS)]))
        has = r.random() < .35
        if has:
            toks.insert(r.randrange(len(toks) + 1), r.choice(_LABEL_TOKENS))
        out.append((has, " ".join(toks) if r.random() < .8 else "".join(toks)))
    return out


# ------------------------------------------------------------------------------------------------ hosts (H)

def _punycode(label: str) -> str:
    try:
        return "xn--" + label.encode("punycode").decode("ascii")
    except UnicodeError:
        return label


TLDS = ("com", "org", "net", "io", "co", "de", "fr", "jp", "ru", "kr", "gr", "co.uk", "com.br", "at", "in", "th")


def real_hosts() -> list:
    """Real, public host names already embedded in the suite (portal hosts of review 98b76cde, the hosts of the
    ordinary URL corpora)."""
    import ast
    from urllib.parse import urlsplit

    import url_corpus
    import utility_corpus_v2
    src = (Path(__file__).resolve().parents[1] / "test_review_98b76cde.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") ==
                "PORTAL_HOSTS")
    hosts = node.value.func.value.value.split()          # (the string literal before ``.split()``)
    for u in list(url_corpus.corpus()) + list(utility_corpus_v2.urls()):
        h = urlsplit(u if isinstance(u, str) else u[0]).hostname
        if h:
            hosts.append(h)
    return sorted(set(hosts))


def hosts(n: int = 10000) -> list:
    """(family, host): generated host families."""
    r = _rng("hosts")
    latin = WORDS["pl"] + WORDS["de"] + list(EN_WORDS)
    fams = ("words_dot", "words_hyphen", "idn", "idn_hyphen", "hash_label", "numeric", "letters", "mixed")
    out = []
    for i in range(n):
        f = fams[i % len(fams)]
        tld = r.choice(TLDS)
        if f == "words_dot":
            h = ".".join(r.choice(latin).lower() for _ in range(r.randint(1, 3))) + "." + tld
        elif f == "words_hyphen":
            h = "-".join(r.choice(latin).lower() for _ in range(r.randint(2, 4))) + "." + tld
        elif f == "idn":
            h = _punycode(r.choice(WORDS[r.choice(LANGS)]).lower()) + "." + tld
        elif f == "idn_hyphen":
            lang = r.choice(LANGS)
            h = _punycode("-".join(r.choice(WORDS[lang]).lower() for _ in range(2))) + "." + tld
        elif f == "hash_label":
            h = "".join(r.choice("0123456789abcdef") for _ in range(r.choice((8, 12, 16)))) + "." + r.choice(
                latin).lower() + "." + tld
        elif f == "numeric":
            h = f"{r.choice(latin).lower()}{r.randint(1, 9999)}.{tld}"
        elif f == "letters":
            h = "".join(r.choice(LOWER) for _ in range(r.randint(3, 12))) + "." + tld
        else:
            h = f"{r.choice(latin).lower()}-{r.randint(1, 99)}-{r.choice(latin).lower()}.{tld}"
        h = "".join(c for c in h if c.isascii() and (c.isalnum() or c in "-.")).strip("-.")
        if h and "." in h and ".." not in h:
            out.append((f, h))
    return out


def lanes_host(mods: dict, h: str) -> tuple:
    """The six host lanes (H-1): sink class, tenant registration, competitor registration, inspect_url host,
    check, checked_host - True when the lane TAKES the host."""
    sg, sink, tenants, audits, callum = (mods[k] for k in ("sg", "sink", "tenants", "audits", "callum"))
    u = sg.inspect_url(f"https://{h}/about?x=1")
    # (2b26d980, before the host rule: registration and the competitor list took a host by ``valid_domain``)
    reg = (not tenants.domain_refused(h)) if hasattr(tenants, "domain_refused") else bool(tenants.valid_domain(h))
    comp = (not audits._domain_refused(h)) if hasattr(audits, "_domain_refused") else bool(tenants.valid_domain(h))
    return (sink.classify(h) == "hostname", reg, comp,
            bool(u) and bool(u.get("host")) and u.get("host_kind") is None, sg.check(h) is None,
            callum.checked_host(h) == {"host": h, "host_withheld": None})


HOST_LANES = ("sink", "registration", "competitor", "inspect_url", "check", "checked_host")


# ------------------------------------------------------------------------------------------------ URLs (U)

def urls(per_lang: int = 500) -> list:
    """(lang, place, url): ordinary-word URLs of every language, raw (as a browser writes non-ASCII letters) and
    percent-encoded once."""
    out = []
    for lang in LANGS:
        r = _rng("urls", lang)
        ws = WORDS[lang]
        for i in range(per_lang):
            a, b, c = (r.choice(ws) for _ in range(3))
            place = ("path", "slug", "query", "fragment", "subdomain")[i % 5]
            if place == "path":
                u = f"https://site.test/{a}/{b}"
            elif place == "slug":
                u = f"https://site.test/blog/{a}-{b}-{c}"
            elif place == "query":
                u = f"https://site.test/search?q={a} {b}&page=2"
            elif place == "fragment":
                u = f"https://site.test/page#{a}-{b}"
            else:
                u = f"https://{_punycode(a.lower())}.site.test/{b}"
            out.append((lang, place + ".raw", u.replace(" ", "%20")))
            out.append((lang, place + ".enc", quote(u, safe=":/?&=#")))
    return out


def lanes_url(sg, u: str) -> tuple:
    return (bool(sg.url_has_secret(u)),)


# ------------------------------------------------------------------------------------------------ machine values (MV)

def _hex(r, n):
    return "".join(r.choice("0123456789abcdef") for _ in range(n))


def _b32(r, n):
    return "".join(r.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(n))


def machine(per_class: int = 300) -> list:
    """(class, value): values in the shapes of the sink classes (S-1), seeded."""
    out = []
    r = _rng("machine")
    crock = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    gens = {
        "service_id": lambda: f"seo-{r.choice(('aud', 'cit', 'evd', 'rpt', 'sch'))}-{_hex(r, 40)}",
        "scoped_id": lambda: f"{r.choice(('audit', 'citation_run', 'log'))}:{_hex(r, 16)}",
        "invoice_id": lambda: "fin-inv-" + "".join(r.choice(crock) for _ in range(26)),
        "finance_id": lambda: "fin-je-" + _hex(r, 40),
        "ruling_id": lambda: "cmp-rul-" + _b32(r, 26),
        "jurisdiction": lambda: "US-" + r.choice(("CA", "NY", "TX", "WA", "ID", "PW")),
        "key_id": lambda: f"2026-{r.randint(1, 12):02d}",
        "request_id": lambda: str(uuid.UUID(int=r.getrandbits(128), version=4)),
        "amount": lambda: f"{r.randint(0, 99999)}.{r.randint(0, 999999):06d}",
        "request_key": lambda: f"audit|acme|{uuid.UUID(int=r.getrandbits(128), version=4)}",
        "hash": lambda: _hex(r, r.choice((16, 32, 40, 64))),
        "timestamp": lambda: f"2026-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}T{r.randint(0, 23):02d}:00:00Z",
        "date": lambda: f"20{r.randint(10, 30)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}",
        "number": lambda: str(r.randint(-10 ** 6, 10 ** 9)),
        "version": lambda: f"v{r.randint(0, 9)}.{r.randint(0, 20)}.{r.randint(0, 99)}",
        "boolean": lambda: r.choice(("true", "false")),
        "actor": lambda: r.choice(("seo", "andre", "dashboard")),
        "hostname": lambda: f"{r.choice(EN_WORDS)}.{r.choice(EN_WORDS)}.{r.choice(('com', 'org', 'net'))}",
        "code": lambda: r.choice(("COMPLETED", "RUN_FAILED", "SINK_REFUSED", "audit_requested", "engine.v2_rules",
                                  "svc_audits:run:" + str(r.randint(1, 99)), "GSC_AUTH_REFUSED")),
    }
    for cls, g in gens.items():
        out += [(cls, g()) for _ in range(per_class)]
    return out


def lanes_machine(mods: dict, v: str) -> tuple:
    sg, sink = mods["sg"], mods["sink"]
    return (str(sink.classify(v)), sg.check(v) is not None, bool(sg.client_view_findings(v)))


def stream_hash(items) -> str:
    h = hashlib.sha256()
    for it in items:
        h.update(json.dumps(it, ensure_ascii=True).encode("ascii") + b"\n")
    return h.hexdigest()


def b64(bits: bytes) -> str:
    return base64.b64encode(bits).decode("ascii")


# ------------------------------------------------------------------------------------------------ W sample (CI)

W_JOINS = (("space", " "), ("hyphen", "-"), ("dot", "."), ("glued", ""), ("slash", "/"), ("colon", ":"),
           ("comma", ","), ("plus", "+"), ("bar", "|"), ("comma_space", ", "), ("spaced_dash", " - "), ("equals", "="),
           ("dot_space", ". "), ("double_dash", "--"))
W_LANES = ("strict", "cv_bare", "cv_ready", "cv_value", "url_raw", "url_enc")


def _letters(s: str) -> str:
    """The letters of ``s`` case folded, every mark and the tatweel dropped, the final sigma a sigma (the loose
    skeleton the derived joins are found on)."""
    import unicodedata
    f = unicodedata.normalize("NFKC", s).casefold()
    f = unicodedata.normalize("NFC", "".join(c for c in unicodedata.normalize("NFD", f) if unicodedata.category(c)[0] != "M"))
    return "".join(c for c in f.replace("\u0640", "").replace("\u03c2", "\u03c3") if c.isalpha())


def wsample() -> list:
    """(lang, join, text): every ordered pair of the embedded 220-word samples whose letters hold a script label's
    letters ACROSS the word boundary (and not inside either word), in the 4 join forms and the 10 extra joins of
    O-4 W (the derived-join construction of review 0334eeae, widened to the 220-word samples)."""
    from conformance import core_ref as R
    keys = {_letters(e) for _id, e, kind, _s, _g in R.entries() if kind not in ("joined", "piece")}
    keys |= {_letters(w) for w in R.EL_WRITTEN}
    out = []
    for lang in ("ja", "hi", "th", "ar", "fa", "ru", "el", "pl", "de"):
        ws = [(w, _letters(w)) for w in WORDS[lang]]
        for a, la in ws:
            for b, lb in ws:
                j = la + lb
                if any(k in j and k not in la and k not in lb for k in keys):
                    for name, sep in W_JOINS:
                        out.append((lang, name, f"{a}{sep}{b}"))
    return out


def lanes_w(sg, t: str) -> tuple:
    v = "Kx7#mQ2zB"
    slug = t.replace(" ", "-")
    return (sg.check(t) is not None, bool(sg.client_view_findings(t)),
            "label_value" in sg.client_view_findings(f"{t}: ready today"),
            "label_value" in sg.client_view_findings(f"{t}: {v}"),
            bool(sg.url_has_secret(f"https://site.test/blog/{slug}/")),
            bool(sg.url_has_secret("https://site.test/blog/" + quote(slug) + "/")))
