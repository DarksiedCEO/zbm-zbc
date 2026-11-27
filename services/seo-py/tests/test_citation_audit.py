"""SEO-L4 citation audit: unit rules plus certification against the shared MVP fixture pool (SEO-L4, 200 businesses
per seed, 15-45 listings each). The key is the generator's planted answer; this test never imports the pool's oracle."""

import sys
from pathlib import Path

import pytest

import citation_audit as ca

POOLS = Path(__file__).resolve().parents[3] / "fixtures" / "mvp_pools"
RECORD = {"name": "Desert Sky Plumbing & Air 512", "street_number": "4533", "street_name": "Testwell",
          "street_type": "Drive", "suite": "4", "city": "Las Vegas", "state": "NV", "zip": "89101",
          "phone": "+17025550102", "toll_free": "+18885550120", "website": "https://www.desert-sky-4120.test/",
          "open": True}


def listing(**kw):
    base = {"citation_id": "cit-1", "name": "Desert Sky Plumbing & Air 512", "street": "4533 Testwell Drive, Suite 4",
            "city": "Las Vegas", "state": "NV", "zip": "89101", "phone": "(702) 555-0102",
            "website": "https://www.desert-sky-4120.test/", "status": "open"}
    base.update(kw)
    return base


def one(**kw):
    return ca.audit({"canonical": RECORD, "listings": [listing(**kw)]})[0]


@pytest.mark.parametrize("kw", [{}, {"name": "DESERT SKY PLUMBING AND AIR 512"}, {"street": "4533 Testwell Dr., Ste. 4"},
                                {"street": "4533 Testwell Dr #4"}, {"phone": "702.555.0102"}, {"phone": "1-702-555-0102"},
                                {"phone": "(888) 555-0120"}, {"website": "http://desert-sky-4120.test"},
                                {"website": None}, {"status": None}, {"zip": "89101-1234"}])
def test_formatting_and_the_owners_toll_free_number_are_correct(kw):
    v = one(**kw)
    assert (v.verdict, v.differences, v.proposed) == ("correct", (), {})


@pytest.mark.parametrize("kw, verdict, field", [
    ({"name": "Desert Sky Plumbing & Air 512 Express"}, "wrong_name", "name"),
    ({"street": "4633 Testwell Drive, Suite 4"}, "wrong_address", "address"),
    ({"street": "4533 Testwell Drive, Suite 5"}, "wrong_address", "address"),
    ({"city": "Henderson"}, "wrong_address", "address"),
    ({"phone": "(702) 555-0119"}, "wrong_phone", "phone"),
    ({"phone": "(888) 555-0121"}, "wrong_phone", "phone"),
    ({"status": "permanently_closed"}, "closed_listing", "status"),
    ({"status": "temporarily_closed"}, "closed_listing", "status")])
def test_each_fault_is_named_and_the_record_value_is_proposed(kw, verdict, field):
    v = one(**kw)
    assert v.verdict == verdict and field in v.differences and v.proposed


def test_a_listing_with_two_faults_shows_both():
    v = one(name="Other Name 1", phone="(702) 555-0119")
    assert v.verdict == "wrong_name" and set(v.differences) == {"name", "phone"}
    assert v.proposed["name"] == RECORD["name"] and v.proposed["phone"] == RECORD["phone"]


def test_a_different_website_is_listed_but_is_not_a_fault_on_its_own():
    v = one(website="https://elsewhere.test/")
    assert v.verdict == "correct" and v.differences == ("website",) and v.proposed == {"website": RECORD["website"]}


def test_a_closed_business_listed_as_closed_is_not_a_fault():
    rec = dict(RECORD, open=False)
    assert ca.audit({"canonical": rec, "listings": [listing(status="permanently_closed")]})[0].verdict == "correct"


@pytest.mark.parametrize("bad", [None, "x", {"citation_id": "c"}, listing(name=None), listing(street=""),
                                 listing(phone="555-0102"), listing(phone=None)])
def test_an_unreadable_listing_is_never_correct(bad):
    assert ca.audit({"canonical": RECORD, "listings": [bad]})[0].verdict == "unreadable"


@pytest.mark.parametrize("rec", [None, {}, dict(RECORD, name=""), dict(RECORD, phone="555"), dict(RECORD, open="yes"),
                                 dict(RECORD, street_number=None, street_name=None, street_type=None),
                                 dict(RECORD, city=None)])
def test_an_unreadable_record_refuses_the_whole_audit(rec):
    with pytest.raises(ValueError):
        ca.audit({"canonical": rec, "listings": [listing()]})


@pytest.mark.parametrize("bad", [None, [], {"canonical": RECORD}, {"canonical": RECORD, "listings": "x"}])
def test_an_unreadable_business_refuses(bad):
    with pytest.raises(ValueError):
        ca.audit(bad)


# ---------------------------------------------------------------- certification on the shared pool
def _pool():
    if str(POOLS) not in sys.path:
        sys.path.insert(0, str(POOLS))
    from poolgen import seo_local
    return seo_local


def _grade(seed, audit=ca.audit):
    subjects, key = _pool().generate_l4(seed)
    by_id = {k["business_id"]: k for k in key}
    assert len(by_id) == len(subjects) == len(key)
    wrong, n = [], 0
    for b in subjects:
        want = {c["citation_id"]: c["verdict"] for c in by_id[b["business_id"]]["citations"]}
        got = audit(b)
        assert sorted(v.citation_id for v in got) == sorted(want)
        for v in got:
            n += 1
            if v.verdict != want[v.citation_id]:
                wrong.append((want[v.citation_id], v.verdict, v.differences))
    return n, wrong


@pytest.mark.parametrize("seed", ["dev-a", "dev-b", "dev-c", "l4-cert-1", "l4-cert-2", "l4-cert-3"])
def test_certification_on_the_shared_l4_pool(seed):
    n, wrong = _grade(seed)
    assert n >= 200 * 15
    assert wrong == [], wrong[:5]


def test_the_certification_grader_catches_one_missed_fault():
    def leaky(b):
        out = ca.audit(b)
        for i, v in enumerate(out):
            if v.verdict == "wrong_phone" and not getattr(leaky, "done", False):
                leaky.done = True
                out[i] = ca.CitationVerdict(v.citation_id, "correct", (), {})
        return out
    assert _grade("dev-a", leaky)[1]


@pytest.mark.parametrize("kw", [{"status": "OPEN"}, {"status": " Open "}, {"state": "Nevada"}, {"state": "nv"},
                                {"phone": "\uff17\uff10\uff12 555 0102"}])
def test_more_formatting_is_correct(kw):
    assert one(**kw).verdict == "correct"


def test_a_listing_shown_open_while_the_business_is_closed_is_wrong_status():
    rec = dict(RECORD, open=False)
    v = ca.audit({"canonical": rec, "listings": [listing(status="open")]})[0]
    assert v.verdict == "wrong_status" and v.proposed["status"] == "closed"


def test_a_hostile_listing_is_unreadable_and_the_audit_goes_on():
    class Boom(dict):
        def get(self, *a):
            raise RuntimeError("boom")
    out = ca.audit({"canonical": RECORD, "listings": [Boom(), listing()]})
    assert [v.verdict for v in out] == ["unreadable", "correct"]


# ---------------------------------------------------------------- AEGIS round 1 (Inquisitor, L5+L4 at 4ce895db)
def _biz(canonical, listing):
    return {"canonical": canonical, "listings": [dict(listing, citation_id="c1")]}


_REC = {"name": "Desert Sky Plumbing", "street": "12 Main St", "city": "Las Vegas", "state": "NV", "zip": "89101",
        "phone": "(702) 555-0102", "website": "https://desert-sky.test/", "open": True}
_LIST = {"name": "Desert Sky Plumbing", "street": "12 Main St", "city": "Las Vegas", "state": "NV", "zip": "89101",
         "phone": "702-555-0102", "status": "open"}


@pytest.mark.parametrize("suite, listed", [("4", "12 Main St Suite 4"), (4, "12 Main Street #4"), ("Ste 4", "12 Main St Unit 4"),
                                           ("Floor 2", "12 Main St Fl 2")])
def test_a_record_street_and_suite_field_are_both_used(suite, listed):
    v = ca.audit(_biz(dict(_REC, suite=suite), dict(_LIST, street=listed)))[0]
    assert v.verdict == "correct"
    v = ca.audit(_biz(dict(_REC, suite=suite), _LIST))[0]
    assert v.verdict == "wrong_address"


def test_a_suite_that_is_not_text_or_a_number_refuses_the_audit():
    with pytest.raises(ValueError):
        ca.audit(_biz(dict(_REC, suite=["4"]), _LIST))


@pytest.mark.parametrize("kw", [{"street": "12 Main St."}, {"state": "Nev."}, {"zip": 89101}, {"phone": "٧٠٢ 555 0102"}])
def test_formatting_in_a_listing_is_still_correct(kw):
    assert ca.audit(_biz(_REC, dict(_LIST, **kw)))[0].verdict == "correct"


def test_mount_and_mt_are_one_street():
    rec = dict(_REC, street="5 Mount Vernon Road")
    assert ca.audit(_biz(rec, dict(_LIST, street="5 Mt. Vernon Rd")))[0].verdict == "correct"


def test_no_status_reads_as_open():
    lst = {k: v for k, v in _LIST.items() if k != "status"}
    assert ca.audit(_biz(_REC, lst))[0].verdict == "correct"
    assert ca.audit(_biz(dict(_REC, open=False), lst))[0].verdict == "wrong_status"
