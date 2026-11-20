"""Unit tests for Priya's tenant word list T (priya-spec/2 2.4 T-1..T-4, OW-4, PR-3; lead ruling NB-9 option (a)).

T has two inputs: operator-added words (OwnerWordBook, audited) and the harvest port TenantTermSource. Live harvesting is
OFF by default and fails closed (T = operator words only). The FixtureTermSource below is the test-only implementation.
ASCII-only source: every non-ASCII input is a \\u escape."""

from __future__ import annotations

import hashlib
import itertools
from datetime import datetime, timezone

import pytest
from helpers import FakeLedger

from agents import priya_gate as g
from agents import priya_tenant as pt
from ledger import Recorder

NOW = datetime(2026, 11, 14, 19, 30, tzinfo=timezone.utc)
KOSHKA = "\u041a\u043e\u0448\u043a\u0430"  # Cyrillic "Koshka"
MIXED = "koshka\u043a"  # Latin + Cyrillic letter in one word


class FixtureTermSource:
    """Test-only TenantTermSource: per-tenant page texts and catalog product titles."""

    name = "fixture"

    def __init__(self, pages=None, catalog=None, fail=False):
        self._pages = pages or {}
        self._catalog = catalog or {}
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def pages(self, tenant_id):
        self.calls.append(("pages", tenant_id))
        if self.fail:
            raise RuntimeError("fixture source down")
        return list(self._pages.get(tenant_id, ()))

    def catalog_titles(self, tenant_id):
        self.calls.append(("catalog", tenant_id))
        return list(self._catalog.get(tenant_id, ()))


def harvest(pages=(), catalog=(), tenant="t-a", book=None):
    src = FixtureTermSource({tenant: list(pages)}, {tenant: list(catalog)})
    return pt.build_tenant_vocab(
        tenant, owner_book=book, source=src, harvest_enabled=True
    )


def new_book():
    led = FakeLedger()
    return pt.OwnerWordBook(Recorder(led)), led


# ---- T-1 (a): harvested words ----
def test_page_word_on_two_pages_accepted():
    t = harvest(pages=["sphynx sweater sale", "warm Sphynx sweater"])
    assert "sphynx" in t and "sweater" in t
    assert "sale" not in t and "warm" not in t
    assert t.harvest == "ON"


def test_three_letter_word_rejected():
    t = harvest(pages=["the cat bed", "cat bed the"])
    assert len(t) == 0
    assert t.rejected["TOO_SHORT"] == 3


def test_one_page_word_rejected():
    t = harvest(pages=["fleece hoodie", "hoodie only"])
    assert "fleece" not in t and "only" not in t
    assert "hoodie" in t
    assert t.rejected["FEW_PAGES"] == 2


def test_catalog_title_word_accepted_once():
    t = harvest(catalog=["Fleece Hoodie for Hairless Cats"])
    assert {"fleece", "hoodie", "hairless", "cats"} <= set(t.words)
    assert "for" not in t


def test_catalog_word_is_canonical_non_latin():
    t = harvest(catalog=[KOSHKA])
    assert g.canonical(KOSHKA) in t
    assert g.canonical(KOSHKA) == "\u043a\u043e\u0448\u043a\u0430"


def test_letters_only():
    t = harvest(pages=["size xl2024 tee", "xl2024 tee shirt"], catalog=["2024 edition"])
    assert "xl2024" not in t and "2024" not in t
    assert "edition" in t
    assert t.rejected["NOT_LETTERS"] == 2


def test_harvested_mixed_script_word_rejected_og2():
    t = harvest(catalog=[MIXED + " hoodie"])
    assert "hoodie" in t
    assert g.canonical(MIXED) not in t
    assert t.rejected["OG-2"] == 1


# ---- T-1 (b) + T-2: owner "allow this word" ----
@pytest.mark.parametrize(
    "word,reason",
    [
        ("john@doe", "OG-1"),
        ("@kitten", "OG-1"),
        (MIXED, "OG-2"),
        ("4111111111111111", "STAGE2"),
        ("hairless cat", "NOT_ONE_WORD"),
        ("", "NOT_ONE_WORD"),
    ],
)
def test_owner_add_rejected(word, reason):
    book, led = new_book()
    entry, why = book.add("t-a", word, "andre", NOW)
    assert entry is None and why == reason
    assert led.events == []
    assert len(pt.build_tenant_vocab("t-a", owner_book=book)) == 0


def test_owner_add_audited():
    book, led = new_book()
    entry, why = book.add("t-a", "Sphynxwear", "andre", NOW)
    assert why is None
    assert (entry.tenant_id, entry.word, entry.actor, entry.at) == (
        "t-a",
        "sphynxwear",
        "andre",
        NOW.isoformat(),
    )
    (ev,) = led.events
    assert (
        ev["event_type"] == pt.OWNER_ADD_EVENT
        and ev["actor"] == "andre"
        and ev["subject_id"] == "t-a"
    )
    assert ev["event_id"] == entry.event_id
    assert ev["_payload"] == {
        "tenant_id": "t-a",
        "word": "sphynxwear",
        "actor": "andre",
        "at": NOW.isoformat(),
        "rule": "priya-spec/2 T-1(b)",
    }
    t = pt.build_tenant_vocab("t-a", owner_book=book)
    assert "sphynxwear" in t and t.harvest == "OFF"
    again, why2 = book.add("t-a", "sphynxwear", "andre", NOW)
    assert why2 is None and again == entry and len(led.events) == 1


def test_owner_add_refused_when_audit_not_recorded():
    book, led = new_book()
    led.fail = True
    entry, why = book.add("t-a", "sphynxwear", "andre", NOW)
    assert entry is None and why == "AUDIT_NOT_RECORDED"
    assert "sphynxwear" not in pt.build_tenant_vocab("t-a", owner_book=book)


def test_owner_add_needs_aware_time():
    book, led = new_book()
    entry, why = book.add("t-a", "sphynxwear", "andre", datetime(2026, 11, 14, 12, 0))
    assert entry is None and why == "BAD_TIME" and led.events == []


# ---- T-2 cap: 20,000 words per tenant ----
def _words(n):
    return [
        "".join(p)
        for p in itertools.islice(itertools.product("bcdfghjklm", repeat=5), n)
    ]


def test_cap_20001st_rejected(monkeypatch):
    assert pt.TENANT_WORDS_MAX == 20000
    monkeypatch.setattr(
        pt, "t2_reason", lambda w: None
    )  # cap rule only; T-2 itself is tested above
    words = _words(20001)
    t = harvest(catalog=[" ".join(words)])
    assert len(t) == 20000
    assert words[-1] not in t and words[-2] in t
    assert t.rejected["T_CAP"] == 1


def test_cap_owner_words_first(monkeypatch):
    monkeypatch.setattr(pt, "t2_reason", lambda w: None)
    book, _ = new_book()
    book.add("t-a", "zzzzowner", "andre", NOW)
    words = _words(20000)
    t = harvest(catalog=[" ".join(words)], book=book)
    assert len(t) == 20000 and "zzzzowner" in t and words[-1] not in t


def test_owner_book_cap(monkeypatch):
    monkeypatch.setattr(pt, "TENANT_WORDS_MAX", 2)
    book, led = new_book()
    assert book.add("t-a", "alpha", "andre", NOW)[1] is None
    assert book.add("t-a", "bravo", "andre", NOW)[1] is None
    assert book.add("t-a", "charlie", "andre", NOW) == (None, "T_CAP")
    assert len(led.events) == 2


# ---- NB-9: live harvesting OFF by default, fails closed ----
def test_harvest_off_by_default():
    src = FixtureTermSource(
        {"t-a": ["fleece hoodie", "fleece hoodie"]}, {"t-a": ["Sphynx Sweater"]}
    )
    t = pt.build_tenant_vocab("t-a", source=src)
    assert len(t) == 0 and t.harvest == "OFF" and src.calls == []


def test_harvest_not_connected():
    assert pt.build_tenant_vocab("t-a", harvest_enabled=True).harvest == "NOT_CONNECTED"
    t = pt.build_tenant_vocab(
        "t-a", source=pt.NotConnectedTermSource(), harvest_enabled=True
    )
    assert t.harvest == "NOT_CONNECTED" and len(t) == 0


@pytest.mark.parametrize(
    "src", [FixtureTermSource(fail=True), FixtureTermSource({"t-a": [b"bytes page"]})]
)
def test_harvest_failure_fails_closed(src):
    book, _ = new_book()
    book.add("t-a", "sphynxwear", "andre", NOW)
    src._catalog = {"t-a": ["Fleece Hoodie"]}
    t = pt.build_tenant_vocab("t-a", owner_book=book, source=src, harvest_enabled=True)
    assert t.harvest == "FAILED" and set(t.words) == {"sphynxwear"}


def test_confusables_mismatch_rejects_every_word(monkeypatch):
    monkeypatch.setattr(pt.g, "gate_status", lambda v: "CONFUSABLES_MISMATCH")
    book, led = new_book()
    assert book.add("t-a", "sphynxwear", "andre", NOW) == (None, "CONFUSABLES_MISMATCH")
    t = harvest(catalog=["Fleece Hoodie"])
    assert len(t) == 0 and t.rejected["CONFUSABLES_MISMATCH"] == 2 and led.events == []


# ---- T-3: per tenant, sha256 in every run report ----
def test_tenant_a_never_visible_to_tenant_b():
    book, _ = new_book()
    book.add("t-a", "sphynxwear", "andre", NOW)
    src = FixtureTermSource(
        {"t-a": ["fleece hoodie", "fleece hoodie"]}, {"t-b": ["Dog Leash"]}
    )
    ta = pt.build_tenant_vocab("t-a", owner_book=book, source=src, harvest_enabled=True)
    src.calls.clear()
    tb = pt.build_tenant_vocab("t-b", owner_book=book, source=src, harvest_enabled=True)
    assert {"sphynxwear", "fleece", "hoodie"} <= set(ta.words)
    assert set(tb.words) == {"leash"} and tb.tenant_id == "t-b"
    assert {t for _, t in src.calls} == {"t-b"}
    assert book.words("t-b") == [] and ta.sha256 != tb.sha256


@pytest.mark.parametrize("tenant", ["", None, 7])
def test_bad_tenant_id(tenant):
    with pytest.raises(ValueError):
        pt.build_tenant_vocab(tenant)


def test_sha_recorded_in_report():
    t = harvest(catalog=["hoodie fleece"])
    expected = hashlib.sha256(b"fleece\nhoodie\n").hexdigest()
    assert t.sha256 == expected == pt.tenant_vocab_sha256({"hoodie", "fleece"})
    assert t.report_fields() == {
        "tenant_vocab_sha256": expected,
        "tenant_vocab_n": 2,
        "tenant_harvest": "ON",
    }
    assert harvest(catalog=["fleece hoodie"]).sha256 == expected


# ---- T-4: certification runs use T = empty ----
def test_certification_T_is_empty():
    e = pt.empty_tenant_vocab()
    assert (
        len(e) == 0
        and e.sha256 == pt.EMPTY_TENANT_VOCAB_SHA256 == hashlib.sha256(b"").hexdigest()
    )
    assert e.report_fields() == {
        "tenant_vocab_sha256": hashlib.sha256(b"").hexdigest(),
        "tenant_vocab_n": 0,
        "tenant_harvest": "OFF",
    }


def test_T_plugs_into_og():
    t = harvest(catalog=["Sphynxwear"])
    assert g.og("sphynxwear", frozenset(), t) == ("PASS", None)
    assert g.og("sphynxwear", frozenset(), pt.empty_tenant_vocab()) == (
        "WITHHOLD",
        "OG-6",
    )
