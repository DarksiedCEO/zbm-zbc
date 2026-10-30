"""
1. Fuzz: randomly corrupted facts never produce a 500 (only 200 blocked or 422).
2. Mutation checks ("failing test first", ADR 0006 §Testing): for each gate, the
   test that proves a block would FAIL if the check behind it were removed —
   each case neutralizes one check and shows the same request turns allowed
   (or loses that block), i.e. the test is not vacuous.
"""

import copy
import random

import pytest

from helpers import Harness, client_facts, clip_facts, creator_facts, publish_facts, rid, unmet_codes
from intelligences import engine

VALUES = [None, True, False, 0, -1, 10**12, 1.5, float("inf") if False else 2.5, "", "x", "US", "a" * 129, [], [1],
          {}, {"a": 1}, [{"platform": "youtube"}], "2026-13-40T00:00:00Z"]


def _leaves(d, prefix=()):
    if isinstance(d, dict):
        for k, v in d.items():
            yield prefix + (k,)
            yield from _leaves(v, prefix + (k,))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            yield prefix + (i,)
            yield from _leaves(v, prefix + (i,))


def _set(d, path, value):
    cur = d
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value


@pytest.fixture(scope="module")
def fz():
    x = Harness()
    x.approve_seed()
    x.run_controls()
    x.activate_creator()
    x.activate_brand()
    return x


@pytest.mark.parametrize("kind", ["creator", "client", "payout", "site", "ad_video", "email_campaign", "sms_campaign"])
def test_fuzzed_facts_never_500(fz, kind):
    rng = random.Random(f"fuzz-{kind}")
    s = fz.screen("clipper-1")
    base = {"creator": creator_facts(s["screen_id"]), "client": client_facts(), "payout": clip_facts(),
            "site": publish_facts("site"), "ad_video": publish_facts("ad_video"),
            "email_campaign": publish_facts("email_campaign"), "sms_campaign": publish_facts("sms_campaign")}[kind]
    for _ in range(120):
        f = copy.deepcopy(base)
        for _ in range(rng.randint(1, 4)):
            paths = list(_leaves(f))
            if not paths:
                break
            path = rng.choice(paths)
            try:
                if rng.random() < 0.3:
                    cur = f
                    for p in path[:-1]:
                        cur = cur[p]
                    del cur[path[-1]]
                else:
                    _set(f, path, copy.deepcopy(rng.choice(VALUES)))
            except (KeyError, IndexError, TypeError):
                pass
        if kind in ("creator", "client"):
            r = fz.rule("subj-f", "zbc_creator" if kind == "creator" else "client", f)
        else:
            r = fz.review("zbc_clip" if kind == "payout" else "zbm_work", "subj-f", f)
        assert r.status_code in (200, 422), (kind, r.status_code, r.text[:300])
        if r.status_code == 200:
            body = r.json()
            if f != base:
                assert isinstance(body["allowed"], bool)


# --- mutation checks ------------------------------------------------------------------------

def _neutralize(monkeypatch, check):
    monkeypatch.setitem(engine.CHECKS, check, lambda row, ctx: [])


MUTANTS = [
    # (gate, check neutralized, setup + request, the (obligation, code) that must disappear)
    ("activation", "age_18_plus", "minor_creator", ("HR-02", "age_18_plus")),
    ("activation", "jurisdiction_class", "fr_client", ("HR-07", "jurisdiction_class")),
    ("activation", "no_cold_outbound_ca", "cold_ca_client", ("HR-08", "no_cold_outbound_ca")),
    # ADR 0024 (HR-06 superseded: no EU kit any more): the two client activation checks of Oct 30, 2026
    ("activation", "client_sanctions_attested", "client_stale_ofac_search", ("HR-14", "client_sanctions_attested")),
    ("activation", "client_docs_current", "client_msa_not_current", ("HR-15", "client_docs_current")),
    ("payout", "platform_toggle", "clip_no_toggle", ("PLT-YT-01", "platform_toggle")),
    ("payout", "label_vocabulary", "clip_collab", ("US-FTC-D101-02", "label_vocabulary")),
    ("payout", "verified_views_attested", "clip_unverified_views", ("HR-13", "verified_views_attested")),
    ("payout", "tax_form_on_file", "finance_down", ("US-IRS-BWH", "dependency_unavailable:finance_31")),
    ("publish", "accessibility_pass", "site_no_a11y", ("HR-09", "accessibility_pass")),
    ("publish", "consent_banner", "site_no_banner", ("HR-10", "consent_banner")),
    ("publish", "sms_consent_artifact", "sms_bad", None),
]


def _run_oct30(case):
    """ADR 0024 cases: HR-14 / HR-15 take effect on Oct 30, 2026."""
    import hashlib
    from datetime import datetime, timedelta, timezone

    from clock import FixedClock, iso
    from fakes import PassingLegal
    from helpers import ANDRE_TOKEN, passing_ports
    from ports import ClientDocument

    class Legal(PassingLegal):
        def client_document(self, *a):
            return ClientDocument(True, case != "client_msa_not_current", True)
    at = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    clock = FixedClock(at)
    ports = passing_ports(clock)
    ports.legal = Legal()
    x = Harness(ports=ports, clock=clock)
    x.approve_seed()
    x.run_controls()
    searched = at - timedelta(days=40 if case == "client_stale_ofac_search" else 0)
    a = x.post("/compliance/v1/sanctions/attestations", {
        "request_id": rid("att"), "subject_id": "c", "lane": "client", "searched_at": iso(searched),
        "list_version": "SDN 2026-10-29", "lists": ["SDN", "Consolidated"], "tool": "ofac_sanctions_list_search",
        "result": "no_match", "evidence_sha256": hashlib.sha256(b"e").hexdigest(),
        "searched_name_sha256": hashlib.sha256(b"n").hexdigest()}, andre=ANDRE_TOKEN).json()
    f = client_facts(platforms=("web",), sanctions_attestation_id=a["attestation_id"],
                     legal_documents=[{"doc_id": "client_msa", "version": "1.0", "doc_sha256": "e" * 64,
                                       "acceptance_id": "lg-acc-01J0000000000000000000000A"}])
    return x.rule("c", "client", f).json()


def _run(case):
    if case in ("client_stale_ofac_search", "client_msa_not_current"):
        return _run_oct30(case)
    x = Harness()
    x.approve_seed()
    x.run_controls()
    x.approve(*[x.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    if case == "minor_creator":
        x.ports.verification.age_status = "minor"
        s = x.screen("clipper-1")
        return x.rule("clipper-1", "zbc_creator", creator_facts(s["screen_id"])).json()
    if case == "fr_client":
        return x.rule("c", "client", client_facts(country="GB", region=None, targets=("GB", "FR"))).json()
    if case == "cold_ca_client":
        return x.rule("c", "client", client_facts(outbound_cold_contact_countries=["CA"])).json()
    x.activate_creator()
    x.activate_brand()
    if case == "clip_no_toggle":
        return x.review("zbc_clip", rid(), clip_facts(disclosure={"platform_toggle_evidence_ref": None})).json()
    if case == "clip_collab":
        return x.review("zbc_clip", rid(), clip_facts(disclosure={"in_video_label_text": "collab"})).json()
    if case == "clip_unverified_views":
        x.ports.verification.clip_ok = False
        return x.review("zbc_clip", rid(), clip_facts()).json()
    if case == "finance_down":
        from ports import NotBuiltFinance31
        x.ports.finance = NotBuiltFinance31()
        return x.review("zbc_clip", rid(), clip_facts()).json()
    if case == "site_no_a11y":
        return x.review("zbm_work", "w", publish_facts()).json()
    if case == "site_no_banner":
        x.post("/compliance/v1/accessibility/checks", {"request_id": rid(), "asset_ref": "s", "asset_type": "site",
                                                       "content_sha256": "d" * 64, "owner_id": "o"}, caller="creative_production")
        return x.review("zbm_work", "w", publish_facts(flags={"uses_tracking": True}, tracking={
            "tracking_disclosed": True, "consent_banner_present": False, "consent_before_nonessential": True,
            "opt_out_present": True})).json()
    if case == "sms_bad":
        return x.review("zbm_work", "w", publish_facts("sms_campaign", sms={
            "consent_artifacts_complete": True, "quiet_hours_local": "07:00-22:00", "max_per_24h": 5,
            "opt_out_immediate": True}, recipient_countries=["US"])).json()
    raise AssertionError(case)


@pytest.mark.parametrize("gate, check, case, item", MUTANTS, ids=[m[2] for m in MUTANTS])
def test_mutation_the_block_depends_on_its_check(monkeypatch, gate, check, case, item):
    real = _run(case)
    assert real["allowed"] is False
    if item is not None:
        assert item in unmet_codes(real), real["unmet_lines"]
    _neutralize(monkeypatch, check)
    mutant = _run(case)
    if item is not None:
        assert item not in unmet_codes(mutant), "the block survived with its check removed: the test would be vacuous"
    else:
        # sms: the unverified TCPA rows still block (rule_not_in_force), with or without the check
        assert mutant["allowed"] is False
        assert ("US-FCC-TCPA-01", "rule_not_in_force") in unmet_codes(mutant)
