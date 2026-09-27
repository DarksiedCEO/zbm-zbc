"""
Guardrail adapter — record-first per tool call (spec §C.3; F-03, F-04, F-05, F-08, SP-04).

``ZbmGuardrailProvider`` is what deer-flow's ``GuardrailMiddleware`` calls before EVERY tool call of the lead agent
and of every subagent (``agents/middlewares/tool_error_handling_middleware.py:261-281`` builds it from
``guardrails.provider.use``; ``fail_closed: true`` so an exception here is a deny in DF as well). Per request:

1. resolve the run from ``thread_id`` (unknown thread → deny ``NO_RUN``); the effective user id must be the run's
   principal and never ``"default"`` (mismatch → deny + ``identity_mismatch``, §C.5);
2. classify (§B.5, ``policy.classify``);
3. ``args_sha256`` = canonical JSON of ``tool_input``;
4. RECORD ``tool_call_decided {tool, class, decision, args_sha256, token_id, policy_version, is_subagent,
   tool_call_id}`` — a failed record → deny ``LEDGER_UNAVAILABLE`` and the run is marked failed (never allow what
   could not be recorded);
5. answer with ``GuardrailReason(code, message)``.

"With run token" (§C.3.1) = the run is ``running``/``suite``, its deadline has not passed and the token's scope
(service, workspace) covers the call. The unconditional classes (``git_remote``, ``destructive_outside_workspace``,
``acp``, ``mcp``, ``self_modify``, ``network``) are denied whatever the token, header or environment says (A1).

Round 18 R5/R7: an opaque exec (interpreter, shell, make, find -delete, git -c) is recorded ``decision:
allow_opaque, class: exec, opaque: true`` and counted on the binding; every write-capable operand of a bash call is
resolved INSIDE the container (its longest existing prefix) and the call is denied when the resolver answers None
or the path lands outside ``services/<service>/`` and ``docs/adr/`` (fail closed).

Round 19 R8: resolution is bounded (16 operands, 64 path components per operand; a breach is a deny) and happens
in ONE exec through the engine's pinned helper, AFTER the decision is on the ledger: a call that needs resolution
is recorded ``tool_call_decided`` with ``decision: pending`` first, resolved, then recorded ``tool_call_resolved``
with the final decision — an exec/ledger amplification through the resolver is no longer possible before the
record exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timezone
from typing import Any

from deerflow.guardrails.provider import GuardrailDecision, GuardrailReason, GuardrailRequest
from deerflow.runtime.user_context import get_effective_user_id

from zbm_delivery import policy, registry
from zbm_delivery.ledger import derived_id

ACTOR = "intel_03_guardrail"
NAME = "zbm-guardrail"


def args_sha256(tool_input: Any) -> str:
    return hashlib.sha256(json.dumps(tool_input, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8", "surrogatepass")).hexdigest()


def _token_id(binding: registry.RunBinding) -> str:
    return hashlib.sha256(binding.token).hexdigest()[:16]


class ZbmGuardrailProvider:
    name = NAME

    def __init__(self, **kwargs):
        # deer-flow passes framework="deerflow" when the constructor accepts **kwargs; nothing else is configurable.
        self.framework = kwargs.get("framework")

    # --- DF GuardrailProvider protocol ----------------------------------------------------------------------------

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        rt = registry.runtime_or_none()
        if rt is None:
            return _deny("NO_RUN", "guardrail has no runtime")
        binding = registry.lookup(request.thread_id)
        if binding is None:
            return _deny("NO_RUN", "tool call on a thread with no registered run")
        now = rt.clock.now().astimezone(timezone.utc)
        effective = request.user_id or get_effective_user_id()
        if effective in (None, "", "default") or effective != binding.principal_user_id:
            self._record_identity(rt, binding, effective, request)
            return _deny("IDENTITY_MISMATCH", "tool call user id is not the run's principal")
        ctx = policy.Context(service=binding.service, workspace=binding.workspace, evidence_root=rt.evidence_root)
        verdict = policy.classify(rt.policy_seed, request.tool_name, request.tool_input, ctx)
        decision = verdict.decision
        # a policy deny is reported under its CLASS (git_remote, network, destructive_outside_workspace, acp, mcp,
        # self_modify, unknown): the class is the reason the model and the ledger see (A1)
        code, message = (verdict.klass if verdict.deny else verdict.code), verdict.message
        if not verdict.deny:
            if not binding.live(now):
                decision, code, message = "deny", "DEADLINE" if now >= binding.deadline_at else "NO_RUN", \
                    "run is not running (deadline passed, finished or not started)"
            elif verdict.needs_token and not _token_covers(binding, request):
                decision, code, message = "deny", "TOOL_DENIED", "run token does not cover this call"
            elif verdict.klass == "subagent":
                if binding.subagents >= rt.settings.max_subagents_per_run:
                    decision, code, message = "deny", "TOOL_DENIED", "subagents are off in this build (R8)"
        pending = (not verdict.deny and decision != "deny" and bool(verdict.write_targets)
                   and (verdict.klass in ("exec", "read") and request.tool_name == "bash" or verdict.klass == "write"))
        if pending:
            if len(verdict.write_targets) > policy_cap_operands():
                pending, decision, code, message = False, "deny", "TOOL_DENIED", f"more than {policy_cap_operands()} write operands in one call (R8)"
            elif any(policy_path_depth(t) > policy_cap_depth() for t in verdict.write_targets):
                pending, decision, code, message = False, "deny", "TOOL_DENIED", f"a write operand deeper than {policy_cap_depth()} components (R8)"
        binding.tool_calls += 1
        if decision == "deny":
            binding.denies += 1
        elif verdict.klass == "subagent":
            binding.subagents += 1
        elif decision == "allow_opaque":
            binding.opaque_execs += 1
        payload = {"run_id": binding.run_id, "tool": str(request.tool_name)[:64], "class": verdict.klass,
                   "decision": "pending" if pending else decision, "opaque": decision == "allow_opaque", "code": code,
                   "message": str(message)[:160],
                   "args_sha256": args_sha256(request.tool_input),
                   "token_id": _token_id(binding), "policy_version": rt.policy_seed.get("policy_version", 0),
                   "is_subagent": bool(request.is_subagent), "tool_call_id": str(request.tool_call_id or "")[:64],
                   "seq": binding.tool_calls, "write_operands": len(verdict.write_targets)}
        try:
            rt.record(derived_id("tc", binding.run_id, binding.tool_calls, payload["args_sha256"], payload["tool_call_id"]),
                      "tool_call_decided", ACTOR, binding.run_id, payload,
                      f"Tool call {binding.tool_calls} {payload['decision']} ({verdict.klass}) on {binding.run_id}")
        except Exception as exc:  # noqa: BLE001 - any failure to record is a deny and a failed run
            rt.on_ledger_failure(binding.run_id, f"tool_call_decided record failed: {type(exc).__name__}")
            return _deny("LEDGER_UNAVAILABLE", "the decision could not be recorded; nothing runs unrecorded")
        if pending:
            # R8: resolution AFTER the record, ONE exec, then the final decision is its own event
            decision, code, message = self._resolve_check(rt, binding, verdict, decision, code, message)
            if decision == "deny":
                binding.denies += 1
                if verdict.opaque:
                    binding.opaque_execs -= 1
            payload2 = {**payload, "decision": decision, "code": code, "message": str(message)[:160]}
            try:
                rt.record(derived_id("tcr", binding.run_id, binding.tool_calls, payload["args_sha256"], payload["tool_call_id"]),
                          "tool_call_resolved", ACTOR, binding.run_id, payload2,
                          f"Tool call {binding.tool_calls} resolved: {decision} ({verdict.klass}) on {binding.run_id}")
            except Exception as exc:  # noqa: BLE001
                rt.on_ledger_failure(binding.run_id, f"tool_call_resolved record failed: {type(exc).__name__}")
                return _deny("LEDGER_UNAVAILABLE", "the resolved decision could not be recorded; nothing runs unrecorded")
        if decision == "deny":
            return _deny(code, message, verdict.klass)
        return GuardrailDecision(allow=True, reasons=[GuardrailReason(code="ALLOW", message=verdict.klass)],
                                 policy_id=f"dlv-policy-v{rt.policy_seed.get('policy_version', 0)}",
                                 metadata={"class": verdict.klass, "decision": decision})

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        return await asyncio.to_thread(self.evaluate, request)

    # --- helpers -----------------------------------------------------------------------------------------------------

    @staticmethod
    def _record_identity(rt: registry.Runtime, binding: registry.RunBinding, effective: Any, request: GuardrailRequest) -> None:
        try:
            rt.record(derived_id("idm", binding.run_id, "guardrail", str(effective)[:64], str(request.tool_call_id)[:64]),
                      "identity_mismatch", ACTOR, binding.run_id,
                      {"run_id": binding.run_id, "where": "guardrail",
                       "expected_sha256": hashlib.sha256(binding.principal_user_id.encode()).hexdigest(),
                       "got_sha256": hashlib.sha256(str(effective).encode()).hexdigest()},
                      f"Identity mismatch on a tool call ({binding.run_id})")
        except Exception:  # noqa: BLE001 - the deny stands whether or not it was recorded
            pass

    @staticmethod
    def _resolve_check(rt, binding, verdict, decision, code, message):
        """Every write-capable operand of the call is resolved INSIDE the sandbox in one exec (its longest existing
        prefix; the classifier cannot see the container's filesystem, A2). None → deny (R7: fail closed); a path
        that lands outside ``services/<service>/`` or ``docs/adr/00NN-*.md`` → deny."""
        ctx = policy.Context(service=binding.service, workspace=binding.workspace, evidence_root=rt.evidence_root)
        roots = policy.write_roots(ctx)
        targets = list(verdict.write_targets)
        many = getattr(rt, "resolve_sandbox_paths", None)
        if many is not None:
            reals = many(binding.run_id, targets)
        else:
            reals = [rt.resolve_sandbox_path(binding.run_id, t) for t in targets]
        for real in reals:
            if real is None:
                return "deny", "TOOL_DENIED", "a write operand could not be resolved inside the sandbox (fail closed)"
            if real in roots or not any(policy.inside(real, r) for r in roots):
                return "deny", "TOOL_DENIED", "a write operand resolves outside the service directory (symlink)"
            if policy.inside(real, roots[1]) and not policy.adr_name_ok(real, ctx):
                return "deny", "TOOL_DENIED", "a write operand resolves to a docs/adr/ path that is not 00NN-*.md (R7)"
        return decision, code, message


def policy_cap_operands() -> int:
    from zbm_delivery.adapters.sandbox import MAX_RESOLVE_OPERANDS
    return MAX_RESOLVE_OPERANDS


def policy_cap_depth() -> int:
    from zbm_delivery.adapters.sandbox import MAX_PATH_DEPTH
    return MAX_PATH_DEPTH


def policy_path_depth(path: str) -> int:
    from zbm_delivery.adapters.sandbox import path_depth
    return path_depth(path)


def _token_covers(binding: registry.RunBinding, request: GuardrailRequest) -> bool:
    """The token is scoped to (service, workspace); a request on another thread never reaches this binding, and a
    ``tool_input`` that names a bearer/token string cannot widen the scope (A1: a service bearer in the input)."""
    return bool(binding.token) and bool(binding.service) and bool(binding.workspace)


def _deny(code: str, message: str, klass: str = "") -> GuardrailDecision:
    return GuardrailDecision(allow=False, reasons=[GuardrailReason(code=code, message=message)],
                             policy_id="dlv-policy", metadata={"class": klass})
