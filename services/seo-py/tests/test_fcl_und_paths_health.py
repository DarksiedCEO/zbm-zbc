"""FCL-PATHS (lead 21:05 PT) + FCL-UND /health (lead ruling 19:05 PT FCL-Q1). Synthetic values only.

* FCL-PATHS: ``svc_audits._check_paths`` with a non-str element at any depth (``[["/"]]``, a dict, bytes, a number)
  is PAGES_INVALID (422), never a TypeError from ``set()`` (a 500 on malformed input).
* FCL-Q1: the unauthenticated /health 503 body {"status": "closed"} has no ``detail``; it is typed in the sink table
  as (error, health, /status) = a code enum of the service's status values, and the T-4 recorder tags a detail-less
  error body as kind ``health``. No API change.
"""
from __future__ import annotations

import pytest

import sink_fields as sf
import svc_audits
import t4_recorder
from errors import Invalid
from helpers import Harness

MALFORMED = [
    [["/"]], [[["/"]]], [[[["/"]]]], ["/", ["/"]], [{"p": "/"}], ["/", {"p": "/"}], [("/",)], [b"/"], [1], [None],
    [1.5], [True], [["/", "/about"]], [[], "/"],
]


@pytest.mark.parametrize("paths", MALFORMED, ids=repr)
def test_check_paths_non_str_element_at_any_depth_is_pages_invalid(paths):
    with pytest.raises(Invalid) as ei:
        svc_audits._check_paths(paths, 5)
    assert ei.value.reason == "PAGES_INVALID"


@pytest.mark.parametrize("paths", MALFORMED[:6], ids=repr)
def test_check_paths_non_str_wins_even_over_too_many_pages(paths):
    with pytest.raises(Invalid) as ei:                  # max_pages 0: still a 422 PAGES_INVALID, never a TypeError
        svc_audits._check_paths(paths * 3, 0)
    assert ei.value.reason == "PAGES_INVALID"


@pytest.mark.parametrize("paths", ["/", {"p": "/"}, None, 7])
def test_check_paths_non_list_container_is_pages_invalid(paths):
    with pytest.raises(Invalid) as ei:
        svc_audits._check_paths(paths, 5)
    assert ei.value.reason == "PAGES_INVALID"


def test_check_paths_unchanged_for_str_lists():
    svc_audits._check_paths(["/", "/about"], 5)
    svc_audits._check_paths(("/",), 5)
    for paths, code in ((["/a", "/b", "/c"], "AUDIT_TOO_LARGE"), ([], "PAGES_INVALID"), (["/", "/"], "PAGES_INVALID"),
                        (["//evil.example/x"], "PAGES_INVALID")):
        with pytest.raises(Invalid) as ei:
            svc_audits._check_paths(paths, 2)
        assert ei.value.reason == code, paths


def test_health_status_row_is_a_closed_code_enum():
    row = sf.SINK_FIELDS[("error", "health", "/status")]
    assert row.types == {"code"}
    for st in ("ok", "degraded", "closed"):
        assert sf.matches(row, st, "status")
    for bad in ("open", "CLOSED", "closed now", "x" * 12, 1, None):
        assert not sf.matches(row, bad, "status"), bad
    assert "health" not in sf.ERROR_KWARGS                  # not a reason code: error_body never builds it


def test_closed_health_503_is_recorded_decided(tmp_path):
    h = Harness(tmp_path / "h")
    with t4_recorder.recording() as seen:
        assert h.client.get("/health").status_code == 200   # 200 bodies are not the error sink
        h.svc.close()
        r = h.client.get("/health")
        assert r.status_code == 503 and r.json() == {"status": "closed"}
        rows = {(s, k, p, d) for (s, k, p, d) in seen}
    # (a prebuilt BODY_TOO_LARGE body of the app is also rendered on first use; only the health rows are this test's)
    assert {x for x in rows if x[1] == "health"} == {("error", "health", "/status", True)}
    assert all(d for (_s, _k, _p, d) in rows)


def test_a_detail_less_body_with_another_field_stays_undecided():
    with t4_recorder.recording() as seen:                   # planted: kind health, an off-table path -> undecided
        from fastapi.responses import JSONResponse
        JSONResponse(status_code=503, content={"status": "closed", "planted_extra": 1})
        rows = {(s, k, p, d) for (s, k, p, d) in seen}
    assert ("error", "health", "/planted_extra", False) in rows and ("error", "health", "/status", True) in rows
