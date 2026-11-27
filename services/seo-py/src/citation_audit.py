"""
SEO-L4 — citation audit (MVP spec v0.1, Local / Business Profile). One job: compare every directory listing of one
business with the NAP record the owner confirmed, and say for each listing whether it is correct or which fault it
carries, with the corrected values to propose. Deterministic; nothing is filed or edited from here: every correction
is a proposal the owner approves before anyone submits it to a directory.

Rules:
- Formatting is never a fault (``nap``): case, "&" / "and", punctuation and legal suffixes in the name; street
  abbreviations and unit styles; phone punctuation and a leading 1; http / https / www and a trailing slash. The
  owner's own toll-free number, when the record has one, is a correct phone.
- A listing marked closed (any status but "open", case and spaces aside; a listing with no status at all reads as
  open, since many directories carry none) while the record says the business is open is ``closed_listing``; a listing shown open while the record says it is closed is
  ``wrong_status``. Otherwise the first field that really differs names the fault, in this order: ``wrong_name``,
  ``wrong_address`` (street, unit, city, state or ZIP), ``wrong_phone``; ``differences`` lists every field that
  differs, so a listing with two faults shows both.
- A website that differs is listed in ``differences`` (``website``) but is not a planted fault on its own; a missing
  website is not a fault (many directories carry none).
- A listing that cannot be read (not an object, no name, no street or no phone) is ``unreadable``: never "correct".
  A record that cannot be read refuses the whole audit (ValueError): nothing is judged against an unknown truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import nap

VERDICTS = ("correct", "closed_listing", "wrong_status", "wrong_name", "name_needs_review", "wrong_address",
            "address_needs_review",
            "wrong_phone", "unreadable")


@dataclass(frozen=True)
class CitationVerdict:
    citation_id: str
    verdict: str
    differences: tuple          # every field that differs: name, address, phone, website, status
    proposed: dict              # field -> the record's value, for the owner to approve; empty when nothing differs
    #                             (a "correct" listing whose website differs still proposes the record's website)


def _with_suite(street: str, suite: object) -> str:
    """The record's street with its suite field: a suite that already names its kind ("Ste 4", "#4", "Floor 2") is
    used as written, a bare one ("4") is a suite; a street that already ends in a unit keeps it (AEGIS L4 round 1)."""
    if suite is None or (isinstance(suite, str) and not suite.strip()):
        return street
    text = str(suite).strip().rstrip(".,;: ")
    # a suite that names its kind anywhere ("Ste 4", "#4", "4th Floor", "Floor 2") is used as written; a bare one is a
    # suite (AEGIS L4 round 2: "4th Floor" became "Suite 4th Floor")
    if nap.street_key("1 x, " + text)[1] is None and not re.search(
            r"(?<![^\W\d_])(?:suite|ste|unit|apt|apartment|bldg|building|fl|floor|rm|room|no)(?![^\W\d_])|#", text,
            re.IGNORECASE):
        text = f"Suite {text}"
    have = set((nap.street_key(street)[1] or "").split("|")) - {""}
    if set((nap.street_key("1 x, " + text)[1] or "").split("|")) <= have:
        return street                  # the street line already names this unit
    return f"{street}, {text}"         # both count, even when the street line has another unit (AEGIS L4 round 3)


def _record_street(rec: dict) -> str:
    return " ".join(str(rec.get(k) or "") for k in ("street_number", "street_name", "street_type")).strip()


def _record(rec: object) -> dict:
    try:
        return _read_record(rec)
    except ValueError:
        raise
    except Exception as exc:       # a field that cannot be read as text: the record is unreadable (AEGIS L4 round 2)
        raise ValueError("the confirmed record cannot be read") from exc


def _read_record(rec: object) -> dict:
    if not isinstance(rec, dict):
        raise ValueError("the confirmed record must be an object")
    street = rec.get("street") if isinstance(rec.get("street"), str) else _record_street(rec)
    suite = rec.get("suite")
    if suite is not None and type(suite) not in (str, int):
        raise ValueError("the confirmed record's suite must be text or a number")
    street = _with_suite(street, suite)                # a street line plus a suite field: both count
    out = {"name": rec.get("name"), "street": street, "city": rec.get("city"), "state": rec.get("state"),
           "zip": rec.get("zip"), "phone": rec.get("phone"), "toll_free": rec.get("toll_free"),
           "website": rec.get("website"), "open": rec.get("open")}
    ck = nap.city_key(out["city"], out["state"], out["zip"])
    if not nap.name_key(out["name"]) or not nap.street_key(street)[0] or nap.phone_key(out["phone"]) is None \
            or not ck[0] or not ck[1] or not ck[2] or out["open"] not in (True, False):
        raise ValueError("the confirmed record is missing its name, street, city, state, ZIP, phone or open state")
    if out["website"] is not None and type(out["website"]) is not str:
        raise ValueError("the confirmed record's website must be text")     # (AEGIS L4 round 3)
    return out


def audit(business: dict) -> list[CitationVerdict]:
    """Judge every listing of one business against its confirmed record (``canonical``). Anything about the business
    that cannot be read refuses with ValueError (AEGIS L4 round 3: a hostile object raised RuntimeError)."""
    try:
        return _audit(business)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("the business cannot be read") from exc


def _audit(business: dict) -> list[CitationVerdict]:
    if not isinstance(business, dict):
        raise ValueError("a business must be an object")
    rec = _record(business.get("canonical"))
    listings = business.get("listings")
    if not isinstance(listings, list):
        raise ValueError("listings must be a list")
    return [_judge_safely(rec, x) for x in listings]


def _judge_safely(rec: dict, x: object) -> CitationVerdict:
    try:
        return _judge(rec, x)
    except Exception:              # a hostile object in one listing: that listing is unreadable, the audit goes on
        return CitationVerdict("unknown", "unreadable", (), {})


def _judge(rec: dict, x: object) -> CitationVerdict:
    cid = x.get("citation_id") if isinstance(x, dict) else None
    cid = cid if isinstance(cid, str) and cid else "unknown"
    if not isinstance(x, dict) or not nap.name_key(x.get("name")) or not nap.street_key(x.get("street"))[0] \
            or nap.phone_key(x.get("phone")) is None:
        return CitationVerdict(cid, "unreadable", (), {})
    diffs, proposed = [], {}
    status = x.get("status")
    # no status, or an empty one, reads as open (AEGIS L4 round 3: "" was read as closed)
    closed = not (status is None or (isinstance(status, str) and status.strip().casefold() in ("open", "")))
    if closed == rec["open"]:      # listed closed while open, or listed open (or with no status) while closed
        diffs.append("status")
        proposed["status"] = "open" if rec["open"] else "closed"
    if nap.name_key(x.get("name")) != nap.name_key(rec["name"]):
        diffs.append("name")
        proposed["name"] = rec["name"]
    same_place = (nap.street_key(x.get("street")) == nap.street_key(rec["street"]) and
                  nap.city_key(x.get("city"), x.get("state"), x.get("zip")) ==
                  nap.city_key(rec["city"], rec["state"], rec["zip"]))
    if not same_place:
        diffs.append("address")
        proposed.update({"street": rec["street"], "city": rec["city"], "state": rec["state"], "zip": rec["zip"]})
    phone = nap.phone_key(x.get("phone"))
    if phone not in {nap.phone_key(rec["phone"]), nap.phone_key(rec["toll_free"])} - {None}:
        diffs.append("phone")
        proposed["phone"] = rec["phone"]
    site = nap.website_key(x.get("website"))
    if site is not None and site != nap.website_key(rec["website"]):
        diffs.append("website")
        if rec["website"]:
            proposed["website"] = rec["website"]
    if "status" in diffs:
        verdict = "closed_listing" if rec["open"] else "wrong_status"
    elif "name" in diffs:
        # the same letters with other spacing ("Mercedes-Benz" / "Mercedes Benz"): a person checks (round 6)
        verdict = "name_needs_review" if nap.loose(nap.name_key(x.get("name"))) == nap.loose(nap.name_key(rec["name"])) \
            else "wrong_name"
    elif "address" in diffs:
        # the same house number, unit and city with street words that still differ: an unknown spelling or another
        # street; the data cannot tell, so a person checks (AEGIS L4 round 3: the inverted default)
        same_city = nap.city_key(x.get("city"), x.get("state"), x.get("zip")) == \
            nap.city_key(rec["city"], rec["state"], rec["zip"])
        lk, rk = nap.city_key(x.get("city"), x.get("state"), x.get("zip")), nap.city_key(rec["city"], rec["state"],
                                                                                       rec["zip"])
        same_street = nap.street_key(x.get("street")) == nap.street_key(rec["street"])
        verdict = "address_needs_review" if (same_city and nap.same_number_other_words(
            nap.street_key(x.get("street")), nap.street_key(rec["street"]))) or (
            same_street and nap.same_zip_other_city_words(lk, rk)) else "wrong_address"
    elif "phone" in diffs:
        verdict = "wrong_phone"
    else:
        verdict = "correct"
    return CitationVerdict(cid, verdict, tuple(diffs), proposed if verdict != "correct" or diffs else {})
