# ADR 0008 — Clipper Network Department (CN) Architecture

**Status:** Accepted for build (Sep 26, 2026). **Not certified for any real
clipper.** Verification and Integrity, Compliance (38), Creative Production,
Finance (31), Legal (37), People (43), the messaging provider, the clipper
hub and the push channel are reached only through ports whose default
stand-ins answer "not allowed yet"; with them, no clipper can be admitted,
enrolled or messaged — by design.
**Service:** `services/clipper-network-py` (port 8400 by default).
**Spec:** Clipper Network locked build spec, rev 1 (Sep 26, 2026)
(`CLIPPER_NETWORK_SPEC.md`), research
`research_notes/Clipper Network department/clipper_networks.md`, the
Onboarding spec (P1, P4, P7), BUILD_CONTRACTS.md, ADR 0006 (with the AEGIS
round 14 / 15 amendments) for every protection copied from compliance-py.

**Seed:** `services/clipper-network-py/seed/cn_rules_seed.json` — 27 rules
(CN-00 … CN-26), 8 counsel holds (CN-CQ-01 … CN-CQ-08, status `open`) and the
17 §C.6 message templates (version 1), generated from the spec's §B.9 table
and §C.6 list. SHA-256
`4ae4553c6f7ad8bd794cac1c01bb7a6bb9eb8174c8fb7a30d8da44e2c4aa4af4`,
pinned in `src/config.py` (`PINNED_SEED_SHA256`) and checked at every start.

## Context

CN is the system of record for a clipper from application to offboarding
(spec §0.3): it recruits, admits, tiers, equips (hands on Creative's signed
kit), messages, disciplines and offboards clippers. It never pays, never
counts or verifies views, never drafts legal text, never judges clips, and
never trusts another service's boolean about a clipper — it asks the owning
department (V&I for age, identity, connections, integrity, strikes and
certifications; Compliance for jurisdiction and activation; Finance for
the tax-form status, rate card and open items; Legal for the current
agreement version; Creative for the live rulebook and signed kit).

## Decisions

1. **One FastAPI service, compliance-py's conventions and code.** `src/`
   layout, pydantic v2, pinned requirements identical to compliance-py (a
   test compares the files), no new dependency. Copied unchanged except for
   names: `serve.py` (h11, 16 KiB head cap, head/idle timeouts, concurrency
   limit; env prefix `CN_`), `store.py` (hash-chained JSONL log),
   `ledger.py` (BUILD_CONTRACTS §2 client, department `clipper_network`,
   ids `cn-<abbrev>-<40 hex>`), `founder.py`, `clock.py`, `textguard.py`
   (plus money/earnings and contact detectors), `jurisdictions.py` and
   `data/iso3166.json`, the transport half of `api.py` (`InputLimits`,
   bearer check with `hmac.compare_digest` in `try/except TypeError`,
   `Callers`, error sanitizing) and `intelligences/i10_evidence_audit.py`
   (from compliance-py's `i11_evidence_audit.py`). Docs routes off; bind
   `127.0.0.1` (`CN_BIND_ADDR`).
2. **Four identities, never interchangeable.** Service bearer
   (`CN_SERVICE_TOKEN`, required to start); caller identity
   `X-CN-Caller-Token` (`CN_CALLER_TOKENS`: hub, onboarding,
   creative_production, verification_integrity, finance_31,
   compliance_38, scheduler); Andre's `X-Andre-Approval-Token`
   (`CN_ANDRE_APPROVAL_TOKEN`, FounderGate: a token equal to the service,
   a caller or a delegate token counts as not configured); delegate tokens
   `X-CN-Delegate-Token` (`CN_DELEGATE_TOKENS`, empty by default) that count
   only on the dispute-outcome route and only when People (43) confirms the
   delegate (the stand-in never does: Andre only). Every token, and the
   identity HMAC key, must be ≥ 32 printable ASCII and all must differ, or
   the service refuses to start. A refused Andre token is a 403 recorded as
   `founder_approval_refused`.
3. **The rules are data; the intelligences are code.** One register holds
   the rules, the counsel holds and the templates; one version counter;
   versions are immutable and chained (`prev_version_sha256`). The seed is
   one proposal; until Andre approves it every decision is
   `RULES_NOT_IN_FORCE` (CN-00). Only Andre proposes and approves (§0.1.5);
   a decision call is atomic.
4. **Record-first, everywhere** (ADR 0006 decision 4). Port calls are
   recorded as `crossing_<port>_requested` (ids and a hash of the
   non-sensitive arguments only) before they are made; then the operation's
   own events; then ONE local-log line holding every object the operation
   changes (a `batch` of whole-object puts) is anchored on the ledger,
   appended and fsynced, and only then applied and answered. Any failure →
   503 `{"issued": false}` and nothing changed (tested for every write
   route). Message delivery is a separate recorded step after the commit.
5. **Event-sourced state with the round-14/15 anchoring.** State is rebuilt
   by replaying the log; every line is anchored (`local_log_appended`,
   `cn-log-<epoch>-<seq>-<sha40>`), rule versions are recorded as
   `cn-ver-<epoch>-<n>-<hash32>`, every start of a disk log writes an
   `instance_lease`, and `i10_evidence_audit.assess` sorts mismatches into
   FATAL (rewrite, rollback of a published rule version, cited event
   missing, another log's anchors, empty log where the ledger anchors one,
   a reconcile record the ledger does not match) and VOIDABLE (stray anchor
   / truncated tail, a ruling on the ledger the log lacks, a newer lease
   from another instance). FATAL refuses start-up; VOIDABLE refuses until
   Andre reconciles (`CN_RECONCILE_MODE=1`, `GET`/`POST /cn/v1/reconcile`),
   exactly as compliance-py.
6. **Ports with fail-closed stand-ins; thin clients only for V&I,
   Compliance and Creative.** `ports.py` holds only `NotBuilt*`/`NotWired*`
   defaults; `httpclients.py` has the compliance38.py-pattern clients
   (10 s total budget, one retry with the same request, 1 MiB cap, no
   redirects, identity encoding only, echo checks of request id / facts hash
   / subject, `rules_pinned`/`seed_pinned` false refused unless the operator
   opts in), wired only when URL + service token (+ caller token) are all
   set. Passing fakes live in `tests/fakes.py` (G3 parses `src/`). A port
   that raises, or answers the wrong type, is unavailable.
7. **Contact data never enters the append-only log.** Email, display name
   and connected-account handles live in a separate, atomically replaced
   contact store (`cn_contacts.json`, mode 0600); the log records only their
   HMAC / SHA-256. See choice 5.
8. **Client text is data.** Strict models (unknown key → 422, so a caller's
   `age_verified`, `guardian_*` or `w9_on_file` is refused at the edge),
   bounded strings, control characters refused, ISO codes checked against
   the shipped lists, IANA time zones checked against the system database.
   Instruction-like text in a display name, application statement, appeal
   statement, announcement facts or proposal is recorded as
   `injection_text_ignored` (rule names and counts only) and changes
   nothing. Statements are kept only as SHA-256 and length.

## Choices made where the spec is silent (safest option; for Andre to confirm)

Numbered so code comments can cite them ("ADR 0008 choice N").

1. **§H values that are rule parameters stay with Andre.** Tier thresholds
   and caps (CN-10), the appeal window and SLA (CN-18), the S2 suspension
   length (CN-19), the rate-change notice (CN-17), the quiet window (CN-15),
   retention (CN-21) and "admission needs a connection" (CN-06) are
   parameters of seeded rules. The `CN_*` env names §H lists may only
   restate the pinned seed value; any other value refuses start-up and names
   the rule to propose instead (§0.1.5 outranks §0.2). `CN_CHANNELS` may
   only narrow the four default channels. `CN_MESSAGE_PROVIDER`,
   `CN_FINANCE_URL`, `CN_LEGAL_URL`, `CN_PEOPLE_URL`, `CN_HUB_URL` and
   `CN_PUSH_URL` must stay unset (no adapter is built). A partly configured
   thin client refuses start-up. `CN_IDENTITY_HMAC_KEY` is required.
2. **Weakening.** Every rule/template proposal carries `weakening` and
   reasons, recomputed at approval against the register as it stands then
   (N15-3); approval of a flagged one needs `acknowledge_weakening: true`.
   Flagged: a parameter moved in its weakening direction (a per-rule map:
   lower minimums, higher caps, removed exclusions, `true→false` on
   requirements; any unmapped parameter change), a statement or kind change,
   a retire, a template's added channel, changed purpose or variable type,
   or changed body. Refused outright (422): CN-01's guardian path (§0.1.1),
   retiring CN-00, changing a counsel row except by a counsel memo (not
   flagged — the spec's memo path), a parameter shape unlike the seed's, a
   new rule with parameters (code reads only seeded rules), a template that
   could carry money or drops a required element.
3. **An adverse item must cite a rule in force (G4).** An item whose rule is
   retired or absent is replaced by `CN-00 / RULE_NOT_IN_FORCE:<id>` — the
   decision stays negative; retiring a rule never silently passes a check.
4. **Templates.** Variables have closed types (`id`, `ids`, `rule_ids`,
   `date`, `int`, `word`, `sha256`, `until`, plus service-filled
   `display_name`, `automation_disclosure`, `postal_address`,
   `opt_out_link`); there is no text, money, amount or rate type. Required
   elements are enforced on every proposal (the CN-16 disclosure in
   `application_received` and `admission_decision`; "Advertisement", "18+
   only", the opt-out link and postal address in `recruiting_invite`; rule
   ids and an appeal deadline in every adverse notice). The CN-16 wording is
   a CN-16 parameter (counsel CN-CQ-08 updates it through Andre). A display
   name carrying money or earnings text is refused at intake (422) so no
   rendered message can carry it.
5. **Contact store.** The store is written BEFORE the log line; if the
   operation then fails, the entry is an orphan, removed at once (best
   effort) and at the next start. Values are never updated in place (each
   handle is its own key), so a hash the log recorded always matches or the
   file was edited: an edited value refuses start-up; a missing value makes
   that contact unavailable (no message can go to it) and is counted by
   `GET /cn/v1/integrity`.
6. **One identity per person.** A re-application while the identity is
   still an applicant updates that identity. An email CN already holds under
   an admitted, suspended, banned or refused identity creates a separate
   applicant record that admission refuses as `DUPLICATE_IDENTITY` (CN-03,
   source `clipper_network`), whatever V&I says; the first identity is
   untouched. An email of an identity Andre banned is refused as
   `BANNED_IDENTITY` (CN-20) even after that identity's exit. V&I's
   `incomplete` identity check is `IDENTITY_CHECK_INCOMPLETE` (CN-03).
   Emails are normalized lowercase + NFKC, no dot/plus folding (V&I C.7).
7. **Minors.** `declared_18_plus: false` → 422 and nothing is stored. A V&I
   `minor` answer — at the age-check relay or at admission — sets the
   clipper `refused` (the only status that is `refused`), refuses the open
   application with `AGE_NOT_ADULT` (CN-01), starts the exit with trigger
   `minor` (connections revoked at once) and deletes the contact data with
   no retention wait. The email HMAC is kept so no re-application path
   exists.
8. **Admission.** Every other unmet item leaves the clipper an `applicant`
   ("refused-waiting"). The facts sent to Compliance's `zbc_creator` rule
   are only those CN holds (jurisdiction with the application id as
   attestation ref, `network_country_signal: null` — CN never sees an IP —,
   recruitment channel mapped to inbound/referral, agreement version,
   training attested, accounts with a handle hash); Compliance names every
   other fact missing. The latest-activation read must return the ruling
   just issued. The admission-decision message is queued when the outcome
   changed since the clipper's previous ruling (always when admitted).
9. **Replays.** A replayed request id whose ruling took effect (admitted,
   enrolled) returns the stored answer; a replay of a negative ruling is
   re-evaluated and answered under a new id when the outcome changed
   (N14-15b). Ruling idempotency is rebuilt from the log at start. Hashes of
   a DOB, an OAuth code or state in the idempotency store use a per-process
   random key, never persisted.
10. **Agreement acceptance.** `box_ticked` must be true and
    `presented_sha256` must equal the document hash Legal names current;
    the record holds ids, version, hashes, time, method and a session-ref
    hash — no IP, user agent or text.
11. **Enrolment.** A stored jurisdiction resolve older than CN-12
    `jurisdiction_freshness_hours` (24) is re-asked; the campaign must be
    between `opens_at` and `closes_at`; one active enrolment per campaign;
    caps count active and paused enrolments; the campaign's Compliance
    activation is read with the latest-activation read; CN-CQ-01 blocks
    every enrolment and CN-CQ-03 blocks SAG-AFTRA members' until memos.
12. **Network config.** Platforms must be CN-06 enabled platforms (X is
    refused while its V&I flag is off). The first rate card needs no notice;
    a changed `rate_card_ref` must take effect ≥ CN-17 days ahead (else 409
    citing CN-17, recorded as `network_config_refused`) and is announced
    (`rate_card_changed`) to active and paused enrolments; the effective date
    changes only with a new reference.
13. **Rulebook announcements** (Creative's `announce_rulebook_version`):
    `allowed` is true only when every active enrolment got a message queued
    and recorded and none was refused by the provider (a message deferred
    for quiet hours counts as queued); with zero active enrolments the
    announcement is recorded and allowed. A new kit delivery is made only
    when Creative's kit is signed for the announced version.
14. **Quiet hours.** The recipient-local window applies to every channel;
    in-app without a time zone is sent at once (it waits in the hub);
    a message found outside the window at flush is re-deferred; up to 5
    delivery attempts, then `failed` (variables dropped). A rendered body is
    never stored (its SHA-256 is).
15. **Member messages** go to applicants, active and suspended clippers;
    once offboarding/banned/refused only exit notices (ban, offboarding,
    export, appeal) go out; nothing after `offboarded`.
16. **Recruiting.** Campaign recipients are stored only as HMACs; a phone
    number is `SMS_OFF` (CN-09); an email needs an active opt-in not
    suppressed (CN-08); recipients whose opt-in country is in CN-08
    `refuse_recipient_countries` (CA) are refused; the opt-in form must
    confirm 18+ and give a time zone; an opt-out is honoured at once and
    deletes that email's opt-in contacts; a new explicit opt-in lifts the
    suppression. Every send first asks Compliance's publish gate as
    `email_campaign` with the facts CN holds; `CN_POSTAL_ADDRESS` and
    `CN_OPT_OUT_URL` must be configured. **Discord server posts are refused**
    (`COMPLIANCE_ASSET_TYPE_MISSING`): Compliance's publish gate has no asset
    type for them and the spec requires the gate for recruiting mail.
17. **Disputes.** An appeal names the notice message it answers (a CN
    message of the matching template sent to that clipper) and a subject
    that notice was about; one per subject; the window runs from the
    notice; the SLA counts Mon–Fri business days (no holiday calendar).
    Routing: clip flags, strikes and V&I findings → V&I; bans, suspensions,
    tiers, enrolment and admission → CN (Andre or a confirmed delegate).
    A granted suspension appeal lifts that suspension.
18. **Discipline.** A strike is valid only if it carries finding ids and
    evidence ids, every finding exists at V&I, belongs to the same clipper
    and is upheld, and every evidence id of the strike appears in a
    resolved finding. A strike for a clipper CN does not hold is refused.
    Strikes on banned/exiting clippers are mirrored only. S2: enrolment
    suspension for the CN-19 days and tier cap T0. S3: status `suspended`,
    tier T0, active enrolments `paused`, ban proposal to Andre. Overturned:
    the strike's suspension is lifted, paused enrolments resume, a pending
    ban proposal is withdrawn. The feed cursor is kept.
19. **Bans.** Andre's approval sets `banned`, withdraws enrolments, sends
    the ban notice, calls `POST /vi/v1/bans` (a failure leaves the ban in
    force with propagation `pending`, retried by `/cn/v1/offboarding/run`)
    and starts the exit (trigger `ban`).
20. **Tiers.** The tier is the highest whose entry holds at each run
    (so a tier is lost when its entry no longer holds); T3 needs Andre's
    nomination (`POST /cn/v1/clippers/{id}/tier-nomination`, a route the
    spec implies but does not list); YouTube certifications are left out of
    the median while V&I VI-CQ-01 is open (CN-10
    `median_exclude_platforms`); a revised certification counts with its
    current value.
21. **Offboarding.** Runs without a rule version in force for steps 1–4
    (stop, access, Finance, export — they only remove access); deletion
    waits for CN-21 to be in force. Voluntary exits keep connections by
    default while V&I reports a pending/certified/revised certification
    whose `revision_watch_end` is ahead (V&I unavailable → treated as
    unsettled); ban/minor revoke at once. CN-side access ends at once; the
    hub step is `pending_dependency` while the hub is a stand-in. The record
    closes only when contact data was deleted, Finance answered `none`, no
    dispute is open and no connection is kept. The export is served live to
    the hub (`GET /cn/v1/clippers/{id}/export`); the exit records its hash.
22. **Routes the spec implies but does not list:** `GET /cn/v1/inbox`,
    `GET /cn/v1/rules`, `GET /cn/v1/integrity` (log chain, ledger verify,
    anchor problems — CN has no control catalog), `POST
    /cn/v1/disputes/sla-run`, `POST /cn/v1/offboarding/run`, `POST
    /cn/v1/clippers/{id}/tier-nomination`, `GET
    /cn/v1/clippers/{id}/export`, `GET`/`POST /cn/v1/reconcile`,
    `GET /intelligences`.
23. **Event types added to §F:** `rules_seed_loaded`,
    `rules_proposal_created`, `rules_proposal_approved`,
    `rules_proposal_rejected`, `connection_relayed`, `age_status_mirrored`,
    `recruiting_campaign_defined`, `network_config_refused`,
    `strike_refused`, `tier_nominated`. (`rules_version_published`,
    `template_version_published` and the rest are §F's.)
24. **Kit hash.** `kit_sha256` is the SHA-256 of Creative's kit response
    bytes exactly as read (CN never edits or re-serializes the kit).
25. **Application intake before rules are in force** is recorded (it is
    not a decision); messages start once a rule version is in force.
26. **Request limits:** recruiting campaigns 512 KiB (1,000 recipients),
    rule/template proposals 64 KiB, decisions 32 KiB, disputes 48 KiB,
    reconcile 1 MiB, everything else 16 KiB; JSON depth ≤ 32, members
    ≤ 20,000; JSON bodies only.

## Amendments — AEGIS round 16 (fix wave 17, Sep 26, 2026)

- **N16-2 — ban propagation carries Andre's own token.** V&I's `POST /vi/v1/bans` needs the clipper_network
  caller token AND Andre's approval token. `ban-decision` passes the exact `X-Andre-Approval-Token` Andre sent
  (already verified by the FounderGate) to the V&I client for that one call; it is never stored (not in a record,
  the idempotency store, a crossing payload, a log line or an export — byte-scanned in the test and the live run). A
  request without it is refused (403) before any effect. The scheduler holds no token, so `offboarding/run` no
  longer propagates (`needs_andre`); Andre re-sends `approve` on the approved proposal and CN propagates with that
  request's token (V&I's request-id idempotency makes the retry safe). Deployment: `VI_ANDRE_APPROVAL_TOKEN` must
  be Andre's same token as `CN_ANDRE_APPROVAL_TOKEN`.
- **N16-4 — CN-21 holds on day one.** The exit deadline (`post_exit_retention_days`, 0 for a minor) is fixed when
  the exit starts. Contact data and handles are deleted at the deadline WHATEVER Finance answers (spec C.9 said
  "unless … an open Finance item"; amended by the lead): an unresolved Finance question (open, unknown, or the
  stand-in) is recorded (`finance_question`) and pushed to Andre then; the record still cannot close until Finance
  answers `none`. Connections "kept until my last settlement" get a hard end: `revoke_after` = the last
  `revision_watch_end` or the deadline, whichever is first (the deadline when V&I cannot answer). An open dispute
  still delays deletion (spec C.9, unchanged). A 365-day property test covers Finance stand-in / open / late-none
  and kept / not-kept connections.
- **N16-5 — one appeal per underlying flag.** A V&I-routed appeal (clip flag, V&I finding, strike) records
  `appeal_keys`: the ref plus every strike id, finding id and clip id linked to it in the strike mirror and in the
  notice's subject refs; any earlier non-refused appeal of the clipper sharing a key refuses the new one
  (`DISPUTE_ALREADY_FILED`), whichever kind is chosen. CN-routed appeals keep their (kind, ref) key.
- **N16-6 — the V&I client reads V&I's real wire shapes** (see ADR 0007 N16-6 for the V&I side):
  `/age/subjects` `{allowed, status, attestation_id, …}`, `/age/checks` `{request_id, facts_sha256 (no DOB),
  status, …}`, `/identity/checks` `{request_id, status: clear|finding|incomplete, clear, findings}` (V&I's
  `finding` is CN's `duplicate`), `/connections` `{clipper_id, items}`, `/connections/complete` and `/revoke`
  `{request_id, connection: {…}}`, `/clippers/{id}/integrity` as V&I's facts (clear = not banned, no active S3,
  no open clipper hold), `/strikes` `{items, next_cursor: int}` (an `active` strike past `expires_at` is read as
  `expired`), `/findings/{id}`, `/certifications?clipper_id=`, `/bans` `{request_id, clipper_id}`.
  `tests/test_contract_vi.py` runs this client against V&I's real app in-process (TestClient transport; 27 checks,
  every port method exercised), so a drift on either side fails a test.
- **N16-7** — as ADR 0007 N16-7 (shared evidence-audit code): a forged `rules_version_published` event makes
  start-up refuse until Andre voids it through the recorded reconcile; it no longer bricks the service. This
  changes the "Known limitations" residual below: a ledger-token holder can still make start-up refuse, and
  Andre's reconcile now lifts it for version events too.
- **N16-9 — `allowed` is the authority on an age answer**: adult only when V&I says `allowed: true` AND
  `status: adult`; any contradiction is not adult (unavailable).
- **N16-10 — the live run asserts** every narrated behaviour and exits 1 on any mismatch.
- **N16-11 — display names are names.** Letters (any script, with their marks), digits, space and `. ' -`; ≤ 80;
  at least one letter; no leading/trailing/doubled spaces; no URL- or domain-like text; no format/bidi controls
  (Unicode Cf) or line separators; no money words — else 422 at intake. At render the stored name is re-checked
  and escaped for the channel: HTML for email and in-app (the messaging provider must send those bodies as
  HTML), Markdown for Discord.

## Spec items not built, and why

- **Message triggers with no source yet:** `certification_result`,
  `clip_flagged` (clip flags reach the clipper as `strike_notice`, whose
  subject refs carry the submission ids), `agreement_new_version` (Legal 37
  has no change feed) and `rate_card_published` (a first rate card reaches
  clippers in `kit_delivered`). The templates exist and are approved with
  the seed.
- **Tier-based rate-card visibility (T2/T3 rate cards)** — Finance has no
  rate-card documents per tier; the config carries one reference.
- **Platform anchors for tiers** — off by spec (`platform_anchors: false`).
- **Messaging provider, hub, push, Finance, Legal, People adapters** — not
  built (stand-ins only); V&I, Compliance and Creative thin clients are
  built but their services lack routes CN needs (see "Changes").
- **Referral-link generation** — only the referral channel's check (an
  active referrer) is built.
- **Discord recruiting posts** — refused (choice 16).
- **Outcomes of V&I-routed clip-flag and strike appeals** — V&I has no
  route that returns an appeal's outcome; CN reads only a finding's status
  change (for `vi_finding` appeals). The others stay open until V&I has one
  (an open dispute also delays CN-21 deletion — see Known limitations).
- **Reinstatement after a granted ban appeal** — recorded; the clipper
  re-applies (the exit already revoked access).
- **SLA breach escalation beyond the day-8 push.**
- **`max_submissions_per_clipper` and `view_terms`** are stored and
  delivered with the config; enforcing them is V&I's and Creative's.

## Changes other services must make (listed, NOT made in this build)

As the spec lists (creative-py `HttpClipperNetwork` and a kit read route
with caller identity; compliance-py caller name `clipper_network`, `/rule`
for `zbc_creator`, `/jurisdictions/resolve`, `/review` for
`email_campaign`, and `GET /compliance/v1/activations/{lane}/{subject_id}/latest`;
onboarding-py's creator lane handing off to `POST /cn/v1/applications` and
`/admission`; Finance, Legal and People when built), plus what this build
found:
- **V&I:** DONE in fix wave 17 (AEGIS N16-6, both services in one branch):
  age answers carry `status` and `attestation_id`; `GET /vi/v1/findings/{id}`
  and `GET /vi/v1/certifications?clipper_id=` (with `revision_watch_end`)
  exist; every write answer echoes `request_id`; every answer CN reads
  carries `rules_pinned`; the strike feed carries evidence ids and clip refs.
  Integrity stays V&I's facts (CN derives "clear"). STILL NEEDED: an
  appeal-outcome read (clip-flag and strike appeals).
- **Compliance:** a publish-gate asset type for a Discord server post.

## Known limitations

- Single process, one lock around every operation; state rebuilt from the
  log (fine for this department's volume; not horizontally scalable). One
  disk-backed instance per ledger (a second one reads as a foreign log).
- A delivery whose `message_sent` record fails after the provider accepted
  it stays queued and is sent again by the next flush (the provider must
  de-duplicate by `message_id`).
- A copied data directory started while the original is idle is detected
  only once one of them writes (leases are checked at start and by
  `/cn/v1/integrity`).
- Residuals stated in ADR 0006 N15-1 apply: a holder of BOTH the ledger
  token and write access to the data directory can forge a consistent
  reconcile; any ledger-token holder can make start-up refuse (DoS lifted
  only by Andre's reconcile — for forged version events too since N16-7).
- Remote calls are made while the service lock is held (the AEGIS N16-1
  class, fixed in V&I, NOT here): admission (up to eight V&I / Compliance /
  Legal / Finance calls), the connection and age relays, enrolment,
  discipline sync, tiers, disputes and offboarding each call their ports
  inside the lock, so a slow V&I or Compliance (each call bounded to 10 s
  wall clock) stalls every other CN route for that long. Fixing it needs an
  I/O phase before the lock per operation (as V&I's Compliance prefetch).
- An undecided dispute delays CN-21 deletion without a bound (spec C.9);
  the SLA push (day 8) is the only pressure.
- The contact store is integrity-checked against the log's hashes but not
  encrypted at rest (file mode 0600 only).
- Quiet hours use the time zone the clipper declared; the business-day SLA
  has no holiday calendar.
- The live run's full flow uses the test fakes through
  `devtools/live_server.py` (a fixture file drives time, certifications and
  strikes); the production entrypoint is shown blocked on stand-ins.

## Unlock list — what must flip before a real clipper can be admitted

1. Andre approves the seed (`POST /cn/v1/rules/decisions`) — rule version 1.
2. V&I built, with `CN_VI_URL` / `CN_VI_SERVICE_TOKEN` /
   `CN_VI_CALLER_TOKEN` set and the V&I routes above, its token vault
   (connections) and an age-assurance provider live.
3. Compliance: caller `clipper_network` added, the latest-activation route,
   and `CN_COMPLIANCE_*` set; Compliance's own creator activation needs its
   sanctions provider, V&I, Finance and counsel rows (ADR 0006).
4. Finance (31) built (tax form status; rate cards and open items for
   enrolment and exits) — no adapter exists in CN yet.
5. Legal (37) built with the Clipper Agreement (spec §D) versioned by
   SHA-256 — no adapter exists in CN yet.
6. Creative: kit read route with caller identity and `CN_CREATIVE_*` set
   (enrolment).
7. Counsel memos approved by Andre: CN-CQ-01 (talent agency — blocks every
   enrolment), CN-CQ-03 (SAG-AFTRA members' enrolment), CN-CQ-06 (email to
   Canadian clippers), CN-CQ-08 (disclosure wording, update CN-16);
   CN-CQ-05 / CN-CQ-07 only if Reddit, X or SMS are ever wanted.
8. A messaging provider adapter (and vendor monitoring, CAN-SPAM) and the
   clipper hub; `CN_POSTAL_ADDRESS` and `CN_OPT_OUT_URL` for recruiting.
9. People (43) for delegates (otherwise disputes are Andre's alone).

## Testing

`cd services/clipper-network-py && python3 -m pytest -q` — spec §G S1–S10
(`test_cert_scenario.py`), A1–A12 (`test_cert_attack.py`), G1–G6
(`test_cert_guardrail.py`), properties (no admission or enrolment while any
input is absent, a stand-in, exploding or negative; no message to the
provider without consent or outside the recipient-local window), auth on
every route, limits, idempotency, ledger/store/contact-store failure = no
effect on every write route, reconcile / anchor / lease / contact-store
tamper, rules register and weakening, thin clients (httpx MockTransport),
workflows and a no-500 fuzz. A socket guard fails any test that opens a
connection. Live run: `LEDGER_BIN=… python3 devtools/live_run.py --ports
19350,19351,19352,19353`.

## Fix wave 25 amendments (Oct 1, 2026; scout B)

- `CN_VI_ACCEPT_UNPINNED` / `CN_COMPLIANCE_ACCEPT_UNPINNED` (read only when that thin client is configured; then
  0/1, default 0, anything else refuses to start) make the V&I / Compliance thin client accept an answer that states
  `rules_pinned: false` / `seed_pinned: false`. They were documented nowhere; the README now names them, and the
  serve.py tuning knobs, as test/staging-only (nothing in `/health` shows them). `tests/test_env_documented.py`
  fails on any `CN_*` variable `src/` reads that README and this ADR do not name — a switch that unlocks or weakens
  something must be named exactly (a documented `CN_COMPLIANCE_*` glob had "covered" the Compliance switch) — and
  holds the code to the README's description of the two switches.
- `service.py` passed the OAuth code/state and the DOB to the V&I call through closures over names it `del`-eted
  right after the call (ruff F821): safe only while `PortCalls.call` runs the function synchronously and once. They
  are now bound into the call (lambda defaults); the frame's names are still deleted.
- `tests/test_cert_attack.py` A10: the age-check replay went to an ACTIVE clipper, refused 409 CN-21 on both calls,
  so the replay's 409 proved nothing about request-id idempotency; it now uses an applicant (200, then 409 that is
  not CN-21). The log-tamper guardrail expects the store's `StoreCorrupt`, not any exception mentioning "hash".
- `tests/contract_vi_runner.py` (run outside pytest, so the conftest socket guard never reached it) installs its own
  guard, as legal-py's contract runner does; the G5 seed test no longer names a literal `/tmp` path.

## Bug sweep C fixes (Oct 7, 2026; sweep of integration 5d49ee9)

Every fix is pinned by a test in `services/clipper-network-py/tests/test_sweep_c.py` that fails on 5d49ee9 (the
sweep's probes were lost; each finding was re-derived from the code first). Live run: `devtools/live_run.py` adds
one check (`GET /cn/v1/audit/evidence`: no `attempted` event after the whole walk).

| Id | Finding | Fix |
|---|---|---|
| R6 | Evidence ids left out the payload hash (e.g. `ban_approved_by_andre`, `message_queued`): a retry after the state moved (the notice's send window, a changed outcome) got the ledger's 409 → a lasting 503. `data_exported`'s id carried the wall clock — the premise "lasting 409" does not hold there: each retry was instead a NEW, unnamed ledger event | The bizdev R6 pattern in `_record`: the given id is the action key `rk`; the recorded id is `i10_evidence_audit.evidence_id(rk, type, payload_sha256, actor/subject/summary hash)` over a payload carrying `rk` and `seq` (no clock added; `data_exported` keyed by the export's hash). The committing line names its evidence (`data.evidence`); `GET /cn/v1/audit/evidence` classifies every event `committed` / `cited` / `attempted`; `i10.assess` does not count an earlier attempt of a committed action as a ghost (bound by ledger position). Admission and enrolment ruling ids stay raw (callers read rulings by them) and, like compliance-py's N14-15b, a 409 on the request's first id issues under the outcome-derived id; a ruling recorded by a try that never committed is named by the next line (`attempted_event_ids`). Crossings, anchors, leases, reconciles and version events keep their exact ids |
| E-5/F-3 | Old `store.py`: one fsync error left a line on disk that memory did not hold and bricked the log for good; no single-writer lock; no `close()` | finance-py's `store.py` backported (exact-size append, adopt/truncate, short-write cut-back, `fault` → `LOCAL_LOG_WRITE_FAULT`, `O_NOFOLLOW`, empty line refuses start, `DataDirLock` on `cn.lock`); `config.load` takes the flock, `api.build_service` claims it, `CNService.close()` releases it and makes the instance inert (503) |
| M (lock) | `integrity()` ran the ledger's verify and entries (HTTP) and the chain re-read (disk) under the service lock | All three run outside the lock (re-read once if a commit landed meanwhile); only the comparison runs under it |
| M (homoglyph) | The CN-26 money blocklist (`textguard.money_or_earnings`, templates, variables, display names) was NFKC-only: `еarn cаsh` (Cyrillic), `g​uaranteed`, `éarn`, `5 υsd` passed | `fold_for_matching`: NFKC, every format / Default_Ignorable character removed, diacritics stripped, casefold, confusables mapped to Latin (creative-py's `shared/text.py` CONFUSABLES table, copied; plus the generated "LATIN ... LETTER X WITH ..." folds). `money_or_earnings` matches the plain NFKC view AND the folded view; ordinary names in other scripts still pass |
