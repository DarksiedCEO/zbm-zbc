"""Change Watcher (spec C.4, H.14): regression fixtures, cosmetic changes, feed safety."""

from datetime import datetime, timezone

import pytest

from helpers import ANDRE_TOKEN, rid
from intelligences import i06_change_watcher as w

FR_FTC_RULE = ("https://www.federalregister.gov/api/v1/documents.rss?conditions[agencies][]=federal-trade-commission"
               "&conditions[type][]=RULE")
CPPA = "https://cppa.ca.gov/regulations/ccpa_updates.html"
TIKTOK_CG = "https://www.tiktok.com/community-guidelines/en/integrity-authenticity"


def enable_watcher(h):
    h.svc.config.watcher_enabled = True


def run(h):
    r = h.post("/compliance/v1/watcher/run", {"request_id": rid("w")}, caller="scheduler")
    assert r.status_code == 200, r.text
    return r.json()


def rss(*items: str) -> bytes:
    return ("<?xml version='1.0'?><rss version='2.0'><channel><title>Federal Register</title>"
            + "".join(items) + "</channel></rss>").encode()


OLD_ITEM = ("<item><title>Rules of Practice</title><link>https://www.federalregister.gov/documents/2024/07/01/2024-11111/"
            "rules-of-practice</link><description>Procedural amendments.</description>"
            "<pubDate>Mon, 01 Jul 2024 00:00:00 -0400</pubDate><guid>2024-11111</guid></item>")
CRR_ITEM = ("<item><title>Trade Regulation Rule on the Use of Consumer Reviews and Testimonials</title>"
            "<link>https://www.federalregister.gov/documents/2024/08/22/2024-18519/trade-regulation-rule-on-the-use-of-"
            "consumer-reviews-and-testimonials</link><description>The Federal Trade Commission issues a final rule "
            "prohibiting fake reviews and testimonials. DATES: This rule is effective October 21, 2024.</description>"
            "<pubDate>Thu, 22 Aug 2024 00:00:00 -0400</pubDate><guid>2024-18519</guid></item>")


def _latest_for(h, target):
    return [p for p in h.inbox() if p["target_id"] == target]


def test_h14_consumer_review_rule_fixture_proposes_against_the_part_465_rows(hs):
    enable_watcher(hs)
    hs.clock.at = datetime(2024, 8, 21, 12, 0, tzinfo=timezone.utc)
    hs.ports.fetcher.pages = {FR_FTC_RULE: rss(OLD_ITEM)}
    first = run(hs)
    assert first["proposals"] == []  # baseline
    hs.clock.at = datetime(2024, 8, 22, 15, 0, tzinfo=timezone.utc)
    hs.ports.fetcher.pages[FR_FTC_RULE] = rss(CRR_ITEM, OLD_ITEM)
    out = run(hs)
    targets = {hs.svc.proposals[p]["target_id"] for p in out["proposals"]}
    assert targets == {"US-FTC-465-02", "US-FTC-465-04", "US-FTC-465-07", "US-FTC-465-08"}
    p = hs.svc.proposals[out["proposals"][0]]
    assert p["kind"] == "amend" and p["proposed_row"]["status"] == "unverified"
    assert p["evidence"]["doc_number"] == "2024-18519"
    assert p["watch"]["feed_effective_date"] == "2024-10-21"
    assert p["watch"]["detection_latency_hours"] == 15.0
    assert p["evidence"]["source_url"].startswith("https://www.federalregister.gov/documents/2024/08/22/2024-18519/")
    assert "Consumer Reviews" in p["evidence"]["quoted_excerpt"]
    assert hs.ledger.of_type("watcher_change_detected")
    # the register did not change: only Andre's decision can
    assert hs.svc.version_number == 1


def test_h14_cppa_package_page_change_proposes_against_the_cppa_row(hs):
    enable_watcher(hs)
    hs.ports.fetcher.pages = {CPPA: b"<html><body><h1>CCPA Updates</h1><p>Proposed regulations under review.</p></body></html>"}
    assert run(hs)["proposals"] == []
    hs.clock.advance(hours=3)
    hs.ports.fetcher.pages[CPPA] = (b"<html><body><h1>CCPA Updates</h1><p>The regulations on automated decisionmaking "
                                    b"technology, risk assessments and cybersecurity audits take effect January 1, 2026."
                                    b"</p></body></html>")
    out = run(hs)
    targets = {hs.svc.proposals[p]["target_id"] for p in out["proposals"]}
    assert "US-CPPA-2025" in targets
    p = _latest_for(hs, "US-CPPA-2025")[0]
    assert p["evidence"]["source_url"] == CPPA
    assert "January 1, 2026" in p["evidence"]["quoted_excerpt"]
    assert p["watch"]["effective_date_flag"]  # page gives no machine-readable effective date: flagged for Andre


def test_h14_tiktok_guidelines_change_proposes_against_plt_tt_03(hs):
    enable_watcher(hs)
    hs.clock.at = datetime(2025, 8, 13, 9, 0, tzinfo=timezone.utc)
    hs.ports.fetcher.pages = {TIKTOK_CG: b"<main><h1>Integrity and Authenticity</h1><p>Old guidelines.</p></main>"}
    run(hs)
    hs.clock.at = datetime(2025, 8, 14, 18, 0, tzinfo=timezone.utc)
    hs.ports.fetcher.pages[TIKTOK_CG] = (b"<main><h1>Integrity and Authenticity</h1><p>Updated Community Guidelines, "
                                         b"announced August 14, 2025, effective September 13, 2025.</p></main>")
    out = run(hs)
    targets = {hs.svc.proposals[p]["target_id"] for p in out["proposals"]}
    assert targets == {"PLT-TT-03"}


def test_h14_cosmetic_only_change_makes_no_proposal(hs):
    enable_watcher(hs)
    hs.ports.fetcher.pages = {TIKTOK_CG: b"<main><p>Same   words.</p></main>"}
    run(hs)
    hs.clock.advance(hours=1)
    hs.ports.fetcher.pages[TIKTOK_CG] = b"<main>\n<div class='x'><p>Same words.</p></div><script>track()</script></main>"
    out = run(hs)
    assert out["proposals"] == [] and out["cosmetic_discarded"] == 1


def test_unmatched_feed_items_are_dropped_and_counted(hs):
    enable_watcher(hs)
    hs.ports.fetcher.pages = {FR_FTC_RULE: rss(OLD_ITEM)}
    run(hs)
    hs.clock.advance(hours=1)
    other = OLD_ITEM.replace("2024-11111", "2024-22222").replace("Rules of Practice", "Hart-Scott-Rodino thresholds")
    hs.ports.fetcher.pages[FR_FTC_RULE] = rss(other, OLD_ITEM)
    out = run(hs)
    assert out["proposals"] == [] and out["dropped_unmatched"] == 1


def test_watcher_disabled_by_default_and_c02_red(hs):
    r = hs.post("/compliance/v1/watcher/run", {"request_id": rid()}, caller="scheduler").json()
    assert r["ran"] is False
    assert hs.get("/compliance/v1/controls/C-02").json()["status"] == "red"


def test_failed_sources_are_recorded_and_keep_c02_red(hs):
    enable_watcher(hs)
    out = run(hs)
    assert out["failed"] and hs.ledger.of_type("watcher_source_failed")
    hs.run_controls()
    assert hs.get("/compliance/v1/controls/C-02").json()["status"] == "red"


def test_page_sources_are_fetched_at_most_twice_a_day(hs):
    enable_watcher(hs)
    hs.ports.fetcher.pages = {CPPA: b"<p>a</p>"}
    for _ in range(3):
        run(hs)
    assert hs.ports.fetcher.requested.count(CPPA) == 2


@pytest.mark.parametrize("doc", [
    b"<?xml version='1.0'?><!DOCTYPE lolz [<!ENTITY lol 'lol'>]><rss><channel><item><title>&lol;</title></item></channel></rss>",
    b"<rss><channel><item><title>unclosed",
])
def test_hostile_or_broken_feeds_are_refused(doc):
    with pytest.raises(w.FeedParseError):
        w.parse_feed(doc)


def test_atom_feeds_parse():
    atom = (b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>urn:1</id><title>Consumer review rule</title>"
            b"<link href='https://www.legislation.gov.uk/uksi/2025/1/contents'/><summary>testimonials</summary>"
            b"<updated>2025-04-06T00:00:00Z</updated></entry></feed>")
    items = w.parse_feed(atom)
    assert items[0].title == "Consumer review rule" and items[0].published == "2025-04-06"
    assert items[0].link == "https://www.legislation.gov.uk/uksi/2025/1/contents"


def test_approving_a_watcher_proposal_marks_the_row_unverified_and_blocks(hs):
    enable_watcher(hs)
    hs.ports.fetcher.pages = {TIKTOK_CG: b"<p>v1</p>"}
    run(hs)
    hs.clock.advance(hours=1)
    hs.ports.fetcher.pages[TIKTOK_CG] = b"<p>v2 changed</p>"
    run(hs)
    p = _latest_for(hs, "PLT-TT-03")[0]
    hs.approve(p)
    assert hs.svc.current.by_id()["PLT-TT-03"]["status"] == "unverified"
    assert hs.svc.version_number == 2
    assert ANDRE_TOKEN
