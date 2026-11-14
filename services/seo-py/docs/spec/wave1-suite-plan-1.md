# SUITE PLAN - the permanent, cumulative conformance suite for the seo-py Wave-1 agents

Implements SPEC.md (`wave1-spec/1`). Owner decision 2: every attack shape any reviewer has found is a permanent
test; the suite only grows; nothing is pushed unless it passes.

## 1. Layout (`services/seo-py/tests/conformance/`, one module per spec section)

```
tests/conformance/
  README.md                     how to run each tier; how to add a catalogue entry
  conftest.py                   markers (conformance, lead_gate), seeds, budgets, the service fixture, the
                                universal-bound checker (SPEC 12 header), the scanner hook (D-05)
  catalogue.json                every attack shape ever found: id, source (review + finding id), spec item, test id,
                                generator id, date added, status (live | equivalent-with-proof); append-only
  catalogue.lock                sorted ids + count of the last merged catalogue (written by the lead gate)
  oracles/                      independent reference implementations (no import from src/)
    rfc9309.py                  O-RFC: bytes in, dynamic programming over octets (SPEC 1)
    sitemaps.py                 sitemaps.org 0.9 validator: limits, namespace, loc, index rules
    w3c_datetime.py             W3C-DTF grammar written from the note
    llms_shape.py               llmstxt.org shape rules (H1, blockquote, H2 link lists)
    noindex.py                  O-NOIX: X-Robots-Tag / meta robots per crawler, from Google and Bing docs
    bcp47.py                    RFC 5646 grammar + CLDR validity (Unicode-3.0 data file, notice entry)
    templates.py                O-TPL: placeholder shapes of B-CD-04
    drift.py                    rule-order reference R1-R8 written from SPEC 10
    headings.py, xml_enc.py     heading-skip and XML 1.0 appendix F encoding detection
  gen/                          seeded generators; every value built at run time
    rng.py                      one `Random(seed)` per test; the seed goes into every failure message
    robots_gen.py               random rule sets, hostile shapes (A-01..A-07), large real-shaped files (O-06)
    xml_gen.py                  sitemap / index / DOCTYPE forms x encodings (A-08, A-10, A-11)
    bodies.py                   codings and bombs (A-09, A-15), Location lengths (A-13)
    sites.py                    the O-01 corpus (languages, CMS shapes) with ground-truth findings
    logs_gen.py                 log lines and hostile lines (A-16, A-17, A-30)
    plants.py                   A-18 shapes x surfaces (synthetic only; see 3)
    drift_gen.py                failure kinds x sides (A-28), real-change controls
  harness/
    service.py                  in-process service on a temp data dir with a generated vault key
    webmock.py                  fixture HTTP server: latency, trickle, status, codings, redirects, socket audit
    scanner.py                  D-05 scan of every output in every encoding form
    budget.py                   CPU (thread_time), wall, tracemalloc, RSS, /health sampler
    mutate.py                   in-place mutant runner over a tar copy (lead gate)
  test_b_robots.py   test_b_fetch.py   test_b_codes.py   test_b_selene.py   test_b_logs.py
  test_b_delia.py    test_b_roman.py   test_b_entity.py  test_b_osei.py     test_b_drift.py
  test_b_labels.py              B-UL invariants over every finding produced in the session (session-scoped
                                collector fed by the service fixture)
  test_a_robots_cost.py (A-01..A-03, A-17)   test_a_robots_input.py (A-04..A-07)
  test_a_xml.py (A-08, A-10, A-11)           test_a_bodies.py (A-09, A-13, A-15)
  test_a_ssrf.py (A-12, A-21)                test_a_slow.py (A-14)
  test_a_logs.py (A-16, A-30)                test_a_injection.py (A-19, A-20, A-27)
  test_a_isolation.py (A-22..A-25)           test_a_records.py (A-26, A-28, A-29, A-33)
  test_a_licence.py (A-32)
  test_d_plants.py (D-05, D-06)   test_d_sinks.py (D-02a-d, D-07)   test_d_outputs.py (D-02e-m, D-03, D-04)
  test_o_corpus.py (O-01, O-02, O-04, O-05, O-07)   test_o_codes.py (O-03)   test_o_large.py (O-06)
  test_catalogue.py             every catalogue entry maps to a collected test id; ids in catalogue.lock are a
                                subset of catalogue.json (the suite only grows); every SPEC item id is referenced
                                by at least one test (parsed from SPEC.md, which is copied to docs/spec/)
```

SPEC.md itself goes to `services/seo-py/docs/spec/wave1-spec-1.md`; `test_catalogue.py` parses its item ids.

## 2. Tiers and runtime budget

| Tier | Marker / command | Contents | Budget (2 vCPU CI runner) |
|---|---|---|---|
| Fast (every push, CI and pre-push) | `pytest tests/conformance -m "conformance and not lead_gate" -n 2` | every B item at its fast N; every A class at its fast N; D-05 / D-06 on 1,000 plants an agent through 12 service audits + 2 log ingests + drift + both report formats; O-01 on 40 sites; catalogue checks | <= 8 min wall (target), hard fail at 10 min (`--timeout` per test 120 s; session timer in conftest fails the run past 600 s) |
| Lead gate (before certification, nightly) | `pytest tests/conformance -m lead_gate` + `devtools/conformance/mutants.py` + war room seeds 1-5 | lead-tier N of every item (40,000 rule sets, 400-site corpus, 3,000 plants an agent, 1,556 drift pairs, 2,640 XML cases); mutants (SPEC A-31); war room | <= 90 min |

Budget allocation for the fast tier (measured targets, each module's own timer asserts it): oracles and unit
differentials 2.0 min (4,000 rule sets x 6 paths at about 20 us a decision both sides; 2,000 W3C values; 500 llms
documents; 4,000 lang tags); attack generators on primitives 1.5 min; service-path audits (12 plant audits, 40 corpus
sites, 20 hostile audits at <= 3 s each) 3.5 min; scans 0.5 min; slack 2.5 min. Heavy cases (A-03 costliest shapes,
50 MiB sitemaps, 0.7 s latency sites) run one representative each in the fast tier and the full set in the lead gate.

## 3. Generators (seeded, synthetic at run time, no literal secret / personal-data shapes)

- Seeds: the fast tier runs fixed seeds `(7, 11)`; the lead gate `(7, 11, 12, 13, 14, 15, 20261113)` plus one seed
  from the date (printed). A failure prints the seed and the case index; `--conformance-seed` replays it.
- No literal in source or test files is shaped like a secret or personal data. Shapes are built from parts at run
  time: a strong-key prefix is assembled from character codes listed as integers; a Luhn number is generated and
  completed; names come from syllable grammars (consonant-vowel tables) plus a census-style rank table GENERATED from
  a seeded Zipf model, so no real name list is bundled; e-mail spellings join generated parts with the 6 separators;
  passwords are generated by common-password GRAMMARS (word + digits + symbol, keyboard walks built from a QWERTY
  adjacency table), not copied lists. `gitleaks 8.30.1` with the repo config must report 0 on the tests tree
  (lead gate).
- Every planted value is checked at generation time to be absent from the service's constant texts, codes and
  vocabularies (collected by importing the modules and walking their string constants), so a hit is always a leak
  (SPEC D-05).
- Corpus sites (O-01) are built from per-language word tables GENERATED at run time from permissive or approved
  sources only (the language's Unicode-3.0 CLDR exemplar characters and syllable rules, plus the service's own
  English UI words); no wordfreq, NLTK or schema.org data. Each site carries its ground truth (expected findings)
  computed by the oracles, never by src/.
- Robots generators: the rv-wave1b `rfc_ref.py` / `robots_diff.py` regimes (plain, query, case, ties, pct_unreserved,
  pct_reserved, invalid_utf8, non_ascii) re-implemented in `gen/robots_gen.py`; hostile shapes: k-star near-match,
  needle `a`*(m-2)+`ba` x 4,348 rules (rv-wave1c `cost1.py`), 27,530 all-tried rules, 24,908 distinct prefixes,
  2,000 seven-wildcard rules, filler-then-disallow, long allow / anchored disallow, BOM / UTF-16 / CR-only files,
  gzip of empty members (2,200,043 B), 499 / 500 / 501 KiB ASCII and 3-octet files.

## 4. Oracles (one per behaviour family, written from the standard, sharing no code with src/)

| Oracle | Written from | Used by | Agreement required |
|---|---|---|---|
| O-RFC `oracles/rfc9309.py` | RFC 9309 2.2.1-2.2.3, 2.3.1, 2.5; RFC 3986 2.3 | B-RB-01..04, B-RB-07, B-SE-04, B-DE-11, B-LG-03, A-01..A-07 | 0 disagreements; undecided allowed only where the service reports it |
| sitemaps `oracles/sitemaps.py` | sitemaps.org protocol 0.9 | B-DE-01..12, O-01 | finding equality |
| W3C-DTF `oracles/w3c_datetime.py` | W3C note "Date and Time Formats" | B-DE-12 | 0 disagreements |
| llms `oracles/llms_shape.py` | llmstxt.org proposal text | B-DE-14 | 0 disagreements |
| O-NOIX `oracles/noindex.py` | Google "robots meta tag and X-Robots-Tag", Bing webmaster guidelines | B-SE-12, B-RO-10 | per-engine equality |
| BCP 47 `oracles/bcp47.py` | RFC 5646 section 2.1 + CLDR validity | B-RO-05, D-01 L | 0 disagreements |
| O-TPL `oracles/templates.py` | SPEC 0.1 / B-CD-04 | B-CD-04, B-LG-08, O-05 | equality |
| drift `oracles/drift.py` | SPEC 10 rule list | B-DR-01..08, A-28 | class and rule equality |
| XML `oracles/xml_enc.py` | XML 1.0 appendix F, 4.3.3 | B-DE-05, A-08 | detected encoding equality |
| careful-human corpus truth | the above, composed per site | O-01, O-02 | multiset equality per site |

## 5. How the bar is enforced ("no push unless it passes"; "only grows")

- CI: a job `conformance (seo-py)` runs the fast tier on every push and PR; required status for merge. (The lead adds
  the workflow step; builders do not edit `.github/workflows`.)
- Local: `services/seo-py/devtools/conformance/pre_push.sh` runs the fast tier; the builder briefs require it before
  any `git push`, and the PR template asks for its summary line.
- Growth: `test_catalogue.py` fails when (a) an id in `catalogue.lock` is missing from `catalogue.json`, (b) an entry
  has no collected test, (c) a test it names is skipped or xfail outside the lead tier, (d) a SPEC item id has no
  test, (e) an entry's fast-tier N is below the SPEC N. The lead gate rewrites `catalogue.lock` after a green run.
- Numbers: each test asserts the SPEC N it ran (`assert ran >= SPEC_N["A-08"]["fast"]`) so a generator that silently
  yields fewer cases fails.
- Universal bound (SPEC 12 header): the service fixture records per request 5xx, wall, CPU, traced peak, non-public
  connects and `/health` samples; a session-finish hook fails the run on any breach and prints the case id.
- Mutation (A-31): `devtools/conformance/mutants.py` holds every catalogued mutant as (file, anchor text, replacement);
  the lead gate applies each to a tar copy, runs the fast tier's tests for that module, and fails on a survivor that
  is not marked `equivalent` with its proof.
- A new reviewer finding becomes: one catalogue entry + one generator case + one test, in the same PR as the fix (or
  in a PR of its own marked xfail-strict in the lead tier until fixed; xfail is never allowed in the fast tier).

## 6. Catalogue seed: reviewer harnesses -> conformance tests

| Reviewer harness (scratchpad) | Finding(s) | Conformance test |
|---|---|---|
| rv-wave1 `redos.py`, `redos2.py`, `redos_svc.py`, `rules_cost.py`, `r2b.py` | M-S1 backtracking, rule floods, /health stall | `test_a_robots_cost.py::test_k_star_near_match`, `::test_rule_floods`, `::test_health_during_hostile_audit` |
| rv-wave1 `r1_selene.py` | BOM, NOINDEX per engine | `test_b_robots.py::test_bom`, `test_b_selene.py::test_noindex_oracle` |
| rv-wave1 `r1_delia.py`, `xml_probe.py` | UTF-16 DOCTYPE, fullwidth lastmod, H2-first llms | `test_a_xml.py::test_dtd_every_encoding`, `test_b_delia.py::test_w3c_oracle`, `::test_llms_oracle` |
| rv-wave1 `r1_roman.py` | heading skips, readiness | `test_b_roman.py::test_heading_oracle`, `::test_readiness_labels` |
| rv-wave1 `r1_osei.py` | drift sitemap 503 / llms drop REAL_CHANGE, silent caps | `test_b_drift.py::test_r1_failure_kinds`, `test_b_osei.py::test_caps_counted` |
| rv-wave1 `r3_audit.py`, `r3_logs.py`, `plants.py`, `find_leak.py` | M-S2..M-O3 leaks, robots text in log, `;` matrix key | `test_d_plants.py::test_every_surface_every_shape`, `test_d_sinks.py::test_log_ingest_inputs_vaulted` |
| rv-wave1 `r2.py` | SSRF-shaped redirects / canonicals / Sitemap / index children | `test_a_ssrf.py::test_positions_x_targets` |
| rv-wave1 `r4.py`, rv-wave1b `t_tenant.py`, rv-wave1c `tenants.py` | isolation 22 / 41 / 25 checks | `test_a_isolation.py::test_cross_tenant_every_route` (incl. evidence map) |
| rv-wave1 `mutants.py`, rv-wave1b `mutate.py`, rv-wave1c `mut/mutants.py` | 65 + 79 + 74 mutants, survivors | `devtools/conformance/mutants.py` entries (A-31) |
| rv-wave1b `rfc_ref.py`, `robots_diff.py`, rv-wave1c `rdiff.py` | M1 reserved / invalid UTF-8 encodings | `oracles/rfc9309.py`; `test_b_robots.py::test_oracle_regimes` |
| rv-wave1b `t_robots_truth.py`, `t_robots_truth2.py` | `%2A` allow fail-open, quoted rule not in file, filler rules, long allow | `test_b_robots.py::test_truth_cases`, `test_b_selene.py::test_quoted_rules_in_file` |
| rv-wave1b `t_slow.py`, `t_slow2.py`, `t_bigsite.py` | wall-clock budget false blocked / unreadable | `test_a_slow.py::test_slow_ordinary_site`, `::test_large_slow_site` |
| rv-wave1b `t_logq.py` | log query undercount | `test_b_logs.py::test_query_and_single_decode` |
| rv-wave1b `t_drift_budget.py`, `t_drift_canon.py`, `t_drift_corpus.py`, rv-wave1c `driftbat.py` | canonical-target failure REAL_CHANGE | `test_b_drift.py::test_own_fetch_state_either_side`, `test_a_records.py::test_drift_failure_matrix` |
| rv-wave1b `t_osei_caps.py`, step 18 notes | caps, oversized JSON-LD record | `test_b_osei.py::test_record_basis_table`, `test_a_records.py::test_jsonld_boundaries` |
| rv-wave1b `t_restart.py` | restart residual | `test_a_logs.py::test_restart_points` |
| rv-wave1b `t_plant.py`, `locate.py`, rv-wave1c `plant.py`, `hostleak.py`, `hostleak2.py` | letters-only names, host labels | `test_d_plants.py` (all surfaces; host-label surfaces explicit) |
| rv-wave1b `corpus.py`, `run_corpus.py`, `cmp_corpus.py`, `tpl_stats.py`, rv-wave1c `corpus.py`, `templ.py` | differential, over-templating | `test_o_corpus.py::test_oracle_equality`, `::test_differential` |
| rv-wave1b `r1_units.py` | lastmod, llms, XML encodings, lang | `test_b_delia.py`, `test_a_xml.py`, `test_b_roman.py::test_lang_oracle` |
| rv-wave1b `t_hostile.py`, rv-wave1c `battery.py` | 38 + 421 hostile cases | `test_a_xml.py`, `test_a_bodies.py`, `test_a_records.py` |
| rv-wave1c `cost1.py`, `health.py` | MUST-4 cost charging | `test_a_robots_cost.py::test_costliest_per_unit`, `::test_units_track_cpu` |
| rv-wave1c `gzbig.py` | MUST-2 gzip empty members | `test_a_robots_input.py::test_wire_only_excess` |
| rv-wave1c `dbg.py`, `unread.py` | MUST-3 long Location | `test_a_bodies.py::test_location_lengths` |
| rv-wave1c `delia_names.py` | well-known names digested | `test_o_corpus.py::test_well_known_names_readable` |
| rv-wave1c `lang.py` | lang="123" | `test_b_roman.py::test_lang_invalid_finding` |
| rv-wave1c licence notes | MUST-5 | `test_a_licence.py::test_bundled_sources_permissive`, `::test_third_party_notices` |
| builder briefs wave1fix1-3 (every M / S / NIT) | as listed there | one catalogue entry each, ids `r1-M-S1` ... `r3-NIT-lang` |

Existing tests at 91db0abb are kept where they are (`test_wave1_cert.py` 77, `test_wave1_cert_r2.py` 60,
`test_drift.py` 9, `test_logs.py` 16, `test_entity_check.py` 6) and referenced from the catalogue by id; tests that
assert the dictionary design (`test_letters_only_values_are_digests_keyed_per_tenant`,
`test_word_list_file_is_ascii_and_disclosed`, `test_unknown_types_are_counted_not_stored` against
`schema_types.py`) are REPLACED by the code-design tests, each replacement listed in the PR with the old assertion
and the stronger new one (no assertion weakened).

## 7. War-room scenarios (ADR 0018) to add, each case failing on 91db0abb and passing after

| Scenario | Cases | Invariants |
|---|---|---|
| `store-codes-planted` | one per A-18 surface group (robots, page markup, URLs same / other host, sitemaps, llms.txt, log ingest, entity) x 3 shapes | `planted_values_readable_nowhere` (D-05 scan), `owner_display_resolves_own_evidence` (D-06), `non_owner_sees_codes_only` |
| `robots-wire-and-length` | `gzip_empty_members`, `utf8_multibyte_over_limit`, `long_location_2011`, `long_location_8193`, `robots_429` | `no_false_unreachable`, `no_truncated_location_followed`, `findings_from_own_failure_are_ie` (B-UL-02) |
| `robots-cost-charging` | `needle_99_x_4348`, `paths_29000`, `distinct_prefix_24908` | `step_cpu_within_2s`, `within_the_stated_time_bound`, `no_interpreter_stall` |
| `host-labels` | other-host canonical / robots `Sitemap:` / sitemap loc with name-shaped and hex labels; own-domain lookalikes | `other_hosts_coded`, `lookalike_never_own` |
| `large-sitemaps` | `plain_6mib_45000`, `gzip_12mib`, `over_50mib` | `valid_sitemap_read`, `no_false_decompress_failed` |
| `codes-and-drift` | same site twice, key rotation between runs, two tenants | `codes_stable`, `rotation_is_r2_not_change`, `tenants_never_share_codes` |
| `vault-unavailable-audit` | no key, evidence blob lost | `refused_before_fetch`, `owner_display_not_available`, `nothing_raw_written` |

The three round-2 scenarios (`robots-matching-cost`, `drift-classification`, `robots-and-safety-round-2`) stay;
`drift-classification`'s `planted_letters_only_value_readable_nowhere` becomes the D-05 scan. Acceptance as ADR 0018 /
round 2 R9: every new case replayed one by one fails on 91db0abb (and is reported for a4af277) and passes at the
certified head; seeds 1-5 with the replay library all PASS.

## 8. Order of work (for the builder brief that follows approval)

1. Copy SPEC.md to `docs/spec/`, add `catalogue.json` with every entry of section 6, `test_catalogue.py` (red: tests
   missing).
2. Oracles and generators (sections 3-4) with their own self-tests (each oracle checked on the RFC / protocol
   examples it was written from).
3. B tests per module; A tests; D scanner and plants; O corpus; mark lead-gate parts.
4. Move the agents onto the vault / sink base (186471eb line): `primitives/codes.py`, evidence map blob, report blob
   holds codes; remove `path_words.txt`, `schema_types.py`, the exempt list; THIRD_PARTY_NOTICES.
5. Close the OPEN items (B-RB-09 b/c, B-FT-04, B-SE-03, B-DE-03, B-EN-01 canonical, B-RO-05 lang, B-OS-01 basis,
   A-03, A-13, A-32, the r1c mutant survivors) until the fast tier and the lead gate are green.
6. War-room scenarios of section 7; seeds 1-5; replays against 91db0abb.
