"""C2-4b (core-spec/2 T-3..T-5; lead ruling 17:50 PT F4-2 / F4-3): the T-4 recorder records what is WRITTEN.

F4-2: log_sink writes a typed record with an exception as ``<listed template> [TypeName]`` and clears the exception;
      a later pass (logger filter, Handler.handle) sees only that. The recorder keeps the template and exception the
      record was judged with: the written record is decided (/template + /exc_type); a real hole (an unlisted
      template, or a message that is not exactly the judged template [+ " [TypeName]"]) stays undecided (planted).
F4-3: outbound.check parses a bytes / str body before judging it; the recorder now records the parsed leaves (it
      recorded path '' for every bytes body). A body that is not a typed document (scalar, empty, list, null) is
      refused whole by outbound.violation (path '', reason untyped); a bypassed check letting one through is flagged.
"""
import logging

import pytest
import t4_recorder as rec

import log_sink
import outbound
import sink_fields as sf

URL = "https://api.openai.com/v1/responses"


@pytest.fixture
def cap(caplog):
    log_sink.install()
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    return caplog


def _rows(seen, sink, kind):
    return {(p, d) for (s, k, p, d) in seen if s == sink and k == kind}


def test_a_typed_exception_record_is_recorded_as_its_listed_template_and_exc_type(cap):
    log = logging.getLogger("seo.api")
    with rec.recording() as seen:
        try:
            raise ValueError("t4 synthetic")
        except ValueError:
            log.exception("unexpected %s", "ValueError")        # factory + logger filter + root filter passes
        got, rows = rec.undecided(), _rows(seen, "logging", "seo.api")
    assert got == [], got
    assert rows == {("/template", True), ("/args/0", True), ("/exc_type", True)}, rows
    assert cap.records[-1].getMessage() == "unexpected ValueError [ValueError]"     # what was written


def test_a_typed_record_without_exception_is_decided_and_has_no_exc_type(cap):
    with rec.recording() as seen:
        logging.getLogger("seo.api").error("unexpected %s", "KeyError")
        got, rows = rec.undecided(), _rows(seen, "logging", "seo.api")
    assert got == [] and rows == {("/template", True), ("/args/0", True)}, rows


@pytest.mark.parametrize("msg, exc", [
    ("t4 planted %s", True),                     # an unlisted template with an exception
    ("unexpected %s [ValueError]", False),       # a listed-looking suffix with no exception judged
    ("unexpected %s [ValueError]", True),        # the suffix twice would be written
])
def test_planted_unlisted_written_templates_stay_undecided(monkeypatch, msg, exc):
    monkeypatch.setattr(log_sink, "violation", lambda r: None)             # a bypassed log check (real hole)
    info = None
    if exc:
        try:
            raise ValueError("t4 synthetic")
        except ValueError as e:
            info = (type(e), e, e.__traceback__)
    record = logging.LogRecord("seo.api", logging.ERROR, __file__, 1, msg, ("ValueError",), info)
    with rec.recording():
        log_sink.scrub_record(record)
        log_sink.scrub_record(record)                                       # a second pass (filter / handle)
        got = rec.undecided()
    assert got == [("logging", "seo.api", "/template")], got


def test_a_refused_record_is_recorded_as_the_typed_log_refused_record(cap):
    with rec.recording() as seen:
        logging.getLogger("seo.api").error("t4 not listed %s", "x")
        got, rows = rec.undecided(), _rows(seen, "logging", sf.LOG_REFUSED)
    assert got == [] and ("/template", True) in rows, rows


@pytest.mark.parametrize("body", [b'"t4 scalar"', b"5", b"null", b"{}", b"[]", b'[{"model": "m"}]', "true",
                                  {}, [], None, "t4 not json", b"\xff"])
def test_a_body_that_is_not_a_typed_document_is_refused_whole(body):
    before = outbound.COUNTS[("openai", "untyped")]
    assert outbound.violation("openai", URL, body) == ("", "untyped")
    assert outbound.check("openai", URL, body) == "untyped"
    assert outbound.COUNTS[("openai", "untyped")] == before + 1


def test_a_bytes_body_is_recorded_by_its_parsed_leaves(monkeypatch):
    monkeypatch.setattr(outbound, "violation", lambda *a: None)            # bypassed: only the recording is judged
    with rec.recording() as seen:
        outbound.check("openai", URL, b'{"model": "m", "store": false}')
        outbound.check("openai", URL, '{"include": ["x"]}')
        got, rows = rec.undecided(), _rows(seen, "outbound", "openai")
    assert got == [] and rows == {("/model", True), ("/store", True), ("/include[]", True)}, rows


@pytest.mark.parametrize("body", [b'"t4 scalar"', b"{}", {"planted_extra": 1}])
def test_planted_non_documents_let_through_a_bypassed_check_are_flagged(monkeypatch, body):
    monkeypatch.setattr(outbound, "violation", lambda *a: None)
    with rec.recording():
        assert outbound.check("openai", URL, body) is None
        got = rec.undecided()
    path = "/planted_extra" if isinstance(body, dict) else ""
    assert got == [("outbound", "openai", path)], got
