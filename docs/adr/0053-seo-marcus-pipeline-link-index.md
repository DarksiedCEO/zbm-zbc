# ADR 0053 - The Marcus index pipeline v1: ZBM's own link index, built to marcus-pipeline-spec/1

Status: accepted (v1, fixtures only). Spec: `marcus-pipeline-spec/1` (FROZEN v1, owner-approved 2026-11-13, every
owner question at its default) with the v1.1 clarifications GP-1..GP-6, CL-0 and CL-1. Base:
`claude/seo-callum-engines-20261108` at 186471eb.

## Decision

Five single-task services build the link index Marcus (the backlink report, marcus-spec/1, not built) will read.
Each is in `svc_manager.REGISTRY` under its role id (assignment, inputs, outputs: CB-1); `svc_manager.NAMES` holds
`name_pending: true` and its proposed name (Q-MP-1), shown beside the registry block on `GET /seo/v1/department`
(the registry block keeps exactly three keys, as `tests/test_callum.py` requires of every agent):

| Role id | Proposed name | Module | The one job |
|---|---|---|---|
| `cc_release_watcher` | Ruth | `marcus/release_watcher.py` | notice a new Common Crawl crawl or web-graph release and record it |
| `cc_wat_fetcher` | Felix | `marcus/wat_fetcher.py` | deliver the WAT files of one crawl (or one targeted subset) to the Link Extractor, verified |
| `cc_link_extractor` | Esther | `marcus/link_extractor.py` | turn WAT records into normalised, personal-data-scrubbed link rows |
| `cc_index_builder` | Ivan | `marcus/index_builder.py` | build one snapshot: the link slices for the registered target domains, sharded, encrypted, manifested |
| `cc_graph_importer` | Gideon | `marcus/graph_importer.py` | import the host / domain graph rank files of one release |

The v2 services (Link Verifier, Spam Scorer, RDAP Enricher) are in `NOT_BUILT_AGENTS` and their settings in
`config.NOT_BUILT`, as are the live Common Crawl ports (`SEO_CC_RELEASES_ENABLED`, `SEO_CC_WAT_ENABLED`).

### Jobs (svc_link_index.py)

`POST /seo/v1/link-index/jobs` (dashboard, scheduler) starts one job of the Release Watcher, the Index Builder or the
Graph Importer; `GET /seo/v1/link-index/jobs/{job_id}` (dashboard, scheduler, compliance_38) answers its envelope
(machine facts only). The builder job runs the WAT Fetcher, which delivers each verified file to the Link Extractor,
whose rows the Index Builder slices. Record-first: `link_index_job_requested` (ledger and log) before any port read;
`link_index_job_recorded` is the result and the PUBLISH POINT of a snapshot; `link_index_job_interrupted` for a kill
switch (KILLED_*, the partial result discarded), a restricted service (AGENT_RESTRICTED), a crash (RUN_FAILED with a
safe location) or a dead process (PROCESS_DIED, from the interrupted-audits job). Per file `wat_file_done` /
`wat_file_quarantined`; `cc_release_seen` once per release; `index_snapshot_retired`; `cc_spike_recorded` (B-MP-24,
Andre only, no route in v1). Kill switches: global, write, `capability:link_index`, `provider:commoncrawl`, before every
step and every read. One running job per service.

### The index at rest (GP-4)

The index is not tenant data (Q-MP-2). Every index object goes through `marcus/index_store.py`: AES-256-GCM under the
service key `vault._derive(<vault key>, b"marcus-index/v1")` (the sub-key is derived in `vault._Material` with the
others, since the raw key is not kept; `CitationVault.index_keys`), a random 96-bit nonce, the additional data binding
kind, name and key id; the manifest holds the sha256 of each shard's stored bytes. No vault key: every build and import
is refused `INDEX_KEY_UNAVAILABLE` (503) before anything is read (AC-10).

### Atomic publish (B-MP-33)

Shards are written, each read back and hash-checked; then the manifest `marcus.index_snapshot/1`
(`index_builder.MANIFEST_KEYS`, `complete: true`) is written - the last index write - and then the job is recorded. A
manifest without its record (a crash between the two) is deleted at the next start, with shards no published snapshot
names. A reader uses `complete_snapshots()`: published, unretired, manifest present with its recorded hash.

### Scrubbing (B-MP-22 / 23)

IDNA via domain_match strict round-trip (IDNA2003 codec, refuse-if-changed); IDNA2008-only names refused. A manifest's
shard domain is accepted only in that canonical form (ASCII lower-case A-labels, `index_builder.canonical_domain`);
a U-label, upper case or a changed label is refused whole (CB-19), never converted.

`normalise` lower-cases scheme and host, applies the host rule of `agents/domain_match.py` (IDNA round trip), removes
a trailing dot, the default port and dot segments, drops the fragment, the tracking parameters (the core lane's list)
and the user info. `scrub_url` replaces every segment and query value `secret_guard.inspect_url` reads by the core
lane's placeholder and every one `prompt_guard.personal_data` reads by `{withheld}`, before `make_row` (the one row
constructor); a host with a finding withholds the link. Anchors: NFC, no control or format characters, 256 characters,
`{withheld}` on any finding. The wave1 placeholders `{email} {number} {uuid} {token}` are BLOCKED until
`primitives/codes.py` is on the base (GP-5, CL-0): their tests are strict xfail.

### AC-2 on this base (measured, 400 fast cases)

- Anchors: 11 readable values are CORE FINDINGS under the SUITE_PLAN 1.0 routing rule ("a leak through a path with
  every required call made is a core finding (core residual list)"; R-MP-3): every required anchor call is made
  (`secret_guard.check`, `prompt_guard.personal_data`) and neither reads the value. Shapes: C4 vanity numbers
  (`8XX` + seven letters) D01 2, D10 3; C7 street addresses (number, street, city, state, ZIP) D01 3, D10 3.
- URL parts: 18 readable values (17 link targets, 1 page URL; C4 and C7 in path, query and host placements) are
  BLOCKED, not routed: B-MP-23 also requires the wave1 placeholder call there, which needs `primitives/codes.py`
  (GP-5, CL-0). They are pinned in `test_B_MP_23_AC_2_url_parts_after_codes` (strict xfail): once codes land it
  must pass with 0 readable or the spec fails.
- Everything else: 0 readable.

## Readings taken where the spec leaves a choice (reported to the owner)

Registry inputs / outputs wording; a crawl's published date is null (GP-1's crawl shape has none); allowed release-list
hosts `index.commoncrawl.org` and `data.commoncrawl.org`; graph release id grammar `cc-main-...`; `licence_ack:
FIXTURES_ONLY`; a host-label finding drops the link (counted); the duplicate-host check covers the kept hosts (the 64 MiB
bound on 10^8 rows); a report's retention pin is read from `backlink_reports` (empty until Marcus is built); job views
carry Marcus's `monthly` refresh tier; targeted path lists hold at most 1,000 paths (the 128 KiB body limit); a resumed
build re-reads its plan (files, order, targets) from its encrypted staging.

## Consequences

Throughput is reported, not certified (B-MP-26); the real numbers, rel capture and the WAT shape on a real file are the
Phase-0 spike's (Q-MP-4, owner decision). Residuals R-MP-1..3 accepted as the spec proposes.

## Review round 1 (2026-11-15): test-only changes and readings
- AC-14 / CB-23 apply to every new module: `svc_link_index.py` now has a generated-mutant lane (24 generated, 24 killed by `test_mp_link_index_edges.py`).
- O-MP-1 lead test asserts exact row equality, not only the row count.
- AC-1 harness: filler hosts and anchors are labelled with letters, so a case index can never equal a planted digit value (19 false positives before). On the stored surface (log lines, ledger payloads, encrypted index objects), a planted all-digit probe of 5 characters or fewer is NOT counted only where it lies inside an unbroken hex/digit run of 10 or more characters (a sha256 / hash-chain value, an epoch, an id): such a probe appears there by chance in about 35% of builds (9 of 26 identical builds, a different field each time), which is not the value being readable. Anywhere else - plain, glued to a label ("passcode4821"), next to a short number - it is readable (review MR-3: the first, looser rule missed 5 of 12 such forms and was replaced). Every other probe and surface is judged unchanged.
- Reading Q-MP-3: a report whose retention date is exactly now no longer protects its snapshot from retirement.
- Residuals: duplicate WAT records across different files of one snapshot produce duplicate link rows (9 of 129 in a review probe; A-MP-01 covers duplicates within a file); a core vault key removed between runs is refused by the core integrity gate (503 INTEGRITY_UNVERIFIED) before Marcus runs.
