"""
Outbound request bodies typed (core-spec/2 T-3 / T-5, Wave-1 D-02m) with the ONE closed exemption E-LLM (owner
decision 14:57 PT, lead ruling F3-4, card C2-3c).

``check(kind, url, body)`` judges one request body BEFORE it is sent (refused at write = the request is not sent):
every leaf of the JSON body must have a decision for (``outbound``, kind, path): a T-1 type in
``sink_fields.SINK_FIELDS`` (matched by ``sink_fields.matches``) or an E-LLM path in ``sink_fields.OUTBOUND_E_LLM``.
An E-LLM path holds ONLY the constant ``SYSTEM_PROMPT`` ("system") or a text of the tenant prompt set bound with
``prompt_scope`` ("prompt"), and ONLY when the URL's host is the kind's configured provider host
(``sink_fields.OUTBOUND_HOSTS``). No bound prompt set -> every "prompt" field is refused (fail closed). A refusal
returns a reason from ``sink_fields.OUTBOUND_REASONS`` and is counted in ``COUNTS`` (never a value).
"""
from __future__ import annotations

import contextlib
import contextvars
import json
from collections import Counter
from typing import Optional
from urllib.parse import urlsplit

import sink_fields

SYSTEM_PROMPT = "Answer the question concisely and cite your sources."   # == answer_adapter.SYSTEM_PROMPT (tested)
COUNTS: Counter = Counter()          # (kind, reason) -> refused requests (in-process, like log_sink.COUNTS)
_PROMPTS: contextvars.ContextVar = contextvars.ContextVar("outbound_prompts", default=None)
_CONFIGURED: contextvars.ContextVar = contextvars.ContextVar("outbound_configured", default=None)
_ECOMP: contextvars.ContextVar = contextvars.ContextVar("outbound_ecomp", default=None)
_SIG = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@contextlib.contextmanager
def configured(**values):
    """C2-3c4 (E-AUTH, rulings 17:15 PT): bind the tenant's / client's CONFIGURED values (compliance client id, GSC
    property, the GSC assertion's signing input) for the calls inside; a None value stays unbound."""
    token = _CONFIGURED.set({**(_CONFIGURED.get() or {}), **{k: v for k, v in values.items() if isinstance(v, str)}})
    try:
        yield
    finally:
        _CONFIGURED.reset(token)


@contextlib.contextmanager
def e_comp(code, coder, host):
    """C2-6g2 E-COMP (owner decision 2026-11-15 02:00 PT): bind the tenant's finance client T-2 CODE, its coder and
    the configured Compliance host. The path admits a value only when its code equals the bound code and the URL's
    host is exactly that host (unbound / no host -> refused). The raw value is never bound or recorded here."""
    token = _ECOMP.set((code, coder, host) if isinstance(code, str) and isinstance(host, str) else None)
    try:
        yield
    finally:
        _ECOMP.reset(token)


def _configured_ok(rule: tuple, value, url_host=None) -> bool:
    mode, name = rule
    if mode == "ecomp":
        b = _ECOMP.get()
        if b is None or type(value) is not str or url_host is None or url_host != b[2]:
            return False
        try:
            return "c:text:" + b[1].code("text", value) == b[0]
        except Exception:  # noqa: BLE001 - fail closed
            return False
    if mode == "const":
        return type(value) is str and value == name
    bound = (_CONFIGURED.get() or {}).get(name)
    if type(value) is not str or bound is None:
        return False
    if mode == "exact":
        return value == bound
    head, dot, sig = value.rpartition(".")
    return mode == "jwt" and dot == "." and head == bound and bool(sig) and set(sig) <= _SIG


@contextlib.contextmanager
def prompt_scope(prompts):
    """Bind the tenant's own configured prompt set (the texts a "prompt" field may carry) for the calls inside."""
    token = _PROMPTS.set(frozenset(p for p in prompts if isinstance(p, str)))
    try:
        yield
    finally:
        _PROMPTS.reset(token)


def scoped(prompts):
    """``enumerate(prompts)`` with the set bound while the caller's loop runs (reset when the loop ends or breaks)."""
    token = _PROMPTS.set(frozenset(p for p in prompts if isinstance(p, str)))
    try:
        yield from enumerate(prompts)
    finally:
        try:
            _PROMPTS.reset(token)
        except ValueError:          # closed from another context: unbind there
            _PROMPTS.set(None)


def _leaves(node, path: str = ""):
    """(normalised path, value) of every leaf; ``[]`` for a list item; an empty object / list is the empty value."""
    if isinstance(node, dict):
        if not node:
            yield path, ""
        for k, v in node.items():
            yield from _leaves(v, path + "/" + (k if isinstance(k, str) else "*"))
    elif isinstance(node, list):
        if not node:
            yield path, ""
        for v in node:
            yield from _leaves(v, path + "[]")
    else:
        yield path, node


def _e_llm_ok(rule: str, value) -> bool:
    if type(value) is not str:
        return False
    if value == SYSTEM_PROMPT and "system" in rule.split("|"):
        return True
    scope = _PROMPTS.get()
    return "prompt" in rule.split("|") and scope is not None and value in scope


def violation(kind: str, url: str, body) -> Optional[tuple]:
    """(path, reason) of the first leaf that may not be sent, or None. ``body``: bytes / str JSON or a parsed doc."""
    client = kind in sink_fields.OUTBOUND_CLIENT_HOSTS           # C2-3c3: finance / compliance / gsc (no E-LLM path)
    host = sink_fields.OUTBOUND_CLIENT_HOSTS[kind] if client else sink_fields.OUTBOUND_HOSTS.get(kind)
    if host is None and not client:
        return "", "untyped"
    try:
        doc = json.loads(body) if isinstance(body, (bytes, str)) else body
    except ValueError:
        return "", "untyped"
    if not isinstance(doc, dict) or not doc:     # C2-4b F4-3 (ruling 17:50 PT): not a typed document -> refused whole
        return "", "untyped"
    try:
        url_host = urlsplit(url).hostname
    except ValueError:
        url_host = None
    if client and host is not None and url_host != host:         # D-02m: a fixed-host client sends to that host only
        return "", "host"
    for path, value in _leaves(doc):
        rule = sink_fields.OUTBOUND_E_LLM.get((kind, path))
        if rule is not None:
            if url_host != host:
                return path, "host"
            if rule == "role":                     # checked per message below (needs the sibling role)
                continue
            if not _e_llm_ok(rule, value):
                return path, "e_llm"
            continue
        conf = sink_fields.OUTBOUND_CONFIGURED.get((kind, path))
        if conf is not None:                       # C2-3c4 E-AUTH: exactly the configured value, own host only
            if not _configured_ok(conf, value, url_host):
                return path, "configured"
            continue
        field = sink_fields.SINK_FIELDS.get((sink_fields.OUTBOUND, kind, path))
        if field is None:
            return path, "untyped"
        if not sink_fields.matches(field, value, sink_fields.leaf_key(path)):
            return path, "field_type"
    return _role_violation(kind, doc)


def _role_violation(kind: str, doc) -> Optional[tuple]:
    """C2-3c2 (ruling 16:58 PT F3c-2): a role-bound E-LLM path ``/<list>[]/<field>``: the item's ``role`` picks the
    rule (OUTBOUND_ROLES: system -> SYSTEM_PROMPT only, user -> the bound prompt set only); any other role refused."""
    for (k, path), rule in sink_fields.OUTBOUND_E_LLM.items():
        if k != kind or rule != "role":
            continue
        head, _sep, field = path[1:].partition("[]/")
        items = doc.get(head) if isinstance(doc, dict) else None
        for item in items if isinstance(items, list) else ():
            if not isinstance(item, dict) or field not in item:
                continue
            role_rule = sink_fields.OUTBOUND_ROLES.get(item.get("role"))
            if role_rule is None or not _e_llm_ok(role_rule, item[field]):
                return path, "e_llm"
    return None


def check(kind: str, url: str, body) -> Optional[str]:
    """The refusal reason (counted) or None when the body may be sent."""
    v = violation(kind, url, body)
    if v is None:
        return None
    COUNTS[(kind, v[1])] += 1
    return v[1]
