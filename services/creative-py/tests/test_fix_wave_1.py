"""
Fix wave 1 (Sep 24 2026) — one reproduction per finding, written to FAIL on
the pre-fix code and pass after the fix:

F8   2-round review cap is per deliverable variant of a BRIEF, survives new
     jobs, and an open escalation freezes the brief until Andre resolves it.
F9   record first, then any outside call (every stand-in port) and any
     state change — one test per call site with the ledger down.
F11  deterministic event ids: a timed-out-but-committed record plus an
     identical retry is ONE ledger record and the decision takes effect once.
F12  never-say / must-say / disclosure matching survives confusables,
     separator-split letters and zero-width characters; obfuscated text is
     never an automatic pass.
F13  the server records receipt time; an old version is judged
     automatically only inside the grace window after it was superseded.
F14  no Decimal path can raise InvalidOperation as a 500.
F16  summary sanitising and the fake ledger match ledger-rust exactly (C1).
D2   ids whose derived ledger fields can't fit are a 422, never a 503.
D3   devtools/live_smoke.py handles every ledger entry kind.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from conftest import TEST_FOUNDER_TOKEN
from fakes import AcceptingCreativeAgents, PassingCompliance, PassingLegal, PassingVerification
from flows import C, ok, zbc_live, zbc_open, zbc_rights_on_file, zbm_approved_brief, zbm_work_at_quality
from samples import CAMPAIGN, ZBC_ASSETS, zbc_clip, zbc_goal, zbc_kit_request, zbc_license, zbc_music_clearance, \
    zbc_source, zbm_clearance, zbm_work

Q = {"actor_id": "zbm_creative_quality", "notes": ["Not premium."]}


# =====================================================================================
# helpers
# =====================================================================================

class Spy:
    """Wraps a port; records every call (and how many ledger events existed at that moment)."""

    def __init__(self, inner, ledger):
        self._inner, self._ledger, self.calls = inner, ledger, []

    def __getattr__(self, name):
        fn = getattr(self._inner, name)

        def call(*a, **k):
            self.calls.append((name, len(self._ledger.events), [e["event_type"] for e in self._ledger.events]))
            return fn(*a, **k)

        return call


def spied(ledger, **passing):
    """Departments where every port is a Spy (around the given fake or the fail-closed stand-in)."""
    from shared.departments import Departments

    base = Departments(**passing)
    spies = {f.name: Spy(getattr(base, f.name), ledger) for f in dataclasses.fields(Departments)}
    return Departments(**spies), spies


def all_calls(spies):
    return {k: s.calls for k, s in spies.items() if s.calls}


def ledger_snapshot(api):
    return [e["event_id"] for e in api.ledger.events]


def zbm_state(api):
    z = api.zbm
    return (repr(sorted(z.briefs.items())), repr(sorted(z.jobs.items())), repr(sorted(z.work.items())))


def zbc_state(api):
    z = api.zbc
    return (repr([rb.model_dump() for cid in {k[0] for k in z.rulebooks._by_key} for rb in z.rulebooks.versions(cid)]),
            repr(sorted(z.rights_checks.items())), repr(sorted(z.kits.items())), repr(sorted(z.decisions.items())),
            repr(z.memory.library))


# =====================================================================================
# F8 — review cap per brief deliverable, survives new jobs, escalation freezes the brief
# =====================================================================================

def _to_quality(api, job_id):
    w = ok(api.post(f"/zbm/jobs/{job_id}/work", zbm_work()), 201)
    ok(api.post(f"/zbm/work/{w['work_id']}/export-validation"))
    ok(api.post(f"/zbm/work/{w['work_id']}/rights"))
    return w


def test_f8_new_job_on_same_brief_does_not_reset_the_round_count(api):
    brief, job, w = zbm_work_at_quality(api)
    assert ok(api.post(f"/zbm/work/{w['work_id']}/quality", Q))["stage"] == "sent_back"
    job2 = ok(api.post(f"/zbm/briefs/{brief['brief_id']}/jobs"), 201)
    w2 = ok(api.post(f"/zbm/jobs/{job2['job_id']}/work", zbm_work()), 201)
    assert w2["round"] == 2, "round count must follow the brief's deliverable, not the job"


def test_f8_open_escalation_blocks_new_jobs_and_rounds_on_the_brief_until_andre_resolves(api):
    brief, job, w = zbm_work_at_quality(api)
    ok(api.post(f"/zbm/work/{w['work_id']}/quality", Q))
    w2 = _to_quality(api, job["job_id"])
    assert ok(api.post(f"/zbm/work/{w2['work_id']}/quality", Q))["stage"] == "escalated_to_andre"
    jobs_before = dict(api.zbm.jobs)

    # the probe's bypass: a fresh job on the same approved brief
    r = api.post(f"/zbm/briefs/{brief['brief_id']}/jobs")
    assert r.status_code == 409 and "escalat" in r.json()["detail"].lower()
    assert api.zbm.jobs == jobs_before
    # no new round in the existing job either
    assert api.post(f"/zbm/jobs/{job['job_id']}/work", zbm_work()).status_code == 409
    # the service token / a wrong token cannot resolve it
    assert api.post(f"/zbm/work/{w2['work_id']}/escalation", {"decision": "kill"}).status_code == 403
    assert api.post(f"/zbm/work/{w2['work_id']}/escalation", {"decision": "kill"}, andre="nope").status_code == 403
    assert api.post(f"/zbm/briefs/{brief['brief_id']}/jobs").status_code == 409

    ok(api.post(f"/zbm/work/{w2['work_id']}/escalation", {"decision": "kill"}, andre=TEST_FOUNDER_TOKEN))
    # resolved: a new job may open, but the capped deliverable gets no third round in ANY job
    job3 = ok(api.post(f"/zbm/briefs/{brief['brief_id']}/jobs"), 201)
    r3 = api.post(f"/zbm/jobs/{job3['job_id']}/work", zbm_work())
    assert r3.status_code == 409 and "2 review rounds" in r3.json()["detail"]


def test_f8_only_one_version_of_a_deliverable_in_flight_across_jobs(api):
    brief, job, w = zbm_work_at_quality(api)          # w is at rights_cleared in job 1
    job2 = ok(api.post(f"/zbm/briefs/{brief['brief_id']}/jobs"), 201)
    r = api.post(f"/zbm/jobs/{job2['job_id']}/work", zbm_work())
    assert r.status_code == 409


# =====================================================================================
# F9 — record first: ledger down => zero outside calls, zero state change (per call site)
# =====================================================================================

@pytest.fixture
def spy_api(make_api, ledger):
    def _make(**passing):
        deps, spies = spied(ledger, **passing)
        api = make_api(departments=deps)
        return api, spies
    return _make


def _down(api):
    api.ledger.fail_all = True


def _assert_refused(r):
    assert r.status_code == 503, (r.status_code, r.text)
    assert r.json()["took_effect"] is False


def test_f9_open_job_ledger_down_no_commission_no_job(spy_api):
    api, spies = spy_api(creative_agents=AcceptingCreativeAgents())
    brief = zbm_approved_brief(api)
    before, led = zbm_state(api), ledger_snapshot(api)
    _down(api)
    _assert_refused(api.post(f"/zbm/briefs/{brief['brief_id']}/jobs"))
    assert all_calls(spies) == {}
    assert zbm_state(api) == before and ledger_snapshot(api) == led
    # recovery: the id the failed attempt would have used was not consumed
    api.ledger.fail_all = False
    job = ok(api.post(f"/zbm/briefs/{brief['brief_id']}/jobs"), 201)
    assert job["job_id"] == "job-0002"
    # every commission left only after production_opened was on the ledger
    for _, _, types in spies["creative_agents"].calls:
        assert "production_opened" in types


def test_f9_open_job_probe_c2_outcome_record_failure_is_reported_honestly(make_api):
    from shared.departments import Departments
    from shared.ledger import FakeLedgerClient, LedgerRecordError

    class FailType(FakeLedgerClient):
        target: str = ""

        def record_event(self, **kw):
            if kw["event_type"] == self.target:
                raise LedgerRecordError("down")
            return super().record_event(**kw)

    led = FailType()
    deps, spies = spied(led, creative_agents=AcceptingCreativeAgents())
    api = make_api(ledger=led, departments=deps)
    brief = zbm_approved_brief(api)
    led.target = "production_opened"      # the probe's C2 failure point
    _assert_refused(api.post(f"/zbm/briefs/{brief['brief_id']}/jobs"))
    assert spies["creative_agents"].calls == [] and not api.zbm.jobs
    # the only failure AFTER an outside call is the receipt record; it must not claim "did not take effect"
    led.target = "crossing_creative_agents"
    r = api.post(f"/zbm/briefs/{brief['brief_id']}/jobs")
    assert r.status_code == 503 and r.json()["took_effect"] == "partial"
    job = next(iter(api.zbm.jobs.values()))
    assert all(c["status"] == "requested" and c["commissioned"] is None for c in job.commissions)


def test_f9_zbm_rights_check_ledger_down_no_legal_no_stamp_no_stage_change(spy_api, monkeypatch):
    import shared.media as media

    stamps = []
    monkeypatch.setattr(media.C2paStandIn, "stamp", lambda self, ref, m: stamps.append(ref) or media.NotIntegrated("c2pa-rs"))
    api, spies = spy_api(legal=PassingLegal())
    ok(api.post("/rights/clearances", {"actor_id": "rights_desk", "record": zbm_clearance()}), 201)
    brief = zbm_approved_brief(api)
    job = ok(api.post(f"/zbm/briefs/{brief['brief_id']}/jobs"), 201)
    w = ok(api.post(f"/zbm/jobs/{job['job_id']}/work", zbm_work(uses_ai_generative_fill=True)), 201)
    ok(api.post(f"/zbm/work/{w['work_id']}/export-validation"))
    calls_before, before, led = dict(all_calls(spies)), zbm_state(api), ledger_snapshot(api)
    _down(api)
    _assert_refused(api.post(f"/zbm/work/{w['work_id']}/rights"))
    assert all_calls(spies) == calls_before and stamps == []
    assert zbm_state(api) == before and ledger_snapshot(api) == led


def test_f9_zbm_compliance_gate_ledger_down_no_compliance_call(spy_api):
    api, spies = spy_api(compliance=PassingCompliance())
    _, _, w = zbm_work_at_quality(api)
    ok(api.post(f"/zbm/work/{w['work_id']}/quality", {"actor_id": "zbm_creative_quality", "notes": []}))
    before, led = zbm_state(api), ledger_snapshot(api)
    _down(api)
    _assert_refused(api.post(f"/zbm/work/{w['work_id']}/compliance"))
    assert spies["compliance"].calls == []
    assert zbm_state(api) == before and ledger_snapshot(api) == led


def test_f9_zbc_rights_check_ledger_down_no_legal_call(spy_api):
    api, spies = spy_api(legal=PassingLegal())
    zbc_rights_on_file(api)
    before, led = zbc_state(api), ledger_snapshot(api)
    _down(api)
    _assert_refused(api.post(f"{C}/rights-check", {"assets": ZBC_ASSETS, "uses_ai_generative_fill": True}))
    assert spies["legal"].calls == []
    assert zbc_state(api) == before and ledger_snapshot(api) == led


def _signed_with_rights(api, gen_fill=False):
    zbc_rights_on_file(api)
    ok(api.post(f"{C}/rulebooks", {"goal": zbc_goal()}), 201)
    ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.post(f"{C}/rulebooks/1/sign", andre=TEST_FOUNDER_TOKEN))
    ok(api.post(f"{C}/rights-check", {"assets": ZBC_ASSETS, "uses_ai_generative_fill": gen_fill}))


def test_f9_go_live_ledger_down_no_announcement_no_legal_no_state(spy_api):
    api, spies = spy_api(legal=PassingLegal())
    ok(api.post("/rights/licenses", {"actor_id": "rights_desk",
                                     "license": zbc_license(ai_generative_fill_permitted=True)}), 201)
    ok(api.post("/rights/clearances", {"actor_id": "rights_desk", "record": zbc_music_clearance()}), 201)
    ok(api.post(f"{C}/rulebooks", {"goal": zbc_goal()}), 201)
    ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.post(f"{C}/rulebooks/1/sign", andre=TEST_FOUNDER_TOKEN))
    ok(api.post(f"{C}/rights-check", {"assets": ZBC_ASSETS, "uses_ai_generative_fill": True}))
    legal_before = len(spies["legal"].calls)
    before, led = zbc_state(api), ledger_snapshot(api)
    _down(api)
    _assert_refused(api.post(f"{C}/rulebooks/1/go-live"))
    assert spies["clipper_network"].calls == [] and len(spies["legal"].calls) == legal_before
    assert zbc_state(api) == before and ledger_snapshot(api) == led
    assert api.zbc.rulebooks.get(CAMPAIGN, 1).status.value == "signed"


def test_f9_go_live_probe_c3_live_record_failure_means_no_announcement(make_api):
    from shared.ledger import FakeLedgerClient, LedgerRecordError

    class FailType(FakeLedgerClient):
        target: str = ""

        def record_event(self, **kw):
            if kw["event_type"] == self.target:
                raise LedgerRecordError("down")
            return super().record_event(**kw)

    led = FailType()
    deps, spies = spied(led)
    api = make_api(ledger=led, departments=deps)
    _signed_with_rights(api)
    led.target = "rulebook_live"
    _assert_refused(api.post(f"{C}/rulebooks/1/go-live"))
    assert spies["clipper_network"].calls == [], "clippers must never be told about a version that isn't live"
    led.target = ""
    ok(api.post(f"{C}/rulebooks/1/go-live"))
    (_, _, types), = spies["clipper_network"].calls
    assert "rulebook_live" in types


def test_f9_build_kit_ledger_down_no_commission_no_kit(spy_api):
    api, spies = spy_api(creative_agents=AcceptingCreativeAgents())
    zbc_live(api)
    ok(api.post(f"{C}/moment-map", zbc_source()))
    ok(api.post(f"{C}/hook-sheets"))
    before, led = zbc_state(api), ledger_snapshot(api)
    _down(api)
    _assert_refused(api.post(f"{C}/kit", zbc_kit_request()))
    assert spies["creative_agents"].calls == []
    assert zbc_state(api) == before and ledger_snapshot(api) == led
    api.ledger.fail_all = False
    kit = ok(api.post(f"{C}/kit", zbc_kit_request()), 201)
    assert kit["kit_id"] == "kit-0001" and kit["seed_clips_produced"] is True
    for _, _, types in spies["creative_agents"].calls:
        assert "campaign_kit_built" in types


def test_f9_payout_eligibility_ledger_down_no_verification_no_compliance(spy_api):
    api, spies = spy_api(verification=PassingVerification(), compliance=PassingCompliance())
    zbc_open(api)
    ok(api.post("/zbc/clips", zbc_clip()), 201)
    before, led = zbc_state(api), ledger_snapshot(api)
    _down(api)
    _assert_refused(api.post("/zbc/clips/clip_001/payout-eligibility"))
    assert spies["verification"].calls == [] and spies["compliance"].calls == []
    assert zbc_state(api) == before and ledger_snapshot(api) == led


def test_f9_zbc_memory_ledger_down_no_attestation_request(spy_api):
    api, spies = spy_api(verification=PassingVerification())
    zbc_open(api)
    ok(api.post("/zbc/clips", zbc_clip()), 201)
    res = {"result_id": "res_1", "campaign_id": CAMPAIGN, "submission_id": "clip_001", "vertical": "podcasts",
           "platform": "youtube", "angle_id": "A01", "hook": "This budget myth costs you",
           "source": "platform_export", "reported_views": 10}
    before, led = zbc_state(api), ledger_snapshot(api)
    _down(api)
    _assert_refused(api.post("/zbc/memory/results", res))
    assert spies["verification"].calls == []
    assert zbc_state(api) == before and ledger_snapshot(api) == led


def test_f9_finance_is_never_called_on_any_path(spy_api):
    api, spies = spy_api(compliance=PassingCompliance(), verification=PassingVerification(),
                         creative_agents=AcceptingCreativeAgents(), legal=PassingLegal())
    zbc_open(api)
    ok(api.post("/zbc/clips", zbc_clip()), 201)
    ok(api.post("/zbc/clips/clip_001/payout-eligibility"))
    _, _, w = zbm_work_at_quality(api)
    ok(api.post(f"/zbm/work/{w['work_id']}/quality", {"actor_id": "zbm_creative_quality", "notes": []}))
    ok(api.post(f"/zbm/work/{w['work_id']}/compliance"))
    ok(api.post(f"/zbm/work/{w['work_id']}/final-approval", andre=TEST_FOUNDER_TOKEN))
    assert spies["finance"].calls == []


def test_f9_every_outside_request_is_on_the_ledger_before_it_is_made(spy_api):
    api, spies = spy_api(compliance=PassingCompliance(), verification=PassingVerification(),
                         creative_agents=AcceptingCreativeAgents(), legal=PassingLegal())
    zbc_open(api)
    ok(api.post("/zbc/clips", zbc_clip()), 201)
    ok(api.post("/zbc/clips/clip_001/payout-eligibility"))
    seen = 0
    for name, spy in spies.items():
        for method, n_events, types in spy.calls:
            seen += 1
            assert n_events > 0 and types[-1].endswith("_requested") or types[-1] in (
                "production_opened", "campaign_kit_built", "rulebook_live"), (name, method, types[-3:])
    assert seen >= 4


# =====================================================================================
# F11 — deterministic event ids: timed-out-but-committed + retry = one record, one effect
# =====================================================================================

@pytest.fixture
def flaky_ledger():
    from shared.ledger import FakeLedgerClient, LedgerRecordError

    @dataclasses.dataclass
    class CommitThenTimeout(FakeLedgerClient):
        timeout_on: set = dataclasses.field(default_factory=set)

        def record_event(self, **kw):
            super().record_event(**kw)
            if kw["event_type"] in self.timeout_on:
                self.timeout_on.discard(kw["event_type"])
                raise LedgerRecordError("ledger unreachable: ReadTimeout (simulated, after the ledger committed)")

    return CommitThenTimeout()


def test_f11_retry_after_committed_timeout_is_one_record_one_effect(make_api, flaky_ledger):
    api = make_api(ledger=flaky_ledger)
    b = ok(api.post("/zbm/briefs", {"requirements": __import__("samples").zbm_requirements()}), 201)
    flaky_ledger.timeout_on.add("brief_approved")
    r = api.post(f"/zbm/briefs/{b['brief_id']}/review", {"actor_id": "zbm_creative_lead"})
    assert r.status_code == 503 and api.zbm.get_brief(b["brief_id"]).status.value == "draft"
    again = ok(api.post(f"/zbm/briefs/{b['brief_id']}/review", {"actor_id": "zbm_creative_lead"}))
    assert again["status"] == "approved"
    approvals = [e for e in flaky_ledger.of_type("brief_approved") if e["subject_id"] == b["brief_id"]]
    assert len(approvals) == 1
    assert again["ledger_event_ids"][-1] == approvals[0]["event_id"]


def test_f11_clip_retry_reuses_the_first_receipt_time_and_records_once(make_api, flaky_ledger, clock):
    api = make_api(ledger=flaky_ledger)
    zbc_open(api)
    first_receipt = clock.now()
    flaky_ledger.timeout_on.add("clip_reviewed")
    assert api.post("/zbc/clips", zbc_clip()).status_code == 503
    assert "clip_001" not in api.zbc.decisions
    clock.at = clock.at + timedelta(minutes=7)
    d = ok(api.post("/zbc/clips", zbc_clip()), 201)
    assert len(flaky_ledger.of_type("clip_reviewed")) == 1
    assert d["received_at"] == first_receipt.isoformat().replace("+00:00", "Z") or \
        d["received_at"].startswith(first_receipt.isoformat()[:19])


def test_f11_retry_with_different_content_under_same_submission_is_a_conflict(make_api, flaky_ledger):
    api = make_api(ledger=flaky_ledger)
    zbc_open(api)
    flaky_ledger.timeout_on.add("clip_reviewed")
    assert api.post("/zbc/clips", zbc_clip()).status_code == 503
    r = api.post("/zbc/clips", zbc_clip(caption="something else entirely #ad"))
    # fix wave 2 (N1): refused by the service itself (409) — never reaches the ledger as a new decision
    assert r.status_code == 409
    assert "clip_001" not in api.zbc.decisions and len(flaky_ledger.of_type("clip_reviewed")) == 1


def test_f11_distinct_identical_decisions_are_still_distinct_records(api):
    zbc_rights_on_file(api)
    ok(api.post(f"{C}/rulebooks", {"goal": zbc_goal()}), 201)
    ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.put(f"{C}/rulebooks/1", {"actor_id": "zbc_rulebook_writer", "goal": zbc_goal()}))
    ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    assert len(api.ledger.of_type("rulebook_approved")) == 2


def test_f11_event_id_is_a_pure_function_of_the_operation():
    from shared.ledger import EvidenceRecorder, FakeLedgerClient

    a, b = FakeLedgerClient(), FakeLedgerClient()
    ra, rb = EvidenceRecorder(a, instance_id="i1"), EvidenceRecorder(b, instance_id="i1")
    ida = ra.record("brief_approved", "zbm_creative_lead", "brief-0001", {"x": 1, "y": [2]}, "s")
    idb = rb.record("brief_approved", "zbm_creative_lead", "brief-0001", {"y": [2], "x": 1}, "s")
    assert ida == idb and len(ida) <= 128
    assert ra.record("brief_approved", "zbm_creative_lead", "brief-0001", {"x": 1, "y": [2]}, "s") != ida  # next op


# =====================================================================================
# F12 — never-say evasion; obfuscated text never auto-passes
# =====================================================================================

CYR_E = "е"  # Cyrillic small ie, looks like Latin e


@pytest.mark.parametrize("text", [
    f"guarant{CYR_E}ed returns",                   # Cyrillic confusable
    "g u a r a n t e e d returns",                 # spaced letters
    "g.u.a.r.a.n.t.e.e.d r.e.t.u.r.n.s",           # dotted letters
    "guaran​teed returns",                    # zero-width space inside the word
    "guaran­teed returns",                    # soft hyphen
    "guаrаnteed returns",                # Cyrillic a
    "ɡuaranteed returns",                     # Latin small script g
    "ＧＵＡＲＡＮＴＥＥＤ returns",                  # fullwidth
    "guaránteed returns",                          # diacritic
])
def test_f12_never_say_matcher_sees_through_evasions(text):
    from shared.text import PhraseMatch, match_phrase

    assert match_phrase(text, "guaranteed returns") is not PhraseMatch.NONE


def test_f12_contains_phrase_function_level_repro():
    from shared.text import contains_phrase

    assert contains_phrase(f"guarant{CYR_E}ed income", "guaranteed income") is True


@pytest.mark.parametrize("field", ["caption", "on_screen_text", "transcript"])
def test_f12_confusable_never_say_in_clip_is_never_an_automatic_pass(api, field):
    zbc_open(api)
    base = zbc_clip()
    d = ok(api.post("/zbc/clips", zbc_clip(**{field: base[field] + f" guarant{CYR_E}ed returns"})), 201)
    assert d["outcome"] == "reject" and "NS-01" in [b["rule_id"] for b in d["broken_rules"]]


def test_f12_spaced_never_say_in_clip_is_not_a_pass(api):
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip(transcript="This budget myth costs you. Listen on Pod Plus. "
                                                     "g u a r a n t e e d  r e t u r n s")), 201)
    assert d["outcome"] != "pass"


def test_f12_mixed_script_caption_goes_to_human_review_even_without_a_hit(api):
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip(caption=f"The budg{CYR_E}t myth nobody talks about #ad")), 201)
    assert d["outcome"] == "human_review"
    assert any("obfuscat" in r or "mixed-script" in r for r in d["human_review_reasons"])


def test_f12_must_say_and_disclosure_via_confusables_are_not_automatic_passes(api):
    zbc_open(api)
    ms = ok(api.post("/zbc/clips", zbc_clip("c_ms", transcript="This budget myth costs you. Listen on Pоd Plus.")), 201)
    assert ms["outcome"] != "pass"
    dc = ok(api.post("/zbc/clips", zbc_clip("c_dc", caption="The budget myth nobody talks about #аd")), 201)
    assert dc["outcome"] != "pass"


def test_f12_ordinary_text_is_not_flagged(api):
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip(caption="The budget myth nobody talks about in the U.S.A. \U0001F468‍\U0001F469‍\U0001F467 #ad")), 201)
    assert d["outcome"] == "pass", d


def test_f12_zbm_quality_obfuscated_disclosure_is_not_a_pass(api):
    _, job, w = zbm_work_at_quality(api, work=zbm_work(quality={
        "opening_text": "Watch this froth.", "hook_ends_at_seconds": 1.5,
        "script_text": "Watch this froth. Acme oat milk froths like dairy. Try it free.",
        "supers": ["Acme logo end card"], "disclosure_text": "Pаid partnership"}))
    q = ok(api.post(f"/zbm/work/{w['work_id']}/quality", {"actor_id": "zbm_creative_quality", "notes": []}))
    assert q["stage"] != "quality_passed"


# =====================================================================================
# F13 — clipper can't pick an older, friendlier rulebook by backdating
# =====================================================================================

def _v2_live(api, clock, hours_after=2):
    zbc_open(api)
    goal2 = zbc_goal(never_say=["guaranteed returns", "get rich"])
    ok(api.post(f"{C}/revisions", {"actor_id": "zbc_rulebook_writer", "goal": goal2}), 201)
    ok(api.post(f"{C}/rulebooks/2/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.post(f"{C}/rulebooks/2/sign", andre=TEST_FOUNDER_TOKEN))
    clock.at = clock.at + timedelta(hours=hours_after)
    ok(api.post(f"{C}/rulebooks/2/go-live"))
    return clock.now()


LENIENT = "Get rich slowly. This budget myth. Listen on Pod Plus."


def test_f13_backdated_clip_long_after_supersession_goes_to_human_review(api, clock):
    superseded_at = _v2_live(api, clock)
    clock.at = superseded_at + timedelta(hours=73)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_backdated", transcript=LENIENT)), 201)  # posted_at = old NOW, in v1 window
    assert d["outcome"] == "human_review"
    assert any("grace" in r for r in d["human_review_reasons"])
    assert d["received_at"].startswith(clock.now().isoformat()[:19])


def test_f13_old_version_within_grace_is_judged_automatically(api, clock):
    superseded_at = _v2_live(api, clock)
    clock.at = superseded_at + timedelta(hours=71)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_ontime", transcript=LENIENT)), 201)
    assert d["outcome"] == "pass" and d["rulebook_version"] == 1


def test_f13_grace_window_is_configurable(make_api, clock):
    api = make_api(superseded_grace_hours=1)
    superseded_at = _v2_live(api, clock)
    clock.at = superseded_at + timedelta(hours=2)
    assert ok(api.post("/zbc/clips", zbc_clip("c", transcript=LENIENT)), 201)["outcome"] == "human_review"


def test_f13_receipt_time_is_on_the_ledger_and_future_posts_refused(api, clock):
    zbc_open(api)
    ok(api.post("/zbc/clips", zbc_clip()), 201)
    ev = api.ledger.of_type("clip_reviewed")[-1]
    assert ev["payload"]["received_at"].startswith(clock.now().isoformat()[:19])
    fut = (clock.now() + timedelta(seconds=1)).isoformat()
    assert api.post("/zbc/clips", zbc_clip("c_fut", posted_at=fut)).status_code == 409


def test_f13_bad_grace_config_refuses_to_start(monkeypatch):
    from api import grace_hours_from_env

    monkeypatch.setenv("CREATIVE_SUPERSEDED_GRACE_HOURS", "-5")
    with pytest.raises(RuntimeError):
        grace_hours_from_env()
    monkeypatch.setenv("CREATIVE_SUPERSEDED_GRACE_HOURS", "abc")
    with pytest.raises(RuntimeError):
        grace_hours_from_env()
    monkeypatch.delenv("CREATIVE_SUPERSEDED_GRACE_HOURS")
    assert grace_hours_from_env() == 72


# =====================================================================================
# F14 — no money / Decimal path can 500
# =====================================================================================

@pytest.mark.parametrize("target", ["NaN", "sNaN", "Infinity", "-Infinity", "1e5", " 5 ", "5.", ".5", "1" * 5000,
                                    "9" * 400 + ".5", 1e308, 10 ** 400, -0.0, "٣", True, None, [], {}])
def test_f14_success_target_never_500s(api, target):
    from samples import zbm_requirements

    req = zbm_requirements()
    req["success_in_numbers"][0]["target"] = target
    r = api.post("/zbm/briefs", {"requirements": req})
    assert r.status_code in (201, 422), (target, r.status_code, r.text[:200])


def test_f14_memory_comparison_never_500s(api):
    b = zbm_approved_brief(api)
    for v in (1e308, 0.0, 5e-324):
        r = api.post("/zbm/memory/results", {"brief_id": b["brief_id"], "result": {
            "result_id": "r1", "client_id": "client_acme", "creative_id": "c1", "vertical": "v", "platform": "youtube",
            "placement": "shorts", "hook_type": "question", "provenance": "measured", "measurement_source": "crm",
            "measurement_ref": "x", "impressions": 1, "metrics": {"trial_signups": v}}})
        assert r.status_code == 200, r.text


# =====================================================================================
# F16 / integration defect 5 — match ledger-rust validation exactly
# =====================================================================================

GOOD = dict(event_id="cp:x", department="creative_production", event_type="brief_approved",
            actor="zbm_creative_lead", subject_id="brief-0001", payload={}, summary="ok")

RUST_REJECTS = [
    {"summary": "a\u0085b"},             # C1 NEL
    {"summary": "a\u009fb"},             # C1 APC
    {"summary": "a\x7fb"},               # DEL
    {"summary": "\ud800"},               # lone surrogate: not valid UTF-8 / JSON for serde
    {"summary": "é" * 281},              # 281 scalar values
    {"summary": ""},
    {"subject_id": "brief-0001\n"},      # trailing newline ($ in Python re.match would allow it)
    {"event_id": "cp:x\n"},
    {"actor": "zbm_creative_lead\n"},
    {"subject_id": "b" * 129},
    {"subject_id": "brïef"},
]
RUST_ACCEPTS = [
    {"summary": "é" * 280},              # 280 chars, 560 bytes
    {"summary": "line sep"},       # U+2028 is Zl, not a control char
    {"summary": "pipes | and emoji \U0001F600"},
    {"subject_id": "b" * 128},
]


@pytest.mark.parametrize("bad", RUST_REJECTS)
def test_f16_fake_ledger_rejects_what_ledger_rust_rejects(bad):
    from shared.ledger import FakeLedgerClient, LedgerRecordError

    with pytest.raises(LedgerRecordError):
        FakeLedgerClient().record_event(**{**GOOD, **bad})


@pytest.mark.parametrize("bad", RUST_REJECTS)
def test_f16_http_client_refuses_locally_what_ledger_rust_rejects(bad):
    import httpx

    from shared.ledger import HttpLedgerClient, LedgerRecordError

    sent = []
    c = HttpLedgerClient("http://l.test", "t", transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(201)))
    with pytest.raises(LedgerRecordError):
        c.record_event(**{**GOOD, **bad})
    assert sent == []


@pytest.mark.parametrize("good", RUST_ACCEPTS)
def test_f16_fake_ledger_accepts_what_ledger_rust_accepts(good):
    from shared.ledger import FakeLedgerClient

    FakeLedgerClient().record_event(**{**GOOD, **good})


@pytest.mark.parametrize("raw", ["a\u0085b", "a\u009f\u0080b", "x\ud800y", "tab\there", "é" * 400])
def test_f16_recorder_summary_always_acceptable_to_ledger_rust(raw):
    from shared.ledger import EvidenceRecorder, FakeLedgerClient

    f = FakeLedgerClient()
    EvidenceRecorder(f).record("clip_reviewed", "zbc_clip_review", "clip_1", {}, raw)
    s = f.events[0]["summary"]
    assert not any(0x80 <= ord(ch) <= 0x9f or ord(ch) < 0x20 or ord(ch) == 0x7f or 0xd800 <= ord(ch) <= 0xdfff for ch in s)
    assert 1 <= len(s) <= 280


def test_f16_devtools_fake_server_matches_ledger_rust():
    import importlib.util
    import os
    from pathlib import Path

    os.environ.setdefault("FAKE_LEDGER_TOKEN", "t")
    p = Path(__file__).resolve().parents[1] / "devtools" / "fake_ledger_server.py"
    spec = importlib.util.spec_from_file_location("fake_ledger_server", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from shared.ledger import payload_sha256

    def body(over):
        b = {**GOOD, **over}
        b["payload_sha256"] = payload_sha256(b.pop("payload"))
        return b

    for bad in RUST_REJECTS:
        assert mod.validate(body(bad)) is not None, bad
    for good in RUST_ACCEPTS:
        assert mod.validate(body(good)) is None, good


# =====================================================================================
# Integration defect 2 — long ids are a 422, never a fake "ledger failure" 503
# =====================================================================================

def test_d2_campaign_id_whose_subject_cannot_fit_is_422_not_503(api):
    cid = "c" * 126
    zbc_rights_on_file(api)
    led = ledger_snapshot(api)
    r = api.post(f"/zbc/campaigns/{cid}/rulebooks", {"goal": zbc_goal(campaign_id=cid)})
    assert r.status_code == 422, (r.status_code, r.text[:300])
    assert ledger_snapshot(api) == led


def test_d2_path_ids_are_validated_before_any_work(api):
    led = ledger_snapshot(api)
    for bad in ("c" * 101, "has space", "semi;colon"):
        r = api.post(f"/zbc/campaigns/{bad}/rights-check", {"assets": ZBC_ASSETS})
        assert r.status_code in (404, 422) and r.status_code != 503, (bad, r.status_code)
    assert ledger_snapshot(api) == led


def test_d2_longest_accepted_campaign_id_goes_live(api):
    cid = "c" * 100
    ok(api.post("/rights/licenses", {"actor_id": "rights_desk", "license": zbc_license(campaign_id=cid)}), 201)
    ok(api.post("/rights/clearances", {"actor_id": "rights_desk", "record": zbc_music_clearance()}), 201)
    p = f"/zbc/campaigns/{cid}"
    ok(api.post(f"{p}/rulebooks", {"goal": zbc_goal(campaign_id=cid)}), 201)
    ok(api.post(f"{p}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.post(f"{p}/rulebooks/1/sign", andre=TEST_FOUNDER_TOKEN))
    ok(api.post(f"{p}/rights-check", {"assets": ZBC_ASSETS}))
    assert ok(api.post(f"{p}/rulebooks/1/go-live"))["status"] == "live"


def test_d2_recorder_refuses_unfit_fields_as_422_without_calling_the_ledger():
    from shared.errors import ValidationFailed
    from shared.ledger import EvidenceRecorder, FakeLedgerClient

    f = FakeLedgerClient()
    with pytest.raises(ValidationFailed):
        EvidenceRecorder(f).record("rulebook_drafted", "zbc_rulebook_writer", "c" * 126 + ":v1", {}, "s")
    assert f.events == []


# =====================================================================================
# Integration defect 3 — live_smoke handles finding entries
# =====================================================================================

def test_d3_live_smoke_summarises_every_entry_kind():
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "devtools" / "live_smoke.py"
    spec = importlib.util.spec_from_file_location("live_smoke", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # must not run anything or need env at import
    entries = [
        {"seq": 1, "finding_id": "disc-ord_1", "agent_id": "a", "entity_id": "e", "leak_category": "x",
         "amount_usd": "1.00", "recorded_at": "2026-09-24T00:00:00Z", "prev_hash": "0", "hash": "1"},
        {"seq": 2, "kind": "finding", "finding_id": "f2", "agent_id": "a", "entity_id": "e", "leak_category": "x",
         "amount_usd": None, "recorded_at": "2026-09-24T00:00:00Z", "prev_hash": "1", "hash": "2"},
        {"seq": 3, "kind": "event", "event_id": "e", "department": "onboarding", "event_type": "t", "actor": "a",
         "subject_id": "s", "payload_sha256": "0" * 64, "summary": "s", "recorded_at": "x", "prev_hash": "2", "hash": "3"},
        {"seq": 4, "kind": "event", "event_id": "e2", "department": "creative_production", "event_type": "brief_drafted",
         "actor": "a", "subject_id": "s", "payload_sha256": "0" * 64, "summary": "s", "recorded_at": "x",
         "prev_hash": "3", "hash": "4"},
    ]
    s = mod.summarize_entries(entries)
    assert s["findings"] == 2 and s["events"] == 2
    assert s["departments"] == ["creative_production", "onboarding"]
    assert s["creative_event_types"] == ["brief_drafted"]


# =====================================================================================
# follow-ups found while fixing (same classes of bug)
# =====================================================================================

def test_f13_a_refused_attempt_never_reserves_an_early_receipt_time(api, clock):
    superseded_at = _v2_live(api, clock)
    # 70h after supersession: a request refused for a NON-ledger reason (future posted_at)...
    clock.at = superseded_at + timedelta(hours=70)
    fut = (clock.now() + timedelta(days=1)).isoformat()
    assert api.post("/zbc/clips", zbc_clip("clip_x", posted_at=fut)).status_code == 409
    # ...must not let the same submission id claim that time later
    clock.at = superseded_at + timedelta(hours=80)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_x", transcript=LENIENT)), 201)
    assert d["outcome"] == "human_review" and d["received_at"].startswith(clock.now().isoformat()[:19])


def _fail_type_ledger():
    from shared.ledger import FakeLedgerClient, LedgerRecordError

    class FailType(FakeLedgerClient):
        target: str = ""

        def record_event(self, **kw):
            if kw["event_type"] == self.target:
                raise LedgerRecordError("down")
            return super().record_event(**kw)

    return FailType()


def test_f9_go_live_answer_record_failure_is_partial_not_did_not_take_effect(make_api):
    led = _fail_type_ledger()
    api = make_api(ledger=led)
    _signed_with_rights(api)
    led.target = "crossing_clipper_network"
    r = api.post(f"{C}/rulebooks/1/go-live")
    assert r.status_code == 503 and r.json()["took_effect"] == "partial"
    assert api.zbc.rulebooks.get(CAMPAIGN, 1).status.value == "live"   # it IS live, and recorded
    assert led.of_type("rulebook_live")


def test_f9_kit_answer_record_failure_is_partial_and_answers_not_applied(make_api):
    from shared.departments import Departments

    led = _fail_type_ledger()
    api = make_api(ledger=led, departments=Departments(creative_agents=AcceptingCreativeAgents()))
    zbc_live(api)
    ok(api.post(f"{C}/moment-map", zbc_source()))
    ok(api.post(f"{C}/hook-sheets"))
    led.target = "crossing_creative_agents"
    r = api.post(f"{C}/kit", zbc_kit_request())
    assert r.status_code == 503 and r.json()["took_effect"] == "partial"
    kit = api.zbc.kits[CAMPAIGN]
    assert kit.seed_clips_produced is False and all(c["status"] == "requested" for c in kit.commissions)


def test_f8_concurrent_submissions_cannot_open_two_rounds(make_api):
    import threading
    import time

    from shared.ledger import FakeLedgerClient

    class Slow(FakeLedgerClient):
        def record_event(self, **kw):
            if kw["event_type"] == "work_submitted":
                time.sleep(0.05)
            return super().record_event(**kw)

    api = make_api(ledger=Slow())
    brief, job, w = zbm_work_at_quality(api)
    ok(api.post(f"/zbm/work/{w['work_id']}/quality", Q))  # sent back: one new version allowed
    codes = []
    ts = [threading.Thread(target=lambda: codes.append(api.post(f"/zbm/jobs/{job['job_id']}/work", zbm_work()).status_code))
          for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [201, 409, 409, 409]


def test_f12_ordinary_latin_letters_of_real_languages_are_not_signals():
    from shared.text import obfuscation_signals

    assert obfuscation_signals("Bu bütçe efsanesi ılık değil #ad") == []   # Turkish dotless i
    assert obfuscation_signals("Clips in the U.S.A. #ad") == []
    assert obfuscation_signals("family \U0001F468‍\U0001F469‍\U0001F467") == []
    assert obfuscation_signals("b u d g e t") != []
    assert obfuscation_signals("bud​get") != []


def test_http_client_bad_url_is_not_recorded_not_a_500():
    from shared.ledger import HttpLedgerClient, LedgerRecordError

    with pytest.raises(LedgerRecordError):
        HttpLedgerClient("http://[bad", "t").record_event(**GOOD)
