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
    assert (v.is_duplicate, v.action) == (False, "leave_both")


@pytest.mark.parametrize("name", ["Dr. Riley Juniper, DDS", "Jordan Mockridge DMD", "Casey Testwell, Esq."])
def test_a_practitioner_inside_the_practice_is_left_alone(name):
    assert dl.check(pair(listing(name="Fixture Dental 964"), listing(name=name))).action == "leave_both"


@pytest.mark.parametrize("suffix", [" - Service Department", " - Parts Department", " - Billing Office", " | Sales"])
def test_a_department_is_left_alone(suffix):
    v = dl.check(pair(listing(), listing(name="Desert Sky Plumbing LLC" + suffix, phone="(702) 555-0119")))
    assert (v.is_duplicate, v.reason) == (False, "department_listing")


def test_two_chain_locations_sharing_a_website_are_not_a_move():
    v = dl.check(pair(listing(), listing(street="77 Other Ave", phone="(702) 555-0144")))
    assert (v.is_duplicate, v.action) == (False, "leave_both")


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



# ---------------------------------------------------------------- AEGIS round 1 (Inquisitor, f04cd609)
SHARED = {"website": "https://food-hall.test/", "street": "100 Main St", "city": "Las Vegas", "state": "NV"}


@pytest.mark.parametrize("x, y", [("Sushi \u6771\u4eac", "Sushi \u5927\u962a"), ("Kim Motors", "Kim Motors \uc11c\ube44\uc2a4"),
                                  ("Fixture Dental", "Fixture Dental \u0434"), ("PC Repair Pros", "Repair Pros"),
                                  ("Orange Co Pools", "Orange Pools"), ("The Food Co-op", "Food Op")])
def test_names_that_differ_in_any_script_or_a_mid_name_word_are_never_merged(x, y):
    v = dl.check(pair(listing(name=x, phone=None, **SHARED), listing(name=y, phone=None, **SHARED)))
    assert v.is_duplicate is False


@pytest.mark.parametrize("x, y", [("Desert Sky Plumbing LLC", "Desert Sky Plumbing"), ("The Fixture Co", "Fixture"),
                                  ("Fixture Dental, PLLC", "fixture dental")])
def test_a_trailing_legal_suffix_or_a_leading_the_is_still_formatting(x, y):
    assert dl.check(pair(listing(name=x), listing(name=y))).is_duplicate


def test_two_states_that_share_their_first_letters_are_two_places():
    a = listing(street="100 Main St", city="Jackson", state="Mississippi", zip=None, phone="(601) 555-0101",
                website="https://chain.test/")
    b = listing(street="100 Main St", city="Jackson", state="Michigan", zip=None, phone="(517) 555-0101",
                website="https://chain.test/")
    assert dl.check(pair(a, b)).is_duplicate is False


@pytest.mark.parametrize("x, y", [("100 Main St Floor 2", "100 Main St Suite 2"), ("100 Main St Bldg 4", "100 Main St Room 4")])
def test_a_unit_of_another_kind_is_another_place(x, y):
    assert nap.street_key(x) != nap.street_key(y)


@pytest.mark.parametrize("x, y", [("100 Main St Ste #4", "100 Main St Suite 4"), ("100 Main St #4", "100 Main St Unit 4")])
def test_unit_styles_of_one_kind_are_formatting(x, y):
    assert nap.street_key(x) == nap.street_key(y)


def test_two_different_local_phones_are_never_one_business():
    v = dl.check(pair(listing(), listing(phone="(702) 555-0144")))
    assert (v.is_duplicate, v.reason) == (False, "different_phones")


def test_missing_city_and_zip_is_unreadable_never_a_duplicate():
    v = dl.check(pair(listing(city=None, state=None, zip=None), listing(city=None, state=None, zip=None)))
    assert (v.is_duplicate, v.action) == (False, "unreadable")


@pytest.mark.parametrize("kw", [{"state": "Nevada"}, {"city": "LAS VEGAS"}, {"zip": "89101-1234"}])
def test_state_city_and_zip_formatting_is_still_the_same_place(kw):
    v = dl.check(pair(listing(), listing(**kw)))
    assert v.action == "merge_or_remove_duplicate"


def test_saint_and_st_are_one_city():
    assert nap.city_key("St. Louis", "MO", None) == nap.city_key("Saint Louis", "Missouri", None)


# ---------------------------------------------------------------- AEGIS round 2 (Inquisitor, L5+L4 at 4ce895db)
@pytest.mark.parametrize("x, y", [("100 St. Charles Ave", "100 Saint Charles Avenue"), ("12 Main St. N.W.", "12 Main Street NW"),
                                  ("12 Main St N", "12 Main Street North"), ("5 Mt Vernon Rd", "5 Mount Vernon Road"),
                                  ("9 Ft. Union Blvd", "9 Fort Union Boulevard"), ("12 Main St", "12 Main Street")])
def test_street_abbreviations_in_any_position_are_formatting(x, y):
    assert nap.street_key(x) == nap.street_key(y)


def test_a_street_word_starting_with_a_unit_word_is_not_a_unit():
    assert nap.street_key("12 Main Street Northwest") == ("12 main street northwest", None)
    assert nap.street_key("12 Norwood Ave") == ("12 norwood avenue", None)


@pytest.mark.parametrize("raw, want", [("D.C.", "dc"), ("Nev.", "nv"), ("N. H.", "nh"), ("Calif.", "ca"), ("wash", "wa"),
                                       ("NV", "nv"), ("Nevada", "nv")])
def test_state_punctuation_and_ap_abbreviations(raw, want):
    assert nap.state_key(raw) == want


def test_a_zip_stored_as_a_number_keeps_its_leading_zero():
    assert nap.city_key("Boston", "MA", 2108) == nap.city_key("Boston", "MA", "02108")
    assert nap.city_key("Boston", "MA", True)[2] == ""


@pytest.mark.parametrize("x, y", [("ガス屋", "カス屋"), ("パン屋", "ハン屋")])
def test_marks_on_other_scripts_make_another_name(x, y):
    assert nap.name_key(x) != nap.name_key(y)
    assert nap.name_key("Café Rosa") == nap.name_key("CAFE ROSA")


def test_phone_digits_of_any_script_are_read():
    assert nap.phone_key("٧٠٢ ٥٥٥ ٠١٠٢") == "7025550102"


def test_a_closed_listing_is_the_old_one_on_a_move():
    v = dl.check(pair(listing(street="9000 Mockridge Way", last_owner_update_days_ago=900),
                      listing(status="permanently_closed", last_owner_update_days_ago=5)))
    assert (v.action, v.old_listing) == ("mark_old_listing_moved", "b")


def test_an_unreadable_listing_is_unreadable_before_any_comparison():
    v = dl.check(pair(listing(), listing(name="Other Co", street=None)))
    assert (v.action, v.reason) == ("unreadable", "missing_street")
    v = dl.check(pair(listing(), listing(name="Other Co", city=None, zip=None)))
    assert (v.action, v.reason) == ("unreadable", "missing_city")


# ---------------------------------------------------------------- AEGIS round 3 (Inquisitor, L5 6c0e86f3 / L4 fa92b095)
def test_street_names_of_every_script_are_kept():
    assert nap.street_key("100 東京通り") != nap.street_key("100 大阪通り")
    v = dl.check(pair(listing(street="100 東京通り"), listing(street="100 大阪通り")))
    assert v.action != "merge_or_remove_duplicate"


@pytest.mark.parametrize("x, y", [("Joe's Pizza", "Joes Pizza"), ("Joe’s Pizza", "Joes Pizza"), ("A.A.A. Locksmith", "AAA Locksmith")])
def test_punctuation_inside_a_name_is_formatting(x, y):
    assert nap.name_key(x) == nap.name_key(y)
    assert dl.check(pair(listing(name=x), listing(name=y))).action == "merge_or_remove_duplicate"


@pytest.mark.parametrize("x, y", [("12 Main St Ste 4.", "12 Main Street Suite 4"), ("12 Main St, Suite 4,", "12 Main St #4"),
                                  ("12 Main St, Suite 400, Fl 4", "12 Main Street Floor 4 Suite 400"),
                                  ("12 Main St, 4th Floor", "12 Main St Fl 4"), ("7 St. Mary's Rd", "7 Saint Marys Road")])
def test_trailing_punctuation_and_several_unit_parts_are_formatting(x, y):
    assert nap.street_key(x) == nap.street_key(y)
    assert dl.check(pair(listing(street=x), listing(street=y))).action == "merge_or_remove_duplicate"


def test_a_missing_unit_part_is_another_place():
    assert nap.street_key("12 Main St, Suite 400, Fl 4") != nap.street_key("12 Main St, Suite 400")


@pytest.mark.parametrize("raw, want", [("W.Va.", "wv"), ("W. Va.", "wv"), ("N.Mex.", "nm"), ("S.Dak.", "sd")])
def test_more_ap_state_abbreviations(raw, want):
    assert nap.state_key(raw) == want


# ---------------------------------------------------------------- AEGIS round 4 (Inquisitor, L5 52246b7b / L4 52231682)
@pytest.mark.parametrize("x, y", [("100 S St", "100 South St"), ("100 N St", "100 North Street")])
def test_a_lettered_street_is_never_a_directional_one(x, y):
    assert nap.street_key(x) != nap.street_key(y)
    v = dl.check(pair(listing(street=x), listing(street=y)))
    assert v.is_duplicate is False and v.action == "owner_review"


@pytest.mark.parametrize("x, y", [("5 Main St Ext", "5 Main Street Extension"), ("5 Civic Plz", "5 Civic Plaza"),
                                  ("5 Town Ctr", "5 Town Center"), ("5 Main St Ste 4-B", "5 Main St Suite 4B"),
                                  ("5 Main St Spc 12", "5 Main St Space 12"), ("5 Main St, Fourth Floor", "5 Main St 4th Floor")])
def test_usps_suffixes_and_units_are_formatting(x, y):
    assert nap.street_key(x) == nap.street_key(y)
    assert dl.check(pair(listing(street=x), listing(street=y))).action == "merge_or_remove_duplicate"


@pytest.mark.parametrize("x, y", [("N Las Vegas", "North Las Vegas"), ("W. Hollywood", "West Hollywood"),
                                  ("Redondo Bch", "Redondo Beach")])
def test_city_abbreviations_are_formatting(x, y):
    assert nap.city_key(x, "NV", None)[0] == nap.city_key(y, "NV", None)[0]


@pytest.mark.parametrize("x, y", [("J. R. Plumbing", "J.R. Plumbing"), ("A. A. A. Locksmith", "A.A.A. Locksmith"),
                                  ("A.J Plumbing", "AJ Plumbing")])
def test_spaced_initials_are_formatting(x, y):
    assert nap.name_key(x) == nap.name_key(y)
    assert dl.check(pair(listing(name=x), listing(name=y))).action == "merge_or_remove_duplicate"


def test_an_unknown_street_spelling_at_one_number_asks_the_owner():
    v = dl.check(pair(listing(street="4533 Testwell Drv"), listing(street="4533 Testwell Dryve")))
    assert (v.is_duplicate, v.action, v.requires_owner_approval) == (False, "owner_review", False)


# ---------------------------------------------------------------- AEGIS round 5 (Inquisitor, L5 9453329b / L4 5c37e1d1)
@pytest.mark.parametrize("x, y", [("100A S St", "100A South St"), ("100 1/2 S St", "100 1/2 South St"),
                                  ("1200-B N St", "1200-B North St")])
def test_a_lettered_street_after_any_house_number_is_never_directional(x, y):
    assert nap.street_key(x) != nap.street_key(y)
    assert dl.check(pair(listing(street=x), listing(street=y))).action != "merge_or_remove_duplicate"


@pytest.mark.parametrize("x, y", [("1 Pier Ave", "1 Pier Avenue"), ("5 Post Office Rd", "5 Post Office Road"),
                                  ("5 Lot Rd", "5 Lot Road")])
def test_a_street_name_that_starts_like_a_unit_is_a_street(x, y):
    assert nap.street_key(x) == nap.street_key(y) and nap.street_key(x)[1] is None
    assert dl.check(pair(listing(street=x), listing(street=y))).action == "merge_or_remove_duplicate"


@pytest.mark.parametrize("x, y", [("5 Main St Ste 1-10", "5 Main St Ste 110"), ("5 Main St Unit 2-14", "5 Main St Unit 214"),
                                  ("5 Main St Spc 1-2", "5 Main St Spc 12")])
def test_a_hyphen_between_digits_is_another_unit(x, y):
    assert nap.street_key(x) != nap.street_key(y)
    assert dl.check(pair(listing(street=x), listing(street=y))).is_duplicate is False or \
        dl.check(pair(listing(street=x), listing(street=y))).action != "merge_or_remove_duplicate"


@pytest.mark.parametrize("x, y", [("Mtn View", "Mountain View"), ("Grand Jct", "Grand Junction"), ("Idaho Fls", "Idaho Falls"),
                                  ("Palm Bch Gdns", "Palm Beach Gardens")])
def test_more_city_abbreviations_are_formatting(x, y):
    assert nap.city_key(x, "CA", None)[0] == nap.city_key(y, "CA", None)[0]


# ---------------------------------------------------------------- AEGIS round 6 (Inquisitor, L5 325bd497 / L4 e4dc6fa9)
@pytest.mark.parametrize("x, y", [("1200-B N St", "1200B N St"), ("100-A Main St", "100A Main St"),
                                  ("100½ Main St", "100 1/2 Main St")])
def test_house_number_formatting_is_formatting(x, y):
    assert nap.street_key(x) == nap.street_key(y)
    assert dl.check(pair(listing(street=x), listing(street=y))).action == "merge_or_remove_duplicate"


def test_two_units_of_one_kind_keep_their_order():
    assert nap.street_key("5 Main St Unit 5, Ste 4") != nap.street_key("5 Main St Unit 4, Ste 5")


@pytest.mark.parametrize("x, y", [("A-1 Plumbing", "A1 Plumbing"), ("Desert-Sky Plumbing", "DesertSky Plumbing")])
def test_a_hyphen_inside_a_name_is_formatting(x, y):
    assert nap.name_key(x) == nap.name_key(y)


@pytest.mark.parametrize("x, y", [("Clinton Twp", "Clinton Township"), ("Shaker Hgts", "Shaker Heights"), ("O'Fallon", "OFallon")])
def test_more_city_formatting(x, y):
    assert nap.city_key(x, "MI", "48038") == nap.city_key(y, "MI", "48038")


def test_an_unknown_city_spelling_with_the_same_zip_asks_the_owner():
    v = dl.check(pair(listing(city="Las Vegas"), listing(city="Lss Vegas")))
    assert (v.is_duplicate, v.action) == (False, "owner_review")


# ---------------------------------------------------------------- AEGIS round 7 (Inquisitor, L5 25c20c24 / L4 570b349a)
@pytest.mark.parametrize("x, y", [("Mercedes-Benz of Las Vegas", "Mercedes Benz of Las Vegas"),
                                  ("Mercedes–Benz of Las Vegas", "Mercedes Benz of Las Vegas")])
def test_a_name_that_differs_only_in_spacing_asks_the_owner(x, y):
    v = dl.check(pair(listing(name=x), listing(name=y)))
    assert (v.is_duplicate, v.action) == (False, "owner_review")


@pytest.mark.parametrize("x, y", [("100 Main St Suite-B", "100 Main St Suite B"), ("100 Main St Apt-C", "100 Main St Apt C"),
                                  ("100 Main St Bldg-A", "100 Main St Bldg A")])
def test_a_unit_joined_by_a_dash_is_still_a_unit(x, y):
    assert nap.street_key(x) == nap.street_key(y) and nap.street_key(x)[1]


@pytest.mark.parametrize("x, y", [("100⅜ Main St", "100 3/8 Main St"), ("100⅝ Main St", "100 5/8 Main St"),
                                  ("100⅕ Main St", "100 1/5 Main St")])
def test_every_vulgar_fraction_keeps_the_house_number(x, y):
    assert nap.street_key(x) == nap.street_key(y)


def test_a_hyphenated_city_without_a_zip_is_one_city():
    v = dl.check(pair(listing(city="Winston-Salem", state="NC", zip=None), listing(city="Winston Salem", state="NC", zip=None)))
    assert v.action == "merge_or_remove_duplicate"


# ---------------------------------------------------------------- AEGIS round 8 (Inquisitor, L5 c9324159 / L4 8b4b2b4b)
@pytest.mark.parametrize("x, y", [("Riley Juniper DDS", "Riley Juniper DDS PLLC"), ("Riley Juniper DDS", "Riley Juniper D.D.S."),
                                  ("Smile Dental DC", "Smile Dental DC, LLC")])
def test_a_practitioner_or_department_pattern_never_splits_one_name(x, y):
    assert dl.check(pair(listing(name=x), listing(name=y))).action == "merge_or_remove_duplicate"


def test_a_practitioner_inside_a_practice_is_still_left_alone():
    assert dl.check(pair(listing(name="Fixture Dental 964"), listing(name="Dr. Riley Juniper, DDS"))).action == "leave_both"


def test_zero_thirds_keeps_the_house_number():
    assert nap.street_key("100↉ Main St") == nap.street_key("100 0/3 Main St")


# ---------------------------------------------------------------- AEGIS round 9 (Inquisitor, L5 5802246c / L4 1109b645)
@pytest.mark.parametrize("x, y", [("100 Main St Suite E", "100 Main St Suite W"), ("100 Main St Apt N", "100 Main St Apt S"),
                                  ("4533 Testwell Drive #E", "4533 Testwell Drive E")])
def test_a_one_letter_unit_that_is_also_a_direction_is_a_unit(x, y):
    assert nap.street_key(x) != nap.street_key(y)
    assert dl.check(pair(listing(street=x), listing(street=y))).action != "merge_or_remove_duplicate"


@pytest.mark.parametrize("x, y", [("100 1/2 Main St", "100 Main St"), ("100-102 Main St", "100 Main St")])
def test_another_house_number_is_never_only_other_street_words(x, y):
    assert not nap.same_number_other_words(nap.street_key(x), nap.street_key(y))


@pytest.mark.parametrize("x, y", [("Riley Juniper DDS", "RileyJuniper D.D.S."), ("Dr. Riley Juniper", "Dr.RileyJuniper"),
                                  ("Desert Sky Plumbing - Repair", "DesertSky Plumbing Repair")])
def test_spacing_variants_are_asked_of_the_owner_not_set_aside_by_patterns(x, y):
    assert dl.check(pair(listing(name=x), listing(name=y))).action == "owner_review"


def test_superscript_fractions_keep_the_house_number():
    assert nap.street_key("100¹⁄₂ Main St") == nap.street_key("100 1/2 Main St")


# ---------------------------------------------------------------- AEGIS L5 round 10
@pytest.mark.parametrize("x, y", [("100 5th Ave", "100 Fifth Ave"), ("100 1st St", "100 First St"),
                                  ("100 12th St", "100 Twelfth St"), ("100 21st St", "100 Twenty-First St"),
                                  ("100 21st St", "100 twenty first St"), ("100 I-5", "100 Interstate 5"),
                                  ("200 Fourth Floor Plaza", "200 4th Floor Plaza")])
def test_ordinal_words_and_numerals_are_one_street(x, y):
    assert nap.street_key(x) == nap.street_key(y)
    assert dl.check(pair(listing(street=x), listing(street=y))).action == "merge_or_remove_duplicate"


@pytest.mark.parametrize("x, y", [("100 5th Ave", "100 6th Ave"), ("100 Fifth Ave", "100 Sixth Ave"),
                                  ("100 21st St", "100 Twenty-Second St"), ("100 I-5", "100 I-15")])
def test_other_ordinal_streets_stay_other_streets(x, y):
    assert nap.street_key(x) != nap.street_key(y)


def test_the_floor_reading_of_an_ordinal_word_is_kept():
    assert nap.street_key("4533 Testwell Drive Fourth Floor") == nap.street_key("4533 Testwell Drive, 4th Fl")


@pytest.mark.parametrize("x, y", [("4533 Testwell Drive Suite NE", "4533 Testwell Drive Suite NW"),
                                  ("4533 Testwell Drive Ste SE", "4533 Testwell Drive Ste SW"),
                                  ("4533 Testwell Drive Unit NE", "4533 Testwell Drive")])
def test_a_two_letter_direction_after_a_unit_word_is_a_unit(x, y):
    a, b = nap.street_key(x), nap.street_key(y)
    assert a[0] == b[0] and a[1] != b[1]
    assert nap.street_key("4533 Testwell Drive Suite NE")[1] == "unit:ne"
