# ADR 0018 — The war room: a red-team launch gate for public-facing departments

Status: accepted for build, approved by Andre on Oct 7 2026; built on branch `warroom-redteam-gate`. In force for the
seven departments it has scenario libraries for (below; seo-py added Oct 10 2026, ADR 0017 W3-3; ooh-py added Oct 16 2026, ADR 0019; a second seo-py library, seo-py-priya, for Priya's keyword ledger (Search Console and Bing Webmaster Tools) added Nov 8 2026, ADR 0047, with the generative layer). Owner: department 41, Adversarial Testing & Red Team.

## Context

Every department that deals with the public has to be ready to work with people before it goes live: people who opt
out in odd ways, write in other languages, paste half a thread back, try to get paid twice, or hide instructions in
their messages. Each department's own suite pins the cases someone already thought of. Andre's idea (Oct 7 2026):
each public-facing department gets a war room, and the red team creates as much chaos as it can there so the
department gets better.

## Founder decisions (Oct 7 2026; binding)

| Question | Answer |
|---|---|
| Where | **Sandbox only.** Nothing real is sent, charged or revoked. |
| Who attacks | **Synthetic adversarial customers**: personas in scenario libraries, with seeded chaos. |
| Memory | **A replay library that only grows**: every failure becomes a permanent case. |
| Consequence | **A launch gate**: a department cannot go live until it passes. |
| Who grades | **Scored independently**, not self-graded: the war room's invariants, not the department's own report. |
| When | **Per release, in CI**, not continuously. |

## Decision

`devtools/warroom/` (standard library only; the services' dependencies are used only inside the workers):

1. **Scenario libraries** (`scenarios/<service>.json`, versioned data). A scenario names the target service, a persona,
   the seed phrases, ordered steps (HTTP calls through the service's own in-process app), the chaos transforms
   allowed, and invariants. Seed phrases are **extracted by AST** from the service's pinned tests and source (a named
   tuple, the n-th `for` loop over a literal, a `parametrize` table, a module constant such as `OPT_OUT_TERMS`), never
   retyped; a reference that no longer resolves is an ERROR, so a moved test cannot silently empty a scenario.
2. **Workers.** One process per service, started in the service's directory with its `src/` and `tests/` on the path
   (the services share module names: `service`, `api`, `store`, `helpers`), reusing the service's own test harness
   (`tests/helpers.py` `Harness`; onboarding-py: `tests/conftest.py` `make_service`). Each case gets a fresh harness
   and its own temporary directory.
3. **Invariants** are pure predicates over what a case saw: the responses AND the resulting state and evidence read
   back through the service's API or the harness's recording fakes (consents, alerts, suppression, holds, submissions,
   1099 totals, ledger events). Severity: **MUST** (gate-blocking) or **SHOULD** (scored and reported). Outcome per
   case: **PASS** (every MUST held), **FAIL** (a MUST did not), **ERROR** (the harness or a predicate crashed, or the
   worker never answered; never a pass).
4. **Chaos generators** (`chaos.py`), seeded and reproducible; each case records its transform chain; a case id
   (`<service>/<scenario>#<seed index>.<variant>@<global seed>`) rebuilds its input exactly. Variant 0 of every seed
   phrase is the unmutated phrase (the base case).
5. **Gate.** A department passes when every case PASSes, except replay-library cases marked with a triaged finding
   (`known_failure`), and no case ERRORs. `run.py` exits 1 when any department fails. CI runs `--all --seed 1` in the
   `warroom` job, required by the aggregate `required` job.
6. **Replay library** (`replay/<service>.json`): `--promote` appends a run's new MUST failures, materialised (the
   exact input sent, its chain, the case it came from), with `known_failure: null`, which still blocks the gate. A
   person files the finding in `findings.md` and writes its id into the entries. Promotion only appends. When the
   service is fixed the case passes, its mark is cleared, and it stays as a regression case. The self-test fails on
   an untriaged entry or a finding id `findings.md` lacks.
7. **LLM personas** are a port that answers `NOT_CONNECTED`. Nothing is generated or faked.

### The sandbox guarantee

The worker makes every socket connect raise before any service code is imported, clears the service's environment
variables and sets test-only values (as each conftest does), and runs every case on in-memory state with fake
ledgers, fixed clocks and recording fakes or the services' own fail-closed stand-ins: no provider, ledger, payment
rail or person is reachable. The self-test proves the network seal on a stand-in driver.

### Expected outcomes come from the departments' documents

The war room checks the current, AEGIS-certified guarantees under chaos; it does not invent policy. Every scenario
names its basis (the ADR decision, the pinned test). Where a transform takes a case outside what the department's
documents promise, the library lowers the affected MUST to SHOULD for that transform (`downgrade`, with the reason in
`downgrade_basis`): the miss is scored and reported, not gate-blocking. Examples: below an unmarked Outlook quote
service-py reads only strong wording (ADR 0014 N1, R2, M-1); a text over its cap keeps its head and tail, not its
middle; the wide lookalike set (Cherokee, insular, small capitals) is in no service-py or verification-py document.
Base cases (no chaos) must all PASS: a base FAIL means the library's expectation is wrong.

### Invariant model for the first departments

| Department | MUST | SHOULD |
|---|---|---|
| service-py (14) | inbound never refused; every clear opt-out revokes a consent or raises OPT_OUT_POSSIBLE / EMAIL_OPT_OUT_UNCLEAR / OPT_OUT_IN_QUOTED_TEXT / SMS_OPT_OUT_SUSPECTED / EMAIL_OPTED_OUT_BY_REQUEST; "email me instead" never revokes email; ordinary mail changes no consent; complaints never revoke email; injection text changes no consent; another contact's consents are untouched and its identifiers never appear in a response | email revoked for an email opt-out; SMS revoked for an SMS opt-out; no opt-out alert on ordinary mail |
| sales-py (13) | a reply is never refused; an opt-out reply is suppressed or held for a person (task); a disguised opt-out (lookalikes, full-width, invisible characters, leet, spacing, markup) is suppressed, a reply written in another script suppresses nothing (war room fixes, ADR 0013); another lead untouched and never echoed | the opt-out is suppressed |
| verification-py (33) | the same post under URL variants is refused as a duplicate of the first (one submission: paid once), also for another clipper; a second connected account and a shared account are refused; a minor's look-alike email (plus / dash tags, Gmail dots, googlemail, case, core homoglyphs, full-width) stays a minor or is refused | the sharer is held; invisible characters / wide lookalikes in the email |
| clipper-network-py (32) | a money or earnings word in a display name is refused (homoglyph, leet, spacing, invisible characters, full-width, accents) | ordinary names pass |
| onboarding-py (1) | one person's re-spelled legal name aggregates to one 1099 total (or the variant is refused at the schema); a replayed payment is idempotent and recorded once; the id reused with another amount or creator is 409 and records nothing | a spelled-out name |
| seo-py (2) | the audit answers (never a server error) whatever a hostile site serves; no request ever reaches a non-public address (private, loopback, link-local, CGNAT, mapped, NAT64, rebinding, numeric spellings; redirects, canonical links, robots / sitemap / llms.txt) and nothing an internal address would answer appears anywhere; odd ports, other schemes and credentials refused; compression bombs and odd codings contained within the byte budget; a trickled body stopped by the deadline; DOCTYPE / ENTITY XML quarantined; crawled or logged instructions change no state; uploaded logs keep nothing identifying (the suite's own PII needles, read through the shared lookalike fold) and quarantine broken lines; the hub never reaches another tenant (404 identical to a missing object, nothing echoed); personal-data keys refused 422 with nothing recorded; kill switches refuse with their own code, stop crawling before any request, interrupt a run mid-way, and only Andre releases them; a paid run is refused, unrecorded and uncrawled unless Finance (31) confirms the invoice | none: every invariant is MUST (the log-identifier downgrade was removed with the W3-3 templating fix) |
| seo-py-priya (2, ADR 0047) | no personal data or secret ever in a ledger row, its key, a frame, the hand-off, a response, a stored record or a log record (the suite's own attack corpus, tests/attack_corpus.py, and the round 8-10 drop tables, alone or inside ordinary queries, through NFKC, digit folding and the shared fold; the harness's live tokens and keys); query text never persisted (data directory, ledger events, the append-only log, the service's log records); never invented volume or difficulty (conservation: the ledger never holds more impressions than the fakes served; market volume and difficulty always null; a derived variant carries no measurement); UNKNOWN never 0 (an unread source shows no totals; a rate over zero runs is null); the status says what was measured; no cross-tenant data (the hub cannot run it; another tenant never reads acme's property, runs or request ids); caps never exceeded (SEO_GSC_LEDGER_READS_PER_DAY counted on what reached Google, kept apart from audits'; requests per read <= SEO_GSC_MAX_REQUESTS; rows, pages, ROWS_CAP); bounded work (fake-clock seconds <= deadline + one timeout; no wall clock); one running run per tenant; a replay reads nothing; kill switches before and mid-read | a disclosed-gap spelling (attack_corpus KNOWN_GAPS) dropped anyway; no secret in third-party log records at INFO (httpx logs Bing's URL, whose query carries the key by Bing's design); an INCONCLUSIVE opportunity shows no click_gap_modeled 0 |
| ooh-py (15) | vendor text carrying instructions (to verify funds, zero the markup, buy) changes nothing and the unfunded purchase is refused FUNDS_NOT_VERIFIED; an authorized amount off the quote or a substituted vendor (lookalikes, leet, invisible characters, full-width) is refused and no authorization exists; only Andre authorizes (agent, console without his token, Compliance, scheduler: 403); a second submission of an open order is DUPLICATE_ORDER_BLOCKED and the vendor commitment is counted once; a bank transfer reference respelt (case, separators, method words, punctuation) never funds a second campaign; one face in two quotes is never priced or bought twice; one face respelt is never registered twice; a vendor answer never moves a disputed campaign; a 0.00 funds record is refused (AEGIS round 1, ADR 0019); a zero-padded or spelled-out transfer number never funds a second campaign; a face on another client's open order for an overlapping flight is refused without naming that client; a respelt vendor id (separators, corporate forms, leetspeak) is never registered (AEGIS round 2) | — |

### Chaos catalogue

Case flips; zero-width and soft-hyphen insertion; NFKC / full-width; homoglyphs from the repo's own confusables
tables (core: the Cyrillic / Greek lookalikes both creative-py's `CONFUSABLES` and sales-py's `_CONFUSABLE` list;
wide: all of creative-py's); leetspeak; letter spacing (spaces; dots / underscores / hyphens); diacritics; HTML
wrapping (`<br>`, `<p>`, `<div>`, `<blockquote>`); quoted replies (`>` quotes, "On ... wrote:" in English / Spanish /
French, Outlook header blocks above or below); trailing signatures; Spanish / French / Portuguese lines (and the
services' own opt-out words in those languages as seeds); prompt-injection lines; very long bodies around the text
caps; legal-name punctuation and whitespace; email sub-addressing and provider variants; post-URL variants; parameter
variation (platform, time gap, replay count, creator). Character-level transforms leave HTML tags and entities alone.
The full table is in `devtools/warroom/README.md`.

### Generative layer (Nov 8 2026)

The libraries list the cases someone thought of; `devtools/warroom/grammar.py` invents the ones nobody did. A
generated scenario names a grammar (cited URLs, markdown / multilingual answers with injections kept apart,
redirect chains, structural mutations of a documented engine response, hostile Search Console properties and storms), a count, and its ATOMS: corpora extracted by
AST from the service's pinned tests and source, exactly as seeds are. Case i is a pure function of (global seed,
scenario, i), so every seed invents other cases and a case id rebuilds its input. The grammar only builds inputs; the
driver's independent oracle and the invariants judge. Memory only grows: a failing generated case is appended to the
replay library on the run that found it (`known_failure: null`, gate-blocking until triaged); on CI, where the
checkout must stay clean, the entries are written to the report directory (`replay-additions.json`) and the gate
fails on them. One service may now have several libraries; cases and replay libraries are filed under the library's
name (`seo-py-priya/R0001`).

### Runtime and determinism

A case costs a fraction of a second (a fresh harness each); variants per seed are capped per library so `--all` takes
a few minutes on one machine (CI timeout 25 minutes). For a given seed the cases, outcomes and report are identical
from run to run apart from the report's `timing` section, which is measured and never asserted. CI uses seed 1;
other seeds are exploration, and what they find is promoted into the replay library, which every seed runs.

## Consequences

- A public-facing department gets a war room by adding a driver and a scenario library; until then it has none and
  is not gated by it.
- Findings are not fixed by the war room: a MUST failure is filed in `devtools/warroom/findings.md` with its replay
  ids and marked `known_failure`, so the gate stays honest (the failure is visible in every report) without blocking
  on work that belongs to the department.
- The libraries are data; expanding them is cheap. The seed corpora follow the services' pinned tests automatically.

## What is NOT covered yet

- **LLM personas**: the port answers `NOT_CONNECTED`; personas are hand-written in the libraries.
- **Dashboards**: the report is JSON and markdown in the CI log; no dashboard reads it.
- **Departments not yet seeded**: every public-facing department other than the six above (for example Influencer
  (11), New Business Development (12), Creative Production, Fulfillment, Finance, Legal, Delivery) has no war room
  yet and is not gated by it.
- **Live channels**: no real email, SMS, chat, platform OAuth, payment rail or ledger is exercised; the services'
  own live runs (`live-runs` job) cover the real ledger over HTTP, not chaos.
- **Multi-message conversations, concurrency and load**: each case is one short, sequential exchange on a fresh
  harness (seo-py-priya's concurrency cases start a second ledger run from inside the first one's in-flight Search
  Console request, in process; no real parallel load).
- **Certification-time attacks** that need the 14-day job schedule (for example the same video behind a short link,
  certified once) are left to verification-py's own suite.
