"""
The ONE type table for vaulted report documents in the owner's view (owner decision 19:20 PT, "Show machine fields";
card C2-OV). It replaces lead rulings 16:15 / 16:58 (F3c-1) on ``svc_vaulted.owner_view``.

``REPORT_FIELDS`` maps (document kind, field path) to the type of a field our code writes. In the owner's view of
their OWN report such a field is shown as a typed machine value; every other path keeps the client-view route
(``svc_vaulted._shown``: CV-3 / CV-4 / CV-6 / CV-7). This table is separate from ``sink_fields.SINK_FIELDS`` (T-3) and
repeats none of its entries: a vault blob is not a sink.

Fail closed: a typed field whose value does not match its grammar, a path that is not listed, a document kind that is
not listed, and a non-string value all go the CV route.

Path grammar: ``/`` + keys; ``[]`` after a key = every list item; ``*`` = any one dict key.

Types (T-1 classes where one exists; the closed grammars below where no T-1 class admits the shape):
  * ``hash``: the T-1 class (16/32/40/64 lower hex with a letter) by ``sink_guard.classify``.
  * ``template``: a log path template (agents/logs.template_path): ``/`` + segments, each segment either exactly one
    placeholder that template code can write (read from ``agents.logs``, lead ruling 19:50 PT OV-1 / card C2-OV2:
    ``logs.PLACEHOLDERS`` = {email} {uuid} {number} {id} {token}, or ``logs.FILE_PLACEHOLDER`` + ``logs.FILE_EXT`` =
    ``{file}.<ext>``, <ext> 1-5 lower alnum) or a literal (no ``{`` ``}`` whitespace, <= 40 code points) that is not
    valueish (CV-3: the CV leaves it unchanged); one valueish literal -> CV for the whole value.
  * ``content_type``: ``type/subtype`` with optional ``; name=value`` parameters; every piece not valueish (CV-3).
  * ``model_version``: ``<model>@<version>`` pieces split on ``/`` and ``@`` (agents/callum: ``f"{model}@{version}"``);
    every piece not valueish (CV-3) or a closed date / version / number / timestamp shape.
  * ``code_text``: fixed prose our code writes (lead ruling 20:15 PT, card C2-OV3): the value must EXACTLY equal one
    string of the closed set the modules themselves produce, regenerated from them at first use (never a copied
    list): ``/agents[]/methodology`` = ``methodology_text()`` of agents selene / delia / roman (their METHODOLOGY
    template filled with code constants); ``/agents[]/limitations[]`` and ``/limitations[]`` = ``svc_gsc.LIMITS``.
    Any other string (an off-template methodology, another agent's text) -> CV. Lead ruling 20:35 PT OV3-2 (card
    C2-OV3b) adds ``entity_check.METHODOLOGY``, ``svc_audits.METHODOLOGIES`` (incl. Osei's hygiene text),
    ``svc_gsc.METHOD``; ``/not_connected_why/*`` = ``svc_audits.NOT_CONNECTED_WHY`` values. Callum's (filled) stays CV.
  * ``code``: the T-1 ``code`` class by ``sink_guard.classify`` (entity_check's per-field comparison results).
  * ``req_path`` / ``req_domain`` / ``req_scheme``: the owner's own request fields (ruling 20:35 PT OV3-1), SAME
    grammar the API validated (``svc_audits.request_path_ok`` + ``client_report`` grammars); failing -> CV.
  Lead ruling 20:55 PT (card C2-OV3c):
  * Q1: Callum's ``/agents[]/methodology`` is code_text only when it EXACTLY equals ``probes.METHODOLOGY`` rendered
    from the SAME report (name / version / sha256[:16] / samples_per_prompt from that agent's facts, version and
    sha256 equal to the report's ``/prompt_set``, ``/domain``, ``probes.MIN_USABLE``). The report carries no brand
    terms, so a methodology with the ``WARN_COMMON`` suffix cannot be rebuilt from it -> CV (fail closed).
  * F1: keys of ``/agents[]/facts/fields`` (``<path>#<field>``) are shown when <path> is one of the report's own
    ``/paths[]`` passing ``req_path`` and <field> is in ``svc_entity.FIELDS`` (the field list entity_check compares);
    any other key -> CV.
  * F2: ``tz`` = ``search_performance.TIMEZONE``; ``gsc_template`` = the log template rule with
    ``search_performance._PLACEHOLDER`` as the placeholder set; entity_check on_site ``sha256`` = ``hash``.
"""

from __future__ import annotations

import re
from types import MappingProxyType

import sink_guard
from agents import logs as _logs

AUDIT_REPORT = "audit_report"
DRIFT_REPORT = "drift_report"
LOG_REPORT = "log_report"
TYPES = frozenset(("hash", "template", "content_type", "model_version", "code_text", "code", "req_path", "req_domain",
                   "req_scheme", "tz", "gsc_template"))

_AUDIT = {
    "/agents[]/facts/engines/*/prompts[]/answers_sha256[]": "hash",
    "/agents[]/facts/engines/*/prompts[]/prompt/sha256": "hash",
    "/agents[]/facts/engines/*/model_versions[]": "model_version",
    "/agents[]/facts/engines/*/prompts[]/model_versions[]": "model_version",
    "/agents[]/facts/pages/*/title/sha256": "hash",
    "/agents[]/facts/prompt_set/sha256": "hash",
    "/agents[]/facts/robots/text_sha256": "hash",
    "/agents[]/facts/robots/fetch/content_type": "content_type",
    "/agents[]/facts/llms_txt/content_type": "content_type",
    "/agents[]/findings[]/detail/content_type": "content_type",
    "/pages/*/fetch/content_type": "content_type",
    "/pages/*/fingerprint/*": "hash",
    "/prompt_set/sha256": "hash",
    "/agents[]/methodology": "code_text",
    "/agents[]/limitations[]": "code_text",
    "/limitations[]": "code_text",
    "/not_connected_why/*": "code_text",
    "/agents[]/facts/fields/*": "code",
    "/paths[]": "req_path",
    "/domain": "req_domain",
    "/scheme": "req_scheme",
    "/search_performance/section/window/timezone": "tz",
    "/search_performance/section/by_page_template/templates[]/template": "gsc_template",
    "/agents[]/findings[]/detail/on_site/sha256": "hash",
}
_DRIFT = {
    "/changes[]/evidence/models_before[]": "model_version",
    "/changes[]/evidence/models_after[]": "model_version",
}
_LOG = {
    "/envelope/facts/families/*/top_path_templates[]/template": "template",
}
REPORT_FIELDS = MappingProxyType({(doc, path): t for doc, table in ((AUDIT_REPORT, _AUDIT), (DRIFT_REPORT, _DRIFT),
                                                                    (LOG_REPORT, _LOG)) for path, t in table.items()})
DOC_KINDS = frozenset(doc for doc, _ in REPORT_FIELDS)


def _segments(path: str) -> tuple:
    """``/a/b[]/c`` -> ("a", "b", "[]", "c")."""
    out = []
    for part in path.split("/")[1:]:
        n = 0
        while part.endswith("[]"):
            part, n = part[:-2], n + 1
        out.extend([part] + ["[]"] * n)
    return tuple(out)


def _compile():
    by_doc: dict = {}
    for (doc, path), t in REPORT_FIELDS.items():
        if t not in TYPES:
            raise ValueError(f"report field type not closed: {doc} {path} {t}")
        by_doc.setdefault(doc, []).append((_segments(path), t))
    return {d: tuple(v) for d, v in by_doc.items()}


_BY_DOC = _compile()


def field_type(doc, segs: tuple):
    """The table type of the value at ``segs`` (keys, ``"[]"`` per list level) of a ``doc`` document, else None."""
    for pat, t in _BY_DOC.get(doc, ()):
        if len(pat) == len(segs) and all(p == "*" and s != "[]" or p == s for p, s in zip(pat, segs)):
            return t
    return None


def _plain(piece: str) -> bool:
    """Not valueish by CV-3 (the client-view masker leaves it unchanged)."""
    from svc_optext import text_view
    return text_view(piece)[0] == piece


_PLACEHOLDERS = _logs.PLACEHOLDERS
_LITERAL = re.compile(r"[^{}\s/]{1,40}")
_CT_PART = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}")
_CT_PARAM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
_MV = re.compile(r"[A-Za-z0-9?][A-Za-z0-9._:?-]{0,63}")
_MV_CLOSED = frozenset(("date", "version", "number", "timestamp"))


def _file_placeholder(seg: str) -> bool:
    head, ext = seg[:len(_logs.FILE_PLACEHOLDER)], seg[len(_logs.FILE_PLACEHOLDER):]
    return head == _logs.FILE_PLACEHOLDER and _logs.FILE_EXT.fullmatch(ext) is not None


def _template(v: str) -> bool:
    if not v.startswith("/") or len(v) > 300:
        return False
    for seg in v.split("/")[1:]:
        if seg == "" or seg in _PLACEHOLDERS or _file_placeholder(seg):
            continue
        if not _LITERAL.fullmatch(seg) or not _plain(seg):
            return False
    return True


def _content_type(v: str) -> bool:
    if len(v) > 128:
        return False
    head, *params = v.split(";")
    mt = head.split("/")
    if len(mt) != 2 or not all(_CT_PART.fullmatch(p) and _plain(p) for p in mt):
        return False
    for p in params:
        name, eq, val = p.strip().partition("=")
        if not eq or not all(_CT_PARAM.fullmatch(x) and _plain(x) for x in (name, val)):
            return False
    return True


def _model_version(v: str) -> bool:
    if len(v) > 128 or v.count("@") < 1 or v.count("@") > 3 or v.count("/") > 2:
        return False
    for piece in re.split(r"[/@]", v):
        if not _MV.fullmatch(piece):
            return False
        if not (_plain(piece) or sink_guard.classify(piece) in _MV_CLOSED or piece == "?"):
            return False
    return True


def _methodologies() -> frozenset:
    import svc_audits
    import svc_gsc
    from agents import delia, entity_check, roman, selene
    return frozenset([m.methodology_text() for m in (selene, delia, roman)] + [entity_check.METHODOLOGY, svc_gsc.METHOD]
                     + list(svc_audits.METHODOLOGIES))


def _not_connected_why() -> frozenset:
    import svc_audits
    return frozenset(svc_audits.NOT_CONNECTED_WHY.values())


def _limitations() -> frozenset:
    import svc_gsc
    return frozenset(svc_gsc.LIMITS)


# (doc, path) -> the producer of its closed code_text set; read lazily (svc_gsc imports service modules)
_CODE_TEXT_SOURCES = {(AUDIT_REPORT, "/agents[]/methodology"): _methodologies,
                      (AUDIT_REPORT, "/agents[]/limitations[]"): _limitations,
                      (AUDIT_REPORT, "/limitations[]"): _limitations,
                      (AUDIT_REPORT, "/not_connected_why/*"): _not_connected_why}
_CODE_TEXT_BY_SEGS = {(doc, _segments(path)): src for (doc, path), src in _CODE_TEXT_SOURCES.items()}
_code_text_cache: dict = {}


def code_texts(doc, segs: tuple) -> frozenset:
    """The closed set of code-written strings allowed at ``segs`` of a ``doc`` document (empty when none)."""
    segs = tuple(segs)
    src = next((s for (d, pat), s in _CODE_TEXT_BY_SEGS.items() if d == doc and len(pat) == len(segs) and all(
        p == "*" and x != "[]" or p == x for p, x in zip(pat, segs))), None)
    if src is None:
        return frozenset()
    got = _code_text_cache.get(src)
    if got is None:
        got = _code_text_cache[src] = src()
    return got


def _assert_code_text_paths() -> None:
    listed = {(d, _segments(p)) for (d, p), t in REPORT_FIELDS.items() if t == "code_text"}
    if listed != set(_CODE_TEXT_BY_SEGS):
        raise ValueError("code_text paths and their sources differ")


_assert_code_text_paths()

def _req_path(v: str) -> bool:
    import client_report
    import svc_audits
    return svc_audits.request_path_ok(v) and client_report._REQ_PATH.fullmatch(v) is not None


def _req_domain(v: str) -> bool:
    import client_report
    return client_report._DOMAIN.fullmatch(v) is not None


def _req_scheme(v: str) -> bool:
    import client_report
    return v in client_report.REQ_SCHEMES


def _tz(v: str) -> bool:
    from agents import search_performance
    return v == search_performance.TIMEZONE


def _gsc_template(v: str) -> bool:
    from agents import search_performance
    if not v.startswith("/") or len(v) > 300:
        return False
    for seg in v.split("/")[1:]:
        if seg == "" or search_performance._PLACEHOLDER.fullmatch(seg):
            continue
        if not _LITERAL.fullmatch(seg) or not _plain(seg):
            return False
    return True


_FIELDS_KEY = ("agents", "[]", "facts", "fields")
_METHOD_SEGS = ("agents", "[]", "methodology")


def _callum_methodology(root: dict, e: dict):
    from agents import probes
    facts, rp, domain = e.get("facts"), root.get("prompt_set"), root.get("domain")
    ps = facts.get("prompt_set") if isinstance(facts, dict) else None
    if not (isinstance(ps, dict) and isinstance(rp, dict) and isinstance(domain, str)):
        return None
    name, ver, sha, n = ps.get("name"), ps.get("version"), ps.get("sha256"), facts.get("samples_per_prompt")
    if not (isinstance(name, str) and type(ver) is int and type(n) is int and isinstance(sha, str)
            and sink_guard.classify(sha) == "hash" and rp.get("version") == ver and rp.get("sha256") == sha):
        return None
    return probes.METHODOLOGY.format(ps=name, ver=ver, sha=sha[:16], n=n, domain=domain, m=probes.MIN_USABLE)


def report_context(doc, root) -> dict:
    """Closed sets bound to ONE report (ruling 20:55 PT): ``{"code_text": {segs: set}, "keys": {segs: set}}``."""
    texts: set = set()
    keys: set = set()
    if doc == AUDIT_REPORT and isinstance(root, dict) and isinstance(root.get("agents"), list):
        from agents import entity_check, probes
        from svc_entity import FIELDS
        paths = root.get("paths")
        own = [p for p in paths if isinstance(p, str) and _req_path(p)] if isinstance(paths, list) else []
        for e in root["agents"]:
            if not isinstance(e, dict):
                continue
            if e.get("agent") == probes.AGENT_CALLUM:
                m = _callum_methodology(root, e)
                if m is not None:
                    texts.add(m)
            elif e.get("agent") == entity_check.AGENT:
                keys.update(f"{p}#{f}" for p in own for f in FIELDS)
    return {"code_text": {_METHOD_SEGS: frozenset(texts)}, "keys": {_FIELDS_KEY: frozenset(keys)}}


def key_shown(doc, parent_segs: tuple, key, ctx=None) -> bool:
    """True when a dict KEY under ``parent_segs`` is a typed machine key of this report (F1); False = CV."""
    if doc != AUDIT_REPORT or not isinstance(key, str) or not ctx:
        return False
    return key in ctx.get("keys", {}).get(tuple(parent_segs), ())


_CHECK = {"hash": lambda v: sink_guard.classify(v) == "hash", "template": _template, "tz": _tz,
          "gsc_template": _gsc_template,
          "content_type": _content_type, "model_version": _model_version,
          "code": lambda v: sink_guard.classify(v) == "code", "req_path": _req_path, "req_domain": _req_domain,
          "req_scheme": _req_scheme}


def shown_typed(doc, segs: tuple, value, ctx=None) -> bool:
    """True when ``value`` is shown as a typed machine value: its path is in the table AND it matches the type's
    grammar. False = the CV route (fail closed)."""
    t = field_type(doc, segs)
    if t is None or not isinstance(value, str):
        return False
    if t == "code_text":
        bound = ctx.get("code_text", {}).get(tuple(segs), ()) if ctx and doc == AUDIT_REPORT else ()
        return value in code_texts(doc, segs) or value in bound
    return _CHECK[t](value)
