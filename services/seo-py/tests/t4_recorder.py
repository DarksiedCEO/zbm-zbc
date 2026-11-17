"""C2-4 (core-spec/2 T-4 (b)): the recording sink of the meta-test ``test_every_sink_field_has_a_type_decision``.

Installed by tests/conftest.py BEFORE the sink observer guard (so the guard's originals are these wrappers and no test
is marked as silencing the guard). The wrappers call the original first and change nothing; they record only while
``STATE["active"]``: set for the whole run by the env flag ``T4_RECORD_OUT=<evidence.json>`` (gate runners C2-G1 /
C2-G4: full suite and war room), or for one block by ``recording()`` (the meta-test's own smoke).

What is recorded is what is actually WRITTEN, per T-3 sink:
- seo_log / ledger: every record ``sink_guard.enforce`` let through (the write gate of service._commit and ledger.py);
- logging: every record ``log_sink.scrub_record`` did not drop (kind = logger name; /template, /args/<i>, /exc_type,
  /extra/<name>, as ruled F3-1 / C2-3a);
- error: every JSON response body with status >= 400 (kind = the reason in /detail, or ``validation`` for a 422 list;
  paths as ruled F3-2 / C2-3b);
- outbound: every request ``outbound.check`` let through (kind = the engine / client kind; F3-4 / C2-3c).
A path is DECIDED when it is a key of ``sink_fields.SINK_FIELDS``, or (logging /template) a listed LOG_TEMPLATES
literal of its logger, or (outbound) an E-LLM / E-AUTH path (OUTBOUND_E_LLM / OUTBOUND_CONFIGURED). Paths, kinds and
counts only, never a value; a kind that is not a name is kept as a short sha256 tag.
"""
import atexit
import collections
import contextlib
import hashlib
import json
import logging
import os
import re
import weakref

STATE = {"active": False, "out": os.environ.get("T4_RECORD_OUT") or None, "installed": False, "paused": 0}
_GATE0 = {}                           # FCL-REC: the log_sink gate parts as installed (a test that swaps one = gate off)
SEEN = collections.Counter()          # (sink, kind, path, decided) -> observations
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}\Z")
_STD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


def _kind_tag(kind) -> str:
    if isinstance(kind, str) and _NAME.match(kind):
        return kind
    return "?" + hashlib.sha256(repr(kind).encode()).hexdigest()[:12]


def _paths(data, path=""):
    """The normalised leaf paths of ``data`` exactly as sink_fields.field_violations walks it."""
    import sink_fields
    if isinstance(data, dict):
        for k, v in data.items():
            seg = sink_fields._seg(k)
            if seg == "*":
                yield path + "/{key}"
            if isinstance(v, (dict, list, tuple)):
                yield from _paths(v, path + "/" + seg)
            else:
                yield path + "/" + seg
    elif isinstance(data, (list, tuple)):
        for v in data:
            if isinstance(v, (dict, list, tuple)):
                yield from _paths(v, path + "[]")
            else:
                yield path + "[]"
    else:
        yield path


def decided(sink: str, kind: str, path: str, template=None) -> bool:
    import sink_fields as sf
    if sink == "logging" and path == "/template":
        return template in sf.LOG_TEMPLATES.get(kind, ())
    if sink == "outbound" and ((kind, path) in sf.OUTBOUND_E_LLM or (kind, path) in sf.OUTBOUND_CONFIGURED):
        return True
    return (sink, kind, path) in sf.SINK_FIELDS


def observe(sink: str, kind, paths, template=None) -> None:
    if not STATE["active"] or STATE["paused"]:
        return
    k = _kind_tag(kind)
    for p in set(paths):
        SEEN[(sink, k, p, k == kind and decided(sink, kind, p, template))] += 1


def undecided() -> list:
    return sorted({(s, k, p) for (s, k, p, d) in SEEN if not d})


def report() -> dict:
    rows = [{"sink": s, "kind": k, "path": p, "decided": d, "count": n} for (s, k, p, d), n in sorted(SEEN.items())]
    per = collections.Counter(s for s, _k, _p, _d in SEEN)
    return {"spec": "core-spec/2 T-4 (b)", "recorded_paths": len(SEEN), "per_sink": dict(per),
            "undecided": [list(u) for u in undecided()], "rows": rows}


def write(path=None) -> None:
    path = path or STATE["out"]
    if path:
        with open(path, "w") as fh:
            json.dump(report(), fh, indent=0)


@contextlib.contextmanager
def recording():
    """Record for one block (the meta-test's smoke); the session record (env flag) is kept and restored."""
    saved, before = STATE["active"], SEEN.copy()
    SEEN.clear()
    STATE["active"] = True
    try:
        yield SEEN
    finally:
        STATE["active"] = saved
        SEEN.clear()
        SEEN.update(before)              # the block's own (planted) rows never enter the session record


@contextlib.contextmanager
def paused():
    """FCL-REC (G-7): nothing is recorded inside the block. ONLY for a block that turns a write gate OFF on purpose
    (mutation / load-bearing tests: what is written there is test-synthetic, never production's). Nests."""
    STATE["paused"] += 1
    try:
        yield
    finally:
        STATE["paused"] -= 1


def _log_gate_on(log_sink) -> bool:
    """FCL-REC: the logging gate is as installed - judging enabled and the extra scrub not swapped out. A record
    written while a test has turned it off is test-synthetic and not recorded (classes.md B rows 1, 4, 5)."""
    return log_sink._STATE.get("enabled") is True and log_sink._scrub_extra is _GATE0.get("scrub_extra")


def _log_paths(record):
    args = record.args
    out = ["/template"]
    if isinstance(args, dict):
        out += ["/args/{key}"]
    elif args:
        out += [f"/args/{i}" for i in range(len(args))]
    if record.exc_info:
        out.append("/exc_type")
    import log_sink
    marks = {getattr(log_sink, "_DROP", None), getattr(log_sink, "_MARK", None)}
    out += ["/extra/" + k for k in record.__dict__ if k not in _STD and k not in marks]
    return out


_ORIG = weakref.WeakKeyDictionary()    # C2-4b F4-2: record -> (template, had exception) at its first judgement


def _first_judgement(record, log_sink):
    """(msg, had exception) when log_sink is about to judge ``record`` afresh (not a re-pass of an already judged,
    unchanged record), else None. log_sink appends `` [TypeName]`` to a written record's message and clears its
    exception, so a later pass (logger filter, Handler.handle) no longer shows either."""
    d = record.__dict__
    done = d.get(getattr(log_sink, "_MARK", ""))
    if done is not None and record.msg is done[0] and record.args is done[1] and not record.exc_info \
            and not record.exc_text and not record.stack_info:
        return None
    return record.msg, bool(record.exc_info or record.exc_text)


def _log_written(record, log_sink):
    """(template, paths) of what log_sink WROTE (C2-4b F4-2). A refused record is the typed ``log_refused`` record as
    it stands. A written one: its template is the message it was judged with, and only when the written message is
    exactly that, or that plus `` [TypeName]`` when it carried an exception (then ``/exc_type`` is a written path);
    any other written message is recorded as its own (undecided) template."""
    import sink_fields as sf
    paths = _log_paths(record)
    if record.name == sf.LOG_REFUSED:
        return record.msg, paths
    tmpl, exc = _ORIG.get(record, (record.msg, bool(record.exc_info)))
    msg = record.msg
    if type(tmpl) is not str or type(msg) is not str:
        return msg, paths
    if exc and msg.startswith(tmpl + " [") and msg.endswith("]") and _NAME.match(msg[len(tmpl) + 2:-1] or "-"):
        return tmpl, paths + (["/exc_type"] if "/exc_type" not in paths else [])
    return (tmpl if msg == tmpl else msg), paths


def _parsed(body):
    """The body as outbound.violation judges it (C2-4b F4-3: bytes / str JSON parsed first)."""
    if isinstance(body, (bytes, str)):
        try:
            return json.loads(body)
        except ValueError:
            return body
    return body


def install() -> None:
    """Idempotent. Wraps the four write gates (seo_log / ledger, logging, error, outbound)."""
    if STATE["installed"]:
        return
    import log_sink
    import outbound
    import sink_guard
    from starlette.responses import JSONResponse

    enforce0, scrub0, check0, render0 = sink_guard.enforce, log_sink.scrub_record, outbound.check, JSONResponse.render
    _GATE0["scrub_extra"] = log_sink._scrub_extra

    def enforce(kind, data):
        enforce0(kind, data)
        if STATE["active"]:
            led = isinstance(kind, str) and kind.startswith(sink_guard.LEDGER)
            observe("ledger" if led else "seo_log", kind[len(sink_guard.LEDGER):] if led else kind, _paths(data))

    def scrub_record(record):
        first = _first_judgement(record, log_sink)
        if first is not None:
            _ORIG[record] = first
        out = scrub0(record)
        if STATE["active"] and _log_gate_on(log_sink) and not record.__dict__.get(getattr(log_sink, "_DROP", "")):
            tmpl, paths = _log_written(record, log_sink)
            observe("logging", record.name, paths, template=tmpl)
        return out

    def check(kind, url, body):
        reason = check0(kind, url, body)
        if reason is None and STATE["active"]:
            observe("outbound", kind, [p for p, _v in outbound._leaves(_parsed(body))])
        return reason

    def render(self, content):
        if STATE["active"] and getattr(self, "status_code", 200) >= 400:
            detail = content.get("detail") if isinstance(content, dict) else None
            kind = "validation" if isinstance(detail, list) else detail
            if detail is None and isinstance(content, dict):
                kind = "health"           # lead ruling 19:05 PT FCL-Q1: only /status is typed; any other path undecided
            observe("error", kind, _paths(content) if isinstance(content, (dict, list)) else [""])
        return render0(self, content)

    enforce.__wrapped__, scrub_record.__wrapped__, check.__wrapped__ = enforce0, scrub0, check0
    sink_guard.enforce, log_sink.scrub_record, outbound.check, JSONResponse.render = enforce, scrub_record, check, render
    STATE["installed"] = True
    if STATE["out"]:
        STATE["active"] = True
        atexit.register(write)
