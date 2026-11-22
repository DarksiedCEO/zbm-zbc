"""
The sink type decision table (core-spec/2 T-1, T-2, T-3; enforcement half of T-5).

``SINK_FIELDS`` maps (sink, record kind, normalised field path) -> ``F``, the field's ONE type. A type is exactly one
of the 21 v1 S-1 classes (``sink_guard.CLASSES``, T-1): there is no free-text type, no "string", no "any". An enum is a
``code`` field whose value set is a closed constant of the code base (``F.values``). A vault reference field (T-2) is a
``code`` field that also holds the exact brace-less Wave-1 B-CD-01 grammar ``c:(<codes.KINDS>):[0-9a-f]{16}``
(``F.ref``); the braced form ``{c:..}`` is refused (Wave-1 D-02a), and so is a bare 16-hex value.

Paths use sink_guard's normalised form: ``/a/b`` for dict fields, ``[]`` for a list item, ``*`` for a dict key that is
not a schema name, ``{key}`` for the KEY of such a dict (its own type). Sinks in this module (card C2-2):
``seo_log`` (data/seo_log.jsonl line data, kind = the line kind) and ``ledger`` (a ledger payload, kind =
``ledger:<event_type>``). The other T-3 sinks (logging, error bodies, events, outbound) are card C2-3.

``field_violations`` reports (path, reason) only, never a value: ``untyped`` (the path is not in the table: T-3 /
T-4) or ``field_type`` (the value does not match its field's type: T-5). Nothing is truncated, scanned or rewritten.
Matching is by class: a string matches when ``sink_guard.classify`` puts it in the field's class (the digit-only hex
of the ``DIGIT_HEX_KEYS`` and a configured key id under ``KEY_ID_KEYS`` as in sink_guard); a JSON ``true`` / ``false``
matches ``boolean``; a JSON integer of at most 8 digits matches ``number``. JSON null matches only a ``nullable`` field and a finite
float only a ``numeric`` one (lead ruling CL-6); a union field (CL-7) matches any of its declared types.

STATUS (C2-2b + C2-2b2, 2026-11-14): the table is frozen from the suite inventory (sink_fields_table.py), dynamic
dict keys included. Wired by card C2-2c: ``sink_guard.violations`` (hence ``refused`` / ``enforce``) adds
``field_violations`` for every ledger payload and every seo_log kind in ``LOG_KINDS``; SINK_REFUSED is unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional

import reasons
import sink_guard
from primitives import codes

TYPES = sink_guard.CLASSES                       # T-1: the closed type list, unchanged from v1 S-1
SINKS = ("seo_log", "ledger", "logging", "error", "outbound")   # C2-3a: + logging (F3-1); C2-3b: + error (F3-2/F3-6); C2-3c: + outbound (F3-4)
LEDGER = sink_guard.LEDGER
REF_RX = re.compile(r"c:(" + "|".join(codes.KINDS) + r"):[0-9a-f]{16}\Z")
NUMBER_MAX = 99_999_999                          # ``number``: 1..8 digits, optionally signed
CODE_GRAMMAR_OVERLAP: frozenset = frozenset()    # classes a ``code`` field also takes: none (one type per field)
REASONS = ("untyped", "field_type")


@dataclass(frozen=True)
class F:
    """One field's type decision (CL-7: one declared, closed type SET): ``type`` from ``TYPES`` (None only for a
    null-only field), ``also`` the other members of a CL-7 union (listed with a reason in ``UNIONS``); ``values`` a
    closed enum (``code`` only); ``ref`` the T-2 vault reference grammar (``code`` only); ``numeric`` (CL-6) a finite
    JSON float is also a ``number``; ``nullable`` (CL-6) JSON null is accepted; ``digit_hex`` (lead ruling F2b-4,
    field-scoped) a ``hash`` field also takes fixed-length lowercase hex that is all digits (listed with its reason
    in ``DIGIT_HEX_FIELDS``; the global ``sink_guard.DIGIT_HEX_KEYS`` rule is unchanged)."""
    type: Optional[str]
    values: Optional[frozenset] = None
    ref: bool = False
    also: frozenset = frozenset()
    numeric: bool = False
    nullable: bool = False
    digit_hex: bool = False
    grammar: Optional[re.Pattern] = None   # C2-6f (ruling 01:20 PT (B)): exact code-base grammar, code only

    @property
    def types(self) -> frozenset:
        return frozenset(((self.type,) if self.type is not None else ()) + tuple(self.also))

    def __post_init__(self):
        if self.type is None and not (self.nullable and not self.also):
            raise ValueError("only a null-only (nullable) field has no type")
        if not self.types <= frozenset(TYPES):
            raise ValueError("sink field type outside the closed T-1 list")
        if self.numeric and "number" not in self.types:
            raise ValueError("numeric widens a number field only")
        if self.digit_hex and "hash" not in self.types:
            raise ValueError("digit_hex widens a hash field only")
        if (self.values is not None or self.ref) and self.types != {"code"}:
            raise ValueError("an enum / a reference field is typed code")
        if (self.values is not None or self.ref) and self.type != "code":
            raise ValueError("an enum / a reference field is typed code")
        if self.values is not None and self.ref:
            raise ValueError("a field is an enum or a reference, not both")
        if self.grammar is not None and (self.type != "code" or self.types != {"code"} or self.ref
                                         or self.values is not None):
            raise ValueError("a grammar field is a plain code field")


def is_ref(value) -> bool:
    """T-2: the brace-less B-CD-01 code of a vaulted text, exactly."""
    return type(value) is str and REF_RX.match(value) is not None


def leaf_key(path: str) -> str:
    """The dict key a path ends in (``/a/epoch`` -> ``epoch``), or "" for a list item / a dynamic key."""
    if not path or path.endswith(("[]", "/*", "/{key}")):
        return ""
    return path.rsplit("/", 1)[-1]


def matches(field: F, value, key: str = "", stored: bool = False) -> bool:
    """True when ``value`` is a value of ``field``'s type (T-5). ``key``: the dict key it sits under (the digit-hex
    and key-id rules of sink_guard are per key). ``stored``: a line read back from the log."""
    t, types = type(value), field.types
    if value is None:
        return field.nullable                       # CL-6
    if t is bool:
        return "boolean" in types and field.values is None and not field.ref
    if t is int:
        return "number" in types and -NUMBER_MAX <= value <= NUMBER_MAX
    if t is float:
        return field.numeric and value - value == 0 and -NUMBER_MAX <= value <= NUMBER_MAX   # CL-6: finite only
    if t is not str:
        return False                                # bytes, objects: no class
    if field.ref:
        return is_ref(value)
    if field.grammar is not None:                   # C2-6f: the exact grammar replaces the open-code shape rule ONLY
        return field.grammar.fullmatch(value) is not None and sink_guard._check(value) is None
    if field.values is not None:
        return value in field.values and sink_guard.classify(value) is not None
    if (field.digit_hex or key in sink_guard.DIGIT_HEX_KEYS) and "hash" in types and sink_guard._is_hash(value, True):
        return True
    if key in sink_guard.KEY_ID_KEYS and "key_id" in types and sink_guard._key_id_ok(value, stored):
        return True
    cls = sink_guard.classify(value)
    return cls in types or ("code" in types and cls in CODE_GRAMMAR_OVERLAP)


def sink_of(kind: str) -> tuple:
    """(sink, record kind) of a sink_guard kind: ``ledger:<event_type>`` is a ledger payload, anything else a log
    line kind."""
    if kind.startswith(LEDGER):
        return "ledger", kind[len(LEDGER):]
    return "seo_log", kind


def _seg(k) -> str:
    return sink_guard._key(k)[0] if type(k) is str else "*"


def field_violations(kind, data, stored: bool = False, table=None) -> list:
    """Every (normalised path, reason) in ``data`` whose leaf has no type decision (``untyped``) or does not match
    its decision (``field_type``). A dynamic dict key is checked against ``<path>/{key}``. Sorted, no duplicates."""
    table = SINK_FIELDS if table is None else table
    if not isinstance(kind, str):
        return [("", "untyped")]
    sink, rkind = sink_of(kind)
    out = set()

    def leaf(path: str, value, key: str):
        field = table.get((sink, rkind, path))
        if field is None:
            out.add((path, "untyped"))
        elif not matches(field, value, key, stored):
            out.add((path, "field_type"))

    stack = [(data, "", 0)]
    while stack:
        obj, path, depth = stack.pop()
        if depth > sink_guard.MAX_DEPTH:
            out.add((path, "field_type"))
            continue
        if isinstance(obj, dict):
            for k, v in obj.items():
                seg = _seg(k)
                if seg == "*":
                    leaf(path + "/{key}", k, "")
                sub = path + "/" + seg
                if isinstance(v, (dict, list, tuple)):
                    stack.append((v, sub, depth + 1))
                else:
                    leaf(sub, v, k if seg != "*" else "")
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                if isinstance(v, (dict, list, tuple)):
                    stack.append((v, path + "[]", depth + 1))
                else:
                    leaf(path + "[]", v, "")
        else:
            leaf(path, obj, "")
    return sorted(out)


# ------------------------------------------------------------------------------------------------- the table
# Frozen by card C2-2b from the suite inventory (core-v2/c22-inv, 2994 passed): the data is sink_fields_table.py, one
# spec string per (sink, kind, path): T-1 types joined by "|" (a CL-7 union), "?" nullable, "#" numeric (CL-6), "!"
# digit-hex (F2b-4). C2-2b2 added the dynamic-key entries (<dict path>/{key}) from inventory2.json. The
# C2-2 seed (ledger log_anchor /epoch hash, /seq number, /line_shaN hash) is part of it unchanged.
import sink_fields_table as _data                    # noqa: E402  (data only, no imports of its own)


def _parse(spec: str) -> F:
    nullable, numeric, digit_hex = "?" in spec, "#" in spec, "!" in spec
    types = [t for t in spec.replace("?", "").replace("#", "").replace("!", "").split("|") if t]
    first = "code" if "code" in types else (types[0] if types else None)
    return F(first, also=frozenset(t for t in types if t != first), numeric=numeric, nullable=nullable,
             digit_hex=digit_hex)


_TABLE = {(sink, kind, path): _parse(spec)
          for sink, kinds in _data.TABLE.items() for kind, paths in kinds.items() for path, spec in paths.items()}

# ------------------------------------------------------------------------------------- sink `logging` (C2-3a)
# Lead ruling F3-1 (14:58 PT): record kind = the logger name; paths /template, /args/<i>, /extra/<name>, /exc_type.
# Frozen from the static inventory of every own logging call in src (12 sites; tests/test_log_sink_typed.py checks
# it against the source with ast). /template is not a T-1 field (a template holds spaces): it must be one of the
# literal constants listed in LOG_TEMPLATES for its logger (static check) AND a literal of the calling code (run time).
# No own call passes ``extra=``, so no /extra/<name> path is typed: any extra attribute refuses the record (T-5).
# The `log_refused` counter record (written instead of a refused one) is typed here too.
LOG_REFUSED = "log_refused"
LOG_REASONS = ("untyped", "template", "field_type")
LOG_TEMPLATES = MappingProxyType({
    "seo.api": frozenset(("unexpected %s",)),
    "seo.vault": frozenset(("vault: the final line was repaired at open (%d bytes removed, %d records removed, "
                            "%d records kept)",)),
    "seo.sink": frozenset(("commit refused by the sink guard: sink=%s paths=%d first=%s",)),
    "seo.audits": frozenset(("audit run failed: %s in %s at %s", "audit report not stored: %s")),
    "seo.citations": frozenset(("citation run failed: %s at %s",)),
    "seo.gsc": frozenset(("search_performance read failed: %s",)),
    "seo.schedules": frozenset(("drift not recorded: %s",)),
    LOG_REFUSED: frozenset(("log_refused logger=%s reason=%s",)),
})
_LOGGING = {
    "seo.api": {"/args/0": "code", "/exc_type": "code"},
    "seo.vault": {"/args/0": "number", "/args/1": "number", "/args/2": "number", "/exc_type": "code"},
    "seo.sink": {"/args/0": "code|hostname", "/args/1": "number", "/args/2": "code|hostname", "/exc_type": "code"},
    "seo.audits": {"/args/0": "code", "/args/1": "code", "/args/2": "code", "/exc_type": "code"},
    "seo.citations": {"/args/0": "code", "/args/1": "code", "/exc_type": "code"},
    "seo.gsc": {"/args/0": "code", "/exc_type": "code"},
    "seo.schedules": {"/args/0": "code", "/exc_type": "code"},
    LOG_REFUSED: {"/args/0": "code|hostname", "/args/1": "code"},
}
_LOG_UNIONS = {
    ("logging", "seo.sink", "/args/0"): "the guard kind: a code, or a ledger kind written with dots (ledger.<type>) "
                                         "that sink_guard.classify reads as hostname (service.py _commit)",
    ("logging", "seo.sink", "/args/2"): "the first refused path written with dots (content.prompts.list), which "
                                         "classify reads as hostname; 'none' / 'job' are codes",
    ("logging", LOG_REFUSED, "/args/0"): "a logger name: dotted names (seo.audits) classify as hostname",
}
_TABLE.update({("logging", kind, path): _parse(spec) for kind, paths in _LOGGING.items() for path, spec in paths.items()})

# --------------------------------------------------------------------------------------- sink `error` (C2-3b)
# Lead rulings 14:58 PT F3-2 / F3-6 (ADR 0051 dec 45). Record kind = the reason code (``detail``) of a domain error
# (SeoError), or ``validation`` for a 422 request-validation body. Domain bodies: ``detail`` + a CLOSED set of kwargs
# per reason, frozen from the static inventory of every raise site in src (tests/test_error_body_typed.py re-scans
# it): ``field`` (a request field name) on INVALID only; ``override_allowed`` on the reasons the invoice / Compliance
# checks raise with it (svc_invoices.py: R(code) / R(exc.code) from finance_client + compliance_client, and
# VAULT_UNAVAILABLE). An unknown or ill-typed kwarg -> the body is sent as {"detail": <reason>} only and the refusal
# is counted (api.ERROR_REFUSED). Validation bodies: ``msg`` prose is replaced by ``msg_code`` (closed set below).
ERROR = "error"
ERROR_VALIDATION = "validation"
ERROR_REASONS = ("untyped", "field_type")
ERROR_OVERRIDE_REASONS = frozenset((
    "FINANCE_NOT_CONFIGURED", "FINANCE_UNAVAILABLE", "FINANCE_AUTH_REFUSED", "FINANCE_RESPONSE_INVALID",
    "INVOICE_NOT_FOUND", "INVOICE_ENTITY_MISMATCH", "INVOICE_TENANT_MISMATCH", "INVOICE_CURRENCY_UNSUPPORTED",
    "INVOICE_NOT_PAID", "INVOICE_REFUNDED", "INVOICE_HELD",
    "COMPLIANCE_NOT_CONFIGURED", "COMPLIANCE_UNAVAILABLE", "COMPLIANCE_AUTH_REFUSED", "COMPLIANCE_RESPONSE_INVALID",
    "COMPLIANCE_NOT_ACTIVATED", "VAULT_UNAVAILABLE"))
ERROR_FIELD_REASONS = frozenset(("INVALID",))
ERROR_FIELD_NAMES = frozenset(("engines", "path", "reason"))   # enum: the literal `field=` values of the raise sites
_ERROR = {r: {"/detail": "code",
              **({"/field": "code"} if r in ERROR_FIELD_REASONS else {}),
              **({"/override_allowed": "boolean"} if r in ERROR_OVERRIDE_REASONS else {})}
          for r in sorted(reasons.CODES)}
_ERROR[ERROR_VALIDATION] = {"/detail[]/loc[]": "code|number", "/detail[]/msg_code": "code", "/detail[]/type": "code",
                            "/errors_total": "number"}
_ERROR_UNIONS = {
    (ERROR, ERROR_VALIDATION, "/detail[]/loc[]"): "a loc element is a closed request name (api.ERROR_LOC_NAMES) "
                                                  "or a list index (number)",
}
_TABLE.update({(ERROR, kind, path): _parse(spec) for kind, paths in _ERROR.items() for path, spec in paths.items()})
_TABLE.update({(ERROR, r, "/field"): F("code", values=ERROR_FIELD_NAMES) for r in ERROR_FIELD_REASONS})
# Lead ruling 19:05 PT FCL-Q1: the unauthenticated /health 503 body {"status": "closed"} carries no ``detail``; it is
# typed here (kind ``health``, /status = a code ENUM of service.health()'s status values), no API change. Not a reason
# code, so not in ERROR_KWARGS (error_body never builds it).
ERROR_HEALTH = "health"
HEALTH_STATUSES = frozenset(("ok", "degraded", "closed"))   # service.SeoService.health()["status"]
_TABLE[(ERROR, ERROR_HEALTH, "/status")] = F("code", values=HEALTH_STATUSES)
ERROR_KWARGS = MappingProxyType({k: frozenset(p[1:] for p in paths if p != "/detail") for k, paths in _ERROR.items()
                                 if k != ERROR_VALIDATION})


def error_body(reason, body: dict) -> tuple:
    """(content, refusal) of a domain error body: every kwarg must be in the reason's closed set and match its type
    (T-5); otherwise content is {"detail": reason} only and refusal is "untyped" / "field_type" (never rewritten)."""
    allowed = ERROR_KWARGS.get(reason)
    for k, v in body.items():
        if allowed is None or k not in allowed:
            return {"detail": reason}, "untyped"
        if not matches(_TABLE[(ERROR, reason, "/" + k)], v, k):
            return {"detail": reason}, "field_type"
    return {"detail": reason, **body}, None


# ------------------------------------------------------------------------------------ sink `outbound` (C2-3c)
# Lead ruling F3-4 / owner decision 14:57 PT: outbound request bodies to non-audited hosts are typed (T-1 / T-3), kind =
# the engine name (perplexity's rollback protocol = `perplexity.sonar`); frozen from each engine's request_body. The
# ONE closed exemption E-LLM (OUTBOUND_E_LLM, not in SINK_FIELDS: not a T-1 type): "system" = exactly the constant
# SYSTEM_PROMPT, "prompt" = a text of the bound tenant prompt set, only to OUTBOUND_HOSTS[kind] (outbound.py enforces).
# An empty object / list (google's `google_search: {}`) is checked as the empty value.
OUTBOUND = "outbound"
OUTBOUND_REASONS = ("untyped", "field_type", "host", "e_llm", "configured")   # C2-3c4: + E-AUTH / configured
_OUTBOUND = {
    "openai": {"/model": "model_id", "/tools[]/type": "code", "/include[]": "code", "/store": "boolean"},
    "perplexity": {"/model": "model_id", "/effort": "code", "/stream": "boolean", "/store": "boolean"},
    "perplexity.sonar": {"/model": "model_id", "/messages[]/role": "code", "/stream": "boolean"},
    "anthropic": {"/model": "model_id", "/max_tokens": "number", "/messages[]/role": "code", "/tools[]/type": "code",
                  "/tools[]/name": "code", "/tools[]/max_uses": "number"},
    "google": {"/contents[]/role": "code", "/tools[]/google_search": "empty"},
}
_TABLE.update({(OUTBOUND, k, p): _parse(s) for k, paths in _OUTBOUND.items() for p, s in paths.items()})
# every outbound `code` field is an ENUM (a canary shaped like a code must not pass): the literal constants of the
# engines' request_body (tests/test_outbound_typed.py checks them against the engine modules)
OUTBOUND_ENUMS = MappingProxyType({"/tools[]/type": frozenset(("web_search", "web_search_20250305")),
                                   "/include[]": frozenset(("web_search_call.action.sources",)),
                                   "/effort": frozenset(("fast",)), "/messages[]/role": frozenset(("system", "user")),
                                   "/contents[]/role": frozenset(("user",)), "/tools[]/name": frozenset(("web_search",))})
_TABLE.update({(OUTBOUND, k, p): F("code", values=OUTBOUND_ENUMS[p]) for k, paths in _OUTBOUND.items() for p in paths
               if p in OUTBOUND_ENUMS})
OUTBOUND_ROLES = MappingProxyType({"system": "system", "user": "prompt"})
OUTBOUND_E_LLM = MappingProxyType({
    ("openai", "/instructions"): "system", ("openai", "/input"): "prompt",
    ("perplexity", "/instructions"): "system", ("perplexity", "/input"): "prompt",
    # C2-3c2 (lead ruling 16:58 PT F3c-2): sonar's shared path is ROLE-BOUND (OUTBOUND_ROLES): role system ->
    # SYSTEM_PROMPT only, role user -> a prompt of the bound set only, any other role refused
    ("perplexity.sonar", "/messages[]/content"): "role",
    ("anthropic", "/system"): "system", ("anthropic", "/messages[]/content"): "prompt",
    ("google", "/systemInstruction/parts[]/text"): "system", ("google", "/contents[]/parts[]/text"): "prompt",
})
OUTBOUND_HOSTS = MappingProxyType({"openai": "api.openai.com", "perplexity": "api.perplexity.ai",
                                   "perplexity.sonar": "api.perplexity.ai", "anthropic": "api.anthropic.com",
                                   "google": "generativelanguage.googleapis.com"})
# ------------------------------------------------------- sink `outbound`: the non-LLM clients (C2-3c3)
# T-3 "outbound request bodies and parameters" / Wave-1 D-02m for finance_client, compliance_client, gsc_client. The
# doc each client checks (outbound.check) where it builds its request: {"path": {<URL path parameters>}, "body":
# {<body fields>}}; finance / compliance send a GET with one path parameter and no body or query; gsc.sites has no
# parameter ({"path": {}}); gsc.query = the property split into its type and host, plus the JSON body. Headers carry
# only the constant names and the configured credentials (not values found anywhere). Hosts: OUTBOUND_CLIENT_HOSTS
# (None = the configured service base URL of that client instance; Google = gsc_client.ALLOWED_HOSTS's API host).
# NOT typed here (lead questions in core-v2/HANDOFF 5ab): the token request (form body: the signed JWT `assertion`)
# a URL-prefix property's path part, and compliance's /path/client_id (a plain `code` takes code-shaped canaries).
OUTBOUND_CLIENT_ENUMS = MappingProxyType({
    "/path/site_type": frozenset(("sc-domain", "https", "http")),
    "/body/dimensions[]": frozenset(("date", "page", "query")), "/body/type": frozenset(("web",)),
    "/body/dataState": frozenset(("final",)), "/body/aggregationType": frozenset(("auto", "byPage", "byProperty"))})
_OUTBOUND_CLIENTS = {
    "finance": {"/path/invoice_id": "invoice_id"},
    "gsc.sites": {"/path": "empty"},
    "gsc.query": {"/path/site_type": "code", "/path/site_host": "hostname", "/body/startDate": "date",
                  "/body/endDate": "date", "/body/dimensions[]": "code", "/body/type": "code",
                  "/body/dataState": "code", "/body/aggregationType": "code", "/body/rowLimit": "number",
                  "/body/startRow": "number"},
}
_TABLE.update({(OUTBOUND, k, p): (F("code", values=OUTBOUND_CLIENT_ENUMS[p]) if p in OUTBOUND_CLIENT_ENUMS
                                  else _parse(s)) for k, paths in _OUTBOUND_CLIENTS.items() for p, s in paths.items()})
OUTBOUND_CLIENT_HOSTS = MappingProxyType({"finance": None, "compliance": None, "gsc.sites": "www.googleapis.com",
                                          "gsc.query": "www.googleapis.com", "gsc.token": "oauth2.googleapis.com"})
# C2-3c4 (lead rulings 17:15 PT F3c3-1 / F3c3-2, E-AUTH): paths that admit ONLY the exact configured value bound at
# run time with outbound.configured(name=value) (unbound -> refused, fail closed). "exact" = equal to the bound
# value; "jwt" = header.claims equal to the bound signing input (build_assertion for the configured key) plus one
# base64url signature segment; "const" = exactly that constant of the code base (the OAuth grant-type URN has ':'
# so no T-1 type takes it). Each goes only to its kind's host above (credentials to their issuer only).
OUTBOUND_CONFIGURED = MappingProxyType({
    ("compliance", "/path/client_id"): ("ecomp", "compliance_client_code"),   # C2-6g2 E-COMP (owner 02:00 PT)
    ("gsc.query", "/path/property"): ("exact", "gsc_property"),
    ("gsc.token", "/body/assertion"): ("jwt", "gsc_assertion_input"),
    ("gsc.token", "/body/grant_type"): ("const", "urn:ietf:params:oauth:grant-type:jwt-bearer")})   # == GRANT_TYPE

# C2-15 (AL-1): the catalog allowlist audit row, one per change attempt (agents/cv_allowlist.py; the strings below
# equal cv_allowlist.SOURCES (+ "unknown") / OUTCOMES - test_cv_allowlist checks it; no import: cv_allowlist imports us
# through sink_guard). Ledger event ``cv_allowlist_changed``: actor, time, counts, source, outcome; all typed.
# FCL-IDS (lead ruling F2e-1 / F2f-2, C2-FCL #15): /tenant_id takes the SAME union as every other tenant_id
# field (code|hash, sink_fields_table UNIONS "C2-2f (F2e-1)"): the route accepts 16-hex-shaped tenant ids.
_CV_TENANT = F("code", also=frozenset(("hash",)))
CV_ALLOWLIST_ENUMS = MappingProxyType({"/source": frozenset(("shopify", "unknown")),
                                       "/outcome": frozenset(("applied", "refused_cap", "refused_source"))})
_TABLE.update({(LEDGER.removesuffix(":"), "cv_allowlist_changed", p): s for p, s in {
    "/tenant_id": _CV_TENANT, "/actor": F("actor"), "/at": F("timestamp"), "/count": F("number"),
    "/previous_count": F("number"), "/requested_count": F("number"),
    **{p: F("code", values=v) for p, v in CV_ALLOWLIST_ENUMS.items()}}.items()})
# C2-15b: the service commits the row as ``_commit`` evidence (+ rk / seq) under the log line ``cv_allowlist_set``
_CV_ROW = {"/tenant_id": _CV_TENANT, "/actor": F("actor"), "/at": F("timestamp"), "/count": F("number"),
           "/previous_count": F("number"), "/requested_count": F("number"), "/rk": F("request_key"),
           "/seq": F("number"), **{p: F("code", values=v) for p, v in CV_ALLOWLIST_ENUMS.items()}}
_TABLE.update({(LEDGER.removesuffix(":"), "cv_allowlist_changed", p): _CV_ROW[p] for p in ("/rk", "/seq")})
_TABLE.update({("seo_log", "cv_allowlist_set", p): s for p, s in {
    "/tenant_id": _CV_TENANT, "/actor": F("actor"), "/count": F("number"), "/ref": F("service_id", nullable=True),
    "/outcome": F("code", values=CV_ALLOWLIST_ENUMS["/outcome"]), "/evidence[]/event_id": F("service_id"),
    "/evidence[]/event_type": F("code"), "/evidence[]/subject_id": F("code"),
    **{"/evidence[]/payload" + p: f for p, f in _CV_ROW.items()}}.items()})

# C2-14 (RV-1): the reveal audit row, one per attempt (svc_reveal.py; the strings below equal svc_reveal.SURFACES /
# OUTCOMES - test_reveal checks it; no import: svc_reveal imports the service). Ledger event ``span_revealed`` committed
# as ``_commit`` evidence under the log line ``span_reveal``; the span code is the T-2 brace-less ``c:text:<16hex>``.
# The revealed value is in no field (RV-3).
RV_ENUMS = MappingProxyType({"/surface": frozenset(("citation_run",)),
                             "/outcome": frozenset(("revealed", "not_found", "rate_limited"))})
_RV_ROW = {"/tenant_id": _CV_TENANT, "/actor": F("actor"), "/at": F("timestamp"), "/span": F("code", ref=True),
           "/rk": F("request_key"), "/seq": F("number"), **{p: F("code", values=v) for p, v in RV_ENUMS.items()}}
_TABLE.update({(LEDGER.removesuffix(":"), "span_revealed", p): s for p, s in _RV_ROW.items()})
_TABLE.update({("seo_log", "span_reveal", p): s for p, s in {
    "/tenant_id": _CV_TENANT, "/actor": F("actor"), "/span": F("code", ref=True),
    **{p: F("code", values=v) for p, v in RV_ENUMS.items()}, "/evidence[]/event_id": F("service_id"),
    "/evidence[]/event_type": F("code"), "/evidence[]/subject_id": F("code"),
    **{"/evidence[]/payload" + p: f for p, f in _RV_ROW.items()}}.items()})


# ------------------------------------------------------------- C2-6e: T-4 brand terms = tenant text -> T-2 references
# The two paths T-4 found undecided (full suite 00:45 PT, t4_full.json): a prompt set's brand terms. They are tenant
# free text (core-spec/2 V-1: operator text is stored ONLY in the vault as codes; T-2: a field that points at free text
# carries the brace-less B-CD-01 code). The service writes no such path (svc_audits vaults the document, the record
# carries content_ref / content_hmac / facts counts); a record that re-inlines the document (test_canary_all_routes
# mutation) is therefore refused: the raw term does not match the T-2 grammar. Never typed open ``code``.
for _p in ("/inline/brand_terms[]", "/inline/brand_term_rules[]/term"):
    _TABLE[("seo_log", "prompt_set_version", _p)] = F("code", ref=True)
# C2-6g2 E-COMP (ruling 01:20 PT (A), owner decision 2026-11-15 02:00 PT): the finance client reference is held at
# every sink only as its T-2 reference c:text:<16hex> (raw only in the tenant vault); never typed open code/number
for _s, _p in ((LEDGER.removesuffix(":"), "/finance_client_id"), ("seo_log", "/finance_client_id"),
               ("seo_log", "/evidence[]/payload/finance_client_id")):
    _TABLE[(_s, "tenant_finance_client_set", _p)] = F("code", ref=True)

# ------------------------------------------------------------------- C2-6a: closed code fields -> enums (F6-1 (1))
# Lead ruling 21:25 PT (T-1 "Enums are code fields whose value set is a closed constant of the code base"): a plain
# ``code`` field whose writer's value set is a closed constant is bound to THAT constant (imported, never copied, so
# it cannot drift); membership is exact. Only plain ``code`` fields (no CL-7 union, no enum yet, no T-2 ref) are
# bound. The modules imported here import nothing that imports sink_fields (no cycle). Decisions per field and the
# fields left open (C2-6b): core-v2/c26a/field_decisions.md. Rule: (sinks, kinds or None = every kind, leaf) ->
# (constant name, value set); the leaf is the last path segment.
import config as _config                             # noqa: E402
import envelope as _envelope                         # noqa: E402  (C2-6a2; imports stdlib only)
import ports as _ports                               # noqa: E402
import svc_manager as _svc_manager                   # noqa: E402

_MOVE_KINDS = frozenset(("agent_moved", "agent_lifecycle"))   # svc_manager.ManagerMixin move: the lifecycle record
LEDGER_EVENT_TYPES = frozenset(k for s, k, _p in _TABLE if s == "ledger")   # the ledger kinds this table types
# C2-6a2: an ``*_interrupted`` record's agent also holds the pseudo-agent svc_manager.REPORT_AGENT (svc_audits
# _run_audit / _vault_failure): bound to svc_manager.RUN_AGENTS = AGENTS + (REPORT_AGENT,).
_AGENT_KINDS = frozenset(k for _s, k, _p in _TABLE if not k.endswith("_interrupted"))
_INTERRUPT_KINDS = frozenset(k for _s, k, _p in _TABLE if k.endswith("_interrupted"))
# C2-6a2: the kinds whose ``outcome`` is an envelope outcome (envelope() refuses any other; svc_audits
# overall_outcome / worst_outcome, svc_logs, svc_gsc and the engine outcomes of agents.callum / probes write
# literals of envelope.OUTCOMES). Left OPEN-CODE: svc_citations._code (citation_run_recorded facts) may write
# svc_citations.WITHHELD, which is not an outcome and lives in a module that imports sink_guard.
_OUTCOME_KINDS = frozenset(("audit_completed", "audit_report_recorded", "gsc_read_recorded",
                            "log_report_recorded", "log_ingest_finished", "citation_run_recorded"))
_LEFT_OPEN = frozenset((("seo_log", "citation_run_recorded", "/facts/outcome"),
                        ("seo_log", "citation_run_recorded", "/evidence[]/payload/outcome"),
                        ("ledger", "citation_run_recorded", "/outcome")))
_SL = ("seo_log", "ledger")
ENUM_BINDINGS = (
    (("error",), None, "detail", "reasons.CODES", frozenset(reasons.CODES)),
    (("seo_log", "ledger"), _AGENT_KINDS, "agent", "svc_manager.AGENTS", frozenset(_svc_manager.AGENTS)),
    (("seo_log", "ledger"), _MOVE_KINDS, "from", "svc_manager.STATES", frozenset(_svc_manager.STATES)),
    (("seo_log", "ledger"), _MOVE_KINDS, "to", "svc_manager.STATES", frozenset(_svc_manager.STATES)),
    (("seo_log", "ledger"), _MOVE_KINDS, "reason", "svc_manager.REASONS", frozenset(_svc_manager.REASONS)),
    (("seo_log", "ledger"), None, "engine", "ports.ANSWER_ENGINES", frozenset(_ports.ANSWER_ENGINES)),
    (("seo_log", "ledger"), None, "engines[]", "ports.ANSWER_ENGINES", frozenset(_ports.ANSWER_ENGINES)),
    (("seo_log", "ledger"), None, "mode", "config.INVOICE_VERIFICATION_MODES",
     frozenset(_config.INVOICE_VERIFICATION_MODES)),
    (("seo_log",), None, "event_type", "sink_fields.LEDGER_EVENT_TYPES", LEDGER_EVENT_TYPES),
    # C2-6a2 (rulings 21:25 / 22:00 PT): the closed groups whose constant now exists and imports without a cycle
    (_SL, _INTERRUPT_KINDS, "agent", "svc_manager.RUN_AGENTS", frozenset(_svc_manager.RUN_AGENTS)),
    (_SL, _OUTCOME_KINDS, "outcome", "envelope.OUTCOMES", frozenset(_envelope.OUTCOMES)),
    (_SL, ("audit_completed",), tuple("agent_outcomes/" + a for a in _svc_manager.AGENTS), "envelope.OUTCOMES",
     frozenset(_envelope.OUTCOMES)),
    (_SL, None, "interval", "envelope.INTERVALS", frozenset(_envelope.INTERVALS)),
    (_SL, None, "findings[]/severity", "envelope.SEVERITIES", frozenset(_envelope.SEVERITIES)),
    (_SL, None, ("findings[]/data_class", "provenance/data_class", "mnc_priority/data_class"),
     "envelope.DATA_CLASSES", frozenset(_envelope.DATA_CLASSES)),
    (_SL, None, ("findings[]/decision", "summary/decision/{key}"), "envelope.DECISIONS",
     frozenset(_envelope.DECISIONS)),
    (_SL, None, ("findings[]/capability", "summary/capability/{key}", "legend/capabilities/{key}"),
     "envelope.CAPABILITIES", frozenset(_envelope.CAPABILITIES)),
    (_SL, None, "tenant_kind", "config.TENANT_KINDS", frozenset(_config.TENANT_KINDS)),
    (("error",), None, "msg_code", "reasons.ERROR_MSG_CODES", frozenset(reasons.ERROR_MSG_CODES)),
)
ENUM_BOUND = {}                                      # (sink, kind, path) -> constant name (field_decisions.md)
for (_sk, _kd, _p), _f in list(_TABLE.items()):
    if _f.types != {"code"} or _f.values is not None or _f.ref or (_sk, _kd, _p) in _LEFT_OPEN:
        continue
    for _sinks, _kinds, _leaf, _name, _vals in ENUM_BINDINGS:
        _leaves = (_leaf,) if isinstance(_leaf, str) else _leaf     # C2-6a2: a leaf is the path's last segment(s)
        if _sk in _sinks and (_kinds is None or _kd in _kinds) and any(_p.endswith("/" + x) for x in _leaves):
            _TABLE[(_sk, _kd, _p)] = F("code", values=_vals, nullable=_f.nullable)
            ENUM_BOUND[(_sk, _kd, _p)] = _name
            break
ENUM_BOUND = MappingProxyType(ENUM_BOUND)

# ------------------------------------------------- C2-6f: entity ids = exact code-base grammar (lead ruling 01:20 PT (B))
# ``ent-<tenant id>`` (svc_entity: ``eid = f"ent-{tid}"``, ZBM_ENTITY ``ent-zbm``; the evidence subject is
# ``entity:<eid>``) with the tenant-id grammar of FCL-IDS (config.TENANT_ID, imported, never copied). Checked INSTEAD
# of the open-code shape rule for these fields only (like T-2); the strict check still applies. A VG value never
# carries the prefix, so T-5 stays 0 on them.
ENTITY_ID_RX = re.compile("ent-(?:" + _config.TENANT_ID.pattern + ")")
ENTITY_SUBJECT_RX = re.compile("entity:ent-(?:" + _config.TENANT_ID.pattern + ")")
ENTITY_ID_FIELDS = MappingProxyType({
    **{("seo_log", k, p): ENTITY_ID_RX for k in ("entity_created", "entity_field_set")
       for p in ("/entity_id", "/_obj", "/evidence[]/payload/entity_id", "/inline/entity_id")},
    **{("seo_log", k, "/evidence[]/subject_id"): ENTITY_SUBJECT_RX for k in ("entity_created", "entity_field_set")},
    **{("ledger", k, "/entity_id"): ENTITY_ID_RX for k in ("entity_created", "entity_field_set")},
    ("seo_log", "audit_completed", "/report/agents[]/facts/entity_id"): ENTITY_ID_RX})
for _k, _rx in ENTITY_ID_FIELDS.items():
    _f = _TABLE[_k]
    _TABLE[_k] = F("code", grammar=_rx, nullable=_f.nullable)

SINK_FIELDS = MappingProxyType(_TABLE)
_CV_UNIONS = {(sink, kind, path): "FCL-IDS (F2e-1 / F2f-2): same union and reason as every other tenant_id "
              "field - TENANT pattern + svc_tenants.identifier_refused(prose=True) accept 16-hex-shaped tenant "
              "ids (classify hash; 18 of 5548 generated, test_sink_api_ids); was code"
              for sink, kind, path in ((LEDGER.removesuffix(":"), "cv_allowlist_changed", "/tenant_id"),
                                       ("seo_log", "cv_allowlist_set", "/tenant_id"),
                                       ("seo_log", "cv_allowlist_set", "/evidence[]/payload/tenant_id"))}
_CV_UNIONS.update({(sink, kind, path): "C2-14: the reveal audit row's tenant_id, the same union and reason as "
                   "cv_allowlist (FCL-IDS F2e-1 / F2f-2)"
                   for sink, kind, path in ((LEDGER.removesuffix(":"), "span_revealed", "/tenant_id"),
                                            ("seo_log", "span_reveal", "/tenant_id"),
                                            ("seo_log", "span_reveal", "/evidence[]/payload/tenant_id"))})
UNIONS = MappingProxyType({**dict(_data.UNIONS), **_LOG_UNIONS, **_ERROR_UNIONS, **_CV_UNIONS})   # CL-7: every multi-type field, with its reason
NUMERIC = frozenset(k for k, f in _TABLE.items() if f.numeric)      # CL-6 lists
NULLABLE = frozenset(k for k, f in _TABLE.items() if f.nullable)
DIGIT_HEX_FIELDS = MappingProxyType(dict(_data.DIGIT_HEX_FIELDS))   # F2b-4: field-scoped all-digit hex, with reasons
KEY_UNTYPED = MappingProxyType(dict(_data.KEY_UNTYPED))   # C2-2b2: {key} paths left untyped (refused keys), reasons
LOG_KINDS = frozenset(k for s, k, _p in _TABLE if s == "seo_log")   # C2-2c: the seo_log kinds sink_guard types
