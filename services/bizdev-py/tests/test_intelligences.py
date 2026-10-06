"""Unit vectors for the deterministic intelligences that carry money, identity or legal exposure."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import money
from intelligences import i01_qualification, i02_identity, i03_deadline, i05_gov_checklist, i07_deal_threshold, \
    i09_replies, i12_tax_refs

NOW = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)


def test_deadline_boundaries_and_offsets():
    assert i03_deadline.passed("2026-10-06T18:00:00Z", NOW) is True
    assert i03_deadline.passed("2026-10-06T18:00:01Z", NOW) is False
    assert i03_deadline.passed("2026-10-06T11:00:01-07:00", NOW) is False      # 18:00:01 UTC
    assert i03_deadline.passed("2026-10-06T11:00:00-07:00", NOW) is True
    assert i03_deadline.passed("not a date", NOW) is True                      # unreadable counts as passed
    assert i03_deadline.passed("2026-10-07T00:00:00", NOW) is True             # naive: unreadable


def test_org_key_variants():
    for name in ("Acme Inc", "ACME, Incorporated", "The Acme Co.", "acme  llc", "Ácme"):
        assert i02_identity.org_key(name) == "acme", name
    assert i02_identity.org_key("Inc.") is None


def test_email_canonical():
    assert i02_identity.email(" Pat+Deals@West.TEST ") == "pat@west.test"
    assert i02_identity.email("not-an-email") is None


def test_qualification_requires_every_criterion():
    with pytest.raises(ValueError):
        i01_qualification.recommend({"scope_fit": "yes"})
    with pytest.raises(ValueError):
        i01_qualification.recommend({c: "maybe" for c in i01_qualification.CRITERIA})


def test_checklist_hash_binds_pursuit_and_label():
    a = i05_gov_checklist.item_sha256("p1", "custom", "x")
    assert a != i05_gov_checklist.item_sha256("p2", "custom", "x") != i05_gov_checklist.item_sha256("p1", "custom",
                                                                                                    "y")


def test_threshold_group_and_window():
    deals = {"a": {"keys": ["ref:a", "domain:x.test"], "value": "6000.00", "opened_at": "2026-10-01T00:00:00Z",
                   "status": "identified"},
             "b": {"keys": ["ref:b", "domain:x.test"], "value": "6000.00", "opened_at": "2026-10-02T00:00:00Z",
                   "status": "registered"},
             "c": {"keys": ["ref:b"], "value": "1.00", "opened_at": "2026-10-02T00:00:00Z", "status": "lost"},
             "d": {"keys": ["ref:a"], "value": "9.00", "opened_at": "2024-01-01T00:00:00Z", "status": "won"}}
    total, members = i07_deal_threshold.aggregate("a", deals, NOW, 365)
    assert total == Decimal("12000.00") and members == ["a", "b"]
    assert i07_deal_threshold.needs_andre(Decimal("10000.00"), Decimal("10000.00")) is False
    assert i07_deal_threshold.needs_andre(Decimal("10000.01"), Decimal("10000.00")) is True
    deals["a"]["opened_at"] = "garbage"
    assert i07_deal_threshold.aggregate("b", deals, NOW, 365)[0] == Decimal("12000.00")


def test_money_parse_refuses_floats_and_noncanonical():
    for bad in (1.5, 1, "1", "1.5", "1.500", "-1.00", "NaN", "Infinity", "1e2", True):
        with pytest.raises(money.MoneyError):
            money.parse(bad)
    with pytest.raises(money.MoneyError):
        money.parse_rate("0.00")
    with pytest.raises(money.MoneyError):
        money.commission_total(Decimal("1.00"), Decimal("NaN"))
    assert money.commission_total(Decimal("0.10"), Decimal("5.00")) == Decimal("0.01")     # 0.005 half up


def test_raw_tax_id_shapes():
    for raw in ("123-45-6789", "123 45 6789", "123456789", "12-3456789", "SSN: 1", "EIN 9", "ein#12",
                "taxpayer id 1 23 45 6789", "123_45_6789", "12/3456789", "123|45|6789", "12 : 3456789",
                "１２３-４５-６７８９"):
        assert i12_tax_refs.raw_tax_id(raw), raw
    for ok in ("Suite 400, Los Angeles 90001", "partner since 2019", "12345678", "1 2 3 4 5 6 7 8",
               "+1 310 555 0100", "1234567890", "310-555-0100", "hubspot:12345678901"):
        assert not i12_tax_refs.raw_tax_id(ok), ok          # a run of exactly nine digits only (AEGIS round 2 N1)
    assert i12_tax_refs.ref_ok("vault:tax:AbCdEf_123-45-xyz") is True
    assert i12_tax_refs.ref_ok("vault:tax:12345678901234567") is False
    assert i12_tax_refs.ref_ok("vault:tax:1a2b3c4d5e6f7g8h9i") is False          # nine digits however arranged
    assert i12_tax_refs.ref_ok("vault:tax:1a2b3c4d5e6f7g8hij") is True


def test_reply_classification_never_needed_for_the_hold():
    assert i09_replies.classify("S.T.O.P") == "unsubscribe"
    assert i09_replies.classify("ＳＴＯＰ") == "unsubscribe"
    assert i09_replies.classify("sounds good, call me") == "interested"
    assert i09_replies.classify("what is this about") == "review"
    assert timedelta
