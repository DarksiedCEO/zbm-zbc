# ADR 0006 — Compliance Department (38) Architecture

**Status:** Accepted for build (Sep 26, 2026). **Not certified for any real
client, clipper, payout or publish.** Every outside dependency (Verification
and Integrity, Finance 31, Legal 37, the OFAC screening provider, the
accessibility checker) is a fail-closed stand-in, so on day one the service
blocks most things — by design (spec §D "Day-one effect").
**Service:** `services/compliance-py`
**Spec:** Compliance department (38) locked build spec, rev 1 (Sep 26, 2026),
with the seed `compliance_obligations_seed.json` (118 rows, SHA-256
`4e3821d0…845f584d`, copied unchanged to `services/compliance-py/seed/`).

## Context

Compliance is the second line (IIA Three Lines). It owns the obligation
register, runs the hard gates before **activation** (Onboarding), **payout**
(Creative's payout eligibility) and **publish** (Creative's ZBM gate),
monitors controls (Vanta-style) and records evidence. It never drafts policy
(Legal 37), engineers security (Cybersecurity 22), detects view fraud (V&I),
or touches money (Finance 31 pays). Every block cites a register obligation
id and that row's source URL. Only Andre approves anything into force.

## Decisions

1. **One FastAPI service, same conventions as onboarding-py/creative-py.**
   `src/` layout, pydantic v2, pinned requirements identical to theirs, no new
   dependency. Bearer auth on every route but `/health`
   (`hmac.compare_digest` in `try/except TypeError`: a non-ASCII token is a
   401, never a 500); docs routes off; bind `127.0.0.1`
   (`COMPLIANCE_BIND_ADDR`), port 8380; the hardened launcher copied from
   onboarding-py (`src/serve.py`: h11, 16 KiB head cap, head/idle timeouts,
   concurrency limit).

2. **Three identities, never interchangeable.** The service bearer
   (`COMPLIANCE_SERVICE_TOKEN`, required to start); a caller identity from
   `X-Compliance-Caller-Token` (`COMPLIANCE_CALLER_TOKENS`, JSON
   name→token for onboarding, creative_production, finance_31,
   verification_integrity, legal_37, cybersecurity_22, people_43, vendor_33,
   scheduler; each ≥ 32 printable ASCII, all distinct, none equal to the
   service or Andre token, else refuse to start; digests compared against
   every token with no early exit); and Andre's approval token
   (`X-Andre-Approval-Token`, `COMPLIANCE_ANDRE_APPROVAL_TOKEN`, the
   creative-py `FounderGate`, extended so a token equal to the service token
   or any caller token counts as not configured). A wrong caller is 403; a
   refused Andre token is 403 and is recorded as `founder_approval_refused`
   (best effort — a refusal stands whether or not it could be recorded).

3. **The register is data; the engine is code.** Rows are the seed's dicts,
   validated strictly (`register.ObligationRow`) and hashed canonically
   (`rows_sha256` over rows sorted by id, `sort_keys`, `(",", ":")`). A
   version is immutable (`register.Version`), chained by
   `prev_version_sha256` (SHA-256 of the previous version's metadata). The
   gate algorithm (spec C.2) is one engine (`intelligences/engine.py`) shared
   by the three gate intelligences; each C.10 check is a pure function of an
   evaluation context. Applicability follows B.2 exactly (a dimension the
   context does not carry does not restrict); effective status follows B.3
   (verified and `today >= expires_at` → expired, derived at read time).

4. **Record-first, everywhere.** Every state change follows one order: each
   port call is recorded on the ledger first (`crossing_<port>_requested`,
   payload = ids and an argument hash only); then the change's own events;
   then the local log append (fsynced); only then is it applied and answered.
   Any failure → 503 `{"issued": false}` and nothing changed (tested for
   every write route, `tests/test_ledger_no_effect.py`). Event ids are
   deterministic (`cmp-<abbrev>-<40 hex>` derived from request id / proposal
   id / ruling id; the ruling event id IS the ruling id), so a retry after a
   lost answer gets the ledger's 200, not a duplicate.

5. **Event-sourced local state.** `COMPLIANCE_DATA_DIR/compliance_log.jsonl`
   is append-only; each line carries `prev_line_sha256` and its own
   `record_sha256`; the chain is verified at start (any mismatch, deletion,
   reordering or torn line refuses start-up) and by control C-11. All
   in-memory state (versions, proposals, rulings, screens, accessibility
   results, control results, holds, watcher snapshots) is rebuilt by
   replaying the log. Without a data dir the log is in memory, `/health`
   says `in_memory: true`, and after a restart nothing is in force.

6. **Ports with fail-closed stand-ins; no passing fake in `src/`.**
   `ports.py` holds only `NotBuilt*`/`NotWired*` classes; the passing fakes
   live in `tests/fakes.py` (guardrail test H.28 parses `src/` and fails on
   any `Fake*`/`Passing*` class). A port that raises is "unavailable", never
   a pass. A provider answer of `available=False` is an unmet
   `dependency_unavailable:<port>`.

7. **Seed load and approval.** At start the seed file's SHA-256 must equal
   `COMPLIANCE_SEED_SHA256` (default: the spec's hash) or the service refuses
   to start; one `seed` proposal is created (`seed_loaded`,
   `register_proposal_created`). Until Andre approves it, every gate answers
   blocked with exactly one item, `register_not_in_force` citing HR-04.

8. **Proposals and decisions.** Only Andre's decision route changes the
   register. Proposals come from legal_37 (caller token), Andre (token), i01
   (re-verification drafts) and i06 (Change Watcher). B.5 validation is
   enforced; `expires_at` is always recomputed; `approved_by`, `approved_at`,
   `in_force`, `register_version` are unknown fields → 422 (anywhere in the
   body or the row). A decision call (≤ 200 decisions) is atomic: all
   approvals build one new version or nothing applies (409 on a hash mismatch,
   a stale proposal, or a decided proposal; 404 on an unknown one).

9. **Controls are computed, never asserted.** Green only with a `pass`
   inside the SLA and every feeding obligation in force (A.4). Owners push
   results; Compliance-owned controls (C-01, C-02, C-04, C-05, C-11, C-12,
   C-15, C-17) are computed by `POST /compliance/v1/controls/internal/run`
   (scheduler). A red control listed in `blocks_gates` blocks with
   `control_red:<id>` citing its first obligation id. The trust center
   returns exactly `{control_id, title, status, last_passed_at}`.

10. **Change Watcher never changes the register.** fetch → raw and
    normalized SHA-256 → compare with the last snapshot → classify → draft an
    `amend` proposal that marks the row `unverified` with the new evidence
    attached (a changed rule is stale until Andre re-verifies it). The fetch
    port refuses before any request: non-https, hosts off the allowlist, X
    and Meta hosts (CQ-13), login/account paths, robots.txt disallow.
    GET only, no cookies, no redirects followed, 10 s, 5 MB.

11. **Thin clients in the two callers.** onboarding-py
    `integrations/compliance38.HttpComplianceDepartment` and creative-py
    `shared/compliance38.HttpCompliance38` implement the existing protocols
    against `POST /compliance/v1/rule` and `/review`. Wired only when
    `COMPLIANCE_SERVICE_URL`, `COMPLIANCE_SERVICE_TOKEN` and
    `COMPLIANCE_CALLER_TOKEN` are all set; otherwise the fail-closed stand-in
    stays. 10 s timeout, one retry with the same `request_id` on a transport
    error or 5xx, and any non-200, parse error or inconsistent answer maps to
    "not allowed". Nothing else in those services changed.

12. **Client text is data.** Strict schemas (unknown key → 422), bounded
    strings, control characters → 422, no evaluation or templating of client
    text. Injection patterns (onboarding-py's guardrail family, copied) found
    in facts, `caller_context` or watched pages are recorded as
    `injection_text_ignored` (rule names and counts only) and change nothing.
    `caller_context` (≤ 8 KB) is stored only as its SHA-256.

## Choices made where the spec is silent (safest option; for Andre to confirm)

Numbered so code comments can cite them ("ADR 0006 choice N").

1. **Clip Review not `pass`** (A.2 item 1) has no code in the C.3 list; it is
   reported as code `clip_review_pass` citing HR-03.
2. **Flags that do not exist in a lane are fixed, not defaulted** (B.2):
   creators have no claims (`claims_present`/`health_or_earnings_claim` =
   false in `zbc_creator`); client lanes have no payee (`entity_payee`,
   `foreign_payee` = false in `client`/`zbc_brand`). Every other flag a row
   can name is a required fact (a test asserts the whole list).
3. **Title length 240, not 160** (B.1): three seed rows (CQ-02 223,
   CQ-05 173, CQ-11 165) carry the report's questions verbatim (§E) and the
   seed is binding and unchanged.
4. **House rules** (HR-*) may be proposed only by Andre, as `source_quality:
   founder`, no source URL, no evidence; that is how HR-05/06/07 jurisdiction
   lists change. Counsel questions are never verified directly; they are
   superseded by a counsel-memo row (§E).
5. **EU creators** (declared country in DE/NL/IE/ES/IT) must acknowledge the
   current EU-kit version at activation (`eu_kit_version_acknowledged` added
   to the creator facts); the MSA clause applies to client lanes only. A local
   label row at activation (NL-CVDM) requires the current EU kit.
6. **Lane scope of two creator/client rows:** PLT-TT-03 (accounts disclosed)
   applies to the `zbc_creator` lane only (A.1 table); the prohibited-category
   check does not apply to creators (the campaign's category is checked at
   brand activation and at payout).
7. **Prohibited-category table** keyed by platform row (C.1): PLT-TT-02 and
   PLT-X-02 lists from their verified obligation text; PLT-X-02's "AU/EU/UK
   limits" (financial products, crypto, gambling) are refused when any target
   is AU, GB or EU5.
8. **C-04 blocks creator activation and payout only** (H.15 says "payout and
   creator activation"); client and brand activation are not screened
   (screening clients is an open item, §I).
9. **OFAC comprehensively sanctioned set** = HR-07 `refuse` minus FR and
   CA-QC; those codes also cite US-OFAC-04. Bare CA and CA-QC also cite HR-05
   (its `operate_excludes`).
10. **Holds:** a network-signal mismatch hold cites HR-05 and is keyed by
    (subject, declared, signal) — after Andre releases it the same pair does
    not reopen it; sanctions holds cite US-OFAC-01 and sit on the payee
    (for an owner screen, on `owner_of`). Payout checks holds on the clip,
    the clipper and the campaign; publish on the work and the client.
11. **Payout context:** jurisdictions = clipper + campaign client + campaign
    targets (audience = targets); Compliance also enforces the HR-13
    settlement lag itself (`now ≥ posted_at + settlement_lag_days`) before
    asking V&I.
12. **Change Watcher classification:** among domains whose watch terms hit,
    only those with the most distinct hits are kept (the Consumer Review Rule
    hits `reviews_metrics` twice and `advertising_disclosure` once → the four
    Part 465 rows, not the Part 255 rows); watch terms match their plural;
    a feed source only targets rows under its jurisdiction (FTC/FCC/IRS/DOJ →
    US, legislation.gov.uk → GB, Canada Gazette → CA). Page sources also map
    rows explicitly: the two CPPA pages → US-CPPA-2025 and US-PRIV-CA (no row
    has those URLs, and the spec's CPPA regression fixture needs a target);
    the YouTube changelog → PLT-YT-01/02. The first fetch of a source is a
    baseline. An open page proposal for the same (source, row) is redrafted
    in place (new content hash; a decision against the old hash is 409).
13. **Re-verification drafts (i01)** are made only from a watcher snapshot of
    the row's own `source_url` taken within the last 2 days, and only for
    primary/vendor rows; otherwise no draft and C-01 goes red (Andre supplies
    evidence through a proposal).
14. **`retire`** sets the row's status to `superseded` with no replacement.
15. **Proposal `diff`, `proposed_by`, `created_at`, `proposal_id`,
    `content_sha256`** are server-set; a client may not send them (422).
    Proposals carry one extra hashed field, `watch` (source, detection time,
    detection latency, the feed's effective date or a flag for Andre).
16. **Stale proposals:** at decision time every `diff.old` value must still
    equal the current row, else 409 and nothing applies.
17. **A control-only decision** changes the catalog and publishes no new
    register version.
18. **Disclosure checks on publish** apply only when `paid_or_endorsement`;
    `disclosure_present` requires a non-blank in-video label.
19. **AI/synthetic disclosure** (US-NY-396B, EU-AIACT-50): the C.1 schema
    has no field that evidences such a disclosure, so the check always blocks
    when it applies (consistent with the day-one effect in §D).
20. **Publish needs the client's activation** (a `client`-lane ruling whose
    subject is `client_id`) when a target is EU5 or a platform is TikTok/X
    (category and EU kit come from it). `dm_campaign` requires
    `recipient_countries` and `recipients_cold`.
21. **Fact typing:** a wrong-typed or wrongly formatted value is
    `fact_missing:<key>` (C.1); an over-long string, too many list items or a
    control character is 422 (malformed input); an incomplete list item or
    document object makes its container `fact_missing`.
22. **Idempotency** is keyed by (caller, `request_id`) across routes; only
    2xx answers are stored; the store is in memory (bounded to 200k ids) and
    is not replayed after a restart.
23. **Request limits:** gate routes 256 KiB, proposals 128 KiB, decisions
    64 KiB, everything else 16 KiB (1 MiB service cap); JSON depth ≤ 32 and
    ≤ 20,000 members; bodies must be `application/json` or `+json` (415).
24. **Accessibility port** returns two extra facts the gate needs:
    `covers_captions` (video) and `overlay_scripts_disabled`; an unavailable
    check is stored as not passed.
25. **Sanctions:** activation uses the screen id named in the facts and
    requires it be a payee screen of the same subject; payout uses the
    latest payee screen of the clipper (by record order). A screen time in the
    future is not fresh.
26. **Control results:** Compliance-owned controls cannot be pushed (403);
    `site_owner` resolves through `COMPLIANCE_SITE_OWNER_CALLER`; `andre`
    controls take Andre's token; `tested_at` more than 5 minutes in the future
    is 422. `control_status_changed` is recorded when a result is recorded
    (a control that goes red by SLA timeout alone is red at read time, with no
    event).
27. **C-12, C-15, C-17** have no evidence intake in this build; C-12 and C-17
    fail, C-15 fails whenever a row takes effect within 60 days. (All three
    are red anyway through unverified feeding obligations.)
28. **`obligation_expired`** is recorded once per (row, `expires_at`), at the
    first gate evaluation or internal control run that sees it.
29. **Config that asks for something not built refuses to start:**
    `COMPLIANCE_SANCTIONS_PROVIDER` / `COMPLIANCE_A11Y_PROVIDER` other than
    unset/`none`, `COMPLIANCE_AUTO_REVERIFY_UNCHANGED=1`,
    `COMPLIANCE_WAYBACK_CAPTURE=1`.
30. **Audit export** is readable by any caller, so screen names/aliases,
    hold-release reasons and page text are exported only as hashes.
31. **`/jurisdictions/resolve`** records `jurisdiction_resolved` and notes a
    signal mismatch, but opens no hold (only a gate does).

## Spec items not built, and why

- **Caller changes (§F.2)** — onboarding passing C.1 facts and `dept.unmet`
  into i15, the i15 message, the campaign-approval call; creative passing
  payout/publish facts. The spec says "report, do not make them in this
  build"; only the thin clients (§F.1) were added. Until those callers
  change, Compliance answers blocked with every `fact_missing` named.
- **OpenStates/Plural and Regulations.gov API sources** — adapters not built
  (keys, pagination and response formats not specified).
- **Competition Bureau Atom feed and TikTok newsroom** — their URLs are not in
  the spec ("URL from notes"; "TikTok newsroom"); not guessed. EUR-Lex and
  California leginfo are excluded by the spec.
- **Real providers** for OFAC screening and WCAG checking (open items §I);
  auto-re-verification and Wayback capture (open items, default off).
- **Evidence intake** for C-12 (monthly counters), C-15 (owner action log),
  C-17 (review log).
- **An AI/synthetic-disclosure evidence fact** (choice 19).
- **Public trust center** — auth-only (open item).

## Known limitations

- Single process, one lock around every operation; in-memory state rebuilt
  from the log (fine for this department's volume; not horizontally scalable).
- The log's hash chain detects edits, deletions, reordering and torn writes;
  it does not stop someone who rewrites the whole file and recomputes every
  hash — the ledger (holding every event id) is the external anchor.
- If the ledger records an event and the local log write then fails, the
  ledger holds a record of something that did not take effect here; the
  identical retry (deterministic ids) completes it.
- The watcher runs every source sequentially inside one scheduler request.
- The live run's Change Watcher leg uses a devtools fixture fetcher
  (`devtools/live_server.py`), not the network.

## Testing

Certification tests §H 1–31 are in `tests/test_cert_scenario.py`,
`test_cert_attack.py`, `test_cert_guardrail.py` and `test_watcher.py` (H.14).
Also: property tests (every row an allowed ruling evaluated, and random
subsets of them, made expired or unverified → blocked and cited; any required
fact dropped → blocked and named; flags never defaulted), a no-500 fuzz of
all fact schemas, auth tests on every route (missing/wrong/non-ASCII bearer →
401), body-limit and malformed-input tests, idempotency, ledger/store failure
= no effect on every write route, and mutation checks that each gate's block
disappears when its check is removed. The gate tests were written after the
engine, not before it; the mutation checks are the evidence that they would
have failed without it. A socket guard makes any network use in a test fail.
