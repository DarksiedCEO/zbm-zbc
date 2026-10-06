"""AEGIS round 1 follow-up (Oct 6 2026, coordinator-approved): a submission whose outcome is unknown — a timeout, an
exception, an unknown or reference-less answer — stays ``sending``: never failed, never resubmittable, reconciled
through ``submission_status``; only an explicit refusal makes it resubmittable; past its deadline it opens a task
for Andre and is never auto-failed. Each test fails on 22fe413."""

from __future__ import annotations

from datetime import timedelta

from helpers import Harness, RecordingSubmission, rid, wired_ports
from ports import NotWiredSubmission


def _queued(tmp_path, sub):
    h = Harness(tmp_path, ports=wired_ports(submission=sub))
    p = h.pursuit()
    h.bid(p["pursuit_id"])
    r = h.ready_response(p["pursuit_id"])
    s = h.ok(h.submit(r), 201)
    return h, p, r, s


def test_timeout_leaves_sending_and_forbids_resubmit(tmp_path):
    h, p, r, s = _queued(tmp_path, RecordingSubmission(TimeoutError("answer lost")))
    out = h.ok(h.job("submission-queue"))
    assert out["unknown"] == 1 and "failed" not in out
    assert h.ok(h.get("/submissions"))[0]["status"] == "sending"
    h.refused(h.submit(r), 409, "RESPONSE_ALREADY_QUEUED")
    h.ok(h.job("submission-queue"))
    assert len(h.ports.submission.calls) == 1                     # never resent
    assert h.ports.submission.reconciled == [s["submission_id"]]


def test_unknown_answers_are_not_failures(tmp_path):
    for i, answer in enumerate(("failed", "unavailable", "not_wired", "accepted_no_ref")):
        sub = RecordingSubmission(answer)
        if answer == "accepted_no_ref":
            sub.status = "accepted"
            sub.submit = lambda *a, _s=sub: (_s.calls.append(a), __import__("ports").SendResult("accepted", None))[1]
        h, p, r, s = _queued(tmp_path / str(i), sub)
        h.ok(h.job("submission-queue"))
        assert h.ok(h.get("/submissions"))[0]["status"] == "sending", answer
        h.refused(h.submit(r), 409, "RESPONSE_ALREADY_QUEUED")


def test_only_an_explicit_refusal_is_resubmittable(tmp_path):
    h, p, r, s = _queued(tmp_path, RecordingSubmission("refused"))
    assert h.ok(h.job("submission-queue"))["refused"] == 1
    sub = h.ok(h.get("/submissions"))[0]
    assert sub["status"] == "refused" and sub["reason"] == "REFUSED_BY_RECIPIENT"
    h.ports.submission.status = "accepted"
    h.ok(h.submit(r), 201)
    assert h.ok(h.job("submission-queue"))["submitted"] == 1


def test_reconcile_resolves_sending(tmp_path):
    h, p, r, s = _queued(tmp_path, RecordingSubmission(TimeoutError("lost")))
    h.ok(h.job("submission-queue"))
    h.ports.submission.status_answer = ("accepted", "sub-ref-held")
    assert h.ok(h.job("submission-queue"))["submitted"] == 1
    assert h.ok(h.get(f"/pursuits/{p['pursuit_id']}"))["stage"] == "submitted"
    h2, p2, r2, s2 = _queued(tmp_path / "b", RecordingSubmission(TimeoutError("lost")))
    h2.ok(h2.job("submission-queue"))
    h2.ports.submission.status_answer = ("refused", None)
    assert h2.ok(h2.job("submission-queue"))["refused"] == 1
    h2.ports.submission.status = "accepted"
    assert h2.ok(h2.submit(r2), 201)


def test_status_call_that_raises_stays_unknown(tmp_path):
    h, p, r, s = _queued(tmp_path, RecordingSubmission(TimeoutError("lost")))
    h.ok(h.job("submission-queue"))

    def boom(sid):
        raise ConnectionError("down")
    h.ports.submission.submission_status = boom
    assert h.ok(h.job("submission-queue"))["unknown"] == 1
    assert h.ok(h.get("/submissions"))[0]["status"] == "sending"


def test_deadline_passing_while_sending_opens_one_task_never_fails(tmp_path):
    h, p, r, s = _queued(tmp_path, RecordingSubmission(TimeoutError("lost")))
    h.ok(h.job("submission-queue"))
    h.clock.advance(days=20)
    h.ok(h.job("submission-queue"))
    h.ok(h.job("deadline-sweep"))
    h.ok(h.job("submission-queue"))
    tasks = [t for t in h.ok(h.get("/tasks?status=open")) if t["kind"] == "submission_unknown"]
    assert len(tasks) == 1 and tasks[0]["code"] == "SUBMISSION_OUTCOME_UNKNOWN"
    assert h.ok(h.get("/submissions"))[0]["status"] == "sending"
    assert h.ledger.of_type("submission_outcome_unknown")


def test_deadline_sweep_alone_flags_a_sending_submission(tmp_path):
    h, p, r, s = _queued(tmp_path, RecordingSubmission(TimeoutError("lost")))
    h.ok(h.job("submission-queue"))
    h.clock.advance(days=20)
    assert h.ok(h.job("deadline-sweep"))["tasks_opened"] >= 1
    assert any(t["kind"] == "submission_unknown" for t in h.ok(h.get("/tasks?status=open")))
    assert h.ok(h.get("/submissions"))[0]["status"] == "sending"


def test_unknown_survives_restart_and_is_not_resent(tmp_path):
    sub = RecordingSubmission(TimeoutError("lost"))
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=wired_ports(submission=sub))
    p = h.pursuit()
    h.bid(p["pursuit_id"])
    r = h.ready_response(p["pursuit_id"])
    h.ok(h.submit(r), 201)
    h.ok(h.job("submission-queue"))
    h2 = h.restart()
    h2.ok(h2.job("submission-queue"))
    assert len(sub.calls) == 1 and h2.ok(h2.get("/submissions"))[0]["status"] == "sending"
    h2.refused(h2.submit(r), 409, "RESPONSE_ALREADY_QUEUED")


def test_stand_in_status_is_unknown():
    assert NotWiredSubmission().submission_status("x").status == "unknown"
    assert rid() and timedelta
