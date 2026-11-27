"""SEO-L5 duplicate listing finder and the shared NAP keys: unit rules plus certification against the shared MVP fixture
pool (SEO-L5, 200 pairs per seed). The key is the generator's planted answer; this test never imports the pool's oracle.
"""

import sys
from pathlib import Path

import pytest

import duplicate_listings as dl
import nap

POOLS = Path(__file__).resolve().parents[3] / "fixtures" / "mvp_pools"


def listing(**kw):
    base = {"name": "Desert Sky Plumbing LLC", "street": "4533 Testwell Drive", "city": "Las Vegas", "state": "NV",
            "zip": "89101", "phone": "(702) 555-0102", "website": "https://www.desert-sky.test/", "status": "open",
            "last_owner_update_days_ago": 100}
    base.update(kw)
    return base


def pair(a=None, b=None, pid="pair-1"):
    return {"pair_id": pid, "listing_a": a if a is not None else listing(), "listing_b": b if b is not None else listing()}


# ---------------------------------------------------------------- NAP keys
@pytest.mark.parametrize("x, y", [("Desert Sky Plumbing LLC", "desert sky plumbing"), ("Smith & Sons", "Smith and Sons, Inc."),
                                  ("Café Rosa", "CAFE ROSA"), ("The Fixture Co.", "fixture")])
def test_name_formatting_is_not_a_difference(x, y):
    assert nap.name_key(x) == nap.name_key(y)


@pytest.mark.parametrize("x, y", [("4533 Testwell Drive", "4533 Testwell Dr."), ("12 N Main St Ste 4", "12 North Main Street #4"),
                                  ("12 Main St, Unit 4", "12 main street suite 4")])
def test_street_formatting_is_not_a_difference(x, y):
    assert nap.street_key(x) == nap.street_key(y)


def test_a_different_street_number_or_unit_is_a_difference():
    assert nap.street_key("4533 Testwell Drive") != nap.street_key("4534 Testwell Drive")
    assert nap.street_key("12 Main St Ste 4") != nap.street_key("12 Main St Ste 5")


@pytest.mark.parametrize("raw, want", [("(702) 555-0102", "7025550102"), ("+1 702.555.0102", "7025550102"),
                                       ("1-702-555-0102", "7025550102"), ("555-0102", None), ("(102) 555-0102", None),
                                       (7025550102, None), (None, None)])
def test_phone_key(raw, want):
    assert nap.phone_key(raw) == want


@pytest.mark.parametrize("x, y", [("https://www.desert-sky.test/", "http://desert-sky.test"), ("DESERT-SKY.TEST", "desert-sky.test/")])
def test_website_formatting_is_not_a_difference(x, y):
    assert nap.website_key(x) == nap.website_key(y)


def test_toll_free():
    assert nap.is_toll_free("8885550102") and not nap.is_toll_free("7025550102") and not nap.is_toll_free(None)


# ---------------------------------------------------------------- decisions
def test_the_same_business_at_the_same_address_is_a_duplicate_to_merge_with_approval():
    v = dl.check(pair(listing(), listing(name="DESERT SKY PLUMBING", street="4533 Testwell Dr.", phone="702.555.0102")))
    assert (v.is_duplicate, v.action, v.requires_owner_approval) == (True, "merge_or_remove_duplicate", True)


def test_a_move_names_the_listing_the_owner_touched_longest_ago():
    v = dl.check(pair(listing(last_owner_update_days_ago=600), listing(street="9000 Mockridge Way", last_owner_update_days_ago=10)))
    assert (v.is_duplicate, v.action, v.requires_owner_approval, v.old_listing) == (True, "mark_old_listing_moved", True, "a")
    v = dl.check(pair(listing(street="9000 Mockridge Way", last_owner_update_days_ago=10), listing(last_owner_update_days_ago=600)))
    assert v.old_listing == "b"


@pytest.mark.parametrize("ages", [(None, 10), (10, 10), ("10", 5), (True, 5), (-1, 5)])
def test_a_move_whose_ages_cannot_tell_leaves_the_choice_to_the_owner(ages):
    v = dl.check(pair(listing(last_owner_update_days_ago=ages[0]),
                      listing(street="9000 Mockridge Way", last_owner_update_days_ago=ages[1])))
    assert v.action == "mark_old_listing_moved" and v.old_listing is None


def test_a_shared_address_alone_is_never_a_duplicate():
    v = dl.check(pair(listing(), listing(name="Sample Insurance 760", phone="(702) 555-0125", website="https://other.test/")))
    assert (v.is_duplicate, v.action) == (False, "leave_both")


def test_a_shared_name_alone_is_never_a_duplicate():
    v = dl.check(pair(listing(), listing(street="1 Other Rd", phone="(702) 555-0199", website="https://else.test/")))
    assert (v.is_duplicate, v.reason) == (False, "same_name_but_no_shared_phone_or_website")


@pytest.mark.parametrize("name", ["Dr. Riley Juniper, DDS", "Jordan Mockridge DMD", "Casey Testwell, Esq."])
def test_a_practitioner_inside_the_practice_is_left_alone(name):
    assert dl.check(pair(listing(name="Fixture Dental 964"), listing(name=name))).action == "leave_both"


@pytest.mark.parametrize("suffix", [" - Service Department", " - Parts Department", " - Billing Office", " | Sales"])
def test_a_department_is_left_alone(suffix):
    v = dl.check(pair(listing(), listing(name="Desert Sky Plumbing LLC" + suffix, phone="(702) 555-0119")))
    assert (v.is_duplicate, v.reason) == (False, "department_listing")


def test_two_chain_locations_sharing_a_website_are_not_a_move():
    v = dl.check(pair(listing(), listing(street="77 Other Ave", phone="(702) 555-0144")))
    assert (v.is_duplicate, v.action, v.reason) == (False, "leave_both", "possible_other_location")


def test_two_locations_sharing_a_central_toll_free_number_are_not_a_move():
    v = dl.check(pair(listing(phone="(888) 555-0102"), listing(street="77 Other Ave", phone="1-888-555-0102")))
    assert (v.is_duplicate, v.reason) == (False, "possible_other_location")


@pytest.mark.parametrize("bad", [None, [], "pair", {}, {"pair_id": "p", "listing_a": {}, "listing_b": listing()},
                                 pair(listing(name=None)), pair(listing(phone=None, website=None)),
                                 pair(listing(), listing(street=None, phone="(702) 555-0102"))])
def test_anything_unreadable_is_never_a_duplicate(bad):
    v = dl.check(bad)
    assert v.is_duplicate is False and v.action in ("unreadable", "leave_both")


def test_a_hostile_object_never_raises():
    class Boom:
        def __str__(self):
            raise RuntimeError("boom")
    v = dl.check(pair(listing(name=Boom())))
    assert v.is_duplicate is False


def test_every_duplicate_needs_owner_approval_and_nothing_else_does():
    for p in (pair(), pair(listing(), listing(street="9000 Mockridge Way")), pair(listing(), listing(name="Other Co 1",
              phone="(702) 555-0177", website="https://o.test/"))):
        v = dl.check(p)
        assert v.requires_owner_approval is v.is_duplicate


# ---------------------------------------------------------------- certification on the shared pool
def _pool():
    if str(POOLS) not in sys.path:
        sys.path.insert(0, str(POOLS))
    from poolgen import seo_local
    return seo_local


def _grade(seed, check=dl.check):
    pairs, key = _pool().generate_l5(seed)
    by_id = {k["pair_id"]: k for k in key}
    assert len(by_id) == len(pairs) == len(key)
    wrong = []
    for p in pairs:
        k, v = by_id[p["pair_id"]], check(p)
        if (v.is_duplicate, v.action, v.requires_owner_approval) != (k["is_duplicate"], k["action"],
                                                                     k["requires_owner_approval"]):
            wrong.append((k["kind"], v.action, v.reason))
        elif k["action"] == "mark_old_listing_moved" and v.old_listing != "a":
            wrong.append((k["kind"], "old_listing", v.old_listing))     # the pool's listing a is the stale one
    return len(pairs), wrong


@pytest.mark.parametrize("seed", ["dev-a", "dev-b", "dev-c", "l5-cert-1", "l5-cert-2", "l5-cert-3"])
def test_certification_on_the_shared_l5_pool(seed):
    n, wrong = _grade(seed)
    assert n == 200
    assert wrong == [], wrong[:5]


def test_the_certification_grader_catches_one_wrong_merge():
    def leaky(p):
        v = dl.check(p)
        if v.reason == "department_listing" and not getattr(leaky, "done", False):
            leaky.done = True
            return dl.PairVerdict(v.pair_id, True, "merge_or_remove_duplicate", True, "x")
        return v
    assert _grade("dev-a", leaky)[1]
