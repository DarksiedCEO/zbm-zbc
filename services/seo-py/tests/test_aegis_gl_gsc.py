"""AEGIS review of golive-gsc (0c9a96a): regressions for every finding fixed on this branch. Each test here fails on
0c9a96a. No network: the fake Google of gsc_fakes.py; time is an injected clock."""

from __future__ import annotations

import json
import logging

import pytest
from gsc_fakes import FakeGoogle
from test_gsc import PROP, W, audit, bind, client, code_of, gsc_harness, section

import gsc_client as g
from agents import search_performance as sp

BIG = "1" + "0" * 400                           # a 400-digit JSON integer: json.loads makes it an int


def _setup(tmp_path, fake, **env):
    h = gsc_harness(tmp_path, fake, **env)
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    return h


def _first_query_answers(fake, raw: bytes):
    armed = [True]

    def on(fk, req):
        if req.url.raw_path.decode().endswith("/searchAnalytics/query") and armed:
            armed.pop()
            fk.script.append((200, raw))
    fake.on_request = on


# ====================================================================== M1: malformed answers never crash the audit

def test_m1_permission_level_that_is_not_a_string_is_a_protocol_error():
    for level in (["siteOwner"], {"x": 1}, 7, None):
        fake = FakeGoogle()
        fake.sites = [{"siteUrl": PROP, "permissionLevel": level}]
        e = code_of(lambda f=fake: client(f).open_read(lambda: None).check_property(PROP))
        assert (e.code, e.detail) == ("GSC_PROTOCOL_ERROR", "site_entry"), level


def test_m1_token_error_that_is_not_a_string_is_a_protocol_error():
    for err in ({"code": 400, "message": "bad"}, ["invalid_grant"], 400):
        fake = FakeGoogle()
        fake.token_script = [(400, {"error": err})]
        e = code_of(lambda f=fake: client(f).open_read(lambda: None).token())
        assert (e.code, e.detail) == ("GSC_PROTOCOL_ERROR", "token_error_shape"), err
    fake = FakeGoogle()                         # the documented string form still maps to the closed set
    fake.token_script = [(400, {"error": "invalid_grant"})]
    assert code_of(lambda: client(fake).open_read(lambda: None).token()).code == "GSC_AUTH_REFUSED"


@pytest.mark.parametrize("field", ["clicks", "impressions", "ctr", "position"])
def test_m1_a_huge_integer_metric_is_a_protocol_error(field):
    row = {"keys": [W[1]], "clicks": 1, "impressions": 2, "ctr": 0.5, "position": 3.0}
    raw = json.dumps({"rows": [row]}).replace(f'"{field}": {row[field]}', f'"{field}": {BIG}').encode()
    fake = FakeGoogle()
    _first_query_answers(fake, raw)
    rd = client(fake).open_read(lambda: None)
    e = code_of(lambda: sp.read_aggregates(rd, PROP, *W, {"acme.test"}, ["acme"]))
    assert e.code == "GSC_PROTOCOL_ERROR" and e.detail in ("row_counts", "row_ctr", "row_position")


def test_m1_finite_bounds():
    assert g._finite(10 ** 15) and not g._finite(10 ** 15 + 1) and not g._finite(-(10 ** 400), lo=-1e300)
    assert not g._finite(int(BIG)) and not g._finite(float("inf")) and not g._finite(float("nan"))
    assert not g._finite(True) and g._finite(0.5, 0.0, 1.0)


def _token_dict(fake):
    fake.token_script = [(400, {"error": {"code": 400, "message": "bad"}})]


def _level_list(fake):
    fake.sites = [{"siteUrl": PROP, "permissionLevel": ["siteOwner"]}]


def _bigint(fake):
    _first_query_answers(fake, ('{"rows":[{"keys":["%s"],"clicks":%s,"impressions":2}]}' % (W[1], BIG)).encode())


@pytest.mark.parametrize("arm", [_token_dict, _level_list, _bigint], ids=["token_dict", "level_list", "bigint"])
def test_m1_the_audit_completes_with_the_section_failed(tmp_path, arm):
    fake = FakeGoogle()
    arm(fake)
    h = _setup(tmp_path, fake)
    view = audit(h, "acme", "acme.test")
    e = section(view)
    assert view["status"] == "completed" and view["report"] is not None
    assert (e["outcome"], e["reason"]) == ("FAILED", "GSC_PROTOCOL_ERROR")


def test_m1_any_other_exception_in_the_read_is_a_failed_section_and_only_its_type_is_logged(tmp_path, monkeypatch,
                                                                                         caplog):
    fake = FakeGoogle()
    h = _setup(tmp_path, fake)

    def boom(*a, **k):
        raise ValueError("GOOGLE-SAID jane.doe@example.com")
    monkeypatch.setattr(sp, "read_aggregates", boom)
    with caplog.at_level(logging.DEBUG):
        view = audit(h, "acme", "acme.test")
    e = section(view)
    assert view["status"] == "completed"
    assert (e["outcome"], e["reason"], e["facts"]["error_type"]) == ("FAILED", "GSC_PROTOCOL_ERROR", "ValueError")
    assert "GOOGLE-SAID" not in caplog.text and "ValueError" in caplog.text
    assert "GOOGLE-SAID" not in json.dumps(view) + json.dumps([ev["_payload"] for ev in h.ledger.events])
    assert h.ledger.of_type("gsc_read_recorded")[0]["_payload"]["reason"] == "GSC_PROTOCOL_ERROR"
