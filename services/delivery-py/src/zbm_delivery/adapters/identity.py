"""
Identity adapter — missing principal = refuse (spec §C.5, ST-07).

One ``Principal`` per run, derived from the caller identity (``X-DLV-Caller-Token`` → ``aegis`` |
``andre_session`` | ``scheduler``; there is no body field). The runner binds deer-flow's user context to the run's
user id (``tenant--run_id``) around every turn and asserts, before every turn, inside the sandbox provider and inside
the guardrail, that the effective user id equals the run's and is never ``"default"``
(``deerflow/runtime/user_context.py:98`` returns ``"default"`` when unset — the fallback this adapter refuses).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from deerflow.runtime.user_context import get_effective_user_id, reset_current_user, set_current_user

from zbm_delivery.ports import Principal

TENANT = "zbm"
CALLER_KIND = {"aegis": "service", "andre_session": "session", "scheduler": "service"}


class PrincipalMissing(PermissionError):
    pass


def principal_for(caller: str) -> Principal:
    kind = CALLER_KIND.get(caller)
    if kind is None:
        raise PrincipalMissing("the caller maps to no principal")
    return Principal(kind=kind, id=caller, tenant=TENANT)


@dataclass
class _UserObj:
    id: str


@contextmanager
def bound_user(user_id: str):
    """``set_current_user`` / ``reset_current_user`` around one agent turn."""
    if not user_id or user_id == "default":
        raise PrincipalMissing("refusing to bind the default user")
    token = set_current_user(_UserObj(id=user_id))
    try:
        yield
    finally:
        reset_current_user(token)


def assert_effective(expected: str) -> None:
    got = get_effective_user_id()
    if got != expected or got == "default":
        raise PrincipalMissing("effective user id is not the run's principal")
