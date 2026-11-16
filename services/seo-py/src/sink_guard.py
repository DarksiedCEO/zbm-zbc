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
  finance_id   any other id Finance (31) issues: ``fin-<prefix>-<26 Crockford base32>`` (finance-py ``service.rid``) or
               ``fin-<prefix>-<40 hex>`` (finance-py ``ledger.derived_id``), ``<prefix>`` = a lower-case letter and up
               to 7 lower-case letters / digits (finance-py ``models.OWN_ID_RE``)
  ruling_id    ``cmp-rul-<26 lower-case base32>`` (Compliance's ruling id: compliance-py ``service._b32``). Any other
               ruling id shape is never written: the service writes the code ``RULING_ID_HASHED`` with a keyed HMAC
  jurisdiction ``US-`` + two capital letters (seo-py ``compliance_client.JURISDICTION``; the territories ``PR``, ``GU``,
               ``VI``, ``AS``, ``MP`` are codes). Closed by shape: ``US-PW`` reads as a credential label otherwise
  key_id       a vault key id, ONLY as the value of a ``key_id`` field and ONLY when it equals a key id this process
               has configured (``register_key_ids``: the current and the previous vault key id; a closed set, as the
               model ids). A key id is refused at start (``key_id_refusal``) when it is key-shaped
  request_id   a lower-case UUID (models.REQUEST_ID)
  request_key  ``<op>|<target>|<uuid>`` (SeoService.rk; each ``/`` piece of the target is itself a machine value) or
               ``seo|<kind>`` (an internal commit)
  hash         lower-case hex of 16 / 32 / 40 / 64 characters holding at least one of a-f
  timestamp    ISO-8601 UTC date-time ``YYYY-MM-DDTHH:MM[:SS[.f]]Z`` (clock.iso);   date   ``YYYY-MM-DD``
  number       1..8 digits, optionally signed
  amount       a US-dollar amount as the service writes it: 1..6 digits, ``.``, exactly 6 digits (``str`` of a
               ``Decimal`` quantised to a millionth: ``spend_usd``, ``reserved_usd``). Review 2b26d980: a run that
               spent or reserved 100.000000 or more holds 9+ digits and was refused as a number
  version      ``[v]N(.N){1,3}``, a dated revision ``YYYY-MM-DD.N``, or ``<name>/<N | version | date>``
  boolean      ``true`` / ``false``;             actor     the service's closed actor set
  hostname     a syntactically valid host name (labels of 1..63, 253 in all, a letters-only TLD of 2..24 or a
               punycode TLD, every ``xn--`` label decoding to letters) that ``host_refusal`` passes: no label has a
               strong-key prefix shape, 32+ hex characters or a 24+ character letters-and-digits token; no run of
               9+ digits (joined across ``-`` / ``.`` between all-digit pieces), under 12 digits in all-digit pieces.
               A credential WORD in a label is allowed (``1password.com``): a host name carries no value after it,
               except a letters-and-digits piece of 6+ right after the word, which is refused. Review 98b76cde:
               the rule holds NO e-mail form (``finance.yahoo.com`` and ``look.at.example.org`` are host names),
               and a host this rule refuses is still a host name when the release before the rule (2b26d980)
               took it (``_legacy_host_ok``): the rule only ever ADDS host names, it never takes one away
  code         an enum / reason code: 1..64 characters of ``[A-Za-z0-9_.:-]`` holding a letter
  model_id     an answer-engine model id (sink job C): EXACTLY one of ``MODEL_IDS``, the closed set of ids the engine
               adapters declare (their allowlists; a configured model is always one of them). Nothing is matched by
               shape: any other model string is written by the service as the code ``UNKNOWN_MODEL`` with a keyed HMAC

No class admits whitespace or ``@``; no value is longer than ``MAX_LEN``. A value that is not an id, a hash, a
timestamp or a host name must also pass ``secret_guard.check`` (no key prefix, no long random token, no run of 9+
digits). A float that is not finite (``nan``, ``inf``) is refused: JSON cannot carry it.

``violations`` reports normalised paths and closed reason names only: never a value, never a dynamic key.
There is NO exception list (sink job D removed ``LEGACY_PROSE_PATHS``; jobs B-D moved every free-text field into the
vault): any non-machine value refuses its write. ``scrub`` applies the same judgement to the third sink, Python
logging (log_sink.py). Everything is linear in the input: only ``str`` methods here; the strict lane's check and
its strong-key prefix table (agents.secret_guard) are asked about one value or one host label at a time.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from typing import Callable, Optional

from agents import secret_guard
from config import KNOWN_CALLERS, TENANT_ID
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
CLASSES = ("empty", "service_id", "scoped_id", "invoice_id", "finance_id", "ruling_id", "jurisdiction", "key_id", "request_id",
           "amount",
           "request_key", "hash", "timestamp", "date", "number", "version", "boolean", "actor", "hostname", "code",
           "model_id")
REASONS = ("type", "key_type", "long", "space", "at", "charset", "shape", "secret", "host", "depth", "kind", "float",
           "untyped", "field_type")     # the last two: core-spec/2 T-3 / T-5 per-field typing (sink_fields, card C2-2c)
LEDGER = "ledger:"                       # the sink name of a ledger event: ``ledger:<event_type>``
# a hex value of digits only is not a hash anywhere (it reads as a long number), except under these keys, where the
# service itself derives it: the log epoch (the first 16 hex of a line's SHA-256), a raw vault id (128 random bits)
# and a vault repair id (32 hex of a SHA-256). Without this, one epoch in about 1,800 would stop its log for good.
# Review 2b26d980: ``key_fp`` too (the first 16 hex of the log hash key's SHA-256: one deployment key in about 1,800
# has a digits-only fingerprint and could never create a log ingest).
DIGIT_HEX_KEYS = frozenset(("epoch", "vault_id", "repair_id", "key_fp"))
KEY_ID_KEYS = frozenset(("key_id",))     # the only fields a vault key id is written under (the operator-text stub)

_HEX = "0123456789abcdef"
_DIGITS = "0123456789"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_BASE32 = "abcdefghijklmnopqrstuvwxyz234567"   # base64.b32encode(...).lower(): what compliance-py's ``_b32`` emits
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


# core-spec/2 T-1 / T-5, step (3) of lead ruling 21:25 PT (card C2-6b; F6-1, ruling 22:00 PT): the CLOSED shape rule
# of the `code` strict check, for an open code (no closed constant). A code is split at its separators [_.:-]; it is
# refused (reason ``shape``) when
#   (1) it holds upper AND lower letters AND a digit (a code is written in ONE case style; digits stay in snake /
#       UPPER codes: ``api_401``, ``HTTP_404``, ``sha256``);
#   (2) a piece is a run of 6+ digits that is not a compact calendar date YYYYMMDD (a number or an id fragment:
#       those have their own T-1 classes; a dated name ``web_search_20250305`` keeps its date);
#   (3) a piece is a run of 6+ lower hex characters (an id / hash fragment: ``hash`` / ``scoped_id`` hold those);
#   (4) a piece mixing upper and lower case is not a camel word: lower or Capitalized words of 2+ letters and
#       acronym runs of 2+ capitals (``rateLimitExceeded``, ``OpenAIEngine``, ``ChatGPT``, ``ValueError``);
#   (5) a single-case piece with digits has them in one run, at its end (``http2``, ``seo_02``) or at its start
#       before at most 2 letters (``2nd``, ``5xx``, ``0x``).
# It runs after the strict check (a credential keeps reason ``secret``) and only on the code alphabet (the charset
# gate keeps "@" / whitespace). The service's own catalogues (``CODES``, ``SERVICE_WORDS``) and enum fields (exact
# membership) are not judged by it.
# No threshold, no score: each rule is a grammar. ADR text: C2-19.
_CODE_SEPARATORS = re.compile(r"[_.:\-]")
_CAMEL_WORD = re.compile(r"(?:[a-z]{2,}|[A-Z][a-z]+|[A-Z]{2,}(?=[A-Z][a-z]|$))"
                         r"(?:[A-Z][a-z]+|[A-Z]{2,}(?=[A-Z][a-z]|$))*")
_DIGIT_UNIT = re.compile(r"[a-z]+[0-9]+|[A-Z]+[0-9]+|[0-9]+[a-z]{1,2}|[0-9]+[A-Z]{1,2}")
_HEX_RUN = re.compile(r"[0-9a-f]{6,}")
_DATE8 = re.compile(r"(?:19|20)[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])")


def code_shape_refusal(s: str) -> Optional[str]:
    """The rule (1)-(5) above that refuses ``s`` as an open code, or None. Linear in ``len(s)`` (<= ``CODE_MAX``)."""
    if s.strip(_CODE_CHARS):
        return None                                         # not in the code alphabet: the charset gate's verdict
    lower, upper = s != s.upper(), s != s.lower()
    if lower and upper and not _only(s, _LOWER + _UPPER + "_.:-"):
        return "mixed_case_digits"
    for piece in _CODE_SEPARATORS.split(s):
        if not piece:
            continue
        if piece.isdigit():
            if len(piece) >= 6 and not _DATE8.fullmatch(piece):
                return "digit_run"
            continue
        if _HEX_RUN.fullmatch(piece):    # C2-6f: narrowing (ruling 01:20 PT (B)) REVERTED - T-5 rose (HANDOFF 2bh)
            return "hex_run"
        if piece != piece.upper() and piece != piece.lower():
            if not _CAMEL_WORD.fullmatch(piece):
                return "case"
        elif not _only(piece, _LOWER + _UPPER) and not _DIGIT_UNIT.fullmatch(piece):
            return "inner_digits"
    return None


# ------------------------------------------------------------------------------------------------ host names
# Review 2b26d980 SHOULD 1: real host names (punycode labels, platform sub-domains with a hex label, hosts whose name
# holds a credential word) are machine values. The rule is precise and measured (tests/test_review_2b26d980.py):
HOST_MAX = 253
HOST_TLD_MAX = 24                        # the longest public TLD has 24 characters
HOST_HEX_MIN = 32                        # a label holding 32+ hex characters in a row is a key
HOST_TOKEN_MIN = 24                      # a label of 24+ letters and digits ...
HOST_TOKEN_CHANGES = 2                   # ... with 2+ letter<->digit changes is a random token
HOST_DIGIT_RUN = 9                       # 9+ digits in a row (all-digit pieces joined across ``-`` / ``.``)
HOST_DIGITS_TOTAL = 12                   # digits in all-digit pieces of the whole host
# a platform hash label (``1a2b3c4d5e6f.ngrok.io``): all hex, 12..31 characters; a digit run of at most 12 inside
# it is part of the hash (one hex label in about 30 has a run of 9+) ONLY when the piece holds 4+ hex letters
# (review 98b76cde MUST 3: a telephone number or an SSN with one to three hex letters attached, before it, after
# it or around it, is a number, not a hash; with 2+ letters, as before, 35..100% of those passed). Measured in
# tests/test_review_98b76cde.py and ADR 0051: the cost is hash labels with a 9+ digit run and under 4 letters
HOST_HASH_LABEL = (12, 31)
HOST_HASH_LETTERS = 4
HOST_HASH_RUN_MAX = 12
HOST_VALUE_MIN = 6                       # a letters-and-digits piece this long right after a credential word
# ``password-is-<value>``: the words that may stand between a credential word and its value (review 98b76cde)
HOST_VALUE_LINKS = frozenset(("is", "was", "are", "equals"))
HOST_REASONS = ("syntax", "tld", "idna", "prefix", "hex", "token", "digits", "label_value")
_HOST_CACHE: dict = {}                   # host -> reason or None (bounded; a refusal is remembered too)
_LEGACY_CACHE: dict = {}                 # host the rule refuses -> the 2b26d980 verdict (bounded)
_LABEL_PAIRS = frozenset(w for w in secret_guard._LABEL_WORDS if len(w) >= 6)


def _idna_label_ok(label: str) -> bool:
    """An ``xn--`` label that decodes (punycode) to a lower-case label of letters, marks, digits and hyphens with at
    least one non-ASCII letter, and encodes back to itself."""
    body = label[4:]
    if not body:
        return False
    try:
        text = body.encode("ascii").decode("punycode")
        if not text or text.isascii() or text != text.lower() or text.encode("punycode").decode("ascii") != body:
            return False
    except (UnicodeError, ValueError):
        return False
    letters = 0
    for c in text:
        if c == "-" or c in _LOWER or c in _DIGITS:
            continue
        cat = unicodedata.category(c)
        if cat[0] == "L":
            letters += 1
        elif cat not in ("Mn", "Mc", "Nd"):
            return False
    return letters > 0


def _hex_run(j: str) -> int:
    """The longest run of hex characters in ``j`` that holds a digit (a hex key of 32+ characters with no digit at
    all does not occur: 6/16 to the 32nd power; a long name of the letters a-f does)."""
    best = run = 0
    digit = False
    for c in j:
        if c in _HEX:
            run += 1
            digit = digit or c in _DIGITS
            if digit and run > best:
                best = run
        else:
            run, digit = 0, False
    return best


def _changes(j: str) -> int:
    """Letter<->digit changes along ``j`` (letters and digits only)."""
    n, prev = 0, None
    for c in j:
        d = c in _DIGITS
        if prev is not None and d != prev:
            n += 1
        prev = d
    return n


def _valueish(piece: str) -> bool:
    return len(piece) >= HOST_VALUE_MIN and bool(piece.strip(_DIGITS)) and bool(piece.strip(_LOWER))


def _label_word(pieces: list, i: int) -> bool:
    """Piece ``i`` of a label (split at ``-``) is a credential label word, alone or joined with the piece before it
    (``api-key``, ``access-token``)."""
    words = secret_guard._LABEL_WORDS
    return pieces[i] in words or (i > 0 and pieces[i - 1] + pieces[i] in _LABEL_PAIRS)


def _hash_piece(piece: str) -> bool:
    """A platform hash piece, whose digit run of up to 12 is part of the hash: all hex, 12..31 characters, 4+ hex
    letters."""
    return (HOST_HASH_LABEL[0] <= len(piece) <= HOST_HASH_LABEL[1] and not piece.strip(_HEX)
            and sum(1 for c in piece if c in "abcdef") >= HOST_HASH_LETTERS)


def _host_digits(labels: list) -> bool:
    """True when the host's digits read as a number: a run of 9+ (all-digit pieces joined across ``-`` and ``.``;
    a run inside one piece), or 12+ digits in all-digit pieces. A platform hash piece (``_hash_piece``) keeps a run
    of at most 12."""
    total = chain = 0
    for label in labels:
        for piece in label.split("-"):
            if piece and not piece.strip(_DIGITS):
                total += len(piece)
                chain += len(piece)
                if chain >= HOST_DIGIT_RUN:
                    return True
                continue
            chain = 0
            run = best = 0
            for c in piece:
                run = run + 1 if c in _DIGITS else 0
                if run > best:
                    best = run
            if best >= HOST_DIGIT_RUN and not (best <= HOST_HASH_RUN_MAX and _hash_piece(piece)):
                return True
    return total >= HOST_DIGITS_TOTAL


def _host_judge(s: str, labels: list) -> Optional[str]:
    for label in labels:
        if label[:4] == "xn--":
            if not _idna_label_ok(label):
                return "idna"
        j = label.replace("-", "")
        up = label.upper()
        if (secret_guard._prefix_found(label) or secret_guard._prefix_found(up)
                or secret_guard._AWS_JOINED.search(j.upper()) or secret_guard._JOINED_PREFIX.match(j)):
            return "prefix"
        if len(j) >= HOST_HEX_MIN and _hex_run(j) >= HOST_HEX_MIN:
            return "hex"
        if label[:4] != "xn--":
            # the pieces that are not plain words (letters only), read together: a token split by hyphens too
            t = "".join(x for x in label.split("-") if x.strip(_LOWER))
            if len(t) >= HOST_TOKEN_MIN and t.strip(_DIGITS) and _changes(t) >= HOST_TOKEN_CHANGES:
                return "token"
    # a token split over two adjacent labels (review 98b76cde): their non-word pieces read together
    prev = ""
    for label in labels[:-1]:
        t = "" if label[:4] == "xn--" else "".join(x for x in label.split("-") if x.strip(_LOWER))
        if prev and t and len(prev) + len(t) >= HOST_TOKEN_MIN and (prev + t).strip(_DIGITS) \
                and _changes(prev + t) >= HOST_TOKEN_CHANGES:
            return "token"
        prev = t
    # a key split over labels: all-hex labels read together; a plain word label between two of them does not end
    # the key (review 98b76cde: ``<16 hex>.shop.<16 hex>``)
    run, digit = 0, False
    for label in labels[:-1]:
        j = label.replace("-", "")
        if j.strip(_HEX):
            if j.strip(_LOWER):
                run, digit = 0, False
            continue
        run += len(j)
        digit = digit or bool(j.strip("abcdef"))
        if digit and run >= HOST_HEX_MIN:
            return "hex"
    if _host_digits(labels):
        return "digits"
    n = len(labels)
    # the value of a credential label: a letters-and-digits piece of 6+ right after the word (or after the word and
    # ``is`` / ``was`` / ``are`` / ``equals``), in the same label (``password-hunter2``) or as the next label when
    # that is not the registrable name (``token.abc123.site.tld``)
    for i, label in enumerate(labels):
        pieces = label.split("-")
        for k in range(len(pieces) - 1):
            if not _label_word(pieces, k):
                continue
            m = k + 1
            if m < len(pieces) - 1 and pieces[m] in HOST_VALUE_LINKS:
                m += 1                                      # ``password-is-<value>`` (review 98b76cde)
            if _valueish(pieces[m]):
                return "label_value"
        if i + 1 < n - 2 and _label_word(pieces, len(pieces) - 1) and all(
                p in secret_guard._LABEL_WORDS or p in secret_guard.LABEL_ACTION_WORDS for p in pieces) \
                and _valueish(labels[i + 1].replace("-", "")):
            return "label_value"
    return None


def host_refusal(s) -> Optional[str]:
    """Why ``s`` is not an accepted host name (a name from ``HOST_REASONS``), or None. Never raises; linear in
    ``len(s)`` (at most ``HOST_MAX``)."""
    if type(s) is not str or not 4 <= len(s) <= HOST_MAX or "." not in s or s.strip(_HOST_CHARS):
        return "syntax"
    got = _HOST_CACHE.get(s, "")
    if got != "":
        return got
    labels = s.split(".")
    if not all(0 < len(x) <= 63 and x[0] != "-" and x[-1] != "-" for x in labels):
        return "syntax"
    tld = labels[-1]
    if not ((2 <= len(tld) <= HOST_TLD_MAX and not tld.strip(_LOWER)) or (tld[:4] == "xn--" and _idna_label_ok(tld))):
        return "tld"
    try:
        reason = _host_judge(s, labels)
    except Exception:  # noqa: BLE001 - fail closed
        reason = "syntax"
    if len(_HOST_CACHE) >= _CACHE_MAX:
        _HOST_CACHE.clear()
    _HOST_CACHE[s] = reason
    return reason


def _legacy_host_ok(host: str) -> bool:
    """What release 2b26d980 (before the host rule) decided for a host-shaped value, kept so that the rule never
    refuses a host that release took (review 98b76cde, P1): the strict URL inspection of ``https://<host>/`` and
    the strict check of the whole host; a value whose last label is an ``xn--`` label was not a host name there
    and was judged as a code. In both, the strict lane falls back to its own 2b26d980 checks for a host the rule
    refuses (``secret_guard.check`` / ``_inspect``), so nothing here asks the rule again."""
    if host.rsplit(".", 1)[-1][:4] == "xn--":
        return len(host) <= CODE_MAX and _check(host) is None
    u = secret_guard.inspect_url("https://" + host + "/")
    return bool(u) and bool(u.get("host")) and u.get("host_kind") is None and _check(host) is None


def _host_ok(host: str) -> bool:
    """The hostname class: a host the rule takes, or one the release before the rule took."""
    if host_refusal(host) is None:
        return True
    got = _LEGACY_CACHE.get(host)
    if got is None:
        try:
            got = _legacy_host_ok(host)
        except Exception:  # noqa: BLE001 - fail closed
            got = False
        if len(_LEGACY_CACHE) >= _CACHE_MAX:
            _LEGACY_CACHE.clear()
        _LEGACY_CACHE[host] = got
    return got


def host_shaped(s) -> bool:
    """``s`` has the shape of a host name (so it is judged by ``host_refusal`` and by nothing else)."""
    return type(s) is str and 0 < len(s) <= MAX_LEN and _is_hostname(s)


def _host_like(s) -> bool:
    return type(s) is str and 4 <= len(s) <= HOST_MAX and _is_hostname(s)


# the strict lane and the client-view lane judge a host name by this rule too (agents.secret_guard.host_verdict)
secret_guard.HOST_RULE, secret_guard.HOST_LIKE = host_refusal, _host_like


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


def _is_finance_id(s: str) -> bool:
    """``fin-<prefix>-<26 Crockford base32 | 40 hex>``: every id Finance (31) issues (finance-py ``models.OWN_ID_RE``:
    ``service.rid`` and ``ledger.derived_id``)."""
    if s[:4] != "fin-":
        return False
    prefix, sep, body = s[4:].partition("-")
    if not sep or not 0 < len(prefix) <= 8 or prefix[0] not in _LOWER or not _only(prefix, _LOWER + _DIGITS):
        return False
    return (len(body) == 26 and _only(body, _CROCKFORD)) or (len(body) == 40 and _only(body, _HEX))


def _is_ruling_id(s: str) -> bool:
    """``cmp-rul-<26 lower-case base32>``: exactly what Compliance (38) issues (compliance-py ``service._b32``)."""
    return len(s) == 34 and s[:8] == "cmp-rul-" and _only(s[8:], _BASE32)


def _is_jurisdiction(s: str) -> bool:
    """``US-`` + two capital letters (seo-py ``compliance_client.JURISDICTION``)."""
    return len(s) == 5 and s[:3] == "US-" and s[3] in _UPPER and s[4] in _UPPER


def _is_amount(s: str) -> bool:
    """``N{1,6}.NNNNNN``: US dollars to a millionth, as ``str`` of the quantised ``Decimal`` the service writes."""
    return 8 <= len(s) <= 13 and s[-7] == "." and _only(s[:-7] + s[-6:], _DIGITS)


RULING_ID_HASHED = "RULING_ID_HASHED"    # what the service writes for a ruling id of any other shape (with its HMAC)
KEY_ID_MAX = 32                          # vault.KEY_ID: ``[a-z0-9][a-z0-9-]{0,31}``
KEY_ID_DIGITS_MAX = 12                   # ``k-20261110-0001``: a date and a serial; more digits read as a number
KEY_IDS: set = set()                     # the vault key ids this process has configured (``register_key_ids``)


def key_id_refusal(kid) -> Optional[str]:
    """Why a configured vault key id cannot be written to the log (a closed reason name), or None. A key id is an
    operator's NAME for a key (``2026-11``, ``k-20261110-01``, ``prod-2``): date stamps are fine; key material,
    a strong-key prefix, a long random token or 13+ digits are not."""
    if type(kid) is not str or not 0 < len(kid) <= KEY_ID_MAX or kid.strip(_LOWER + _DIGITS + "-") or kid[0] == "-":
        return "shape"
    j = kid.replace("-", "")
    if sum(1 for c in j if c in _DIGITS) > KEY_ID_DIGITS_MAX:
        return "digits"
    if len(j) >= 16 and not j.strip(_HEX) and j.strip(_DIGITS):
        return "hex"                                        # 16+ hex characters with a hex letter: key material
    if len(j) >= 16 and j.strip(_DIGITS) and _changes(j) >= HOST_TOKEN_CHANGES:
        return "token"
    if secret_guard.check(kid, frozenset(("e_digits",))) is not None:
        return "secret"                                     # a credential label, a prefix, a token (digits waived)
    return None


def register_key_ids(ids) -> None:
    """The service's start: the configured vault key ids (current, previous) join the closed ``key_id`` set. Raises
    ``ValueError`` naming the reason only (never the id) when one cannot be written to the log."""
    wanted = [k for k in ids if k is not None]
    for kid in wanted:
        reason = key_id_refusal(kid)
        if reason is not None:
            raise ValueError(f"a vault key id cannot be written to the log (reason: {reason}); name the key with "
                             "lower-case letters, digits and hyphens, at most 12 digits, not key material")
    KEY_IDS.update(wanted)


def _is_hash(s: str, digits_ok: bool = False) -> bool:
    if len(s) not in HASH_LENGTHS or not _only(s, _HEX):
        return False
    return digits_ok or not _only(s, _DIGITS)


def _is_version(s: str) -> bool:
    """``[v]N.N[.N[.N]]`` or a dated revision ``YYYY-MM-DD.N``."""
    if not s:       # C2-6c (ruling 22:50 PT F6b-3): "name/" (empty version) is no version -> refused, never IndexError
        return False
    if len(s) > 10 and s[10] == "." and _is_date(s[:10]):
        return 0 < len(s) - 11 <= 4 and _only(s[11:], _DIGITS)
    body = s[1:] if s[0] == "v" else s
    parts = body.split(".")
    return 2 <= len(parts) <= 4 and all(0 < len(p) <= 8 and _only(p, _DIGITS) for p in parts)


def _is_hostname(s: str) -> bool:
    """Host-name SHAPED: dotted labels of host characters ending in a TLD (letters, or a punycode label). A value of
    this shape is a host name or nothing: one ``host_refusal`` refuses is never read as a code instead."""
    if "." not in s or not _only(s, _HOST_CHARS):
        return False
    labels = s.split(".")
    if not all(0 < len(x) <= 63 and x[0] != "-" and x[-1] != "-" for x in labels):
        return False
    tld = labels[-1]
    return (len(tld) >= 2 and _only(tld, _LOWER)) or tld[:4] == "xn--"


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
    if n == 34 and _is_ruling_id(s):
        return "ruling_id", None
    if s[:4] == "fin-" and _is_finance_id(s):
        return "finance_id", None
    if n == 5 and _is_jurisdiction(s):
        return "jurisdiction", None
    lead = s[0]
    if lead in _DIGITS or lead == "-":
        if _is_date(s):
            return "date", None
        if _is_timestamp(s):
            return "timestamp", None
        digits = s[1:] if lead == "-" else s
        if 0 < len(digits) <= 8 and _only(digits, _DIGITS):
            return "number", None
        if 8 <= n <= 13 and _is_amount(s):
            return "amount", None
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
        if _is_name(name) and (_is_service_id(rest) or _is_finance_id(rest) or _is_ruling_id(rest)
                               or _is_uuid(rest) or _is_hash(rest)):
            return "scoped_id", None
        if len(s) > 49 and s[48] == ":" and _is_service_id(s[:48]):
            parts = s[49:].split(":")                       # a part of a service object: ``<prompt set id>:v2:7``
            if len(parts) <= 3 and all(0 < len(x) <= 8 and _only(x, _LOWER + _DIGITS) for x in parts):
                return "scoped_id", None
    if len(s) > CODE_MAX or _only(s, _DIGITS + "_.:-"):
        return None, "shape"                                # too long for a code, or no letter in it
    if _check(s) is not None:
        return None, "secret"
    if code_shape_refusal(s) is not None:
        return None, "shape"                                # C2-6b: the open-code shape rule (T-5, F6-1)
    return "code", None


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


def _key_id_ok(v: str, stored: bool) -> bool:
    """A ``key_id`` field's value: a key id this process has configured. ``stored`` (a line READ BACK from the log,
    for the export and the legacy count): also a well-formed key id of a key since retired."""
    return v in KEY_IDS or (stored and key_id_refusal(v) is None)


def violations(kind: str, data, stored: bool = False) -> list:
    """Every (normalised path, reason) in ``data`` that is not a machine value: dict keys and values, lists and tuples
    are walked; int / finite float / bool / None pass; ``nan`` / ``inf``, bytes and any other object are violations.
    Sorted, no duplicates. ``stored``: ``data`` is a line read back from the log (see ``_key_id_ok``)."""
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
                    if reason is not None and not (k in DIGIT_HEX_KEYS and _is_hash(v, True)) \
                            and not (k in KEY_ID_KEYS and _key_id_ok(v, stored)) \
                            and not _t2_ref_ok(kind, path + "/" + seg, v, reason):
                        out.add((path + "/" + seg, reason))
                elif tv in _SCALARS:
                    if tv is float and v - v != 0:          # nan / inf: not a machine value (JSON cannot carry it)
                        out.add((path + "/" + seg, "float"))
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
                    if reason is not None and not _t2_ref_ok(kind, sub, v, reason):
                        out.add((sub, reason))
                elif tv in _SCALARS:
                    if tv is float and v - v != 0:
                        out.add((sub, "float"))
                else:
                    stack.append((v, sub, depth + 1))
        elif isinstance(obj, str):
            reason = _judge(str(obj))[1]
            if reason is not None or type(obj) is not str:
                out.add((path, reason or "type"))
        elif type(obj) not in _SCALARS:
            out.add((path, "type"))
        elif type(obj) is float and obj - obj != 0:
            out.add((path, "float"))
    out.update(_typed(kind, data, stored))
    return sorted(out)


_FIELDS = None                           # sink_fields, imported on first use (it imports this module)


def _t2_ref_ok(kind, path: str, value: str, reason=None) -> bool:
    """core-spec/2 T-2 (lead ruling 21:45 PT, card C2-14b): ``value`` sits in a field DECLARED a T-2 vault reference
    (``sink_fields`` ``F("code", ref=True)``) of a typed sink record AND matches the exact brace-less B-CD-01 grammar
    ``c:(<codes.KINDS>):[0-9a-f]{16}``. Only then is the strict value-class verdict waived (the 16 random hex of a
    code look like a token). The same string in any other field, or a non-matching value in a T-2 field, keeps the
    strict check; there is no global exemption."""
    typed = _typed_kinds()                   # F6f3-2 (ruling 02:15 PT): load the field table BEFORE the kind test
    if not isinstance(kind, str) or not (kind.startswith(LEDGER) or kind in typed):
        return False
    if reason in ("secret", "shape") and _own_id(kind, path, value):   # F6f3-1 (ruling 02:15 PT): registered only
        return True
    sink, rkind = _FIELDS.sink_of(kind)
    field = _FIELDS.SINK_FIELDS.get((sink, rkind, path))
    if field is not None and field.grammar is not None:     # C2-6f (ruling 01:20 PT (B)): an exact code-base
        return reason == "shape" and _FIELDS.matches(field, value)   # grammar waives the SHAPE verdict only
    return field is not None and field.ref and _FIELDS.is_ref(value)


def _typed_kinds() -> frozenset:
    _typed("", None, False)                    # imports sink_fields on first use
    return _FIELDS.LOG_KINDS


def _typed(kind, data, stored: bool) -> list:
    """core-spec/2 T-3 / T-5 (card C2-2c): for a record of a typed sink (a ``ledger:<event_type>`` payload, or a
    seo_log line of a kind in ``sink_fields.SINK_FIELDS``), every path with no type decision (``untyped``) or whose
    value does not match its field's type (``field_type``). Other kinds (not a C2-2 sink; C2-3 adds the rest) are
    value-class checked only, as before. Refuses, never rewrites."""
    global _FIELDS
    if _FIELDS is None:
        import sink_fields as _FIELDS_MOD      # noqa: PLC0415 - circular at import time
        _FIELDS = _FIELDS_MOD
    if not isinstance(kind, str) or not (kind.startswith(LEDGER) or kind in _FIELDS.LOG_KINDS):
        return []
    out = _FIELDS.field_violations(kind, data, stored)
    out = [v for v in out if not (v[1] == "field_type" and _own_ids_at(kind, v[0], data))]      # F6f3-1
    have = {v[0] for v in out}
    out += [(p, "field_type") for p in _own_id_paths(kind)
            if p not in have and any(not _own_id(kind, p, v) for v in _values_at(data, p) if _grammar_ok(kind, p, v))]
    return out


# --- F6f3-1 (lead ruling 02:15 PT; FCL-IDS, O-4 I, T-5, T-7): the tenant registry the guard reads. A tenant_id sink
# field accepts a grammar-valid id (config.TENANT_ID) only when it is a REGISTERED tenant id; an ``ent-`` field
# (sink_fields.ENTITY_ID_FIELDS) only ``ent-`` + a registered tenant id. The service registers a tenant id BEFORE the
# first sink write that names it (creation, seed, replay). A closed set at runtime, like an enum; never a heuristic.
_REGISTERED: set = set()
_OWN_PATHS: dict = {}


def register_tenant(tid) -> None:
    """Registers a tenant id (exact config.TENANT_ID grammar; anything else is ignored, never registered)."""
    if type(tid) is str and TENANT_ID.fullmatch(tid):
        _REGISTERED.add(tid)


def _own_rx(kind, path: str):
    """The own-id grammar of (kind, path): ENTITY_ID_FIELDS' rx, or config.TENANT_ID for a tenant_id code field."""
    if not isinstance(kind, str) or _FIELDS is None:
        return None
    key = (*_FIELDS.sink_of(kind), path)
    rx = _FIELDS.ENTITY_ID_FIELDS.get(key)
    if rx is not None:
        return rx
    field = _FIELDS.SINK_FIELDS.get(key)
    if field is None or not path.endswith("/tenant_id") or "code" not in field.types or field.ref:
        return None
    return TENANT_ID


def _grammar_ok(kind, path: str, value) -> bool:
    rx = _own_rx(kind, path)
    return rx is not None and type(value) is str and rx.fullmatch(value) is not None


def identifier_refused(x, prose: bool = False) -> bool:
    """Review c0c09bd1: an identifier (tenant id, Finance client id) holding a key, a 9+ digit run, an e-mail form
    (review 2b26d980: also one written without its "@"), or failing the strict check (secret_guard.identifier_refusal).
    ``prose`` (a tenant id, a Finance client id): also prose folded with hyphens / dots (secret_guard.folded_prose).
    C2-6i (lead ruling 07:40 PT): moved here from svc_tenants (which re-exports this SAME function, cycle-free) so the
    route's tenant-id check and the sink's own-id check (:func:`_own_id`) are one predicate, never a copy."""
    if secret_guard.identifier_refusal(x) is not None:
        return True
    if prose and secret_guard.folded_prose(x):
        return True
    # sink job D: an identifier is LOGGED, so it must be a machine value of the sink guard (a host name the host rule
    # takes, a code of at most 64 characters holding a letter, a small number). One that is not is
    # refused here (422 IDENTIFIER_WITHHELD) instead of being accepted and then refused at the sink (503)
    return classify(x) is None


def _own_id(kind, path: str, value) -> bool:
    """Exact own-id grammar of THIS field AND the tenant id inside it is registered AND (C2-6i, ruling 07:40 PT) the
    route's own tenant-id check (:func:`identifier_refused` with ``prose=True``, as svc_tenants.create_tenant) does
    not refuse it; any other field: False. So the sink refuses an invalid id even when the endpoint check is gone."""
    if not _grammar_ok(kind, path, value):
        return False
    tid = value.removeprefix("entity:").removeprefix("ent-") if _own_rx(kind, path) is not TENANT_ID else value
    return tid in _REGISTERED and not _route_refused(tid)


@functools.lru_cache(maxsize=4096)
def _route_refused(tid: str) -> bool:
    """:func:`identifier_refused` (``prose=True``) of a REGISTERED tenant id, memoised (a pure function of the
    string; called once per own-id cell of every write)."""
    return identifier_refused(tid, prose=True)


def _own_id_paths(kind) -> list:
    if not isinstance(kind, str) or _FIELDS is None:
        return []
    sk = _FIELDS.sink_of(kind)
    if sk not in _OWN_PATHS:
        _OWN_PATHS[sk] = [k[2] for k in _FIELDS.SINK_FIELDS if k[:2] == sk and _own_rx(kind, k[2]) is not None]
    return _OWN_PATHS[sk]


def _values_at(data, path: str) -> list:
    """Every value at ``path`` (``[]`` = each list item) of ``data``; absent branches add nothing."""
    cur = [data]
    for seg in path.split("/")[1:]:
        name, many, nxt = seg.removesuffix("[]"), seg.endswith("[]"), []
        for c in cur:
            if isinstance(c, dict) and name in c:
                v = c[name]
                nxt.extend(v if many and isinstance(v, (list, tuple)) else () if many else (v,))
        cur = nxt
    return cur


def _own_ids_at(kind, path: str, data) -> bool:
    """True when every value at ``path`` of ``data`` is a registered :func:`_own_id` (none = False)."""
    vals = _values_at(data, path)
    return bool(vals) and all(_own_id(kind, path, v) for v in vals)


def _unknown_log_kind(kind) -> bool:
    """C2-3b2 (lead ruling 17:50 PT F4-0, T-5 fail closed): a seo_log record whose kind has no row in
    ``sink_fields.SINK_FIELDS`` is refused at write (reason ``kind``), never written untyped."""
    if isinstance(kind, str) and kind.startswith(LEDGER):
        return False
    _typed("", None, False)                    # imports sink_fields on first use
    return not (isinstance(kind, str) and kind in _FIELDS.LOG_KINDS)


def refused(kind: str, data) -> list:
    """The violating paths of ``data`` (sorted). There are no exceptions (sink job D: the legacy list is gone)."""
    found = violations(kind, data)
    if _unknown_log_kind(kind):
        found = sorted(set(found) | {("", "kind")})
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
        if value is None or type(value) in (int, bool):
            return value
        if type(value) is float:
            return value if value - value == 0 else WITHHELD   # nan / inf
        if type(value) is str:
            return value if (value in _CACHE or _judge(value)[1] is None) else WITHHELD
        return WITHHELD
    except Exception:  # noqa: BLE001 - fail closed
        return WITHHELD


def _t2_ref_field(kind, path: str, value) -> bool:
    """core-spec/2 T-2 at the served export (lead ruling 03:55 PT F6g4-1, pattern of ruling 21:45 PT): ``value`` sits
    in a field DECLARED a T-2 vault reference (``F("code", ref=True)``) of the typed sink record ``kind`` AND matches
    the exact brace-less grammar ``c:(<codes.KINDS>):[0-9a-f]{16}``. Only the T-2 branch: no own-id or open-grammar
    waiver here, and no global exemption (the same string in an undeclared / other field keeps today's check)."""
    if not isinstance(kind, str) or not (kind.startswith(LEDGER) or kind in _typed_kinds()):
        return False
    field = _FIELDS.SINK_FIELDS.get((*_FIELDS.sink_of(kind), path))
    return field is not None and field.ref and _FIELDS.is_ref(value)


def _ref_sink(kind):
    """(sink, record kind) of a typed sink record ``kind`` (``ledger:<event_type>`` or a seo_log kind of
    ``sink_fields.SINK_FIELDS``), else None."""
    if not isinstance(kind, str) or not (kind.startswith(LEDGER) or kind in _typed_kinds()):
        return None
    return _FIELDS.sink_of(kind)


@functools.lru_cache(maxsize=256)
def _ref_paths(ref_sink):
    """C2-10c6 (F10c5-1): compiled matchers of the DECLARED T-2 reference paths of ``ref_sink`` ((sink, record kind)),
    shape-tolerant: each declared ``[]`` (a list) also matches no segment or ONE object key, so object-shaped
    evidence (``/evidence/payload/...`` or ``/evidence/<key>/payload/...``) and nested lists (``[][]``) meet the same
    field; a leaf nested anywhere under a declared reference field (a list or an object in it) is in that field."""
    out = []
    for key, field in _FIELDS.SINK_FIELDS.items():
        if key[:2] != tuple(ref_sink) or not field.ref:
            continue
        parts = [re.escape(x) for x in key[2].split("[]")]
        out.append(re.compile("(?:/[^/]+)?".join(parts) + "(?:/.*)?"))
    return tuple(out)


def _norm_path(path: str) -> str:
    return path.replace("[]", "")


def withhold(data, kind=None) -> tuple:
    """A copy of a STORED log line's data as it may be served (review 2b26d980 SHOULD 5), and how many leaves were
    replaced: every string (a dict key too), non-finite float or other object that is not a machine value becomes
    the code ``WITHHELD``. A line written since sink job D holds machine values only and comes back unchanged; a
    line written before it (prose inline) keeps its ids, codes, hashes and numbers and loses its text. ``kind``: the
    line's record kind; a leaf is then also judged by its FIELD's declared type (T-1 / T-3; ruling 03:55 PT F6g4-1):
    a T-2 code in a declared T-2 reference field is served (``_t2_ref_field``). C2-10c4 (F10c3-1): any other
    non-null leaf in a DECLARED T-2 reference field (``F("code", ref=True)``; e.g. a legacy raw finance_client_id,
    which has a machine shape) is withheld and counted once, as ``violations(stored=True)`` flags it (field_type):
    the export and the stored-line guard agree. C2-10c6 (F10c5-1): the same holds when the line nests that field in
    an OBJECT where a list is declared, in nested lists, or in a list / object inside the field (``_ref_paths``;
    bounded by MAX_DEPTH like every walk). Never raises (anything it cannot walk is withheld whole)."""
    count = [0]
    ref_sink = _ref_sink(kind)               # None: not a typed sink record (no declared T-2 reference fields)

    def ref_leaf_bad(obj, path) -> bool:
        if ref_sink is None or obj is None:
            return False
        field = _FIELDS.SINK_FIELDS.get((*ref_sink, path))
        if field is None:                        # C2-10c6: object / nested-list shape of a declared ref field
            np = _norm_path(path)
            return any(m.fullmatch(np) for m in _ref_paths(tuple(ref_sink))) and not _FIELDS.is_ref(obj)
        return field.ref and not _FIELDS.is_ref(obj)

    def leaf_ok(k, v, path) -> bool:
        if v in _CACHE or _judge(v)[1] is None:
            return True
        return (k in DIGIT_HEX_KEYS and _is_hash(v, True)) or (k in KEY_ID_KEYS and _key_id_ok(v, True)) \
            or (kind is not None and _t2_ref_field(kind, path, v))

    def walk(obj, key, depth, path):
        t = type(obj)
        if t is not dict and t not in (list, tuple) and ref_leaf_bad(obj, path):
            count[0] += 1
            return WITHHELD
        if obj is None or t in (int, bool):
            return obj
        if t is float:
            if obj - obj == 0:
                return obj
        elif t is str:
            if leaf_ok(key, obj, path):
                return obj
        elif t is dict and depth < MAX_DEPTH:
            out: dict = {}
            for k, v in obj.items():
                name = k
                if type(k) is not str or not (k in _CACHE or _judge(k)[1] is None):
                    count[0] += 1
                    name, n = WITHHELD, 2
                    while name in out:
                        name, n = f"{WITHHELD}.{n}", n + 1
                seg = _key(k)[0] if type(k) is str else "*"
                out[name] = walk(v, k if type(k) is str else None, depth + 1, path + "/" + seg)
            return out
        elif t in (list, tuple) and depth < MAX_DEPTH:
            return [walk(v, None, depth + 1, path + "[]") for v in obj]
        count[0] += 1
        return WITHHELD

    try:
        return walk(data, None, 0, ""), count[0]
    except Exception:  # noqa: BLE001 - fail closed
        return WITHHELD, count[0] + 1
