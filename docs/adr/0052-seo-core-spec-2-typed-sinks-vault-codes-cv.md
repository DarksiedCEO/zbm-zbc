# ADR 0052 - SEO (Dept 2): core-spec/2 - typed sinks, vault codes, label-independent client view

Number to be confirmed at merge (0052 is the next unused number in `docs/adr` on this branch). Status: built on
`claude/seo-callum-engines-20261108` (base 3120f0b0), uncommitted tree, 2026-11-14 .. 2026-11-16. **Not in force**
until the core-spec/2 gates pass on the exact tree and the branch is merged. Fixtures and synthetic data only (frozen
generator VG, `tests/conformance/gen_values_v2.py`); no real key, no client data. Successor to ADR 0050 (redaction
core) and ADR 0051 (client citation vault); ADR 0051 decision 76 is corrected in place by a dated note (G-8, F-1).

Spec: FROZEN core-spec/2 (`SPEC_CORE_v2.md`, owner-approved 2026-11-14 ~09:30 PT, every [OWNER DEFAULT] as written),
plus the clarifications CL-1..CL-7 and the lead rulings / owner decisions listed in section 4.

Evidence: every number below is copied from the lane hand-off `core-v2/HANDOFF.md` (cited as `H:<line>`) and the
evidence file that line names, under the lane folder `core-v2/` of the build session (not committed). A row whose
full-lane number is still running is marked **PENDING** with the file it will come from. No number is estimated.

Tree at writing (md5): `src/agents/cv_shape.py` b7e37a358150342ccd86ec1708cdaebd, `src/sink_guard.py`
7758b00553b5093f3a04cdba963d8ac8, `src/primitives/codes.py` a7e7ef5f6c21678bc310f7b087967ad8, `src/vault.py`
0d2305c71566a54cbbfb3c1c16aff877 (codes / vault = Wave-1 certified b1611751, H:6-7).

## Context

Round 8 (3120f0b0) masked secrets in the client view by reading LABELS ("password:", "api key =" ...) in many
spellings. The rv-sink9 review showed label spellings the lanes do not read (FINDING F-1: Greek UPPER label split
after the inner sigma), so a value after such a label was shown. Council 1 and the owner (2026-11-14 ~09:20 PT,
decisions 1, 3, 4) replaced the label race with three structural rules: every sink field has a closed type (no free
text can be written), free text lives only in the per-tenant vault as codes, and the client view masks any
value-shaped token whatever precedes it.

## Decision

### 1. Typed sinks (T-1..T-7)

1. T-1: a sink field's type is one of the 21 v1 S-1 classes; no free-text / string / any type. Enums are `code`
   fields whose value set is a closed constant of the code base. CL-6: JSON bool -> boolean, bounded int -> number,
   finite float -> number only where the table declares it numeric, null only where declared.
2. T-2: a field that points at free text carries the brace-less Wave-1 B-CD-01 code `c:text:<16 lower hex>`, typed
   `code` and validated by the exact grammar `c:(<KINDS>):[0-9a-f]{16}`; the braced form stays refused at sinks
   (ruling 21:45 PT F14-1). The owner client view is an API response, not a sink field, so its span list carries the
   braced form (ruling 15:30 PT F8-1).
3. T-3: one frozen table `SINK_FIELDS` (`src/sink_fields.py`) maps (sink, record kind, field path) -> a closed type
   set, for `data/seo_log.jsonl`, ledger payloads, Python logging (kind = logger name; `/template` a code-base
   literal, `/args/<i>`, `/extra/<name>`, `/exc_type` type name only; unknown loggers -> `log_refused` counter;
   ruling 14:58 PT F3-1, 15:48 PT), error bodies (kind = reason code or `validation`; `msg` prose replaced by
   `msg_code`; closed kwargs per reason; F3-2, F3-6, F3b-1..3, F3b2-1..3) and outbound requests (F3-4).
   CL-7: a union of 2+ T-1 types only where the inventory shows the field holds them today; unions and their reasons
   are recorded with the table (rulings 12:10, C2-2b2, F2e-1, F2f-2).
4. T-4: `test_every_sink_field_has_a_type_decision` (static + dynamic recorder). Full suite on the C2-10e tree:
   1,129 paths recorded, 0 undecided (H:1819, `c210e/full_suite.txt`).
5. T-5: a value that does not match its field's grammar is refused at write (`SINK_REFUSED`), never rewritten.
   Open `code` fields carry a closed shape rule (C2-6b, `sink_guard.code_shape_refusal`, no score or threshold): an
   open code split at `[_.:-]` is refused when (1) it holds upper + lower + digit; (2) a piece is 6+ digits and not a
   compact date YYYYMMDD; (3) a piece is 6+ lower hex; (4) a mixed-case piece is not a camel word; (5) a single-case
   piece with digits has them other than one run at its end, or at its start before <= 2 letters (H:1288). Union
   fields are judged on their `code` branch (ruling 22:00 PT F6a-3). Tenant-id / `ent-` fields accept only a
   REGISTERED tenant id that matches `config.TENANT_ID` AND that the route's own identifier predicate accepts (same
   function, imported; rulings 02:15 PT F6f3-1, 07:40 PT, 08:15 PT F6i-1).
   Result: 1968 fields x 4 classes x 200 = 1,574,400 plants, accepted 0, e2e top-level 0
   (H:1334, `c26c/t5_matrix_200_c26c.json`; earlier 42,761 -> 30,561 -> 0, H:1125, H:1210, H:1290).
6. T-6: canary in every Wave-1 D-05 form at every route, incl. the reveal route (ruling 01:20 PT (D); reveal OFF in
   the canary harness = 503, ruling 02:15 PT). Last named run: `test_e_comp + test_sink_job_d +
   test_canary_all_routes + test_sink_fields` 2071 passed, 0 failed (H:1994, `c210c6/ev/run2.txt`).
7. T-7: `SINK_REFUSED` = 0 outside the closed list `EXPECTED_SINK_REFUSALS` (test_sink_guard.py), held by the full
   suite: 5,815 passed / 0 failed / 17 skipped on the C2-6i tree (H:1643, `c26i/full_run.txt`); 5,830 passed / 1
   failed (timing cell under load, passes alone 11 x3) / 19 skipped on the C2-10e tree (H:1819).
   Final tree (secret_guard fbb997f7, cv_shape b7e37a35, sink_guard 7758b005; tree md5 start = end,
   `gate/r2_tree_md5_{start,end}.txt`): full suite x2 = 5880 passed / 0 failed / 24 skipped both runs, the T-4 and
   T-7 tests included (`gate/full_1.txt` 1,891 s, `gate/full_2.txt` 1,824 s; lead 11:10 PT).

### 2. Vault and per-tenant codes (V-1..V-6)

1. V-1: free text the core keeps (answer excerpts, cited titles / text, hints, operator text, evidence excerpts,
   prompt-set names F2f-1, full URLs F2b-2) is stored only in the per-tenant AES-256-GCM vault as {code -> raw}; the
   existing `citation_vault.jsonl` and operator-text vault are such blobs with their ADR 0051 bounds (ruling 17:55
   PT F9-1). Writer list (CL-1 / F9-3): svc_citations, svc_vaulted, svc_optext, svc_audits, svc_logs.
2. V-2: no key -> `VAULT_UNAVAILABLE` before any fetch or write.
3. V-3: `codes.py` / `vault.py` are md5-equal to Wave-1 b1611751, so B-CD-01 / B-CD-02 rows are CARRIED OVER
   (H:1646). Owner-view span codes are computed under the tenant's own coder (C2-14d, ruling 15:25 PT F18-2):
   50 identical tokens in two tenants -> 0 equal codes; 40 x 2 cross-tenant attempts -> 0 wins (H:1886).
   **PENDING:** B-CD-02 rotation number - the judge confirms it from the Wave-1 review of record
   (`claude/review-2026-11-14-wave1v2-b1611751.md`) or marks it NOT-TESTED (ruling 09:10 PT).
4. V-4: decrypt only for the owner caller with tenant authentication. The Compliance (38) caller is refused before
   any decrypt (F10-1 fixed in C2-10b); dashboard / seo_agent callers without tenant authentication get the
   code-only run view (`view: "codes_only"`, items None) and never a decrypt; the 409 check runs before the vault is
   opened (ruling 10:05 PT F10b-1, F10b-2; confirmed 10:55 PT). The client document rendered for the owning tenant
   is an owner display surface and uses the same owner-view routing (ruling 20:15 PT).
5. V-5: 200 planted own-text tokens revealed by the owner: 200/200 found; other tenant / Finance 31 / Compliance 38 /
   cross-tenant displays + 150 non-owner reveal attempts: 0 found (H:1649).
6. V-6: data dirs of ffd28ca8, cbdf5e78, 3120f0b0 start on the build: `CONFORMANCE_HEAVY=1 test_core_upgrade`
   3 passed (H:1994, `c210c6/ev/`); 0 5xx, 0 skipped slots after the harness key-handover fix (H:1866; the judge's
   cell cites the C2-10c2+ runs, not `c210c` 2ca, F10c2-2). Legacy finance ids: section 3.3.

### 3. Owner exceptions and owner decisions

1. **E-LLM** (owner 2026-11-14 14:57 PT, "Allow narrow exception"; F3-4): LLM request text fields (openai /
   perplexity `instructions` + `input`, anthropic `messages[].content`, google `contents[].parts[].text`) may carry
   ONLY the code-base constant SYSTEM_PROMPT and the tenant's own configured prompt-set text, ONLY to the closed list
   of configured provider hosts. System-role paths admit only the exact SYSTEM_PROMPT; user-role only a bound prompt;
   sonar `messages[]/content` is role-bound (ruling 16:58 PT F3c-2). Every other outbound field is typed or a closed
   enum (F3c-3).
2. **E-AUTH** (ruling 17:15 PT F3c3-2, stricter): a credential is sent only to its own issuer and only when it is
   exactly the configured one (GSC token `assertion` = what build_assertion produces for the configured key, to
   oauth2.googleapis.com only); Compliance `/path/client_id` is bound to the tenant's configured client id (F3c3-1);
   no fail-open import fallback (F3c3-3). The bound value comes from the same tenant record the caller passes, so
   E-AUTH stops a value swapped between service and client, not a wrong tenant record (F3c4-1, H:683).
3. **E-COMP** (owner 2026-11-15 ~02:00 PT, "Narrow exception (Recommended)"): the tenant's finance client reference
   is stored at every sink only as its T-2 reference; the raw value lives only in that tenant's vault and is opened
   for exactly one non-display purpose - the Compliance activation request to the configured Compliance host (exact
   match). The outbound record stores the code. Finance / "client changed" checks compare code to code. The route
   validates the reference by its own exact grammar, not by a sink rule (ruling 03:20 PT F6g3-1); schema cap = the
   reference grammar {1,100} (F6g4-2); alnum first character kept (no "." / ".." path segment, F6g6-3).
4. **Legacy finance ids: count and contain** (owner 2026-11-15 ~15:40 PT, "Count and contain (Recommended)"): an
   exception to V-6's "re-written on first touch" for raw finance party ids in append-only seo_log lines written by
   ffd28ca8 / cbdf5e78 / 3120f0b0. The lines stay untouched (no history mutation); never shown or exported (export
   withholds); counted by `/health` (`legacy_prose_lines`); on replay the build converts the raw reference to its T-2
   code in memory and seals it in the tenant vault once; nothing new is written raw. Observe-only start seals nothing,
   so E-COMP fails closed 503 VAULT_UNAVAILABLE until a normal start (H:1911).
5. **Owner view "Show machine fields"** (owner 2026-11-14 19:20 PT): in the owner's view of their own reports, fields
   written by our code (template paths, hashes, content types, model versions, ids, timestamps, counts, codes,
   `code_text` = exact code-authored strings) are shown as typed machine values; only free-text fields go through
   the CV. A machine field not matching its grammar, and any unknown path, falls back to the CV (fail closed).
   Rulings 16:15 / 16:58 PT on owner_view are withdrawn by it (OV-1..OV-3, 20:15, 20:35, 20:55 PT).
6. **Mask more** (owner 2026-11-15 ~10:00 PT, "Mask more (Recommended)"): see CV-4 in section 3a.

### 3a. Label-independent client view (CV-1..CV-10)

1. CV-1 / CV-2: N-1 canonical view; tokens = maximal non-space runs; space runs collapse to one space, so "two
   separators" in CV-4 means two J characters after canonicalisation (ruling 15:12 PT F7-1, F7-2).
2. CV-3 value-shape rule (VS): a token of >= 8 code points whose core holds two of {letter, digit, ASCII symbol}
   and is not a CV-6 exempt shape is masked whatever precedes it; K-1 / K-2 / K-5 shapes always. UUIDs, hashes,
   SKUs, order ids are masked (CV-6 does not exempt them; AL-1 and RV-1 recover them). Any v1 finding keeps the
   whole-field v1 mask (stricter, F8-2).
3. CV-4 joined-run rule (JR) + **union (mask-more) rule**: runs of >= 2 fragments (1..24 cp, one J character
   between) whose concatenation (<= 64 cp) is valueish are masked as one span. Owner "mask more": a piece longer
   than 24 cp whose token CV-3 already masks counts as a fragment for joining. Implemented MONOTONIC: final mask =
   old CV-3 / CV-4 spans UNION the new spans; a new run failing the 64-cp limit is not added (ruling 10:40 PT
   F13c-1). Property old-subset-of-new: 0 violations on CV-8 samples 3,000, CV-10c CI 29,084, VM CI e1 387,608 /
   e1b 49,728 / e1c 78,480 / p9 128,632 texts (H:1787, `c213d/run_monotonic.txt`).
4. CV-5: each span renders `[hidden: looks like a credential]`; the response lists {start, end, code}, code =
   braced B-CD-01 under the tenant's coder (F8-1, F18-2).
5. CV-6 exempt shapes: closed list as frozen (shape-only dates, frozen ISO 4217 list, prefix or suffix currency,
   F8-3). CV-7: URL / e-mail tokens go to the URL lane / K-4 / K-5 as in v1.
6. CV-8 mask-rate cap (<= 2.0%): FULL run on the C2-17 tree: N 84 / 45,600 = 0.184% (baseline old_masked 62
   confirmed); ord18 + oq19 word classes 0.0% each; disclosed (not judged): tire_size 58.0 / 41.1%, model_number
   38.7 / 34.6%, part_number 100% (oq19), flight 35.4% (oq19), date_time 29.7% (ord18), product_code 9.8% (ord18)
   (H:1065, `c217/run_full_cli.txt`). After mask-more: CV-8 full PASS (H:1807, `c213d/run_cv8_full.txt`); CI sample
   N 2 / 1,000 = 0.2% (H:2006, `c213e/run_lanes.txt`).
7. CV-10a value matrix VM (E1 578,854 / E1b 48,860 / E1c 24,100 / p9 23,603 cells x 4 lead variants): full run on
   the pre-mask-more rule evaluated 669,715, shown 0 (H:1667, `c212g/run_full_{0..3}.txt`); full 4/4 shards PASS on
   cv_shape a663ee17 (H:1807, H:1821, `c213d/run_full_{0..3}.txt`, `c213d/ev/`). Span identity is judged on the
   value's own code points (ruling 21:35 PT F12-1); separator "=" / "=>" masked by a joined run identically in all
   variants is allowed (23:58 PT class A); where the corpus label is itself valueish or joins the value's run
   (M1 / M2 / M3) identity is judged on the value core, classified from the variant itself (00:10 PT; confirmed
   00:35 PT); a separator between label and value may differ when the difference is extra masking (F13c-2).
8. CV-10b F-1 cells built as a FIXED set by construction (ruling 21:50 PT): 60 cells (10 spellings x {space, NBSP}
   x {": ", " = ", " is "}), the WHOLE e1b inner-sigma / first-sigma classes (stricter than the review's 18+18 /
   33+33 subsets, ruling 22:05 PT F12c-1), H4 / H5 = first-sigma split at position 11 (F12c-2); named tests GREEN
   (H:1253, `c212d/`).
9. CV-10c split set FS: CI 158,400 candidates / 29,084 accepted / shown 0 (H:2006); full FS PASS on a663ee17
   (H:1807, `c213d/run_c_full.txt`). CV-10d: full 26 families x 2,000 x 51 disguises = 2,652,000 cells, shown 0
   (H:1821, `c213d/run_d_full_{0..3}.txt`). CV-10e: 43 forms / shown 0 / disk + logs 0 / error bodies 0 (H:2006).
10. Full CV lanes on the final cv_shape b7e37a35: the C2-13e change sits inside `if cv_allowlist.active()`, so the
   lanes run without an allowlist scope are byte-path unaffected and stand (ruling 15:40 PT, H:2014).
11. Final tree heavy lane (all four srcs final; 4 shards x 11 passed, 0 failed, `gate/heavy/shard_{0..3}.txt`): VM
   full 675,417 cells, 669,715 evaluated, shown 0, span_diff 0 (`gate/heavy/full_*_{0..3}of4.json`); CV-10b/c/d full,
   CV-8 full, V-6 upgrade and TI in the same shards (lead 12:10 PT).

### 3b. BL, AL, RV, LP, TI

1. BL-1..BL-3: held by the kept v1 `label_value` lane (whole-field mask, stricter than one token); no span-level BL
   rule added (H:1110). CI subset: BL-3 cells 10,908, prose shown 0; BL-2 negatives 5,112, masked 0; Q2-11: 6 J23
   cells counted apart (H:1112, `c216/ci_counts.json`). Full matrix = 14,487,390 cells (H:1113).
   BL full on the final secret_guard fbb997f7 (md5 start = end, `c216e/bl_md5_{start,end}.txt`): 3 x 4,829,130 =
   14,487,390 cells, shown 0 (BL-3); BL-2 negatives 3 x 122,688 = 368,064, masked 0; Q2-11 144 per seed
   (`c216e/blfull_seed{0,1,2}.json`; lead 08:45 PT). Earlier BL full runs (blfull/, c216b/, c216d/) are superseded
   by the C2-16b..e secret_guard changes (section 3c).
2. AL-1 / AL-2: per-tenant catalog codes (source `shopify` only, F15-1; exact span text, F15-2). AL-2 e2e incl.
   result_ref + HTTP: 1000 / 0 x4, mutation red (H:1972, `test_core_al_e2e`); K-1 x4 + K-5 x4 listed: 0 shown
   (H:782, `c215b/run1.txt`). **Per-token showing inside merged spans** (C2-13e): AL-1 shows a listed token that
   CV-4 had joined with a neighbour into one run; the remaining neighbour piece is masked alone; 3,600 listed tokens
   masked -> 0 (H:2002-2014). Spec AL-1 words the rule per span; the per-token reading is the lead's (F13e-1).
3. RV-1..RV-4: audited reveal of own text, audit committed before the value, Cache-Control no-store, 100 / actor /
   hour. N = 1000 cell passed (H:1887, `c214d/run_reveal_n1000.txt`); 2-tenant span-code reveal: 0 cross wins.
4. LP-1 (reading, ruling 15:25 PT): a reference release masks a cell when its client_view_findings is non-empty; the
   build masks it when the owner display hides the whole field or its CV-5 spans cover every value-core character.
   CI incl. 24 e1 shards: e1 48,451 / e1b 6,216 / e1c 9,810 / p9 16,079 = 80,556 evaluated, LOST 0 (H:1924,
   `c218/ev/run_c218b_all.txt`, `c218/ev/lp_ci_*.json`). LP-1 full on the final tree (heavy lane, 4 shards,
   `gate/heavy/lp_full_{e1,e1b,e1c,p9}_{0..3}_4.json`): 669,715 evaluated, LOST 0; R-prose 1,424 lost prose cells of
   314,214 prose cells a reference masks, unchanged vs `c218/run_lp_full_{0..3}.txt` (lead 12:10 PT).
5. TI-1: 2 tenants x 4 disjoint sets of 200: own list shown 200/200, equal codes across tenants 0, other tenant's
   values visible 0, cross-tenant reveal wins 0 / 120, allowlist cross 0 (H:1923-1924).

### 3c. C2-16b..e: script words after a SEP-3b verb (secret_guard only)

1. C2-16b (lead 16:15 / 17:20 PT): BL full showed BL-2 negatives masked 240 + 540 (seeds 0, 1; CI had 0). One class:
   Thai / Devanagari prose words holding a combining mark read as symbols by `_cv_valueish`. LATENT (780 also on
   cv_shape 2f9eb8de / a663ee17), not caused by mask-more. Fix: a mark counts as part of its letter (edf4f5a6).
2. Regression caught by the gate (lead 22:40 PT): full suite x2 red on `test_review_ffd28ca8` l.275 - 4 Latin letters
   glued to a Devanagari word after a Korean label was no longer masked. C2-16c (accepted 00:05 PT, 61a6d3ad): prose
   only for a one-script word. C2-16d (lead, 64879182): Han + hiragana + katakana + iteration mark = ONE script group.
3. C2-16d BL full RED (lead 03:40 PT, `c216d/blfull_seed*.json`): BL-2 negatives masked 19,110 / 15,288 / 19,170 of
   122,688; Q2-11 114 / 120 / 114 - Greek / Cyrillic prose words read GREEK+LATIN on the lookalike-folded marked view.
4. C2-16e (fbb997f7, accepted 05:00 PT pending BL full, PASS 08:45 PT): Latin + exactly ONE other script counts as one
   script when that script is one the lookalikes table reads as ASCII letters (derived from `_FOLD.mapping`: Greek,
   Cyrillic, ...; not Devanagari / Thai / kana / Han). Latin + Devanagari, Latin + kana, Latin + 2 scripts, Greek +
   Cyrillic stay value-ish. Harness BL-2 neg 0/0/0, Q2-11 144 x3, BL-3 touched 2,897,478 shown 0; 840 family tests
   (`c216e/h/rnew*.json`, `c216e/ev/run_family.txt`). Lesson (03:40 PT): every secret_guard change gets BL-2 negatives
   FULL before acceptance. Residual R-16e above. Mutants bl_mixed_script_is_prose / bl_lookalike_pair_off added.

### 3d. Performance - owner decision PF (2026-11-16 12:18 PT)

1. Andre: "I'm giving the ok to run whatever it is that we need to do to continue to move this app forward" - read by
   the lead as approval of the recommended option B (absolute ceilings); Andre may reverse it (`SPEC_CORE_v2.md`).
   Bars: every PF-1 cell per-text median <= 2.0 ms; every PF-3 64k cell <= 1.0 s; PF-2 cold start <= 1.10x.
   G-2 is NOT changed by this decision.
2. PF-1 absolute (`gate/pfabs/pf1_abs.json`, pf.py inputs, process_time, best of 3, two runs): 28 cells, max
   0.3399 ms (pl|cv); cv lane 0.0983..0.3399 ms, strict 0.0673..0.2694 ms; 0 cells
   over 2.0 ms -> PASS. Load 1.20 at start, 1.68 at end (another lane's job; per-process CPU time).
3. PF-3 (`gate/pf/pf3.txt`, quiet box): 44 cells, max 64k 0.3322 s (spelled_near_miss|cv), 0 over 1.0 s -> PASS;
   max doubling from 8k 2.63 > the 2.5 linearity limit of pf.py on 4 cell(s): many_separators|cv [1.95, 1.89, 2.61]
   (64k 0.1183 s); one_script_cyrillic|cv [1.93, 2.53, 1.83] (64k 0.216 s); spelled_near_miss|strict [2.04, 2.03,
   2.53] (64k 0.1488 s); spelled_piece_runs|cv [1.75, 2.63, 1.92] (64k 0.2593 s) - the PF-4 (linear) row is NOT
   changed by the owner decision; judged by the judge from pf3.txt. PF-2 0.9969 (`gate/pf/pf2.txt`) -> PASS.
4. Relative rise vs ffd28ca8 (REPORTED, lead 16:20 PT, `gate/pf/pf1.txt`): 17 of 28 PF-1 cells over +10% both runs -
   every cv-lane language +77..+253% (hi +253, en +246, ko +199, de +164, th +137, ja +124, el +122, he +121); strict
   he +39, uk +28, ja +17%. PF-3: 17 of 44 cells over the relative rule (e.g. mark_run|cv 64k 0.055 s = 15x ffd).

### 3e. Other gates on the final tree

1. G-2 CI lane: 31 files, 2654 passed / 0 failed / 12 skipped; quiet box (load 0.50..1.00) 706 s > 600 s bar ->
   G-2 time RED (`gate/ci_quiet.txt`; 739 s in `gate/r2_ci_lane.txt`). Remedy card C2-G2b (lead 16:45 PT), not run.
2. G-5 war room seeds 1-5: 555 / 570 / 561 / 567 / 569 PASS, 0 FAIL, 0 ERROR, MUST 100% (`gate/wr/seo_s{1..5}.txt`,
   md5 `gate/wr/md5.txt`). G-6 mutants (mutants_core2 580fde94, C2-19d): 35 run, 35 KILLED (`gate/mut/chunk1-4.jsonl`).

## Residuals and disclosures (one line each; owner ACCEPT / REJECT per spec section 8 / Q2-9)

- R-prose (CI subset): 52 lost cells (e1 31, e1b 21, e1c 0, p9 0), ALL outside BL scope (IN 0): e1b = SP-25 split
  forms; e1 = in-word forms 25, label no matrix form 3, S19/SP30 2, S20/SP12 1 (H:1920-1922,
  `c218/ev/rprose_bl_class_e1{,b}.json`). FULL: 1,424 / 314,214 prose cells (`gate/heavy/lp_full_*.json`, 3b.4).
- R-16e (reviewer note, lead 05:00 PT; F16e-1): a letters-only Latin + Greek / Cyrillic (or other lookalike-script)
  word glued after a SEP-3b verb such as 'was' reads as prose, as it did before C2-16b (section 3c).
- F18b-1: 1 of 200 synthetic 14-char [A-Z0-9] tokens gets a whole-field mask (no CV-5 span) in the owner view, in
  both tenants' scope (fail closed).
- F18b-2: 3 e1 lost prose cells have labels that are no matrix form and a non-alnum e1 form tag; OUT by BL's closed
  scope; their e1 generator family is not named.
- R-T5: 2 / 30,000 VG valueish values at 10,000 / seed (outside the frozen 200 / class set) pass the code rule
  (single-case piece + trailing digit run, same grammar as real codes like HTTP_404); not tuned away (H:1290, 22:50 PT).
- F14d-2: within one keyring `parse_vault_key` refuses previous id == current id and equal bytes, so key_id names one
  key in-process; residual: an operator re-using a key id for DIFFERENT bytes across deploys is not detectable from
  the id; sealed records still fail closed on MAC (H:1898).
- C2-2g (F2f-3), copied from the C2-19 card: an API-entered domain must be a hostname in the host rule's grammar, else
  422 DOMAIN_INVALID before any write; the 13 six-type domain unions are reverted. Seed 20261114 corpus (8,566 previously
  accepted): registration 0 change; competitor domains 7,824 -> 422 (code 7,135, number 587, version 87, amount 9,
  hash 6; no dot 2,772, TLD with digit / hyphen 3,612, leading / trailing '-' 862, 1-letter TLD 287, digit TLD 244,
  empty label 47). Unregistered non-hostname run domains: 422 DOMAIN_INVALID (was 403 DOMAIN_NOT_AUTHORIZED); bad-IDNA
  hosts at registration / competitor: DOMAIN_INVALID (was IDENTIFIER_WITHHELD). After the C2-6b code rule the pinned
  counts are 8566 -> 2779, changed 7824 -> 2037, code 7135 -> 1348; newly refused 5,787 were all 422 before (H:1331).
- F10c3 / C2-10c4..c6 withhold rules: export withhold judges every declared T-2 ref field by its type (raw -> withheld,
  T-2 code -> kept), walks dicts AND lists (C2-10c6); e2e: 13 GETs 200, raw forms shown 0, export withheld == 2
  (H:1952, H:1999). F10c3-2: the ledger's own evidence payload holds the raw id; the ledger is outside this service's
  data dir and is not counted by plaintext_at_rest (H:1914).
- F10c4-2: withhold counts a WITHHELD already in a declared T-2 field again on a second pass; export does one pass,
  so no effect today.
- F10c5-2 / F10c5-3: a hand-edited or tampered stored log (envelope keys) or a malformed ledger entry can fail start or
  5xx `/health`, `/audit/integrity`, `/audit/export`; ruled fail-closed behaviour (the hash chain already marks the
  log untrusted), no card (H:1986).
- F10c6-1: a raw finance id under an UNDECLARED nesting that only shares the key name is judged by value class only;
  no new rule: an undeclared path is refused at write by T-5 (closed SINK_FIELDS), so only a planted legacy line can
  carry it, and legacy lines are count-and-contain (H:1999).
- F13e-1: AL-1 per-token showing inside merged spans (3b.2). F13e-2: cv_shape calls the private
  `cv_allowlist._never` for the whole-span K check; a public alias would be cleaner (not changed).
- F11b-1: the CV columns of O-4 N and O-4 PIN still ASSERT (stricter than spec section 6 "reported"); kept while
  green; if one goes red it is re-pinned to REPORTED naming ST-3, never deleted (H:1944).
- C2-15d: NO HTTP route for the allowlist sync; sources are the tenant's own catalog integrations only; the sync
  stays service-level from the integration (H:1932).
- Glued label: an ordinary word glued to a value with "=" or ":" is hidden with the value (ruling 16:45 PT; the 16:25
  split rule was withdrawn after it showed a prefix for 76 / 6,000 VG values).
- Join disclosure: ordinary words glued by | , / + : = -- (and 36 zero-width-space glued cases) are masked in the
  client view (ruling 20:40 PT); the v1 excerpt utility bar is replaced by CV-8 for VS / JR masks (16:00 PT F7b-1).
- E-COMP history: a pre-change request retried -> 409 (idempotency body changed shape); data dirs holding a
  route-refused tenant id get SINK_REFUSED on replay writes (fail closed; no production data, H:1514, H:1641).
- Logging: unknown loggers (incl. uvicorn.error) write no traceback text, only a `log_refused` count (F3a-5).

## Rulings and owner decisions that changed a reading (since the core v2 round started)

Owner: 14:57 PT E-LLM; 19:20 PT machine fields; 02:00 PT E-COMP; 10:00 PT mask more; 15:40 PT count and contain.
Lead (11-14): 10:00 CL-1..4; 10:20 CL-5..7; 12:10 F2b-1..3; C2-2b2 {key} paths / job_ran union / digit-only hex;
F2e-1 finance_client_id union; F2f-1..3 (domains hostname, names to vault); 14:58 F3-1..6; 15:12 F7-1..7; 15:30
F8-1..5; 15:32 F3b-1..5; 15:48 F3a-1..5; 16:00 F7b-1; 16:12 C2-CL; 16:15 (CL5, later withdrawn by owner 19:20);
16:25 (withdrawn 16:45); 16:45 glued token; 16:58 F3c-1..5; 17:15 F3c3-1..3; 17:50 F4-0..3; 17:55 F9-1..4; 18:20
F3b2-1..3; 18:25 F15-1..4; 19:05 FCL-Q1/Q2; 19:50 OV-1..3; 20:15 OV2-1 + V-4 export; 20:35 OV3-1/2; 20:40 FCL-CV;
20:55 OV3b; 21:05 C2-17; 21:25 F6-1/F6-2; 21:35 F12-1..4; 21:45 F14-1; 21:50 F12b; 22:00 F6a; 22:05 F12c; 22:50 F6b.
Lead (11-15): 23:58 class A/D/E; 00:10 F12f; 00:35 separator exemptions; 01:20 C2-6e; 01:50 F6f-1; 02:15 F6f3; 02:40
C2-6g2; 03:20 F6g3-1; 03:55 F6g4-1..3; 04:55 F6g6-1..3; 07:40 route predicate; 08:15 F6i; 09:10 F10-1..3; 10:05
F10b-1..3; 10:40 F13c-1/2; 10:55 codes-only view; 15:00 C2-15d; 15:05 F11b-1; 15:25 C2-18; 16:05 F14d-2 closed;
15:30 F10c6-1; 15:40 C2-13e; 15:45 F10c5-2/3; 16:15 C2-16b; 17:20 F16b-1/2; 21:30 C2-19b F19b-1..3; 22:40 C2-16c.
Lead (11-16): 00:05 C2-16c accepted + C2-16d; 03:40 C2-16d not accepted -> C2-16e, BL-2-full rule; 05:00 C2-16e +
R-16e; 08:45 BL full PASS; 11:10 core gate r2; 12:10 heavy lane; 12:30 C2-19d; 13:15 G-6; 14:20 G-5; 16:20 PF red,
owner question; 16:45 G-2 time red -> C2-G2b. Owner (11-16): 12:18 PT PF option B (3d). Full text of each: `core-v2/HANDOFF.md` headings "Lead ruling" /
"OWNER DECISION".

## Consequences

- No free text at any sink by type, not by scanning; a new writer field fails T-4 until it gets a type decision.
- The client view no longer depends on reading labels; code-class texts are masked at the disclosed CV-8 rates until
  the tenant's catalog (AL-1) or an audited reveal (RV-1) recovers them.
- PENDING / not green before this ADR is true by measurement: V-3 rotation confirmation (judge); G-2 time RED
  (C2-G2b); G-3 ruff / gitleaks, IM-1 / IM-2, O rows and the reported job: no final-tree result in
  `core-v2/HANDOFF.md` at 2026-11-16 ~12:40 PT (C2-G3, C2-G8..G10).
