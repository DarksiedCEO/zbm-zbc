"""
Fix wave 8 (Sep 24, 2026) — AEGIS round-7 findings against creative-py.

N7-1  the JSON shape pre-scan (member / depth caps) gated on the exact
      content type `application/json`, while FastAPI parses every
      `application/*+json` body: `application/hal+json` with a 60k-key
      body reached the framework (60,001 validation errors, /health 3.7 s
      under 20 senders, RSS never released). Now the pre-scan applies to
      every body FastAPI would parse as JSON (`application/json`,
      `application/<x>+json`, any parameters, any case), any other
      content type on a body is 415 BEFORE the body is read, and freed
      heap pages are handed back after a large parse (malloc_trim, as in
      fulfillment-py).
N7-3  rule ids were `XX-NN` (00-99) while the goal model admits 1,000
      never-say / 100 must-say entries: the 100th entry was a 500, and
      `revise()` never reuses retired ids, so a churning campaign wedged
      at NS-100. Ids are now `XX-` + a decimal number of 2 to 9 digits
      (NS-01 … NS-99, NS-100, …; the two-digit form is unchanged, so
      every existing id parses); the numbering is computed once per
      prefix per revision.
N7-4  an emoji or symbol standing for a phrase word auto-passed ("make
      💰", "make $$$ fast", "free 💸", "guaranteed 📈", "get 💎 quick").
      A documented symbol lexicon (shared.text.SYMBOL_LEXICON) maps
      money / wealth / growth / gem / rocket / fire / lock / check symbols
      and `$$$`-style runs to concept words; a never-say phrase with a
      word stood in by a lexicon symbol, or by any other pictograph in
      that word's place, is a human's call (`symbol_stand_in`).
N7-5  a respelled phrase split across caption and bio passed while the
      same halves in caption + on-screen text went to a human: the
      fields were only adjacent in model order. Now every ordered pair of
      fields is read across its boundary with every never-say signal.
B     stacked respellings ("grnteed retunrs", "overnlte sccss", "lose
      vvait fst") passed: no single signal was within budget. Root
      causes fixed (the skeleton signal's vowel-drop evidence ignored the
      very token that dropped the vowels; the skeleton and phonetic
      signals now also read the visually normalised stream, rn/m cl/d
      vv/w) and, as required, two of the three signals each within
      budget + 1 on the same window is a human's call (`stacked_near_miss`).
N7-7  Clip Review of 99 phrases against a 50 KB vowel-dropped transcript
      cost 5.9 s CPU under the serialised lock: per-phrase work in
      `near_miss` / `match_phrase` was repeated for every phrase. The
      per-haystack views are now built once and shared; worst case
      measured below.
"""

from __future__ import annotations

import itertools
import json
import random
import string
import time

import pytest
from conftest import NOW
from ordinary_captions import CAPTIONS as IMPL_CAPTIONS
from samples import TODAY, zbc_clip, zbc_goal
from test_fix_wave_6 import AEGIS_CAPTIONS as AEGIS5_CAPTIONS
from test_fix_wave_7 import CORPUS6, NEVER_SAY, _review, _rulebook

TAIL = " This budget myth. Listen on Pod Plus. #ad"

# The AEGIS round-7 corpus (probes/corpus7.py), VERBATIM (130 captions).
CORPUS7 = [
    'Three steps, two minutes, one glow. The routine is in the pinned comment',
    'We reformulated the serum. Less fragrance, same finish',
    'Wear it under makeup or alone. Either way, SPF 50',
    'The lip oil restock is live. Set an alarm next time',
    'Patch test first. Always. Then tell us what you think',
    'Our cleanser now ships in a refill pouch. Less plastic, same price',
    'Dermatologist tested, fragrance free, made in small batches',
    'Ask us anything about retinol in the comments tonight at 8',
    'Winter skin needs a heavier cream. Here is ours',
    'Sold out twice. Back on the shelf Friday',
    'The trench is back in two lengths. Which one are you?',
    'Made in Portugal, cut for real shoulders',
    'Pre-orders for the wool coat close Sunday',
    'Wash cold, hang dry, wear forever',
    'New drop: five colours of the everyday tee',
    'Our jeans come in three rises now. Find yours in the size guide',
    'Vintage wash, modern fit. The denim jacket you keep borrowing',
    'Free returns on every order through the holidays',
    'Sizes XS to 4X, because our customers asked',
    'The sneaker is lighter this year. We shaved forty grams',
    'Single origin from Huila, roasted Tuesday, shipped Wednesday',
    'Oat milk is now the default. Dairy on request',
    'Our decaf is Swiss water processed. Yes, it tastes like coffee',
    'Cold brew concentrate: one bottle, twelve cups',
    'The seasonal blend is back, and it is very much a cinnamon situation',
    'Two for one on pastries with any pour-over before nine',
    'We switched to compostable cups this month',
    'Loyalty cards are now in the app. Your stamps carried over',
    'Sparkling tea, zero sugar, four flavours. Try the yuzu first',
    'Wholesale accounts open for cafes in the metro area',
    'Grain free is not always better. Here is what the vet actually said',
    'The harness fits dogs from 8 to 90 pounds. Measure twice',
    'Our new chew is one ingredient: dried sweet potato',
    'Litter that clumps in seconds and does not track. We tested seven',
    'Adopt, then shop. Ten percent of March sales go to the shelter',
    'Slow feeder bowls back in stock in all three sizes',
    'Cat trees that survive a Maine Coon. Assembly takes ten minutes',
    'Reach out on chat if the collar does not fit; exchanges are free',
    'Every treat bag now has a batch number and a best-before date',
    'The puppy course starts in April. Six weeks, small groups',
    'Round-ups are live. Spare change goes to your savings pot automatically',
    'Your statements are now available as PDF and CSV',
    'We cut the international transfer fee to a flat two dollars',
    'Budget categories you can rename. Finally.',
    'Card frozen from the app in one tap. Unfreeze the same way',
    'Interest is paid monthly and shown in the app the same day',
    'Our support team answers in under four minutes on average',
    'New: split a bill with anyone, even if they are not on the app',
    'Read the fee schedule before you open any account. Ours is one page',
    'Tax season tools are ready. Export your year in a click',
    'The cohort fills every time. Waitlist opens Monday',
    'Homework is optional. The office hours are not',
    'We recorded every lecture, so miss one and catch up later',
    'New module: writing cold emails that get answered',
    'Students get the textbook free with enrolment',
    'Learn to sew a zipper in one afternoon. Kits included',
    'The Spanish for travel course is now four weeks instead of six',
    'Certificates issued on completion, shareable on your profile',
    'Scholarship applications close on the 15th',
    'Our instructors are working designers. Ask them anything',
    'Ten minute mobility, every morning this month. Follow along',
    'The kettlebell set ships in two boxes. Both arrive the same day',
    'Sleep, water, protein. Then worry about the supplement',
    'Rest between sets is part of the set',
    'Class packs do not expire. Use them when life allows',
    'Reach the top of the hill without stopping. That is the goal this week',
    'Foam rollers are back in the studio shop',
    'Our trainers are all certified. Ask to see it',
    'New timetable from Monday: earlier spin, later yoga',
    'Hydration packs on sale until the end of the month',
    'Plant the bulbs now, thank yourself in April',
    'Our linen sheets soften with every wash',
    'The candle burns for sixty hours. We timed it',
    'Compost bins with a charcoal filter, so the kitchen stays fresh',
    'Get the garden ready for frost with our checklist',
    'Ceramic planters in three glazes, made in our studio',
    'Rugs you can put in the washing machine. Yes, the big one too',
    'The sofa arrives in a box and takes fifteen minutes to build',
    'Seed packets are two for the price of one this weekend',
    'The care guide for every plant we sell is on the label',
    'Book the boiler service before the cold snap',
    'Same-day bike repairs while you wait, most parts in stock',
    'Our cleaners bring their own supplies. You bring nothing',
    'The barbershop now takes walk-ins on Sundays',
    'Tutoring for exams: small groups, evenings and weekends',
    'Dog walking slots open in the north of the city',
    'Movers who show up on time. Read the reviews',
    'Photography for small businesses: half-day sessions from March',
    'We fix screens, batteries and ports. Most repairs done in an hour',
    'The tailor is back from holiday. Alterations from Tuesday',
    'Doors at seven, first band at eight, home by eleven',
    'The market moves indoors for winter. Same stalls, warmer hands',
    'Volunteer sign-ups for the river clean are open',
    'Tickets for the film night are pay what you can',
    'Our book club reads one short novel a month. Join any time',
    'Kids under twelve come free to the science fair',
    'The pub quiz is back on Thursdays. Teams of six',
    'Bring a plate to the street party on Saturday',
    'Free parking after six for the night market',
    'The annual bake sale raised more than last year. Thank you',
    'New episode: why the budget myth will not die. Listen on Pod Plus',
    'We answered your questions about the move. Full video up now',
    'Behind the scenes of the shoot, no filter, no script',
    'The newsletter goes out Sundays. It is short',
    'This week we talk about pricing with a working freelancer',
    'Reaching a hundred thousand of you this month. Thank you',
    'We got a new mic. Tell us if it sounds different',
    'Part two of the kitchen series drops Thursday',
    'Our guest this week runs a bakery with four locations',
    'The reading list from the episode is in the description',
    'Integrations with the three tools you already use. Set up in minutes',
    'Audit logs are now retained for a year on every plan',
    'The new roles let you give read-only access to your accountant',
    'Reach your customers where they are: SMS, email and chat in one inbox',
    'Uptime last quarter was four nines. The report is public',
    'Pricing is per seat, no minimums, cancel any time',
    'Our SOC 2 report is available on request',
    'Now with a dark theme for the dashboard',
    'Invoices sync to your books nightly',
    'Data lives in the region you choose',
    'Rent a van by the hour from three locations',
    'The night train to the coast runs again from May',
    'Winter tyres fitted while you wait, coffee included',
    'Our hostel has a rooftop and a very opinionated cat',
    'Campsite bookings open for the summer. Pitches with power go first',
    'Electric bikes for hire, helmets included, no deposit',
    'Guided walks every Saturday, rain or shine',
    'The ferry timetable changes on the first of October',
    'Roof racks that fit most cars, fitted in fifteen minutes',
    'Airport parking with a free shuttle every twenty minutes',
]
assert len(CORPUS7) == 130

CORPORA = [("corpus7", CORPUS7), ("corpus6", CORPUS6), ("AEGIS round-5 (my_captions)", AEGIS5_CAPTIONS),
           ("implementer", IMPL_CAPTIONS)]
FP_BOUND = 0.03

# AEGIS round-7 evasion class B (probes/ns_evade7.py), VERBATIM: vowel drop + homophone + ONE lookalike.
AEGIS7_B = ["gt rltch", "gt rlch", "get rltch", "mk rnunny", "mk rnny", "make rnunny", "grnteed retunrs", "gauranteecl rtrns",
            "rsk phree", "rlsk fre", "pssv lncum", "passlv incm", "fnanclal freedum", "flnanchal frdm", "phree rnny", "fre rnunny",
            "kno rlsk", "no rysk", "dbl yr rnunny", "dubble ur rnny", "luze vvght fst", "lose vvait fst", "ovrnlte success", "overnlte sccss",
            "mirakle cr", "mrcl kure", "cllnicly prvn", "kllnically provn", "cloctr recomended", "docter rcmmencled"]
# The round-7 class-B misses (MISS(pass) lines of logs/ns_evade7.txt): 9 of 30.
AEGIS7_B_MISSES = ["mk rnunny", "mk rnny", "grnteed retunrs", "phree rnny", "dbl yr rnunny", "dubble ur rnny",
                   "luze vvght fst", "lose vvait fst", "overnlte sccss"]
# Class C (VERBATIM): the phrase split across two fields, each half respelled.
AEGIS7_C = [("gt", "ritch"), ("mk", "munny"), ("gt", "rch"), ("make", "mny"), ("grnteed", "returnz"), ("rsk", "phree"),
            ("pssv", "incum"), ("phree", "mny"), ("kno", "rsk"), ("get", "rlch"), ("g3t", "r1ch"), ("financial", "freedum"),
            ("lose weight", "fst")]
# Class M (VERBATIM) and the ns_probe7b.py "emoji-for-word" cases.
AEGIS7_M = ["make 💰", "make $$$", "get 💎", "💰 make", "make m💰ney", "get r💲ch", "guaranteed 📈", "risk 🆓", "passive 💵",
            "free 💸", "double your 💰", "make money 💰"]
AEGIS7_EMOJI = ["make 💰", "make 💰💰💰", "get 💎 quick", "passive 💵", "free 💸", "guaranteed 📈", "double your 💰",
                "make $ online", "make $$$ fast", "make 🤑"]

_n = itertools.count()


def _clip_review(registry, rb, **fields):
    from zbc import clip_review

    base = zbc_clip(f"w8_{next(_n)}")
    base["caption"] = "hello #ad"
    base["transcript"] = " This budget myth. Listen on Pod Plus."
    base.update(fields)
    return clip_review.review(clip_review.ClipSubmission.model_validate(base), rb, registry, NOW)


# =====================================================================================
# N7-1 — the JSON shape pre-scan applies to every JSON content type; anything else is 415
# =====================================================================================

BIG = json.dumps({f"k{i}": 1 for i in range(60_000)}).encode()
JSON_TYPES = ["application/json", "application/hal+json", "application/vnd.api+json", "APPLICATION/JSON",
              "Application/Problem+JSON", "application/json; charset=utf-8", "application/ld+json;charset=UTF-8",
              "application/json ; charset=\"utf-8\"", "application/merge-patch+json; q=1",
              "application/+json"]  # (FastAPI reads the last one as JSON too: subtype "+json")
NOT_JSON = ["text/plain", "text/json", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
            "application/jsonx", "application/json+xml", "application/octet-stream", "json", "application/"]


@pytest.mark.parametrize("ct", JSON_TYPES)
def test_n7_1_shape_prescan_applies_to_every_json_content_type(api, ct):
    """A 60k-key body under ANY JSON content type is refused by the shape
    gate before the framework builds 60,001 validation errors."""
    r = api.client.post("/zbc/campaigns/camp_pod_01/rulebooks", content=BIG, headers={
        "Content-Type": ct, "X-Creative-Actor-Token": api.actor_tokens["zbc_rulebook_writer"]})
    assert r.status_code == 422, (ct, r.status_code, r.text[:200])
    assert r.json()["error"] == "PayloadTooManyMembers", (ct, r.text[:200])
    assert len(r.content) < 512


@pytest.mark.parametrize("ct", NOT_JSON)
def test_n7_1_a_body_of_any_other_content_type_is_415(api, ct):
    r = api.client.post("/zbc/campaigns/camp_pod_01/rulebooks", content=b'{"a": 1}', headers={
        "Content-Type": ct, "X-Creative-Actor-Token": api.actor_tokens["zbc_rulebook_writer"]})
    assert r.status_code == 415, (ct, r.status_code, r.text[:200])
    assert r.json()["error"] == "UnsupportedMediaType"


def test_n7_1_a_body_without_a_content_type_is_415_but_an_empty_body_is_not(api):
    r = api.client.post("/zbc/campaigns/camp_pod_01/rulebooks", content=b'{"a": 1}',
                        headers={"X-Creative-Actor-Token": api.actor_tokens["zbc_rulebook_writer"]})
    assert r.status_code == 415, r.text[:200]
    # a body-less POST to a route with no body model is untouched (404: no such work, not 415)
    r = api.client.post("/zbm/work/w_none/rights")
    assert r.status_code == 404, r.text[:200]
    # GET never carries a body and is untouched
    assert api.get("/health").status_code == 200


def test_n7_1_non_json_is_refused_before_the_body_is_read():
    """With a declared non-JSON content type the middleware answers 415
    without a single receive() of the body."""
    import asyncio

    from api import BodyLimit

    async def inner(scope, receive, send):
        raise AssertionError("the app must not be reached")

    async def receive():
        raise AssertionError("the body must not be read")

    sent = []

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/zbc/clips", "raw_path": b"/zbc/clips", "query_string": b"",
             "headers": [(b"content-type", b"text/plain"), (b"content-length", b"10")]}
    asyncio.run(BodyLimit(inner)(scope, receive, send))
    assert sent[0]["status"] == 415
    assert json.loads(sent[1]["body"])["error"] == "UnsupportedMediaType"


def test_n7_1_is_json_content_type():
    from api import is_json_content_type

    for ct in JSON_TYPES:
        assert is_json_content_type(ct.encode()), ct
    for ct in NOT_JSON:
        assert not is_json_content_type(ct.encode()), ct
    assert not is_json_content_type(None)
    assert not is_json_content_type(b"")


def test_n7_1_no_other_content_type_gate_in_the_service():
    """Sweep: the only content-type decision in the service is the shared
    predicate; no source line compares a header to the literal
    `application/json` or splits a content type by hand."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for p in src.rglob("*.py"):
        for n, line in enumerate(p.read_text().splitlines(), 1):
            code = line.split("#")[0]
            if re.search(r"==\s*b?[\"']application/json[\"']|[\"']application/json[\"']\s*==|split\(b?[\"'];", code):
                offenders.append((p.name, n, line.strip()))
    assert offenders == [], offenders


def test_n7_1_is_json_content_type_matches_fastapi_for_random_types():
    """The predicate and FastAPI's own decision agree on 2,000 random
    content-type strings (FastAPI: email.message main type `application`,
    subtype `json` or `*+json`)."""
    import email.message

    from api import is_json_content_type

    def fastapi_would_parse(ct: str) -> bool:
        m = email.message.Message()
        m["content-type"] = ct
        return m.get_content_maintype() == "application" and (m.get_content_subtype() == "json"
                                                               or m.get_content_subtype().endswith("+json"))

    rng = random.Random(1)
    parts = ["application", "Application", "text", "json", "JSON", "+json", "hal", "vnd.api", "/", ";", " ", "=",
             "charset", "utf-8", "\"", "x", "+", "-", "."]
    for _ in range(2000):
        ct = "".join(rng.choice(parts) for _ in range(rng.randint(1, 8)))
        assert is_json_content_type(ct.encode("latin-1")) == fastapi_would_parse(ct), ct


def test_n7_1_heap_is_trimmed_after_a_large_parse(monkeypatch):
    """After a large body has been parsed and no large body is in flight,
    the freed pages are handed back (malloc_trim, off the event loop);
    a small body schedules nothing; while another large body is in
    flight the trim waits."""
    import asyncio

    import api as api_mod
    from api import BodyLimit

    calls = []
    monkeypatch.setattr(api_mod, "_malloc_trim", lambda: calls.append(1))
    monkeypatch.setattr(api_mod, "TRIM_IDLE_S", 0.05)
    gate = asyncio.Event()

    async def inner(scope, receive, send):
        await receive()
        if scope["path"] == "/slow":
            await gate.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = BodyLimit(inner, large=1000)

    def request(path, body):
        msgs = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return msgs.pop(0)

        async def send(msg):
            pass

        scope = {"type": "http", "method": "POST", "path": path, "raw_path": path.encode(), "query_string": b"",
                 "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]}
        return mw(scope, receive, send)

    async def run():
        await request("/small", b'{"a": 1}')
        await asyncio.sleep(0.2)
        assert calls == [], "a small body must not schedule a trim"
        slow = asyncio.ensure_future(request("/slow", b'{"a": "' + b"x" * 2000 + b'"}'))
        await asyncio.sleep(0)
        await request("/big", b'{"a": "' + b"x" * 2000 + b'"}')
        await asyncio.sleep(0.2)
        assert calls == [], "the trim waits while another large body is in flight"
        gate.set()
        await slow
        await asyncio.sleep(0.3)
        assert calls == [1], calls

    asyncio.run(run())


def test_n7_1_the_round7_probe_through_the_api_hal_json_is_bounded(api):
    """probes/cre_ct_bypass7.py, the single-request form: a 60k-key body as
    application/hal+json is refused by the shape gate in milliseconds
    with a small body, exactly like application/json."""
    for ct in ("application/json", "application/hal+json"):
        t0 = time.process_time()  # fix wave 25 (scout A C3; R-HYGIENE L1): CPU of this process (TestClient), not wall
        r = api.client.post("/zbc/campaigns/camp_pod_01/rulebooks", content=BIG, headers={
            "Content-Type": ct, "X-Creative-Actor-Token": api.actor_tokens["zbc_rulebook_writer"]})
        dt = time.process_time() - t0
        assert r.status_code == 422 and r.json()["error"] == "PayloadTooManyMembers" and len(r.content) < 200
        assert dt < 0.5, (ct, dt)


# =====================================================================================
# N7-3 — the rule-id space fits the model limits
# =====================================================================================

def _writer():
    from zbc import rulebook_writer
    from zbc.rulebook_writer import CampaignGoal

    return rulebook_writer, CampaignGoal


def test_n7_3_the_hundredth_never_say_entry_is_a_rule_not_an_error(registry):
    rulebook_writer, CampaignGoal = _writer()
    for n in (99, 100, 150, 1000):
        goal = CampaignGoal.model_validate(zbc_goal(never_say=[f"phrase {i}" for i in range(n)]))
        rb = rulebook_writer.draft(goal, registry, TODAY)
        ids = [r.rule_id for r in rb.rules if r.rule_id.startswith("NS-")]
        assert len(ids) == n and ids[0] == "NS-01" and ids[98] == "NS-99"
        if n >= 100:
            assert ids[99] == "NS-100", ids[95:105]
    goal = CampaignGoal.model_validate(zbc_goal(must_say=[f"say {i}" for i in range(100)]))
    rb = rulebook_writer.draft(goal, registry, TODAY)
    assert sum(r.rule_id.startswith("MS-") for r in rb.rules) == 100


def test_n7_3_through_the_api_never_a_500(api):
    from flows import zbc_rights_on_file

    zbc_rights_on_file(api)
    for n in (100, 150):
        goal = zbc_goal(campaign_id=f"camp_ns{n}", never_say=[f"phrase {i}" for i in range(n)])
        r = api.post(f"/zbc/campaigns/camp_ns{n}/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": goal})
        assert r.status_code == 201, (n, r.status_code, r.text[:300])
        assert sum(x["rule_id"].startswith("NS-") for x in r.json()["rules"]) == n
    goal = zbc_goal(campaign_id="camp_ms100", must_say=[f"say {i}" for i in range(100)])
    r = api.post("/zbc/campaigns/camp_ms100/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": goal})
    assert r.status_code == 201, r.text[:300]


def test_n7_3_rule_id_format_is_backward_compatible():
    from zbc.rulebook import Rule, RuleKind, next_rule_number, parse_rule_number

    # every existing two-digit id is unchanged and parses
    assert Rule(rule_id="NS-07", kind=RuleKind.NEVER_SAY, text="t").rule_id == "NS-07"
    assert parse_rule_number("NS", "NS-07") == 7 and parse_rule_number("NS", "NS-99") == 99
    assert parse_rule_number("NS", "NS-100") == 100 and parse_rule_number("NS", "NS-123456789") == 123456789
    assert parse_rule_number("NS", "MS-07") is None and parse_rule_number("NS", "NS-7") is None
    assert parse_rule_number("NS", "NS-0100") is None  # no leading zeros beyond the two-digit form
    assert next_rule_number("NS", {"NS-07", "NS-99"}) == 100
    assert next_rule_number("NS", {"NS-07", "NS-100", "NS-2000"}) == 2001
    assert next_rule_number("NS", set()) == 1
    for bad in ("NS-7", "NS-0100", "NS-", "NS-1234567890", "ns-01", "NS_01"):
        with pytest.raises(ValueError):
            Rule(rule_id=bad, kind=RuleKind.NEVER_SAY, text="t")
    for good in ("NS-01", "NS-99", "NS-100", "NS-999999999"):
        Rule(rule_id=good, kind=RuleKind.NEVER_SAY, text="t")


def test_n7_3_maximal_legal_rulebook_and_200_full_churn_revisions(registry):
    """The largest goal the model admits, then 200 revisions that replace
    every never-say and must-say entry: no error, ids never reused,
    unchanged rules keep their ids, and the numbering never wedges."""
    rulebook_writer, CampaignGoal = _writer()
    from zbc.rulebook import RuleKind

    def goal(rev: int):
        return CampaignGoal.model_validate(zbc_goal(
            never_say=[f"rev{rev} phrase {i}" for i in range(1000)],
            must_say=[f"rev{rev} say {i}" for i in range(100)],
            angles=[{"name": f"Angle {i}", "description": "d", "keywords": [f"k{j}" for j in range(100)],
                     "hook_lines": [f"hook {j}" for j in range(100)]} for i in range(20)],
            platforms=[{"platform": f"p{i}", "placement": f"pl{i}"} for i in range(48)]
            + [{"platform": "youtube", "placement": "shorts"}, {"platform": "instagram", "placement": "reels"}],
            disclosure_any_of=[f"#ad{i}" for i in range(50)],
            cleared_asset_ids=[f"asset_{i}" for i in range(500)],
            source_asset_ids=[f"src_{i}" for i in range(200)]))

    t0 = time.thread_time()  # fix wave 25 (scout A C3; R-HYGIENE L1): this thread's CPU time, not the wall clock
    rb = rulebook_writer.draft(goal(0), registry, TODAY)
    assert sum(r.kind is RuleKind.NEVER_SAY for r in rb.rules) == 1000
    assert sum(r.kind is RuleKind.MUST_SAY for r in rb.rules) == 100
    stable = {r.rule_id for r in rb.rules if r.kind not in (RuleKind.NEVER_SAY, RuleKind.MUST_SAY)}
    seen: set[str] = set(r.rule_id for r in rb.rules)
    for rev in range(1, 201):
        rb = rulebook_writer.revise(rb, goal(rev), registry, TODAY, rev + 1)
        ids = {r.rule_id for r in rb.rules}
        assert stable <= ids, rev  # unchanged rules keep their ids
        fresh = {r.rule_id for r in rb.rules if r.kind in (RuleKind.NEVER_SAY, RuleKind.MUST_SAY)}
        assert not (fresh & seen), rev  # a retired id is never reused
        assert fresh.isdisjoint(rb.retired_rule_ids)
        seen |= fresh
    ns_max = max(int(r.rule_id[3:]) for r in rb.rules if r.kind is RuleKind.NEVER_SAY)
    assert ns_max == 201 * 1000, ns_max
    assert len(rb.retired_rule_ids) == 200 * 1100
    dt = time.thread_time() - t0
    print(f"\nN7-3 maximal rulebook + 200 full-churn revisions: {dt:.1f}s, NS-{ns_max}, {len(rb.retired_rule_ids)} retired")
    assert dt < 120  # measured 20-30 s: 220,000 retired ids are re-sorted and re-validated on every revision


def test_n7_3_rev_wedge7_probe_no_longer_wedges(registry):
    """probes/rev_wedge7.py: 20 phrases churned 19 times wedged at NS-100."""
    rulebook_writer, CampaignGoal = _writer()
    rb = rulebook_writer.draft(CampaignGoal.model_validate(zbc_goal(never_say=[f"phrase {i} alpha" for i in range(20)])),
                               registry, TODAY)
    for rev in range(1, 20):
        g = CampaignGoal.model_validate(zbc_goal(never_say=[f"rev{rev} phrase {i} alpha" for i in range(20)]))
        rb = rulebook_writer.revise(rb, g, registry, TODAY, rev + 1)
    assert max(int(r.rule_id[3:]) for r in rb.rules if r.rule_id.startswith("NS-")) == 400


def _random_goal(rng: random.Random, big: bool) -> dict:
    alphabet = string.ascii_letters + string.digits + " $€£!?-'#@💰🔥éßо" + "​"

    def phrase():
        t = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
        return t if t.strip() else "x"  # NonEmptyStr strips

    n_ns = rng.randint(0, 1000 if big else 40)
    n_ms = rng.randint(0, 100 if big else 10)
    return zbc_goal(
        never_say=[phrase() if rng.random() < 0.8 else {"phrase": phrase(), "fuzzy": rng.random() < 0.5} for _ in range(n_ns)],
        must_say=[phrase() for _ in range(n_ms)],
        angles=[{"name": phrase(), "description": phrase(), "keywords": [phrase() for _ in range(rng.randint(1, 5))],
                 "hook_lines": [phrase() for _ in range(rng.randint(0, 3))]} for _ in range(rng.randint(1, 20))],
        platforms=[{"platform": rng.choice(["youtube", "instagram", "tiktok", f"p{rng.randint(0, 9)}"]),
                    "placement": rng.choice(["shorts", "reels", "feed", f"pl{rng.randint(0, 9)}"])} for _ in range(rng.randint(1, 8))],
        disclosure_any_of=[phrase() for _ in range(rng.randint(1, 5))],
        min_days_live=rng.randint(1, 365), min_transformation_elements=rng.randint(1, 7),
        min_resolution_height_px=rng.randint(240, 4320),
        campaign_max_length_seconds=rng.choice([None, rng.randint(3, 3600)]))


def test_n7_3_fuzz_no_model_legal_goal_raises(registry):
    """Random model-legal goals (sizes up to the model maxima, symbols,
    emoji, lookalikes, invisibles, duplicates): draft and revise never
    raise, through the model and through the API."""
    rulebook_writer, CampaignGoal = _writer()
    rng = random.Random(8)
    prev = None
    for i in range(60):
        g = CampaignGoal.model_validate(_random_goal(rng, big=i % 10 == 0))
        rb = rulebook_writer.draft(g, registry, TODAY)
        if prev is not None:
            rb2 = rulebook_writer.revise(prev, g, registry, TODAY, prev.version + 1)
            assert rb2.rule_ids().isdisjoint(rb2.retired_rule_ids)
            prev = rb2
        else:
            prev = rb


def test_n7_3_fuzz_through_the_api_never_500(api):
    from flows import zbc_rights_on_file

    zbc_rights_on_file(api)
    rng = random.Random(9)
    for i in range(25):
        g = _random_goal(rng, big=i % 5 == 0)
        g["campaign_id"] = "camp_pod_01"
        r = api.post("/zbc/campaigns/camp_pod_01/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": g})
        assert r.status_code in (201, 409, 422), (r.status_code, r.text[:300])
        assert r.status_code != 500
        if r.status_code == 201:
            r = api.put("/zbc/campaigns/camp_pod_01/rulebooks/1", {"actor_id": "zbc_rulebook_writer", "goal": g})
            assert r.status_code in (200, 409, 422), (r.status_code, r.text[:300])


# =====================================================================================
# N7-4 — a symbol standing for a phrase word
# =====================================================================================

@pytest.mark.parametrize("text", AEGIS7_EMOJI)
def test_n7_4_aegis_emoji_for_word_cases_go_to_a_human(registry, text):
    rb = _rulebook(registry)
    d = _clip_review(registry, rb, caption=text + " #ad")
    assert d.outcome == "human_review", (text, d.outcome, d.human_review_reasons)
    assert any("NS-" in r and "symbol" in r for r in d.human_review_reasons), d.human_review_reasons


def test_n7_4_class_m_of_the_round7_generator(registry):
    rb = _rulebook(registry)
    misses = [t for t in AEGIS7_M if _clip_review(registry, rb, caption=t + " #ad").outcome == "pass"]
    # "💰 make" is the phrase's words in the other order, which no rule catches (documented limitation)
    assert misses == ["💰 make"], misses


def test_n7_4_symbol_lexicon_is_documented_and_applied():
    from shared.text import SYMBOL_LEXICON, symbol_stand_in

    for sym in ("💰", "💵", "💸", "🤑", "💎", "📈", "🚀", "🔥", "🔒", "✅", "$", "€", "£"):
        assert sym in SYMBOL_LEXICON, sym
    for sym, words in SYMBOL_LEXICON.items():
        assert words and all(w.isalpha() and w.islower() for w in words), (sym, words)
    assert "money" in SYMBOL_LEXICON["💰"] and "rich" in SYMBOL_LEXICON["💎"] and "returns" in SYMBOL_LEXICON["📈"]
    assert "free" in SYMBOL_LEXICON["🆓"] and "guaranteed" in SYMBOL_LEXICON["🔒"] and "cure" in SYMBOL_LEXICON["💊"]
    assert symbol_stand_in("make 💰", "make money")
    assert symbol_stand_in("make $$$ fast", "make money")
    assert symbol_stand_in("make €€€ fast", "make money")
    assert symbol_stand_in("get 💎 quick", "get rich quick")
    assert symbol_stand_in("free 💸", "free money")
    assert symbol_stand_in("guaranteed 📈", "guaranteed returns")
    assert symbol_stand_in("miracle 💊", "miracle cure")
    assert symbol_stand_in("🔒 returns", "guaranteed returns")
    assert symbol_stand_in("make💰", "make money")  # no space
    assert symbol_stand_in("make 💰💰💰", "make money")  # a run is one symbol
    assert symbol_stand_in("MAKE 💰", "make money")
    # an emoji the lexicon does not know, OCCUPYING a phrase word's place — between the phrase's words
    # or closing the statement — is a human's call too
    assert symbol_stand_in("get 🍕 quick", "get rich quick")
    assert symbol_stand_in("guaranteed 🎯", "guaranteed returns")
    assert symbol_stand_in("guaranteed 🎯 #ad", "guaranteed returns")
    assert symbol_stand_in("make 🤞\nMyth #1", "make money")  # a line break closes the statement
    # fix wave 9 (AEGIS round 8 M2) CHANGED the next four: wave 8 let an ordinary word after the emoji
    # ("the emoji illustrates that word") and a line break end the rule, so "make 💱 This budget myth" and
    # "make\n💰" passed. ANY symbol in a missing word's place is now a stand-in, whatever follows it, and
    # line breaks are read across (tests/test_fix_wave_9.py measures the cost on emoji captions).
    assert symbol_stand_in("make 🤞 today", "make money")
    assert symbol_stand_in("Get 🎟️ tickets before Friday", "get rich")
    assert symbol_stand_in("Free 🚚 shipping this weekend", "free money")
    assert symbol_stand_in("get 🍕\nquick", "get rich quick")
    # ... but not when the fragment left is only function words
    assert not symbol_stand_in("no 🎯", "no risk")
    assert not symbol_stand_in("your 💰", "double your money")
    assert not symbol_stand_in("make money 💰", "make money")  # the phrase is there (exact, not a stand-in)
    assert not symbol_stand_in("Sold out twice 🎉 Back Friday", "make money")
    assert not symbol_stand_in("New drop 🔥 five colours", "get rich")
    assert not symbol_stand_in("make $20 in a week", "make money")  # a price is a number, not a stand-in
    assert not symbol_stand_in("free $10 credit", "free money")
    assert not symbol_stand_in("made 💰", "make money")  # the other words must be the phrase's words
    assert not symbol_stand_in("cure", "cure")


def test_n7_4_symbols_alone_are_not_a_phrase(registry):
    """At least one content word of the phrase must be written: "$ $",
    "💸 💸" or "🎉 $" say no phrase (the unverified wave-8 draft read "$ $"
    as every money phrase: 7 symbol reasons on the round-7 50 KB symbols
    transcript, ns_cost7b.py)."""
    from shared.text import symbol_stand_in

    for text, phrase in [("$ $", "make money"), ("💸 💸", "free money"), ("🎉 $", "guaranteed returns"),
                         ("💰 🚀 📈", "get rich quick"), ("your 💰", "double your money"), ("costs$ you money€", "make money")]:
        assert symbol_stand_in(text, phrase) is None, (text, phrase)
    rng = random.Random(7)
    words = "the budget myth costs you money every month listen on pod plus garage microphones apps spreadsheet weather outro rant".split()
    " ".join(rng.choice(words) for _ in range(9000))  # the probe's first transcript, drawn first (same stream)
    tr = " ".join(rng.choice(words) + rng.choice("$€1!@") for _ in range(9000))[:49900]
    d = _clip_review(registry, _rulebook(registry), transcript=tr + " This budget myth. Listen on Pod Plus.")
    assert not [r for r in d.human_review_reasons if "symbol standing" in r], d.human_review_reasons


def test_n7_4_decorative_emoji_elsewhere_still_passes(registry):
    rb = _rulebook(registry)
    for text in ["Sold out twice 🎉 Back on the shelf Friday", "New drop 🔥 five colours of the everyday tee",
                 "Doors at seven 🎶 first band at eight", "Three steps, two minutes, one glow ✨",
                 "The candle burns for sixty hours ⏱ We timed it", "Free returns 🚚 on every order this weekend 🎁",
                 "make money 💰"]:
        d = _clip_review(registry, rb, caption=text + " #ad")
        if text == "make money 💰":
            assert d.outcome == "reject"
        else:
            assert d.outcome == "pass", (text, d.outcome, d.human_review_reasons)


# Thirty ordinary captions WITH emoji (the corpora have none), written for this wave: the cost of the
# symbol rule on emoji-heavy creator text. Wave 8 flagged two, both lexicon hits a human should see
# ("Get 💸 back on every referral" ~ get rich, "Make 💰 moves this quarter" ~ make money). Fix wave 9
# (AEGIS round 8 M2) made ANY symbol in a missing word's place a stand-in, whatever follows it: these
# captions were written to START with a never-say phrase's first word followed by an emoji, the worst
# case of that rule, and 18 of 30 now go to a human (the AEGIS round-8 corpus, 40 emoji captions of
# ordinary creator text: 0 — tests/test_fix_wave_9.py).
EMOJI_CAPTIONS = [
    "New drop 🔥 five colours of the everyday tee", "Sold out twice 🎉 Back on the shelf Friday",
    "Get 🎟️ tickets before Friday", "Free 🚚 shipping this weekend", "Make 🎄 memories with the family bundle",
    "Get ready 💪 for the 30-day challenge", "Doors at seven 🎶 first band at eight",
    "Free ☕ with every pastry before nine", "Get 🔥 deals in the app", "Make 🍕 night easy with the sourdough kit",
    "Our candles 🕯️ burn for sixty hours", "Winter skin 🧴 needs a heavier cream", "Free 🎁 with orders over $40",
    "Get 💌 the newsletter every Sunday", "Risk 🧗 assessment for the climb is in the guide",
    "No 🚗 needed, the studio is by the station", "Sale ends midnight 🔥 no code needed",
    "Get 💸 back on every referral", "Guaranteed 🌱 to grow in any light",
    "Make 💰 moves this quarter (finance course)", "Lose ⚖️ the guesswork with our scale",
    "Overnight 🌙 oats, three flavours", "Passive 🎧 listening playlist for study",
    "Financial 📚 literacy course for teens", "Double 🍔 patties on Tuesdays", "Free 💧 refills all summer",
    "Rich 🍫 chocolate, 70 percent cocoa", "Get 🚴 fit this spring with our bike plan",
    "Make 🎨 something this weekend", "Cure 🧀 board for two",
]


def test_n7_4_symbol_rule_on_ordinary_emoji_captions():
    from shared.text import symbol_stand_in

    flagged = [(c, p, symbol_stand_in(c, p)) for c in EMOJI_CAPTIONS for p in NEVER_SAY if symbol_stand_in(c, p)]
    print(f"\nN7-4 symbol stand-in on {len(EMOJI_CAPTIONS)} ordinary emoji captions: {len(flagged)} flagged")
    for f in flagged:
        print("   ", f)
    assert {c for c, _, _ in flagged} == WAVE9_FLAGGED, {c for c, _, _ in flagged} ^ WAVE9_FLAGGED
    # every one is a caption that opens with a never-say phrase's word and puts an emoji in the next word's place
    assert all(c.split()[0].lower() in {w for p in NEVER_SAY for w in p.split()} for c, _, _ in flagged)


WAVE9_FLAGGED = {
    "Get 🎟️ tickets before Friday", "Free 🚚 shipping this weekend", "Make 🎄 memories with the family bundle",
    "Free ☕ with every pastry before nine", "Get 🔥 deals in the app", "Make 🍕 night easy with the sourdough kit",
    "Free 🎁 with orders over $40", "Get 💌 the newsletter every Sunday", "Risk 🧗 assessment for the climb is in the guide",
    "Get 💸 back on every referral", "Guaranteed 🌱 to grow in any light", "Make 💰 moves this quarter (finance course)",
    "Lose ⚖️ the guesswork with our scale", "Passive 🎧 listening playlist for study", "Financial 📚 literacy course for teens",
    "Free 💧 refills all summer", "Get 🚴 fit this spring with our bike plan", "Make 🎨 something this weekend",
}


@pytest.mark.parametrize("name,captions", CORPORA)
def test_n7_4_symbol_rule_false_positives(name, captions):
    from shared.text import symbol_stand_in

    flagged = [(c, p) for c in captions for p in NEVER_SAY if symbol_stand_in(c, p)]
    print(f"\nN7-4 symbol stand-in FP on {name}: {len(flagged)}/{len(captions)}")
    assert len(flagged) / len(captions) <= FP_BOUND, flagged


# =====================================================================================
# N7-5 — every field pair is read across its boundary with every signal
# =====================================================================================

@pytest.mark.parametrize("a,b", AEGIS7_C)
def test_n7_5_respelled_halves_across_any_field_pair_are_at_least_a_humans_call(registry, a, b):
    rb = _rulebook(registry)
    for fields in ({"caption": a + " #ad", "account_bio": b}, {"on_screen_text": a, "transcript": b + " This budget myth. Listen on Pod Plus."},
                   {"caption": a + " #ad", "on_screen_text": b}, {"account_bio": a, "caption": b + " #ad"},
                   {"transcript": "This budget myth. Listen on Pod Plus. " + a, "account_bio": b}):
        d = _clip_review(registry, rb, **fields)
        assert d.outcome != "pass", (fields, d.outcome)


def test_n7_5_a_spread_names_both_fields(registry):
    rb = _rulebook(registry)
    d = _clip_review(registry, rb, caption="gt #ad", account_bio="ritch")
    assert d.outcome == "human_review"
    assert any("spread over caption and account_bio" in r for r in d.human_review_reasons), d.human_review_reasons


def test_n7_5_cross_field_false_positives_on_corpus_pairs(registry):
    """Ordinary captions paired at random (caption + bio, bio + caption,
    on-screen + transcript): the cross-field read alone must not send
    more than 3% to a human."""

    rb = _rulebook(registry)
    rng = random.Random(8)
    pool = CORPUS7 + CORPUS6
    flagged = []
    pairs = [(rng.choice(pool), rng.choice(pool)) for _ in range(300)]
    for a, b in pairs:
        d = _clip_review(registry, rb, caption=a + " #ad", account_bio=b)
        spread = [r for r in d.human_review_reasons if "spread over" in r]
        if spread:
            flagged.append((a, b, spread))
    print(f"\nN7-5 cross-field FP on {len(pairs)} corpus pairs: {len(flagged)} = {len(flagged) / len(pairs):.1%}")
    for f in flagged:
        print("   ", f)
    assert len(flagged) / len(pairs) <= FP_BOUND, flagged


# =====================================================================================
# B — stacked respellings
# =====================================================================================

def test_b_round7_class_b_misses_are_caught(registry):
    rb = _rulebook(registry)
    misses = [t for t in AEGIS7_B_MISSES if _clip_review(registry, rb, caption=t + TAIL).outcome == "pass"]
    assert misses == [], misses


def test_b_round7_class_b_miss_rate(registry):
    rb = _rulebook(registry)
    misses = [t for t in AEGIS7_B if _clip_review(registry, rb, caption=t + TAIL).outcome == "pass"]
    rate = len(misses) / len(AEGIS7_B)
    print(f"\nB class B miss rate: {len(misses)}/{len(AEGIS7_B)} = {rate:.1%} (round 7: 9/30 = 30.0%) {misses}")
    assert rate <= 0.05, misses


def test_b_stacked_signal_two_of_three_within_budget_plus_one_on_the_same_window():
    from shared.text import near_miss, stacked_near_miss

    # "ovrnlte success": 3 visual edits (budget 2), skeleton "ovrnlt scs" 2 edits from "ovrnght scs"
    # with no vowel dropped (budget 2, but the signal proper needs a drop), sound "AFRNLT SKS" one
    # key edit from "AFRNT SKS": no signal alone; two relaxed ones on the same window
    assert stacked_near_miss("ovrnlte success", "overnight success")
    assert near_miss("ovrnlte success", "overnight success", stacked=False) is None
    assert stacked_near_miss("grnteed retrunz", "guaranteed returns")
    # one signal alone, even relaxed, is not enough
    assert not stacked_near_miss("form", "free money")
    assert not stacked_near_miss("no rush", "no risk")
    # ordinary words near the phrase show no respelling: a real word plus a prefix, an inflection
    assert not stacked_near_miss("the target rich environment", "get rich")
    assert not stacked_near_miss("your budget rich in detail", "get rich")
    assert not stacked_near_miss("doctors recommend 150 minutes", "doctor recommended")
    assert not stacked_near_miss("myth money", "make money")
    assert not stacked_near_miss("money money", "make money")
    # a short entry stays exact-only (N3)
    assert not stacked_near_miss("kure", "cure")


def test_b_first_key_letter_table_is_sound():
    """The phonetic pass prunes runs by their first letter (_FIRST_KEY): the
    table must admit every first key letter phonetic_key can produce."""
    from shared.text import _FIRST_KEY, phonetic_key

    rng = random.Random(3)
    alpha = string.ascii_lowercase + "hhhhcccsssppptttgggkkwwx"
    for _ in range(200_000):
        w = "".join(rng.choice(alpha) for _ in range(rng.randint(1, 10)))
        k = phonetic_key(w)
        assert not k or k[0] in _FIRST_KEY[w[0]], (w, k)


def test_b_vis_readings_keep_genuine_pairs_matching():
    """The consonant / phonetic signals read each token as written AND with
    rn/cl/vv contracted: a genuine rn or cl in the phrase ("guaranteed",
    "miracle") keeps matching, and a pair standing for m / d is read."""
    from shared.text import phonetic_near_miss, skeleton_near_miss

    assert skeleton_near_miss("grnteed retunrs", "guaranteed returns")
    assert skeleton_near_miss("mirakle cr", "miracle cure")
    assert skeleton_near_miss("mk rnunny", "make money")
    assert phonetic_near_miss("phree rnny", "free money")
    assert phonetic_near_miss("lose vvait fst", "lose weight fast")


def test_b_skeleton_vowel_drop_evidence_counts_the_token_that_dropped_them():
    """Root cause of "grnteed retunrs" / "overnlte sccss": the token whose
    skeleton equals the word's was skipped before the vowel check."""
    from shared.text import skeleton_near_miss

    assert skeleton_near_miss("grnteed retunrs", "guaranteed returns")
    assert skeleton_near_miss("overnlte sccss", "overnight success")


def test_b_two_consonant_tolerance_needs_a_sound_alike():
    """The skeleton signal tolerates one edit in a two-consonant word
    skeleton ("luze" for "lose": "lz" / "ls") only when the token sounds
    like the word; the unverified wave-8 draft tolerated any one edit, so
    "fragrance free, made in small batches" (corpus7) was "free money"."""
    from shared.text import near_miss, skeleton_near_miss

    assert skeleton_near_miss("luze vvght fst", "lose weight fast")
    assert skeleton_near_miss("Dermatologist tested, fragrance free, made in small batches", "free money") is None
    assert near_miss("Dermatologist tested, fragrance free, made in small batches", "free money") is None


@pytest.mark.parametrize("name,captions", CORPORA)
def test_b_never_say_gate_false_positives_at_most_3_percent(registry, name, captions):
    """Every signal, the stacked and symbol rules included, on each corpus."""
    from shared.text import PhraseMatch, match_phrase, near_miss

    ordinary = [c for c in captions if all(match_phrase(c, p) is PhraseMatch.NONE for p in NEVER_SAY)]
    flagged = [(c, [(p, near_miss(c, p)) for p in NEVER_SAY if near_miss(c, p)]) for c in ordinary]
    flagged = [f for f in flagged if f[1]]
    rate = len(flagged) / len(ordinary)
    print(f"\nB gate FP on {name}: {len(flagged)}/{len(ordinary)} = {rate:.1%}")
    for c, nm in flagged:
        print("   ", repr(c), [(p, h[:50]) for p, h in nm])
    assert rate <= FP_BOUND, flagged
    rb = _rulebook(registry)
    for c in ordinary:
        d = _review(registry, rb, c)
        assert not any(b.rule_id.startswith("NS-") for b in d.broken_rules), (c, d.broken_rules)


# =====================================================================================
# N7-7 — bounded review cost
# =====================================================================================

def _cost_cases():
    rng = random.Random(7)
    words = "the budget myth costs you money every month listen on pod plus garage microphones apps spreadsheet weather outro rant".split()
    many = [f"{a} {b}" for a in words for b in words][:99]
    texts = [("plain 50k", " ".join(rng.choice(words) for _ in range(9000))[:49900]),
             ("symbols 50k", " ".join(rng.choice(words) + rng.choice("$€1!@") for _ in range(9000))[:49900]),
             ("vowel-dropped 50k", " ".join("".join(ch for ch in rng.choice(words) if ch not in "aeiou") or "x" for _ in range(11000))[:49900]),
             ("emoji 50k", " ".join(rng.choice(words) + rng.choice([" 💰", " $$$", " 🔥", ""]) for _ in range(8000))[:49900])]
    # adversarial shapes written for this wave (none is in the round-7 probe): consonant soup, the
    # lookalike pairs everywhere (every view differs), one 50,000-letter "word" (it used to make every
    # field joint 50 KB: 4.5 s), and the phrases themselves run together without vowels
    adv = random.Random(5)
    texts += [("dense consonants 50k", " ".join("".join(adv.choice("bcdfghjklmnpqrstvwxz") for _ in range(12)) for _ in range(4000))[:49900]),
              ("lookalike pairs 50k", " ".join(adv.choice(["rnclvv", "nnuurl", "llcIi", "rnrn", "vvuu"]) + adv.choice("bdgmt") for _ in range(8000))[:49900]),
              ("one 50k token", "".join(adv.choice("bdgtmthcstsmnyvry") for _ in range(49900))),
              ("phrases run together 50k", " ".join("".join(ch for ch in adv.choice(many).replace(" ", "") if ch not in "aeiou") for _ in range(6000))[:49900])]
    return many, texts


def _clear_every_cache():
    """Cold: every per-text and per-phrase cache of the never-say gate."""
    from shared import text
    from zbc import clip_review

    for mod in (text, clip_review):
        for v in list(vars(mod).values()):
            if hasattr(v, "cache_clear"):
                v.cache_clear()
    for name in dir(text):
        v = getattr(text, name)
        if name.startswith("_") and name.endswith(("MEMO", "SPANS", "STARTS")) and hasattr(v, "clear"):
            v.clear()
    text.clear_memos()  # fix wave 9: the per-text memos are per thread (reviews run off the workflow lock)


def test_n7_7_clip_review_worst_case_is_bounded(registry):
    """probes/ns_cost7b.py (99 phrases + 50 KB transcript; 5.9 s in round
    7) plus four adversarial transcripts: CPU time of one review with
    every cache cleared, best of 3 (the machine's noise, not the
    algorithm), at most 1.5 s each; also with the must-say tail removed
    (no phrase is then said exactly, so no pattern is retired early)."""
    from zbc import clip_review

    many, texts = _cost_cases()
    rb = _rulebook(registry, many)
    worst = 0.0
    for label, tr in texts:
        for tail in (" This budget myth. Listen on Pod Plus.", ""):
            sub = clip_review.ClipSubmission.model_validate(zbc_clip("cost", transcript=tr + tail))
            best = min(_cpu(lambda: clip_review.review(sub, rb, registry, NOW)) for _ in range(3))
            print(f"\nN7-7 99 phrases, {label}{'' if tail else ' (no exact phrase)'}: {best * 1000:.0f} ms CPU")
            worst = max(worst, best)
            assert best <= 1.5, (label, tail, best)
    print(f"N7-7 worst: {worst * 1000:.0f} ms CPU")


def _cpu(fn):
    _clear_every_cache()
    t0 = time.process_time()
    fn()
    return time.process_time() - t0


def test_n7_7_field_edges_stop_at_a_giant_token(registry):
    """A field that is one 50,000-letter "word" no longer makes every
    field joint 50 KB; a phrase spread over two fields is still read."""
    from zbc import clip_review

    rb = _rulebook(registry)
    giant = "".join(random.Random(1).choice("bdgtmthcsts") for _ in range(49000))
    sub = clip_review.ClipSubmission.model_validate({**zbc_clip("edge"), "caption": "hello gt #ad",
                                                     "transcript": giant + " This budget myth. Listen on Pod Plus.",
                                                     "account_bio": "ritch"})
    edges = clip_review._field_edges(sub)
    assert all(len(t) <= clip_review.EDGE_TOKEN_CHARS for h, tl in edges.values() for t in h + tl)
    assert edges["transcript"][0] == []  # the head stops at the giant token
    d = clip_review.review(sub, rb, registry, NOW)
    assert any("spread over caption and account_bio" in r for r in d.human_review_reasons), d.human_review_reasons


def test_n7_7_all_suffix_osa_matches_the_reference():
    """`_osa_suffixes` (every window ending at one position, one DP) equals
    `_osa_within` per window."""
    from shared.text import _osa_suffixes, _osa_within

    rng = random.Random(77)
    for _ in range(20000):
        tail = "".join(rng.choice("abcl") for _ in range(rng.randint(0, 14)))
        p = "".join(rng.choice("abcl") for _ in range(rng.randint(1, 9)))
        k = rng.randint(0, 4)
        got = _osa_suffixes(tail, p, k)
        for t in range(len(tail) + 1):
            assert got[t] == _osa_within(tail[len(tail) - t:], p, k), (tail, p, k, t)


def test_n7_7_packed_scan_strict_and_relaxed_budgets_and_live_masks():
    """A pack with (pattern, k, kx): every hit within k is reported, a hit
    within kx only while the pattern's relaxed bit is live, and nothing
    for a pattern switched off — against the reference OSA."""
    from shared.text import _osa_within, _Pack

    def ref(p, S, ST, j):
        return min((0 if ST[s] else 1) + (_osa_within(S[s:j], p, 99) or 0) if _osa_within(S[s:j], p, 99) is not None
                   else 99 for s in range(0, j + 1))

    rng = random.Random(78)
    for trial in range(300):
        n = rng.randint(1, 18)
        S = "".join(rng.choice("abc") for _ in range(n))
        ST = bytes(1 if p == 0 or rng.random() < 0.3 else 0 for p in range(n + 1))
        EN = bytes(1 if p == n or rng.random() < 0.3 else 0 for p in range(n + 1))
        pats = []
        for _ in range(rng.randint(1, 4)):
            p = "".join(rng.choice("abc") for _ in range(rng.randint(1, 6)))
            k = rng.randint(0, 2)
            pats.append((p, k, k + rng.randint(0, 1)))
        pack = _Pack(pats)
        off = {i for i in range(len(pats)) if rng.random() < 0.2}
        no_relax = {i for i in range(len(pats)) if rng.random() < 0.3}
        live = [pack.TOPS, pack.TOPS]
        for i in off:
            live[0] &= ~pack.bit(i)
        for i in no_relax:
            live[1] &= ~pack.bit(i)
        got = {(j, i): d for j, i, d in pack.scan(S, ST, EN, live)}
        for j in range(1, n + 1):
            if not EN[j]:
                continue
            for i, (p, k, kx) in enumerate(pats):
                best = ref(p, S, ST, j)
                want = i not in off and (best <= k or (best <= kx and i not in no_relax))
                assert ((j, i) in got) == want, (trial, S, ST, EN, pats, j, i, best, off, no_relax)
                if want:
                    assert got[(j, i)] == best


def test_n7_7_ascii_fast_paths_equal_the_full_normalisation():
    """`canonical` / `_folded` skip NFKC, the format-character strip, the
    mark strip and the confusable fold for ASCII text: the same result
    as the full path for every ASCII character in every position."""
    from shared import text as T

    def slow_canonical(t):
        t = T._nfkc(t)
        t = T._drop_format(t)
        t = T._strip_marks(t).casefold().translate(T._CONFUSABLE_TABLE)
        return " ".join(T._NON_WORD.sub(" ", t).split())

    def slow_folded(t):
        t = T._drop_format(T._nfkc_words(t))
        t = T._strip_marks(t).replace("\u1e9e", "\ue000").replace("\u00df", "\ue000")
        return t.casefold().translate(T._CONFUSABLE_TABLE).replace("\ue000", "\u00df")

    rng = random.Random(79)
    samples_ = [c for c in map(chr, range(128))] + ["".join(rng.choice([chr(i) for i in range(128)]) for _ in range(40))
                                                    for _ in range(500)]
    for x in samples_:
        for t in (x, f"a{x}b", f"Get {x}RICH{x}{x} now"):
            assert T.canonical(t) == slow_canonical(t), repr(t)
            assert T._folded(t) == slow_folded(t), repr(t)


def test_n7_7_a_token_starting_with_a_digit_still_starts_a_phonetic_run():
    """The run pass of the phonetic signal prunes starts by the first
    letter of the key: a token's digits are not letters (phonetic_key
    ignores them), so "4rizkphree" still starts a run for "risk free"
    (the unverified wave-8 draft pruned it by its first character)."""
    from shared.text import phonetic_near_miss

    assert phonetic_near_miss("4rizkphree now", "risk free") == "4rizkphree"
    assert phonetic_near_miss("_rizk phree", "risk free") == "_rizk phree"
