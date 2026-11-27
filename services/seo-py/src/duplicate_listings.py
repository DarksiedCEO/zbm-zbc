"""
SEO-L5 — duplicate listing finder (MVP spec v0.1, Local / Business Profile). One job: for a pair of Business-Profile-
like listings, decide whether they are the same business listed twice, and what to prepare: merge or remove the
duplicate, mark the old listing of a business that moved, or leave both alone. Deterministic; nothing is filed from
here, and every removal or merge needs the owner's approval and proof of ownership (spec v0.1 "Must never").

Rules:
- Same business = the same name once formatting is set aside (case, "&" / "and", punctuation, a leading "the", trailing
  legal suffixes) AND either the same local phone, or the same website at the same address with no conflicting phone:
  two listings with two different readable phones are never one business. A matching address alone never makes a
  duplicate (two businesses can share a building), and a matching name alone never does either. Two locations of a
  chain share a name and a website, and often a central toll-free number: the same name and website at two street
  addresses with different local phones, or a shared toll-free number at two addresses, is ``leave_both``
  (``possible_other_location``), never a move.
- Not duplicates, ever: a practitioner's own listing inside a practice ("Dr. Riley Juniper, DDS" at a dental office),
  a department of the business ("... - Service Department") with its own phone, and different businesses at one
  address.
- A duplicate at the same address (formatting aside) is ``merge_or_remove_duplicate``; a duplicate at a different
  street address is a move: ``mark_old_listing_moved``, the old listing being the one marked closed, else the one the
  owner touched longest ago (``old_listing`` says which). When neither tells (no closed mark, ages missing or equal),
  the move is still reported but ``old_listing`` is None and the owner decides. Known limit (AEGIS L5 round 2): two
  locations of a chain that list the SAME local number at two addresses look exactly like a move and are reported as
  one; the data cannot tell them apart, which is one reason every move needs the owner's approval.
- One house number in one city whose street words differ ("100 S St" / "100 South St", "Plz" spelled some way this
  module does not know) is ``owner_review``: neither a merge nor a move is safe on that evidence (AEGIS L5 round 4).
- Anything that cannot be read (a listing missing its name, street or city/ZIP, or neither phone nor website readable)
  is ``unreadable`` (checked before any comparison): never a duplicate. ``check`` never raises: an input it cannot read at all is ``unreadable``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import nap

_PRACTITIONER = re.compile(r"^\s*dr\.?\s|\b(?:dds|dmd|md|do|dc|pa-c|np|esq|cpa|od|dpm|phd)\b\.?\s*$|"
                           r"\b(?:attorney at law|abogad[oa])\b", re.IGNORECASE)
_DEPARTMENT = re.compile(r"\s[-–—|:]\s*(?:service|parts|billing|sales|collision|finance|accounting|"
                         r"leasing|body shop|warranty|repair|claims)\b[\w ]{0,60}?(?:department|dept\.?|office|center|desk)?"
                         r"\s*$|\b(?:department|dept\.?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class PairVerdict:
    pair_id: str
    is_duplicate: bool
    action: str                        # merge_or_remove_duplicate | mark_old_listing_moved | leave_both | owner_review
    #                                    | unreadable
    requires_owner_approval: bool
    reason: str
    old_listing: Optional[str] = None  # "a" | "b" on a move, when the update ages tell


def _age(listing: dict) -> Optional[int]:
    v = listing.get("last_owner_update_days_ago")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _closed(listing: dict) -> bool:
    v = listing.get("status")
    return isinstance(v, str) and v.strip().casefold() in ("closed", "permanently_closed", "temporarily_closed")


def check(pair: dict) -> PairVerdict:
    try:
        return _check(pair)
    except Exception:              # a hostile object (a field whose str() raises): never a duplicate, never a raise
        return PairVerdict("unknown", False, "unreadable", False, "unreadable_input")


def _check(pair: dict) -> PairVerdict:
    pid = pair.get("pair_id") if isinstance(pair, dict) else None
    pid = pid if isinstance(pid, str) and pid else "unknown"
    a = pair.get("listing_a") if isinstance(pair, dict) else None
    b = pair.get("listing_b") if isinstance(pair, dict) else None
    if not isinstance(a, dict) or not isinstance(b, dict):
        return PairVerdict(pid, False, "unreadable", False, "missing_listing")
    na, nb = nap.name_key(a.get("name")), nap.name_key(b.get("name"))
    if not na or not nb:
        return PairVerdict(pid, False, "unreadable", False, "missing_name")
    pa, pb = nap.phone_key(a.get("phone")), nap.phone_key(b.get("phone"))
    wa, wb = nap.website_key(a.get("website")), nap.website_key(b.get("website"))
    if not (pa or wa) or not (pb or wb):
        return PairVerdict(pid, False, "unreadable", False, "no_phone_or_website")

    ca = nap.city_key(a.get("city"), a.get("state"), a.get("zip"))
    cb = nap.city_key(b.get("city"), b.get("state"), b.get("zip"))
    if not (ca[0] or ca[2]) or not (cb[0] or cb[2]):
        return PairVerdict(pid, False, "unreadable", False, "missing_city")
    street_a, street_b = nap.street_key(a.get("street")), nap.street_key(b.get("street"))
    if not street_a[0] or not street_b[0]:
        return PairVerdict(pid, False, "unreadable", False, "missing_street")
    # (every field is read before any comparison, so an unreadable pair is always `unreadable`; AEGIS L5 round 2)

    def leave(reason: str) -> PairVerdict:
        return PairVerdict(pid, False, "leave_both", False, reason)

    for x, y in ((a, b), (b, a)):
        if _PRACTITIONER.search(str(y.get("name") or "")) and not _PRACTITIONER.search(str(x.get("name") or "")):
            return leave("practitioner_listing")
        if _DEPARTMENT.search(str(y.get("name") or "")) and not _DEPARTMENT.search(str(x.get("name") or "")):
            return leave("department_listing")
    if na != nb:
        return leave("different_name")
    same_phone = bool(pa) and pa == pb
    same_site = bool(wa) and wa == wb
    if pa and pb and pa != pb:         # two different local phones are two businesses or two lines: never merged
        return leave("different_phones")
    if not (same_phone or same_site):
        return leave("same_name_but_no_shared_phone_or_website")
    # the same city: the same city name and state when both have them, and the same ZIP when both have one
    same_city = all(x == y for x, y in zip(ca, cb) if x and y) and (ca[0] == cb[0] or (ca[2] and ca[2] == cb[2]))
    if same_city and street_a == street_b:
        return PairVerdict(pid, True, "merge_or_remove_duplicate", True, "same_business_same_address")
    if same_city and nap.same_number_other_words(street_a, street_b):
        # one house number, other street words ("100 S St" / "100 South St", or a suffix this module does not know):
        # neither a merge nor a move is safe, so the owner is asked (AEGIS L5 round 4)
        return PairVerdict(pid, False, "owner_review", False, "same_number_street_words_differ")
    # two addresses: a move only on the same local (not toll-free) phone; a shared site or a central number is a chain
    if not same_phone or nap.is_toll_free(pa):
        return leave("possible_other_location")
    closed_a, closed_b = _closed(a), _closed(b)
    old = None
    if closed_a != closed_b:           # a listing marked closed is the old one, whatever the ages say (AEGIS L5 round 2)
        old = "a" if closed_a else "b"
    else:
        age_a, age_b = _age(a), _age(b)
        if age_a is not None and age_b is not None and age_a != age_b:
            old = "a" if age_a > age_b else "b"
    return PairVerdict(pid, True, "mark_old_listing_moved", True, "same_business_new_address", old)
