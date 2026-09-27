"""
The fix-engine loop (spec §C.8, FIX_WAVE_1_COMMON run mechanically). ``FixEngine.execute(run_id)`` runs on the
service's worker thread inside its own exception boundary (a harness crash is a run failure, not a service crash):

prepare (§C.8.2) → sandbox → suite.before (§C.8.5) → per finding (§C.8.4: brief → agent turn → RED run by the
engine → agent turn(s) → GREEN → revert check → sweep → suite → commit) → suite.after → report (§C.8.6) →
``awaiting_review``; or ``failed`` with every finding's current state (deadline §C.8.8, cancel, blocked after D12
rounds, ledger failure, harness error). Every transition is a ledger event recorded BEFORE it takes effect, through
the service's record-first operations; every captured output is content-addressed evidence.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
from typing import Optional

from zbm_delivery import fsops, registry
from zbm_delivery import reasons as R
from zbm_delivery.adapters.identity import bound_user
from zbm_delivery.adapters.sandbox import extract_tar, tar_of_dir
from zbm_delivery.engine import brief as B
from zbm_delivery.engine import parsers, report
from zbm_delivery.errors import Unavailable
from zbm_delivery.gitport import GitPort, GitRefused
from zbm_delivery.policy import WORKSPACE
from zbm_delivery.ports import LLMNotConfigured
from zbm_delivery.runner import RunnerRefused, TestRunner

ENGINE = "intel_08_engine"
JUNK_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "target", ".venv")
JUNK_SUFFIXES = (".pyc", ".pyo")
MIN_DISPROOF_STATEMENT = 40


class RunEnded(Exception):
    """Raised inside the loop to stop: the run is already in a terminal state (recorded)."""


class Turn:
    def __init__(self, text: str, tool_calls: int, tokens_in: int, tokens_out: int, overflow: bool = False):
        self.text, self.tool_calls, self.tokens_in, self.tokens_out = text, tool_calls, tokens_in, tokens_out
        self.overflow = overflow          # the turn hit the recursion limit (D3): a failed round, not a run failure


def _sha_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


def _junk(path: str) -> bool:
    parts = path.split("/")
    return any(p in JUNK_DIRS for p in parts) or path.endswith(JUNK_SUFFIXES)


class FixEngine:
    def __init__(self, svc, settings, git: GitPort, harness_factory, provider_factory, prompts: dict, test_seed: dict,
                 policy_seed: dict):
        self.svc = svc
        self.settings = settings
        self.git = git
        self.harness_factory = harness_factory        # (thread_id, system_prompt, middlewares) -> DeerFlowClient-like
        self.provider_factory = provider_factory      # () -> ZbmDockerSandboxProvider (DF's singleton)
        self.prompts = prompts
        self.test_seed = test_seed
        self.policy_seed = policy_seed

    # ================================================================ entry

    def execute(self, run_id: str) -> None:
        run = self.svc.run_get(run_id)
        binding: Optional[registry.RunBinding] = None
        provider = None
        sandbox_id = None
        try:
            self.svc.run_transition(run_id, "preparing", "fix_run_started",
                                    {"run_id": run_id, "request_id": run["request_id"], "facts_sha256": run["facts_sha256"]},
                                    f"Fix run started ({run_id})")
            worktree, branch, base_sha = self._prepare(run)
            run = self.svc.run_get(run_id)
            binding = registry.RunBinding(run_id=run_id, thread_id=run["thread_id"], service=run["service"],
                                          principal_user_id=run["principal_user_id"], workspace=WORKSPACE,
                                          deadline_at=self.svc.parse_time(run["deadline_at"]),
                                          status=lambda: self.svc.run_status(run_id))
            registry.bind(binding)
            self.svc.run_transition(run_id, "running", "fix_run_running", {"run_id": run_id, "branch": branch},
                                    f"Fix run running ({run_id})")
            provider = self.provider_factory()
            sandbox_id = provider.acquire(run["thread_id"], user_id=run["principal_user_id"])
            box = provider.get(sandbox_id)
            copied = provider.copy_in(sandbox_id, worktree)
            self.svc.run_update(run_id, "sandbox_acquired",
                                {"run_id": run_id, "sandbox_id": sandbox_id, "image": self.settings.sandbox_image,
                                 "container_id_sha256": getattr(box, "container_id_sha256", None), "workspace_bytes": copied},
                                f"Sandbox acquired ({run_id})",
                                {"sandbox": {"image_digest": self.settings.sandbox_image.split("@sha256:")[1],
                                             "container_id_sha256": getattr(box, "container_id_sha256", None)}})
            runner = TestRunner(self.test_seed, run["service"], box, worktree, self.settings.cmd_timeout_s)
            baseline = self._suite(run_id, runner, "before")
            findings = self.svc.findings_get(run_id)
            system = B.system_prompt(self.prompts, service=run["service"], policy_summary=B.policy_summary(self.policy_seed))
            self.svc.run_update(run_id, "prompts_loaded", {"run_id": run_id, "system_prompt_sha256": _sha_text(system),
                                                           "manifest_sha256": run["prompts_manifest_sha256"]},
                                f"Prompts loaded ({run_id})")
            self.svc.evidence_put(run_id, "prompt_manifest", system.encode("utf-8"))
            for finding in findings:
                self._check_live(run_id)
                self._finding_loop(run, finding, runner, worktree, provider, sandbox_id, system, baseline)
            self._check_live(run_id)
            self._suite(run_id, runner, "after")
            findings = self.svc.findings_get(run_id)
            not_done = [f["finding_id"] for f in findings if f["state"] not in ("fixed", "disproved")]
            if not_done:
                self._fail(run_id, R.item("BLOCKED", f"{len(not_done)} finding(s) not fixed or disproved: "
                                                     + ", ".join(not_done[:10])))
                raise RunEnded()
            self.svc.run_transition(run_id, "reporting", "fix_run_reporting", {"run_id": run_id}, f"Reporting ({run_id})")
            self._report(run_id)
        except RunEnded:
            pass
        except Unavailable as exc:
            self._fail_best_effort(run_id, R.item("LEDGER_UNAVAILABLE", exc.reason[:160]))
        except LLMNotConfigured as exc:
            self._fail_best_effort(run_id, R.item("LLM_NOT_CONFIGURED", str(exc)[:160]))
        except (GitRefused, RunnerRefused, PermissionError) as exc:
            self._fail_best_effort(run_id, R.item("GIT_REFUSED" if isinstance(exc, GitRefused) else "HARNESS_ERROR",
                                                  f"{type(exc).__name__}: {str(exc)[:140]}"))
        except Exception as exc:  # noqa: BLE001 - the harness/adapter exception boundary: a run failure, never a crash
            self._fail_best_effort(run_id, R.item("HARNESS_ERROR", f"{type(exc).__name__}"))
        finally:
            if provider is not None and sandbox_id is not None:
                try:
                    provider.destroy(sandbox_id, run_id)
                    self.svc.try_run_update(run_id, "sandbox_released", {"run_id": run_id, "sandbox_id": sandbox_id},
                                            f"Sandbox released ({run_id})")
                except Exception:  # noqa: BLE001
                    pass
            if binding is not None:
                registry.unbind(binding.thread_id)
            self.svc.run_finished(run_id)

    # ================================================================ prepare

    def _prepare(self, run: dict) -> tuple[str, str, str]:
        run_id = run["run_id"]
        service = run["service"]
        if run.get("parent_run_id"):
            branch = run["branch"]
            worktree = run["worktree_path"]
            if not os.path.isdir(worktree):
                raise GitRefused("the branch's worktree is gone; a re-run needs the original worktree")
            head = self.git.rev_parse("HEAD", worktree, run_id)
            if self.git.status(worktree, run_id).strip():
                raise GitRefused("the branch's worktree is not clean")
            base_sha = head
        else:
            if not self.git.is_ancestor(run["base_sha"], self.settings.base_ref, run_id):
                raise GitRefused("base_sha is not an ancestor of DLV_BASE_REF")
            branch = self.git.next_fix_branch(service, run_id)
            worktree = os.path.join(os.path.realpath(self.settings.worktrees_dir), branch)
            self.git.worktree_add(worktree, branch, run["base_sha"], run_id)
            base_sha = run["base_sha"]
        self.svc.run_update(run_id, "worktree_created", {"run_id": run_id, "branch": branch, "path_sha256": _sha_text(worktree),
                                                         "base_sha": base_sha},
                            f"Worktree created on {branch} ({run_id})",
                            {"branch": branch, "worktree_path": worktree, "base_sha": base_sha})
        return worktree, branch, base_sha

    # ================================================================ helpers

    def _check_live(self, run_id: str) -> None:
        status = self.svc.run_status(run_id)
        if status in ("failed", "reviewed_pass", "reviewed_fail"):
            raise RunEnded()
        now = self.svc.now()
        run = self.svc.run_get(run_id)
        if now >= self.svc.parse_time(run["deadline_at"]):
            self.svc.run_transition(run_id, "failed", "fix_run_deadline",
                                    {"run_id": run_id, "deadline_at": run["deadline_at"],
                                     "finding_states": self._finding_states(run_id)},
                                    f"Fix run deadline passed ({run_id})",
                                    {"reasons": [R.item("DEADLINE", "run wall clock expired")]})
            raise RunEnded()

    def _finding_states(self, run_id: str) -> list[dict]:
        return [{"finding_id": f["finding_id"], "state": f["state"]} for f in self.svc.findings_get(run_id)]

    def _fail(self, run_id: str, reason: dict) -> None:
        self.svc.run_transition(run_id, "failed", "fix_run_failed",
                                {"run_id": run_id, "code": reason["code"], "finding_states": self._finding_states(run_id)},
                                f"Fix run failed: {reason['code']} ({run_id})", {"reasons": [reason]})

    def _fail_best_effort(self, run_id: str, reason: dict) -> None:
        try:
            if self.svc.run_status(run_id) not in ("failed", "reviewed_pass", "reviewed_fail"):
                self._fail(run_id, reason)
        except Exception:  # noqa: BLE001 - the ledger is down: the in-memory state is marked failed (unrecorded)
            self.svc.mark_failed_unrecorded(run_id, reason)

    def _suite(self, run_id: str, runner: TestRunner, phase: str, finding_id: Optional[str] = None,
               commit_sha: Optional[str] = None) -> parsers.Counts:
        t = runner.run_suite()
        ev = self.svc.evidence_put(run_id, "suite_output", t.output.encode("utf-8", "surrogatepass"))
        rec = {"argv": t.argv, "exit": t.exit, "counts": t.counts.as_dict(), "output_sha256": t.output_sha256,
               "evidence_id": ev, "timed_out": t.timed_out, "truncated": t.truncated}
        payload = {"run_id": run_id, "phase": phase, "argv": t.argv, "exit": t.exit, "passed": t.counts.passed,
                   "failed": t.counts.failed, "errors": t.counts.errors, "skips": t.counts.skipped,
                   "output_sha256": t.output_sha256, "evidence_id": ev}
        if finding_id:
            payload["finding_id"] = finding_id
        if commit_sha:
            payload["commit_sha"] = commit_sha
            rec["commit_sha"] = commit_sha
        fields = {}
        if phase in ("before", "after"):
            run = self.svc.run_get(run_id)
            suite = dict(run.get("suite") or {})
            suite[phase] = rec
            fields["suite"] = suite
        self.svc.run_update(run_id, "suite_run", payload, f"Suite {phase}: {t.counts.passed} passed, {t.counts.failed} failed ({run_id})",
                            fields)
        return t.counts

    def _sync_out(self, provider, sandbox_id: str, worktree: str, service: str) -> None:
        """Copy the writable parts of the sandbox back onto the host worktree (services/<svc> and docs/adr)."""
        for rel in (f"services/{service}", "docs/adr"):
            data = provider.copy_out(sandbox_id, posixpath.join(WORKSPACE, rel))
            parent = os.path.join(worktree, os.path.dirname(rel))
            target = os.path.join(worktree, rel)
            if os.path.isdir(target):
                fsops.delete_tree(target, within=worktree)
            os.makedirs(parent, exist_ok=True)
            names = extract_tar(data, parent)
            for n in names:
                if _junk(n):
                    full = os.path.join(parent, n)
                    if os.path.isdir(full):
                        fsops.delete_tree(full, within=worktree)
                    elif os.path.exists(full):
                        fsops.delete_file(full, within=worktree)

    def _sync_in(self, provider, sandbox_id: str, worktree: str, service: str) -> None:
        """Replace the sandbox's service directory with the host worktree's (after a stash / pop)."""
        box = provider.get(sandbox_id)
        rel = f"services/{service}"
        r = box.exec_argv(["rm", "-rf", "--", posixpath.join(WORKSPACE, rel)], timeout=60)
        if r.exit_code != 0:
            raise RuntimeError("could not clear the sandbox service directory")
        data = tar_of_dir(os.path.join(worktree, rel), exclude_dirs=(".git", *JUNK_DIRS))
        import io
        import tarfile
        # re-root the stream under services/<svc>/
        buf = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as src, tarfile.open(fileobj=buf, mode="w") as dst:
            for m in src.getmembers():
                m.name = posixpath.join(service, m.name)
                dst.addfile(m, src.extractfile(m) if m.isfile() else None)
        res = registry.runtime().docker.run(["cp", "-", f"{box.container}:{posixpath.join(WORKSPACE, 'services')}"],
                                            timeout_s=600, stdin=buf.getvalue())
        if res.exit_code != 0:
            raise RuntimeError("docker cp into the sandbox failed")

    def _changed(self, worktree: str, run_id: str) -> list[str]:
        return sorted(p for p in self.git.changed_paths(worktree, run_id) if not _junk(p))

    # ================================================================ the agent turn

    def _turn(self, client, run: dict, prompt: str) -> Turn:
        self._check_live(run["run_id"])
        text_by_id: dict[str, list[str]] = {}
        last_id = ""
        tool_calls = tokens_in = tokens_out = 0
        from langgraph.errors import GraphRecursionError
        overflow = False
        try:
            with bound_user(run["principal_user_id"]):
                for ev in client.stream(prompt, thread_id=run["thread_id"], user_id=run["principal_user_id"],
                                        recursion_limit=self.settings.recursion_limit):
                    tool_calls, tokens_in, tokens_out, last_id = self._collect(ev, text_by_id, tool_calls, tokens_in, tokens_out, last_id)
        except GraphRecursionError:
            overflow = True
        self._check_live(run["run_id"])
        return Turn("".join(text_by_id.get(last_id, [])), tool_calls, tokens_in, tokens_out, overflow)

    @staticmethod
    def _collect(ev, text_by_id, tool_calls, tokens_in, tokens_out, last_id):
        if ev.type == "messages-tuple" and ev.data.get("type") == "ai":
            if ev.data.get("tool_calls"):
                tool_calls += len(ev.data["tool_calls"])
            delta = ev.data.get("content") or ""
            if delta:
                mid = ev.data.get("id") or ""
                text_by_id.setdefault(mid, []).append(delta)
                last_id = mid
            usage = ev.data.get("usage_metadata") or {}
            tokens_in += int(usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output_tokens") or 0)
        elif ev.type == "end":
            usage = (ev.data or {}).get("usage") or {}
            if usage.get("input_tokens") or usage.get("output_tokens"):
                tokens_in = max(tokens_in, int(usage.get("input_tokens") or 0))
                tokens_out = max(tokens_out, int(usage.get("output_tokens") or 0))
        return tool_calls, tokens_in, tokens_out, last_id

    # ================================================================ per finding

    def _finding_loop(self, run: dict, finding: dict, runner: TestRunner, worktree: str, provider, sandbox_id: str,
                      system: str, baseline: parsers.Counts) -> None:
        run_id, fid = run["run_id"], finding["finding_id"]
        max_rounds = self.settings.max_rounds_per_finding
        doc = self.svc.finding_document(run_id, fid)
        test_argv_example = [("tests/test_<file>.py::test_<name>" if a == "{target}" else a) for a in runner.fw["test"]]
        brief_text = B.compile_brief(self.prompts["brief.template.md"], run=run, finding=doc, round_no=1,
                                     max_rounds=max_rounds, test_argv=test_argv_example, suite_argv=runner.suite_argv())
        brief_ev = self.svc.evidence_put(run_id, "brief", brief_text.encode("utf-8"))
        hits = self.svc.injection_hits(run_id, fid)
        self.svc.finding_update(run_id, fid, "brief_written", {"run_id": run_id, "finding_id": fid,
                                                               "brief_sha256": B.brief_sha256(brief_text), "evidence_id": brief_ev,
                                                               "injection_rules": hits},
                                f"Brief written for {fid} ({run_id})", {"brief_evidence_id": brief_ev})
        self.svc.finding_update(run_id, fid, "finding_started", {"run_id": run_id, "finding_id": fid,
                                                                 "severity": finding["severity"], "state": finding["state"]},
                                f"Finding {fid} started ({run_id})")
        from zbm_delivery.adapters.prompt import ZbmSystemPromptMiddleware
        from zbm_delivery.adapters.receipts import ZbmToolReceiptMiddleware
        client = self.harness_factory(run["thread_id"] + "-" + fid.lower().replace(".", "-"),
                                      [ZbmSystemPromptMiddleware(system), ZbmToolReceiptMiddleware(run["thread_id"])])
        rounds = int(finding.get("rounds") or 0)
        prompt = brief_text
        agent = {"thread_id": run["thread_id"], "turns": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0}
        current_test: Optional[str] = None
        while True:
            f = self.svc.findings_get_one(run_id, fid)
            if f["state"] in ("fixed", "disproved", "blocked"):
                return
            if rounds >= max_rounds:
                self._block(run_id, fid, agent, f"no RED→GREEN or disproof within {max_rounds} rounds")
                return
            turn = self._turn(client, run, prompt)
            agent["turns"] += 1
            agent["tool_calls"] += turn.tool_calls
            agent["tokens_in"] += turn.tokens_in
            agent["tokens_out"] += turn.tokens_out
            reply = parsers.parse_reply(turn.text, run["service"])
            if turn.overflow:
                rounds += 1
                self.svc.finding_update(run_id, fid, "round_failed", {"run_id": run_id, "finding_id": fid, "round": rounds,
                                                                      "code": "ROUND_FAILED", "why": "recursion_limit"},
                                        f"Round {rounds} failed for {fid}: turn hit the recursion limit", {"rounds": rounds})
                prompt = self._note("Your turn hit the recursion limit (too many tool calls without a reply line). Reply "
                                    "with a contract line.", rounds, max_rounds)
                continue
            if reply.blocked:
                rounds += 1
                self._block(run_id, fid, agent, "engineer replied BLOCKED", rounds)
                return
            if reply.disproof:
                rounds += 1
                if self._disproof(run_id, fid, runner, reply, agent, rounds):
                    return
                prompt = self._note("The reproduction did not demonstrably contradict the finding (exit != 0 or no "
                                    "statement). Write the failing test instead, or a better reproduction.", rounds, max_rounds)
                continue
            f = self.svc.findings_get_one(run_id, fid)
            if f["state"] == "queued" or (f["state"] == "red" and reply.test and not reply.fixed):
                if not reply.test:
                    rounds += 1
                    self.svc.finding_update(run_id, fid, "round_failed", {"run_id": run_id, "finding_id": fid, "round": rounds,
                                                                          "code": "ROUND_FAILED", "why": "no_test_line"},
                                            f"Round {rounds} failed for {fid}: no TEST line", {"rounds": rounds})
                    prompt = self._note("Your reply carried no `TEST: <path>::<name>` line. Write the failing test and reply "
                                        "with exactly that line.", rounds, max_rounds)
                    continue
                target = f"{reply.test[0]}::{reply.test[1]}"
                self._sync_out(provider, sandbox_id, worktree, run["service"])
                try:
                    t = runner.run_test(target)
                except RunnerRefused as exc:
                    rounds += 1
                    self.svc.finding_update(run_id, fid, "round_failed", {"run_id": run_id, "finding_id": fid, "round": rounds,
                                                                          "code": "ROUND_FAILED", "why": "bad_target"},
                                            f"Round {rounds} failed for {fid}: bad test target", {"rounds": rounds})
                    prompt = self._note(f"The test target was refused ({exc}). Reply `TEST: tests/<file>.py::<name>`.",
                                        rounds, max_rounds)
                    continue
                ev = self.svc.evidence_put(run_id, "test_output", t.output.encode("utf-8", "surrogatepass"))
                rec = {"test_path": reply.test[0], "test_name": reply.test[1], "argv": t.argv, "exit": t.exit,
                       "output_sha256": t.output_sha256, "evidence_id": ev}
                self.svc.finding_update(run_id, fid, "test_run", {"run_id": run_id, "finding_id": fid, "phase": "red",
                                                                  "argv": t.argv, "exit": t.exit, "output_sha256": t.output_sha256,
                                                                  "evidence_id": ev},
                                        f"RED run for {fid}: exit {t.exit} ({run_id})", {"agent": dict(agent)})
                if t.exit == 0:
                    rounds += 1
                    self.svc.finding_update(run_id, fid, "round_failed", {"run_id": run_id, "finding_id": fid, "round": rounds,
                                                                          "code": "ROUND_FAILED", "why": "test_passes_on_unfixed_code",
                                                                          "evidence_id": ev},
                                            f"Round {rounds} failed for {fid}: test passes on unfixed code", {"rounds": rounds})
                    prompt = self._note("The engine ran your test on the UNFIXED code and it PASSED (exit 0): it does not "
                                        "reproduce the finding (you're testing existing behavior). Rewrite it so it fails "
                                        "on the current code, then reply `TEST: ...` again.\n\nCaptured output tail:\n"
                                        + self._tail(t.output), rounds, max_rounds)
                    continue
                ok = self.svc.finding_transition(run_id, fid, "red", {"red": rec, "rounds": rounds}, [ev])
                if not ok:
                    rounds += 1
                    prompt = self._note("The RED transition was refused by the engine's invariants.", rounds, max_rounds)
                    continue
                current_test = target
                prompt = self._note(f"RED confirmed: `{target}` failed with exit {t.exit} on the unfixed code. Now fix the "
                                    "ROOT CAUSE and sweep the class hint; list every site as `SWEEP: file:line`; list any "
                                    "existing test you changed as `CHANGED_TEST: path — why`; then reply `FIXED`.\n\n"
                                    "Captured output tail:\n" + self._tail(t.output), rounds, max_rounds)
                continue
            if f["state"] in ("red", "green", "swept") and reply.fixed:
                if current_test is None:
                    current_test = f"{f['red']['test_path']}::{f['red']['test_name']}"
                rounds += 1
                outcome = self._green_phase(run, f, runner, worktree, provider, sandbox_id, reply, current_test, agent,
                                            rounds, baseline)
                if outcome is None:
                    return
                prompt = self._note(outcome, rounds, max_rounds)
                continue
            rounds += 1
            self.svc.finding_update(run_id, fid, "round_failed", {"run_id": run_id, "finding_id": fid, "round": rounds,
                                                                  "code": "ROUND_FAILED", "why": "no_contract_line"},
                                    f"Round {rounds} failed for {fid}: no contract line", {"rounds": rounds})
            prompt = self._note("Your reply carried no contract line the engine accepts in this state. Reply `FIXED` "
                                "(after RED) with your `SWEEP:` lines, `DISPROOF: <argv>` with a statement, or "
                                "`BLOCKED: <why>`.", rounds, max_rounds)

    def _green_phase(self, run, f, runner, worktree, provider, sandbox_id, reply, target, agent, rounds, baseline) -> Optional[str]:
        """GREEN → revert check → sweep → suite → commit → fixed. Returns None when fixed, else the note for the
        next turn (the round failed)."""
        run_id, fid, service = run["run_id"], f["finding_id"], run["service"]
        self._sync_out(provider, sandbox_id, worktree, service)
        changed = self._changed(worktree, run_id)
        # §C.8.4 step 8: every EXISTING test file the engineer touched needs a CHANGED_TEST line
        tracked_changed = [p for p in self.git.diff_name_only(worktree, run_id=run_id) if not _junk(p)]
        touched_tests = [p for p in tracked_changed if runner.is_test_path(p)]
        missing = [p for p in touched_tests if parsers.service_relative(p, service) not in reply.changed_tests]
        if missing:
            self.svc.finding_update(run_id, fid, "round_failed", {"run_id": run_id, "finding_id": fid, "round": rounds,
                                                                  "code": "ROUND_FAILED", "why": "changed_test_unexplained",
                                                                  "paths": missing[:20]},
                                    f"Round {rounds} failed for {fid}: changed tests unexplained", {"rounds": rounds})
            return ("You changed existing test file(s) without a `CHANGED_TEST: <path> — <why>` line for each: "
                    + ", ".join(missing) + ". Add the lines (or revert the change) and reply `FIXED` again.")
        # GREEN
        t = runner.run_test(target)
        ev = self.svc.evidence_put(run_id, "test_output", t.output.encode("utf-8", "surrogatepass"))
        self.svc.finding_update(run_id, fid, "test_run", {"run_id": run_id, "finding_id": fid, "phase": "green", "argv": t.argv,
                                                          "exit": t.exit, "output_sha256": t.output_sha256, "evidence_id": ev},
                                f"GREEN run for {fid}: exit {t.exit} ({run_id})", {"agent": dict(agent), "rounds": rounds})
        if t.exit != 0:
            return ("The engine ran the test after your fix and it still FAILS (exit %d). Fix the root cause and reply "
                    "`FIXED` again.\n\nCaptured output tail:\n%s" % (t.exit, self._tail(t.output)))
        green = {"test_path": f["red"]["test_path"], "test_name": f["red"]["test_name"], "argv": t.argv, "exit": t.exit,
                 "output_sha256": t.output_sha256, "evidence_id": ev}
        if not self.svc.finding_transition(run_id, fid, "green", {"green": green}, [ev], red_test_name=f["red"]["test_name"]):
            return "The GREEN transition was refused by the engine's invariants (no matching RED)."
        # revert check (VBC :84): stash the non-test changes, must fail; pop, must pass
        non_test = [p for p in changed if not runner.is_test_path(p)]
        rc = {"exit": None, "restored_exit": None, "evidence_id": None}
        if non_test:
            self.git.stash_push(worktree, non_test, run_id)
            try:
                self._sync_in(provider, sandbox_id, worktree, service)
                t_rev = runner.run_test(target)
            finally:
                self.git.stash_pop(worktree, run_id)
                self._sync_in(provider, sandbox_id, worktree, service)
            t_res = runner.run_test(target)
            ev_rc = self.svc.evidence_put(run_id, "test_output", (t_rev.output + "\n--- restored ---\n" + t_res.output)
                                          .encode("utf-8", "surrogatepass"))
            rc = {"exit": t_rev.exit, "restored_exit": t_res.exit, "evidence_id": ev_rc, "argv": t_rev.argv,
                  "output_sha256": _sha_text(t_rev.output + "\n--- restored ---\n" + t_res.output)}
            self.svc.finding_update(run_id, fid, "test_run", {"run_id": run_id, "finding_id": fid, "phase": "revert_check",
                                                              "argv": t_rev.argv, "exit": t_rev.exit, "restored_exit": t_res.exit,
                                                              "output_sha256": rc["output_sha256"], "evidence_id": ev_rc},
                                    f"Revert check for {fid}: reverted exit {t_rev.exit}, restored exit {t_res.exit} ({run_id})",
                                    {"revert_check": rc})
            if t_rev.exit == 0 or t_res.exit != 0:
                self.svc.finding_transition(run_id, fid, "red", {}, [ev_rc])
                return ("Revert check failed: with your non-test changes stashed the test %s (exit %d), and restored it "
                        "exits %d. A regression test must FAIL without the fix and PASS with it. Fix and reply `FIXED`."
                        % ("still passed" if t_rev.exit == 0 else "failed", t_rev.exit, t_res.exit))
        else:
            ev_rc = self.svc.evidence_put(run_id, "test_output", b"(no non-test changes to revert: nothing was fixed)\n")
            self.svc.finding_update(run_id, fid, "test_run", {"run_id": run_id, "finding_id": fid, "phase": "revert_check",
                                                              "argv": [], "exit": 0, "output_sha256": _sha_text("none"),
                                                              "evidence_id": ev_rc},
                                    f"Revert check for {fid}: no non-test changes ({run_id})", {"revert_check": {"exit": 0, "evidence_id": ev_rc}})
            self.svc.finding_transition(run_id, fid, "red", {}, [ev_rc])
            return "You replied FIXED but changed no non-test file: nothing was fixed. Fix the root cause and reply `FIXED`."
        # sweep
        sites = [{"file": p, "line": n} for p, n in reply.sweep]
        ev_sw = self.svc.evidence_put(run_id, "diff", self.git.diff(worktree, run_id=run_id).encode("utf-8", "surrogatepass"))
        sweep = {"class_hint": f.get("class_hint"), "sites": sites, "evidence_id": ev_sw}
        if not self.svc.finding_transition(run_id, fid, "swept", {"sweep": sweep}, [ev_sw]):
            return "The sweep transition was refused."
        # suite (§C.8.4 step 5)
        counts = self._suite(run_id, runner, "per_finding", finding_id=fid)
        own_test_file = f["red"]["test_path"]
        allowed = self.svc.attributable_failures(run_id, fid, baseline.failed_names)
        remaining = [n for n in counts.failed_names if n not in allowed and not n.startswith(own_test_file + "::" + f["red"]["test_name"])]
        new_defects = [n for n in remaining if n not in baseline.failed_names]
        self.svc.finding_update(run_id, fid, "suite_checked", {"run_id": run_id, "finding_id": fid, "failures": len(remaining),
                                                               "new_defects": len(new_defects)},
                                f"Suite checked for {fid}: {len(remaining)} blocking failure(s) ({run_id})",
                                {"suite_failures": remaining}, run_fields={"new_defects": sorted(set(self.svc.run_get(run_id).get("new_defects") or []) | set(new_defects))})
        if remaining:
            self.svc.finding_transition(run_id, fid, "red", {}, [])
            return ("The whole suite is not green after your fix: %s. Every failure — including one you did not cause "
                    "— blocks `fixed` until green. Fix and reply `FIXED`." % ", ".join(remaining[:20]))
        # commit
        changed = self._changed(worktree, run_id)
        self.git.add(worktree, changed, run_id)
        subject = f"fix({service}): {fid} at {f['file']}:{f['line']}"
        body = "Class hint: %s\nSweep sites: %s\nChanged tests: %s" % (
            f.get("class_hint") or "-", ", ".join(f"{p}:{n}" for p, n in reply.sweep) or "-",
            ", ".join(sorted(reply.changed_tests)) or "-")
        sha = self.git.commit(worktree, subject, body, run_id)
        files = self.git.diff_name_only(worktree, run_id=run_id, commit=sha)
        message_sha = _sha_text(subject + "\n\n" + body)
        ev_diff = self.svc.evidence_put(run_id, "diff", self._commit_diff(worktree, sha, run_id).encode("utf-8", "surrogatepass"))
        run_now = self.svc.run_get(run_id)
        commits = list(run_now.get("commits") or []) + [{"sha": sha, "message_sha256": message_sha, "files": files,
                                                         "finding_id": fid, "diff_evidence_id": ev_diff}]
        self.svc.finding_update(run_id, fid, "commit_recorded", {"run_id": run_id, "finding_id": fid, "sha": sha,
                                                                 "message_sha256": message_sha, "files": files[:200],
                                                                 "evidence_id": ev_diff},
                                f"Commit {sha[:12]} recorded for {fid} ({run_id})",
                                {"commit_sha": sha, "commit_files": files,
                                 "changed_tests": [{"path": p, "why_sha256": _sha_text(w)} for p, w in sorted(reply.changed_tests.items())],
                                 "agent": dict(agent)},
                                run_fields={"commits": commits})
        if not self.svc.finding_transition(run_id, fid, "fixed", {}, [ev_diff], suite_after_commit=True):
            return "The fixed transition was refused."
        return None

    def _commit_diff(self, worktree: str, sha: str, run_id: str) -> str:
        return self.git.commit_diff(worktree, sha, run_id)

    def _disproof(self, run_id, fid, runner, reply, agent, rounds) -> bool:
        try:
            t = runner.run_reproduction(reply.disproof)
        except RunnerRefused:
            self.svc.finding_update(run_id, fid, "round_failed", {"run_id": run_id, "finding_id": fid, "round": rounds,
                                                                  "code": "ROUND_FAILED", "why": "disproof_argv_refused"},
                                    f"Round {rounds} failed for {fid}: disproof argv refused", {"rounds": rounds})
            return False
        ev = self.svc.evidence_put(run_id, "test_output", t.output.encode("utf-8", "surrogatepass"))
        statement_sha = _sha_text(reply.disproof_statement)
        self.svc.evidence_put(run_id, "brief", ("DISPROOF STATEMENT (engineer text, untrusted)\n\n" + reply.disproof_statement).encode("utf-8", "surrogatepass"))
        self.svc.finding_update(run_id, fid, "test_run", {"run_id": run_id, "finding_id": fid, "phase": "disproof", "argv": t.argv,
                                                          "exit": t.exit, "output_sha256": t.output_sha256, "evidence_id": ev,
                                                          "statement_sha256": statement_sha},
                                f"Disproof reproduction for {fid}: exit {t.exit} ({run_id})", {"rounds": rounds, "agent": dict(agent)})
        contradicts = t.exit == 0 and len(reply.disproof_statement) >= MIN_DISPROOF_STATEMENT
        if not contradicts:
            return False
        disproof = {"reproduction_argv": t.argv, "exit": t.exit, "output_sha256": t.output_sha256,
                    "statement_sha256": statement_sha, "evidence_id": ev}
        return self.svc.finding_transition(run_id, fid, "disproved", {"disproof": disproof, "reasons": [
            R.item("BLOCKED", "disproof — verify (a reproduction the engine ran exited 0 with a written statement)", [ev])]}, [ev])

    def _block(self, run_id, fid, agent, why, rounds=None) -> None:
        fields = {"reasons": [R.item("BLOCKED", why)], "agent": dict(agent)}
        if rounds is not None:
            fields["rounds"] = rounds
        self.svc.finding_transition(run_id, fid, "blocked", fields, [])

    @staticmethod
    def _tail(text: str, n: int = 80) -> str:
        return "\n".join(text.splitlines()[-n:])

    @staticmethod
    def _note(text: str, rounds: int, max_rounds: int) -> str:
        return f"[engine — round {rounds + 1} of {max_rounds}]\n\n{text}"

    # ================================================================ report

    def _report(self, run_id: str) -> None:
        run = self.svc.run_get(run_id)
        findings = self.svc.findings_get(run_id)
        text = report.render(run, findings, lambda ev: self.svc.evidence_text(run_id, ev))
        ev = self.svc.evidence_put(run_id, "report", text.encode("utf-8", "surrogatepass"))
        sha = _sha_text(text)
        self.svc.run_update(run_id, "report_written", {"run_id": run_id, "report_sha256": sha, "evidence_id": ev},
                            f"Report written ({run_id})", {"report_sha256": sha, "report_evidence_id": ev})
        self.svc.run_transition(run_id, "awaiting_review", "fix_run_awaiting_review",
                                {"run_id": run_id, "report_sha256": sha, "commits": [c["sha"] for c in run.get("commits") or []]},
                                f"Fix run awaiting review ({run_id})", {"finished_at": self.svc.now_iso()})

