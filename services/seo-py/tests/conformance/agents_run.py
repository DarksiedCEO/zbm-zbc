"""Running the Wave-1 agents on the in-memory web (tests/cert_web.py) inside a coding context, the way the service
runs them (svc_audits): Selene, then Delia, Roman and the entity check on Selene's pages. Every envelope made here
is kept in ``SEEN`` for the B-UL invariants."""

from __future__ import annotations

import contextlib

import vault
from agents import delia, entity_check, roman, selene
from cert_web import ctx

try:
    from primitives import codes as codes_mod
except ImportError:            # the starting tree (before primitives/codes.py): run without a coding context
    codes_mod = None

SEEN: list = []
_KEY = vault.ephemeral_vault_key()


def coder(tenant: str = "zbm"):
    return vault.Coder(_KEY, tenant) if hasattr(vault, "Coder") else None


def coding(tenant: str = "zbm"):
    if codes_mod is None:
        return contextlib.nullcontext({})
    return codes_mod.coding(coder(tenant), own=("site.test",))


def run3(web, paths=("/",), entity=None, tenant="zbm", **kw):
    """(ctx, selene env, delia env or None, roman env, pages, evidence map)."""
    c = ctx(web, paths, **kw)
    with coding(tenant) as ev:
        s, pages = selene.run(c)
        d = delia.run(c) if c.robots is not None else None
        r = roman.run(c, pages)
        e = entity_check.run(c, pages, entity) if entity is not None else None
    for env in (s, d, r, e):
        if env is not None:
            SEEN.append(env)
    return c, s, d, r, pages, ev


def codes(env: dict) -> set:
    return {f["code"] for f in env["findings"]}


def by_code(env: dict, code: str) -> list:
    return [f for f in env["findings"] if f["code"] == code]


def one(env: dict, code: str) -> dict:
    got = by_code(env, code)
    assert len(got) == 1, (code, [f["code"] for f in env["findings"]])
    return got[0]


def sev(f: dict) -> tuple:
    return f["severity"], f["data_class"], f["decision"]
