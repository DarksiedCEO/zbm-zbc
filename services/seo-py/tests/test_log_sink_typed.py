"""C2-3a: the logging sink is typed (core-spec/2 T-3 / T-5; lead ruling F3-1 of 2026-11-14 14:58 PT; Wave-1 D-04).

Own loggers: written as they are only with a listed literal template and T-1 typed arguments; anything else is NOT
written and becomes the typed counter record ``log_refused`` {logger, reason}. uvicorn.access / httpx / httpcore
(and asyncio / py.warnings, no typed table) are not emitted at all (D-04 drop, not SINK_REFUSED). No WITHHELD."""

from __future__ import annotations

import ast
import logging
import pathlib

import pytest

import log_sink
import sink_fields as sf
import sink_guard as sg

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}
PROSE = "the client wrote two words here"          # synthetic prose (whitespace: no T-1 class)

# each call holds its template as a literal of this module's code, as an own call site does
CALLS = {
    "seo.api": lambda *a: logging.getLogger("seo.api").error("unexpected %s", *a),
    "seo.vault": lambda *a: logging.getLogger("seo.vault").warning(
        "vault: the final line was repaired at open (%d bytes removed, %d records removed, %d records kept)", *a),
    "seo.sink": lambda *a: logging.getLogger("seo.sink").error(
        "commit refused by the sink guard: sink=%s paths=%d first=%s", *a),
    "seo.audits/run": lambda *a: logging.getLogger("seo.audits").error("audit run failed: %s in %s at %s", *a),
    "seo.audits/report": lambda *a: logging.getLogger("seo.audits").error("audit report not stored: %s", *a),
    "seo.citations": lambda *a: logging.getLogger("seo.citations").error("citation run failed: %s at %s", *a),
    "seo.gsc": lambda *a: logging.getLogger("seo.gsc").error("search_performance read failed: %s", *a),
    "seo.schedules": lambda *a: logging.getLogger("seo.schedules").warning("drift not recorded: %s", *a),
}
TYPED = {
    "seo.api": ("ValueError",), "seo.vault": (12, 1, 40), "seo.sink": ("ledger.job_ran", 1, "content.prompts.list"),
    "seo.audits/run": ("RuntimeError", "selene", "svc_audits:_run_audit:12"), "seo.audits/report": ("OSError",),
    "seo.citations": ("RuntimeError", "svc_citations:_run:160"), "seo.gsc": ("TimeoutError",),
    "seo.schedules": ("SCHEDULE_NOT_FOUND",),
}


@pytest.fixture()
def cap(caplog):
    log_sink.install()
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    return caplog


def _own_call_sites():
    """(logger, template) of every logging call in src whose logger is a literal ``logging.getLogger("seo...")``."""
    sites = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) and ast.unparse(node.value.func) == "logging.getLogger" \
                    and node.value.args and isinstance(node.value.args[0], ast.Constant):
                names[node.targets[0].id] = node.value.args[0].value
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LEVELS):
                continue
            owner, logger = node.func.value, None
            if isinstance(owner, ast.Name) and owner.id in names:
                logger = names[owner.id]
            elif isinstance(owner, ast.Call) and ast.unparse(owner.func) == "logging.getLogger" and owner.args \
                    and isinstance(owner.args[0], ast.Constant):
                logger = owner.args[0].value
            if logger is None:
                continue
            tmpl = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
            sites.append((f"{path.name}:{node.lineno}", logger, tmpl, len(node.args) - 1, node.keywords))
    return sites


def test_static_every_own_call_site_has_a_listed_literal_template_and_typed_args():
    sites = _own_call_sites()
    assert len(sites) == 12, sites                                     # the 12 own call sites of the inventory
    seen = {}
    for where, logger, tmpl, nargs, keywords in sites:
        assert type(tmpl) is str, where                                # /template: a literal constant (static)
        assert tmpl in sf.LOG_TEMPLATES[logger], where
        assert not [k for k in keywords if k.arg == "extra"], where    # no /extra/<name> is typed
        for i in range(nargs):
            assert ("logging", logger, f"/args/{i}") in sf.SINK_FIELDS, (where, i)
        assert tmpl.count("%") == nargs, where                       # every argument has its /args/<i>
        seen.setdefault(logger, set()).add(tmpl)
    assert {k: set(v) for k, v in sf.LOG_TEMPLATES.items() if k != sf.LOG_REFUSED} == seen   # nothing listed unused


def test_the_logging_table_is_typed_by_the_closed_list_only():
    rows = {k: f for k, f in sf.SINK_FIELDS.items() if k[0] == "logging"}
    assert {k[1] for k in rows} == set(sf.LOG_TEMPLATES)
    for (_s, _kind, path), field in rows.items():
        assert path == "/exc_type" or path.startswith("/args/"), path
        assert field.types <= frozenset(sf.TYPES) and (len(field.types) > 1) is ((_s, _kind, path) in sf.UNIONS)
    assert not [k for k in rows if k[2].startswith(("/extra/", "/template"))]


@pytest.mark.parametrize("site", sorted(CALLS))
def test_each_own_call_site_with_typed_arguments_is_written_as_is(cap, site):
    CALLS[site](*TYPED[site])
    (rec,) = cap.records
    logger = site.split("/")[0]
    assert rec.name == logger and rec.getMessage() == rec.msg % TYPED[site]
    assert sg.WITHHELD not in cap.text


@pytest.mark.parametrize("site", sorted(CALLS))
def test_each_own_call_site_with_a_prose_argument_is_not_written(cap, site):
    before = log_sink.COUNTS["refused"][(site.split("/")[0], "field_type")]
    args = (PROSE,) + TYPED[site][1:]
    CALLS[site](*args)
    (rec,) = cap.records
    assert rec.name == sf.LOG_REFUSED and rec.msg == log_sink.REFUSED_TEMPLATE
    assert rec.args == (site.split("/")[0], "field_type")
    assert PROSE not in cap.text and "two words" not in cap.text and sg.WITHHELD not in cap.text
    assert log_sink.COUNTS["refused"][(site.split("/")[0], "field_type")] == before + 1


def test_a_long_number_a_float_and_an_object_are_refused_never_rewritten(cap):
    CALLS["seo.vault"](123456789, 1, 2)                               # 9 digits: outside `number`
    CALLS["seo.vault"](1.5, 1, 2)                                      # a float: the field is not numeric
    CALLS["seo.api"]({"k": "v"})                                     # a single mapping: /args/<key> untyped
    assert [r.args[1] for r in cap.records] == ["field_type", "field_type", "untyped"]
    assert "123456789" not in cap.text and "1.5" not in cap.text


def test_a_template_built_at_run_time_is_refused(cap):
    name = "ValueError"
    logging.getLogger("seo.api").error(f"unexpected {name}")
    logging.getLogger("seo.api").error("".join(("unexpected ", "%s")), name)   # equal text, built at run time
    logging.getLogger("seo.api").error("another literal %s", name)     # a literal, but not a listed template
    assert [r.args for r in cap.records] == [("seo.api", "template")] * 3


def test_an_unknown_logger_is_untyped_and_refused(cap):
    logging.getLogger("seo.test").error("user said %s", PROSE)
    logging.getLogger("uvicorn.error").error("Exception in ASGI application\n")
    logging.getLogger().warning("root %s", "x")
    assert [r.args for r in cap.records] == [("seo.test", "untyped"), ("uvicorn.error", "untyped"),
                                             ("root", "untyped")]
    assert PROSE not in cap.text


def test_an_extra_attribute_refuses_the_record_and_is_removed(cap):
    logging.getLogger("seo.api").error("unexpected %s", "ValueError", extra={"client_note": PROSE, "n": 3})
    (rec,) = cap.records
    assert rec.args == ("seo.api", "untyped") and "client_note" not in rec.__dict__ and "n" not in rec.__dict__
    fmt = logging.Formatter("%(name)s %(message)s").format(rec)
    assert PROSE not in fmt and fmt == "log_refused log_refused logger=seo.api reason=untyped"


def test_an_exception_keeps_its_type_name_only(cap):
    try:
        raise ValueError(PROSE)
    except ValueError:
        CALLS["seo.api"]("ValueError")
        logging.getLogger("seo.api").exception("unexpected %s", "ValueError")
    assert cap.records[-1].getMessage() == "unexpected ValueError [ValueError]"
    assert PROSE not in cap.text and "Traceback" not in cap.text


@pytest.mark.parametrize("name", ["uvicorn.access", "httpx", "httpcore", "httpcore.http11", "asyncio", "py.warnings"])
def test_third_party_d04_lines_are_not_emitted_at_all(cap, tmp_path, name):
    path = tmp_path / "process.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    root = logging.getLogger()
    root.addHandler(handler)
    before = log_sink.COUNTS["dropped"][name]
    refused = sum(log_sink.COUNTS["refused"].values())
    try:
        logging.getLogger(name).warning('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", "/p?q=" + PROSE, "1.1", 200)
        handler.handle(logging.makeLogRecord({"name": name, "levelno": 30, "msg": "raw " + PROSE}))
    finally:
        root.removeHandler(handler)
        handler.close()
    assert path.read_text() == "" and [r for r in cap.records if r.name in (name, sf.LOG_REFUSED)] == []
    assert log_sink.COUNTS["dropped"][name] == before + 2
    assert sum(log_sink.COUNTS["refused"].values()) == refused       # a D-04 drop is not a refusal


def test_a_hand_made_record_is_refused_when_a_handler_takes_it(cap, tmp_path):
    path = tmp_path / "p.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    try:
        handler.handle(logging.makeLogRecord({"name": "seo.api", "levelno": 40, "msg": "unexpected %s",
                                              "args": (PROSE,), "client_note": PROSE}))
    finally:
        handler.close()
    assert path.read_text() == "log_refused logger=seo.api reason=untyped\n"


def test_the_counter_record_is_itself_typed_and_no_withheld_is_left(cap):
    logging.getLogger("seo.test").error("contact %s", PROSE)
    (rec,) = cap.records
    assert rec.msg in sf.LOG_TEMPLATES[sf.LOG_REFUSED] and rec.args[1] in sf.LOG_REASONS
    for i, a in enumerate(rec.args):
        assert sf.matches(sf.SINK_FIELDS[("logging", sf.LOG_REFUSED, f"/args/{i}")], a)
    src = (SRC / "log_sink.py").read_text(encoding="utf-8")
    assert "WITHHELD, None" not in src and "[arguments" not in src       # the v1 substitution is gone (T-5)
