"""
Fix wave 2 (AEGIS round 2, Sep 24 2026) — reproductions written to FAIL on
the pre-fix code (ae75124) and pass after the fix:

N1   one failed ledger write let a submission id keep a fake receipt time
     forever, and different content could be recorded under that id.
     Now: the retry cache is bound to the exact content (canonical hash)
     and expires after RETRY_WINDOW; different content under a used
     submission id is a 409; clip event ids carry the content hash.
F13  (with N1) the backdating route through a reserved receipt time is closed.
N4   (a) actor identity is authenticated (per-actor token, server-side);
     the body can no longer assert who acts; (b) the 2-round review cap
     and the escalation freeze apply per CLIENT DELIVERABLE (client +
     spec fingerprint), not per brief id — a cloned brief can't escape.
N3   never-say survives default-ignorables (Hangul fillers, bidi
     controls), Latin-script lookalikes (small capitals, stroked letters),
     Cherokee lookalikes; any non-Latin letter in an English campaign's
     clip, or script mixing inside a word, is never an automatic pass.
"""

from __future__ import annotations

import random
import zlib
from datetime import timedelta

import pytest

from conftest import NOW, TEST_ACTOR_TOKENS, TEST_FOUNDER_TOKEN, Api
from flows import C, ok, zbc_open, zbm_approved_brief, zbm_work_at_quality
from samples import zbc_clip, zbc_goal, zbm_requirements, zbm_work
from shared.clock import FixedClock
from shared.ledger import FakeLedgerClient, LedgerRecordError

ACTOR_HEADER = "X-Creative-Actor-Token"
LENIENT = "Get rich slowly. This budget myth. Listen on Pod Plus."


def _hdr(actor: str | None) -> dict:
    return {ACTOR_HEADER: TEST_ACTOR_TOKENS[actor]} if actor else {}


# =====================================================================================
# N1 / F13 — receipt time bound to content, expiring
# =====================================================================================

def _clip_events(ledger, sid):
    return [e for e in ledger.of_type("clip_reviewed") if e["subject_id"] == sid]


def test_n1_different_content_under_a_failed_submission_id_is_refused():
    led = FakeLedgerClient()
    api = Api(ledger=led, clock=FixedClock(NOW))
    zbc_open(api)
    led.fail_next = True
    r = api.post("/zbc/clips", zbc_clip("clip_r", transcript="whatever"))
    assert r.status_code == 503
    r = api.post("/zbc/clips", zbc_clip("clip_r", transcript=LENIENT))
    assert r.status_code == 409, r.text
    assert _clip_events(led, "clip_r") == []


def test_n1_f13_aegis_backdating_route_is_closed():
    """AEGIS cr2: junk as clip_r during an outage, v2 goes live, 30 days later a
    backdated v1 clip under clip_r must NOT carry the old receipt time."""
    clock = FixedClock(NOW)
    led = FakeLedgerClient()
    api = Api(ledger=led, clock=clock)
    zbc_open(api)
    led.fail_next = True
    assert api.post("/zbc/clips", zbc_clip("clip_r", transcript="whatever")).status_code == 503
    # same-content attempt made during the outage too (the honest-retry case)
    led.fail_next = True
    assert api.post("/zbc/clips", zbc_clip("clip_q", transcript=LENIENT)).status_code == 503
    goal2 = zbc_goal(never_say=["guaranteed returns", "get rich"])
    ok(api.post(f"{C}/revisions", {"actor_id": "zbc_rulebook_writer", "goal": goal2}), 201)
    ok(api.post(f"{C}/rulebooks/2/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.post(f"{C}/rulebooks/2/sign", andre=TEST_FOUNDER_TOKEN))
    clock.at = clock.at + timedelta(hours=2)
    ok(api.post(f"{C}/rulebooks/2/go-live"))
    clock.at = clock.at + timedelta(days=30)
    # different content under the reserved id: refused, not recorded
    r = api.post("/zbc/clips", zbc_clip("clip_r", transcript=LENIENT))
    assert r.status_code == 409, r.text
    # identical content 30 days later: the old receipt time has expired
    d = ok(api.post("/zbc/clips", zbc_clip("clip_q", transcript=LENIENT)), 201)
    assert d["received_at"].startswith(clock.now().isoformat()[:19])
    assert d["outcome"] == "human_review"
    assert any("grace window" in x for x in d["human_review_reasons"])


def test_n1_retry_inside_window_keeps_first_receipt_time_once():
    clock = FixedClock(NOW)
    led = FakeLedgerClient()
    api = Api(ledger=led, clock=clock)
    zbc_open(api)
    first = clock.now()
    led.fail_next = True
    assert api.post("/zbc/clips", zbc_clip("clip_w")).status_code == 503
    clock.at = first + timedelta(minutes=10)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_w")), 201)
    assert d["received_at"].startswith(first.isoformat()[:19])
    assert len(_clip_events(led, "clip_w")) == 1


class CommitThenLose(FakeLedgerClient):
    """The ledger commits, then the response is lost (the client sees a failure)."""

    lose_next: bool = False

    def record_event(self, *a, **k):
        super().record_event(*a, **k)
        if self.lose_next:
            self.lose_next = False
            raise LedgerRecordError("response lost after commit (test double)")


def test_n1_event_id_bound_to_content_ledger_refuses_second_version():
    clock = FixedClock(NOW)
    led = CommitThenLose()
    api = Api(ledger=led, clock=clock)
    zbc_open(api)
    led.lose_next = True
    assert api.post("/zbc/clips", zbc_clip("clip_x")).status_code == 503
    assert len(_clip_events(led, "clip_x")) == 1  # it IS on the ledger
    # different content under the same id: refused by the service
    assert api.post("/zbc/clips", zbc_clip("clip_x", caption="other #ad")).status_code == 409
    # identical content after the window. Fix wave 4 (LOST) — this test used to assert the
    # WEDGE (ledger 409 -> 503 "did NOT take effect" forever while the ledger held the
    # decision). Now the uncertain attempt's exact record is replayed: the ledger answers
    # 200, the decision takes effect once, with its first (true) receipt time.
    clock.at = clock.at + timedelta(hours=1)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_x")), 201)
    assert d["received_at"].startswith(NOW.isoformat()[:19])
    assert len(_clip_events(led, "clip_x")) == 1


def test_n1_event_id_includes_content_hash(api):
    zbc_open(api)
    ok(api.post("/zbc/clips", zbc_clip("clip_h")), 201)
    ev = _clip_events(api.ledger, "clip_h")[0]
    from zbc.workflow import submission_sha256
    from zbc.clip_review import ClipSubmission

    h = submission_sha256(ClipSubmission.model_validate(zbc_clip("clip_h")))
    rec = api.app.state.recorder
    other = rec.event_id_for("clip_reviewed", "zbc_clip_review", "clip_h", {"submission_id": "clip_h", "content_sha256": "0" * 64})
    assert ev["payload"]["content_sha256"] == h
    assert ev["event_id"] != other


def test_n1_human_review_time_bound_to_verdict():
    """Sweep: the human-review retry cache had the same flaw (keyed by
    submission only). A different verdict never inherits the failed
    attempt's time. (Fix wave 6, N5: this test used to expect the
    different verdict to be REFUSED for 15 minutes after a CERTAIN
    failure; a certain failure holds nothing, so it is accepted at once,
    with the clock's time.)"""
    clock = FixedClock(NOW)
    led = FakeLedgerClient()
    api = Api(ledger=led, clock=clock)
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_hr", resolution_height_px=None)), 201)
    assert d["outcome"] == "human_review"
    led.fail_next = True
    r = api.post("/zbc/clips/clip_hr/human-review", {"actor_id": "zbc_clip_human_reviewer", "outcome": "pass"})
    assert r.status_code == 503 and r.json()["took_effect"] is False
    clock.at = clock.at + timedelta(minutes=1)
    d = ok(api.post("/zbc/clips/clip_hr/human-review",
                    {"actor_id": "zbc_clip_human_reviewer", "outcome": "reject",
                     "broken_rules": [{"rule_id": "QF-01", "reason": "low"}]}))
    assert d["decided_at"].startswith(clock.now().isoformat()[:19])  # the clock, not the failed attempt's time
    assert len(led.of_type("clip_human_reviewed")) == 1


# =====================================================================================
# N4 (a) — authenticated actors
# =====================================================================================

def test_n4_body_asserted_actor_without_credential_is_refused(api):
    b = ok(api.client.post("/zbm/briefs", json={"requirements": zbm_requirements()},
                           headers=_hdr("zbm_brief_writer")), 201)
    r = api.client.post(f"/zbm/briefs/{b['brief_id']}/review", json={"actor_id": "zbm_creative_lead"})
    assert r.status_code == 401, r.text
    r = api.client.post(f"/zbm/briefs/{b['brief_id']}/review", json={"actor_id": "zbm_creative_lead"},
                        headers={ACTOR_HEADER: "not-a-token"})
    assert r.status_code == 401, r.text
    assert api.zbm.get_brief(b["brief_id"]).status.value == "draft"


def test_n4_body_cannot_claim_someone_else(api):
    b = ok(api.client.post("/zbm/briefs", json={"requirements": zbm_requirements()},
                           headers=_hdr("zbm_brief_writer")), 201)
    # the drafter's own credential, claiming to be the Creative Lead
    r = api.client.post(f"/zbm/briefs/{b['brief_id']}/review", json={"actor_id": "zbm_creative_lead"},
                        headers=_hdr("zbm_brief_writer"))
    assert r.status_code == 403, r.text
    assert api.zbm.get_brief(b["brief_id"]).status.value == "draft"
    # identity comes from the credential; body actor_id may be omitted
    d = ok(api.client.post(f"/zbm/briefs/{b['brief_id']}/review", json={}, headers=_hdr("zbm_creative_lead")))
    assert d["status"] == "approved" and d["approved_by"] == "zbm_creative_lead"


def test_n4_drafter_ne_approver_on_authenticated_identity():
    from shared.actors import ActorRegistry, Role

    reg = ActorRegistry()
    reg.add("maya", [Role.ZBC_RULEBOOK_WRITER, Role.ZBC_CAMPAIGN_RULEBOOK])
    tokens = {**TEST_ACTOR_TOKENS, "maya": "maya-token-0123456789abcdef"}
    api = Api(ledger=FakeLedgerClient(), clock=FixedClock(NOW), actors=reg, actor_tokens=tokens)
    from flows import zbc_rights_on_file

    zbc_rights_on_file(api)
    h = {ACTOR_HEADER: tokens["maya"]}
    ok(api.client.post(f"{C}/rulebooks", json={"goal": zbc_goal()}, headers=h), 201)
    r = api.client.post(f"{C}/rulebooks/1/review", json={}, headers=h)
    assert r.status_code == 403 and "self-approval" in r.text


def test_n4_actor_tokens_not_configured_fails_closed():
    api = Api(ledger=FakeLedgerClient(), clock=FixedClock(NOW), actor_tokens={})
    r = api.client.post("/zbm/briefs", json={"requirements": zbm_requirements()},
                        headers=_hdr("zbm_brief_writer"))
    assert r.status_code == 403 and "not configured" in r.text


@pytest.mark.parametrize("bad", [
    {"zbm_creative_lead": "short"},                                            # too short
    {"zbm_creative_lead": "same-token-0123456789", "zbm_brief_writer": "same-token-0123456789"},  # shared
    {"nobody_known": "token-0123456789abcdef"},                                 # unknown actor
    {"andre": "token-0123456789abcdef"},                                        # Andre never via actor token
])
def test_n4_bad_actor_token_config_refuses_to_start(bad):
    from api import build_app

    with pytest.raises(RuntimeError):
        build_app(service_token="svc-token-0123456789", ledger=FakeLedgerClient(), actor_tokens=bad)


def test_n4_actor_token_equal_to_service_or_founder_token_refused():
    from api import build_app

    for tok in ("svc-token-0123456789", "andre-token-0123456789"):
        with pytest.raises(RuntimeError):
            build_app(service_token="svc-token-0123456789", founder_token="andre-token-0123456789",
                      ledger=FakeLedgerClient(), actor_tokens={"zbm_creative_lead": tok})


def test_n4_actor_tokens_from_env(monkeypatch):
    import json as _json

    from api import actor_tokens_from_env

    monkeypatch.setenv("CREATIVE_ACTOR_TOKENS", _json.dumps({"zbm_creative_lead": "lead-token-0123456789"}))
    assert actor_tokens_from_env() == {"zbm_creative_lead": "lead-token-0123456789"}
    monkeypatch.setenv("CREATIVE_ACTOR_TOKENS", "not json")
    with pytest.raises(RuntimeError):
        actor_tokens_from_env()
    monkeypatch.delenv("CREATIVE_ACTOR_TOKENS")
    assert actor_tokens_from_env() == {}


def test_n4_non_ascii_actor_token_is_401_not_500(api):
    r = api.client.post("/zbm/briefs", json={"requirements": zbm_requirements()},
                        headers={ACTOR_HEADER: b"caf\xc3\xa9"})
    assert r.status_code == 401


# =====================================================================================
# N4 (b) — review cap per client deliverable, not per brief id
# =====================================================================================

Q = {"actor_id": "zbm_creative_quality", "notes": ["Not premium."]}


def _to_quality(api, job_id, work=None):
    w = ok(api.post(f"/zbm/jobs/{job_id}/work", work or zbm_work()), 201)
    ok(api.post(f"/zbm/work/{w['work_id']}/export-validation"))
    ok(api.post(f"/zbm/work/{w['work_id']}/rights"))
    return w


def _escalate(api):
    brief, job, w = zbm_work_at_quality(api)
    assert ok(api.post(f"/zbm/work/{w['work_id']}/quality", Q))["stage"] == "sent_back"
    w2 = _to_quality(api, job["job_id"])
    assert ok(api.post(f"/zbm/work/{w2['work_id']}/quality", Q))["stage"] == "escalated_to_andre"
    return brief, job, w2


def test_n4_cloned_brief_cannot_escape_open_escalation(api):
    """AEGIS cr5: clone the brief while an escalation is open -> 409."""
    _escalate(api)
    r = api.post("/zbm/briefs", {"requirements": zbm_requirements()}, as_actor="zbm_brief_writer")
    assert r.status_code == 409, r.text
    assert "fingerprint" in r.text or "escalat" in r.text


def test_n4_fingerprint_ignores_deliverable_id_and_reduces_aspect(api):
    _escalate(api)
    d = [{"deliverable_id": "renamed", "platform": "youtube", "placement": "shorts",
          "length_seconds": 30, "aspect_ratio": "18:32", "format": "mp4", "count": 1}]
    r = api.post("/zbm/briefs", {"requirements": zbm_requirements(deliverables=d)}, as_actor="zbm_brief_writer")
    assert r.status_code in (409, 201)
    if r.status_code == 201:  # 18:32 might be refused by the spec row; either way no third round
        pytest.fail("renamed / non-reduced clone of an escalated deliverable must be refused")


def test_n4_rounds_carry_across_briefs_for_same_client_deliverable(api):
    brief, job, w = zbm_work_at_quality(api)
    assert ok(api.post(f"/zbm/work/{w['work_id']}/quality", Q))["stage"] == "sent_back"
    b2 = zbm_approved_brief(api)  # no escalation yet: a new brief is allowed
    j2 = ok(api.post(f"/zbm/briefs/{b2['brief_id']}/jobs"), 201)
    w3 = _to_quality(api, j2["job_id"])
    assert w3["round"] == 2
    q = ok(api.post(f"/zbm/work/{w3['work_id']}/quality", Q))
    assert q["stage"] == "escalated_to_andre"


def test_n4_only_one_in_flight_version_per_client_deliverable(api):
    brief, job, w = zbm_work_at_quality(api)
    b2 = zbm_approved_brief(api)
    j2 = ok(api.post(f"/zbm/briefs/{b2['brief_id']}/jobs"), 201)
    r = api.post(f"/zbm/jobs/{j2['job_id']}/work", zbm_work())
    assert r.status_code == 409, r.text


def test_n4_different_client_or_spec_is_not_blocked(api):
    _escalate(api)
    ok(api.post("/zbm/briefs", {"requirements": zbm_requirements(client_id="client_other")},
                as_actor="zbm_brief_writer"), 201)
    d = [{"deliverable_id": "d1", "platform": "youtube", "placement": "shorts",
          "length_seconds": 15, "aspect_ratio": "9:16", "format": "mp4", "count": 1}]
    ok(api.post("/zbm/briefs", {"requirements": zbm_requirements(deliverables=d)}, as_actor="zbm_brief_writer"), 201)


def test_n4_after_andre_resolves_a_new_brief_starts_fresh(api):
    _, _, w2 = _escalate(api)
    ok(api.post(f"/zbm/work/{w2['work_id']}/escalation", {"decision": "kill"}, andre=TEST_FOUNDER_TOKEN))
    b3 = zbm_approved_brief(api)
    j3 = ok(api.post(f"/zbm/briefs/{b3['brief_id']}/jobs"), 201)
    w = _to_quality(api, j3["job_id"])
    assert w["round"] == 1


def test_n4_brief_drafted_before_escalation_cannot_be_approved_or_opened(api):
    b_early = ok(api.post("/zbm/briefs", {"requirements": zbm_requirements()}, as_actor="zbm_brief_writer"), 201)
    b_early_ok = ok(api.post("/zbm/briefs", {"requirements": zbm_requirements()}, as_actor="zbm_brief_writer"), 201)
    ok(api.post(f"/zbm/briefs/{b_early_ok['brief_id']}/review", {"actor_id": "zbm_creative_lead"}))
    _escalate(api)
    r = api.post(f"/zbm/briefs/{b_early['brief_id']}/review", {"actor_id": "zbm_creative_lead"})
    assert r.status_code == 409, r.text
    r = api.post(f"/zbm/briefs/{b_early_ok['brief_id']}/jobs")
    assert r.status_code == 409, r.text


# =====================================================================================
# N3 — never-say bypasses
# =====================================================================================

AEGIS_VARIANTS = [
    "Getㅤrich slowly. This budget myth. Listen on Pod Plus.",        # U+3164 Hangul filler
    "ɢet rich slowly. This budget myth. Listen on Pod Plus.",          # U+0262 small capital G
    "Get riᅠch slowly. This budget myth. Listen on Pod Plus.",         # U+FFA0 halfwidth filler
    "‮hcir teG‬ slowly. This budget myth. Listen on Pod Plus.",  # RLO
    "ǥet ricħ slowly. This budget myth. Listen on Pod Plus.",           # stroked g, h
    "Ǥet rich slowly. This budget myth. Listen on Pod Plus.",           # capital G with stroke
    "Ꮐet rich slowly. This budget myth. Listen on Pod Plus.",           # Cherokee GA
    "Get ꭱich slowly. This budget myth. Listen on Pod Plus.",           # Cherokee small letter (U+AB71)
    "Getᅟrich slowly. This budget myth. Listen on Pod Plus.",      # Hangul choseong filler
    "Get⁦ rich slowly. This budget myth. Listen on Pod Plus.",     # LRI isolate
    "Ge\U000E0074t rich slowly. This budget myth. Listen on Pod Plus.",  # tag character
    "Get ri︀ch slowly. This budget myth. Listen on Pod Plus.",     # variation selector
    "Get ʀich slowly. This budget myth. Listen on Pod Plus.",           # small capital R
    "Get rıch slowly. This budget myth. Listen on Pod Plus.",           # dotless i
    "𝐆𝐞𝐭 𝐫𝐢𝐜𝐡 slowly. This budget myth. Listen on Pod Plus.",          # math bold
    "Ｇｅｔ ｒｉｃｈ slowly. This budget myth. Listen on Pod Plus.",          # fullwidth
]


@pytest.fixture
def gr_api(api):
    zbc_open(api, goal=zbc_goal(never_say=["guaranteed returns", "get rich"]))
    return api


@pytest.mark.parametrize("text", AEGIS_VARIANTS)
def test_n3_aegis_variants_never_auto_pass(gr_api, text):
    sid = f"clip_{zlib.crc32(text.encode())}"
    for field in ("transcript", "caption"):
        body = zbc_clip(f"{sid}{field[0]}", **{field: text + (" #ad" if field == "caption" else "")})
        d = ok(gr_api.post("/zbc/clips", body), 201)
        assert d["outcome"] != "pass", (field, text, d)


@pytest.mark.parametrize("text,expect", [
    ("Getㅤrich slowly. This budget myth. Listen on Pod Plus.", "NS-02"),
    ("ɢet rich slowly. This budget myth. Listen on Pod Plus.", "NS-02"),
    ("ǥet ricħ slowly. This budget myth. Listen on Pod Plus.", "NS-02"),
    ("Ǥet rich slowly. This budget myth. Listen on Pod Plus.", "NS-02"),
    ("𝐆𝐞𝐭 𝐫𝐢𝐜𝐡 slowly. This budget myth. Listen on Pod Plus.", "NS-02"),
])
def test_n3_folded_variants_reject_on_the_rule(gr_api, text, expect):
    d = ok(gr_api.post("/zbc/clips", zbc_clip(f"clip_f{zlib.crc32(text.encode())}", transcript=text)), 201)
    if expect:
        assert d["outcome"] == "reject" and expect in [b["rule_id"] for b in d["broken_rules"]], d
    else:
        assert d["outcome"] != "pass"


def test_n3_non_latin_letter_in_english_campaign_goes_to_human(api):
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_cy", transcript="This budget myth costs you. Listen on Pod Plus. Привет")), 201)
    assert d["outcome"] == "human_review"
    assert any("non-Latin" in r for r in d["human_review_reasons"])
    # plain text with emoji and accented Latin still passes automatically
    d = ok(api.post("/zbc/clips", zbc_clip("clip_ok", caption="The budget myth nobody talks about 🔥 café #ad")), 201)
    assert d["outcome"] == "pass", d


def test_n3_bidi_or_filler_anywhere_is_never_auto_pass(api):
    zbc_open(api)
    for i, ch in enumerate(["‮", "⁧", "‏", "؜", "ㅤ", "ﾠ", "ᅟ"]):
        d = ok(api.post("/zbc/clips", zbc_clip(f"clip_bd{i}", caption=f"The budget myth #ad {ch}")), 201)
        assert d["outcome"] != "pass", (hex(ord(ch)), d)


def test_n3_default_ignorable_table_is_complete():
    """Every code point Unicode lists as Default_Ignorable_Code_Point (DerivedCoreProperties
    15.1; ranges copied from the file) is in shared.text's set."""
    from shared.text import is_default_ignorable

    ranges = [(0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
              (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
              (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
              (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF)]
    for lo, hi in ranges:
        for cp in range(lo, hi + 1):
            assert is_default_ignorable(chr(cp)), hex(cp)
    for ch in "a Z0 é ’🔥":
        assert not is_default_ignorable(ch), hex(ord(ch))


# --- fuzz ------------------------------------------------------------------------------

def _pools():
    from shared.text import DEFAULT_IGNORABLE_RANGES, lookalikes_of

    ignorables = [chr(cp) for lo, hi in DEFAULT_IGNORABLE_RANGES for cp in range(lo, min(hi, lo + 40) + 1)]
    return ignorables, lookalikes_of


def test_n3_fuzz_never_say_with_ignorables_and_confusables_never_auto_passes(registry):
    """Random insertion of default-ignorables and substitution of lookalike
    letters into never-say phrases: the clip is never an automatic pass."""
    from zbc import clip_review, rulebook_writer
    from zbc.rulebook import RulebookStatus
    from zbc.rulebook_writer import CampaignGoal
    from samples import TODAY

    phrases = ["get rich", "guaranteed returns", "risk free", "double your money"]
    goal = CampaignGoal.model_validate(zbc_goal(never_say=phrases))
    rb = rulebook_writer.draft(goal, registry, TODAY).model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW})
    ignorables, lookalikes_of = _pools()
    rng = random.Random(20260924)
    tried = 0
    for i in range(1500):
        phrase = rng.choice(phrases)
        chars = list(phrase if rng.random() < 0.5 else phrase.title())
        out = []
        for ch in chars:
            if ch.isalpha() and rng.random() < 0.35:
                alts = lookalikes_of(ch)
                if alts:
                    ch = rng.choice(alts)
            out.append(ch)
            if rng.random() < 0.3:
                out.append(rng.choice(ignorables))
        mutated = "".join(out)
        field = rng.choice(["transcript", "caption", "on_screen_text"])
        base = zbc_clip(f"fz{i}")
        base[field] = f"{base[field]} {mutated}"
        sub = clip_review.ClipSubmission.model_validate(base)
        d = clip_review.review(sub, rb, registry, NOW)
        assert d.outcome != "pass", (i, repr(mutated), field)
        tried += 1
    assert tried == 1500


def test_n3_lookalike_table_covers_required_families():
    from shared.text import canonical

    for s in ["ɢ", "ǥ", "ħ", "Ꮐ", "ꭱ", "г", "ρ", "𝐠", "ｇ", "ɡ", "ʀ", "ı", "ł", "ø", "đ", "ƀ", "ɨ"]:
        assert canonical(s).isascii() and canonical(s).isalpha() or canonical(s) == "", (s, canonical(s))
    assert canonical("Getㅤrich") == "get rich"
    assert canonical("ɢet ʀich") == "get rich"
    assert canonical("Ǥet riᅠch") == "get ri ch"
