"""
The third sink (sink job D): Python logging takes machine values only.

The service's log (``SeoService._commit``) and the ledger refuse free text (sink_guard.py). A process also writes
through ``logging``: this service's own loggers (``seo.*``), uvicorn's (``uvicorn.error``, ``uvicorn.access``: the
request line), httpx / httpcore (request URLs), asyncio and ``py.warnings``. Whatever handler is configured, now or
later, a record passes ``scrub_record`` first:

  * the message TEMPLATE is kept only when it is a string literal of the calling code (it is found among the
    constants of the caller's code object, so an f-string or a concatenation built at run time is not one); any other
    message is replaced whole by ``WITHHELD``;
  * every ARGUMENT is judged by ``sink_guard.scrub``: a machine value (an id, a code, a hash, a host name, a small
    number) stays, anything else (a path, a URL, prose, an e-mail form, a key shape, a long digit run, any object) is
    the code ``WITHHELD``. An int / float of nine or more digits is ``WITHHELD`` as well;
  * EXCEPTION text, tracebacks and stack text are dropped; the record keeps the exception's type name and the
    ``module:function:line`` of the innermost frame inside this service (``svc_audits.where_code``) and nothing else;
  * a template its scrubbed arguments no longer fit (``%d`` given ``WITHHELD``) is written without arguments.
  * every attribute a caller attached with ``extra=`` (review 2b26d980 NIT: a formatter or a JSON handler prints
    them) is judged as an argument: a machine value stays, a dict / list keeps its machine leaves
    (``sink_guard.withhold``), anything else is ``WITHHELD``; an attribute whose NAME is not a machine value is
    removed. They are judged at every pass (``logging`` attaches them after the record factory has run).

Installed three times, on purpose: as the process's log-record FACTORY (every record of every logger is scrubbed
when it is created, whichever handlers exist or are added later, pytest's capture included); as a ``logging.Filter``
on the root logger, the named third-party loggers and every handler present at install; and in
``logging.Handler.handle`` (a record that reaches ANY handler, made by ``logging.makeLogRecord`` or changed after
its creation, is scrubbed before it is emitted). All are idempotent; a record already scrubbed and not changed since
is left as it is.
"""

from __future__ import annotations

import logging
import sys

import sink_guard

WITHHELD = sink_guard.WITHHELD
LOGGERS = ("", "seo", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "asyncio", "py.warnings")
NUMBER_MAX = 10 ** 8                     # an int / float argument below this prints as it is (under 9 digits)
_MARK = "_seo_sink_done"
_FRAMES_MAX = 40
_STATE = {"factory": None, "previous": None, "handle": None, "enabled": True}
# the attributes ``logging`` itself puts on a record; any other one came from ``extra=``
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | frozenset(("message", "asctime", "taskName", _MARK))


def _scrub_extra(record: logging.LogRecord) -> None:
    """The ``extra=`` attributes of ``record`` scrubbed in place. Never raises."""
    d = record.__dict__
    if len(d) <= len(_STANDARD) and all(k in _STANDARD for k in d):
        return
    for k in [k for k in d if k not in _STANDARD]:
        try:
            v = d[k]
            if type(k) is not str or sink_guard.classify(k) is None:
                del d[k]
            elif type(v) in (dict, list, tuple):
                d[k] = sink_guard.withhold(v)[0]
            else:
                d[k] = scrub_arg(v)
        except Exception:  # noqa: BLE001 - fail closed
            d.pop(k, None)


def _number(x):
    try:
        if isinstance(x, bool):
            return x
        if isinstance(x, int):
            return x if -NUMBER_MAX < x < NUMBER_MAX else WITHHELD
        if x != x or x in (float("inf"), float("-inf")):
            return WITHHELD
        return x if -NUMBER_MAX < x < NUMBER_MAX and len(repr(x)) <= 12 else WITHHELD
    except Exception:  # noqa: BLE001 - fail closed
        return WITHHELD


def scrub_arg(x):
    """One logging argument as it may be printed."""
    if x is None:
        return None
    if type(x) in (int, float, bool):
        return _number(x)
    if type(x) is str:
        return sink_guard.scrub(x)
    return WITHHELD


def _literal_of_caller(msg: str, pathname, lineno) -> bool:
    """True when ``msg`` is a string constant of the code that made the logging call (the frame at ``pathname`` /
    ``lineno`` on this thread's stack)."""
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


def _exception_code(exc_info) -> str:
    """``<TypeName>@<module:function:line>`` of a logged exception: codes only, never its text."""
    try:
        exc = exc_info[1] if isinstance(exc_info, tuple) else exc_info
        if not isinstance(exc, BaseException):
            return "Exception@unknown"
        name = type(exc).__name__[:60]
        name = name if sink_guard.classify(name) == "code" else "Exception"
        try:
            from svc_audits import where_code
            where = where_code(exc)
        except Exception:  # noqa: BLE001
            where = "unknown"
        return f"{name}@{where}"
    except Exception:  # noqa: BLE001 - fail closed
        return "Exception@unknown"


def scrub_record(record: logging.LogRecord) -> logging.LogRecord:
    """``record`` scrubbed in place (once). Never raises."""
    if not _STATE["enabled"]:
        return record
    _scrub_extra(record)
    done = record.__dict__.get(_MARK)
    if done is not None and record.msg is done[0] and record.args is done[1] and not record.exc_info \
            and not record.exc_text and not record.stack_info:
        return record                                      # scrubbed already, and nothing was put back since
    try:
        msg, args = record.msg, record.args
        trusted = type(msg) is str and _literal_of_caller(msg, record.pathname, record.lineno)
        if not trusted:
            msg, args = WITHHELD, None
        elif args:
            if isinstance(args, dict):
                args = {(k if type(k) is str and sink_guard.classify(k) is not None else WITHHELD): scrub_arg(v)
                        for k, v in args.items()}
            else:
                args = tuple(scrub_arg(a) for a in (args if isinstance(args, tuple) else (args,)))
            try:
                msg % args
            except Exception:  # noqa: BLE001 - the scrubbed arguments no longer fit the template
                msg, args = msg + " [arguments " + WITHHELD + "]", None    # (no arguments: printed as it is)
        if record.exc_info or record.exc_text:
            code = _exception_code(record.exc_info)
            msg = msg + " [" + (code.replace("%", "%%") if args else code) + "]"
        record.msg, record.args = msg, args
        record.exc_info, record.exc_text, record.stack_info = None, None, None
    except Exception:  # noqa: BLE001 - fail closed: nothing of the record is printed
        record.msg, record.args = WITHHELD, None
        record.exc_info, record.exc_text, record.stack_info = None, None, None
    record.__dict__[_MARK] = (record.msg, record.args)
    return record


class SinkFilter(logging.Filter):
    """``scrub_record`` as a logging filter: never drops a record, only scrubs it."""

    def filter(self, record: logging.LogRecord) -> bool:
        scrub_record(record)
        return True


FILTER = SinkFilter("seo-sink")


def _factory(*a, **k):
    record = _STATE["previous"](*a, **k)
    return scrub_record(record)


def _handle(self, record):
    """``logging.Handler.handle`` with the scrub first: whatever handler a record reaches, and however the record
    was made (``logging.makeLogRecord``, a queue listener, a handler added after ``install``), it is scrubbed."""
    scrub_record(record)
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
