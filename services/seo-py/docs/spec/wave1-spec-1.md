# FROZEN SPEC - seo-py Wave-1 agents: Selene, Delia, Roman, Osei-lite, the entity check, and the shared primitives

Status: FROZEN v1 - APPROVED by the owner (Andre) 2026-11-13 ~13:15 PT, all 12 open questions at their proposed defaults. Once approved it is frozen: certification = pass/fail against the
numbered items below plus the permanent conformance suite (SUITE_PLAN.md). A reviewer finding outside this spec goes
to the owner's residual list (section R); it does not reject an agent. Changing an item needs a new owner approval
and a version bump of this file.

Spec version: `wave1-spec/1` (2026-11-13). Base: the vault / sink base of PR #51 (branch
`claude/seo-callum-engines-20261108`, ADR 0050, ADR 0051) with the Wave-1 agents of `claude/seo-wave1-cert-20261113`
(91db0abb) moved onto it. Reference trees for differentials: a4af277b (Gate 5, what ships), 91db0abb (Wave-1 round 2),
186471eb (core round 7).

Sources encoded here: ADR 0017 (decisions 4-6, 9, 11, 13-16, 18; W2-1..W2-3; CR-1..CR-11; certification C1-C6, R1-R9),
ADR 0018 (war room), ADR 0050, ADR 0051 (decisions 32-51, 62-63, threat model), owner decisions 2026-11-13 12:35 PT,
and every reviewer finding of rv-wave1 (a4af277), rv-wave1b (89fae32b), rv-wave1c (91db0abb) and builder briefs
wave1fix1-3. Reviewer source of each attack class is cited as [r1] / [r2] / [r3] with the finding id.

## 0. Conventions

- Item ids: `B-<AREA>-nn` behaviour, `A-nn` attack class, `D-nn` data-safety rule, `O-nn` over-tightening bar,
  `R-nn` residual, `Q-nn` open question. AREA: RB robots, FT fetch, CD codes (replaces safe_url's word rule), SE Selene,
  LG Selene log task, DE Delia, RO Roman, EN entity check, OS Osei-lite quarantine / observations, DR Osei drift,
  UL uncertainty labels (cross-cutting).
- A finding is written `CODE sev/class/decision`, e.g. `PAGE_HTTP_ERROR high/measured/ACT`. "IE" = decision
  `INSUFFICIENT_EVIDENCE`.
- "Pass" states the exact criterion; "N>=" the minimum generated case count in the permanent suite (fast tier) and,
  after a slash, in the lead-gate tier (`@pytest.mark.lead_gate`). A bound states time / CPU / memory with a number,
  measured on the reference box (2 vCPU Linux runner, the CI image; CPU = `time.thread_time` / `getrusage`).
- Status column: `COVERED` = an existing test or war-room case at 91db0abb pins it (named); `PARTIAL` = pinned in part;
  `OPEN` = a reviewer finding not yet closed (round 3 brief) or not yet tested; `NEW` = introduced by this spec
  (store-codes design or a gap found while writing it).
- Every number in a pass criterion is a requirement, not a measurement, unless marked "measured".

## 0.1 The store-codes model (owner decision 3) in one page

- CODE. `code(tenant, kind, raw)` = `{c:<kind>:<16 lower hex>}` where the hex is the first 64 bits of
  HMAC-SHA256(K_code(tenant, key_id), kind || 0x00 || canonical(raw)). K_code is an HKDF-SHA256 sub-key of the vault
  key with its own info label (`seo-codes/v1`), bound to the tenant id; `key_id` is recorded beside every report
  (`codes_key_id`). `kind` is from a closed set: `host`, `seg` (path segment), `path` (whole path when segment
  structure is not kept), `query`, `rule` (robots pattern), `text` (title / heading / description / free text),
  `jsonld` (a JSON-LD value), `type` (an @type not on the allowlist), `lang` (an invalid lang value), `date`
  (a crawled date / lastmod value), `ctype` (an unknown media type), `ns` (an XML namespace), `root` (an XML root
  name), `ua` (a User-Agent string), `ref` (a referer), `ip` (never stored even as code: see D-04), `entity` (an
  on-site entity value). Codes are deterministic: same tenant + key id + kind + value -> same code.
- PLACEHOLDER. Value-destroying typed placeholders carry no value and are allowed: `{id}` (digits only), `{number}`
  (7+ digits in total), `{uuid}`, `{email}`, `{token}`, `{param}`, `{file}.<ext>` with ext from the closed
  `STATIC_EXT` / document-extension list, `{unreadable}`, `{host}` (unparseable host). A placeholder never carries a
  hash of the value (it groups by shape only).
- ALLOWLIST (readable). Exhaustive in D-01. Everything else that originates outside the service (crawled, uploaded,
  declared by a site) is a CODE or a PLACEHOLDER in every stored or returned form.
- EVIDENCE MAP. For each audit / log ingest, a vault blob `evidence_map` {code -> raw value as observed, bounded
  1,024 characters a value, 20,000 entries} encrypted per tenant (ADR 0051 blob API). It is read only for the owning
  tenant's display surfaces (D-07, D-08). It is never logged, never in the ledger, never in a cross-tenant or
  Finance / Compliance view, never sent to any network peer.
- Findings are computed on RAW values in memory (same site, canonical equality, robots rules, sitemap membership,
  comparisons) before anything is coded; coding changes no finding (O-01).

## 1. Shared primitive: robots.txt (`primitives/robots.py`)

| Id | Input | Required output | Pass criterion | Status |
|---|---|---|---|---|
| B-RB-01 | rule sets with groups for named tokens, `*`, none | A crawler uses every group whose user-agent value equals its product token case-insensitively (all merged); else every `*` group merged; else everything allowed. Consecutive user-agent lines form one group; rules before any user-agent line are ignored and counted `lines_ignored` | Agreement with the independent RFC 9309 oracle (SUITE_PLAN O-RFC) on every decision; N>= 4,000 / 40,000 random rule sets x 6 paths | COVERED (test_percent_encoding_agrees_with_an_rfc9309_octet_oracle; r2 measured 240,000 decisions, 0 disagreements) |
| B-RB-02 | rules of different specificity | Most specific match wins, specificity = octets of the canonical pattern (`*`, final `$` = 1 octet each); equal specificity: allow wins; empty value matches nothing; `/robots.txt` always allowed | as B-RB-01 | COVERED (test_longest_match_and_allow_wins_ties, test_specificity_counts_canonical_octets) |
| B-RB-03 | `*`, `**`, final `$`, inner `$` | `*` matches any octet sequence; `**` = `*`; `$` anchors only at pattern end; an inner `$` is the literal `%24` | as B-RB-01 plus 200 hand cases from RFC 9309 sections 2.2.2-2.2.3 and Google's documented examples | COVERED (test_rfc9309_wildcards) |
| B-RB-04 | percent-encodings | Unreserved `%XX` decoded; every other `%XX` kept with upper-case hex (`%2F` never `/`, `%3F` never `?`); `%2A`, `%24` literal; raw non-ASCII = `%XX` of its UTF-8 octets; an invalid UTF-8 octet kept as itself (`surrogateescape`), two different invalid octets never equal; a stray `%` = `%25`; space, controls, `"<>\^`{|}` encoded | Oracle disagreements 0 on N>= 4,000 / 40,000 rule sets of which >= 25% use a reserved encoding and >= 25% an invalid UTF-8 octet; fail-open decisions (allowed where the oracle disallows) 0; this crawler fetches a disallowed path 0 times | COVERED (test_rfc9309_octet_comparison, test_an_encoded_allow_never_lets_our_crawler_fetch_a_disallowed_path) [r2 M1] |
| B-RB-05 | UTF-8 BOM at byte 0 | Stripped; the first line is read normally; `bom_stripped: true` in facts; a BOM elsewhere is data | `User-agent: *` / `Disallow: /` behind a BOM blocks everything; 0 `ROBOTS_LINES_IGNORED` caused by a BOM | COVERED (test_bom_before_the_first_group_is_stripped) [r1 S-S1] |
| B-RB-06 | line syntax | Keys case-insensitive; accepted spellings `user-agent`, `useragent`, `user_agent`, `allow`, `disallow`, `sitemap`; `#` starts a comment; CR, LF and CRLF all end a line; a line without `:` or with an unknown key is ignored and counted; `sitemap` lines are global (any position) | Exact `lines_ignored` count on N>= 500 generated files with known counts | PARTIAL (count asserted in a few tests only) |
| B-RB-07 | file longer than the parse limit | Parse limit = 512,000 OCTETS (500 KiB) of the decoded body, counted in octets also when the text is multi-byte; the prefix is read, the last (cut) line is not read; `truncated: true`, `bytes_read`; every decision from it `complete: false` | A rule wholly before the cut decides as written; the cut rule is absent; a file of 300,000 three-octet characters (900,000 octets) is truncated at 512,000 octets | PARTIAL: service path cuts octets in fetch (test_a_rule_cut_by_the_parse_limit_is_not_read); `robots.parse` alone compares characters (NEW gap n1) |
| B-RB-08 | HTTP outcome of robots.txt | 2xx: parse; 3xx: follow up to 5 hops, every hop checked as B-FT-02; 4xx (400-499 except 429, see Q-06): unavailable = allow all; 5xx, 429 (Q-06), network failure, redirect loop / over 5 hops: unreachable = disallow all; a body over the wire cap whose DECODED size is within the parse limit is parsed (never unreachable); only a body this service cannot decode within its bounds is `incomplete` | Each class: N>= 20 cases with the required status class | PARTIAL; wire-cap case OPEN [r3 MUST-2: gzip of empty members 2,200,043 B -> 23 B was `too_large` -> false critical] |
| B-RB-09 | any decision | Charged deterministic work proportional to the real matching cost: at least len(path) x (sum of piece lengths tried) / C for a fixed C, plus normalising and index probes; a decision whose charge would pass the step limit is not made (`allowed: None`, `undecided: "work"`); same input -> same decisions and same units in every run, with any network latency | (a) 3 repeated runs and a run with 0.7 s per response give identical decisions and units; (b) CPU per step <= 2.0 s for the costliest generator of A-03 within 512,000 octets (measured r3: 6.02-8.35 s, ADR claim 1.6 s false); (c) units charged / CPU-ns ratio varies < 10x across the A-02 / A-03 shapes | (a) COVERED (test_work_is_deterministic, test_robots_matching_uses_no_clock); (b), (c) OPEN [r3 MUST-4] |
| B-RB-10 | undecided / approximate decisions | `allowed: None` is never counted as blocked nor allowed by any caller; `complete: false` decisions never produce `ROBOTS_BLOCKS_*`, `SITEMAP_LISTS_ROBOTS_BLOCKED_URL` or a readiness pass / fail | Mutation of each caller's None / complete handling is killed by a test | COVERED (test_undecided_paths_are_never_counted_blocked, test_undecided_crawler_access_is_not_a_pass) [r2 S11, R13] |
| B-RB-11 | quoted rule | A rule quoted in a finding is a rule exactly as in the file (stored per D-01 / D-03: structural patterns readable, others coded); never a shortened or rewritten rule | 0 quoted rules absent from the file over every suite case | COVERED (test_reserved_encoding_never_yields_a_rule_not_in_the_file, test_a_robots_rule_reaches_a_finding_only_in_its_safe_form) [r2 M3, S15] |

## 2. Shared primitive: fetch (`primitives/fetch.py`)

| Id | Input | Required output | Pass criterion | Status |
|---|---|---|---|---|
| B-FT-01 | URL scheme / port | http / https only (else `REFUSED_URL`); ports 80 / 443 only (else `REFUSED_PORT`); userinfo, legacy numeric hosts (`0177.0.0.1`, `2130706433`, `0x7f000001`) refused before resolution | 0 connections for N>= 60 refused URL shapes | COVERED (war room ssrf-ports-schemes-credentials, test_refused_urls) |
| B-FT-02 | resolved addresses | Every address of every hop checked (loopback, private, link-local incl. 169.254.169.254, CGNAT, multicast, reserved, fec0::/10, 192.88.99.0/24, IPv4-mapped / 6to4 / NAT64); connect to the checked IP with Host / SNI = name | 0 connections to a non-public address over the whole suite and war room (socket audit hook) | COVERED (war room ssrf-internal-targets; r1-r3 measured 0) |
| B-FT-03 | redirects | Followed by hand, at most `max_redirects` = 5 (configurable 0-10), every hop re-checked (B-FT-01/02, robots for our crawler); more -> `TOO_MANY_REDIRECTS` | Hop 5 followed, hop 6 refused; loop refused | COVERED |
| B-FT-04 | `Location` header | The FULL value is used up to 8,192 octets (RFC 9110 recommends >= 8,000); longer -> state `REDIRECT_TOO_LONG` (findings IE); a stored header may be cut, a followed one never | 0 truncated Locations followed for lengths 1,990-8,192 (N>= 100); > 8,192 -> REDIRECT_TOO_LONG, no request to a cut URL | OPEN [r3 MUST-3: 2,011-char Location cut at 2,000 and followed -> false PAGE_HTTP_ERROR 404] |
| B-FT-05 | body size / codings | At most `max_bytes` (2 MiB default, 64 KiB-10 MiB) DECODED; gzip / deflate, one coding; every gzip member within the same budget; trailing non-gzip bytes `PROTOCOL_ERROR`; stacked or other codings (br, zstd, compress) `UNSUPPORTED_ENCODING`; MemoryError `RESOURCE_LIMIT`; declared Content-Length over the cap -> `TOO_LARGE` before reading (except robots, B-RB-08) | Decoded bytes never exceed the cap + 64 KiB step; peak traced memory per fetch <= 4 x cap | COVERED (war room decompression-bombs, giant-and-malformed-files) |
| B-FT-06 | slow / trickling peer | One hard deadline = 2 x `timeout_s` (20 s default) over connect, TLS, headers and body of every hop; a nested robots fetch never outlives the outer deadline; `TIMEOUT` | Fetch returns <= deadline + 1.0 s for N>= 20 trickle shapes (headers, body, TLS) | COVERED (war room slow-bodies) |
| B-FT-07 | our crawler's robots check | rule disallows -> `BLOCKED_BY_ROBOTS` (`reason: rule`); robots unreachable -> `BLOCKED_BY_ROBOTS` (`reason: unreachable`); work spent or body not decodable within bounds -> `ROBOTS_CHECK_INCOMPLETE` (own state, never BLOCKED_BY_ROBOTS); checked on path + query as written | Each state on N>= 20 cases; slow ordinary site (0.7 s / response) -> 0 `BLOCKED_BY_ROBOTS` / `ROBOTS_CHECK_INCOMPLETE` | COVERED (test_counted_work_spent_is_its_own_state_never_blocked, test_slow_ordinary_site_is_never_blocked_or_unreadable) [r2 M2] |
| B-FT-08 | kill switch | Checked before every socket operation; state `KILLED`; no request after the switch | 0 requests after engage in N>= 8 mid-run cases | COVERED (war room kill-switch-*) |
| B-FT-09 | identity | User-Agent `ZBM-SEO-Audit/<version> (+<bot_info_url>)` on every bot fetch; the browser UA only for access-diff, only where robots.txt allows `ZBM-SEO-Audit` | Every request seen by the fixture carries one of the two UAs; 0 browser-UA requests to a path our crawler may not fetch | COVERED (AEGIS 00cc66b L3 tests) |
| B-FT-10 | observation | Every fetch an agent makes (robots, page, browser UA, canonical target, sitemap, index child, llms.txt, homepage) yields one Osei observation (B-OS-03) | observations = fetches, per kind, in N>= 50 audits | COVERED (test_every_fetch_gets_an_observation) |

## 3. Shared primitive: codes (`primitives/codes.py`, replacing the word rule of `safe_url.py`)

No bundled dictionary (owner decision 3): `path_words.txt` (wordfreq, CC BY-SA 4.0), the NLTK-derived exempt list and
`schema_types.py` (schema.org vocabulary, CC BY-SA 3.0) are removed from the build. Readability comes from the closed
allowlist (D-01) only.

| Id | Input | Required output | Pass criterion | Status |
|---|---|---|---|---|
| B-CD-01 | (tenant, kind, value) | `{c:<kind>:<16 hex>}` per 0.1; deterministic; the key never leaves `vault.py`; no key configured -> the audit / ingest is refused `VAULT_UNAVAILABLE` before any fetch (ADR 0051 decision 9 pattern), nothing written | Same input 1,000 x -> 1 code; 2 tenants x 10,000 values -> 0 equal codes across tenants; 64-bit truncation collisions 0 on 1,000,000 distinct values of one tenant (expected 2.7e-8) | NEW |
| B-CD-02 | key rotation | `codes_key_id` recorded in every report / log report; reports under different key ids are compared by drift as R2 (B-DR-05), never as appeared / disappeared findings | 2 audits across a rotation: 0 REAL_CHANGE, every changed key R2 | NEW |
| B-CD-03 | crawled / declared URL | Stored form = scheme (http / https readable; any other: the name from the closed list `javascript data file ftp mailto tel other` + `:`) + host (D-01 own domain readable; else `{c:host:..}` of the whole lower-cased IDNA host; unparseable `{host}`) + port (number, only when not default) + path (per B-CD-04) + `query_present` / `fragment_present` booleans. Userinfo never, query never, fragment never | 0 raw query / fragment / userinfo / other-host label in any stored URL over A-18 | NEW (replaces C3 / R5; r3 MUST-1 host leak) |
| B-CD-04 | URL path (crawled, log, sitemap) | Decoded once (invisibles removed, NFKC, confusable at-signs folded) then per `/` segment: empty -> empty; an allowlisted well-known file name (D-01 W) -> readable; a value-destroying shape -> its placeholder (`{email}`, `{uuid}`, `{number}`, `{id}`, `{token}`, `{param}`, `{file}.ext`); anything else -> `{c:seg:..}` of the segment. At most 300 characters; segment count kept | Planted values readable 0 (D-section bar); every well-known name of D-01 W readable in every position; the shape rules give the same placeholder for N>= 3,000 generated shapes as the reference (SUITE_PLAN O-TPL) | NEW (shape rules COVERED: test_template_rules_m_s5, test_safe_url_rule) |
| B-CD-05 | text fields (title, headings, description, robots comments, llms.txt text, JSON-LD strings, on-site entity values) | Never stored as text: measured attributes only (length in code points after whitespace collapse, empty, equality flags) and, where a finding needs to name the value, `{c:text:..}` | 0 raw text field in any stored form; Roman title equality (same as H1) unchanged vs raw computation on N>= 1,000 pages | PARTIAL (title withheld: test_title_text_is_withheld_and_measured) |
| B-CD-06 | comparison helpers | `same_site(host)`: equal to a registered domain of the tenant or its `www.` twin, or a subdomain of one (Q-02 for display); compares lower-cased IDNA hosts with the trailing dot removed; lookalikes (A-21) are other hosts | N>= 200 lookalike / control hosts classified as the reference | PARTIAL (war room answer-engine-lookalike-citations covers Callum only) |

## 4. Selene - crawlability (`agents/selene.py`)

| Id | Input | Required output | Pass criterion | Status |
|---|---|---|---|---|
| B-SE-01 | robots.txt unreachable (5xx, network failure, Q-06) | `ROBOTS_UNREACHABLE critical/measured/ACT` (detail fetch_state, status); pages `PAGE_BLOCKED_FOR_OUR_CRAWLER info/measured/IE` with `reason: unreachable` and the note "could not be fetched", never "disallows" | N>= 20 | COVERED (test_robots_unreachable_note_differs_from_a_rule) |
| B-SE-02 | robots.txt 4xx (Q-06) | `ROBOTS_ABSENT info/measured/WATCH`, everything allowed | N>= 10 | COVERED |
| B-SE-03 | robots.txt not decodable within bounds (wire-only excess, B-RB-08) | `ROBOTS_CHECK_INCOMPLETE info/measured/IE` (cap named); never `ROBOTS_UNREACHABLE`; pages `PAGE_NOT_READ_ROBOTS_CHECK_INCOMPLETE` | gzip-of-empty-members 2,200,043 B -> 0 ROBOTS_UNREACHABLE | OPEN [r3 MUST-2] |
| B-SE-04 | bot x path matrix | Every family token of `agents/bots.py` x (`/` + every audited path). A search token (`bots.SEARCH_TOKENS`) disallowed -> `ROBOTS_BLOCKS_SEARCH_CRAWLER critical` when `/` is blocked else `high`, `/measured/ACT`; any other token -> `ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER info/measured/WATCH` with the policy note; detail `blocked_paths` <= 20, `rules` <= 5 (B-RB-11) | Matrix equal to the oracle on N>= 400 sites (O-01); severity boundary mutants killed | COVERED (test_search_crawler_block_severity_critical_on_root_high_elsewhere) |
| B-SE-05 | file truncated at the parse limit | Every block decision -> `ROBOTS_BLOCK_APPROXIMATE info/measured/IE` (`cap: robots_bytes`, 512,000); `ROBOTS_LIMITS_EXCEEDED medium/measured/WATCH` (truncated, bytes_read); never `ROBOTS_BLOCKS_*` | N>= 10 | COVERED (test_truncated_file_gives_approximate_decisions_with_the_cap_named) |
| B-SE-06 | matrix work spent | Undecided cells -> one `ROBOTS_CHECK_INCOMPLETE info/measured/IE` (undecided count, cap) and outcome PARTIAL | N>= 5 | COVERED (test_selene_matrix_budget_degrades_to_partial) |
| B-SE-07 | ignored lines | `ROBOTS_LINES_IGNORED low/measured/WATCH` with the exact count (B-RB-06) | as B-RB-06 | COVERED |
| B-SE-08 | page fetch refused by robots / incomplete / failed | `PAGE_BLOCKED_FOR_OUR_CRAWLER info/measured/IE` (reason rule / unreachable); `PAGE_NOT_READ_ROBOTS_CHECK_INCOMPLETE info/measured/IE`; any other non-OK state -> `PAGE_UNREACHABLE high/measured/IE` with fetch_state; nothing else is derived from that page | N>= 30 | COVERED |
| B-SE-09 | HTTP status | >= 400 -> `PAGE_HTTP_ERROR` `high` if >= 500 else `medium`, `/measured/ACT`; 399 none, 400 medium, 499 medium, 500 high | boundary cases 399/400/499/500 + N>= 20 | COVERED (test_http_error_threshold_and_severity) |
| B-SE-10 | redirects | 1 hop -> `REDIRECTED low/measured/WATCH`; > 1 -> `REDIRECT_CHAIN medium/measured/ACT`; detail statuses, hops, final_url (B-CD-03), `temporary` = any of 302 / 303 / 307 | N>= 20 | COVERED (test_redirect_severity_single_vs_chain) |
| B-SE-11 | non-HTML 2xx | `PAGE_NOT_HTML low/measured/WATCH`, content type from the closed media-type list else `{c:ctype:..}` | N>= 10 | COVERED |
| B-SE-12 | noindex | An X-Robots-Tag directive applies to every crawler unless it follows `<bot>:` (then that bot only); several X-Robots-Tag headers / comma lists combined; a meta robots tag named `robots` applies to all, named for a bot to that bot only; `noindex` or `none`. Engines (bots.ENGINE_SEARCH_BOT) affected -> `NOINDEX` `high` on `/` else `medium`, `/measured/TEST` (engines, bots); only non-engine bots -> `NOINDEX_FOR_BOT info/measured/WATCH` | Per-engine result equal to the oracle (SUITE_PLAN O-NOIX) on N>= 500 header / meta combinations | COVERED (test_noindex_is_per_engine, test_gptbot_meta_noindex_is_reported_for_gptbot_only) [r1 S-S2] |
| B-SE-13 | canonicals | invalid (non-http(s), broken) -> `CANONICAL_INVALID medium/measured/ACT` (count); valid + invalid > 1 -> `CANONICAL_MULTIPLE high/measured/ACT`; one on another site -> `CANONICAL_OTHER_HOST medium/measured/TEST`; noindex + canonical elsewhere -> `NOINDEX_CANONICAL_CONFLICT high/measured/ACT`; none -> `CANONICAL_MISSING low/measured/WATCH` | N>= 60 | COVERED (test_canonical_other_host_only_for_another_site) |
| B-SE-14 | same-site canonical target | At most 5 targets a run fetched; our fetch did not succeed -> `CANONICAL_TARGET_UNCHECKED info/measured/IE`; a measured status != 200 or a redirect -> `CANONICAL_TARGET_NOT_200 high/measured/ACT` | cap boundary 5 / 6; N>= 20 | COVERED (test_canonical_target_check_cap, test_canonical_target_our_fetch_failed_is_insufficient_evidence) |
| B-SE-15 | access differential | Browser-UA fetch only where our crawler may fetch; a UA-dependent difference (diff.access_diff) -> `BOT_DIFFERENTIAL high/measured/TEST` | N>= 10 | COVERED |
| B-SE-16 | parse error | `HTML_PARSE_ERROR low/measured/WATCH` (error code only) | N>= 5 | COVERED |
| B-SE-17 | outcome | OK iff every audited page fetched and no matrix cell undecided; PARTIAL iff >= 1 fetched otherwise; BLOCKED iff every page BLOCKED_BY_ROBOTS; IE iff every page in {BLOCKED_BY_ROBOTS, ROBOTS_CHECK_INCOMPLETE} and not all blocked; else FAILED; KILLED on a switch | each branch N>= 3 | COVERED (test_selene_ok_vs_partial) |
| B-SE-18 | reuse | One bot fetch per audited page; Delia, Roman and the entity check reuse Selene's extract (no second request) | requests per page = 1 bot + <= 1 browser + <= 1 canonical | COVERED |

## 5. Selene log task (`agents/logs.py`, `svc_logs.py`)

| Id | Input | Required output | Pass criterion | Status |
|---|---|---|---|---|
| B-LG-01 | combined / common / jsonl chunks | Parsed in memory; chunk <= 90,000 bytes, line-aligned, idempotent by (request id, seq); lines > 8,192 bytes `LINE_TOO_LONG`, invalid UTF-8 `NOT_UTF8`, `MALFORMED`, `BAD_IP`, `BAD_STATUS` quarantined and counted exactly; `LOG_LINES_QUARANTINED low/measured/WATCH` by reason | exact counts on N>= 1,000 generated lines per format | COVERED (test_combined_common_and_jsonl, test_adversarial_lines_are_quarantined_and_counted) |
| B-LG-02 | User-Agent | Family by token (most specific token wins); `verified` / `spoofed` only through the DNS verification port; else `claimed`; `SPOOFED_CRAWLER_REQUESTS medium/measured/TEST` | N>= 200 | COVERED |
| B-LG-03 | robots check | The request target as written (path + query, encoding untouched) matched once via `robots.canon`; disallowed hits per family; `ROBOTS_DISALLOWED_PATHS_REQUESTED low/measured/WATCH`; unchecked bot lines (work, restart) -> `LOG_ROBOTS_CHECK_INCOMPLETE info/measured/IE` and report PARTIAL | `Disallow: /*?sort=`, `/search?`, `/%2541` x 6 requests -> 4 of 4 hits; oracle agreement N>= 2,000 lines | COVERED (test_log_robots_check_matches_path_with_query_and_decodes_once) [r2 M4; r3 survivor logs.py:179 OPEN] |
| B-LG-04 | 5xx to search crawlers | `SEARCH_CRAWLER_SERVER_ERRORS high/measured/ACT` iff requests >= 20 and 5xx share > 5% (Googlebot, Bingbot) | boundaries 19/20 requests, 5.0% / 5.1% | COVERED |
| B-LG-05 | sitemap sample | `IMPORTANT_URLS_NOT_CRAWLED medium/measured/TEST` for sample paths no search crawler requested (matched by keyed digest in memory); no audit -> missing, never invented | N>= 20 | COVERED (test_end_to_end_with_robots_and_uncrawled, test_without_an_audit_the_sample_is_missing_not_invented) |
| B-LG-06 | no search crawler | `SEARCH_CRAWLER_ABSENT medium/measured/WATCH` | N>= 5 | COVERED |
| B-LG-07 | client IPs | HyperLogLog, 256 registers of keyed hashes; no IP, no IP hash stored | 0 IP-derived values in any output | COVERED (test_no_raw_ip_or_ua_survives) |
| B-LG-08 | bounds | <= 500 templates per family (`templates_over_cap` counted); per-ingest state fixed size; ingest / tenant byte and count limits as W2-2 | N>= 3 | COVERED |
| B-LG-09 | stored inputs | robots.txt text and the sitemap sample live in the `log_ingest_inputs` vault blob only (ADR 0051 decision 42); log lines never stored; templates per B-CD-04 | D-section scan | COVERED on 186471eb (test_sink_job_d) |

## 6. Delia - sitemaps and llms.txt (`agents/delia.py`)

| Id | Input | Required output | Pass criterion | Status |
|---|---|---|---|---|
| B-DE-01 | discovery | robots.txt `Sitemap:` lines on this site (same_site), at most 10 followed in declared order; none -> `/sitemap.xml`; cross-host declared sitemaps counted (`sitemaps_cross_host_count`) and stored as codes, never fetched | N>= 40; 0 fetches of a cross-host sitemap | COVERED (test_declared_sitemaps_same_site_only) |
| B-DE-02 | sitemap fetch outcome | measured 404 / 410 -> `SITEMAP_MISSING medium/measured/ACT`; any other non-200 or failed fetch -> `SITEMAP_UNREADABLE medium/measured/IE` (fetch_state, status); `sitemap_fetch_failures` counted | N>= 30 | COVERED (test_sitemap_unreadable_vs_missing) |
| B-DE-03 | size (sitemaps.org: 50,000 URLs and 52,428,800 bytes uncompressed per file) | A file within the protocol limits is READ (fetch cap for sitemaps >= 52,428,800 decoded bytes, streamed, Q-07); a decompressed size over the protocol limit -> `SITEMAP_TOO_LARGE medium/measured/ACT` (new code, the limit named); a body this service stops before the protocol limit -> `SITEMAP_NOT_FULLY_READ info/measured/IE` with bytes read; never `SITEMAP_DECOMPRESS_FAILED` for a valid gzip | A valid 45,000-URL sitemap of 6 MiB plain and of 12 MiB decompressed -> read, 0 findings beyond the oracle's | NEW gap n2: at 91db0abb fetch cap 2 MiB -> UNREADABLE; DECOMPRESSED_MAX 8 MiB -> DECOMPRESS_FAILED ACT (false measured) |
| B-DE-04 | gzip | `.gz` URL or gzip media type -> bounded decompression; a corrupt / truncated gzip -> `SITEMAP_DECOMPRESS_FAILED medium/measured/ACT` + quarantine `DECOMPRESS_FAILED` with `size_basis: wire` (B-OS-01) | N>= 20 (corrupt, truncated 12 B, trailing garbage, multi-member) | PARTIAL [r3 SHOULD: 12-byte gzip recorded bytes 0 + SHA of empty] |
| B-DE-05 | XML safety | Encoding detected (BOM; UTF-16 / UTF-32 byte pattern; XML declaration); DOCTYPE / ENTITY looked for in the DECODED text; UTF-32 decoded here; then one expat pass with DOCTYPE / entity-declaration / external-entity handlers refusing and parameter entities never parsed; refused -> quarantine `XML_DOCTYPE_REFUSED`; unreadable -> `XML_MALFORMED`; both -> `SITEMAP_QUARANTINED high/measured/ACT` (`why` = reason); 0 entity expansions | A-08 bar | COVERED (test_entity_quarantine_in_every_encoding, test_expat_refuses_a_dtd_even_when_the_text_check_misses_it, test_utf32_sitemap_is_read_not_called_malformed) [r1 M-D1] |
| B-DE-06 | namespace | root namespace != `http://www.sitemaps.org/schemas/sitemap/0.9` -> `SITEMAP_NAMESPACE low/measured/ACT` (namespace: the constant if it is the 0.9 namespace, else `{c:ns:..}`, empty string when absent) | N>= 10 | COVERED (test_namespace_check) |
| B-DE-07 | sitemap index | Same-site children followed, at most 10 a run (`MAX_CHILDREN`), loop-guarded (each URL fetched once); cross-host children counted; over-cap children counted (`sitemap_children_over_cap`) | 200 children -> 10 fetched, 190 counted; self / mutual loop -> each fetched once | COVERED (test_index_children_cap_cross_host_and_loop_guard) |
| B-DE-08 | unknown root | `SITEMAP_ROOT_UNKNOWN medium/measured/ACT`; root from `urlset sitemapindex rss feed html channel` else `{c:root:..}` | N>= 10 | COVERED |
| B-DE-09 | URLs per file | > 50,000 -> `SITEMAP_TOO_MANY_URLS high/measured/ACT`; exactly 50,000 -> none | boundary 50,000 / 50,001 | COVERED (test_urls_per_file_limit) |
| B-DE-10 | loc checks | not absolute http(s) -> `SITEMAP_LOC_NOT_ABSOLUTE medium/measured/ACT`; another host -> `SITEMAP_LOC_OTHER_HOST medium/measured/ACT`; count + <= 5 examples (codes) each; URLs listed counted, at most 100,000 examined (`sitemap_urls_examined`) | N>= 40 | COVERED |
| B-DE-11 | listed-URL robots check | First 500 same-site listed URLs (path + query) checked for `Googlebot`; disallowed -> `SITEMAP_LISTS_ROBOTS_BLOCKED_URL medium/measured/ACT` (count, examples, checked_for); undecided -> `SITEMAP_ROBOTS_CHECK_INCOMPLETE info/measured/IE`, outcome PARTIAL; an undecided URL is never counted blocked | equal to the oracle on the O-01 corpus (r2 measured: 304 sites, 35 blocked, 0 mismatch) | COVERED (test_listed_url_robots_check_is_for_googlebot, test_listed_url_robots_check_budget) [r3 survivor delia.py:356 OPEN] |
| B-DE-12 | lastmod | W3C date-time, ASCII digits, FULL match (`2026`, `2026-11`, `2026-11-13`, `...T10:00Z`, `+hh:mm`, fractions) else `SITEMAP_LASTMOD_INVALID low/measured/ACT` (count, shape examples as codes); > audit time + 1 day -> `SITEMAP_LASTMOD_FUTURE low/measured/ACT` (count); >= 10 values all equal -> `SITEMAP_LASTMOD_ALL_IDENTICAL low/inferred/TEST` | W3C oracle agreement N>= 2,000 values (r2 measured 2,000, 0 disagreements); boundary now+1 day; 9 / 10 values | COVERED (test_future_lastmod_window, test_identical_lastmod_rule, test_fullwidth_digits_are_not_a_w3c_date) [r3 survivor :73 match vs fullmatch OPEN] |
| B-DE-13 | llms.txt fetch | failed fetch or status >= 500 -> `LLMS_TXT_UNREADABLE info/measured/IE` (state unreadable); any other non-200 -> `LLMS_TXT_ABSENT info/measured/WATCH` (absence is not a defect); 200 not UTF-8 -> `LLMS_TXT_NOT_UTF8 low/measured/TEST` + quarantine `NOT_UTF8` | boundary 499 / 500; N>= 20 | COVERED (test_llms_5xx_is_unreadable_and_h2_first_is_one_issue, test_llms_txt_status_exactly_500_is_unreadable_not_absent) |
| B-DE-14 | llms.txt shape (llmstxt.org proposal) | Issues from the closed set `NO_H1_TITLE MORE_THAN_ONE_H1 NO_H2_SECTIONS NO_LINK_LISTS RELATIVE_OR_NON_HTTP_LINKS` -> one `LLMS_TXT_FORMAT low/measured/TEST`; media type not text/plain or text/markdown -> `LLMS_TXT_CONTENT_TYPE low/measured/TEST`; counts of sections / links stored; no text stored | Shape oracle agreement N>= 500 documents (r2 measured 500, 0 disagreements) | COVERED (test_llms_content_type) |
| B-DE-15 | disclosure | facts: sitemaps attempted / read / failures, URLs listed / examined (max), children followed / cross-host / over cap, robots_checked / robots_unchecked, work units | every fact present in every Delia envelope | COVERED |
| B-DE-16 | outcome | OK iff something was read (a sitemap, or llms.txt present / absent) and no fetch failure and nothing unchecked; PARTIAL iff something read otherwise; FAILED iff nothing read; KILLED on a switch | each branch N>= 3 | COVERED |

## 7. Roman - content structure (`agents/roman.py`)

| Id | Input | Required output | Pass criterion | Status |
|---|---|---|---|---|
| B-RO-01 | title | absent / empty -> `TITLE_MISSING high/measured/ACT`; length (code points after whitespace collapse) < 10 or > 60 -> `TITLE_LENGTH low/measured/TEST` (length, range); title text never stored (B-CD-05); stored: length, empty, same_as_h1 (whitespace-folded), over / under | boundaries 9/10/60/61 | COVERED (test_title_length_bounds, test_title_minimum_boundary, test_title_vs_h1_whitespace_normalised) |
| B-RO-02 | meta description | absent -> `META_DESCRIPTION_MISSING low/measured/TEST`; text never stored | N>= 10 | COVERED |
| B-RO-03 | H1 | count != 1 (0 or >= 2) -> `H1_COUNT medium/measured/ACT` | 0 / 1 / 2 | COVERED (test_h1_count_zero_and_two) |
| B-RO-04 | heading order | a level > previous + 1 -> `HEADING_LEVEL_SKIP low/measured/TEST` (<= 10 [from, to] pairs) | oracle agreement N>= 1,000 sequences | COVERED (test_heading_skip_rule) |
| B-RO-05 | lang | absent, `""` or white space -> `LANG_MISSING low/measured/ACT`; not a valid BCP 47 tag -> `LANG_INVALID low/measured/ACT` (NEW, Q-12) and readiness `lang_declared: fail`; valid -> stored as language[-script][-region] with each subtag validated against CLDR validity data (Q-05), an invalid subtag dropped | RFC 5646 oracle agreement N>= 4,000 tags; `lang="123"` -> LANG_INVALID + fail for 5 engines | PARTIAL (test_lang_is_a_bcp47_tag_or_invalid, test_empty_lang_is_missing, test_script_subtag_is_validated_against_iso15924) [r3 NIT lang=123 OPEN] |
| B-RO-06 | JSON-LD | invalid JSON -> `STRUCTURED_DATA_INVALID_JSON medium/measured/ACT` + quarantine `JSONLD_INVALID` (size, sha256 of the block); block > 262,144 characters -> not parsed, `STRUCTURED_DATA_BLOCK_TOO_LARGE medium/measured/TEST` + quarantine `JSONLD_TOO_LARGE` (B-OS-01); rule-table errors -> `STRUCTURED_DATA_ERRORS medium/measured/ACT`; recommended missing -> `STRUCTURED_DATA_RECOMMENDED_MISSING low/measured/TEST`; none -> `STRUCTURED_DATA_ABSENT` medium on `/` else low, `/measured/TEST`; one invalid block beside a valid one is still reported | N>= 100 | COVERED (test_jsonld_invalid_quarantine_has_size_and_sha, test_one_invalid_jsonld_block_beside_a_valid_one_is_reported, test_oversized_jsonld_is_recorded_truncated_with_its_whole_size) |
| B-RO-07 | @type | Readable only when in the type allowlist (D-01 T); else counted `unknown_type` and, if named, `{c:type:..}` | 0 planted @type readable; every allowlisted type readable | NEW (replaces schema_types.py, licence) [r3 MUST-5c] |
| B-RO-08 | entity markup | Organization / LocalBusiness node without sameAs -> `ENTITY_SAMEAS_MISSING low/measured/TEST`; no such node on any page while `/` was read -> `ENTITY_MARKUP_ABSENT medium/measured/TEST` | N>= 20 | COVERED (test_sameas_rule, test_structured_data_absent_severity_and_entity_markup_absent) |
| B-RO-09 | raw text | visible text < 500 characters -> `LITTLE_TEXT_IN_RAW_HTML medium/measured/TEST` (characters, minimum, render state) | boundary 499 / 500 | COVERED (test_raw_text_min) |
| B-RO-10 | readiness | Per engine (google, bing, openai, anthropic, perplexity) checks from CHECK_APPLIES, each `pass` / `fail` / `undecided` / `n/a`; `crawler_allowed` from robots for the engine's token (undecided or approximate -> `undecided`), `indexable` per engine (B-SE-12); `heuristic: true`, `calibrated: false`, the label; no score, no cross-engine total | N>= 200; 0 report keys named score / grade / percent | COVERED (test_undecided_crawler_access_is_not_a_pass) |
| B-RO-11 | outcome | OK iff every page read; PARTIAL iff some; IE iff none | each N>= 3 | COVERED (test_roman_ok_vs_partial) |

## 8. Entity check (`agents/entity_check.py`)

| Id | Input | Required output | Pass criterion | Status |
|---|---|---|---|---|
| B-EN-01 | Organization / LocalBusiness / ProfessionalService / Store nodes vs the canonical record | Per field, narrow stated normalisation (case, punctuation, street abbreviations, region names, phone digits); equal -> `match`; differs -> `ENTITY_FIELD_MISMATCH high/measured/ACT` with the comparison only (result, case / whitespace / normalised-equal flags, lengths, `{c:entity:..}` of the normalised on-site value) and the FIELD NAME; neither the on-site value nor the canonical value is in the finding (the canonical is in the entity vault blob, ADR 0051 decision 42) | 0 on-site or canonical values in any stored form (A-18 entity surfaces); N>= 200 comparisons equal to the reference | PARTIAL (test_entity_mismatch_stores_the_comparison_not_the_page_text); canonical value in `detail.canonical` NEW gap n3 |
| B-EN-02 | missing field | `ENTITY_FIELD_MISSING_IN_MARKUP medium/measured/ACT` (field name only); address fields required only on LocalBusiness / ProfessionalService / Store | N>= 30 | COVERED (test_mismatch_and_missing_fields) |
| B-EN-03 | no node | `ENTITY_MARKUP_ABSENT medium/measured/ACT` | N>= 5 | COVERED |
| B-EN-04 | homepage phone sample | states FOUND / NOT_FOUND_IN_SAMPLE / NO_CANONICAL_PHONE / HOMEPAGE_NOT_SELECTED / ...; not found -> `HOMEPAGE_PHONE_NOT_IN_TEXT_SAMPLE low/measured/TEST`; URLs in coded form | N>= 20 | COVERED |
| B-EN-05 | injected instructions in page / JSON-LD | the record is never written from a page; no decision, state or switch changes | war room crawled-prompt-injection invariant | COVERED |
| B-EN-06 | no record / vault blob unreadable | IE (no record); BLOCKED `VAULT_UNAVAILABLE` (blob unreadable; never compared with null) | N>= 4 | COVERED on 186471eb (ADR 0051 decision 43) |

## 9. Osei-lite - quarantine, observations, refresh tiers (`agents/osei.py`)

| Id | Input | Required output | Pass criterion | Status |
|---|---|---|---|---|
| B-OS-01 | a malformed / oversized input | Record `{kind, reason, url (code form), bytes, sha256, size_basis, truncated, total_bytes, declared_bytes, at}`; reason from the closed list `XML_DOCTYPE_REFUSED XML_MALFORMED TOO_LARGE NOT_UTF8 JSONLD_INVALID JSONLD_TOO_LARGE PROVIDER_ANSWER_MALFORMED DECOMPRESS_FAILED`; `size_basis` from the closed list `wire` (the body as received), `decoded` (after content / gzip decoding), `read_before_stop` (TOO_LARGE: bytes read before the stop; `truncated: true`; `declared_bytes` when Content-Length was sent), `block_prefix` (JSONLD_TOO_LARGE: the prefix read; `truncated: true`; `total_bytes` of the whole block), `serialised_answer`, `none_in_hand` (bytes / sha256 null). The record states which bytes the size and hash are of; never content | Per reason x basis: N>= 10; a 12-byte truncated gzip -> bytes 12, basis wire (not 0 / SHA of empty); a 400,066-byte block -> bytes 262,144, basis block_prefix, total 400,066 | PARTIAL (test_sitemap_too_large_quarantine_records_bytes_read, test_oversized_jsonld_is_recorded_truncated_with_its_whole_size); basis vocabulary NEW [r3 SHOULD delia _xml; r3 survivor osei.py:64 OPEN] |
| B-OS-02 | quarantined input | Never parsed further; contributes no finding beyond its quarantine finding | mutation: parsing a quarantined body is killed | COVERED (war room xml-entity-quarantine `quarantined_unparsed`) |
| B-OS-03 | every fetch | Observation `{kind, url (code form), state, observed_at, refresh_due}`; refresh_due = observed_at + tier | N>= 50 audits, every observation's due = tier | COVERED (test_refresh_tiers, test_every_fetch_gets_an_observation) |
| B-OS-04 | caps | At most 200 quarantine and 500 observation records a run; the rest counted `quarantined_dropped` / `observations_dropped` (never silently lost) | 270 records -> 200 + 70 dropped | COVERED (test_osei_caps_are_counted) |
| B-OS-05 | refresh tiers | robots 1 d, sitemap 1 d, llms_txt 7 d, page 7 d, structured_data 7 d, ai_probe 7 d, entity_record 180 d; a hint only (Wave 1 schedules nothing from them, disclosed) | exact table | COVERED (mutant sitemap tier killed in r1b) |

## 10. Osei drift (`agents/drift.py`)

| Id | Input | Required output | Pass criterion | Status |
|---|---|---|---|---|
| B-DR-01 | two completed reports, same tenant / domain / scheme | Findings keyed by (agent, code, url in code form, detail token / field / engine / prompt_index); a key in one run only, or with a different (severity, decision), is a change (APPEARED / DISAPPEARED / CHANGED); other tenant / target -> NotComparable (404 at the route) | identical runs -> 0 changes (N>= 50 pairs) | COVERED (test_identical_runs_have_no_changes, test_drift_key_separates_bot_tokens) |
| B-DR-02 | each change | Exactly one class by the FIRST rule whose evidence holds, R1..R8, the evidence recorded (`rule`, values) | 0 changes without evidence; class in the closed list | COVERED (test_every_class_is_in_the_closed_list) |
| B-DR-03 | R1 TOOL_FAILURE | Any of, in EITHER run: the agent's outcome not OK / PARTIAL; the page's fetch state not OK; the finding is itself a failed / incomplete / unchecked / approximate observation (`*_UNREADABLE`, `*_UNREACHABLE`, `*_UNCHECKED`, `*_INCOMPLETE`, `*_APPROXIMATE`, `PAGE_NOT_READ_*`, `PAGE_BLOCKED_FOR_OUR_CRAWLER` with reason unreachable); the finding's own `detail.fetch_state` present and not OK; robots.txt unreachable / too large / incomplete; llms.txt state unreadable or missing; that sitemap URL unreadable; a finding over all sitemaps with any sitemap fetch failure or robots_unchecked | 0 REAL_CHANGE among N>= 256 / 1,556 pairs where an own observation failed on either side (16 kinds x 16; r2 corpus 240 sites x 8 perturbations) | COVERED (test_drift_* in test_wave1_cert / _r2; war room drift-classification) [r1 M-O1, r2 Osei M1] |
| B-DR-04 | R2 MEASUREMENT_ERROR / R3 UNKNOWN | Ruler moved (bot family list version, structured-data rule table, entity record version, prompt set) and the measured thing unchanged by fingerprint / robots hash -> R2; ruler AND thing changed -> R3 UNKNOWN (never REAL_CHANGE) | N>= 20 each | COVERED (test_rule_table_change_with_same_page_is_measurement_error, test_drift_ruler_and_page_both_moved_is_unknown_r3) [r3 survivor drift.py:231 OPEN] |
| B-DR-05 | codes key moved | `codes_key_id` differs between the runs -> every change R2 MEASUREMENT_ERROR (`ruler: codes_key`) | rotation pair: 0 REAL_CHANGE | NEW |
| B-DR-06 | R4-R6 answer engines | model versions differ -> MODEL_DRIFT; same, both 95% intervals overlap -> SAMPLING_NOISE; else SURFACE_DRIFT; an interval missing -> R8 UNKNOWN | N>= 40 | COVERED (test_answer_engine_rules) |
| B-DR-07 | R7 REAL_CHANGE | Only with evidence: page fingerprint / served status / final URL (code) / X-Robots-Tag differs; robots SHA-256 differs; Delia's observed counts or states differ (no R1 condition holding) | real-change controls (noindex added, robots change, content change, sitemap URL count change) classed REAL_CHANGE in >= 95% of N>= 100 pairs, the rest UNKNOWN, 0 other classes | COVERED (test_drift_real_sitemap_change_stays_real; r2 corpus: noindex 28/28, robots 30 REAL + 26 UNKNOWN) |
| B-DR-08 | overturned | `overturned_by_agent` counts only DISAPPEARED changes classed MEASUREMENT_ERROR or SAMPLING_NOISE | mutation widening the set killed | COVERED (test_vanished_finding_after_a_ruler_change_counts_as_overturned) |

## 11. Uncertainty labels (cross-cutting, every agent)

| Id | Rule | Pass criterion | Status |
|---|---|---|---|
| B-UL-01 | `data_class` `measured` only for a value this service observed with a successful fetch / parse; `inferred` only for the listed inferred rules (`SITEMAP_LASTMOD_ALL_IDENTICAL`); `effect_class` null on every Wave-1 finding | Invariant over every finding produced by the whole suite and war room: 0 violations | PARTIAL (no global invariant test) NEW |
| B-UL-02 | A finding derived from this service's own failed, refused, bounded, approximate or undecided observation has decision IE and severity `info` (except `PAGE_UNREACHABLE high/IE`, `SITEMAP_UNREADABLE medium/IE`, listed), never ACT / TEST, and names the cause (fetch_state or cap) | Invariant over the whole suite: 0 findings with decision ACT / TEST whose detail carries a non-OK fetch_state, a cap, `undecided` or `complete: false` | NEW (r2 M3, r2 Selene CANONICAL_TARGET_NOT_200 and r3 MUST-2 / MUST-3 are instances) |
| B-UL-03 | Roman readiness and any per-engine checklist carry `heuristic: true`, `calibrated: false` and the label; no score, grade or percentage anywhere; findings of different kinds never added | 0 keys matching `score|grade|percent|rating` in reports and client documents | COVERED (CR-4 tests, test_wave1_cert readiness) |
| B-UL-04 | Envelope outcome, findings and facts are separate; an agent that observed nothing reports no findings; NOT_CONNECTED ports listed | Invariant over the suite | COVERED (envelope tests) |

## 12. Attack classes

Universal bound for EVERY case of every class below (asserted per case, in addition to its own row): HTTP 5xx 0;
connections to a non-public address 0; an uncaught exception in a request handler 0; one audit request <= 30 s wall
on the fixture without injected latency (with injected latency L per response: <= 30 s + requests x L); one log chunk
POST <= 10 s wall; `/health` during the attack p99 <= 250 ms and max <= 1,000 ms (measured r1 pre-fix: 12.9 s stall;
r3: p99 94.5 ms, max 128.4 ms); traced memory peak per request <= 96 MiB and process RSS <= 768 MiB (measured r1:
37.5 MB peak with UTF-16 billion laughs pre-fix; r3: maxrss 288 MB over the battery); the D-section scan clean.
Generators are seeded (seed recorded in the failure message) and build every value at run time (SUITE_PLAN 3).

| Id | Class [source] | Generator | N>= fast / lead | Required outcome | Own bound | Status |
|---|---|---|---|---|---|---|
| A-01 | robots wildcard backtracking [r1 M-S1] | rules of k = 1..12 `*` with literal pieces that almost match; paths 60-8,000 octets of the near-match alphabet; via the matrix, the fetcher, Delia's listed URLs, the log task | 200 / 2,000 | Decisions equal to the oracle or undecided (never wrong); undecided -> the IE findings of B-SE-06 / B-DE-11 / B-LG-03 | one decision <= 5 ms CPU; one step <= 2.0 s CPU | COVERED (test_hostile_wildcards_are_linear_not_backtracking; war room robots-matching-cost wildcards_6x60) |
| A-02 | rule floods [r1 S24-S26] | 2,000-27,530 distinct rules within 512,000 octets: distinct prefixes, shared prefixes, all-tried, named-group floods, 10-wildcard rules, repeated rules | 20 / 200 | Every rule used as written (no caps); decisions equal to the oracle within the step work; undecided disclosed | one step <= 2.0 s CPU; audit of 10 paths <= 5 s wall | COVERED (test_no_rule_bounds_every_rule_is_used_exactly; war room rules_20000, rules_2000_sitemap_500) |
| A-03 | costliest-per-unit matching [r3 MUST-4] | needle of m = 50-99 octets (`a`*(m-2) + `ba`) x 4,348 rules, paths 2,047 / 8,000 / 29,000 octets of `a`; the 24,908 distinct-prefix shape | 6 / 60 | units charged track CPU (B-RB-09 c) | one step <= 2.0 s CPU (measured r3: 6.02-8.35 s) | OPEN |
| A-04 | hostile percent-encodings [r2 M1] | reserved `%2A %2F %3F %24 %23 %25`, lower / upper hex, invalid UTF-8 (`%FF %FE %C0%AF` overlong, lone continuation), raw non-ASCII, stray `%`, `%00`, raw `*` / `$` in paths, in rules and paths, all four combinations | 4,000 / 40,000 rule sets | B-RB-04 | as A-01 | COVERED (test_percent_encoding_agrees_with_an_rfc9309_octet_oracle) |
| A-05 | robots text encodings and line shapes [r1 S-S1, NEW] | UTF-8 BOM, UTF-16 LE / BE file (with / without BOM), Latin-1 octets, CR-only / CRLF / mixed endings, NUL bytes, one 600 KiB line, no final newline, `User-Agent` case and spacing variants, comments inside values | 60 / 600 | BOM per B-RB-05; non-UTF-8 octets kept per B-RB-04; NUL / control octets compared as `%XX`; counts per B-RB-06; a UTF-16 robots.txt is read as UTF-8 octets (RFC 9309: UTF-8) and its rules do not match: reported `ROBOTS_LINES_IGNORED`, never crash | as A-01 | PARTIAL (BOM only) |
| A-06 | robots size and wire shapes [r2 NIT, r3 MUST-2] | 499 / 500 / 501 KiB and 2 MiB files (ASCII and 3-octet characters); declared Content-Length 10 GiB; gzip of empty members 2.2 MB; gzip bomb robots (decodes > 500 KiB); chunked endless | 12 / 60 | B-RB-07, B-RB-08, B-SE-03: parse the first 512,000 decoded octets; never `ROBOTS_UNREACHABLE` for a body that answered 200; decoded bytes kept <= 512,000 | robots fetch <= deadline; peak <= 16 MiB | PARTIAL (test_robots_over_the_parse_limit_has_a_limit_entry, test_robots_fetch_still_too_large_stays_fail_closed); gzip-empty OPEN |
| A-07 | robots status / redirect hostility [NEW] | 500, 502, 503, 429, 401, 403, 404, 410, 451; redirect to another host, to an internal address, loop, 6 hops, 5 hops; trickled headers | 24 / 120 | B-RB-08 / B-SE-01 / B-SE-02 / Q-06; redirect to an internal address refused (unreachable), never connected | as B-FT-06 | PARTIAL (5xx, 4xx covered) |
| A-08 | XML DOCTYPE / ENTITY in every encoding [r1 M-D1, r1c battery] | 24 forms (internal entity, billion laughs, quadratic blow-up, external entity / XXE file and http, parameter entity, external DTD, DOCTYPE past byte 4,096, DOCTYPE in a comment-like position, entity in an index child) x 11 encodings (UTF-8 +/- BOM, UTF-16 LE / BE +/- BOM, UTF-32 LE / BE +/- BOM, ISO-8859-1) x odd declaration names (`utf-16-le`, `UTF16`, `ucs-2`) | 264 / 2,640 | quarantined `XML_DOCTYPE_REFUSED` (or `XML_MALFORMED` for an undecodable one), 0 entity expansions, 0 external fetches; ordinary UTF-16 / UTF-32 sitemaps still read | <= 1 s CPU a file; expanded characters 0 | COVERED (test_entity_quarantine_in_every_encoding, test_doctype_past_the_first_4096_bytes_is_refused; war room xml-entity-quarantine, giant-and-malformed-files) |
| A-09 | decompression bombs [r1 r2, war room] | gzip / deflate / raw deflate / multi-member / stacked / br / zstd / compress / corrupt / trailing garbage / truncated (12 B) / 60 MiB of zeros x 10 members, as Content-Encoding and as `.gz` sitemap | 26 / 260 | B-FT-05 states; sitemap per B-DE-04; quarantine per B-OS-01 | decoded <= cap + 64 KiB; peak <= 96 MiB | COVERED (war room decompression-bombs) |
| A-10 | deep nesting [r1c battery] | XML depth 200-100,000; HTML depth 10,000; JSON-LD depth 10,000; long attribute / text runs | 12 / 60 | parse or quarantine `XML_MALFORMED` / `JSONLD_INVALID`; no RecursionError escapes; no crash | <= 2 s CPU a document | COVERED (giant-and-malformed-files deep_xml) |
| A-11 | huge / recursive sitemap sets [r1 r2 big site] | index with 200 children, self loop, mutual loop, cross-host children, 8 x 45,000 URLs, 60,000 URLs gzip, 100,001 URLs total, child pointing at robots.txt / llms.txt | 10 / 40 | B-DE-07 / B-DE-09 / B-DE-10 / B-DE-03 counts; each URL fetched once; disclosure facts exact | fetches <= 1 + 10 children; memory <= 96 MiB | PARTIAL (test_index_children_cap_cross_host_and_loop_guard, test_large_site_all_sitemaps_read_on_a_slow_run); 50 MiB files NEW |
| A-12 | SSRF-shaped URLs everywhere [r1 R2, war room] | 13+ internal / legacy / mapped / rebinding / port / scheme / userinfo targets x 7 positions (redirect Location, canonical, robots `Sitemap:`, sitemap `loc`, index child, llms.txt link, homepage link) | 91 / 455 | 0 connections to non-public addresses; findings per the position's rule; links in llms.txt never fetched | as universal | COVERED (war room ssrf-internal-targets, ssrf-ports-schemes-credentials; r1 76 cases, 0) |
| A-13 | long Location [r3 MUST-3] | Location lengths 1,990-2,100, 4,000, 8,192, 8,193, 64 KiB; relative and absolute; multi-hop | 20 / 100 | B-FT-04: 0 truncated follows; > 8,192 -> REDIRECT_TOO_LONG / IE | as universal | OPEN |
| A-14 | slow ordinary sites [r1b t_slow / t_slow2] | Allow: / robots, 6-9 pages with canonicals, sitemap, llms.txt; every response 0.7-0.8 s; 8 sitemaps x 3,000 URLs at 0.7 s | 3 / 12 | Every page and file read; 0 `BLOCKED_BY_ROBOTS`, 0 `*_UNREADABLE`, 0 `CANONICAL_TARGET_*`, Delia OK (same findings as with 0 latency) | wall <= 30 s + requests x 0.8 s | COVERED (test_slow_ordinary_site_is_never_blocked_or_unreadable; war room robots-and-safety-round-2 slow_site) |
| A-15 | huge pages and lying sizes | page 2 MiB + 1, declared 10 GiB, declared 0 with a body, endless chunked, 40 x 1 MiB pages | 10 / 40 | TOO_LARGE + quarantine `read_before_stop`; `PAGE_UNREACHABLE high/IE`; nothing parsed from a quarantined body | as B-FT-05 | COVERED (giant-and-malformed-files) |
| A-16 | hostile log lines [war room log-hostile-content] | huge line, invalid UTF-8, NUL and controls, CRLF injection, injection User-Agent, deep / mistyped JSON lines, bad IP / status, 100,000 distinct paths, no trailing newline, `;` matrix parameters, queries, encoded paths, IPv6 / zoned IPs, 8,192 / 8,193-byte lines | 60 / 600 | exact quarantine counts (B-LG-01); 0 state change from log text; templates per B-CD-04 | chunk <= 10 s wall | COVERED (test_adversarial_lines_are_quarantined_and_counted, test_injection_text_in_paths_and_user_agents_is_data) |
| A-17 | log robots cost [r1 S12] | 450 / 600-line chunks against A-01 / A-03 files | 4 / 20 | undecided lines counted `robots_unchecked`, report PARTIAL, never wrong | chunk step <= 2.0 s CPU | PARTIAL (A-03 shapes OPEN) |
| A-18 | planted personal data and secrets in every input surface [r1 M-S2..M-O3, r2 M5, r3 MUST-1] | SURFACES (32): robots `Sitemap:` line host / path, robots comment, robots rule pattern, canonical same host / other host (host label and path), redirect Location host / path / query, X-Robots-Tag bot prefix and value, meta robots name, page title, h1-h6, meta description, html lang, JSON-LD @type / string values / Organization name / telephone / street / sameAs, sitemap loc host / path / query, sitemap lastmod, index child loc, namespace, root element name, llms.txt text and links, robots.txt via log ingest, log path / query / User-Agent / referer / IP, content type, homepage phone text. SHAPES (21): e-mail (6 spellings), phone (E.164, NANP 4 forms), SSN shape, 16-digit Luhn, strong-key prefixes (synthetic, built at run time), JWT shape, UUID, IPv4 / IPv6, 16 / 32 / 64 hex, base64 40, letters-only given name, surname, first-last slug, name from a list NOT used by the implementation, common password, random-letter secret, pronounceable secret, glued words, upper case, non-ASCII name, date of birth shaped date | 1,000 per agent / 3,000 per agent (Selene, log task, Delia, Roman, Osei, entity check: >= 6,000 / 18,000) | D-section bar: 0 readable outside the allowlist in every scanned form; positive control: each planted value on the CLIENT'S OWN evidence is found in the owner display (D-07 / D-08) | as universal | PARTIAL (dictionary design measured r2: 69 / 1,175 common passwords readable; r3: host labels 200 / 200 readable) -> re-baselined by D |
| A-19 | prompt injection in crawled content [war room] | instruction strings in titles, JSON-LD, llms.txt, robots comments, log User-Agents, provider answers | 12 / 60 | 0 changes to a decision, state, switch, tenant or the entity record | as universal | COVERED (war room crawled-prompt-injection) |
| A-20 | markup injection in owner displays [CR-5] | HTML / Markdown / autolink / CSP-breaking values in every resolved field of the client report | 50 / 500 | HTML parser sees only the renderer's tags and attributes; Markdown values only in code spans or escaped; links only to the tenant's audited pages | as universal | COVERED for current fields (test_client_report); resolved-code fields NEW |
| A-21 | own-domain lookalikes [war room answer-engine-lookalike-citations] | `site.test.evil.test`, `evilsite.test`, punycode / Cyrillic lookalikes, `site.tes`, `www-site.test`, `site.test@evil.test`, `evil.test%2esite.test`, trailing dot, upper case, port, IPv6 literal | 40 / 200 | B-CD-06: only the registered domain, its `www.` twin and real subdomains are own; every lookalike is `{c:host:..}` and other-host for findings | as universal | PARTIAL (Callum only) |
| A-22 | code stability and isolation [NEW] | the same raw value in 2 audits of one tenant, in 2 tenants, across a key rotation; 1,000,000 distinct values | 3 / 3 | B-CD-01, B-CD-02, B-DR-05 | <= 2 us a code | NEW |
| A-23 | vault unavailable [ADR 0051] | no key, key removed between runs, evidence blob lost / tampered | 4 / 4 | audit refused `VAULT_UNAVAILABLE` before any fetch (no key); owner display shows `available: false`, never raw, never an error; nothing raw written | as universal | COVERED on 186471eb (test_vault_wiring) for reports; evidence map NEW |
| A-24 | kill switch mid-run [war room] | global, write, tenant, capability:audit / fetch, provider:web at each step | 8 / 40 | KILLED; no request after; completion refused, audit `interrupted` | as universal | COVERED (war room kill-switch-*) |
| A-25 | cross-tenant access [r1 R4, r2, r3] | every route x (other tenant id, other tenant token, no token, hub without token, Finance / Compliance caller) incl. evidence-map display | 41 / 41 | byte-identical 404 as for a missing object, or 401 / 403; 0 evidence-map reads for a non-owner | as universal | COVERED (war room cross-tenant-access; r1b 41/41); evidence map NEW |
| A-26 | unbounded collections [r1b (g)] | 100,000 lastmods, 100,000 canonicals-like links, 2,000 links, 300 headings, 30+ JSON-LD blocks | 6 / 30 | every list bounded by its stated cap and the excess counted | traced <= 96 MiB | PARTIAL |
| A-27 | hostile robots meta / X-Robots-Tag [r1 S-S2] | 50 headers, unknown bot prefixes, case variants, `unavailable_after: <date>`, `max-snippet:-1`, `noindex` inside values, meta names for every family | 100 / 500 | B-SE-12 per engine; directives stored only from the closed list; values per D-01 | as universal | COVERED (test_noindex_is_per_engine) |
| A-28 | drift with failed / partial / hostile reports [r1 M-O1, r1b t_drift_*, r1c driftbat] | 16 failure kinds on either side x 16 sites; reports missing fields; fetch_state in odd places | 256 / 1,556 | B-DR-03: 0 REAL_CHANGE on a failed own observation; 0 exceptions | <= 1 s a comparison | COVERED (war room drift-classification) |
| A-29 | JSON-LD hostility [r1b step 18, r2 R7] | 270 invalid blocks, 400,043-byte block, 262,144 / 262,145-character boundary, BOM in block, duplicate keys, NaN, huge numbers | 20 / 100 | B-RO-06 / B-OS-01 / B-OS-04 | as universal | COVERED (test_oversized_jsonld_is_recorded_truncated_with_its_whole_size, test_osei_caps_are_counted) |
| A-30 | restart mid-ingest [r1b t_restart] | restart before chunk 1, after chunk 1, after the last | 3 / 3 | robots text gone -> `robots_unchecked`, PARTIAL, `LOG_ROBOTS_CHECK_INCOMPLETE`; never a wrong count | as universal | COVERED (test_restart_rebuilds_totals_and_retention_expires) |
| A-31 | mutation survivors [r1 r1b r1c] | every mutant listed in the catalogue (SUITE_PLAN 6): r1 Selene 9, Delia 13, Roman 10, Osei 3; r1b S4 S11 S15 S16, D1 D2 D3 D9 D14, R2 R7 R13 R15 R18, O10 O11 O13 O14 O18; r1c robots.py:102 (x4), logs.py:179, delia.py:356 / :73 / :345, roman.py:157 (equivalent), osei.py:64, drift.py:187 / :231 | all / all | each behavioural mutant killed by a conformance test; an equivalent mutant needs a written proof in the catalogue | lead gate <= 90 min | PARTIAL (r2 M4: 94 / 94 killed; r1c 8 behavioural survivors OPEN) |
| A-32 | licence [r3 MUST-5] | bundled data files and their sources | all | only permissive sources or the approved list (CC-BY-4.0, Unicode-3.0, MIT-CMU, CNRI-Python, PDFium); every bundled source has a THIRD_PARTY_NOTICES entry; no NLTK-derived, wordfreq or schema.org data in the default build | n/a | OPEN |
| A-33 | over-limit numbers and counts in outputs | 10^18 status / count values, negative Content-Length, 64-bit HLL edge | 10 / 50 | sink guard `number` class accepts them (ADR 0051), no refusal of a legitimate write (availability) | as universal | PARTIAL |

## 13. Data safety under "store codes" (replaces dictionary-based detection)

### D-01 The allowlist (exhaustive; everything not listed here that came from outside the service is a CODE or a PLACEHOLDER)

| Part | Readable | Rule |
|---|---|---|
| D-01 H | the tenant's registered domains and their `www.` twin | lower-case IDNA, trailing dot removed, exactly as registered (ADR 0017 decision 7); any other host, including the tenant's other subdomains (Q-02), is `{c:host:..}` |
| D-01 W | well-known file names, as a WHOLE path segment, case-insensitive, closed grammar | `robots.txt llms.txt llms-full.txt favicon.ico ads.txt app-ads.txt security.txt humans.txt manifest.json site.webmanifest browserconfig.xml apple-touch-icon.png rss.xml atom.xml feed index.html index.php .well-known`; sitemap names: `sitemap.xml`, `sitemap_index.xml`, `sitemap-index.xml`, `wp-sitemap.xml`, `sitemap[-_]<S>[-_]<n>.xml`, `<S>-sitemap<n>.xml`, `wp-sitemap-<S>-<S>-<n>.xml`, each also with `.gz`, where `<S>` is one of `index pages page posts post products product categories category tags tag images image video videos news blog misc main authors author taxonomies users` and `<n>` is empty or 1-3 ASCII digits; the root path `/`. Nothing else matching `sitemap*` is readable |
| D-01 S | structural machine values made by the service | finding codes, severities, data classes, decisions, outcomes, fetch states, status classes, quarantine reasons, size bases, change classes, rule ids (closed vocabularies in the code); HTTP status codes 100-599; counts, lengths, sizes, units, limits, indices; booleans; null; the service's own timestamps (`started_at`, `observed_at`, `refresh_due`, `at`), ids, versions and key ids; hashes and keyed HMACs the service computes (D-03); the service's own constant texts (methodology, limitations, notes: exact constants of the code base) |
| D-01 V | closed vocabularies a crawled value is mapped onto | robots directives (`safe_url.ROBOTS_DIRECTIVES` and directive values `-1`, 0-999999, `none standard large`), bot family tokens of `agents/bots.py`, engine names, media types of the closed `MEDIA_TYPES` list, XML root names of `ROOT_NAMES`, the sitemaps.org 0.9 namespace constant, llms.txt issue codes, URL scheme names of B-CD-03, the placeholders of 0.1 |
| D-01 L | language tags | language[-script][-region] where each subtag is valid in the CLDR validity data (Unicode-3.0, approved; Q-05) |
| D-01 T | schema.org type names | exactly the types named in the service's own rule table (`parse.SCHEMA_RULES` keys, `ORG_TYPES`, `LOCAL_BUSINESS_TYPES`), hand-written by the service (Q-04) |
| D-01 R | robots rule patterns | a pattern made only of `/`, `*` and a final `$` (`/`, `/*`, `*`, `/$`, `/*$`), or `/` + one D-01 W name, with its kind `allow` / `disallow`; every other pattern `{c:rule:..}` |
| D-01 O | owner additions | entries of a versioned data file `allowlist_owner.json`, each with the owner's approval reference; empty at approval of this spec |

### D-02 Per output: what may appear readable

| Id | Output | Readable | Coded / placeholder | Never |
|---|---|---|---|---|
| D-02a | `data/seo_log.jsonl` | sink-guard machine classes only (ADR 0051 decisions 32, 41): ids, refs, keyed HMACs, machine facts (counts, outcome codes) | nothing needs a code here: findings live in the report blob | any crawled value, any code (`{c:..}` is refused by the guard's classes) |
| D-02b | ledger payloads | as D-02a | - | as D-02a |
| D-02c | Python logging, every level incl. DEBUG | literal message templates and machine arguments (`log_sink`, ADR 0051 decision 44); httpx / httpcore at WARNING | - | URLs, hosts of other sites, paths, any argument that is not a machine value |
| D-02d | vault files (`citation_vault.jsonl`, operator text vault) | nothing in plaintext (AES-256-GCM lines) | - | any plaintext field |
| D-02e | audit report blob plaintext (= the owner's audit view, `POST` / `GET .../audits/{aid}`) | D-01 only | every other value | raw outside values |
| D-02f | `evidence_map` blob (0.1) | the raw observed values, bounded | - | read by anyone but the owner display surfaces D-02g / D-02h |
| D-02g | owner display: client report Markdown / HTML (CR-1..CR-11) | D-01 plus codes RESOLVED through the run's evidence map, only for the owning tenant, after CR-5 cleaning (controls removed, 160 characters, 5 items, escaping, never a link except the tenant's audited pages) | an unresolvable code (map lost, value over the bound) is shown `[not available]` | another tenant's values; any value for a caller other than the owner |
| D-02h | owner display: dashboard / hub GET with `resolve=evidence` (owner caller with tenant authentication only) | as D-02g | as D-02g | as D-02g |
| D-02i | drift report blob and its view | D-01 | codes (stable per B-CD-01) | raw |
| D-02j | log ingest report blob and its view | D-01; per-family counts, status mix, HLL estimate | path templates per B-CD-04 | raw paths, queries, IPs, UAs, referers |
| D-02k | API views for Finance (31), Compliance (38), exports, evidence view, `/health`, `/status`, manager queues and scorecards | D-01 (Finance: no report body; Compliance: report form, never resolved) | codes | evidence-map values |
| D-02l | error bodies | reason codes, closed `loc` names (ADR 0051 decision 45) | - | input echo, crawled values |
| D-02m | requests the service sends | a value is sent only to the host it was found on (robots / sitemaps / pages of the audited site) or to a declared same-site sitemap; no planted value is ever placed in a request to another host | - | planted values in requests to other hosts |

### D-03 Hashes
SHA-256 of crawled bodies (robots `text_sha256`, page fingerprints, quarantine `sha256`) appear only inside the
encrypted report blob (owner view, Compliance export; Q-08). In the log and the ledger every hash of client content is
a keyed HMAC (ADR 0051 decision 47 table; a hash field not in that table fails `test_every_hash_field_has_a_decision`).

### D-04 Never stored in any form, not even coded
Client IP addresses (HyperLogLog registers only), User-Agent strings (family token only), referers, raw log lines,
page bodies, page text, provider answer bodies (ADR 0017 decision 18).

### D-05 The planted-value bar (all classes, no residual)
Over every A-18 case: the number of planted values found READABLE in D-02a-e, D-02i-m, in any of the scanned forms
(raw, lower / upper case, hex, base64 and base64url at 3 alignments, percent-encoded, JSON-escaped, digits-only,
punycode for host labels) = **0**, for every shape and every surface. A planted value is generated so that it is not a
substring of any service constant text, finding code or vocabulary entry (checked at run time), so every hit is a
leak; the r2 "upper bound" ambiguity does not arise. The generator never plants into a D-01 slot (the tenant's
registered domain, a well-known file name).

### D-06 Positive control (the evidence is still the client's)
Every planted value that sits on the client's own evidence and within the D-02g bounds is FOUND (resolved) in the
owner display D-02g / D-02h: 100% of them; and found in NO display for another tenant, for Finance or for Compliance.

### D-07 Availability
Sink-guard refusals (`SINK_REFUSED`) over the whole conformance suite and war room: 0, except in the closed list of
tests that provoke one on purpose (ADR 0051 decision 41). Storing codes must never make a legitimate audit fail.

### D-08 Universal canary
ADR 0051's `test_canary_all_routes` is extended with crawl-originated canaries on all 32 A-18 surfaces; it passes
with 0 hits outside the known identifier limit of ADR 0051 decision 49.

## 14. Over-tightening bars

| Id | Bar | Pass criterion | Status |
|---|---|---|---|
| O-01 | Correct findings on ordinary sites. Corpus: >= 400 generated realistic sites (fast tier 40), >= 16 languages (en de fr es pt it pl tr nl sv id cs ru uk ja zh ko ar he hi th), CMS shapes (WordPress + Yoast, Shopify, Squarespace, Wix, Next.js, Magento, static), typical robots.txt (Google's documented examples, 120 KB / 1,500-rule files, CMS defaults), sitemaps (index, gzip, image / news / hreflang extensions, 45,000 URLs, files up to 50 MiB), llms.txt (conformant and common deviations), pages with typical meta, canonicals, hreflang, JSON-LD. Oracle: the generator's ground truth plus the independent reference implementations of SUITE_PLAN 4 (what a careful human reading the RFCs and protocol pages concludes) | Per site the multiset of (agent, code, severity, data_class, decision, resolved location) EQUALS the oracle's: 400 / 400 sites; every agent outcome OK on every site meant to be clean; 0 ordinary pages BLOCKED / UNREADABLE / IE / approximate | PARTIAL (r2 400-site corpus: 0 ordinary page blocked; no oracle-equality test in repo) |
| O-02 | Differential | vs a4af277 and 91db0abb on the O-01 corpus: every changed finding listed with the B item that requires it; unexplained changes 0 | PARTIAL (r2 measured: -59 findings vs a4af277, all explained) |
| O-03 | Stable codes | two audits of an unchanged site of one tenant: 100% of codes equal, drift 0 changes; the same site under two tenants: 0 equal codes | NEW |
| O-04 | Useful client report | on the corpus, 100% of findings with a location have a non-empty "Where" in the owner display; 100% of codes in the owner display resolve; every D-01 W name in a finding is readable in both the stored and the displayed form (r3: `sitemap.xml`, `sitemap_index.xml`, `post-sitemap.xml`, `llms.txt`, `favicon.ico` were digested) | NEW / OPEN [r3 SHOULD] |
| O-05 | Grouping | counts and groupings by code equal those by raw value on the corpus (0 collisions); log top path templates group exactly as the placeholder reference | NEW |
| O-06 | Large real-shaped files | a 119,906-byte robots.txt of 1,500 distinct-prefix + 500 query / wildcard rules: nothing approximate, 0 undecided in Selene, Delia (500 URLs) and a 450-line log chunk; a 50 MiB / 50,000-URL sitemap read | PARTIAL (r2 measured robots part) |
| O-07 | Slow / large ordinary sites | A-14 outcome on the corpus with 0.7 s latency on 10% of sites: findings equal to the 0-latency run | COVERED in part (A-14) |

## 15. Known residuals (owner accepts or rejects each; sizes where measured)

| Id | Residual | Size | Proposed |
|---|---|---|---|
| R-01 | 64-bit codes can collide | expected collisions n^2 / 2^65: 2.7e-8 at 10^6 values a tenant | accept |
| R-02 | Within one tenant's report, equal values have equal codes (equality and frequency are visible to whoever reads the report: the owner, Compliance) | by design | accept |
| R-03 | The tenant's registered domain is readable even if it is name-shaped (ADR 0051 decision 49 known limit) | by design | accept |
| R-04 | A well-known sitemap name can carry a 1-3 digit number (`sitemap-12.xml`) readable | values 0-999 only | accept |
| R-05 | Measured attributes are readable: lengths, counts, status codes, booleans of client content (a side channel, no value) | by design | accept |
| R-06 | The owner display shows outside content found on the client's own pages (other sites' hosts in canonicals, third-party names in links) to the owning tenant | by design (Q-03) | accept |
| R-07 | Plain SHA-256 of crawled bodies inside the encrypted report: an owner / Compliance reader can confirm a guess of a short body | bodies of < 64 bytes only practically | accept (Q-08) |
| R-08 | Compliance (38) sees codes, not the client's evidence | by design | accept |
| R-09 | A lost or rotated-away vault key makes the evidence map unreadable: the owner display shows `[not available]`; codes stay consistent until the codes key rotates | by design | accept |
| R-10 | ADR 0017 Wave-1 limits: one network location and time; no rendering; UA-only access differential; bot list incomplete; llms.txt non-standard; readiness heuristic and uncalibrated; fixtures only, nothing real crawled | existing | accept |
| R-11 | Osei-lite is not full Osei: refresh tiers are hints (nothing schedules a refresh), no panel cadence, no first-party integration beyond Search Console [r1 S22, r1c] | existing | accept |
| R-12 | A UTF-16 sitemap without a BOM is `XML_MALFORMED` (XML 1.0 4.3.3 requires the BOM) [r1c battery: 4 cases] | 4 of 421 battery cases | accept |
| R-13 | CPU seconds per step are calibrated on the reference box; on a slower machine the deterministic work bound holds but the seconds scale | factor = machine speed | accept |
| R-14 | Sitemaps past 10 children, listed URLs past the 500 robots sample and past 100,000 examined are counted, not checked | disclosed counts | accept |
| R-15 | Equivalent mutants (roman.py:157 whitespace already folded by `parse._s`; delia D1-D3 redundant DTD layers; D14 regex already excludes `##`) [r1b, r1c] | 5 | accept with written proofs in the catalogue |
| R-16 | Without the schema.org vocabulary, an @type outside the service's rule table is shown to the owner only through the evidence map (stored coded) | all types outside the table | accept unless Q-04 approves the CC BY-SA list |
| R-17 | robots.txt `429` treated per Q-06 | - | per Q-06 |
| R-18 | Retired residual: under the dictionary design names and passwords that are ordinary words stayed readable (r2 measured: common passwords 69 / 1,175, faker names 2 / 1,516, other-source names 28 / 1,200; r3: host labels 200 / 200). Under this spec the bar is 0 (D-05); listed here so the owner sees what the redesign removes | 0 required | n/a |

## 16. Certification bar per agent

An agent is CERTIFIED when every cell of its row holds; any failing cell rejects it; anything else a reviewer finds
goes to section 15 for the owner.

| Agent | B items (all must pass) | A classes that must pass | D | O | Mutants | War room | Suite |
|---|---|---|---|---|---|---|---|
| Shared primitives (robots, fetch, codes) | B-RB-01..11, B-FT-01..10, B-CD-01..06 (27) | A-01..07, A-12, A-13, A-15, A-21, A-22, A-32 | D-05 = 0, D-06 100%, D-07 0 | O-01, O-03, O-05, O-06 | >= 20 each module, 100% behavioural killed | seo-py seeds 1-5 + every replay PASS | conformance fast tier green twice, < 10 min; lead gate green |
| Selene | B-SE-01..18 (18) + B-UL-01..04 | A-01..03, A-05..07, A-12..15, A-18, A-19, A-21, A-24, A-27, A-31 | as above, Selene surfaces | O-01, O-02, O-04, O-07 | >= 20, 100% | as above | as above |
| Selene log task | B-LG-01..09 (9) + B-UL | A-16, A-17, A-18, A-25, A-30, A-31 | as above, log surfaces | O-01 (log part), O-05 | >= 20, 100% | as above | as above |
| Delia | B-DE-01..16 (16) + B-UL | A-04, A-08..12, A-14, A-18, A-26, A-31 | as above, Delia surfaces | O-01, O-02, O-04, O-06 | >= 20, 100% | as above | as above |
| Roman | B-RO-01..11 (11) + B-UL | A-10, A-18, A-19, A-20, A-29, A-31 | as above, Roman surfaces | O-01, O-02 | >= 20, 100% | as above | as above |
| Entity check | B-EN-01..06 (6) + B-UL | A-18 (entity surfaces), A-19, A-23 | as above | O-01 | >= 10, 100% | as above | as above |
| Osei-lite (quarantine, observations) | B-OS-01..05 (5) + B-UL | A-08, A-09, A-15, A-29, A-31 | as above, Osei records | O-01 | >= 20 (with drift), 100% | as above | as above |
| Osei drift | B-DR-01..08 (8) | A-22, A-28 | as above, drift blob | O-03 | included above | as above | as above |

Numbers in a row are the minimums of the item cited; the suite enforces them (SUITE_PLAN 5).

## 17. Open questions for the owner (each with the default built if not answered)

| Id | Question | Proposed default |
|---|---|---|
| Q-01 | Code length | 16 hex (64 bits), replacing today's 8 hex `{seg:..}` (32 bits: about 1 expected collision at 93,000 values a tenant) |
| Q-02 | Are the tenant's own subdomains (`blog.<domain>`) readable? | No: only the registered domain and its `www.` twin; other own subdomains coded, readable in the owner display |
| Q-03 | May the owner display resolve outside values found on the client's own pages (another site's host in a canonical, a declared cross-host sitemap)? | Yes, owner only, escaped, never linked |
| Q-04 | schema.org type names: approve CC BY-SA 3.0 for a bundled list, or keep the service's own rule-table types only | Rule-table types only (D-01 T); no CC BY-SA data |
| Q-05 | Use the CLDR validity data (Unicode-3.0, already approved) to validate BCP 47 subtags | Yes |
| Q-06 | robots.txt answering 429 | Treated like 5xx (unreachable: our crawler does not fetch; `ROBOTS_UNREACHABLE` with the status), as Google documents; RFC 9309 alone would say "unavailable" |
| Q-07 | Raise the sitemap read limit to the protocol's 50 MiB uncompressed (streamed) | Yes; memory bound unchanged (A-11) |
| Q-08 | Keep plain SHA-256 of crawled bodies inside the encrypted report (lets the owner verify a file) | Yes in the report; keyed HMAC in log / ledger |
| Q-09 | Crawled dates (lastmod values) readable? | No: coded; counts and the service's own comparisons readable |
| Q-10 | Structural robots patterns (`/`, `/*`, `$`, well-known names) readable in stored findings | Yes (D-01 R) |
| Q-11 | Owner-approved list of real public sites for a read-only lead-gate sanity crawl | None until the owner names sites; the suite stays fixture-only |
| Q-12 | New finding `LANG_INVALID low/measured/ACT` for a lang that is not BCP 47 | Yes |
