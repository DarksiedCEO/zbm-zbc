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
        decision = "deny" if verdict.deny else "allow"
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
                    decision, code, message = "deny", "TOOL_DENIED", "subagent limit for this run reached"
            elif verdict.klass in ("exec", "read") and request.tool_name == "bash":
                decision, code, message = self._symlink_check(rt, binding, request, decision, code, message)
        binding.tool_calls += 1
        if decision == "deny":
            binding.denies += 1
        elif verdict.klass == "subagent":
            binding.subagents += 1
        payload = {"run_id": binding.run_id, "tool": str(request.tool_name)[:64], "class": verdict.klass,
                   "decision": decision, "code": code, "args_sha256": args_sha256(request.tool_input),
                   "token_id": _token_id(binding), "policy_version": rt.policy_seed.get("policy_version", 0),
                   "is_subagent": bool(request.is_subagent), "tool_call_id": str(request.tool_call_id or "")[:64],
                   "seq": binding.tool_calls}
        try:
            rt.record(derived_id("tc", binding.run_id, binding.tool_calls, payload["args_sha256"], payload["tool_call_id"]),
                      "tool_call_decided", ACTOR, binding.run_id, payload,
                      f"Tool call {binding.tool_calls} {decision} ({verdict.klass}) on {binding.run_id}")
        except Exception as exc:  # noqa: BLE001 - any failure to record is a deny and a failed run
            rt.on_ledger_failure(binding.run_id, f"tool_call_decided record failed: {type(exc).__name__}")
            return _deny("LEDGER_UNAVAILABLE", "the decision could not be recorded; nothing runs unrecorded")
        if decision == "deny":
            return _deny(code, message, verdict.klass)
        return GuardrailDecision(allow=True, reasons=[GuardrailReason(code="ALLOW", message=verdict.klass)],
                                 policy_id=f"dlv-policy-v{rt.policy_seed.get('policy_version', 0)}",
                                 metadata={"class": verdict.klass})

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
    def _symlink_check(rt, binding, request, decision, code, message):
        """A recursive rm whose target is a symlink pointing out of the workspace is resolved INSIDE the sandbox
        (``readlink -f``) — the classifier cannot see the container's filesystem (A2)."""
        cmd = (request.tool_input or {}).get("command", "") if isinstance(request.tool_input, dict) else ""
        for target in policy.rm_recursive_targets(cmd):
            real = rt.resolve_sandbox_path(binding.run_id, target)
            if real is None:
                continue
            if not policy.inside(real, binding.workspace) or real == binding.workspace:
                return "deny", "TOOL_DENIED", "rm target resolves outside the workspace (symlink)"
        return decision, code, message


def _token_covers(binding: registry.RunBinding, request: GuardrailRequest) -> bool:
    """The token is scoped to (service, workspace); a request on another thread never reaches this binding, and a
    ``tool_input`` that names a bearer/token string cannot widen the scope (A1: a service bearer in the input)."""
    return bool(binding.token) and bool(binding.service) and bool(binding.workspace)


def _deny(code: str, message: str, klass: str = "") -> GuardrailDecision:
    return GuardrailDecision(allow=False, reasons=[GuardrailReason(code=code, message=message)],
                             policy_id="dlv-policy", metadata={"class": klass})
