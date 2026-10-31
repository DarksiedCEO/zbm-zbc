"""
Client ownership and OFAC owner screening (ADR 0027 decision 4; MSA 1.5(b), (c), (e); OFAC's 50 Percent Rule).

Andre records who owns a client (``PUT /compliance/v1/clients/{id}/ownership``, his token): each owner's id, kind
(person or entity), share of its parent in basis points (2500 = 25%), the parent (None = the client itself), country
and the SHA-256 of the name he searched -- never the name. The client attests the disclosure (MSA 1.5(c): every
person or entity owning 25% or more, directly or indirectly). Each disclosed owner needs Andre's recorded OFAC search
(the client's attestation route with ``owner_id``), bound to that owner's name hash.

OFAC's rule (Revised Guidance on Entities Owned by Blocked Persons, Aug 13, 2014; FAQ 401): an entity owned 50 percent
or more, directly or indirectly, in the aggregate by one or more blocked persons is itself blocked, whether or not it
is listed. ``blocked_bp`` computes that bottom up: an owner is blocked when its own search hit, or when blocked owners
of it hold ``aggregate_block_bp`` (5000) or more of it; the client's blocked share is the sum of its blocked direct
owners' shares. Founder rule (HR-14): any hit on any owner refuses activation and holds the client permanently, so the
aggregate decides only whether the client is itself BLOCKED property (sanctions-grounded: never refunded, reported).

Pure functions: no I/O. Shares are integers (basis points); effective shares are exact fractions.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Callable, Optional

MAX_OWNERS = 50
MAX_DEPTH = 6
FULL = 10000


class OwnershipInvalid(ValueError):
    pass


def validate(body: dict) -> None:
    """Refuse an inconsistent record (the message names the problem)."""
    owners = body.get("owners") or []
    kind = body.get("client_kind")
    if kind == "sole_proprietor" and owners:
        raise OwnershipInvalid("a sole proprietor has no owners to list (the proprietor IS the client screened)")
    if kind == "sole_proprietor" and body.get("no_owner_25"):
        raise OwnershipInvalid("no_owner_25 is for an entity")
    if owners and body.get("no_owner_25"):
        raise OwnershipInvalid("no_owner_25 says nobody owns 25% or more: then list no owners")
    ids = [o["owner_id"] for o in owners]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise OwnershipInvalid(f"owner {dup[0]} is listed twice")
    by = {o["owner_id"]: o for o in owners}
    shares: dict[Optional[str], int] = {}
    for o in owners:
        p = o.get("parent_owner_id")
        if p is not None:
            if p not in by:
                raise OwnershipInvalid(f"owner {o['owner_id']}: its parent {p} is not in the list")
            if by[p]["kind"] != "entity":
                raise OwnershipInvalid(f"owner {o['owner_id']}: its parent {p} is a person, not an entity")
        shares[p] = shares.get(p, 0) + int(o["ownership_bp"])
        if o["kind"] != "entity" and o.get("no_further_owners_25"):
            raise OwnershipInvalid(f"owner {o['owner_id']}: no_further_owners_25 is for an entity owner")
    for p, total in shares.items():
        if total > FULL:
            raise OwnershipInvalid(f"the shares of {p or 'the client'} add up to more than 100%")
    for o in owners:                           # every chain reaches the client, without a cycle, in MAX_DEPTH steps
        seen, cur, depth = set(), o, 0
        while cur.get("parent_owner_id") is not None:
            if cur["owner_id"] in seen:
                raise OwnershipInvalid(f"owner {o['owner_id']}: the ownership chain is a cycle")
            seen.add(cur["owner_id"])
            cur = by[cur["parent_owner_id"]]
            depth += 1
            if depth > MAX_DEPTH:
                raise OwnershipInvalid(f"owner {o['owner_id']}: the ownership chain is a cycle or deeper than "
                                       f"{MAX_DEPTH} levels")


def effective(owners: list[dict]) -> dict[str, Fraction]:
    """Each owner's share of the CLIENT (the product of the shares along its chain)."""
    by = {o["owner_id"]: o for o in owners}
    out: dict[str, Fraction] = {}

    def eff(oid: str) -> Fraction:
        if oid in out:
            return out[oid]
        o = by[oid]
        share = Fraction(int(o["ownership_bp"]), FULL)
        p = o.get("parent_owner_id")
        out[oid] = share if p is None else share * eff(p)
        return out[oid]
    for o in owners:
        eff(o["owner_id"])
    return out


def blocked(owners: list[dict], hit: Callable[[str], bool], aggregate_bp: int = 5000) -> tuple[int, set]:
    """(the client's blocked share in basis points, the set of blocked owner ids) under the 50 Percent Rule."""
    children: dict[Optional[str], list[dict]] = {}
    for o in owners:
        children.setdefault(o.get("parent_owner_id"), []).append(o)
    memo: dict[str, bool] = {}

    def is_blocked(o: dict) -> bool:
        oid = o["owner_id"]
        if oid not in memo:
            memo[oid] = False                  # (validate() refuses cycles; this only guards recursion)
            share = sum(int(c["ownership_bp"]) for c in children.get(oid, []) if is_blocked(c))
            memo[oid] = bool(hit(oid)) or share >= aggregate_bp
        return memo[oid]
    client_share = sum(int(o["ownership_bp"]) for o in children.get(None, []) if is_blocked(o))
    return client_share, {k for k, v in memo.items() if v}


def disclosure_problems(rec: Optional[dict], disclosure_bp: int = 2500) -> list[str]:
    """What is missing from the ownership record itself (before any search)."""
    if rec is None:
        return ["no ownership record: Andre records who owns the client (PUT /compliance/v1/clients/{id}/ownership; "
                "runbook docs/runbooks/ofac-owner-screening.md)"]
    out = []
    if rec["client_kind"] == "entity":
        if not rec.get("client_attestation"):
            out.append("the client has not attested its ownership (an entity must; MSA 1.5(c)): missing ownership "
                       "attestation")
        if not rec["owners"] and not rec.get("no_owner_25"):
            out.append("no owners are disclosed and the client has not attested that nobody owns 25% or more of it")
    eff = effective(rec["owners"])
    parents = {o.get("parent_owner_id") for o in rec["owners"]}
    for o in rec["owners"]:
        if o["kind"] == "entity" and eff[o["owner_id"]] >= Fraction(disclosure_bp, FULL) and \
                o["owner_id"] not in parents and not o.get("no_further_owners_25"):
            pct = float(eff[o["owner_id"]] * 100)
            out.append(f"owner {o['owner_id']} (an entity) holds {pct:.2f}% of the client, and its own 25% owners are "
                       "not disclosed (list them, or record that nobody owns 25% of it)")
    return out
