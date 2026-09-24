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
    window) is refused (409). The instance id is
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
    record and one effect. If the caller never retries, or a different
    decision on the same subject succeeds first, the ledger keeps a record
    of a decision the service never applied (the service answered 503).
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
    "reversed" letters) is matched only if it folds. Script detection is
    by Unicode character name (Python has no Script property). RLO text
    is flagged, not un-reversed. Costs: every clip with any non-Latin
    letter (a Spanish-only "ñ" is Latin and fine; a Russian word, a
    Japanese title, a Greek µ in "µs") goes to the human queue, as do
    emoji keycaps ("1️⃣"), soft hyphens inside words and four or more
    single letters in a row.

## Verified

See `services/creative-py/README.md` for the exact commands, test
counts and live-run evidence. This ADR records decisions, not results.
