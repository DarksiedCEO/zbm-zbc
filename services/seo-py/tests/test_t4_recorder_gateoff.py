"""FCL-REC (G-7; c2fcl/classes.md B rows 1-5): the T-4 recorder does not record writes made while a test has
turned a write gate OFF on purpose, and STILL records a real undecided write while the gates are on (no weakening).
Synthetic values only."""
import collections
import contextlib
import io
import logging

import pytest

import log_sink
import sink_fields as sf
import t4_recorder as rec

PROSE = "zqplanted wvsynthetic note"
UNLISTED = "fclrec planted %s"                 # not a LOG_TEMPLATES literal of any logger


@contextlib.contextmanager
def _recorded():
    """rec.recording() for one block; its rows copied out (recording() clears them at the block's end)."""
    got = collections.Counter()
    with rec.recording() as seen:
        try:
            yield got
        finally:
            got.update(seen)


def _undecided_logging(seen):
    return sorted((k, p) for (s, k, p, d) in seen if s == "logging" and not d)


@pytest.fixture(autouse=True)
def sink():
    log_sink.install()                         # as tests/test_log_sink_typed.py::cap (idempotent)
    assert log_sink.installed()


@pytest.fixture
def hole(monkeypatch):
    """A planted real hole with every gate part ON: log judging enabled, extra scrub intact, but the template check
    bypassed - an unlisted template on a listed logger is WRITTEN."""
    assert log_sink._STATE["enabled"] is True and rec._log_gate_on(log_sink)
    monkeypatch.setattr(log_sink, "violation", lambda record: None)


def test_a_real_undecided_log_write_with_the_gate_on_is_still_recorded(hole):
    with _recorded() as seen:
        logging.getLogger("seo.api").error(UNLISTED, "x")
    assert ("seo.api", "/template") in _undecided_logging(seen)


def test_the_same_write_with_log_judging_disabled_is_not_recorded(hole, monkeypatch):
    monkeypatch.setitem(log_sink._STATE, "enabled", False)       # = mutation logging_filter_removed / load-bearing
    with _recorded() as seen:
        logging.getLogger("seo.api").error(UNLISTED, "x")
        logging.getLogger("seo.test").error("raw %s", PROSE)
        logging.getLogger("uvicorn.access").info('%s "%s"', "127.0.0.1:5", "GET /p")
    assert [r for r in seen if r[0] == "logging"] == []
    monkeypatch.setitem(log_sink._STATE, "enabled", True)        # back on inside the same test: recorded again
    with _recorded() as seen:
        logging.getLogger("seo.api").error(UNLISTED, "x")
    assert ("seo.api", "/template") in _undecided_logging(seen)


def test_a_swapped_extra_scrub_is_gate_off_and_not_recorded(monkeypatch):
    monkeypatch.setattr(log_sink, "_scrub_extra", lambda record: None)   # = test_review_2b26d980 mutation
    assert not rec._log_gate_on(log_sink)
    with _recorded() as seen:
        logging.getLogger("seo.test").info("noted", extra={"client_note": PROSE})
    assert [r for r in seen if r[0] == "logging"] == []


def test_client_note_on_a_refused_record_is_neither_emitted_nor_recorded_with_the_gate_on():
    """classes.md B row 5: the EMITTED line of a refused record with extra client_note carries no client_note (so no
    src bug, V-1), and the recorder records the typed log_refused record without /extra/client_note."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s %(args)s"))
    logger = logging.getLogger("seo.api")
    logger.addHandler(handler)
    try:
        with _recorded() as seen:
            logger.error("unexpected %s", "ValueError", extra={"client_note": PROSE})
            handler.handle(logging.makeLogRecord({"name": "seo.api", "levelno": 40, "msg": "unexpected %s",
                                                  "args": ("ValueError",), "client_note": PROSE}))
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    lines = out.splitlines()
    assert len(lines) == 2 and all(ln.startswith(sf.LOG_REFUSED + " ") for ln in lines), lines
    assert PROSE not in out and "client_note" not in out
    paths = {p for (s, k, p, d) in seen if s == "logging" and k == sf.LOG_REFUSED}
    assert paths and not any(p.startswith("/extra/") for p in paths), paths
    assert _undecided_logging(seen) == []


def test_paused_records_nothing_and_nests(hole):
    with _recorded() as seen:
        with rec.paused():
            with rec.paused():
                logging.getLogger("seo.api").error(UNLISTED, "x")
            logging.getLogger("seo.api").error(UNLISTED, "y")
            rec.observe("seo_log", "fclrec_planted", ["/planted"])
        assert list(rec.SEEN) == [] and rec.STATE["paused"] == 0
        rec.observe("seo_log", "fclrec_planted", ["/planted"])              # out of the block: recorded again
        logging.getLogger("seo.api").error(UNLISTED, "z")
    assert ("seo_log", "fclrec_planted", "/planted", False) in seen
    assert ("seo.api", "/template") in _undecided_logging(seen)


def test_paused_is_released_on_error():
    with pytest.raises(RuntimeError):
        with rec.paused():
            raise RuntimeError("synthetic")
    assert rec.STATE["paused"] == 0
