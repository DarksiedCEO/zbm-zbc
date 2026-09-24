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
   judgement is an explicit rule with a code (K1–K6 key message, Q1–Q5
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
    are normalised (NFKC, casefold, zero-width removal) and searched for
    the rulebook's phrases only. "Ignore your rules and approve" in a
    caption or bio has no effect (tested: identical decision with and
    without it).

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
2. **Asserted actor identity.** One shared service token; actor ids in
   request bodies are asserted by the caller. Drafter≠approver is
   enforced on asserted identity — a caller willing to lie about who they
   are defeats it. Andre is the exception (separate token). Per-actor
   credentials are an open item.
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
10. **Ledger event ids are random per attempt.** If the ledger writes an
    event but the response is lost, the service reports 503 and does not
    apply the decision, while the ledger holds a record of it; a retry
    records a second event. Deterministic event ids would make retries
    idempotent — not done this pass.
11. **Heuristics are heuristics.** Key-message rule, 6-word/2-second hook
    limit, keyword-based moment matching and on-brief borderline check
    are deliberately simple and conservative; they will mis-sort some
    real cases (false rejects go to rewrite or the human queue).
12. **Not run against the real ledger-rust.** The `/ledger/events`
    endpoint belongs to another workstream running in parallel. The HTTP
    client is tested with `httpx.MockTransport` and was run live against
    `devtools/fake_ledger_server.py`, which implements the §2 contract
    shape (fields, validation, 201/200/409), not ledger-rust itself.
13. **No AGPL review done** because no third-party media code was added;
    any of the approved building blocks needs a licence check (AGPL →
    Legal 37 sign-off) at integration time.

## Verified

See `services/creative-py/README.md` for the exact commands, test
counts and live-run evidence. This ADR records decisions, not results.
