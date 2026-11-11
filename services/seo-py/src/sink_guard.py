"""
The sink guard (sink job A): the service's log and the ledger accept MACHINE VALUES ONLY.

Every log write is one ``SeoService._commit`` and every ledger write is one ``ledger.Recorder.record``. Both pass the
record through ``enforce`` first: each string in it (dict KEYS and values, at any depth) must be a machine value of one
of the closed classes below, or the write is refused before anything is written (fail closed). Prose, e-mail forms,
key-shaped tokens and long digit runs therefore cannot reach the log or the ledger by any route; free text belongs in
the vault (``vault.put_blob``), and the log keeps its ref.

The closed set of classes (``CLASSES``):
  empty        ""
  service_id   ``seo-<prefix>-<40 hex>`` with ``<prefix>`` from ``SERVICE_ID_PREFIXES`` (ledger.derived_id; a vault ref
               ``seo-vlt-`` / a blob ref ``seo-blb-`` + 32 hex + 8 hex check has the same shape)
  scoped_id    ``<name>:<service id | invoice id | uuid | hash>`` (a ledger subject: ``audit:seo-aud-...``), or a
               service id with 1..3 short ``:part`` suffixes (a prompt id: ``<prompt set id>:v2:7``)
  invoice_id   ``fin-inv-<26 Crockford base32>`` (Finance's invoice id)
  request_id   a lower-case UUID (models.REQUEST_ID)
  request_key  ``<op>|<target>|<uuid>`` (SeoService.rk; each ``/`` piece of the target is itself a machine value) or
               ``seo|<kind>`` (an internal commit)
  hash         lower-case hex of 16 / 32 / 40 / 64 characters holding at least one of a-f
  timestamp    ISO-8601 UTC date-time ``YYYY-MM-DDTHH:MM[:SS[.f]]Z`` (clock.iso);   date   ``YYYY-MM-DD``
  number       1..8 digits, optionally signed
  version      ``[v]N(.N){1,3}``, a dated revision ``YYYY-MM-DD.N``, or ``<name>/<N | version | date>``
  boolean      ``true`` / ``false``;             actor     the service's closed actor set
  hostname     a syntactic host name that the strict host check of agents.secret_guard passes (as callum.checked_host)
  code         an enum / reason code: 1..64 characters of ``[A-Za-z0-9_.:-]`` holding a letter
  model_id     an answer-engine model id (sink job C): EXACTLY one of ``MODEL_IDS``, the closed set of ids the engine
               adapters declare (their allowlists; a configured model is always one of them). Nothing is matched by
               shape: any other model string is written by the service as the code ``UNKNOWN_MODEL`` with a keyed HMAC

No class admits whitespace or ``@``; no value is longer than ``MAX_LEN``. A value that is not an id, a hash or a
timestamp must also pass ``secret_guard.check`` (no key prefix, no long random token, no run of 9+ digits).

``violations`` reports normalised paths and closed reason names only: never a value, never a dynamic key.
There is NO exception list (sink job D removed ``LEGACY_PROSE_PATHS``; jobs B-D moved every free-text field into the
vault): any non-machine value refuses its write. ``scrub`` applies the same judgement to the third sink, Python
logging (log_sink.py). Everything is linear in the input: no regular expression is used, only ``str`` methods.
"""

from __future__ import annotations

from typing import Callable, Optional

from agents import secret_guard
from config import KNOWN_CALLERS
from reasons import CODES

MAX_LEN = 160
CODE_MAX = 64
MAX_DEPTH = 64
HASH_LENGTHS = frozenset((16, 32, 40, 64))
SERVICE_ID_PREFIXES = frozenset(("anc", "aud", "blb", "cit", "egr", "evd", "lgi", "pst", "rpt", "sch", "vlt"))
ACTORS = frozenset(KNOWN_CALLERS) | frozenset(("seo", "andre"))
# the service's own enum words and schema keys that NAME a credential class (the strict check's label rule refuses
# them as it would in prose); a closed list of constants, never data
SERVICE_WORDS = frozenset(("SECRET_REDACTED", "NOT_FETCHED_SECRET", "secret_guard", "crawler_token")) | frozenset(
    secret_guard.CHECKS)                 # the strict lane's verdict names (``d_token``): what a ``flags`` entry holds
CLASSES = ("empty", "service_id", "scoped_id", "invoice_id", "request_id", "request_key", "hash", "timestamp", "date", "number",
           "version", "boolean", "actor", "hostname", "code", "model_id")
REASONS = ("type", "key_type", "long", "space", "at", "charset", "shape", "secret", "host", "depth", "kind")
LEDGER = "ledger:"                       # the sink name of a ledger event: ``ledger:<event_type>``
# a hex value of digits only is not a hash anywhere (it reads as a long number), except under these keys, where the
# service itself derives it: the log epoch (the first 16 hex of a line's SHA-256), a raw vault id (128 random bits)
# and a vault repair id (32 hex of a SHA-256). Without this, one epoch in about 1,800 would stop its log for good.
DIGIT_HEX_KEYS = frozenset(("epoch", "vault_id", "repair_id"))

_HEX = "0123456789abcdef"
_DIGITS = "0123456789"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_CHARS = _LOWER + _UPPER + _DIGITS + "_.:-"
_STRUCTURE_CHARS = "|/"                  # only inside a request key / a ``name/N`` version (a declared model
#                                          id is matched whole, before any character rule)
_ALLOWED = _CODE_CHARS + _STRUCTURE_CHARS    # every character a machine value may hold: no whitespace, no "@"
_HOST_CHARS = _LOWER + _DIGITS + "-."
_SNAKE = _LOWER + _DIGITS + "_"
_CACHE_MAX = 65536
_CACHE: dict = {}                        # accepted, reusable values (codes, hosts, versions) -> class
_CACHED = frozenset(("code", "hostname", "version", "actor", "boolean", "timestamp", "date", "number"))
_KEYS: dict = {}                         # dict key -> (path segment, reason or None)


def _declared_model_ids() -> frozenset:
    """Every model id an engine adapter declares in its closed allowlist (config.py only ever configures one of
    them; an adapter refuses any other). Constants of this code base, never data."""
    import anthropic_engine
    import google_engine
    import openai_engine
    import perplexity
    ids: set = set()
    for names in (perplexity.MODELS, perplexity.SONAR_MODELS, openai_engine.MODELS, anthropic_engine.MODELS,
                  google_engine.MODELS):
        ids.update(names)
    return frozenset(ids)


MODEL_IDS: frozenset = _declared_model_ids()             # the ``model_id`` class: exact membership, nothing else
UNKNOWN_MODEL = "UNKNOWN_MODEL"                          # what the service writes for any other model string

OBSERVER: Optional[Callable] = None      # tests: called with (sink, violations, refused paths) per enforced record


class SinkViolation(Exception):
    """A record bound for the log or the ledger holds something that is not a machine value. Carries the sink name and
    the normalised paths only: never a value."""

    def __init__(self, kind: str, paths):
        self.kind = kind if isinstance(kind, str) and _is_name(kind.removeprefix(LEDGER)) else "*"
        self.paths = tuple(sorted(set(paths)))
        super().__init__(f"{self.kind}: {len(self.paths)} path(s) refused: " + ", ".join(self.paths[:8]))


def _only(s: str, chars: str) -> bool:
    return not s.strip(chars)


def _is_name(s: str) -> bool:
    return 0 < len(s) <= CODE_MAX and _only(s, _SNAKE) and s[0] in _LOWER


def _check(value: str) -> Optional[str]:
    """The strict lane's verdict (a check name) or None; every non-id token must pass it. A credential label WORD
    alone (``pass``, ``token``: an enum value, no digit, nothing after it) names no credential, as in a URL path."""
    name = secret_guard.check(value)
    if name == "a_label" and secret_guard.label_only(value):
        return None
    return name


def _host_ok(host: str) -> bool:
    """callum.checked_host's rule: whole and label by label through the strict URL inspection, then the strict check."""
    u = secret_guard.inspect_url("https://" + host + "/")
    return bool(u) and bool(u.get("host")) and u.get("host_kind") is None and _check(host) is None


def _is_date(s: str) -> bool:
    if len(s) != 10 or s[4] != "-" or s[7] != "-" or not _only(s[:4] + s[5:7] + s[8:], _DIGITS):
        return False
    return 1 <= int(s[5:7]) <= 12 and 1 <= int(s[8:]) <= 31


def _is_clock(s: str) -> bool:
    """``HH:MM[:SS[.f{1,9}]]``."""
    if len(s) < 5 or s[2] != ":" or not _only(s[:2] + s[3:5], _DIGITS) or int(s[:2]) > 23 or int(s[3:5]) > 59:
        return False
    rest = s[5:]
    if not rest:
        return True
    if len(rest) < 3 or rest[0] != ":" or not _only(rest[1:3], _DIGITS) or int(rest[1:3]) > 60:
        return False
    frac = rest[3:]
    return not frac or (frac[0] == "." and 1 < len(frac) <= 10 and _only(frac[1:], _DIGITS))


def _is_timestamp(s: str) -> bool:
    if len(s) < 17 or len(s) > 40 or s[10] != "T" or not _is_date(s[:10]):
        return False
    return s[-1] == "Z" and _is_clock(s[11:-1])


def _is_uuid(s: str) -> bool:
    return (len(s) == 36 and s[8] == "-" and s[13] == "-" and s[18] == "-" and s[23] == "-"
            and _only(s[:8] + s[9:13] + s[14:18] + s[19:23] + s[24:], _HEX))


def _is_service_id(s: str) -> bool:
    return len(s) == 48 and s[:4] == "seo-" and s[7] == "-" and s[4:7] in SERVICE_ID_PREFIXES and _only(s[8:], _HEX)


def _is_invoice_id(s: str) -> bool:
    return len(s) == 34 and s[:8] == "fin-inv-" and _only(s[8:], _CROCKFORD)


def _is_hash(s: str, digits_ok: bool = False) -> bool:
    if len(s) not in HASH_LENGTHS or not _only(s, _HEX):
        return False
    return digits_ok or not _only(s, _DIGITS)


def _is_version(s: str) -> bool:
    """``[v]N.N[.N[.N]]`` or a dated revision ``YYYY-MM-DD.N``."""
    if len(s) > 10 and s[10] == "." and _is_date(s[:10]):
        return 0 < len(s) - 11 <= 4 and _only(s[11:], _DIGITS)
    body = s[1:] if s[0] == "v" else s
    parts = body.split(".")
    return 2 <= len(parts) <= 4 and all(0 < len(p) <= 8 and _only(p, _DIGITS) for p in parts)


def _is_hostname(s: str) -> bool:
    if "." not in s or not _only(s, _HOST_CHARS):
        return False
    labels = s.split(".")
    if not all(0 < len(x) <= 63 and x[0] != "-" and x[-1] != "-" for x in labels):
        return False
    return len(labels[-1]) >= 2 and _only(labels[-1], _LOWER)


def _plain(s: str) -> tuple:
    """(class, reason) of a value holding only code characters (no ``|``, no ``/``)."""
    n = len(s)
    if n == 48 and _is_service_id(s):
        return "service_id", None
    if n in HASH_LENGTHS and _is_hash(s):
        return "hash", None
    if n == 36 and _is_uuid(s):
        return "request_id", None
    if n == 34 and _is_invoice_id(s):
        return "invoice_id", None
    lead = s[0]
    if lead in _DIGITS or lead == "-":
        if _is_date(s):
            return "date", None
        if _is_timestamp(s):
            return "timestamp", None
        digits = s[1:] if lead == "-" else s
        if 0 < len(digits) <= 8 and _only(digits, _DIGITS):
            return "number", None
    if s in ("true", "false"):
        return "boolean", None
    if s in ACTORS:
        return "actor", None
    if (lead in _DIGITS or lead == "v") and _is_version(s):
        return ("version", None) if _check(s) is None else (None, "secret")
    if s in CODES or s in SERVICE_WORDS:
        return "code", None                                 # the service's own closed catalogues
    if _is_hostname(s):
        return ("hostname", None) if _host_ok(s) else (None, "host")
    if ":" in s:
        name, _, rest = s.partition(":")
        if _is_name(name) and (_is_service_id(rest) or _is_invoice_id(rest) or _is_uuid(rest) or _is_hash(rest)):
            return "scoped_id", None
        if len(s) > 49 and s[48] == ":" and _is_service_id(s[:48]):
            parts = s[49:].split(":")                       # a part of a service object: ``<prompt set id>:v2:7``
            if len(parts) <= 3 and all(0 < len(x) <= 8 and _only(x, _LOWER + _DIGITS) for x in parts):
                return "scoped_id", None
    if len(s) > CODE_MAX or _only(s, _DIGITS + "_.:-"):
        return None, "shape"                                # too long for a code, or no letter in it
    return ("code", None) if _check(s) is None else (None, "secret")


def _request_key(s: str) -> bool:
    parts = s.split("|")
    if len(parts) == 2:
        return parts[0] == "seo" and _is_name(parts[1])
    if len(parts) != 3 or not _is_name(parts[0]) or not _is_uuid(parts[2]) or not parts[1]:
        return False
    return all(piece and _plain(piece)[0] is not None for piece in parts[1].split("/"))


def _named_version(s: str) -> bool:
    """``<name>/<N>``, ``<name>/<version>`` or ``<name>/<date>``: a rules / report / renderer version."""
    name, _, ver = s.partition("/")
    if not (0 < len(name) <= CODE_MAX and name[0] in _LOWER and _only(name, _LOWER + _DIGITS + "._-")):
        return False
    if not ((0 < len(ver) <= 4 and _only(ver, _DIGITS)) or _is_date(ver) or (ver[:1] in _DIGITS and _is_version(ver))):
        return False
    return _check(s) is None


def _judge(s: str) -> tuple:
    """(class, None) for a machine value, (None, reason) otherwise. Linear in ``len(s)`` (bounded by ``MAX_LEN``)."""
    n = len(s)
    if n == 0:
        return "empty", None
    if n > MAX_LEN:
        return None, "long"
    if s in MODEL_IDS:
        return "model_id", None                             # a declared model id, compared exactly
    if s.strip(_ALLOWED):
        if len(s.split()) != 1 or s != s.strip() or not s.isprintable():
            return None, "space"
        return None, "at" if "@" in s else "charset"
    if "|" in s:
        return ("request_key", None) if _request_key(s) else (None, "shape")
    if "/" in s:
        cls, reason = ("version", None) if _named_version(s) else (None, "shape")
    else:
        cls, reason = _plain(s)
    if cls in _CACHED:                                      # reusable values: the strict check is not run twice
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[s] = cls
    return cls, reason


def classify(value) -> Optional[str]:
    """The class of a machine value, or None (not a string, or not a machine value)."""
    if type(value) is not str:
        return None
    return _CACHE.get(value) or _judge(value)[0]


def _key(k) -> tuple:
    """(path segment, reason or None) for a dict key. A key is named in a path only when it is a short lower-snake
    schema name (digits read ``N``); any other key is ``*``."""
    if type(k) is not str:
        return "*", "key_type"
    reason = None if k in _CACHE else _judge(k)[1]
    seg = "*"
    if 0 < len(k) <= 40 and k[0] in _LOWER + "_" and _only(k, _SNAKE):
        seg = "".join("N" if c in _DIGITS else c for c in k)
        while "NN" in seg:
            seg = seg.replace("NN", "N")
    if len(_KEYS) >= _CACHE_MAX:
        _KEYS.clear()
    _KEYS[k] = (seg, reason)
    return seg, reason


_SCALARS = frozenset((int, float, bool, type(None)))


def violations(kind: str, data) -> list:
    """Every (normalised path, reason) in ``data`` that is not a machine value: dict keys and values, lists and tuples
    are walked; int / float / bool / None pass; bytes and any other object are violations. Sorted, no duplicates."""
    out = set()
    if not isinstance(kind, str) or not _is_name(kind.removeprefix(LEDGER)):
        out.add(("", "kind"))
    stack = [(data, "", 0)]
    cache, keys, hexa, digits = _CACHE, _KEYS, _HEX, _DIGITS
    while stack:
        obj, path, depth = stack.pop()
        if isinstance(obj, dict):
            if depth >= MAX_DEPTH:
                out.add((path, "depth"))
                continue
            for k, v in obj.items():
                if type(k) is str:
                    seg, reason = keys.get(k) or _key(k)
                else:
                    seg, reason = "*", "key_type"
                if reason is not None:
                    out.add((path + "/{key}", reason))
                tv = type(v)
                if tv is str:
                    if v in cache:
                        continue
                    if len(v) == 64 and not v.strip(hexa) and v.strip(digits):
                        continue                            # the commonest unique value: a SHA-256 / HMAC hex
                    reason = _judge(v)[1]
                    if reason is not None and not (k in DIGIT_HEX_KEYS and _is_hash(v, True)):
                        out.add((path + "/" + seg, reason))
                elif tv in _SCALARS:
                    continue
                else:
                    stack.append((v, path + "/" + seg, depth + 1))
        elif isinstance(obj, (list, tuple)):
            if depth >= MAX_DEPTH:
                out.add((path, "depth"))
                continue
            sub = path + "[]"
            for v in obj:
                tv = type(v)
                if tv is str:
                    if v in cache:
                        continue
                    if len(v) == 64 and not v.strip(hexa) and v.strip(digits):
                        continue
                    reason = _judge(v)[1]
                    if reason is not None:
                        out.add((sub, reason))
                elif tv in _SCALARS:
                    continue
                else:
                    stack.append((v, sub, depth + 1))
        elif isinstance(obj, str):
            reason = _judge(str(obj))[1]
            if reason is not None or type(obj) is not str:
                out.add((path, reason or "type"))
        elif type(obj) not in _SCALARS:
            out.add((path, "type"))
    return sorted(out)


def refused(kind: str, data) -> list:
    """The violating paths of ``data`` (sorted). There are no exceptions (sink job D: the legacy list is gone)."""
    found = violations(kind, data)
    bad = sorted({p for p, _ in found})
    if OBSERVER is not None:
        OBSERVER(kind, found, bad)
    return bad


def enforce(kind: str, data) -> None:
    """Raise ``SinkViolation`` when ``data`` (bound for sink ``kind``) holds a non-machine value. Called before
    anything is written."""
    bad = refused(kind, data)
    if bad:
        raise SinkViolation(kind, bad)


WITHHELD = "WITHHELD"                    # what the logging sink writes in place of a non-machine argument


def scrub(value):
    """A value as the Python logging sink may print it (sink job D): a machine value as it is; an int / float /
    bool / None as it is; anything else (prose, an e-mail form, a key shape, an object whose text is not a machine
    value) as the code ``WITHHELD``. Never raises."""
    try:
        if value is None or type(value) in (int, float, bool):
            return value
        if type(value) is str:
            return value if (value in _CACHE or _judge(value)[1] is None) else WITHHELD
        return WITHHELD
    except Exception:  # noqa: BLE001 - fail closed
        return WITHHELD
