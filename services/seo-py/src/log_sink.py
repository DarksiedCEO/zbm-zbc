"""
The third sink (sink job D): Python logging takes typed machine values only (core-spec/2 T-3 / T-5, lead ruling F3-1).

The service's log (``SeoService._commit``) and the ledger refuse free text (sink_guard.py). A process also writes
through ``logging``: this service's own loggers (``seo.*``), uvicorn's, httpx / httpcore, asyncio and ``py.warnings``.
Whatever handler is configured, now or later, a record passes ``scrub_record`` first. Its record kind is its logger
name; its fields are ``/template``, ``/args/<i>``, ``/extra/<name>`` and ``/exc_type`` (``sink_fields``, sink
``logging``):

  * NOT EMITTED AT ALL (Wave-1 D-04: raw log lines, IPs, URLs are never stored in any form): every record of
    ``uvicorn.access`` (the request line) and of ``httpx`` / ``httpcore`` (request URLs), and of ``asyncio`` /
    ``py.warnings`` (no typed table: their arguments are object reprs and warning text, not T-1 values). Counted in
    ``COUNTS["dropped"]`` per logger; never ``SINK_REFUSED`` (T-7 unaffected);
  * WRITTEN AS IT IS: a record whose logger has a ``SINK_FIELDS`` table, whose template is one of its
    ``sink_fields.LOG_TEMPLATES`` literals AND a string constant of the calling code, whose every argument matches the
    type of its ``/args/<i>`` field, which carries no ``extra=`` attribute and whose exception (if any) has a type
    name matching ``/exc_type``. The exception's text, traceback and stack text are dropped; its type name is
    appended to the message (``[TypeName]``);
  * REFUSED (T-5: never rewritten, truncated or scanned): any other record. It is NOT written; the same record object
    becomes the typed counter record ``log_refused`` {logger, reason} (template
    ``log_refused logger=%s reason=%s``, reason one of ``sink_fields.LOG_REASONS``), counted in
    ``COUNTS["refused"]``. The v1 ``WITHHELD`` substitution is gone.

Extra attributes (``extra=``) are attached by ``logging`` after the record factory has run, so they are judged at
every pass (``_scrub_extra``: removed; their presence refuses the record, path ``/extra/<name>`` is untyped).

Installed three times, on purpose: as the process's log-record FACTORY, as a ``logging.Filter`` on the root logger,
the named third-party loggers and every handler present at install (the filter returns False for a dropped record),
and in ``logging.Handler.handle`` (a record that reaches ANY handler is judged before it is emitted; a dropped one is
not emitted). All are idempotent.
"""

from __future__ import annotations

import collections
import logging
import sys

import sink_fields
import sink_guard

WITHHELD = sink_guard.WITHHELD                     # kept for importers; this sink no longer writes it
LOGGERS = ("", "seo", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "asyncio", "py.warnings")
DROPPED = ("uvicorn.access", "httpx", "httpcore", "asyncio", "py.warnings")   # F3-1: these families, children too
NUMBER_MAX = 10 ** 8
REFUSED_TEMPLATE = "log_refused logger=%s reason=%s"
_MARK = "_seo_sink_done"
_DROP = "_seo_sink_drop"
_FRAMES_MAX = 40
_STATE = {"factory": None, "previous": None, "handle": None, "enabled": True}
COUNTS = {"dropped": collections.Counter(), "refused": collections.Counter()}
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | frozenset(("message", "asctime", "taskName", _MARK, _DROP))


def scrub_arg(x):
    """v1 helper kept for importers (judges one value as sink_guard does). The logging sink itself no longer
    rewrites arguments: it types them (T-5)."""
    if x is None:
        return None
    if type(x) is bool:
        return x
    if type(x) in (int, float):
        return x if x == x and -NUMBER_MAX < x < NUMBER_MAX and len(repr(x)) <= 12 else WITHHELD
    if type(x) is str:
        return sink_guard.scrub(x)
    return WITHHELD


def is_dropped_logger(name) -> bool:
    return type(name) is str and any(name == d or name.startswith(d + ".") for d in DROPPED)


def _scrub_extra(record: logging.LogRecord) -> list:
    """The ``extra=`` attributes of ``record`` removed in place; their names (``/extra/<name>`` paths, none typed).
    Never raises."""
    d = record.__dict__
    if len(d) <= len(_STANDARD) and all(k in _STANDARD for k in d):
        return []
    found = []
    for k in [k for k in d if k not in _STANDARD]:
        d.pop(k, None)
        found.append(k)
    return found


def _literal_of_caller(msg: str, pathname, lineno) -> bool:
    """True when ``msg`` is a string constant of the code that made the logging call."""
    try:
        frame = sys._getframe(1)
    except ValueError:
        return False
    n = 0
    while frame is not None and n < _FRAMES_MAX:
        code = frame.f_code
        if code.co_filename == pathname and frame.f_lineno == lineno:
            return any(type(c) is str and c == msg for c in code.co_consts)
        frame = frame.f_back
        n += 1
    return False


def _exc_type(exc_info):
    exc = exc_info[1] if isinstance(exc_info, tuple) else exc_info
    return type(exc).__name__ if isinstance(exc, BaseException) else None


def violation(record: logging.LogRecord):
    """The refusal reason of ``record`` (one of ``sink_fields.LOG_REASONS``), or None when it may be written."""
    name = record.name
    templates = sink_fields.LOG_TEMPLATES.get(name)
    if templates is None:
        return "untyped"
    msg = record.msg
    if type(msg) is not str or msg not in templates or not _literal_of_caller(msg, record.pathname, record.lineno):
        return "template"
    args = record.args
    if isinstance(args, dict):
        return "untyped" if args else None
    args = args if isinstance(args, tuple) else ((args,) if args is not None else ())
    for i, a in enumerate(args):
        field = sink_fields.SINK_FIELDS.get(("logging", name, f"/args/{i}"))
        if field is None:
            return "untyped"
        if not sink_fields.matches(field, a):
            return "field_type"
    try:
        msg % args
    except Exception:  # noqa: BLE001 - arguments that do not fit the template
        return "field_type"
    if record.exc_info or record.exc_text:
        field = sink_fields.SINK_FIELDS.get(("logging", name, "/exc_type"))
        t = _exc_type(record.exc_info)
        if field is None:
            return "untyped"
        if t is None or not sink_fields.matches(field, t):
            return "field_type"
    return None


def _refuse(record: logging.LogRecord, reason: str) -> None:
    """``record`` becomes the typed ``log_refused`` counter record (nothing of the refused record is kept)."""
    logger = record.name if type(record.name) is str else ""
    logger = logger if sink_guard.classify(logger) in ("code", "hostname") else "unknown"
    COUNTS["refused"][(logger, reason)] += 1
    record.name = sink_fields.LOG_REFUSED
    record.msg, record.args = REFUSED_TEMPLATE, (logger, reason)
    record.exc_info, record.exc_text, record.stack_info = None, None, None


def scrub_record(record: logging.LogRecord) -> logging.LogRecord:
    """``record`` judged in place (once; extras at every pass). Never raises. A dropped record is marked ``_DROP``."""
    if not _STATE["enabled"]:
        return record
    d = record.__dict__
    try:
        if d.get(_DROP) or is_dropped_logger(record.name):
            if not d.get(_DROP):
                COUNTS["dropped"][record.name] += 1
            d[_DROP] = True
            record.msg, record.args = "", None
            record.exc_info, record.exc_text, record.stack_info = None, None, None
            _scrub_extra(record)
            return record
        extras = _scrub_extra(record)
        done = d.get(_MARK)
        if done is not None and record.msg is done[0] and record.args is done[1] and not record.exc_info \
                and not record.exc_text and not record.stack_info:
            if extras and record.name != sink_fields.LOG_REFUSED:
                _refuse(record, "untyped")                 # /extra/<name>: no extra attribute is typed
                d[_MARK] = (record.msg, record.args)
            return record
        reason = "untyped" if extras else violation(record)
        if reason is not None:
            _refuse(record, reason)
        else:
            t = _exc_type(record.exc_info) if (record.exc_info or record.exc_text) else None
            if t is not None:
                record.msg = record.msg + " [" + t + "]"
            record.exc_info, record.exc_text, record.stack_info = None, None, None
    except Exception:  # noqa: BLE001 - fail closed: the record is refused whole
        try:
            _refuse(record, "untyped")
        except Exception:  # noqa: BLE001
            d[_DROP] = True
    d[_MARK] = (record.msg, record.args)
    return record


class SinkFilter(logging.Filter):
    """``scrub_record`` as a logging filter: drops a D-04 record, judges every other one."""

    def filter(self, record: logging.LogRecord) -> bool:
        scrub_record(record)
        return not record.__dict__.get(_DROP)


FILTER = SinkFilter("seo-sink")


def _factory(*a, **k):
    record = _STATE["previous"](*a, **k)
    if a and a[0] is None:                             # logging.makeLogRecord's blank record: judged at handle
        return record
    return scrub_record(record)


def _handle(self, record):
    """``logging.Handler.handle`` with the judgement first: a dropped record is not emitted by any handler."""
    scrub_record(record)
    if record.__dict__.get(_DROP) and _STATE["enabled"]:
        return False
    return _STATE["handle"](self, record)


def _attach(target) -> None:
    if target is not None and FILTER not in getattr(target, "filters", ()):
        target.addFilter(FILTER)


def install() -> None:
    """Idempotent. The record factory, and ``FILTER`` on the root logger, the named loggers, their handlers and the
    handler of last resort."""
    if logging.getLogRecordFactory() is not _factory:
        _STATE["previous"] = logging.getLogRecordFactory()
        _STATE["factory"] = _factory
        logging.setLogRecordFactory(_factory)
    if logging.Handler.handle is not _handle:
        _STATE["handle"] = logging.Handler.handle
        logging.Handler.handle = _handle
    for name in LOGGERS:
        logger = logging.getLogger(name)
        _attach(logger)
        for handler in list(logger.handlers):
            _attach(handler)
    _attach(logging.lastResort)


def installed() -> bool:
    return logging.getLogRecordFactory() is _factory and FILTER in logging.getLogger().filters \
        and logging.Handler.handle is _handle
