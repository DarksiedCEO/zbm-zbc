"""
Fix wave 25 — AEGIS round 24 findings (Oct 1, 2026), delivery (H8).

N24-D-1  `_closed_admissions` (the recorded answer of every admission refused after its RED check ran, or cancelled
         while it ran) was an unbounded map, re-materialised from the local log at every start. It is now kept like
         `idem`: the most recent CLOSED_ADMISSIONS_MAX, oldest first out, at start too. A cancel made while the
         admission's containers run is honoured when they end whatever the bounded map has forgotten meanwhile; an
         answer that has gone out of it is not needed for safety — a replay of that admission runs its RED check
         again under the same admission id and is refused (its crossings collide with the first attempt's records).
         The log itself is not compacted: its lines are anchored in the evidence ledger, like every other record's.
N24-D-2  a binary file under src was "shown" in the report's complete source diff as "Binary files ... differ" —
         its content never — while the header said every source file was there "in full". A binary change under
         src now fails the round (`binary_src_change`), and a source diff that still carries content git cannot show
         as text (a binary, a submodule pointer) fails the run before any report is written.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

from helpers import Harness, finding, flat, rid, scenario_s1, two_findings, write_test
from test_round23 import _p1, _states, _whys
from test_round23 import DONE
from test_round24 import HANG_PATH, HANG_RT, OLD

from zbm_delivery import service as SV
from zbm_delivery.engine import srcdiff
from helpers import replace


# ====================================================================== N24-D-1: bounded closed admissions

def test_closed_admission_answers_are_bounded_like_the_idempotency_records(monkeypatch):
    """Applying more admission_closed records than the cap (the start-up replay of the local log does exactly this)
    keeps the most recent CLOSED_ADMISSIONS_MAX. b51f307: a plain dict, every one kept."""
    monkeypatch.setattr(SV, "CLOSED_ADMISSIONS_MAX", 3, raising=False)
    h = Harness(scenario=[])
    try:
        for i in range(10):
            h.svc._apply("admission_closed", {"admission_id": f"adm-{i}", "status": 409, "reason": "r", "body": {}})
        assert list(h.svc._closed_admissions) == ["adm-7", "adm-8", "adm-9"], list(h.svc._closed_admissions)
        h.svc._apply("admission_closed", {"admission_id": "adm-8", "status": 409, "reason": "r", "body": {}})
        assert list(h.svc._closed_admissions) == ["adm-7", "adm-9", "adm-8"]       # the most recent use kept
    finally:
        h.close()


def test_a_cancel_made_while_the_red_check_runs_is_honoured_even_when_the_map_keeps_nothing(monkeypatch):
    """The cap at its extreme (0: no answer is kept): the operator's cancel still refuses the review whose containers
    were running — the bounded map is a convenience for replays, never what enforces the cancel."""
    monkeypatch.setattr(SV, "CLOSED_ADMISSIONS_MAX", 0, raising=False)
    h = Harness(scenario=scenario_s1() + scenario_s1())
    try:
        run1 = h.submit(two_findings(h.base_sha)).json()["run_id"]
        assert h.run(run1)["status"] == "awaiting_review"
        nf = finding("N9-1", line=15, class_hint="argument_validation",
                     reproduction=f"run {HANG_PATH}::test_clamp_hangs: clamp(5, 3, 0) answers 3", expected="ValueError",
                     observed="3", reproduction_test={"path": HANG_PATH, "content": HANG_RT})
        body = {"request_id": rid(), "review_ref": "r25-h8", "sha256": "d" * 64, "verdict": "fail", "reopened": [],
                "new_findings": [nf]}
        res = {}
        th = threading.Thread(target=lambda: res.setdefault("a", h.post(f"/dlv/v1/fix-runs/{run1}/review", body)))
        th.start()
        t0 = time.monotonic()
        while not h.events("reproduction_red_check_started") and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        adm = h.events("reproduction_red_check_started")[0]["payload"]["admission_id"]
        rc = h.post(f"/dlv/v1/fix-runs/{adm}/cancel", {"request_id": rid(), "reason": "stop the pending review"},
                    caller="andre_session")
        assert rc.status_code == 200, rc.text
        assert adm not in h.svc._closed_admissions                    # nothing kept by the map
        th.join(120)
        a = res["a"]
        assert a.status_code == 409 and "cancelled" in a.text, a.text
        h.svc.wait_idle(240)
        assert not h.events("fix_run_reviewed")
        assert not [r for r in h.svc.runs.values() if "N9-1" in (r.get("finding_ids") or [])]
        assert not h.svc._cancelled_running                           # and nothing left behind
    finally:
        h.svc.wait_idle(240)
        h.close()


def test_a_replay_of_an_admission_whose_answer_is_no_longer_kept_is_refused_never_admitted(monkeypatch):
    """The safety the bound leans on, shown: a refused admission's answer pushed out of the map, the same review
    replayed — its RED check runs again under the same admission id and it is refused again; nothing admitted."""
    monkeypatch.setattr(SV, "CLOSED_ADMISSIONS_MAX", 0, raising=False)
    green = ("from toy import calc\n\n\ndef test_clamp_is_green_on_base():\n"
             "    assert calc.clamp(5, 3, 0) == 3\n")
    h = Harness(scenario=scenario_s1())
    try:
        run1 = h.submit(two_findings(h.base_sha)).json()["run_id"]
        assert h.run(run1)["status"] == "awaiting_review"
        nf = finding("N9-3", line=15, class_hint="argument_validation",
                     reproduction=f"run {HANG_PATH}::test_clamp_is_green_on_base: clamp(5, 3, 0) answers 3",
                     expected="ValueError", observed="3", reproduction_test={"path": HANG_PATH, "content": green})
        body = {"request_id": rid(), "review_ref": "r25-h8b", "sha256": "e" * 64, "verdict": "fail", "reopened": [],
                "new_findings": [nf]}
        first = h.post(f"/dlv/v1/fix-runs/{run1}/review", body)
        assert first.status_code == 422 and first.json()["code"] == "reproduction_not_red", first.text
        assert not h.svc._closed_admissions
        again = h.post(f"/dlv/v1/fix-runs/{run1}/review", body)
        assert again.status_code in (409, 422, 503), again.text
        h.svc.wait_idle(240)
        assert h.run(run1)["status"] == "awaiting_review" and not h.events("fix_run_reviewed")
        assert not [r for r in h.svc.runs.values() if "N9-3" in (r.get("finding_ids") or [])]
    finally:
        h.svc.wait_idle(240)
        h.close()


# ====================================================================== N24-D-2: nothing un-showable reaches a report

def test_binary_and_submodule_changes_are_what_a_diff_cannot_show(tmp_path):
    """Against a real git repository: a binary file added and changed, a submodule pointer, a text change — only the
    text change is shown as text; the others are named."""
    def git(*a):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "protocol.file.allow=always",
                               *a], cwd=tmp_path, check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "src" / "blob.bin").write_bytes(b"\x00\x01\x02")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").strip()
    (tmp_path / "src" / "a.py").write_text("x = 2\n")
    (tmp_path / "src" / "blob.bin").write_bytes(b"\x00\x01\x03")
    (tmp_path / "src" / "new.bin").write_bytes(b"PK\x03\x04\x00\x00")
    git("add", "-A")
    git("update-index", "--add", "--cacheinfo", f"160000,{base},src/vendored")     # a submodule pointer (gitlink)
    git("commit", "-qm", "change")
    diff = git("diff", "--no-renames", "--no-color", "--no-ext-diff", base, "HEAD")
    assert srcdiff.binary_paths(diff) == ["src/blob.bin", "src/new.bin", "src/vendored (submodule)"], diff
    text_only = git("diff", "--no-renames", "--no-color", "--no-ext-diff", base, "HEAD", "--", "src/a.py")
    assert srcdiff.binary_paths(text_only) == []
    assert srcdiff.binary_paths('Binary files "a/src/\\303\\244.bin" and "b/src/\\303\\244.bin" differ\n') == [
        '"a/src/\\303\\244.bin" and "b/src/\\303\\244.bin"']      # a quoted name is never dropped
    assert srcdiff.is_binary_file(str(tmp_path / "src" / "new.bin"))
    assert not srcdiff.is_binary_file(str(tmp_path / "src" / "a.py"))


BINARY_FIX = flat([{"tool_calls": [{"name": "write_file", "args": {
    "path": "/mnt/user-data/workspace/services/toy-py/src/toy/table.bin", "content": "\x00\x01lookup\x00"}}]},
    replace("src/toy/calc.py", OLD, "    if whole == 0:\n        return 0.0\n" + OLD)])


def test_a_binary_file_written_under_src_fails_the_round():
    """b51f307: the binary went into the commit and the report showed "Binary files ... differ" under a header that
    said every source file was there in full."""
    h = Harness(scenario=_p1(BINARY_FIX), extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    try:
        run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
        h.svc.wait_idle(240)
        assert "binary_src_change" in _whys(h), _whys(h)
        st = _states(h, run_id)
        assert st["N1-2"] not in DONE, st
        failed = [e["payload"] for e in h.events("round_failed") if e["payload"].get("why") == "binary_src_change"]
        assert failed and "src/toy/table.bin" in " ".join(failed[0]["paths"]), failed
    finally:
        h.close()


def test_the_report_header_says_exactly_what_the_diff_shows():
    from zbm_delivery.engine import report as RP
    lines = RP.source_diff_section({"src_diff_sha256": "0" * 64, "src_diff_evidence_id": "ev", "base_sha": "b" * 40},
                                   lambda ev: "diff --git a/x b/x\n")
    head = " ".join(lines[:3])
    assert "in full as text" in head and "binary_src_change" in head, head
