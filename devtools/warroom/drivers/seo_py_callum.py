"""War room driver: seo-py's multi-engine answer adapter and Callum's citation-source runs (ADR 0046), through the
service's own harness (tests/helpers.py ``Harness``) and its own test builders (tests/test_callum.py,
tests/test_answer_engines.py, tests/test_perplexity.py: the REAL OpenAI / Anthropic / Google / Perplexity adapters on
``httpx.MockTransport`` with injected clocks).

Each case gets fresh harnesses (one per run it needs: the case, its metamorphic control, its re-run) in its own
temporary directory. Every engine is a HOSTILE provider (``Provider``): it serves the documented response shape with
whatever answer text and structured citations the case names — prompt injections, lookalike / homoglyph / punycode /
IP-literal / private / IPv6 / userinfo / backslash / bidi / data: / javascript: / file: citations, redirect chains,
extremely long URLs — or a broken one (malformed, truncated, huge, duplicate keys, reordered, NaN, BOM, gzip, HTML),
or a storm (429 / 5xx / timeouts / partial streams / a key echoed in an error body). Cited-URL resolution, when a case
turns it on, runs the service's REAL SSRF-safe fetcher over the seo-py driver's fake internet (``drivers/seo_py.py``:
fake DNS with hostile names, an internal address that answers a secret marker), extended with a redirect web: a
``/wr-<hex>`` path on any host answers a redirect to the hex-encoded URL, so a chain is visible to the judge without
running anything.

The judge is independent of the service: a cited URL's host is read the way a browser reads it (WHATWG: tabs and
newlines dropped, backslash as a path separator, userinfo stripped), NFKC'd, its A-labels decoded, and compared with
the client's domain; a lookalike is what the repo's shared fold (service-py ``src/lookalikes.py``, the one the seo-py
driver loads) folds onto the client but is not it. Secrets are every key and token the harness holds, searched for
in every response, the ledger and the local log (percent-decoded, folded). Bounds are structural (requests, the fake
clock's seconds, reservations), never wall-clock time.

Nothing leaves the process: the worker refuses every socket connect before any service code is imported, every
engine is a MockTransport, the fetcher's socket is a MockTransport over a fake DNS.
"""

from __future__ import annotations

import codecs
import copy
import importlib.util
import json
import re
import unicodedata
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

WARROOM = Path(__file__).resolve().parents[1]
SEO = None           # the seo-py driver module: the fake internet, the shared fold, the pinned PII needles
H = TC = TE = BASE = CALLUM = AA = httpx = None
SECRETS: dict = {}
CLIENT = "site.test"
BRAND = "Z Best Media"
TEXT = "Z Best Media recovers marketplace revenue in Long Beach."
ENGINES = ("openai", "anthropic", "google", "perplexity")
_RID = [0]


def prepare_env(env) -> None:
    for k in list(env):
        if k.startswith("SEO_") or k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN"):
            env.pop(k, None)


def import_harness() -> None:
    global SEO, H, TC, TE, BASE, CALLUM, AA, httpx, SECRETS
    spec = importlib.util.spec_from_file_location("warroom_driver_seo_py_base", WARROOM / "drivers" / "seo_py.py")
    SEO = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(SEO)
    SEO.import_harness()
    import answer_adapter
    import helpers
    import httpx as _httpx
    import test_answer_engines
    import test_callum
    import test_perplexity
    from agents import callum
    H, TC, TE, BASE, CALLUM, AA, httpx = helpers, test_callum, test_answer_engines, test_perplexity, callum, \
        answer_adapter, _httpx
    SECRETS = {f"{e}_key": TE.KEYS[e] for e in ("openai", "anthropic", "google")}
    SECRETS["perplexity_key"] = BASE.KEY
    SECRETS["service_token"] = H.SERVICE_TOKEN
    SECRETS["andre_token"] = H.ANDRE
    SECRETS.update({f"caller_{c}": t for c, t in H.CALLERS.items()})
    SECRETS.update({f"tenant_{t}": v for t, v in H.TENANT_TOKENS.items()})


def rid() -> str:
    """Deterministic request ids (a case's runs, and so its run ids, are the same every time it is replayed)."""
    _RID[0] += 1
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"warroom-callum-{_RID[0]}"))


# ============================================================================================ the oracle (judge side)

_DOTS = str.maketrans({"。": ".", "．": ".", "｡": "."})
CLIENT_, LOOKALIKE_, OTHER_, NOT_WEB_ = "CLIENT", "LOOKALIKE", "OTHER", "NOT_WEB"


def browser_url(url: str):
    """(scheme, host) the way a browser reads ``url`` (WHATWG: leading / trailing C0 and spaces trimmed, tab and
    newline removed, backslash a path separator for http(s), userinfo dropped). None when it is not readable."""
    if not isinstance(url, str):
        return None
    u = url.strip("".join(chr(i) for i in range(0x21)))
    u = u.replace("\t", "").replace("\n", "").replace("\r", "")
    m = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):", u)
    if m is None:
        return None
    scheme = m.group(1).lower()
    if scheme in ("http", "https"):
        u = u.replace("\\", "/")
    try:
        p = urlsplit(u)
        host = p.hostname
    except ValueError:
        return scheme, None
    if host is None:
        netloc = p.netloc.rsplit("@", 1)[-1]
        host = netloc.split(":")[0].lower() or None
    return scheme, host


def _decode_labels(host: str) -> str:
    out = []
    for lab in host.split("."):
        if lab.startswith("xn--"):
            try:
                lab = codecs.decode(lab[4:].encode("ascii"), "punycode")
            except Exception:  # noqa: BLE001 - an invalid A-label is read as written
                pass
        out.append(lab)
    return ".".join(out)


def oracle_host(host, client: str = CLIENT) -> str:
    """CLIENT: the client's own DNS name (exact, www. twin, subdomain), after NFKC, casefold, IDNA dot mapping and
    A-label decoding, in plain ASCII letters, digits, dots and hyphens. LOOKALIKE: not that, but the shared lookalike
    fold maps it onto the client's name. OTHER: anything else."""
    if not host:
        return OTHER_
    h = unicodedata.normalize("NFKC", host).translate(_DOTS).casefold().rstrip(".")
    h = _decode_labels(h)
    if h.strip("[]").replace(":", "").replace(".", "").isdigit() or ":" in h:
        return OTHER_                                  # an IP literal is never the client's name
    plain = re.fullmatch(r"[a-z0-9.-]+", h) is not None
    on = h == client or h.endswith("." + client)
    if plain and on:
        return CLIENT_
    f = SEO.FOLD.fold_cased(h).rstrip(".")
    if f == client or f.endswith("." + client):
        return LOOKALIKE_
    return OTHER_


def oracle_url(url: str, client: str = CLIENT) -> str:
    b = browser_url(url)
    if b is None or b[0] not in ("http", "https"):
        return NOT_WEB_
    return oracle_host(b[1], client)


def chain_of(url: str, limit: int = 12) -> list:
    """Every URL the fake web would walk from ``url`` (a ``/wr-<hex>`` path redirects to the hex-encoded URL)."""
    out = [url]
    while len(out) < limit:
        m = re.search(r"/wr-([0-9a-f]+)$", out[-1].split("?")[0].split("#")[0])
        if m is None:
            break
        try:
            out.append(bytes.fromhex(m.group(1)).decode("utf-8", "surrogatepass"))
        except ValueError:
            break
    return out


def creditable(url: str, client: str, resolve: bool) -> bool:
    """Could a citation of ``url`` honestly be the client's own page? Directly: the browser host is the client. With
    resolution: the chain lands on the client and NO host on it (the cited one included) is a lookalike."""
    if oracle_url(url, client) == CLIENT_:
        return True
    if not resolve:
        return False
    hops = chain_of(url)
    labels = [oracle_url(u, client) for u in hops]
    return labels[-1] == CLIENT_ and LOOKALIKE_ not in labels and NOT_WEB_ not in labels[:-1]


def tainted(url: str, client: str) -> bool:
    """A lookalike is on the citation's path (cited host or any hop)."""
    return any(oracle_url(u, client) == LOOKALIKE_ for u in chain_of(url))


# ============================================================================================ hostile providers

def body_for(engine: str, text: str, cites: list) -> dict:
    """The documented response shape of each engine (the suite's synthetic fixtures) carrying ``text`` and ``cites``
    as the engine's structured citations."""
    if engine == "openai":
        b = TE.fixture("openai")
        b["output"][0]["action"]["sources"] = [{"type": "url", "url": c} for c in cites]
        b["output"][1]["content"][0]["text"] = text
        b["output"][1]["content"][0]["annotations"] = [
            {"type": "url_citation", "start_index": 0, "end_index": min(len(text), 1), "url": c, "title": "t"}
            for c in cites]
        return b
    if engine == "anthropic":
        b = TE.fixture("anthropic")
        b["content"][2]["content"] = [{"type": "web_search_result", "url": c, "title": "t", "encrypted_content": "Eq"}
                                      for c in cites]
        tb = {"type": "text", "text": text}
        if cites:
            tb["citations"] = [{"type": "web_search_result_location", "url": c, "title": "t", "encrypted_index": "Eo",
                                "cited_text": "..."} for c in cites]
        b["content"] = b["content"][:3] + [tb]
        return b
    if engine == "google":
        b = TE.fixture("google")
        cand = b["candidates"][0]
        cand["content"]["parts"] = [{"text": text}]
        if cites:
            cand["groundingMetadata"]["groundingChunks"] = [{"web": {"uri": c, "title": "t"}} for c in cites]
            cand["groundingMetadata"]["groundingSupports"] = [
                {"segment": {"startIndex": 0, "endIndex": len(text), "text": text},
                 "groundingChunkIndices": list(range(len(cites)))}]
        else:
            del cand["groundingMetadata"]
        return b
    return BASE.sonar(text=text, citations=list(cites))


class _Drip(object):
    def __init__(self, clock):
        self.clock = clock

    def __iter__(self):
        while True:
            self.clock.t += 0.5
            yield b"{"

    def close(self):
        pass


class _Endless(object):
    def __iter__(self):
        while True:
            yield b" " * 65536

    def close(self):
        pass


class _Cut(object):
    """A body that stops half-way with the connection reset (a partial stream)."""

    def __init__(self, data: bytes):
        self.data = data

    def __iter__(self):
        yield self.data[: max(1, len(self.data) // 2)]
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    def close(self):
        pass


def _stream(cls_obj):
    class S(httpx.SyncByteStream):
        def __iter__(self):
            return iter(cls_obj)

        def close(self):
            cls_obj.close()
    return S()


def _raw(status, headers, data: bytes):
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(data))


J = {"content-type": "application/json"}

# protocol modes: a response that is NOT the documented answer; ``*`` modes are the same answer in another JSON form
PROTOCOL_MODES = ("not_json", "truncated", "duplicate_keys", "nan", "infinity", "huge", "endless", "html_ctype",
                  "gzip", "array_root", "string_root", "empty_body", "?bom_prefix", "trailing_garbage",
                  "text_mistyped", "cites_mistyped", "text_null", "?deep_nest", "answer_too_long",
                  "control_bytes", "redirect_internal", "*unknown_keys_flood", "*reordered_keys", "*unicode_escaped", "*pretty_printed",
                  "*duplicate_citations", "*reversed_citations")
STORM_STEPS = ("429", "429ra500", "429ra2", "503", "500", "502", "connect", "timeout", "drip", "cut", "ok", "401echo",
               "403", "402", "400", "404")


def _reorder(o):
    if isinstance(o, dict):
        return {k: _reorder(o[k]) for k in sorted(o, reverse=True)}
    if isinstance(o, list):
        return [_reorder(x) for x in o]
    return o


def _escaped(o) -> str:
    """JSON with every letter a, e, s, t inside a string (keys too) written as a \\u escape: the same document."""
    if isinstance(o, str):
        return '"' + "".join(f"\\u{ord(c):04x}" if c in "aest" else json.dumps(c)[1:-1] for c in o) + '"'
    if isinstance(o, dict):
        return "{" + ", ".join(f"{_escaped(k)}: {_escaped(v)}" for k, v in o.items()) + "}"
    if isinstance(o, list):
        return "[" + ", ".join(_escaped(v) for v in o) + "]"
    return json.dumps(o)


def _retype(b: dict, engine: str, what: str, value) -> dict:
    if engine == "openai":
        msg = b["output"][1]["content"][0]
        if what == "text":
            msg["text"] = value
        else:
            for a in msg["annotations"] or [{}]:
                a["url"] = value
            if not msg["annotations"]:
                msg["annotations"] = [{"type": "url_citation", "start_index": 0, "end_index": 1, "url": value}]
    elif engine == "anthropic":
        blk = b["content"][-1]
        if what == "text":
            blk["text"] = value
        else:
            blk["citations"] = [{"type": "web_search_result_location", "url": value, "title": "t",
                                 "encrypted_index": "E", "cited_text": "."}]
    elif engine == "google":
        cand = b["candidates"][0]
        if what == "text":
            cand["content"]["parts"] = [{"text": value}]
        else:
            cand.setdefault("groundingMetadata", {})["groundingChunks"] = [{"web": {"uri": value, "title": "t"}}]
    else:
        if what == "text":
            b["choices"][0]["message"]["content"] = value
        else:
            b["citations"] = [value]
    return b


def protocol_response(engine: str, mode: str, text: str, cites: list, clock, ops=None):
    """A non-documented response for ``mode`` (see PROTOCOL_MODES), or a structural mutation recipe (``ops``)."""
    good = body_for(engine, text, cites)
    raw = json.dumps(good).encode()
    if mode == "not_json":
        return _raw(200, J, b"{nope " + raw[:40])
    if mode == "truncated":
        return _raw(200, J, raw[: len(raw) // 2])
    if mode == "duplicate_keys":
        first = next(iter(good))
        return _raw(200, J, b'{"' + first.encode() + b'": 1, ' + raw[1:])
    if mode in ("nan", "infinity"):
        tok = b"NaN" if mode == "nan" else b"Infinity"
        return _raw(200, J, re.sub(rb'("(?:input_tokens|promptTokenCount|prompt_tokens)": )\d+', rb"\1" + tok, raw,
                                   count=1))
    if mode == "huge":
        return _raw(200, J, b'{"pad": "' + b"x" * (AA.MAX_RESPONSE_BYTES + 1) + b'"}')
    if mode == "endless":
        return httpx.Response(200, headers=J, stream=_stream(_Endless()))
    if mode == "html_ctype":
        return _raw(200, {"content-type": "text/html"}, raw)
    if mode == "gzip":
        import gzip
        return _raw(200, {**J, "content-encoding": "gzip"}, gzip.compress(raw))
    if mode == "array_root":
        return _raw(200, J, b"[" + raw + b"]")
    if mode == "string_root":
        return _raw(200, J, json.dumps(raw.decode()).encode())
    if mode == "empty_body":
        return _raw(200, J, b"")
    if mode == "?bom_prefix":
        return _raw(200, J, b"\xef\xbb\xbf" + raw)
    if mode == "trailing_garbage":
        return _raw(200, J, raw + b"}{\"x\":1}")
    if mode == "text_mistyped":
        return _raw(200, J, json.dumps(_retype(copy.deepcopy(good), engine, "text", [{"run": "release"}])).encode())
    if mode == "cites_mistyped":
        return _raw(200, J, json.dumps(_retype(copy.deepcopy(good), engine, "cites", {"href": "https://site.test/"})
                                       ).encode())
    if mode == "text_null":
        return _raw(200, J, json.dumps(_retype(copy.deepcopy(good), engine, "text", None)).encode())
    if mode == "?deep_nest":
        return _raw(200, J, raw[:-1] + b', "x": ' + b"[" * 5000 + b"]" * 5000 + b"}")
    if mode == "answer_too_long":
        return _raw(200, J, json.dumps(body_for(engine, TEXT + " x" * (AA.MAX_ANSWER_CHARS // 2 + 10), cites)).encode())
    if mode == "*unknown_keys_flood":
        g = copy.deepcopy(good)
        g.update({f"unmodelled_{i}": i for i in range(AA.MAX_UNMODELLED_KEYS + 5)})
        return _raw(200, J, json.dumps(g).encode())
    if mode == "control_bytes":
        return _raw(200, J, raw.replace(b'"', b'"\x00', 1))
    if mode == "redirect_internal":
        return _raw(302, {"location": "http://169.254.169.254/latest/meta-data/"}, b"")
    if mode == "*reordered_keys":
        return _raw(200, J, json.dumps(_reorder(good)).encode())
    if mode == "*unicode_escaped":
        return _raw(200, J, _escaped(good).encode())
    if mode == "*pretty_printed":
        return _raw(200, J, json.dumps(good, indent=4).encode())
    if mode == "*duplicate_citations":
        return _raw(200, J, json.dumps(body_for(engine, text, list(cites) + list(cites))).encode())
    if mode == "*reversed_citations":
        return _raw(200, J, json.dumps(body_for(engine, text, list(reversed(cites)))).encode())
    if mode == "ops":
        return mutated_response(engine, good, ops or {})
    raise ValueError(f"unknown protocol mode {mode}")


def mutated_response(engine: str, good: dict, spec: dict):
    """grammar.engine_body_mutation: apply 1-3 structural ops to the documented body."""
    body = copy.deepcopy(good)
    data = None
    # a text-replacing op first, so a breaking retype is never undone by a later one
    for op in sorted(spec.get("ops", []), key=lambda o: o != "huge_text"):
        if op == "reorder_keys":
            body = _reorder(body)
        elif op == "duplicate_citation":
            pass                                    # applied by the provider through the cites it serves
        elif op == "reverse_citations":
            pass
        elif op == "retype_text":
            body = _retype(body, engine, "text", 42)
        elif op == "retype_cites":
            body = _retype(body, engine, "cites", ["https://site.test/"])
        elif op == "drop_required":
            body.pop(next(iter(body)), None)
        elif op == "huge_text":
            body = _retype(body, engine, "text", "Z Best Media " * (spec.get("pad", 1) // 13 + 1))
    data = json.dumps(body, indent=4 if "pretty" in spec.get("ops", []) else None,
                      ensure_ascii="unicode_escape" in spec.get("ops", [])).encode()
    for op in spec.get("ops", []):
        if op == "truncate":
            data = data[: int(len(data) * min(0.95, max(0.05, spec.get("cut", 0.5))))]
        elif op == "dup_key":
            data = b'{"' + str(next(iter(body), "k")).encode() + b'": 0, ' + data[1:]
        elif op == "nan":
            data = data.replace(b": 0", b": NaN", 1) if b": 0" in data else data[:-1] + b', "n": NaN}'
        elif op == "infinity":
            data = data[:-1] + b', "n": -Infinity}'
        elif op == "trailing_garbage":
            data = data + b" trailing"
        elif op == "root_array":
            data = b"[" + data + b"]"
        elif op == "root_string":
            data = json.dumps(data.decode("utf-8", "replace")).encode()
        elif op == "bom":
            data = b"\xef\xbb\xbf" + data
        elif op == "deep_nest":
            d = int(spec.get("depth", 50))
            data = data[:-1] + b', "deep": ' + b"[" * d + b"]" * d + b"}"
        elif op == "empty":
            data = b""
        elif op == "control_bytes":
            data = data.replace(b'"', b'"\x01', 1)
    return _raw(200, J, data)


class Provider:
    """One hostile answer engine. ``script``: a list of storm steps (the last repeats), or None: always ``mode``."""

    def __init__(self, engine: str, clock, mode="answer", text=TEXT, cites=(), script=None, ops=None):
        self.engine, self.clock, self.mode, self.text, self.cites = engine, clock, mode, text, list(cites)
        self.script, self.ops = script, ops
        self.n = 0
        self.on_request = None
        self.reserved_before: list = []
        self.svc = None

    def __call__(self, request):
        self.n += 1
        if self.svc is not None:
            self.reserved_before.append(any(e["_payload"].get("engine") == self.engine
                                            for e in self.svc.ledger.of_type("engine_calls_reserved")))
        if self.on_request is not None:
            hook, self.on_request = self.on_request, None
            hook(request)
        if self.script:
            return self.step(self.script[min(self.n - 1, len(self.script) - 1)])
        if self.mode == "answer":
            return _raw(200, J, json.dumps(body_for(self.engine, self.text, self.cites)).encode())
        return protocol_response(self.engine, self.mode, self.text, self.cites, self.clock, self.ops)

    def step(self, s: str):
        if s == "ok":
            return _raw(200, J, json.dumps(body_for(self.engine, self.text, self.cites)).encode())
        if s.startswith("429"):
            ra = s[5:] if s.startswith("429ra") else None
            return _raw(429, {"retry-after": ra} if ra else {}, b"")
        if s in ("500", "502", "503"):
            return _raw(int(s), {}, b"")
        if s in ("400", "402", "403", "404"):
            return _raw(int(s), J, json.dumps({"error": {"message": "refused"}}).encode())
        if s == "401echo":
            key = SECRETS[f"{self.engine}_key"]
            return _raw(401, J, json.dumps({"error": {"message": f"bad key {key}", "key": key}}).encode())
        if s == "connect":
            raise httpx.ConnectError("refused")
        if s == "timeout":
            raise httpx.ReadTimeout("slow")
        if s == "drip":
            return httpx.Response(200, headers=J, stream=_stream(_Drip(self.clock)))
        if s == "cut":
            return httpx.Response(200, headers=J, stream=_stream(_Cut(json.dumps(
                body_for(self.engine, self.text, self.cites)).encode())))
        raise ValueError(f"unknown storm step {s}")


# ============================================================================================ the redirect web

class RedirectWeb(object):
    """The seo-py driver's fake internet (fake DNS: hostile names answer private addresses; an internal address that
    is reached answers the secret marker) plus: any other name is a public host; ``/wr-<hex>`` answers a redirect to
    the hex-encoded URL; ``/robots.txt`` allows everything; a page is a small HTML document, with a canonical to the
    URL hex-encoded after ``canon-`` when the path names one."""

    def __init__(self):
        self.base = SEO.Web()
        self.requests = self.base.requests
        self.internal = self.base.internal
        self.status = 301

    def resolve(self, host: str, port: int) -> list:
        try:
            return self.base.resolve(host, port)
        except OSError:
            if SEO.numeric_host(host) is not None:
                raise
            return [SEO.ATTACKER_PUBLIC]

    def handle(self, request):
        ip = request.url.host
        host = (request.headers.get("host") or "").split(":")[0].lower()
        path = request.url.raw_path.decode("ascii", "replace").split("?")[0]
        self.requests.append(f"{ip} {host} {path}")
        if ip not in SEO.PUBLIC_IPS:
            self.internal.append(f"{ip} {host} {path}")
            return SEO.respond(200, SEO.SECRET, {"content-type": "text/html"})
        if path == "/robots.txt":
            return SEO.respond(200, "User-agent: *\nAllow: /\n", {"content-type": "text/plain"})
        m = re.search(r"/wr-([0-9a-f]+)$", path)
        if m is not None:
            try:
                target = bytes.fromhex(m.group(1)).decode("utf-8", "surrogatepass")
            except ValueError:
                return SEO.respond(400, "bad", {"content-type": "text/plain"})
            return SEO.respond(self.status, b"", {"location": target})
        canon = ""
        c = re.search(r"/canon-([0-9a-f]+)", path)
        if c is not None:
            canon = f'<link rel="canonical" href="{bytes.fromhex(c.group(1)).decode("utf-8", "replace")}">'
        return SEO.respond(200, f"<html><head><title>x</title>{canon}</head><body>page</body></html>",
                           {"content-type": "text/html; charset=utf-8"})


# ============================================================================================ case setup

def setup(tmp):
    _RID[0] = 0
    return SimpleNamespace(tmp=Path(tmp), steps=[], runs=[], cfg=None, main=None, control=None, rerun=None,
                           probe=None, second=None, extra={}, n=0)


def teardown(ctx) -> None:
    for r in ctx.runs:
        try:
            r.h.svc.close()
        except Exception:  # noqa: BLE001 - a harness already closed is fine
            pass


DEFAULTS = {"engines": "openai,anthropic,google", "text": TEXT, "cites": ["https://site.test/services",
                                                                          "https://competitor.test/a"],
            "mode": "answer", "script": None, "samples": 3, "prompts": 1, "competitors": 1, "resolve": "off",
            "resolve_max": 40, "caps": None, "domain": CLIENT, "brand": [BRAND], "rules": None, "injections": [],
            "ops": None, "status": 301, "only_engine": None}


def _norm_cite(c: str) -> str:
    """A pinned host (no scheme) is cited as https://<host>/; a pinned SSRF target loses its fixture ``{port}``."""
    c = c.replace(":{port}", "")
    return c if "://" in c or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", c) else f"https://{c}/"


def build(ctx, cfg: dict):
    """A fresh harness wired to hostile providers for the configured engines, the client's domain registered, a
    prompt set made. Returns the run record (harness, providers, clocks, prompt set id)."""
    ctx.n += 1
    engines = [e for e in cfg["engines"].split(",") if e]
    env = {**TC.BUDGET_ON, "SEO_PROBE_SAMPLES": str(cfg["samples"]), "SEO_PERPLEXITY_API_KEY": BASE.KEY,
           **BASE.BUDGET}
    if cfg["resolve"] == "on":
        env.update(SEO_CALLUM_RESOLVE_URLS="1", SEO_CALLUM_RESOLVE_MAX=str(cfg["resolve_max"]))
    for k, v in (cfg.get("caps") or {}).items():
        env[k] = v
    disabled = cfg.get("disabled") or []
    for e in disabled:
        n = __import__("config").ENGINE_ENV.get(e)
        if n:
            for k in list(env):
                if k.startswith(f"SEO_{e.upper()}_"):
                    env.pop(k)
        if e == "perplexity":
            env.pop("SEO_PERPLEXITY_API_KEY", None)
    h = H.Harness(ctx.tmp / f"h{ctx.n}", **env)
    web = RedirectWeb()
    web.status = cfg.get("status") or 301
    h.ports.fetcher = SEO.FETCH.Fetcher(timeout_s=1, max_bytes=SEO.MAX_BYTES, max_redirects=5, resolver=web.resolve,
                                        transport=httpx.MockTransport(web.handle))
    providers, clocks = {}, {}
    text = cfg["text"]
    if cfg.get("injections"):
        text = "\n\n".join([text] + list(cfg["injections"]))
    for e in engines:
        if e in disabled:
            continue
        clock = BASE.Clock()
        p = Provider(e, clock, mode=cfg["mode"] if cfg.get("only_engine") in (None, e) else "answer", text=text,
                     cites=cfg["cites"], script=cfg.get("script") if cfg.get("only_engine") in (None, e) else None,
                     ops=cfg.get("ops"))
        p.svc = h
        fake = BASE.FakePerplexity(p)
        if e == "perplexity":
            h.ports.engines[e] = BASE.engine(fake, clock)
        else:
            h.ports.engines[e] = TE.make(e, fake, clock)
        providers[e], clocks[e] = (p, fake), clock
    h.ok(h.post(f"/tenants/zbm/domains", {"request_id": rid(), "domains": [cfg["domain"]]}, andre=True))
    comps = ["competitor.test"] + [f"rival{i}.test" for i in range(1, max(1, cfg["competitors"]))]
    prompts = [f"who recovers marketplace revenue question number {i}" for i in range(cfg["prompts"])]
    body = {"request_id": rid(), "name": "warroom", "brand_terms": list(cfg["brand"]), "competitor_domains": comps,
            "prompts": prompts, "engines": engines}
    if cfg.get("rules"):
        body["brand_term_rules"] = cfg["rules"]
    ps = h.ok(h.post("/tenants/zbm/prompt-sets", body), 201)
    run = SimpleNamespace(h=h, web=web, providers=providers, clocks=clocks, psid=ps["prompt_set_id"], cfg=cfg,
                          resp=None, view=None, engines=engines, snap=SEO._fingerprint(h), extra_steps=[])
    ctx.runs.append(run)
    return run


def execute(run, tid="zbm", caller="seo_agent", **extra):
    h = run.h
    r = h.post(f"/tenants/{tid}/citation-runs", {"request_id": rid(), "domain": run.cfg["domain"],
                                                  "prompt_set_id": run.psid, **extra}, caller=caller)
    run.resp = r
    try:
        run.view = r.json()
    except ValueError:
        run.view = None
    return r


def _cfg(ctx, **kw) -> dict:
    cfg = dict(DEFAULTS)
    for k, v in kw.items():
        if v is not None:
            cfg[k] = v
    cfg["cites"] = [_norm_cite(c) for c in cfg["cites"]]
    return cfg


# ============================================================================================ actions

FORWARDS = {"client": "https://{d}/pricing", "lookalike_hop": "https://{l}/fwd", "private_hop": "http://10.0.0.5/fwd",
            "metadata_hop": "http://169.254.169.254/latest/meta-data/", "competitor": "https://competitor.test/x",
            "hostile_dns_hop": "http://internal.attacker.test/admin", "rebind_hop": "http://rebind.attacker.test/"}


def _forward(cited: str, how: str, domain: str) -> str:
    """Make ``cited`` redirect (through the fake web) to the client, through a lookalike, or inside the network."""
    import grammar
    if how in (None, "none") or ("/wr-" in cited and how != "behind_client"):
        return cited
    if how == "behind_client":                    # the client's own URL redirects TO the cited one (open redirect)
        return grammar.via(domain, cited)
    look = chaos_homoglyph_host(domain)
    if how == "lookalike_hop":                    # cited -> a lookalike host -> the client's page
        target = grammar.via(look, f"https://{domain}/pricing")
    else:
        target = FORWARDS[how].format(d=domain, l=look)
    m = re.match(r"([A-Za-z][A-Za-z0-9+.-]*://[^/?#]*)", cited)
    if m is None:
        return cited
    return grammar.via(m.group(1).split("://", 1)[1], target, m.group(1).split("://", 1)[0])


def chaos_homoglyph_host(domain: str) -> str:
    """The client's name with its first letter that has a core lookalike swapped for it (deterministic)."""
    import chaos
    table = chaos.homoglyphs(True)
    for i, c in enumerate(domain):
        if c in table:
            return domain[:i] + table[c][0] + domain[i + 1:]
    return "x" + domain


def citation_run(ctx, cited=None, cites=None, text=None, mode=None, engines=None, resolve=None, prefix=None,
                 suffix=None, inject=None, domain=None, brand=None, samples=None, script=None, only_engine=None,
                 status=None, control="none", rerun="no", forward=None, pii=None):
    """One citation run on fresh harnesses. ``cited`` (a seed) is added to ``cites`` (after ``forward`` puts a
    redirect behind it); ``prefix`` / ``suffix`` wrap the seed text; ``inject`` is an injection the control run
    (``control: injection``) leaves out; ``pii``: personal data that must never reach an excerpt."""
    if isinstance(text, str) and (prefix or suffix):
        text = f"{prefix or ''}{text}{suffix or ''}"
    cl = list(cites if cites is not None else DEFAULTS["cites"])
    if cited is not None:
        cl = [_forward(_norm_cite(cited), forward, domain or CLIENT)] + cl
    if pii is not None:
        ctx.extra["pii"] = pii
    if isinstance(script, str):
        script = script.split(",")
    cfg = _cfg(ctx, cites=cl, text=text, mode=mode, engines=engines, resolve=resolve, domain=domain,
               brand=[brand] if isinstance(brand, str) else brand, samples=samples, script=script,
               only_engine=only_engine, status=status, injections=[inject] if inject else None)
    ctx.cfg = cfg
    ctx.main = build(ctx, cfg)
    r = execute(ctx.main)
    if control == "injection":
        ctx.control = build(ctx, {**cfg, "injections": []})
        execute(ctx.control)
    if rerun == "yes":
        ctx.rerun = build(ctx, cfg)
        execute(ctx.rerun)
    return r


def generated_answer(ctx, spec):
    """grammar.callum_answer: one engine, the generated text and citations, the injections kept apart: the case, its
    control without the injections, and a re-run (determinism)."""
    g = json.loads(spec)
    cfg = _cfg(ctx, engines=g["engine"], text=g["text"] or "x", cites=g["cites"], injections=g["injections"])
    ctx.cfg = cfg
    ctx.main = build(ctx, cfg)
    r = execute(ctx.main)
    ctx.control = build(ctx, {**cfg, "injections": []})
    execute(ctx.control)
    ctx.rerun = build(ctx, cfg)
    execute(ctx.rerun)
    return r


def generated_protocol(ctx, spec):
    """grammar.engine_body_mutation: the documented answer with structural ops applied; the control is the
    unmutated answer (what a preserving op must reproduce, and what no op may add a credit to)."""
    g = json.loads(spec)
    cites = ["https://site.test/services", "https://competitor.test/a", "https://xn--ste-jhd.test/"]
    if "duplicate_citation" in g["ops"]:
        cites = cites + cites[:2]
    if "reverse_citations" in g["ops"]:
        cites = list(reversed(cites))
    cfg = _cfg(ctx, engines=g["engine"], mode="ops", ops=g, cites=cites)
    ctx.cfg = cfg
    ctx.extra["ops"] = g["ops"]
    ctx.main = build(ctx, cfg)
    r = execute(ctx.main)
    ctx.control = build(ctx, _cfg(ctx, engines=g["engine"], cites=cites))
    execute(ctx.control)
    return r


def _hop_url(kind: str, nxt: str, client: str, rng_tag: str) -> str:
    import grammar
    host = {"client": client, "client_sub": "blog." + client, "lookalike": "sіte.test",
            "lookalike_puny": "xn--ste-jhd.test", "shortener": "t.co", "redirector": grammar.REDIRECTOR,
            "competitor": "competitor.test", "third_party": "news.test", "private": "internal.attacker.test",
            "loop": "loop.test", "dead": "dead.test", "slow": "slow.test"}[kind]
    return grammar.via(host, nxt, "http" if kind == "private" else "https")


def generated_chain(ctx, spec):
    """grammar.redirect_chain: a cited URL whose redirect chain the fake web serves hop by hop (resolution on)."""
    g = json.loads(spec)
    import grammar
    final_host = {"client": CLIENT, "client_sub": "blog." + CLIENT, "competitor": "competitor.test",
                  "lookalike": "sіte.test", "third_party": "news.test", "private": "internal.attacker.test"}[g["final"]]
    final = f"{'http' if g['final'] == 'private' else 'https'}://{final_host}/landing"
    if g.get("canonical"):
        ch = {"client": CLIENT, "lookalike": "sіte.test", "competitor": "competitor.test"}[g["canonical"]]
        final += "/canon-" + f"https://{ch}/page".encode().hex()
    url = final
    for i, kind in enumerate(reversed(g["hops"])):
        url = _hop_url(kind, url, CLIENT, str(i))
    if g["start"] in ("client", "competitor") and not g["hops"]:
        url = _hop_url(g["start"], url, CLIENT, "s")
    elif g["start"] not in ("client", "competitor"):
        url = _hop_url(g["start"], url, CLIENT, "s")
    if "loop" in g["hops"]:                       # a loop: the chain points back at its own start
        url = grammar.via("loop.test", grammar.via("loop.test", url))
    cfg = _cfg(ctx, engines="openai", cites=[url, "https://competitor.test/a"], resolve="on",
               text=TEXT if g.get("brand") else "Other firms exist.", status=g.get("status"))
    ctx.cfg = cfg
    ctx.main = build(ctx, cfg)
    return execute(ctx.main)


def concurrent_run(ctx, second):
    """A second request for the same tenant arrives while the first run's first engine request is in flight."""
    cfg = _cfg(ctx, engines="openai,anthropic")
    ctx.cfg = cfg
    run = ctx.main = build(ctx, cfg)
    h = run.h
    got = {}

    def call(body):
        # the service method itself (the request in flight holds the ASGI portal): what the endpoint would answer
        import models
        from errors import SeoError
        body = models.CitationRunCreate.model_validate(body).model_dump(mode="json")    # what the endpoint passes
        try:
            return {"status": 201, "body": h.svc.request_citation_run("seo_agent", "zbm", body, andre=False)}
        except SeoError as e:
            return {"status": {"Conflict": 409, "Forbidden": 403, "NotFound": 404, "Invalid": 422}.get(
                type(e).__name__, 400), "body": {"detail": e.reason}}

    def hook(request):
        if second == "same_tenant_new_request":
            got["r"] = call({"request_id": rid(), "domain": CLIENT, "prompt_set_id": run.psid})
        elif second == "same_tenant_narrowed":
            got["r"] = call({"request_id": rid(), "domain": CLIENT, "prompt_set_id": run.psid,
                             "engines": ["anthropic"]})
        elif second == "same_tenant_other_prompt_set":
            got["r"] = call({"request_id": rid(), "domain": CLIENT, "prompt_set_id": ctx.extra["other_ps"]})
        elif second == "same_request_replayed":
            got["r"] = call(ctx.extra["first_body"])
        got["running"] = len(h.svc.running_citation_runs)
    if second == "same_tenant_other_prompt_set":
        ctx.extra["other_ps"] = h.ok(h.post("/tenants/zbm/prompt-sets", {
            "request_id": rid(), "name": "second", "brand_terms": [BRAND], "prompts": ["another prompt here"],
            "engines": ["openai"]}), 201)["prompt_set_id"]
    body = {"request_id": rid(), "domain": CLIENT, "prompt_set_id": run.psid}
    ctx.extra["first_body"] = body
    run.providers["openai"][0].on_request = hook
    run.resp = h.post("/tenants/zbm/citation-runs", body)
    run.view = run.resp.json()
    r2 = got.get("r")
    ctx.second = {"kind": second, "status": None if r2 is None else r2["status"],
                  "body": None if r2 is None else r2["body"], "running_during": got.get("running")}
    return run.resp


def cross_tenant(ctx, probe):
    """acme (another tenant, with its hub token) tries to reach zbm's citation run, and the same request against
    something that does not exist."""
    cfg = _cfg(ctx, engines="openai")
    ctx.cfg = cfg
    run = ctx.main = build(ctx, cfg)
    h = run.h
    execute(run)
    cid = run.view["citation_run_id"]
    missing = "seo-cit-" + "0" * 40
    h.tenant("acme", domains=("other.test",))
    before = sum(p[0].n for p in run.providers.values())

    def pair(fn):
        a, b = fn(cid), fn(missing)
        return a, b
    hub = {"caller": "hub", "tenant": "acme"}
    calls = {
        "hub_get_run": lambda x: h.get(f"/tenants/zbm/citation-runs/{x}", **hub),
        "hub_get_run_own_path": lambda x: h.get(f"/tenants/acme/citation-runs/{x}", **hub),
        "dashboard_get_under_other_tenant": lambda x: h.get(f"/tenants/acme/citation-runs/{x}"),
        "hub_mnc_priority": lambda x: h.get(f"/tenants/zbm/citation-runs/{x}/mnc-priority", **hub),
        "dashboard_mnc_under_other_tenant": lambda x: h.get(f"/tenants/acme/citation-runs/{x}/mnc-priority"),
        "hub_list": lambda x: h.get("/tenants/zbm/citation-runs", **hub),
        "id_uppercased": lambda x: h.get(f"/tenants/acme/citation-runs/{x.upper()}"),
        "id_trailing_slash": lambda x: h.get(f"/tenants/acme/citation-runs/{x}/"),
        "id_percent_encoded": lambda x: h.get(f"/tenants/acme/citation-runs/{x[:4]}%2D{x[5:]}"),
        "path_traversal": lambda x: h.get(f"/tenants/acme/citation-runs/..%2F..%2Fzbm%2Fcitation-runs%2F{x}"),
        "hub_start_run_on_other_tenant": lambda x: h.post("/tenants/zbm/citation-runs", {
            "request_id": rid(), "domain": CLIENT, "prompt_set_id": run.psid}, caller="hub", tenant="acme"),
        "acme_runs_with_zbm_prompt_set": lambda x: h.post("/tenants/acme/citation-runs", {
            "request_id": rid(), "domain": "other.test", "prompt_set_id": run.psid}),
        "acme_runs_on_zbm_domain": lambda x: h.post("/tenants/acme/citation-runs", {
            "request_id": rid(), "domain": CLIENT, "prompt_set_id": run.psid}),
        "compliance_reads_under_other_tenant": lambda x: h.get(f"/tenants/acme/citation-runs/{x}",
                                                               caller="compliance_38"),
    }
    a, b = pair(calls[probe])
    acme_list = h.get("/tenants/acme/citation-runs")
    ctx.probe = {"probe": probe, "status": a.status_code, "missing_status": b.status_code,
                 "same_as_missing": (a.status_code, a.text.replace(cid, "<id>")) ==
                                    (b.status_code, b.text.replace(missing, "<id>")),
                 "bodies": [a.text, b.text, acme_list.text], "acme_list": acme_list.json() if acme_list.status_code
                 == 200 else None, "engine_requests_added": sum(p[0].n for p in run.providers.values()) - before,
                 # what acme must never learn: zbm's prompt set, its result and its answers' words (a run id the
                 # attacker put in its own request is not a leak when echoed back)
                 "foreign": [run.view.get("result_sha256") or "", "Z Best Media recovers",
                             "who recovers marketplace revenue question"] + ([] if probe.startswith("acme_runs")
                                                                            else [run.psid])}
    return a


def idempotency(ctx, kind):
    cfg = _cfg(ctx, engines="openai,google")
    ctx.cfg = cfg
    run = ctx.main = build(ctx, cfg)
    h = run.h
    body = {"request_id": rid(), "domain": CLIENT, "prompt_set_id": run.psid}
    a = h.post("/tenants/zbm/citation-runs", body)
    n1 = {e: p[0].n for e, p in run.providers.items()}
    if kind == "same_request_twice":
        b = h.post("/tenants/zbm/citation-runs", body)
    elif kind == "same_id_other_body":
        b = h.post("/tenants/zbm/citation-runs", {**body, "engines": ["openai"]})
    elif kind == "same_request_five_times":
        for _ in range(4):
            b = h.post("/tenants/zbm/citation-runs", body)
    else:
        raise ValueError(kind)
    n2 = {e: p[0].n for e, p in run.providers.items()}
    run.resp, run.view = a, a.json()
    ctx.second = {"kind": kind, "status": b.status_code, "body": b.json(), "first": a.json(),
                  "requests_added": sum(n2.values()) - sum(n1.values()),
                  "runs_recorded": len(h.ledger.of_type("citation_run_recorded"))}
    return b


def disabled(ctx, kind):
    """Engines off, unconfigured, budget-less, killed or narrowed: nothing may be asked of what is off."""
    caps = None
    dis = []
    engines = "openai,anthropic,google,perplexity"
    if kind == "all_disabled":
        dis = ["openai", "anthropic", "google", "perplexity"]
    elif kind == "one_disabled":
        dis = ["google"]
    elif kind == "budgets_off":
        caps = {k: "0" for k in TC.BUDGET_ON if "CALLS" in k or "SPEND" in k}
        caps.update({"SEO_PERPLEXITY_DAILY_CALLS": "0"})
    elif kind == "run_caps_zero":
        caps = {k: "0" for k in TC.BUDGET_ON if "RUN_MAX" in k}
    cfg = _cfg(ctx, engines=engines, caps=caps)
    cfg["disabled"] = dis
    ctx.cfg = cfg
    ctx.extra["off"] = list(dis) if kind not in ("budgets_off", "run_caps_zero") else \
        (["openai", "anthropic", "google", "perplexity"] if kind == "budgets_off" else ["openai", "anthropic",
                                                                                         "google"])
    run = ctx.main = build(ctx, cfg)
    h = run.h
    if kind == "provider_killed":
        h.ok(h.switch("provider:anthropic"))
        ctx.extra["off"] = ["anthropic"]
    if kind == "narrowed_to_one":
        ctx.extra["off"] = ["anthropic", "google", "perplexity"]
        ctx.extra["narrowed"] = True
        return execute(run, engines=["openai"])
    if kind == "narrowed_to_none_of_the_set":
        run2 = build(ctx, _cfg(ctx, engines="openai"))
        ctx.main = run2
        ctx.extra["off"] = ["openai"]
        ctx.extra["expect_refused"] = True
        return execute(run2, engines=["google"])
    return execute(run)


def scale(ctx, shape):
    """Enterprise scale: up to 50 prompts x 4 engines x 20 competitors x many citations, with per-run caps."""
    prompts, samples, cites_n, cap = {"50x4x3": (50, 3, 4, None), "50x4x3_capped": (50, 3, 4, "40"),
                                      "50x4x3_rows": (50, 3, 25, None), "20x4x5_spend": (20, 5, 3, "spend"),
                                      "50x1x50": (50, 50, 2, None)}[shape]
    cites = [f"https://site.test/p{i}" if i % 3 == 0 else f"https://rival{i % 20}.test/x{i}" for i in range(cites_n)]
    caps = {}
    if cap == "spend":
        caps = {"SEO_OPENAI_RUN_MAX_SPEND_USD": "1.00", "SEO_ANTHROPIC_RUN_MAX_SPEND_USD": "0.70",
                "SEO_GOOGLE_RUN_MAX_SPEND_USD": "0.45", "SEO_PERPLEXITY_RUN_MAX_CALLS": "1000",
                "SEO_PERPLEXITY_RUN_MAX_SPEND_USD": "0.30"}
    elif cap:
        caps = {"SEO_OPENAI_RUN_MAX_CALLS": cap, "SEO_ANTHROPIC_RUN_MAX_CALLS": cap, "SEO_GOOGLE_RUN_MAX_CALLS": cap,
                "SEO_PERPLEXITY_RUN_MAX_CALLS": cap, "SEO_PERPLEXITY_RUN_MAX_SPEND_USD": "50"}
    cfg = _cfg(ctx, engines="openai,anthropic,google,perplexity", prompts=prompts, samples=samples,
               competitors=20, cites=cites, caps={**TC.BUDGET_ON, **{k: "100000" for k in TC.BUDGET_ON if "DAILY" in k},
                                                  **caps})
    if shape == "50x1x50":
        cfg["engines"] = "openai"
    ctx.cfg = cfg
    run = ctx.main = build(ctx, cfg)
    return execute(run)


def budget(ctx, cap, engine="openai", prompts=3):
    """A per-run or daily cap hit mid-run (``cap``: ``<setting suffix>=<value>``)."""
    name, _, value = cap.partition("=")
    caps = {f"SEO_{engine.upper()}_{name}": value}
    if engine == "perplexity" and name.startswith("RUN_MAX"):
        other = "RUN_MAX_SPEND_USD" if name == "RUN_MAX_CALLS" else "RUN_MAX_CALLS"
        caps[f"SEO_PERPLEXITY_{other}"] = "1000"
    cfg = _cfg(ctx, engines=engine, prompts=int(prompts), caps=caps)
    ctx.cfg = cfg
    ctx.extra["cap"] = (engine, name, value)
    run = ctx.main = build(ctx, cfg)
    return execute(run)


def secret_echo(ctx, kind, engines="openai,anthropic,google,perplexity"):
    """A provider (or a page it read) puts the engine's own API key in what it returns."""
    ctx.extra["secret_kind"] = kind
    names = engines.split(",")
    cfg = _cfg(ctx, engines=engines)
    ctx.cfg = cfg
    run = ctx.main = build(ctx, cfg)
    for e in names:
        p = run.providers[e][0]
        key = SECRETS[f"{e}_key"]
        if kind == "answer_text":
            p.text = f"{BRAND} is listed. Authorization: Bearer {key}"
        elif kind == "answer_text_far":
            p.text = f"{BRAND} is listed. " + "filler " * 40 + f"key={key}"
        elif kind == "cited_path":
            p.cites = [f"https://site.test/{key}", "https://competitor.test/a"]
        elif kind == "cited_query":
            p.cites = [f"https://site.test/page?api_key={key}", f"https://competitor.test/a#{key}"]
        elif kind == "cited_userinfo":
            p.cites = [f"https://{key}@site.test/", f"https://user:{key}@competitor.test/a"]
        elif kind == "cited_host_label":
            p.cites = [f"https://{key[:60]}.competitor.test/a"]
        elif kind == "error_401_body":
            p.script = ["401echo"]
        elif kind == "brand_term_is_key":
            p.text = f"{key} and {BRAND}"
        else:
            raise ValueError(kind)
    return execute(run)


ACTIONS = {"secret_echo": secret_echo, "citation_run": citation_run, "generated_answer": generated_answer, "generated_protocol": generated_protocol,
           "generated_chain": generated_chain, "concurrent_run": concurrent_run, "cross_tenant": cross_tenant,
           "idempotency": idempotency, "disabled": disabled, "scale": scale, "budget": budget}


# ============================================================================================ observation (the judge)

def _outputs(run, steps) -> list:
    h = run.h
    blobs = [json.dumps([s.get("body") for s in steps], ensure_ascii=False),
             run.resp.text if run.resp is not None else "",
             json.dumps([e for e in h.ledger.events], ensure_ascii=False, default=str)]
    try:
        blobs.append(json.dumps(h.svc.audit_export(1, 100_000), ensure_ascii=False, default=str))
    except Exception as e:  # noqa: BLE001 - a closed export is itself reported
        blobs.append(f"export failed: {type(e).__name__}")
    for path in ("/status", "/department"):
        try:
            blobs.append(h.get(path).text)
        except Exception:  # noqa: BLE001
            pass
    return blobs


def _result(run) -> dict:
    v = run.view if isinstance(run.view, dict) else {}
    return v.get("result") or {}


def _rows(run) -> list:
    return list(((_result(run).get("facts") or {}).get("rows")) or [])


def signature(run) -> list:
    """What a classification decided, without run-specific ids, times or excerpts."""
    out = []
    for r in _rows(run):
        out.append([r.get("engine"), r.get("prompt_index"), r.get("sample"), r.get("class"), r.get("reason"),
                    r.get("url"), r.get("confidence"), r.get("flags")])
    tot = ((_result(run).get("facts") or {}).get("metrics") or {}).get("totals") or {}
    out.append({k: tot.get(k) for k in ("usable", "mnc", "mnc_low", "strength", "opportunity_urls", "strength_urls",
                                        "lookalike_urls", "inconclusive_urls", "inconclusive_answers")})
    out.append(_result(run).get("outcome"))
    return out


def _masked(run):
    """The result with what legitimately differs between two runs masked: the derived ids (run, prompt set, engine
    runs: they derive from the request id) and the adapter's wall-clock stamp."""
    s = json.dumps(_result(run), sort_keys=True, ensure_ascii=False)
    s = re.sub(r"seo-[a-z]{3}-[0-9a-f]{40}", "<id>", s)
    return re.sub(r'"provider_asked_at": "[^"]*"', '"provider_asked_at": "<t>"', s)


def _rates(obj, path="", out=None):
    """Every rate dict ({value, n, ...}) in a result, with its path."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        if "value" in obj and "n" in obj and "k" in obj:
            out.append((path, obj))
        for k, v in obj.items():
            _rates(v, f"{path}.{k}", out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _rates(v, f"{path}[{i}]", out)
    return out


def _has_controls(s: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in s or "")


def judge_run(run, state: dict, prefix: str = "") -> None:
    cfg, client, resolve = run.cfg, run.cfg["domain"], run.cfg["resolve"] == "on"
    rows = _rows(run)
    res = _result(run)
    facts = res.get("facts") or {}
    state[prefix + "run_http"] = run.resp.status_code if run.resp is not None else None
    v = run.view if isinstance(run.view, dict) else {}
    state[prefix + "run_status"] = v.get("status")
    state[prefix + "outcome"] = res.get("outcome")
    cites = list(dict.fromkeys(c for c in cfg["cites"] if isinstance(c, str)))
    if cfg.get("mode") == "*duplicate_citations":
        cites = list(dict.fromkeys(cites))
    credit = [c for c in cites if creditable(c, client, resolve) and not tainted(c, client)]
    viol = []
    per_answer: dict = {}
    for r in rows:
        if r.get("class") != "STRENGTH_URL":
            continue
        key = (r.get("engine"), r.get("prompt_index"), r.get("sample"))
        per_answer[key] = per_answer.get(key, 0) + 1
        host = ((r.get("chain") or {}).get("cited") or {}).get("host")
        final = ((r.get("chain") or {}).get("final") or {}).get("host")
        decided = final if final else host
        if oracle_host(decided, client) != CLIENT_:
            viol.append(f"STRENGTH_URL on {decided!r} (cited {r.get('url')!r}) is not the client {client}")
    for key, n in per_answer.items():
        if n > len(credit):
            viol.append(f"{key}: {n} STRENGTH_URL rows but only {len(credit)} cited URL(s) can be the client: "
                        f"{[c[:120] for c in credit]}")
    state[prefix + "strength_violations"] = viol
    # with resolution off only the cited host can be judged; with it on, every hop of the chain
    tainted_cites = [c for c in cites if (tainted(c, client) if resolve else oracle_url(c, client) == LOOKALIKE_)]
    look_rows = sum(1 for r in rows if r.get("class") == "LOOKALIKE_URL")
    state[prefix + "lookalike_unflagged"] = [] if not tainted_cites or look_rows or not rows else \
        [f"lookalike citation(s) {[c[:80] for c in tainted_cites]} produced no LOOKALIKE_URL row"]
    state[prefix + "non_web_rows"] = [r.get("url") for r in rows if r.get("url") and
                                      (r["url"].split(":", 1)[0].lower() not in ("http", "https"))]
    state[prefix + "long_urls"] = [len(r["url"]) for r in rows if isinstance(r.get("url"), str) and len(r["url"]) > 500]
    state[prefix + "control_chars_in_urls"] = [r.get("url") for r in rows
                                               if _has_controls(r.get("url") or "") or _has_controls(
                                                   (((r.get("chain") or {}).get("cited") or {}).get("host")) or "")]
    bad_rates = []
    for path, rate in _rates(res):
        if rate.get("n") == 0 and rate.get("value") is not None:
            bad_rates.append(f"{path}: value {rate.get('value')} with n=0")
    for m in ((facts.get("metrics") or {}).get("by_engine_prompt") or []):
        mnc, low = m.get("mentioned_not_cited") or {}, m.get("mentioned_not_cited_low_confidence") or {}
        if mnc.get("k") == 0 and low.get("k") and mnc.get("value") is not None:
            bad_rates.append(f"{m.get('engine')}/{m.get('prompt_index')}: MNC {mnc.get('value')} from unconfirmed")
    state[prefix + "unknown_shown_as_zero"] = bad_rates
    # engines that can never answer in this case: nothing but INCONCLUSIVE, no usable answer, no not-cited verdict
    failing = []
    for e, (p, _f) in run.providers.items():
        if (p.mode in FAILING_MODES and not p.script) or (p.script and all(s in FAILING_STEPS for s in p.script)):
            failing.append(e)
    counted = []
    for e in failing:
        be = ((facts.get("metrics") or {}).get("by_engine") or {}).get(e) or {}
        if be.get("usable"):
            counted.append(f"{e}: {be.get('usable')} usable answer(s) from a failing provider")
        for r in rows:
            if r.get("engine") == e and r.get("class") != "INCONCLUSIVE":
                counted.append(f"{e}: a {r.get('class')} row from a failing provider")
                break
    state[prefix + "failed_counted"] = counted
    # structural bounds: requests and the fake clock's seconds (never wall-clock time)
    prompts, samples = len([1 for _ in range(cfg["prompts"])]), cfg["samples"]
    over, slow, unreserved = [], [], []
    for e, (p, fake) in run.providers.items():
        eng = run.h.ports.engines.get(e)
        attempts = int(getattr(eng, "attempts", 1) or 1)
        timeout = float(getattr(eng, "timeout_s", 60) or 60)
        bound = prompts * samples * attempts
        if p.n > bound:
            over.append(f"{e}: {p.n} requests > {bound} (prompts x samples x attempts)")
        tbound = prompts * samples * (timeout + 1)
        if run.clocks[e].t > tbound:
            slow.append(f"{e}: {run.clocks[e].t:.1f} fake seconds > {tbound}")
        if not all(p.reserved_before):
            unreserved.append(f"{e}: {p.reserved_before.count(False)} request(s) before any reservation")
    state[prefix + "requests_over_bound"] = over
    state[prefix + "time_over_bound"] = slow
    state[prefix + "unreserved_requests"] = unreserved
    # per-run caps: requests reaching the provider and reserved calls / dollars on the ledger never exceed a cap
    cap_viol = []
    lim_env = run.h.env
    for e, (p, _f) in run.providers.items():
        n = __import__("config").ENGINE_ENV.get(e) or {"run_max_calls": "SEO_PERPLEXITY_RUN_MAX_CALLS",
                                                      "run_max_spend_usd": "SEO_PERPLEXITY_RUN_MAX_SPEND_USD"}
        mc, ms = lim_env.get(n["run_max_calls"]), lim_env.get(n["run_max_spend_usd"])
        res_ev = [x["_payload"] for x in run.h.ledger.of_type("engine_calls_reserved") if x["_payload"]["engine"] == e]
        calls = sum(x["calls"] for x in res_ev)
        spend = sum(float(x["spend_usd"]) for x in res_ev)
        if mc not in (None, "") and (p.n > int(mc) or calls > int(mc)):
            cap_viol.append(f"{e}: {p.n} requests / {calls} reserved calls > run cap {mc}")
        if ms not in (None, "") and spend > float(ms) + 1e-9:
            cap_viol.append(f"{e}: {spend:.6f} reserved > run spend cap {ms}")
        for nm, val in (("daily_calls", calls), ("tenant_daily_calls", calls)):
            key = n.get(nm) if isinstance(n, dict) else None
            if key and lim_env.get(key) not in (None, "") and val > int(lim_env[key]):
                cap_viol.append(f"{e}: {val} reserved calls > {key}={lim_env[key]}")
    state[prefix + "cap_violations"] = cap_viol
    state[prefix + "rows"] = len(rows)
    state[prefix + "rows_truncated"] = facts.get("rows_truncated")
    # evidence: bounded and PII-screened (SHOULD)
    long_ex, pii = [], []
    needles = [n for n in SEO.NEEDLES if len(n) >= 6]
    for r in rows:
        ex = ((r.get("evidence") or {}).get("excerpt") or {})
        t = ex.get("text") if isinstance(ex, dict) else None
        if isinstance(t, str):
            if len(t) > 160:
                long_ex.append(len(t))
            ft = SEO._fold(t)
            pii += [n for n in needles if SEO._fold(n) in ft]
    state[prefix + "long_excerpts"] = long_ex
    state[prefix + "pii_in_excerpts"] = sorted(set(pii))
    state[prefix + "state_unchanged"] = SEO._fingerprint(run.h) == run.snap
    # answer-level mention verdicts (brand scenarios)
    tot = ((facts.get("metrics") or {}).get("totals")) or {}
    usable = tot.get("usable") or 0
    high = sum(1 for r in rows if r.get("class") == "MENTIONED_NOT_CITED" and r.get("confidence") == "HIGH")
    low = sum(1 for r in rows if r.get("class") == "MENTIONED_NOT_CITED" and r.get("confidence") == "LOW_CONFIDENCE")
    items = len(((facts.get("mnc_priority") or {}).get("items")) or [])
    state[prefix + "usable_answers"] = usable
    state[prefix + "all_mnc_high"] = usable > 0 and high == usable and items > 0
    state[prefix + "no_mnc_high"] = high == 0 and items == 0
    state[prefix + "mnc_low_answers"] = low
    want = cfg["prompts"] * cfg["samples"] * len(run.providers)
    state[prefix + "preserved_answer_refused"] = [] if not (cfg.get("mode") or "").startswith("*") or usable == want \
        else [f"{cfg['mode']}: {usable} of {want} answers counted"]


def observe(ctx) -> dict:
    state: dict = {}
    runs = [r for r in (ctx.main, ctx.control, ctx.rerun) if r is not None]
    outs = []
    for r in runs:
        outs += _outputs(r, ctx.steps)
    hay = [SEO._fold(unquote(unquote(b))) for b in outs]
    raw = "\n".join(outs)
    state["secrets_leaked"] = sorted(name for name, val in SECRETS.items()
                                     if val and (val in raw or SEO._fold(val) in "\n".join(hay)))
    state["internal_requests"] = sorted({x for r in runs for x in r.web.internal})
    state["secret_marker_leaked"] = SEO.SECRET in raw
    state["server_errors"] = [f"{s['action']}: {s['status']}" for s in ctx.steps if (s.get("status") or 0) >= 500]
    if ctx.extra.get("pii"):
        pf = SEO._fold(ctx.extra["pii"])
        state["pii_seed_in_excerpts"] = [] if ctx.main is None else [
            r.get("class") for r in _rows(ctx.main)
            if pf in SEO._fold(((((r.get("evidence") or {}).get("excerpt")) or {}).get("text")) or "")]
    if ctx.main is not None and ctx.main.resp is not None:
        judge_run(ctx.main, state)
        state["engine_requests"] = sum(p[0].n for p in ctx.main.providers.values())
    if ctx.control is not None and ctx.control.resp is not None:
        a, b = signature(ctx.main), signature(ctx.control)
        state["injection_changed_classification"] = [] if a == b else [
            f"with injections: {json.dumps(a, ensure_ascii=False)[:700]} | without: "
            f"{json.dumps(b, ensure_ascii=False)[:700]}"]
        judge_run(ctx.control, state, prefix="control_")
        ops = ctx.extra.get("ops")
        if ops is not None:
            import grammar
            breaking = [o for o in ops if o in grammar.BREAKING]
            if breaking:
                state["breaking_answer_counted"] = [] if not any(
                    r.get("class") != "INCONCLUSIVE" for r in _rows(ctx.main)) else [
                    f"ops {ops}: a breaking mutation was counted as an answer"]
            else:
                state["breaking_answer_counted"] = []
            main_credit = sorted({r.get("url") for r in _rows(ctx.main) if r.get("class") == "STRENGTH_URL"})
            ctrl_credit = sorted({r.get("url") for r in _rows(ctx.control) if r.get("class") == "STRENGTH_URL"})
            state["credit_added_by_mutation"] = [u for u in main_credit if u not in ctrl_credit]
            same = all(o in grammar.SAME_CITATIONS for o in ops)
            state["preserving_changed_classification"] = [] if not same or a == b else [
                f"ops {ops} preserve the answer but changed its classification"]
            state["injection_changed_classification"] = []      # no injection in a protocol case
    if ctx.rerun is not None and ctx.rerun.resp is not None:
        state["nondeterministic"] = [] if _masked(ctx.main) == _masked(ctx.rerun) else [
            "two runs of identical input produced different results (run ids masked)"]
    if ctx.probe is not None:
        p = ctx.probe
        state["probe_status"] = p["status"]
        state["probe_refused"] = p["status"] in (403, 404, 409, 422)
        state["probe_same_as_missing"] = p["same_as_missing"] if p["probe"] not in (
            "hub_list", "hub_start_run_on_other_tenant", "acme_runs_with_zbm_prompt_set",
            "acme_runs_on_zbm_domain") else True
        leaked = [f for f in p["foreign"] if f and any(f in b for b in p["bodies"])]
        state["foreign_echoed"] = leaked
        state["probe_engine_requests_added"] = p["engine_requests_added"]
        state["acme_sees_no_run"] = p["acme_list"] in ([], None)
    if ctx.second is not None:
        s = ctx.second
        state["second_status"] = s["status"]
        first_id = (ctx.main.view or {}).get("citation_run_id")
        if s["kind"].startswith("same_tenant"):
            state["second_refused_running"] = s["status"] == 409 and (s["body"] or {}).get("detail") == \
                "CITATION_RUN_RUNNING"
        elif s["kind"] == "same_request_replayed":
            state["second_refused_running"] = s["status"] in (200, 201) and (s["body"] or {}).get(
                "citation_run_id") == first_id
        if "requests_added" in s:
            state["replay_requests_added"] = s["requests_added"]
            state["runs_recorded"] = s["runs_recorded"]
            if s["kind"] == "same_id_other_body":
                state["idempotent_ok"] = s["status"] == 409 and (s["body"] or {}).get("detail") == "REQUEST_ID_REUSED"
            else:
                state["idempotent_ok"] = s["status"] in (200, 201) and json.dumps(s["body"], sort_keys=True) == \
                    json.dumps(s["first"], sort_keys=True)
        if ctx.main is not None:
            p = ctx.main.providers
            per_run = ctx.main.cfg["prompts"] * ctx.main.cfg["samples"]
            state["one_run_of_requests"] = all(x[0].n <= per_run * int(getattr(
                ctx.main.h.ports.engines[e], "attempts", 1) or 1) for e, x in p.items())
            state["citation_runs_recorded"] = len(ctx.main.h.ledger.of_type("citation_run_recorded"))
    if ctx.extra.get("off") is not None and ctx.main is not None:
        off = ctx.extra["off"]
        state["off_engines_asked"] = [e for e in off if e in ctx.main.providers and ctx.main.providers[e][0].n]
        state["off_reservations"] = [e for e in off if any(
            x["_payload"]["engine"] == e for x in ctx.main.h.ledger.of_type("engine_calls_reserved"))]
        if ctx.extra.get("expect_refused"):
            state["outcome_honest"] = ctx.main.resp.status_code == 422
        elif ctx.extra.get("narrowed"):
            state["outcome_honest"] = (ctx.main.view or {}).get("engines") == ["openai"]
        else:
            out = state.get("outcome")
            state["outcome_honest"] = out != "OK" or all(e not in ctx.main.engines for e in off)
    if ctx.extra.get("cap") is not None and ctx.main is not None:
        e, name, value = ctx.extra["cap"]
        eng = (((_result(ctx.main).get("facts") or {}).get("engines")) or {}).get(e) or {}
        asked = ctx.main.cfg["prompts"] - (eng.get("prompts_not_asked") or 0)
        state["unasked_reported"] = (eng.get("prompts_not_asked") or 0) == 0 or state.get("outcome") != "OK"
        state["prompts_asked"] = asked
    return state


# modes and storm steps that can never yield a counted answer (the judge's own list, not the service's)
FAILING_MODES = frozenset(m for m in PROTOCOL_MODES if m[0] not in "*?")       # "?": no documented expectation
FAILING_STEPS = frozenset(s for s in STORM_STEPS if s != "ok")
