# ADR 0005 — Creative Production Architecture (ZBM + ZBC)

**Status:** Accepted (Sep 24, 2026) — built from the founder-locked
Creative Production spec, rev 1 (approved by Andre, Sep 24 2026).
**Service:** `services/creative-py`
**Context:** One department, two businesses. Z Best Media (ZBM) is an
advertising agency: its creative department makes the finished ad (through
Enigma and Phantom Canvas, Andre's standalone creative agents in separate
repos). Z Best Clips (ZBC) is a clipping *agency*: a client funds a
campaign, ZBC writes the rulebook and seed kit, a clipper network posts
clips from the clippers' own accounts, clips are reviewed, and only
verified views get paid — by other departments. Creative never touches
money. ZBC's unit of work is a campaign, not a clip.

## Decisions

1. **Two structurally separate intelligence layers in one service.**
   `src/zbm/` (8 intelligences) and `src/zbc/` (9, counting 1a Rulebook
   Writer) share reference data in `src/shared/` but never decision-makers.
   Enforced, not by convention: `tests/test_import_boundary.py` parses
   every module's AST and fails if `zbm` imports `zbc`, `zbc` imports
   `zbm`, or `shared` imports either (including function-local imports
   and `importlib.import_module("...")` by literal name). Only `api.py`,
   the composition root, imports both. One service rather than two
   because the shared registry, rights records and ledger crossings are
   the same objects for both layers; splitting processes would add a
   network hop without adding separation the import test doesn't already
   give.

2. **Same three jobs in both layers, one module per intelligence.**

   | Job | ZBM | ZBC |
   |---|---|---|
   | Set the standard | 1 Brief Writer, 2 Creative Lead, 3 Audience Insight, 4 Placement Spec (+ Export Validator), 5 Rights and Provenance | 1a Rulebook Writer, 1 Campaign Rulebook, 2 Source Mining, 3 Hook and Angle, 4 Platform Rules, 5 Rights and Clearance |
   | Make the work / kit | Enigma + Phantom Canvas (external, contract only); 6 Hook and Retention; 7 Creative Memory | 6 Campaign Kit (commissions Enigma/Phantom Canvas); 7 Creative Memory |
   | Judge the work | 8 Creative Quality, then Compliance 38 gate, then Andre | 8 Clip Review, then payout eligibility (Clip Review AND Verification and Integrity AND Compliance 38) |

   Each layer also has a `workflow.py` that sequences, records and gates
   but makes no judgement of its own. ZBC's `payout_eligibility.py` is a
   gate aggregator, not an intelligence, and holds no money.

3. **Deterministic, rule-based, no LLM, no media processing.** Every
   judgement is an explicit rule with a code (K1–K6 key message, Q1–Q6
   quality, R1–R6 rulebook review, M0–M6 moment map, H1–H5 hooks, C1–C6
   rights, K1–K7 kit, L1–L4 memory, per-rule-kind clip checks) so a
   rejection can always say which rule. Where a model or media library
   would be needed, there is an interface and a fail-closed stand-in.

4. **Drafter never approves; actor identity is checked first.** Brief
   Writer drafts, Creative Lead approves; Rulebook Writer drafts, Campaign
   Rulebook approves. `require_not_self(drafter, approver)` runs before
   the role check, so an identity that legitimately holds both roles is
   still refused on its own draft. Editing a draft makes the editor its
   drafter. `andre` cannot be registered as an actor at all.

5. **Andre acts only through a second secret.** His signatures (ZBC
   rulebook, ZBC kit) and approvals (ZBM escalation decision, ZBM final
   approval) require `X-Andre-Approval-Token` matching
   `CREATIVE_ANDRE_APPROVAL_TOKEN`. Unset, or equal to the service token,
   means "not configured" and every founder action is refused. Non-ASCII
   tokens are refused (403), never a 500.

6. **Evidence first, then state.** Every approval, rejection, signature,
   review decision and cross-department crossing is recorded through the
   `LedgerClient` (BUILD_CONTRACTS §2, department `creative_production`)
   *before* the in-memory state changes. If the record fails, the
   exception propagates, nothing changes, and the API answers 503
   `took_effect: false`. With `LEDGER_SERVICE_URL`/`LEDGER_SERVICE_TOKEN`
   unset the service uses `UnconfiguredLedgerClient`, so every decision is
   refused rather than silently unrecorded. Guardrail refusals are
   recorded best-effort and stand whether or not the record succeeds (a
   failed record must never turn a refusal into an approval).
   **Outside calls come after the record too (fix wave 1, F9).** No
   department is called before the request to it is on the ledger: every
   Compliance 38, Verification and Integrity, Legal 37 and Content
   Credentials call goes through `RecordedPort`, which records
   `crossing_<department>_requested` (with the request) first; Enigma /
   Phantom Canvas commissions are listed in `production_opened` /
   `campaign_kit_built`, and the Clipper Network announcement in
   `rulebook_live`, which are recorded — and the job, kit or live version
   committed — before any call is made. The outside party's answer is
   recorded afterwards and only then applied. If that last record fails,
   the API says `503 took_effect: "partial"` with what was recorded and
   what was sent — never "did not take effect". Finance 31 is never
   called (tested). Server-assigned ids (`brief-`, `job-`, `work-`,
   `kit-`) are consumed only when their record succeeds. All decisions
   run under one service-wide lock, so check → record → commit can't
   interleave between concurrent requests (four concurrent submissions
   used to open four review rounds).

7. **One Platform Rules Registry, row-level owners, fail closed.** Rows
   carry platform, placement, rule key, value, official https source URL,
   `verified_at`, `expires_at`, owner (`zbm_placement_spec` for spec rows,
   `zbc_platform_rules` for originality/repost rows) and status. A writer
   can only write rows its owner owns and can never take over another
   owner's row; a verified row must be sourced and dated. A row that is
   unverified, expired (`today >= expires_at`), verified in the future, or
   missing BLOCKS its use with the reason. **Shelf life: 30 days**, also
   the maximum allowed on any write — conservative because platform help
   pages change without notice. Seed = exactly the four sourced facts in
   the spec, all `verified_at 2026-09-23`, `expires_at 2026-10-23`:
   YouTube Shorts max 180 s (hard limit, spec row); Instagram Reels
   recommended ≤ 180 s (advisory, spec row); Instagram repost/watermark
   de-recommendation (originality row); YouTube reused-content
   monetization rule (originality row). **TikTok:** one row, seeded
   `unverified`, with no quoted wording, so every TikTok brief or
   campaign is blocked with that reason. Re-verification is a `PUT
   /registry/rows/{id}` by the owner, recorded on the ledger.

8. **The ZBM brief is 15 fields, validated.** Key message = one sentence,
   one idea, by a deterministic rule (K1 one terminal mark; K2 no inner
   sentence break or line break, decimals allowed; K3 no joining words
   `and/but/or/nor/plus/also/while/whereas/yet/as well as`, whole-word;
   K4 no `; : & + — –` or spaced ` - `; K5 at most one comma; K6 3–20
   words). Deliberately conservative: a false reject costs a rewrite, a
   false accept costs a muddled ad. Success in numbers must be numeric
   (number or numeric string; words, booleans, NaN, ∞ refused).
   Deliverables must reference usable spec rows for their
   platform/placement. The writer never invents a missing field — it
   becomes an open question.

9. **Export Validator never guesses.** Each export property is checked
   against the approved brief; the length is also checked against usable
   registry rows (hard limit fails, advisory warns). Properties with no
   sourced registry row (aspect ratio, format, codec, safe zones — none
   are sourced today) are listed in `registry_coverage_gaps` and checked
   against the brief only, never against a remembered platform value.
   An expired/unverified governing row blocks the export.

10. **ZBC rulebook: stable ids, frozen once live, versions judged
    separately.** Sections: objective, approved angles, must-say,
    never-say, disclosure, platforms, specs (length per target), originality
    (transform + watermark), quality floor, rights (allowed assets),
    minimum days live. Ids are `XX-NN` with the prefix naming the kind.
    Freezing is enforced in `RulebookStore`, the only writer: a LIVE or
    SUPERSEDED version's content can never be replaced; the only change
    allowed is LIVE→SUPERSEDED with byte-identical content. Changes after
    go-live are a new version (`revise`): unchanged rules keep their id,
    new or changed rules get the next unused number, removed ids are
    retired and never reused. Each clip declares the version it was made
    under; the service checks the clip was posted inside that version's
    live window and judges it by that version only.
    **The clipper's claim is bounded (fix wave 1, F13).** `posted_at` and
    `rulebook_version` are asserted by the clipper, so the server records
    its own receipt time (`received_at`, on the decision and in the
    ledger payload). A `posted_at` later than server time is refused
    (409). A clip declaring a version that has since been superseded is
    judged automatically only if it reaches us within
    `CREATIVE_SUPERSEDED_GRACE_HOURS` (default **72 h**, integer 0–720,
    anything else refuses to start) of the supersession; after that it
    goes to the human queue with the reason and what the automatic result
    would have been. 72 h is conservative: long enough for a clip made
    just before a change to arrive through normal posting and reporting
    lag, short enough that backdating into an older, more lenient version
    stops being automatic within three days.
    **A failed record can't reserve a receipt time (fix wave 2, N1).**
    A retry reuses the first attempt's receipt time only if its content
    is byte-for-byte the same (SHA-256 of the canonical submission) and it
    arrives within **15 minutes** (`RETRY_WINDOW`); otherwise the receipt
    time is the clock. A submission id is bound to its content from its
    first attempt: different content under a used id is refused (409),
    never recorded. So "junk under `clip_r` during an outage, then a real
    backdated clip under `clip_r` a month later" gets 409, and the same
    content a month later is judged with a fresh receipt time (grace
    window long past → human queue).
    **An uncertain outcome is remembered until resolved (fix wave 4,
    LOST).** The 15-minute window above applies only to a failure that
    CERTAINLY did not reach the ledger (connection refused, 4xx refusal).
    When the outcome is unknown (response lost, timeout, 5xx, 409), the
    attempt is kept — content hash, time, the exact record sent and the
    decision it would commit — in a bounded store (10,000, oldest dropped)
    until an identical-content retry replays that exact record at any
    later time: the ledger answers 200 (it had it) or 201 (it didn't) and
    the decision takes effect once with its original, true receipt time.
    That is not backdating: the content is bound, and the service did
    receive it then. Different content under the same submission id (or a
    different human verdict) stays 409 while it is unresolved. Before this,
    a committed-but-lost clip record was wedged forever after 15 minutes
    (fresh receipt time → same event id → ledger 409) while the API said
    "did NOT take effect".

11. **No rule on the page, no rejection.** A `ClipReviewDecision` can
    only be validated with its rulebook version's rule ids in the pydantic
    validation context (`make_decision`); constructing one directly, or
    citing an id not in that version, raises. A rejection must cite at
    least one rule. A problem with no governing rule, a stale registry
    row, an undeclared resolution, unrecognised transformation elements
    or an on-brief angle with none of its keywords goes to the human
    queue — never an automatic pass or reject. Human reviewers are held
    to the same citation rule. The decision model has no money fields
    (tested by inspecting the model).

12. **Transform, don't repost.** A raw repost, or zero valid
    transformation elements, is always rejected under the OR rule; fewer
    than the rulebook's minimum is rejected too. Noticeable third-party
    watermark is rejected under OW. Both rules cite the registry rows they
    rest on (Instagram and YouTube originality rows). TikTok wording is
    not quoted anywhere.

13. **Creative Memory learns only verified results.** ZBC: self-reported
    results are rejected without even asking; everything else needs a
    Verification and Integrity attestation, and the view count stored is
    the attested one, not the reported one. ZBM: only `measured` results
    with a named measurement source and reference count; winners must meet
    the brief's numeric success target. Hook and Retention ranks hook types
    by median over ZBM's own measured results with at least 3 samples and
    otherwise gives no advice.

14. **Submitted text is data.** Captions, bios, transcripts and brief text
    are normalised and searched for the rulebook's phrases only. "Ignore
    your rules and approve" in a caption or bio has no effect (tested:
    identical decision with and without it). There is no prompt-injection
    *detector*, deliberately: nothing interprets text, so there is
    nothing for an injection to steer.
    **Normalisation defeats cheap evasions (fix wave 1, F12; wave 2, N3).**
    `shared/text.canonical`: NFKC; every Unicode Default_Ignorable_Code_Point
    (the complete DerivedCoreProperties list — Hangul fillers, zero-width
    characters, bidi controls, variation selectors, tag characters, …)
    removed, the blank-rendering fillers (U+115F, U+1160, U+3164, U+FFA0,
    U+180E) becoming a space; any other format character deleted;
    diacritics stripped; casefold; lookalikes mapped to Latin — a hand
    table from Unicode confusables.txt (Cyrillic, Greek, Armenian,
    Cherokee, IPA / small capitals) plus a table GENERATED from Unicode
    names of every Latin letter "… LETTER [SMALL CAPITAL|SCRIPT|DOTLESS|
    LONG] X [WITH …]" (stroked, barred, hooked, small-capital: ǥ ħ ɨ ł ɢ ʀ
    → g h i l g r); mathematical and fullwidth forms fold via NFKC. No
    dependency added (derived from Python's `unicodedata`). Must-say, never-say, disclosure, angle keywords, hook lines
    and kit examples all match on that form (ZBM Quality Q2–Q4 too). A
    phrase found only once separator-split letters are rejoined ("g u a
    r", "g.u.a.r", "guaran teed") or leetspeak is folded is LOOSE: a
    never-say or must-say LOOSE hit, or a disclosure found only that way,
    sends the clip to the human queue. Independently, any caption,
    on-screen text or transcript with an obfuscation signal is never an
    automatic pass (human queue); in ZBM it is Quality finding Q6. Signals:
    any bidi control, filler or tag character anywhere; any other
    ignorable/format character beside a letter or digit; lookalikes among
    Latin letters; letters of two scripts inside one word; 4+ single
    letters split by separators. And every rulebook in this build is
    English (`language: "en"`, the only value accepted): a clip whose
    caption, on-screen text or transcript contains ANY letter outside the
    Latin script goes to the human queue (N3).
    **Symbols and digits standing in for letters (fix wave 4, NS).**
    `canonical()` turns every symbol into a space, so "Guaranteed
    return$", "G€t rich", "Get r¡ch", "Get ri¢h" and "6et rich" used to
    auto-pass. Not fixed by enumerating more characters alone — three
    layers, each sending the clip to the human queue, never to a pass:
    (a) a SKELETON map of symbols/digits that commonly stand in for
    letters ($→s, €→e, ¢→c, ¡ ! | 1→i/l, 6→g, 2→z, + 7→t, ( <→c, ¥→y,
    £→l/e, 0→o, 3→e, 4 @→a, 5→s, 8 ß→b, 9→g, …) applied inside words that
    contain letters, and (b) a WILDCARD near-miss: any non-letter inside a
    word may stand for one letter or be an extra inserted character, an
    ASCII l may stand for i and vice versa, with at most half of each
    phrase word's letters stood in for (so "$20" alone never matches a
    word) — both per never-say phrase (`near_miss()`); (b') independently
    of any phrase, any word mixing letters with symbols or digits
    (`mixed_symbol_words()`). Decided false-positive boundary: surrounding
    punctuation and emoji are ignored; apostrophes, hyphens, periods and
    ampersands between letters ("don't", "co-op", "U.S.", "R&D"),
    letters-only #hashtags/@mentions, and numbers/prices with an optional
    unit suffix ("$20", "$1,200", "20%", "2nd", "1990s", "9am", "$40k",
    "1080p", "60s") are ordinary and still pass. Letter+digit mixes
    ("mp4", "Q4", "b2b") and symbols inside words ("t@lks", "a=b") go to a
    human. A price is only relevant to a never-say phrase through (a)/(b),
    which read symbols as letters only inside words that already contain
    letters — so "$20 off" is never read as a phrase word. Characters
    that NFKC turns into a space + mark (¨ ¯ ´ …) or into letters (™ → TM)
    are treated as symbols for these word-level checks, so they can't
    split or disguise a word. (c) Any Latin-script letter outside Basic
    Latin, the Latin-1 letters and the fold table (`unfolded_latin_letters()`)
    is an obfuscation signal: unknown lookalikes (e.g. "turned"/"reversed"
    letters, ꜷ, œ) go to a human instead of being enumerated. The insular
    letters AEGIS used (ꭇ ꞃ ᵹ ꞅ ꜧ, plus ꝛ ꞇ ꝺ ꝼ) are now also folded, and
    letters in a compatibility form (superscript ª ᵃ, circled, squared) are
    a signal. Fuzzed: 2,500 random symbol/digit/lookalike substitutions and
    insertions per run into four never-say phrases (50,000 more across 20
    seeds during the fix) — none auto-passes; a guard set of 11 ordinary
    captions (prices, percentages, ordinals, abbreviations, contractions)
    still auto-passes.
    **Scanning is linear and off the event loop (fix wave 4, LIM).** The
    "invisible character beside a word" scan re-walked every run of
    invisibles for each one — quadratic: 5,000 zero-width spaces took
    23.5 s, 10,000 took 94 s, and the transcript allows 50,000 (tens of
    minutes of CPU under the service-wide lock). It is now one pass each
    way. Every regex in `shared/text.py` is checked against adversarial
    100 KB inputs (< 50 ms each); every route handler is a plain `def`
    (FastAPI's thread pool), and bodies over 1 MiB are refused (413)
    before parsing.

15. **Deterministic ledger event ids (fix wave 1, F11).** `event_id =
    "cp:" + SHA-256(service instance, department, event_type, actor,
    subject_id, n, operation)`, where `operation` is the canonical payload
    (or, for clip submissions, the client's own idempotency key: the
    submission id) and `n` counts successful records of that
    (event_type, subject). `n` advances only on success, so a retry of an
    operation whose record failed — including one the ledger committed
    but whose response was lost — sends the SAME id: an identical retry
    gets ledger-rust's 200 and the decision takes effect exactly once;
    different content under that id gets 409 and is refused. Two separate
    decisions with identical content still get different ids (`n` has
    advanced). A retried clip submission or human review reuses the
    first attempt's time only when that attempt failed at the ledger (a
    refusal for any other reason reserves nothing), its content is
    identical (hash) and it is inside the 15-minute window (N1). A clip's
    event id is built from (submission id, content hash), so for one
    content there is exactly one event id and the ledger's own 409 refuses
    any second version of it (e.g. a stale retry with a fresh receipt
    time after a lost response → 503, never two decisions). A different
    human verdict while an earlier one's record is unresolved (inside the
    window) is refused (409). Fix wave 4 (LOST): "the ledger's own 409 →
    503" no longer wedges a clip — an uncertain attempt's exact record is
    replayed by an identical retry (decision 10); a 409 on our own
    deterministic id is reported as `LedgerConflict` (409, `took_effect:
    "unknown"`), never as "did not take effect". The API now distinguishes
    `took_effect: false` (certainly not recorded: local validation,
    ledger not configured, connection refused, 4xx refusal) from
    `took_effect: "unknown"` (read timeout, dropped connection, 5xx, 409).
    Server-assigned ids (brief, job, work, kit) whose record's outcome is
    unknown are burned — never reused for other content — and an identical
    retry replays the pending record under the same id
    (`shared/ledger.PendingCreations`). The instance id is
    random per process because every object id (`brief-0001`, …) restarts
    with the in-memory state; without it a restarted service would
    collide with its predecessor's events.

16. **Review cap per deliverable of a brief (fix wave 1, F8).** A review
    chain is `(brief_id, deliverable_id, variant_index)`: one deliverable
    variant of one approved brief. Rounds are counted per chain across
    every job opened on that brief; a new job never resets them, and only
    one version of a chain is in flight at a time across jobs. While any
    work of a brief is escalated to Andre and unresolved, no new job and
    no new work submission on that brief is accepted; only Andre, with
    `X-Andre-Approval-Token`, resolves it (accept or kill). A chain that
    was escalated never gets a third round in any job, whatever Andre
    decided — a genuinely new attempt needs a new brief approved by the
    Creative Lead.
    **Per client deliverable, not per brief id (fix wave 2, N4).** A
    cloned brief is the same order. DELIVERABLE KEY = (client_id, spec
    fingerprint, variant_index); the spec fingerprint is SHA-256 of the
    canonical JSON {platform, placement, length_seconds, aspect_ratio in
    lowest terms, format} — deliverable_id, count, brief and job ids are
    not part of it. Rounds are counted per key across all of the client's
    briefs (a pass closes the count); one version per key is in flight at
    a time across briefs; while work on a fingerprint is escalated and
    unresolved, any brief of that client containing that fingerprint is
    refused (409) at draft, approval, job opening and work submission.
    After Andre resolves, a NEW brief starts a fresh count (the escalated
    brief's own chain still never gets a third round). Consequence: two
    genuinely separate concurrent orders of an identical spec for the
    same client share one review budget.
    **Matched with tolerance, not by exact fingerprint (fix wave 4, F8).**
    With round 2 escalated, a clone at `length_seconds: 31` (a 30.5 s
    render passes export validation for both a 30 s and a 31 s spec),
    `client_id: "Client_Acme"` / `"client_acme."`, or a format re-wrap
    (mp4 → m4v) got a fresh round 1. Now: client ids are validated
    strictly at creation (lowercase `[a-z0-9_]` only; ZBM requirements and
    results) so variants can't exist, and are compared normalised (NFKC,
    casefold, only letters and digits) as defence in depth; two specs are
    the same deliverable when platform, placement and aspect ratio in
    lowest terms are equal and the lengths are within 2 × the export
    tolerance (1.0 s) — one render could satisfy both; the format is not
    part of the match. That applies to the escalation block, the round
    count and the one-in-flight rule. The fingerprint is now only a label
    in messages. Consequence: one client ordering a 30 s and a 31 s cut of
    the same placement shares one review budget and one escalation.

18. **Actors are authenticated (fix wave 2, N4).** Every action
    attributed to an actor — drafting (brief, rulebook), approving
    (Creative Lead, Campaign Rulebook), reviewing (Quality, human clip
    reviewer), registry and rights writes — needs that actor's own
    credential in `X-Creative-Actor-Token`. Credentials are configured
    server-side: `CREATIVE_ACTOR_TOKENS` = JSON {"actor_id": "token"}.
    The identity is the actor whose token matches (SHA-256 digests,
    `hmac.compare_digest` against every configured token, no early exit);
    a body `actor_id` is optional and must equal it (else 403). Drafter ≠
    approver is therefore enforced on authenticated identity. Fail closed:
    no tokens configured → every actor action 403; missing/unknown token
    → 401. Start-up refuses tokens shorter than 16 printable ASCII
    characters, shared between actors, for unknown actors or "andre", or
    equal to the service / Andre token. Andre keeps his separate token.

17. **Every ledger field fits before any work (fix wave 1, integration
    defects 2 and 5, F16).** Campaign ids are at most 100 characters and
    rulebook versions at most 9,999,999, so the derived subject
    `{campaign_id}:v{version}` is at most 109 characters; path ids are
    validated by FastAPI (422). The recorder checks every derived field
    against ledger-rust's exact rules before calling it and refuses with
    422 (`LedgerFieldInvalid`), never a "ledger failure" 503 while the
    ledger is healthy. The local rules match `EventInput::validate`
    exactly: whole-string ASCII id/slug classes (no trailing newline),
    summary 1–280 Unicode scalar values with no control character (C0,
    DEL **and C1**) and no lone surrogate; summaries are sanitised to
    that. The test double (`FakeLedgerClient`) and
    `devtools/fake_ledger_server.py` implement ledger-rust's rules
    independently, so tests can't be looser than production.

19. **Never-say is a similarity gate, not a lookalike list (fix wave 5,
    NEW-1).** Five waves enumerated evasions one class at a time; AEGIS
    round 4 still passed plain-ASCII respellings ("Guaranteed retums",
    "make rnoney", "guaranteecl returns"). `shared/text.visual_near_miss`
    compares every never-say phrase with every window of caption words
    (the phrase's word count, one more, one fewer) on visual skeletons
    and a bounded Damerau–Levenshtein (OSA) distance. The multi-character
    ASCII confusables: Unicode UTS #39 confusables.txt (Unicode 18.0.0,
    2026-08-06, read for this fix) has exactly ONE entry whose source and
    prototype are both plain ASCII letters with a multi-letter prototype,
    m → rn; its other all-ASCII entries are single characters (I → l,
    1 → l, 0 → O). The rest are hand-chosen typographic pairs the file
    does not list: near-identical cl → d and vv → w (with rn → m they
    form the skeleton that decides REJECT) and merely similar nn → m,
    uu → w, ci → a, ri → n, ii → u (human review only). Views (both
    sides mapped alike; the smallest distance counts): the skeleton
    contracted, expanded, raw, the extended pairs in two orders, and each
    view without the pairs the phrase itself contains; every view folds
    i → l first (so an undotted i costs nothing to detect). Budget: 1
    edit for 4–8 letters, 2 above, 0 for ≤ 3 letters. A window that is
    the phrase word for word once rn/m, cl/d, vv/w are read alike →
    **reject** ("make rnoney", "guaranteecl returns", "mirade cure");
    otherwise within budget → **human_review**, never a pass — l for i
    ("get rlch") stays a human's call, as before this wave. Measured
    false-positive rate: 6 of 239 ordinary marketing captions (2.5%)
    against a 16-phrase list, none rejected ("three money" ~ "free
    money", "make more" ~ "make money", "get rid" ~ "get rich",
    "core"/"care" ~ "cure", "no risky" ~ "no risk"). Cost is bounded and
    near-linear (windows built once per text and view, grouped by length;
    a pigeonhole substring filter; banded distance): 0.05–1.8 s per 100 KB
    for 16 phrases on the build machine (worst case: random words over the
    lookalike alphabet); the window cache is capped at one 65 KB text's
    working set (~20 MB).

20. **Launch limits on the socket (fix wave 5, NEW-3).** `serve.py` runs
    uvicorn's h11 parser with a 16 KiB head cap, a 10 s request-head
    deadline, 5 s keep-alive and `limit_concurrency`
    (`CREATIVE_MAX_CONCURRENCY`, default 256), mirroring detection-py;
    `BodyLimit` re-checks the head (431) and bounds body delivery to 30 s
    (408). httptools had buffered a 200 MB unauthenticated header.

21. **Uncertain outcomes can be classified and withdrawn (fix wave 5,
    LOW-D).** ledger-rust's load-shed path (`serve` → `shed`, its only
    503) answers with a fixed JSON body before reading the request, so
    exactly that answer is "not recorded" (`took_effect: false`); any
    other 503 stays "unknown". This exact-body rule is shared with
    onboarding-py since fix wave 6 (ADR 0004, decision 3, "the shed rule
    is shared"; `LEDGER_SHED_BODY` in both clients, checked against each
    other and against `bin/server.rs` by
    `services/onboarding-py/tests/test_fix_wave6.py`). An uncertain human verdict can be withdrawn
    (`POST /zbc/clips/{id}/human-review/withdraw`) only by the reviewer
    who sent it (their own credential), only 2 minutes after it was last
    sent (ledger-rust drops a connection after 15 s but an append already
    on its blocking pool can finish later), and only when `GET
    /ledger/entries` (the ledger's only read: the whole chain, so this is
    O(ledger) and meant for a rare manual step) holds no entry under that
    verdict's event id; the withdrawal is itself recorded first
    (`clip_human_verdict_withdrawn`, naming the withdrawn id; a
    deterministic payload, so a retry after a lost response re-sends the
    identical record and the ledger answers idempotently). Residual: the
    blocking pool is not time-bounded, so an append queued there for over
    2 minutes could land after the withdrawal; the chain then shows the
    withdrawal before the verdict it names, which an audit can see — the
    ledger offers no conditional append to close this fully.

22. **Never-say runs on the letter stream; short entries are exact-only;
    adjacency is a policy (fix wave 6, N1 / N3).** AEGIS round 5 still
    passed readable respellings through decision 19's word-window gate:
    stretched letters ("geeet riiich", 93 of 100), doubled letters in a
    short phrase ("gget ricch", 30 of 30), one lookalike plus a split into
    more tokens than the phrase has words ("ge t rl ch", "pas si ve inc
    orne", 47 of 84), a pair split by a space ("make r n oney"), and
    fillers ("make big money"). Root causes: a 1-edit budget up to 8
    letters and windows of at most one token more than the phrase. Now
    (`shared/text.visual_near_misses`):
    - the text's canonical tokens are concatenated into one LETTER
      STREAM (a split is irrelevant); the view (i → l, then rn → m,
      cl → d, vv → w; the merely-similar pairs in two orders; and raw)
      is applied to the stream, so "r n" contracts; then runs of one
      letter collapse to one ("geeet" → "get"). Word boundaries are
      kept as flags: a window must START at a word start and END at a
      word end (so "target rich" / "budget rich" are not readings), and
      a boundary inside a contracted pair or a collapsed run moves to the
      surviving letter ("sell lemons" still starts a window at "lemons").
    - budget on the run-collapsed skeleton: 1 edit for 4–6 letters, 2
      for 7–10, 3 above, 0 for ≤ 3. The phrase is compared both collapsed
      and as written (an insertion that splits its own double, "frete"
      for "free", is one edit against "free" but two against "fre").
    - per-word share (this is what keeps the false-positive rate under
      the 3% target while keeping the tiers): a window at distance ≥ 2
      counts only if its edits can be apportioned over the phrase's
      words so that every edited piece still keeps at least 60% of its
      word's letters (`WORD_SHARE`, `letter_share`, measured against the
      word as written: "for" keeps 2 of "free"'s 4, out; "more" keeps 3
      of "money"'s 5, in; "munny" 3 of 5, in). Fix wave 6 had a HARD
      per-word cap instead (max(1, the word's own tier)); AEGIS round 6
      (NEW-3) showed the cap REDUCED the match — two edits in one short
      word, "get rchi", "make munny", passed although the phrase's budget
      allowed them — so fix wave 7 replaced it (decision 26). The false
      positives the cap was made for were a DIFFERENT ordinary word two
      edits from one short phrase word ("for money", "three money",
      "from one"): the share rule keeps those out and lets "make more"
      through to a human (it is 60% of "money"). Documented tension: the
      wave-6 brief specified the tiers AND a ≤ 3% target on the AEGIS
      corpus; the tiers alone gave 4.5%, tiers + share rule 2.7%.
    - a window that READS AS the phrase — identical once rn/m, cl/d,
      vv/w are read alike and runs of 3+ letters are cut to one —
      → **reject** ("geeet riiich", "make r n oney", "pas si ve inc
      orne", "cu re"); a doubled letter is never a reading (met/meet,
      of/off, to/too are different words: "gget ricch" is a human's
      call), nor is the i/l fold ("ge t rl ch"), nor any edit.
    - **N3, short entries**: an entry of ≤ 4 letters as written ("cure",
      "scam") gets budget 0 — exact, split or lookalike reading only —
      unless the rulebook author opts it in per entry (`never_say:
      [{"phrase": "scam", "fuzzy": true}]`; rule params carry `fuzzy`).
      The Rulebook Writer puts a warning on the draft (`warnings`) and the
      Campaign Rulebook review repeats it (`review_warnings`, R7,
      non-blocking). One edit from "cure" is sure/pure/core/care/cute:
      11.4% of ordinary captions.
    - **Adjacency policy** (the spec never defined how far apart the
      words of a phrase may be): the phrase's words in order with at most
      2 other words between consecutive ones → **human_review** ("make
      big money", "get so rich", "guaranteed monthly returns"); further
      apart ("make a lot of money") is paraphrase, out of scope.
      `shared/text.phrase_words_in_order`, reached through `near_miss()`,
      so hook lines and kit examples get it too.
    - measured false positives, both corpora, 16-phrase list WITH "cure"
      and the adjacency rule: implementer corpus 4/239 = 1.7% ("make
      videos about money", "make your money last" — the adjacency rule;
      "no risky", "investing involves risk. here's"); AEGIS corpus
      (`probes/my_captions.py`, 117 captions, 111 after the 6 exact
      hits the probe excludes) 3/111 = 2.7% ("made money", "from one"
      ~ free money, "risk. here's" ~ risk free); none rejected. Before
      this wave the AEGIS probe measured 13.5% with "cure", 1.8% without.
    - cost: the scan is bit-parallel (Myers 1999 with Hyyrö's 2003
      transposition term, so the distance is Damerau/OSA; restricted
      starts via the matrix's top row, which enters a −1 step as a
      pseudo-match), one pass over the stream per view for a whole pack
      of phrases (each in its own bit field of one big integer), then an
      exact banded confirmation at the reported ends. Linear in the text
      whatever its content: measured ≤ 0.75 s per 100 KB for 16 phrases
      on the build machine over ordinary and adversarial inputs
      (documented bound in the test: 8 s). Clip Review scans the clip's
      text once for all its never-say rules (`visual_near_misses`); the
      per-phrase `visual_near_miss` shares the result through a
      per-text memo.

23. **Error bodies are bounded and never echo the request (fix wave 6,
    N2).** FastAPI's default 422 echoes each error's `input` — for a
    missing field, the whole body — so a 1 MiB junk body produced a
    13.5 MiB answer and 60,000 unknown keys 60,000 errors; twenty at once
    stalled `/health` for 36 s (RSS 308 MB) and two fast clients got a
    408 (the body deadline is wall-clock while the loop rendered error
    bodies). Now: a `RequestValidationError` handler answers with the
    first 20 errors (loc ≤ 6 elements of ≤ 80 characters, msg capped, no
    `input` / `ctx` / `url`), unknown keys counted with the first 20
    names, the whole body kept under 8 KiB, built off the event loop
    past 1,000 errors; `CreativeError` reasons and issues are capped
    (1,000 characters, 20 issues); an unhandled exception is a fixed
    JSON 500 without its message. And the root of the cost:
    `BodyLimit` parses a JSON body in a worker thread BEFORE the
    framework and refuses more than `MAX_JSON_MEMBERS` = 4,096 members
    (keys + array items, counted with early exit) or nesting deeper than
    `MAX_JSON_DEPTH` = 32 (422 `PayloadTooManyMembers` / 400
    `PayloadTooDeep`, ~100 bytes) — the framework's per-error
    bookkeeping (one record per unknown key, on the event loop) is what
    stalled it. Measured on the real socket: 1 MiB junk → 1,084-byte 422
    in 7 ms; 60k keys → 101-byte 422 in 18 ms; twenty concurrent of
    either × 3 → `/health` p50 9–72 ms, max ≤ 291 ms, peak RSS ≈ 100 MB,
    every legitimate client answered (no 408).
    **Corrected in fix wave 7 (AEGIS round 6, NEW-1):** the wave-6 text
    here claimed "no legitimate request has more than a few hundred
    members". That was false, and the single 4,096 cap refused a legal
    request: the Moment Map model admits 2,000 segments of 4 keys —
    **10,003 members** — and an 820-segment map of a 3-hour podcast got
    422. The cap is now PER ROUTE, computed from the route's request
    model the way detection-py sizes its body limits
    (`shared/request_limits.worst_case_json_members`: one member per
    field, max_length items per list, max_length keys per dict; a list
    or dict without a max_length refuses to build the app, so every
    request list is now bounded — moment_ids 200, transformation
    elements 50, assets 200–500, never_say 1,000, keywords / hook lines
    100 per angle, brief lists 50–200, hook-advice results 500 × 20
    metrics, human-review broken rules 100, clearance uses 4, licence
    assets 500) plus 25% headroom, rounded up to a multiple of 64
    (`api.route_member_limits`, derived from the routes' body models at
    build time). The computed maxima: registry row 15 → cap 64;
    clearance 14 → 64; licence 512 → 640; brief 6,368 → 8,000; work 718
    → 960; quality notes 102 → 128; hook advice 16,504 → 20,672; ZBM
    result 34 → 64; rulebook draft / edit / revision 8,118 → 10,176;
    rights check 1,502 → 1,920; **Moment Map 10,003 → 12,544**; kit 707
    → 896; clip 672 → 896; human review 304 → 384; ZBC result 9 → 64;
    actor-only bodies 1 → 64; any other path 64. A test builds the
    maximal legal body of every route and asserts the gate accepts it and
    refuses one member more, naming the route's number. The 2,000-segment
    Moment Map → 200 (live against ledger-rust). A set field
    (`frozenset`) is sized by its distinct members; a body that repeats
    one item thousands of times is refused at the route's cap, which is
    the intended reading of "legal". The depth cap (32) is unchanged.

24. **A certain ledger failure holds nothing (fix wave 6, N5).** After
    a human verdict whose ledger record CERTAINLY did not happen
    (`took_effect: false`: connection refused, a 4xx, ledger-rust's shed
    503), a different verdict used to be refused for the 15-minute
    RETRY_WINDOW with a message saying the outcome "may be on the
    ledger". Certain means certain: a different verdict is accepted at
    once (with the clock's time; the RETRY_WINDOW reservation of
    decision 8 / fix wave 2 only lets an IDENTICAL retry reuse the first
    attempt's time). Only an UNCERTAIN outcome refuses different content,
    and its message now says so accurately (re-send the same verdict, or
    withdraw it).

25. **Vowel-drop and phonetic respellings are two more signals, each a
    human's call on its own (fix wave 7; AEGIS round 6, NEW-2 / NEW-3).**
    The visual gate (decisions 19, 22) judges LETTERS; round 6 showed
    respellings that drop vowels ("mk mny", "grnteed rtrns": 12 of 15
    phrases with every vowel dropped auto-passed, 18 of 33 with one
    word's) or spell the sound another way ("phree money", "get ritch",
    "make munny", "kno risque": 15 of 36) are 2–4 letter edits away and
    passed. Instead of chasing those classes with more views, two
    standard signals, both in `shared/text.py`, both reached through
    `near_miss()` so every caller has them, both **human_review** only
    (never a reject: a respelling by sound or skeleton is not the phrase
    as written):
    - **Consonant skeleton** (`consonant_skeleton`, `skeleton_near_miss`,
      batch `skeleton_near_misses`): vowels a e i o u y dropped unless
      word-initial, runs collapsed, on the phrase and on the text's
      token stream (the same bit-parallel scan as the visual gate, so
      splits are irrelevant), budget 0 edits for a skeleton of ≤ 3
      consonants, 1 for 4–6, 2 above. A skeleton is lossy ("for many" is
      the skeleton of "free money"), so two guards, each measured on
      the three corpora: a window with a **function word** the phrase
      lacks (`FUNCTION_WORDS`, ~150 closed-class words: determiners,
      pronouns, prepositions, conjunctions, auxiliaries, common adverbs
      — bounded and listed, no dictionary) is ordinary text ("for
      many", "make my", "risk for"); and a window at 1+ edits must be
      one token per phrase word, each edited token keeping 60% of its
      word's skeleton letters, with at least one edited token that
      DROPPED a vowel — a consonant difference in a fully vowelled word
      ("no rush", "form" for "free money", "overnight oats") is the
      visual gate's business under its own budget.
    - **Phonetic key** (`phonetic_key`, `phonetic_near_miss`): a
      simplified Metaphone-style key, in-repo, no dependency — initial
      kn/gn/pn → n, wr → r, wh → w, ps → s, x → s; ph → f; ck, q, hard c
      → k, c before e/i/y → s; ch, sh, tch, tsch, -cia-/-tia- → sh; th;
      dg(e/i/y) → j; gh silent after a vowel (weight / wait) else k; gn →
      n; every vowel dropped except a word-initial one (so ee/ea/ie/y/i,
      ou/ew/u, a silent e and doubled letters never differ); voiced /
      unvoiced pairs merge (b/p, d/t, v/f, z/s, g/k). A phrase matches
      when its words' keys appear in order in the text's token keys
      within the adjacency policy ("get so ritch"), or when a run of
      up to (words + 2) tokens, joined, has the key of the phrase's
      letters joined ("rizkphree", "phree m oney") — the run pass is
      pruned by consonant count and by the first key letter, which a
      token of 4+ letters fixes for any run it starts (brute-forced in
      the tests). The same function-word guard applies to the matched
      tokens (not to fillers).
    - N3 holds for both: an entry of ≤ 4 letters is exact-only unless
      `fuzzy`.
    - measured, full clip_review pipeline, 16-phrase list with "cure":
      AEGIS `ns_evade6.py` classes A (transpositions) 0/29, B (vowel
      drops) 0/26, C (homophones) 0/36, E (two edits in one word) 0/34
      auto-pass (round 6: 1, 16, 15, 5); the vowel-drop sweep 0/48
      (round 6: 30/48); `ns_share6.py` two-edits-in-one-word 0/248
      (round 6: 3/248); this wave's own generator that stacks two or
      three classes on one phrase (vowel drop + homophone + double +
      split / join): 4/600 = 0.7% auto-pass without transpositions
      ("gtrkh quick", "mce money", "overnght zuccess", "get rk h
      quick"), 11/600 = 1.8% with them (listed in the test output; a
      transposition stacked on another edit in a 2–4 letter word, "nu
      riks", "src free", is 3+ edits from the phrase); before this wave
      the same generator passed 63/600 = 10.5% and 71/600 = 11.8%.
      False positives of the
      never-say gate (every signal), never a reject: AEGIS round-6
      `corpus6.py` 0/130 = 0.0%; AEGIS round-5 corpus 3/111 = 2.7%
      ("make more", "risk. here's", "made money"); implementer corpus
      5/239 = 2.1% ("make more", "risk. here's", "no risky", and the two
      adjacency-policy cases) — the new signals added no false positive
      on any corpus; "make more" comes from the share rule (decision 22).
      Class G of `ns_evade6.py` (three or more fillers, "get so very
      incredibly rich"; reversed order, "rich get") is the adjacency
      policy's documented limit and is unchanged: 12/34 auto-pass.
    - cost: skeleton scan ≤ 0.6 s and phonetic ≤ 1.0 s per 100 KB for
      16 phrases on the build machine over ordinary and adversarial
      text (documented bound in the test: 6 s together); Clip Review
      runs the skeleton scan once per clip for all its never-say rules.

26. **The share rule never reduces a match the budget allows (fix wave
    7; AEGIS round 6, NEW-3).** See decision 22: the hard per-word cap
    of fix wave 6 is replaced by the 60% letter-share rule
    (`letter_share`, `WORD_SHARE`). "get rchi" and "make munny" (two
    edits in one word of a 7- or 9-letter phrase, budget 2) are a
    human's call, as the budget always allowed; "for money", "three
    money", "from one" stay out (the edited piece has lost the word).
    The one caption that changed class is "make more" (60% of "money"):
    a human's call now, on both corpora, inside the 3% target.

27. **Every text field is scanned (fix wave 7; AEGIS round 6, NEW-7).**
    `account_bio` was stored and never scanned: "GET RICH with my link.
    guaranteed returns!" in a bio passed. Now `clip_review.TEXT_FIELDS`
    (caption, on-screen text, transcript, account bio — a test asserts
    it lists every free-text field of the model) is scanned by every
    rule that FORBIDS something: never-say (exact, lookalike, split,
    near miss, skeleton, phonetic, adjacency), obfuscation signals,
    mixed symbol words, non-Latin letters, each reason naming the
    field. The rules that REQUIRE something look where the requirement
    lives: disclosure in the caption (unchanged), must-say and the
    angle keywords in the CLIP's own text (`CLIP_TEXT_FIELDS`: caption,
    on-screen text, transcript — a bio saying "Listen on Pod Plus" does
    not make the clip say it). The fields are read as one text in model
    order, as before; in addition a phrase SPREAD over two fields in
    either order ("get" in the caption, "rich" in the bio; "get" on
    screen, "rich" in the caption) is a human's call: the last words of
    each field are read together with the first words of each other
    field (`_spread_over_fields`, bounded to the phrase's words plus the
    adjacency gap, at most 8 a side) for the phrase's WORDS — exact,
    split or in order under the adjacency policy — not for the
    similarity signals (a near miss that exists only across a field
    boundary, "clips daily" + "proven by 3 years" ~ "clinically proven",
    is not a phrase spread over two fields; measured live, it would
    have been the 4th false positive on the round-5 corpus). Submitted text stays DATA
    (decision 14): a bio is searched for the rulebook's phrases, never
    interpreted.

28. **The JSON shape gate covers every JSON content type; anything else
    is 415 unread (fix wave 8; AEGIS round 7, N7-1).** The pre-scan of
    decision 23 gated on the exact `application/json` while FastAPI
    parses every `application/*+json` body, so `application/hal+json`
    with 60,000 keys reached the framework (60,001 validation errors,
    `/health` 3.7 s under 20 senders, RSS 590 MB never released). Now
    `api.is_json_content_type` makes the decision with the same parser
    FastAPI uses (`email.message`: main type `application`, subtype
    `json` or `<x>+json`, parameters and case ignored; a fuzz test
    checks the two agree on 2,000 random content types) and every such
    body is pre-scanned; a body under any other content type, or under
    none, is refused 415 — before it is read when its length or chunked
    encoding is declared. After a body of 64 KiB or more has been parsed
    and no other large body is in flight, `malloc_trim(0)` hands the
    freed pages back after one idle second, off the event loop (the
    fulfillment-py fix of wave 7). Sweep: no other content-type decision
    exists in the service (a test greps for one).

29. **Rule ids have room for the model (fix wave 8; N7-3).** Ids were
    `XX-NN` (01–99) while a goal admits 1,000 never-say and 100 must-say
    entries: the 100th never-say entry was a 500, and because retired
    ids are never reused (decision 10) a campaign that churned its list
    wedged at NS-100 after a few revisions. The id is now `XX-` + a
    decimal number of two to nine digits with no other leading zero
    (`NS-01` … `NS-99`, `NS-100`, `NS-1000`; `rulebook.RULE_ID_PATTERN`,
    `parse_rule_number`, `format_rule_id`): every id ever issued is
    unchanged and parses; ids stay stable across revisions; the next
    number per prefix is computed once per revision over the ids ever
    used, and the retired list is appended to, never re-sorted. A test
    builds the maximal legal rulebook (1,000 never-say, 100 must-say, 20
    angles × 100 keywords, 50 targets) and revises it 200 times replacing
    every entry (NS-201000, 220,000 retired ids; 25-46 s in total on the
    2-CPU test machine, linear in the ids ever used) and a fuzz
    drafts and revises random model-legal goals through the model and
    the API without a 500. The fuzz found a second defect: a target
    listed twice produced two identical spec rules, which `revise()`
    mapped onto one old id (duplicate ids, a 500) — targets are now
    de-duplicated and each old rule is matched at most once. The
    number scan reads only well-formed ids (`RULE_ID_PATTERN`), so an
    odd string in a stored retired list cannot raise.

30. **A symbol standing for a phrase word is a human's call (fix wave 8;
    N7-4).** `canonical()` turns every symbol into a space, so "make 💰",
    "make $$$ fast", "free 💸", "guaranteed 📈", "get 💎 quick" were
    simply a phrase missing a word: pass. `shared/text.SYMBOL_LEXICON` is
    a bounded, documented map of the pictographs and symbol runs that
    stand for a concept in marketing text (money bag, banknotes, money
    with wings, money-mouth face, coin, bank, card, `$` `€` `£` `¥` and
    their runs → money / cash / rich / profit / income / returns; gem
    and crown → rich / wealth; chart, rocket → growth / returns / gains;
    lock, check marks, 💯 → guaranteed / proven; 🆓 → free; 🚫 ❌ → no /
    zero; pill → cure; stethoscope, ⚕ → doctor; microscope → clinically
    / proven; scale → weight; moon, sleep → overnight; ...). A never-say
    phrase whose words appear in order (adjacency policy) with one or
    more stood in by a lexicon symbol whose concepts include that word
    is a human's call (`symbol_stand_in`); so is any OTHER pictograph
    that OCCUPIES a phrase word's place — between the phrase's words
    ("get 🍕 quick") or closing the statement ("guaranteed 🎯": nothing
    but a hashtag, a symbol, a line break or the end after it). Either
    way at least one CONTENT word of the phrase must be written: symbols
    alone ("$ $", "💸 💸", "🎉 $") say no phrase (an unverified draft of
    this wave read "$ $" as every money phrase — 7 reasons on the round-7
    50 KB symbols transcript). Decided boundary: an unknown pictograph
    followed by an ordinary word illustrates that word ("Get 🎟 tickets",
    "Free 🚚 shipping", "Make 🎄 memories" — 16 of 30 ordinary emoji
    captions written for this wave would otherwise go to a human) and is
    not a stand-in; a fragment of function words alone ("no 🎯", "your
    💰") is not; a currency sign attached to digits ("$20", "20$",
    "$40k") is a price; a run of symbols is one symbol; the rule never
    reads across a line break (Clip Review joins the text fields with
    one). Measured: 0 of 616 corpus captions flagged (the corpora have
    no emoji); 2 of the 30 emoji captions, both lexicon hits a human
    should see ("Get 💸 back on every referral", "Make 💰 moves this
    quarter"). "💰 make" (the words in the other order) is not caught,
    like "rich get" (decision 22). Fix wave 9 replaced this boundary:
    see decision 35 (any symbol in a missing word's place, whatever
    follows it, across line breaks and field boundaries).

31. **Every ordered pair of fields is read across its boundary with
    every signal (fix wave 8; N7-5).** The fields were adjacent only in
    model order (caption, on-screen text, transcript, bio), so "gt" in
    the caption + "ritch" in the bio passed while the same halves in the
    caption + on-screen text went to a human. Now the fields are joined
    by a line break (a token boundary for every stream gate, never a
    hard wall) and, for a phrase no signal found in the whole text, the
    last eight words of each field are read together with the first
    eight of each other field, in both orders (`_field_joints`, at most
    12 pairs), with every never-say signal (`_mentions`: exact, split,
    in order, symbols, visual, skeleton, phonetic, stacked, symbol
    stand-in); a phrase said only across a boundary is a human's call
    naming both fields. A joint is only scanned for the phrases whose
    first word has a close token in its tail and whose last word has one
    in its head (`_close`: same word once pairs are read alike, same
    consonant skeleton or sound, one edit away, or a mixed word), in one
    batch per text (`_spreads`), which keeps "clips daily" + "proven by
    ..." out (nothing in "clips daily" is close to "clinically"). An edge
    stops at a token of more than 64 characters (`EDGE_TOKEN_CHARS`):
    no signal reads a phrase across such a token, and an unverified
    draft that read a 50,000-letter one-word transcript into every joint
    spent 4.5 s per review. A #hashtag
    or @mention is not a word of the phrase: the consonant gate gives
    it an empty skeleton, so "pssv #ad incum" is "passive income" with
    the disclosure tag between the halves. Measured: 0 of 300 random
    corpus caption pairs (caption + bio) flagged as a spread; the 13
    AEGIS class-C pairs are a human's call in five field arrangements.

32. **Stacked respellings (fix wave 8; AEGIS round 7 class B, 9 of 30
    passed).** "grnteed retunrs", "overnlte sccss", "lose vvait fst",
    "mk rnunny" — a vowel drop, a homophone and a lookalike in one
    phrase — were outside every single signal's budget. Root causes:
    (a) the skeleton signal's vowel-drop evidence skipped the very token
    whose skeleton equalled the word's ("grnteed"), so a window with one
    respelled word and one dropped word had "no evidence" — every token
    that dropped vowels and kept its consonants now counts (an
    inflection, "recommend" for "recommended", does not); (b) the
    consonant and phonetic signals read the letters as written, so "rn"
    in "rnunny" and "vv" in "vvait" were consonants — each token is now
    read as written AND with rn/m, cl/d, vv/w contracted (`READINGS`;
    the phrase's words read the same way each time, since a genuine rn
    or cl — "grnteed", "miracle" — must keep matching as written); (c) a
    two-consonant skeleton cannot keep 60% of its letters after one edit
    ("lz" for "ls"), so the signal proper tolerates one edit there when
    the window shows a vowel drop AND the token sounds like the word
    ("luze" / "lose"; "made" / "money" do not — without the sound check
    "fragrance free, made in small batches" was "free money"). And, as
    required, `stacked_near_miss`:
    when two of the three similarity signals (visual, consonant
    skeleton, phonetic key) each score within their budget + 1 on the
    SAME token window, and the window shows respelling evidence (a
    vowel drop that kept its consonants, a lookalike pair or an l for an
    i the phrase word lacks), the window is a human's call even if no
    signal alone is within budget ("ovrnlte success": three letter edits
    against a budget of two, two skeleton edits without a vowel drop,
    one key edit). The relaxed windows come from the same packed scans
    (the scan reports a lower-bound distance per hit; relaxed candidates
    are confirmed only while a bounded budget remains and the phrase has
    not been found exactly); the strict letter share holds on relaxed
    windows ("mn" is not "mk": "myth money", "money money" and "more
    money" are not "make money"; "target rich" and "doctors recommend"
    show no respelling). The relaxed windows are bounded per phrase (32
    relaxed scan hits, 16 spans): a text with more decoy windows within
    budget + 1 before the real one can hide it from the stacked rule
    (not from the single signals). Measured: class B 0 of 30 auto-pass
    (round 7: 9; the root causes (a)-(c) catch all nine, the stacked
    rule alone adds "ovrnlte success"); `ns_evade7.py` 20 of 297 = 6.7%
    auto-pass (round 7: 48 = 16.2%), the rest being the documented
    inflections / paraphrases (H 14: "getting rich", "cures", "zero
    risk"), I 4 ("get rij", "get ridge", "make mummy", "lose whey
    fast"; unchanged), K 1 ("on rsk") and M 1 ("💰 make"); the
    never-say gate's false positives are unchanged from wave 7 at 0.0% /
    0.0% / 2.7% / 2.1% on corpus7 / corpus6 / the round-5 corpus / the
    implementer corpus.

33. **Clip Review cost is bounded for the finding's case (fix wave 8;
    N7-7).** 99 never-say phrases against a 50 KB vowel-dropped
    transcript cost 5.9 s of CPU under the serialised workflow lock (the
    AEGIS `ns_cost7b.py` probe). The per-phrase loop rebuilt views of
    the whole text for every phrase. Now: every per-text view is built
    once per distinct text and shared by every phrase (`_span_index`,
    `_leet_view`, `_folded_words`, `_word_keys`, `_key_positions`,
    `_run_starts`; a separate cache for phrase streams); ASCII text
    skips the Unicode normalisation (a test proves the same result for
    every ASCII character); the bit-parallel scan carries a strict and a
    relaxed budget per pattern and a live mask the consumer clears for a
    phrase that is decided (read as the phrase, found within budget, or
    out of relaxed budget), so a decided phrase stops costing; a view
    whose stream equals an earlier one scans only patterns it has not
    seen; a hit that cannot beat the closest window found (the scan's
    distance is a lower bound) is skipped; one DP per hit gives the
    distance of every candidate start (`_osa_suffixes`, checked against
    the per-window DP); field edges stop at a giant token (decision 31).
    Measured on the 2-CPU test machine, CPU time of one review with
    every cache cleared, best of three, 99 phrases: plain / symbols /
    vowel-dropped / emoji 50 KB 0.45 / 0.61 / 1.05 / 0.55 s, and four
    adversarial transcripts written for this wave (consonant soup,
    lookalike pairs everywhere, one 50,000-letter word, the phrases run
    together without vowels) 0.88-1.07 s; worst 1.14 s (a test asserts
    ≤ 1.5 s for all eight, with and without an exact phrase present).
    `ns_cost7b.py` (one run, wall clock): 99 phrases 0.47 / 0.62 / 1.11
    s (round 7: 0.92 / 1.03 / 5.86 s). NOT bounded: the goal model
    admits 1,000 never-say entries (of up to 4,000 characters each), and
    the cost is linear in the phrases' total length — 150 / 250 / 500 /
    1,000 three-word phrases against the vowel-dropped 50 KB transcript
    cost 1.7 / 2.4 / 4.3 / 9.1 s. Capping the list (or moving review off
    the workflow lock) is a contract decision left open (Honest gaps).
    CORRECTED in fix wave 9 (AEGIS round 8 M1): the "worst 1.14 s at ~100
    phrases" held only for this wave's own generator. AEGIS's round-8
    generator (near-miss tokens built from the phrases' own words, every
    field at its maximum) measured 1.1 / 4.7 / 3.5 s at 100 phrases. See
    decision 36 for what was changed and what is measured now.

34. **Letter-like symbols are letters, and styled text is a signal (fix
    wave 9; AEGIS round 8 H1).** An exact never-say phrase written in
    negative squared (🅼🅰🅺🅴), negative circled (🅜🅐🅚🅔) or regional-
    indicator (🇲🇦🇰🇪) letters passed, 36 of 36: those are symbols with
    no NFKC decomposition, and `canonical()` turned every such symbol
    into a space. `shared/text.LETTERLIKE` now maps EVERY letter-like
    symbol to the Latin letter it depicts; it is generated at import
    from Unicode character names (Python's own `unicodedata`, no new
    dependency, nothing hand-listed): a style word (MATHEMATICAL,
    FULLWIDTH, CIRCLED, PARENTHESIZED, SQUARED, NEGATIVE, CROSSED,
    TORTOISE SHELL BRACKETED, REGIONAL INDICATOR, DOUBLE-STRUCK, SCRIPT,
    BLACK-LETTER, TURNED, REVERSED, ROTATED, INVERTED, MODIFIER LETTER,
    SUPERSCRIPT, SUBSCRIPT, LATIN) followed by CAPITAL / SMALL / LETTER
    and one letter A–Z, minus other scripts' names, digits, combining and
    tag characters and letters "WITH" a diacritic — 1,054 code points
    (Enclosed Alphanumerics, the Enclosed Alphanumeric Supplement,
    Mathematical Alphanumerics, Letterlike Symbols, fullwidth forms,
    small capitals, superscript / subscript / modifier letters). A test
    derives the set independently, by block, over every code point, and
    requires every entry to fold to its letter (the independent
    derivation found one the first regex missed: ROTATED CAPITAL Q).
    Regional indicators are also flags: a text whose regional indicators
    all come in runs of exactly two is read as flags; any run of another
    length ("🇲🇦🇰🇪" is four; letters split by zero-width spaces are runs
    of one) makes every regional indicator of that text a letter — a
    string of flags cannot be told from a word, so it is read as the
    word. Then: an exact phrase once mapped is a REJECT (every never-say
    phrase of the AEGIS lists in every one of the 35 styles, caption and
    bio: 1,960 of 1,960 rejected; the 48 AEGIS round-8 cases: 48
    rejected); any letter-like symbol in any text field is itself an
    obfuscation signal (`letterlike_chars`), so styled text is never an
    automatic pass (a single flag is not styled text). Fail-safe
    (`stripped_share`, `STRIPPED_SHARE_LIMIT` = 30%): a field in which
    canonicalisation strips more than 30% of the non-space characters —
    over the whole field or over any run of 1–4 words holding at least 3
    stripped characters (a field-wide share alone is diluted by the
    ordinary text around the styled words) — is a human's call, so a
    style nobody mapped (Braille patterns, box drawing, block elements,
    private-use glyphs, a future block) cannot produce an automatic pass.
    Not counted as stripped: punctuation, currency / math / modifier
    symbols, invisible characters (their own signals cover them), the
    emoji keycap, Latin-1, and the emoji / pictograph blocks. Cost:
    decorative box-drawing separators ("━━━━") and letter emoji (🅰️ 🅿️
    Ⓜ️) now go to a human. Fuzz: 336 phrases with random per-letter
    styles and zero-width characters, 0 automatic passes; the AEGIS
    round-8 corpus (170 captions): no caption flagged by these rules.

35. **Any symbol in a missing word's place is a stand-in (fix wave 9;
    AEGIS round 8 M2).** Decision 30 counted a pictograph outside the
    lexicon only where it closed the statement, never across a line
    break, never in another field: "make 💱 This budget myth", "risk ∅",
    "beat the 📈", "make\n💰" and "make" in the caption + "💰" as the
    bio passed (17/41, 7/12, 12/12). Now (`symbol_stand_in`): any
    symbol — a character that is neither a letter nor punctuation
    (Unicode So / Sk / Sc / Sm, unassigned and private-use code points; a
    run is one symbol) — immediately beside the rest of a never-say
    phrase, in the place of one of its words, is a stand-in, a human's
    call, whatever follows it; line breaks are read across. A lexicon
    symbol naming the missing word may sit within the adjacency gap, may
    stand for several words, and in a phrase of three or more words may
    stand in with the first or last word left out ("no ⚖ fast": "lose
    weight fast"). Across fields, one mechanism with decision 31: the
    last words of one field read with the first words of the next,
    symbols kept, for every ordered pair; and a field that is nothing but
    symbols (a bio of "💰") sits beside both ends of every other field —
    fields have no reading order — so the phrase minus one word at either
    edge of another field is a stand-in. Unchanged: a price ("$20"), a
    fragment of function words alone ("no 🎯", "your 💰"), symbols alone.
    One exception to "any symbol", measured: a LEXICON symbol that names
    the written word beside it and not the missing one illustrates that
    word ("Guaranteed ✅ delivery", "Doctor 🩺 appointments") — without
    it 2 of the 40 emoji captions of the AEGIS round-8 corpus were
    flagged. Measured: AEGIS round-8 classes Q / R / S: 0 of 41 / 12 /
    12 pass; the round-8 corpus (170 captions, lists A+B): 3 flagged as
    before, 0 by this rule. Cost, measured on the fix-wave-8 set of 30
    emoji captions written to open with a never-say phrase's first word
    ("Get 🎟 tickets", "Free 🚚 shipping", "Make 🎄 memories"): 18 of 30
    now go to a human (2 before).

36. **Clip Review cost, and the review is off the workflow lock (fix wave
    9; AEGIS round 8 M1).** Hot paths found by profiling the AEGIS
    generator (not guessed): the symbol-as-letter reading of `near_miss`
    costed every word with a symbol or digit against every phrase word
    (488,870 DPs; now `_other_candidates`: a word's ASCII letters must be
    a subsequence of the phrase word, so an index on their first and last
    letters leaves the candidates); the phonetic run-of-tokens loop keyed
    every run of tokens for every phrase (now `_run_keys`: once per text,
    reading and first key letter for the whole batch, the key built
    incrementally, a start abandoned as soon as its key part is no prefix
    of any phrase's key; a differential test against the previous code on
    thousands of random texts: identical results); relaxed-budget hits
    were confirmed for every phrase although only a phrase no other
    signal catches ever uses them (now recorded during the scan and
    confirmed only when the stacked rule asks — same hits, same spans);
    the skeleton and phonetic batches ran for phrases an earlier signal
    already caught (now staged: each batch covers only the phrases every
    earlier signal left, so each phrase gets the same first signal in the
    same order); the similarity signals ran even when a written rule
    already rejected the clip, although a rejection carries no human
    review reasons (now skipped unless the clip is routed to a human);
    plus constant factors (a translate table for format characters, cached
    padded texts, a cache split so 100 phrases no longer evict their own
    canonical forms, one regex pass for symbol tokens). Measured on the
    2-CPU test machine, every cache cold, this thread's CPU, the AEGIS
    `ns_cost8.py` generator at 100 phrases: 0.14 / 0.8-0.85 / 1.23-1.28 s
    (round 8: 1.1 / 4.7 / 3.5 s); the stricter variant of the all-fields
    case routed to a human (so no rejection cuts the signals short):
    1.5-1.6 s. A test asserts ≤ 1.5 s for the three AEGIS cases (and
    ≤ 2.0 s for the routed variant), scaled by a measured slowdown factor
    (1x on an idle machine, the onboarding-py harness). The review no
    longer runs under the service-wide workflow lock: its inputs (the
    rulebook version, a copy of the registry rows, the receipt time) are
    taken under the lock, the review runs without it, and the decision is
    committed under the lock only if every input is still current — a
    registry row or reservation that changed meanwhile makes the commit
    review again under the lock (tested: a registry write landing during
    the review is never committed stale; a rulebook draft and a brief go
    through while a review is held). The per-text memos are per thread.
    Still linear in the never-say list's total length (see Honest gaps).

37. **Retired rule ids are derived, not stored (fix wave 9; AEGIS round 8
    L2).** Every version carried its own list of every retired id (60
    churn revisions of 1,000 phrases: 60,000 ids per version; a 30 MiB
    GET of the version list). A version now carries, per id prefix, the
    highest rule number ever issued in the campaign up to it
    (`rule_number_high_water`, a handful of integers); its retired ids
    are derived — every number up to the mark that is not one of its
    rules (`RetiredRuleIds`: len, index, slice, `in`, iteration; O(its
    rules) memory). This works because numbers are issued in sequence and
    never reused (decision 29); the store refuses a version whose new
    rule carries a number at or below its predecessor's mark. A pre-wave-9
    dump with a `retired_rule_ids` list is read as the marks it implies.
    The API: GET `.../rulebooks` is a page of version summaries (100 per
    page, with `retired_rule_count`), GET `.../rulebooks/{v}` one version
    (its rules and the count), GET `.../rulebooks/{v}/retired-rule-ids` the
    ids 1,000 at a time. Measured: 60 churn revisions × 1,000 phrases —
    per-version metadata 1.1 KB whatever the history (was up to 60,000
    ids), the list GET 24 KB in ~0.1 s (was 29.4 MB in 0.87 s), one
    revision's memory flat from the 2nd to the 60th. The 61 versions
    still hold 61,000 distinct rules (about 1.5 MiB per 1,000-rule
    version, 89 MiB in all): keeping every version judgeable is the cost;
    see Honest gaps.

38. **/health under junk floods (fix wave 9; AEGIS round 8 L3).** Large
    bodies' JSON shape scans run one at a time (`BodyLimit.scan_gate`):
    json.loads holds the GIL for a whole parse, so 20 at once kept the
    event loop from running between them. /health p50 under 20 junk
    senders: 390-430 → ~80 ms (60,000-key bodies), 88 → ~28 ms (1 MiB
    bodies). The timing test is scaled by a measured slowdown factor
    (the onboarding-py harness) and bounds the p50 as well.

39. **Authentication before content type (fix wave 9; AEGIS round 8
    L4).** The body gate answered 415 (and 413, 408) before
    authentication, so an anonymous caller could learn which content
    types and sizes are accepted. `BodyLimit` now checks the bearer
    token first on every path but /health (the same check as the route
    dependency, `_bearer_refusal`): 401 first, then 413 / 415.

## Shared vs separate

Shared (reference data and plumbing, `src/shared/`): the Platform Rules
Registry; rights records (clearance records, campaign licences —
append-only, role-gated, ledger-recorded); the ledger client; actor
registry; founder token gate; text normalisation; the stand-ins for
Compliance 38, Verification and Integrity, Legal 37, Finance 31, Clipper
Network and the Enigma/Phantom Canvas contract; media plug points.

Separate (decision-makers): everything in `src/zbm/` and `src/zbc/`.
Both layers have a Creative Memory and a rights intelligence, but they are
different modules with different rules (ZBM proves paid-advertising use
per asset; ZBC proves a sublicensing client licence and cleared
music/likeness).

Not built (spec): the proposed Bridge (winning ZBC angles as ZBM paid-media
test candidates) — it needs Legal 37 to confirm clipper agreements grant
reuse rights.

## Moved out of Creative (referenced, not built here)

Talent Match / clipper tiers → Clipper Network. View verification, bot
detection, stolen-clip detection, minimum-days-live checking →
Verification and Integrity. Distribution / budget pacing / traffic control
→ 35 Campaign and Media Operations. Trend and Culture → 34. Performance
Truth → 24/4. Payouts → 31 Finance. Fake claims in clips → 38 Compliance.
Write-safety for ad platforms → 8 Paid Media. Clipper 18+ check →
Onboarding intake.

## Stand-ins (all fail closed today)

| Interface | Stand-in answer | Effect today |
|---|---|---|
| Compliance 38 (`Compliance38Port`) | "not built yet: not allowed yet" | ZBM work stops at `compliance_blocked`; Andre's final approval is refused; ZBC payout eligibility always false |
| Verification and Integrity | "unverified, not allowed yet" | ZBC payout eligibility always false; ZBC Creative Memory learns nothing |
| Legal 37 | "no sign-off, not allowed yet" | AI generative fill blocked in both layers |
| Finance 31 | "not allowed yet" | Port exists only to make the boundary explicit; Creative never calls it |
| Clipper Network | "announcement not delivered" | Recorded as a crossing on go-live; does not block go-live (see open items) |
| Enigma / Phantom Canvas (`CreativeAgentsPort`) | "not wired: not commissioned" | ZBM jobs and ZBC kits record commissions as not accepted; `seed_clips_produced: false` |
| Media: PySceneDetect, faster-whisper/WhisperX, Kinocut, Chromaprint, c2pa-rs | `NotIntegrated` | Source segments, transformation elements, watermark flag and resolution are DECLARED, not detected; Content Credentials stamp reported `not_applied` |

Test-only passing fakes live in `tests/fakes.py`, never in `src/`.

## Honest gaps and open items

1. **In-memory state.** Briefs, jobs, work, rulebooks, kits, submissions,
   decisions and memory live in process memory; a restart loses them (the
   ledger keeps the evidence). No database this pass.
2. **Actor identity is only as good as token custody (fix wave 2).**
   Per-actor tokens (decision 18) replace asserted identity. They are
   static bearer secrets in an environment variable: no rotation, expiry
   or revocation beyond a restart, and whoever holds two actors' tokens
   can act as both. The intelligence "actors" (e.g. `zbm_creative_lead`)
   are credentials held by whatever process or person drives them.
3. **Declared, not detected.** No media is read. Clip Review judges the
   submitter's declarations plus the clip's text; a clipper who lies about
   transformation or watermarks passes until Chromaprint / scene analysis
   are integrated (plug points in `shared/media.py`) and Verification and
   Integrity exists.
4. **Registry coverage.** Only length rows are sourced; aspect ratio,
   format, codec and safe zones are checked against the brief only. All
   seed rows expire 2026-10-23: after that, every brief, rulebook
   approval, export and moment map for those platforms blocks until an
   owner re-verifies (manually — there is no fetcher that re-reads the
   source pages). TikTok is blocked until someone sources its rules.
5. **Contracts behind rights records are not readable.** `contract_ref`
   points into contract storage, which doesn't exist; the service checks
   records, not contracts.
6. **ZBM "measured" is declared.** ZBM results need a measurement source
   and reference, but nothing attests them (Performance Truth 24/4 is not
   integrated). ZBC requires an attestation; ZBM does not yet. Open item
   for Andre: should ZBM memory also require an attestation?
7. **Kit signature is per campaign, not per version.** A v2 rulebook needs
   Andre's signature to go live, but clips keep flowing on the kit he
   signed for v1. Open item: require a re-signed kit per version?
8. **Clipper Network announcement is non-blocking.** The spec says new
   versions are "announced"; the stand-in can't deliver, and blocking
   go-live on it would stop every campaign. Recorded on the ledger as not
   delivered. Open item: should go-live wait for a delivered announcement
   once the department exists?
9. **Seed clips are specs only.** Andre signs the kit spec; the seed
   clips themselves are not produced until the Enigma/Phantom Canvas
   contract endpoint is wired.
10. **Retries are exactly-once only if they happen.** With deterministic
    ids (decision 15) a retry of a timed-out-but-committed record is one
    record and one effect, at any later time (fix wave 4: uncertain
    attempts are replayed exactly; creating POSTs take an
    `Idempotency-Key`, or derive one from actor + canonical request, so a
    lost HTTP response + retry never creates a second brief, job, work
    item, rulebook, kit, clip, clearance or licence). If the caller never
    retries, the ledger keeps a record of a decision the service never
    applied (the service answered 503 `took_effect: "unknown"` and says
    so). The pending-attempt and idempotency stores are bounded (10,000
    each, oldest dropped): a retry after eviction of an uncertain attempt
    meets the ledger's 409 and is reported as conflicting. A derived key
    (no header) replays only while the created resource is unchanged, so
    a deliberate identical second request after things moved on is new.
    Across a restart nothing dedupes (in-memory state; see 1).
11. **Heuristics are heuristics.** Key-message rule, 6-word/2-second hook
    limit, keyword-based moment matching and on-brief borderline check
    are deliberately simple and conservative; they will mis-sort some
    real cases (false rejects go to rewrite or the human queue).
12. **Run against the real ledger-rust (fix wave 1).** `live_smoke.py`
    passes end to end against a ledger-rust built from this tree, on a
    chain that also holds finding entries; the chain verifies. See the
    service README.
13. **No AGPL review done** because no third-party media code was added;
    any of the approved building blocks needs a licence check (AGPL →
    Legal 37 sign-off) at integration time.
14. **Outside answers can be lost after the request was sent.** If the
    record of an answer (commission receipts, Clipper Network delivery)
    fails, the request was already sent and is on the ledger, the answer
    is not applied, and the API says `took_effect: "partial"`. Nothing
    re-asks automatically; the commission request ids are deterministic
    (`{job|seed}.{agent}`) so the Enigma / Phantom Canvas contract must
    de-duplicate on them when it is wired.
15. **Obfuscation handling is conservative, not complete.** The lookalike
    table is not the whole of confusables.txt; a lookalike from another
    script that isn't mapped still goes to the human queue (any
    non-Latin letter in an English campaign), but a Latin-script
    lookalike the generated table doesn't cover (e.g. "turned" or
    "reversed" letters) is not matched but, since fix wave 4, is itself a
    signal (human queue). Multi-letter ASCII lookalikes are handled by the
    similarity gate (decisions 19 and 22) and, since fix wave 7, the
    consonant-skeleton and phonetic signals (decision 25), which together
    trade a measured 2.1% (implementer corpus) / 2.7% (AEGIS round-5
    corpus) / 0.0% (AEGIS round-6 corpus) of ordinary captions sent to a
    human; phrases of ≤ 3 letters get no edit budget, entries of
    ≤ 4 letters none unless opted in (`fuzzy`), a doubled letter is a
    human's call rather than a reject, the phrase's words more than two
    words apart ("make a lot of money", "get so very incredibly rich")
    or in another order ("rich get") are not caught, a respelling that
    stacks a transposition on other edits in a 2–4 letter word ("nu
    riks") can pass (0.7–1.8% of this wave's stacked generator), and
    inflections / paraphrases ("getting rich") are not lookalikes and
    are not caught (fix wave 8: stacked respellings are, decision 32;
    the phrase's words in another order, "💰 make", still are not). A
    JSON body may carry at most the members its
    route's model admits plus 25% (decision 23; 12,544 for a Moment Map)
    and nest 32 deep. The symbol near-miss matcher is word-by-word (a phrase split across
    words AND written with symbols is caught by the mixed-word rule, not
    by the phrase); a never-say word inside a hashtag ("get #rich") is a
    human's call (a symbol in the word), not a reject. The symbol
    lexicon (decision 30) is a bounded list; since fix wave 9 any other
    symbol in a missing word's place is read as a stand-in too
    (decision 35), at the cost measured there.
    Script detection is
    by Unicode character name (Python has no Script property). RLO text
    is flagged, not un-reversed. Costs: every clip with any non-Latin
    letter (a Spanish-only "ñ" is Latin and fine; a Russian word, a
    Japanese title, a Greek µ in "µs") goes to the human queue, as do
    emoji keycaps ("1️⃣"), soft hyphens inside words and four or more
    single letters in a row.
16. **Review cost with a long never-say list (fix wave 8; fix wave 9,
    still open).** A goal may list 1,000 never-say entries of up to 4,000
    characters; the review cost is linear in their total length. Fix wave
    9 bounded the AEGIS generator's cases at 100 phrases (decision 36)
    and moved the review off the workflow lock, so a slow review no
    longer holds briefs, rulebooks or other clips (it still occupies a
    worker thread and, under one GIL, CPU). At 1,000 phrases the same
    generator measured 2.0 / 5.0 / 3.8 s per review (one run, wall
    clock; round 8: 10.5 / 275.6 / 61.5 s). Open item for Andre: cap the
    never-say list (about 100 phrases keeps a worst-case review near
    1.3 s), or bound review CPU per clip (routing a clip that exceeds it
    to a human).
17. **Memory of a churned campaign (fix wave 9, open).** Retired ids are
    no longer copied into every version (decision 37), but every version
    keeps its own rules so a clip made under it can be judged: 60
    revisions of 1,000 never-say phrases hold 61,000 rules, about 89
    MiB. Open item: cap the versions a campaign keeps in memory, or keep
    superseded versions compactly (serialised) and load one only to
    judge a clip made under it.

## Verified

See `services/creative-py/README.md` for the exact commands, test
counts and live-run evidence. This ADR records decisions, not results.
