"""Boilerplate blocks, responses and pitches, Andre's approvals and the submission queue (ADR 0016 decisions 10-11).
A mixin of BizDevService.

Blocks and responses are versioned and immutable: a change is a new version, so any change after an approval is a
different content hash that no approval binds. Andre approves a block version, and each response version, by its
exact SHA-256 (and, for a response, by naming exactly the sensitivity flags it raises). A response is assembled
(i04) only from approved blocks plus custom text; the custom text is approved with the response. Submission needs
the CURRENT approved version whose hash, recomputed from current blocks, still matches, a ``bid`` decision, a
deadline not passed (stored deadline vs the injected clock), a complete government checklist and the deal gate;
it is queued, recorded on the ledger, and goes out through the submission port, which is not wired: approved
submissions stay ``queued`` and are re-checked (deadline included) every time the queue job looks at them."""

from __future__ import annotations

from typing import Optional

from errors import Conflict, NotFound, Throttled, Unavailable
from intelligences import i04_assembly, i06_sensitivity
from ledger import derived_id
from reasons import R


class ResponsesMixin:
    # ------------------------------------------------------------------------------------------------ replay

    def _a_block_created(self, d, at):
        self.blocks[d["block_id"]] = {"block_id": d["block_id"], "block_key": d["block_key"], "brand": d["brand"],
                                      "created_at": at, "versions": {}}
        self._a_block_version_added(d, at)

    def _a_block_version_added(self, d, at):
        self.blocks[d["block_id"]]["versions"][str(d["version"])] = {
            "version": d["version"], "title": d["title"], "text": d["text"], "content_sha256": d["content_sha256"],
            "status": "draft", "approved_sha256": None, "approved_at": None, "created_at": at}

    def _a_block_approved(self, d, at):
        v = self.blocks[d["block_id"]]["versions"][str(d["version"])]
        v.update(status="approved", approved_sha256=d["content_sha256"], approved_at=at)

    def _a_block_retired(self, d, at):
        self.blocks[d["block_id"]]["versions"][str(d["version"])]["status"] = "retired"

    def _a_response_version(self, d, at):
        r = self.responses.get(d["response_id"])
        if r is None:
            r = self.responses[d["response_id"]] = {"response_id": d["response_id"], "pursuit_id": d["pursuit_id"],
                                                    "brand": d["brand"], "created_at": at, "current": 0,
                                                    "versions": {}}
        old = r["versions"].get(str(r["current"]))
        if old is not None and old["status"] in ("draft", "approved"):
            old["status"] = "superseded"
        r["versions"][str(d["version"])] = {"version": d["version"], "parts": d["parts"], "doc": d["doc"],
                                            "content_sha256": d["content_sha256"], "flags": d["doc"]["flags"],
                                            "status": "draft", "approved_at": None, "created_at": at,
                                            "created_by": d["actor"]}
        r["current"] = d["version"]
        for sid in d.get("cancel_submissions", ()):
            self.submissions[sid].update(status="cancelled", reason="RESPONSE_SUPERSEDED", updated_at=at)

    def _a_response_approved(self, d, at):
        v = self.responses[d["response_id"]]["versions"][str(d["version"])]
        v.update(status="approved", approved_at=at, approved_sha256=d["content_sha256"])

    def _a_submission_queued(self, d, at):
        self.submissions[d["submission_id"]] = {
            **{k: d[k] for k in ("submission_id", "pursuit_id", "response_id", "version", "content_sha256")},
            "status": "queued", "reason": None, "queued_at": at, "updated_at": at, "queued_by": d["actor"],
            "provider_ref": None}

    def _a_submission_sending(self, d, at):
        self.submissions[d["submission_id"]].update(status="sending", updated_at=at)

    def _a_submission_result(self, d, at):
        s = self.submissions[d["submission_id"]]
        s.update(status=d["status"], provider_ref=d.get("provider_ref"), updated_at=at,
                 reason=None if d["status"] == "submitted" else "PROVIDER_FAILED")
        if d["status"] == "submitted":
            p = self.pursuits[s["pursuit_id"]]
            if p["stage"] == "responding":
                p.update(stage="submitted", updated_at=at)

    def _a_submission_cancelled(self, d, at):
        self.submissions[d["submission_id"]].update(status="cancelled", reason=d["reason"], updated_at=at)

    # ------------------------------------------------------------------------------------------------ blocks

    def block_view(self, b: dict) -> dict:
        return {**{k: b[k] for k in ("block_id", "block_key", "brand", "created_at")},
                "versions": [dict(v) for _, v in sorted(b["versions"].items(), key=lambda kv: int(kv[0]))]}

    def blocks_view(self) -> list[dict]:
        with self.lock:
            return [self.block_view(b) for b in self.blocks.values()][:2000]

    def block(self, block_id: str) -> dict:
        with self.lock:
            return self.block_view(self._get(self.blocks, block_id, "BLOCK_NOT_FOUND"))

    def create_block(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            bid = derived_id("blk", body["brand"], body["block_key"])
            rk = self.rk("block_create", bid, body)
            if self._idem(caller, rk, body):
                return self.block_view(self.blocks[bid])
            if bid in self.blocks:
                raise Conflict(R("BLOCK_EXISTS"))
            sha = i04_assembly.block_sha256(body["brand"], body["title"], body["text"])
            data = {"block_id": bid, "block_key": body["block_key"], "brand": body["brand"], "version": 1,
                    "title": body["title"], "text": body["text"], "content_sha256": sha}
            self._commit("block_created", self._req(data, caller, rk, body, bid), caller)
            return self.block_view(self.blocks[bid])

    def add_block_version(self, caller: str, block_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("block_version", block_id, body)
            if self._idem(caller, rk, body):
                return self.block_view(self.blocks[block_id])
            b = self._get(self.blocks, block_id, "BLOCK_NOT_FOUND")
            v = max(int(x) for x in b["versions"]) + 1
            sha = i04_assembly.block_sha256(b["brand"], body["title"], body["text"])
            data = {"block_id": block_id, "version": v, "title": body["title"], "text": body["text"],
                    "content_sha256": sha}
            self._commit("block_version_added", self._req(data, caller, rk, body, v), caller)
            return self.block_view(b)

    def approve_block(self, block_id: str, version: int, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("block_approve", f"{block_id}.{version}", body)
            if self._idem("andre", rk, body):
                return self.block_view(self.blocks[block_id])
            b = self._get(self.blocks, block_id, "BLOCK_NOT_FOUND")
            v = b["versions"].get(str(version))
            if v is None:
                raise NotFound(R("BLOCK_NOT_FOUND"))
            if v["status"] == "approved":
                raise Conflict(R("BLOCK_ALREADY_APPROVED"))
            if v["status"] != "draft":
                raise Conflict(R("BLOCK_NOT_APPROVED"))
            current = i04_assembly.block_sha256(b["brand"], v["title"], v["text"])
            if body["content_sha256"] != v["content_sha256"] or current != v["content_sha256"]:
                raise Conflict(R("BLOCK_HASH_MISMATCH"))
            data = {"block_id": block_id, "version": version, "content_sha256": current}
            self._commit("block_approved", self._req(data, "andre", rk, body, version), "andre",
                         evidence=("block_approved", f"block:{block_id}", data, ("andre", rk)))
            return self.block_view(b)

    def retire_block(self, block_id: str, version: int, body: dict) -> dict:
        """A retired version is never used again: a queued submission citing it is cancelled by the queue job."""
        with self.lock:
            self._gate()
            rk = self.rk("block_retire", f"{block_id}.{version}", body)
            if self._idem("andre", rk, body):
                return self.block_view(self.blocks[block_id])
            b = self._get(self.blocks, block_id, "BLOCK_NOT_FOUND")
            v = b["versions"].get(str(version))
            if v is None:
                raise NotFound(R("BLOCK_NOT_FOUND"))
            if v["status"] == "retired":
                raise Conflict(R("BLOCK_NOT_APPROVED"))
            data = {"block_id": block_id, "version": version}
            self._commit("block_retired", self._req(data, "andre", rk, body, version), "andre",
                         evidence=("block_retired", f"block:{block_id}", data, ("andre", rk)))
            return self.block_view(b)

    # ------------------------------------------------------------------------------------------------ responses

    def response_view(self, r: dict) -> dict:
        return {**{k: r[k] for k in ("response_id", "pursuit_id", "brand", "created_at", "current")},
                "versions": [{k: v[k] for k in ("version", "content_sha256", "flags", "status", "approved_at",
                                                "created_at", "created_by", "doc")}
                             for _, v in sorted(r["versions"].items(), key=lambda kv: int(kv[0]))]}

    def response(self, rid_: str) -> dict:
        with self.lock:
            return self.response_view(self._get(self.responses, rid_, "RESPONSE_NOT_FOUND"))

    def _assemble(self, p: dict, version: int, parts: list) -> dict:
        customs = [x["custom"] for x in parts if "custom" in x]
        flags = sorted(set(i06_sensitivity.flags(*customs)) | set(p["flags"]))
        try:
            return i04_assembly.assemble(p, version, parts, self.blocks, flags)
        except i04_assembly.AssemblyProblem as exc:
            raise (NotFound if exc.code == "BLOCK_NOT_FOUND" else Conflict)(R(exc.code)) from None

    def _response_version(self, caller: str, pid: str, rk: str, body: dict, version: int) -> dict:
        p = self._get(self.pursuits, pid, "PURSUIT_NOT_FOUND")
        if p["stage"] != "responding":
            raise Conflict(R("BID_DECISION_REQUIRED" if p["stage"] in ("identified", "qualifying")
                             else "PURSUIT_CLOSED" if p["stage"] not in ("submitted",) else "STAGE_NOT_ALLOWED"))
        parts = [dict(x) for x in body["parts"]]
        a = self._assemble(p, version, parts)
        resp_id = derived_id("rsp", pid)
        cancel = sorted(s["submission_id"] for s in self.submissions.values()
                        if s["response_id"] == resp_id and s["status"] == "queued")
        data = {"response_id": resp_id, "pursuit_id": pid, "brand": p["brand"], "version": version, "parts": parts,
                "doc": a["doc"], "content_sha256": a["content_sha256"], "cancel_submissions": cancel}
        self._commit("response_version", self._req(data, caller, rk, body, resp_id), caller,
                     evidence=[("submission_cancelled", f"submission:{sid}",
                                {"submission_id": sid, "reason": "RESPONSE_SUPERSEDED"}, (caller, rk, sid))
                               for sid in cancel] or None)
        return self.response_view(self.responses[resp_id])

    def create_response(self, caller: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            pid = body["pursuit_id"]
            resp_id = derived_id("rsp", pid)
            rk = self.rk("response_create", pid, body)
            if self._idem(caller, rk, body):
                return self.response_view(self.responses[resp_id])
            if resp_id in self.responses:
                raise Conflict(R("RESPONSE_EXISTS"))
            return self._response_version(caller, pid, rk, body, 1)

    def add_response_version(self, caller: str, resp_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("response_version", resp_id, body)
            if self._idem(caller, rk, body):
                return self.response_view(self.responses[resp_id])
            r = self._get(self.responses, resp_id, "RESPONSE_NOT_FOUND")
            if any(s["response_id"] == resp_id and s["status"] in ("sending", "submitted")
                   for s in self.submissions.values()):
                raise Conflict(R("RESPONSE_ALREADY_QUEUED"))
            return self._response_version(caller, r["pursuit_id"], rk, body, r["current"] + 1)

    def _version_problem(self, r: dict, version: int, sha: str, want: str) -> Optional[str]:
        """None when ``version`` is the current one, in status ``want``, its stored hash is ``sha`` and the document
        re-assembled from CURRENT blocks hashes to it; otherwise the refusal code."""
        if version != r["current"]:
            return "RESPONSE_SUPERSEDED"
        v = r["versions"][str(version)]
        if v["status"] != want:
            return "RESPONSE_NOT_DRAFT" if want == "draft" else "RESPONSE_NOT_APPROVED"
        if sha != v["content_sha256"]:
            return "RESPONSE_HASH_MISMATCH"
        p = self.pursuits[r["pursuit_id"]]
        customs = [x["custom"] for x in v["parts"] if "custom" in x]
        flags = sorted(set(i06_sensitivity.flags(*customs)) | set(p["flags"]))
        try:
            again = i04_assembly.assemble(p, version, v["parts"], self.blocks, flags)
        except i04_assembly.AssemblyProblem as exc:
            return exc.code
        if again["content_sha256"] != v["content_sha256"]:
            return "RESPONSE_HASH_MISMATCH"
        if want == "approved" and v.get("approved_sha256") != v["content_sha256"]:
            return "RESPONSE_NOT_APPROVED"
        return None

    def approve_response(self, resp_id: str, body: dict) -> dict:
        """Andre approves one version by its exact hash, naming exactly the sensitivity flags it raises."""
        with self.lock:
            self._gate()
            rk = self.rk("response_approve", resp_id, body)
            if self._idem("andre", rk, body):
                return self.response_view(self.responses[resp_id])
            r = self._get(self.responses, resp_id, "RESPONSE_NOT_FOUND")
            p = self.pursuits[r["pursuit_id"]]
            if p["stage"] != "responding":
                raise Conflict(R("PURSUIT_CLOSED" if p["stage"] in ("won", "lost", "no_bid", "withdrawn")
                                 else "STAGE_NOT_ALLOWED"))
            problem = self._version_problem(r, body["version"], body["content_sha256"], "draft")
            if problem:
                raise Conflict(R(problem))
            v = r["versions"][str(body["version"])]
            if sorted(body["acknowledged_flags"]) != sorted(v["flags"]):
                raise Conflict(R("FLAGS_NOT_ACKNOWLEDGED"))
            data = {"response_id": resp_id, "version": body["version"], "content_sha256": v["content_sha256"]}
            self._commit("response_approved", self._req(data, "andre", rk, body, resp_id), "andre",
                         evidence=("response_approved", f"response:{resp_id}",
                                   {**data, "pursuit_id": r["pursuit_id"], "flags": v["flags"]}, ("andre", rk)))
            return self.response_view(r)

    # ------------------------------------------------------------------------------------------------ submissions

    def submissions_view(self, status: Optional[str]) -> list[dict]:
        with self.lock:
            return [dict(s) for s in sorted(self.submissions.values(), key=lambda x: (x["queued_at"],
                                                                                      x["submission_id"]))
                    if status is None or s["status"] == status][:1000]

    def submit(self, caller: str, resp_id: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("submit", resp_id, body)
            prev = self._idem(caller, rk, body)
            if prev:
                return dict(self.submissions[prev[1]])
            r = self._get(self.responses, resp_id, "RESPONSE_NOT_FOUND")
            p = self.pursuits[r["pursuit_id"]]
            gate = self._pursuit_submit_problem(p)
            if gate:
                raise gate[0](R(gate[1]))
            problem = self._version_problem(r, body["version"], body["content_sha256"], "approved")
            if problem:
                raise Conflict(R(problem))
            if any(s["pursuit_id"] == p["pursuit_id"] and s["status"] in ("queued", "sending", "submitted")
                   for s in self.submissions.values()):
                raise Conflict(R("RESPONSE_ALREADY_QUEUED"))
            if sum(1 for s in self.submissions.values() if s["status"] == "queued" and s["queued_by"] == caller) \
                    >= self.settings.queue_max_per_caller:
                raise Throttled(R("QUEUE_FULL"))
            sid = derived_id("sub", caller, rk)
            data = {"submission_id": sid, "pursuit_id": p["pursuit_id"], "response_id": resp_id,
                    "version": body["version"], "content_sha256": body["content_sha256"]}
            self._commit("submission_queued", self._req(data, caller, rk, body, sid), caller,
                         evidence=("submission_queued", f"submission:{sid}", data, (caller, rk)))
            return dict(self.submissions[sid])

    def cancel_submission(self, caller: str, sid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            rk = self.rk("submission_cancel", sid, body)
            if self._idem(caller, rk, body):
                return dict(self.submissions[sid])
            s = self._get(self.submissions, sid, "SUBMISSION_NOT_FOUND")
            if s["status"] != "queued":
                raise Conflict(R("SUBMISSION_NOT_QUEUED"))
            data = {"submission_id": sid, "reason": "CANCELLED_BY_CALLER"}
            self._commit("submission_cancelled", self._req(data, caller, rk, body, sid), caller,
                         evidence=("submission_cancelled", f"submission:{sid}", data, (caller, rk)))
            return dict(s)

    def _submission_check(self, s: dict) -> tuple[Optional[str], Optional[str]]:
        """(cancel_code, hold_code) for one queued submission, from CURRENT state. A cancel is final; a hold
        (waiting on Andre's deal approval) leaves it queued."""
        p = self.pursuits[s["pursuit_id"]]
        r = self.responses[s["response_id"]]
        gate = self._pursuit_submit_problem(p)
        if gate and gate[1] == "DEAL_APPROVAL_REQUIRED":
            hold = gate[1]
        elif gate:
            return gate[1], None
        else:
            hold = None
        problem = self._version_problem(r, s["version"], s["content_sha256"], "approved")
        if problem:
            return problem, None
        return None, hold

    def submission_tick(self) -> dict:
        """The ``submission-queue`` job: one submission at a time. Under the lock every gate is re-checked from
        current state (the deadline against the injected clock); a send is recorded (typed ``submission_sending``
        event, then the log line) BEFORE the port is called, outside the lock; the outcome is recorded after. A
        submission whose outcome could not be recorded stays ``sending`` (never resent on its own)."""
        summary = {"submitted": 0, "failed": 0, "cancelled": 0, "held": 0, "not_wired": 0}
        with self.lock:
            self._gate()
            ids = [s["submission_id"] for s in sorted(self.submissions.values(),
                                                      key=lambda x: (x["queued_at"], x["submission_id"]))
                   if s["status"] == "queued"]
        for sid in ids:
            try:
                with self.lock:
                    self._gate()
                    s = self.submissions[sid]
                    if s["status"] != "queued":
                        continue
                    cancel, hold = self._submission_check(s)
                    if cancel:
                        data = {"submission_id": sid, "reason": cancel}
                        self._commit("submission_cancelled", data, "scheduler",
                                     evidence=("submission_cancelled", f"submission:{sid}", data, (sid, "tick", cancel)))
                        summary["cancelled"] += 1
                        continue
                    if hold:
                        summary["held"] += 1
                        continue
                    if not self.ports.submission.wired:
                        summary["not_wired"] += 1           # stays queued, visibly; nothing is recorded as sent
                        continue
                    r = self.responses[s["response_id"]]
                    text = i04_assembly.rendered_text(r["versions"][str(s["version"])]["doc"], self.blocks)
                    ev = {"submission_id": sid, "pursuit_id": s["pursuit_id"], "content_sha256": s["content_sha256"]}
                    self._commit("submission_sending", {"submission_id": sid}, "scheduler",
                                 evidence=("submission_sending", f"submission:{sid}", ev, (sid,)))
                    pid, sha = s["pursuit_id"], s["content_sha256"]
                try:
                    res = self.ports.submission.submit(sid, pid, sha, text)     # outside the lock
                    ok = res.status == "accepted"
                    ref = res.provider_ref if ok else None
                except Exception:      # noqa: BLE001 - an adapter error is a failed submission
                    ok, ref = False, None
                with self.lock:
                    status = "submitted" if ok else "failed"
                    self._commit("submission_result", {"submission_id": sid, "status": status, "provider_ref": ref},
                                 "scheduler", evidence=("submission_result", f"submission:{sid}",
                                                        {"submission_id": sid, "status": status}, (sid, status)))
                    summary["submitted" if ok else "failed"] += 1
            except Unavailable:
                raise
            except Exception:      # noqa: BLE001 - AEGIS round 1 M3: one bad item never stalls the queue
                summary.setdefault("errors", []).append(sid)
        return summary

