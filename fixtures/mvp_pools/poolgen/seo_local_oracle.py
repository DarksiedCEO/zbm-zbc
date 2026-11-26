"""Independent re-derivation of the SEO-L4 and SEO-L5 answers from the listings alone (normalisation rules written
from USPS-style abbreviations and NANP number shape, not imported from the generator)."""

from __future__ import annotations

import re

from . import common

STREET_ABBREV = {"st": "street", "ave": "avenue", "blvd": "boulevard", "rd": "road", "dr": "drive"}
UNIT_WORDS = {"suite", "ste", "unit", "#"}


def norm_name(name: str) -> str:
    n = name.lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return " ".join(n.split())


def norm_street(street: str) -> str:
    s = street.lower().replace("#", " # ")
    tokens = [t for t in re.sub(r"[^a-z0-9# ]+", " ", s).split()]
    out = []
    for t in tokens:
        if t in STREET_ABBREV:
            out.append(STREET_ABBREV[t])
        elif t in UNIT_WORDS:
            out.append("suite")
        else:
            out.append(t)
    return " ".join(out)


def norm_site(url: str) -> str:
    """http/https, a leading www. and a trailing slash are formatting, not a different website."""
    return re.sub(r"^https?://(www\.)?", "", url.strip().lower()).rstrip("/")


def norm_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def canonical_street(c: dict) -> str:
    street = f"{c['street_number']} {c['street_name']} {c['street_type']}"
    if c["suite"]:
        street += f" suite {c['suite']}"
    return norm_street(street)


def l4_verdict(canon: dict, listing: dict) -> str:
    faults = []
    if norm_name(listing["name"]) != norm_name(canon["name"]):
        faults.append("wrong_name")
    if norm_street(listing["street"]) != canonical_street(canon):
        faults.append("wrong_address")
    allowed = {norm_phone(canon["phone"])} | ({norm_phone(canon["toll_free"])} if canon["toll_free"] else set())
    if norm_phone(listing["phone"]) not in allowed:
        faults.append("wrong_phone")
    if (listing["city"].casefold(), listing["state"].upper(), listing["zip"][:5]) != \
            (canon["city"].casefold(), canon["state"].upper(), canon["zip"][:5]):
        faults.append("wrong_address")
    if norm_site(listing["website"]) != norm_site(canon["website"]):
        faults.append("wrong_website")
    if canon["open"] and listing["status"] != "open":
        faults.append("closed_listing")
    if len(faults) > 1:
        return "AMBIGUOUS:" + ",".join(faults)
    return faults[0] if faults else "correct"


def check_l4(businesses: list[dict], key: list[dict]) -> list[str]:
    errors = []
    by_id = {k["business_id"]: k for k in key}
    if sorted(by_id) != sorted(b["business_id"] for b in businesses) or len(by_id) != len(key):
        errors.append("answer rows do not match the businesses one to one")
    for b in businesses:
        k = by_id.get(b["business_id"])
        want = {"business_id": b["business_id"],
                "citations": [{"citation_id": li["citation_id"], "verdict": l4_verdict(b["canonical"], li)}
                              for li in b["listings"]]}
        if common.differs(k, want):
            errors.append(f"{b['business_id']}: key differs from the listings")
    return errors


PRACTITIONER = re.compile(r"^dr\.? .+, (dds|md|do|dmd)$", re.IGNORECASE)
DEPARTMENT = re.compile(r" - .*(department|office)$", re.IGNORECASE)


def l5_kind(pair: dict) -> str:
    a, b = pair["listing_a"], pair["listing_b"]
    if PRACTITIONER.match(b["name"].strip()):
        return "practitioner_listing"
    if DEPARTMENT.search(b["name"]) and norm_name(b["name"]).startswith(norm_name(a["name"])):
        return "department_listing"
    same_name = norm_name(a["name"]) == norm_name(b["name"])
    same_phone = norm_phone(a["phone"]) == norm_phone(b["phone"])
    same_street = norm_street(a["street"]) == norm_street(b["street"])
    if same_name and same_phone:
        return "true_duplicate" if same_street else "moved_business"
    if same_street and not same_name and not same_phone:
        return "different_business_same_address"
    return "AMBIGUOUS"


def check_l5(pairs: list[dict], key: list[dict]) -> list[str]:
    errors = []
    by_id = {k["pair_id"]: k for k in key}
    if sorted(by_id) != sorted(p["pair_id"] for p in pairs) or len(by_id) != len(key):
        errors.append("answer rows do not match the pairs one to one")
    for p in pairs:
        kind = l5_kind(p)
        dup = kind in ("true_duplicate", "moved_business")
        want = {"pair_id": p["pair_id"], "kind": kind, "is_duplicate": dup,
                "action": ("merge_or_remove_duplicate" if kind == "true_duplicate" else
                           "mark_old_listing_moved" if kind == "moved_business" else "leave_both"),
                "requires_owner_approval": dup}
        if common.differs(by_id.get(p["pair_id"]), want):
            errors.append(f"{p['pair_id']}: key {by_id.get(p['pair_id'])} vs listings {want}")
    return errors
