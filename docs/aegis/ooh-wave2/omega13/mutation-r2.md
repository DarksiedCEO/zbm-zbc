# AEGIS MUTATION gate r2 - range H025-H070 (founder: "Run H025-H070, then Omega.")
- candidateSha: cb708b924bb01aa5543b832573a8fa56bd041b80
- treeOid: eba95097622438b226169c88910c81cc9a999212 (verified: git rev-parse cb708b9^{tree}; tree/ made by git archive cb708b9)
- Author: AEGIS MUTATION gate (omega13 r2). I wrote the driver (raw/run_r2.py), the probes (raw/*.py) and this report; independent of the builder. Candidate untouched; every mutant lived in work/ copies, now deleted.
- First gate (given, not re-run): .../omega13/mutation.md sha256 faa98c2e4bd4bb6abeb9fb126e5bbb8a9ab4c9afb54a8d214ee874b5d52a50b3

## VERDICT (range H025-H070): BLOCK
Reason: 22 mutants of the range survive the scoped run AND the full suite on both Pythons and have a concrete probe (pass on pristine, differ on mutant). Three of them (H040, H041, H064) let an entity-spelled card or SSN be stored in vendor_text and recovered by html.unescape (H041/H064 by one unescape, H040 by a 17+ pass decoder); three more (H043, H046, H049) let a 10-digit number through. The whole backstop family (screen_free_text -> _still_holds_personal_data -> _forms) has no test that tells it from deletion. OOH2-PII-1 is founder-graded High, so this blocks. Under the R-GATE rule alone (money / purchase authorization / approvals / funds / tenant isolation / ledger) none of these survivors is in a listed area; I report BLOCK on the OOH2-PII-1 grading, not on R-GATE.
Combined picture with the first gate: the first gate's BLOCK (coverage) stands; this run adds probes showing the unrun backstop mutants are real survivors, not just unrun. Not a GREEN anywhere in H025-H070 as a whole. First-gate results for H035-H039 taken as given: H038 and H039 killed (both Pythons, tests test_r12_decoding_that_never_settles_redacts_the_whole_value and test_decisions_screen_finds_it[4111&amp;nbsp;...]); H035, H036, H037 (_MAX_DECODE 3/4/8) survived scoped and full on both Pythons.

## Counts for the range H025-H070 (46 mutants), identical on 3.12 and 3.13
| item | 3.12 | 3.13 |
|---|---|---|
| in range | 46 | 46 |
| taken from first gate (H035-H039) | 5 | 5 |
| run now | 41 | 41 |
| killed (run now) | 9 | 9 |
| killed-by-hang | 0 | 0 |
| survived scoped | 32 | 32 |
| survivors confirmed on FULL suite (1226 tests, both Pythons, all still pass) | 32 | 32 |
| equivalent (proved) | 9 | 9 |
| survivors with a probe (pass pristine, differ mutant) | 22 | 22 |
| survivor, no probe and no proof (H031) | 1 | 1 |
| UNRUN | 0 | 0 |
Time: first mutant 19:28:30, driver finished 20:41:35 = 73 min of the 90 cap. Process rule: 3 workers; before each pytest start I counted real interpreter processes (argv has pytest and the executable is python, not a shell) and waited while >=4. Pristine control runs of the scoped set: 3.12 978 passed in 77.58 s, 3.13 978 passed in 83.65 s (raw/results.jsonl NOOP), so the 240 s scoped timeout was never near; no timeouts occurred at all.
Mapping check: I reused the first gate's pii.py file list (16 files). I grepped tests for pii/screen_form/screen_free_text/strip_format/_fully_decoded/_still_holds: direct users are 6 files, all in the list; the list is a superset (service-level tests reach pii via the services). Mutant definitions: for all 41 run, orig text matched the code at the recorded offset in the frozen tree, each mutant compiles and differs from orig (0 bad). The first gate's definitions of H038/H039 etc. match too.

## Killed (9, each with its killing test, scoped run, both Pythons, rc 1 on mutant, pristine passes the same file set)
| id | mutation | killing test (uniquely named) |
|---|---|---|
| H025 | strip_format decimal() not digit() | tests/test_ooh2_pii1_cluster_rule.py::test_r12_s12_1_every_digit_valued_character_counts |
| H028 | non-digit default 0 | tests/test_ooh2_pii1_cluster_rule.py::test_decisions_unassigned_characters_are_kept_so_the_hash_is_stable[888] |
| H030 | screen_form one pass | tests/test_ooh2_pii1_cluster_rule.py::test_r12_screen_form_is_idempotent_with_hidden_characters_and_marks |
| H034 | screen_form re-reads original each pass | same test as H030 |
| H055 | stage 2 uses cluster finder | tests/test_ooh2_pii1_cluster_rule.py::test_cluster_labels_say_what_was_redacted |
| H056 | unsettled decoding keeps the text | tests/test_ooh2_pii1_cluster_rule.py::test_r12_nested_entities_and_decoded_forms_are_redacted[123456&amp;amp...] |
| H057 | unsettled decoding reports no kind | same test as H056 |
| H065 | backstop reads raw text | tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] |
| H067 | nothing found keeps raw text | tests/test_hold_confirmation.py::test_vendor_text_in_evidence_is_untrusted_data |
(Note: my own probes show H056, H065 and H067 WOULD leak a card/SSN if untested; they are killed only because those existing tests pin it.)

## Equivalent (9; proof by reading plus executed check)
- H026 (`> \x7f` -> `> \xff`), H029 (`>= \x7f`), H027 (digit() on every char): strip_format applies NFKC first. Executed (raw/equiv_strip.py, both Pythons): the only chars U+0080-U+00FF with a digit value are U+00B2, U+00B3, U+00B9 and NFKC rewrites all three, so none reaches the digit() line; digit("\x7f") is None; no ASCII char changes under digit() (only 0-9 map to themselves). Same output for every input.
- H032 (`return once` for `return t`): after the loop t==once on both exits (break: once==t; exhausted: t=once). H033 (`if False:`): when once==t the assignment t=once is a no-op, so the result is the same, only slower.
- H066 (kept = t not " ".join(t.split())): t is already the output of _norm (screen_form, which collapses whitespace and maps every non-printable to a space) with "[redacted]" substituted; executed: no character is isspace() and isprintable() other than " " (both Pythons), so split/join is the identity. 160k-input differential: 0 diffs.
- H069 (stage 1 given screen_form(text)): _screen_stages begins with screen_form(t); screen_form is a fixed point, so screen_form(screen_form(x))==screen_form(x) (fixed-point caveat: needs <=5 passes; observed max 2 changing passes in 900k inputs, see H031). 0 diffs in 160k.
- H044 (no _norm(f) form): _still_holds already tests _free_text_spans(_norm(f)) for each f, which includes number_cluster_spans(_norm(f)); the removed form adds only _norm(_norm(f)) (norm idempotent in practice). 0 helper-level diffs in 60k, 0 public diffs in 120k. Argued, fuzz-supported, not machine-proved.
- H042 (no forms of the as-kept text): the kept text comes out of the stages, which end by checking find(t) on its own _norm form (or found nothing at all), so its own forms are clean by construction; every form of the decoded text is still tested. Helper-level diffs exist (private function on arbitrary strings) but 0 public diffs in 160k. Argued, fuzz-supported, not machine-proved.

## Survivors with a probe (22). Both Pythons identical (raw/finder_3.12.json, finder_3.13.json are byte-identical)
"Stores card/SSN" = mutant output of screen_free_text (which service.py:77 and svc_campaigns.py:64 write for vendor_text) still decodes (html.unescape + screen_form) to a card, SSN or >=9-digit cluster. Probe inputs use fullwidth entity spellings so the raw-text decode misses them and only the backstop catches them (pristine redacts, kind ["number"]).
| id | what it is | probe (pass on pristine, differs on mutant) | stores card/SSN? | sev |
|---|---|---|---|---|
| H040 | _forms: unsettled gives [t] | fullwidth "&amp;"*16 + hex entities of 4111111111111111 (3794 chars) | YES: kept text nested 17-20 deep, recovered by a >=17-pass entity decoder; card or SSN | High (OOH2-PII-1) |
| H041 | _forms: decoded text dropped | fullwidth "&#x31;&#x32;&#x33;-&#x34;&#x35;-&#x36;&#x37;&#x38;&#x39;" -> mutant stores the string "&#x31;&#x32;&#x33;-&#x34;&#x35;-&#x36;&#x37;&#x38;&#x39;" = SSN 123-45-6789 after one html.unescape (card 4111111111111111 likewise, depth 1 and 2) | YES, SSN and card | High |
| H064 | backstop returns kept not [redacted] | same fullwidth SSN/card probes, depth 1,2,17,20 (kinds ['number']) | YES | High |
| H043 | _forms: no screen_form form | fullwidth "&#x2460;"*8 + "&percnt;40" -> mutant stores "&#x2460;...&percnt;40" (10 digit values after decode, below norm threshold) | number cluster, not a Luhn card/SSN | Medium |
| H046 | cluster test on first form only | same probe as H043 | cluster only | Medium |
| H049 | cluster test dropped from backstop | same probe as H043; also "4111&fflig;1111&fflig;1111&fflig;1111" (Luhn-valid card with ligature separators) stored by H045/H046/H049, pristine redacts. Pristine itself stores "4111ff1111ff1111ff1111" (ASCII letters end clusters by decision), so this is the same documented rule, not a new class | cluster / card spelled with ligature separator | Medium |
| H045 | _forms: no plain form | "112345%40&fflig;1111" -> pristine [redacted], mutant keeps text | cluster only | Low-Medium |
| H050 | unsettled decoding not a hit | the H040 probe -> mutant raises TypeError("'NoneType' object is not iterable") from _still_holds_personal_data (crash on hostile input, no store). Unit-reachable: screen_free_text(fullwidth 17-deep nesting) | no store; crash of the screen on attacker text | Medium (availability, if the service does not catch) |
| H047 | free-text test without _norm | fullwidth-a "ａ#x31; at [at]&#64;ﬀ.com#x34;" -> pristine keeps text, mutant redacts | no (over-redacts, fail closed); behaviour drift | Low |
| H048, H051 | free-text test dropped / first form only | "  5;%40.(7%40⁠  ]&zwj;&#%40 3 com&#64;123-45-6789)(at)3&#①1 dot nbsp;\n5;" -> mutant keeps "[redacted] com@[redacted])@3&#11 dot nbsp; 5;" (an email-shaped residue after decode); SSN part is redacted | email residue, not card/SSN | Medium |
| H052 | stage 1 skipped | "12345%404111" -> pristine kind ['number'], mutant ['email'] | no (wrong label; text redacted) | Low |
| H053 | stage 2 reads t without _norm | "x (at) y.com" -> pristine ['email'], mutant ['number'] | no | Low |
| H054 | stage 1 without screen_form | "111112345́" -> pristine "[redacted]", mutant "[redacted]́" (stray mark kept) | no | Low |
| H058, H061, H062, H070 | decoded form not kept / screened only when equal / from kept text / reads staged text | "&#64;1411112345" -> pristine "@[redacted]" ['phone'], mutant "&#[redacted]" ['phone'] | no; stored text differs (entity half-eaten) | Low |
| H059 | decoded kinds not recorded | "12345&shy;12345" -> pristine ['phone'], mutant ['number'] | no | Low |
| H060 | decoded form kept even if nothing found | "2%4091111&#64;́" -> pristine keeps the entity-spelled text, mutant keeps the decoded one | no | Low |
| H063 | backstop reports kinds without "number" | "112345%40&fflig;1111" -> pristine ['number'], mutant [] (redacted but unlabelled) | no | Low |
| H068 | nothing found keeps staged (normed) text | "%409" -> pristine "%409", mutant "@9" (stored text altered when nothing was found) | no | Low |
Honest scope of the probes: they are pin tests I ran as differential scripts (raw/finder.py, raw/pr3.py, raw/pr4.py, raw/pr5.py), not added to any repo. For each id the same input gave the pristine value on both Pythons and the mutant value shown.
Unresolved: H031 (two passes instead of five in screen_form) survives scoped and full on both Pythons; I found no input where 2 passes and 5 passes differ (900k structured and random inputs over Mn/Mc/Me/Cf/Co/compat/Indic/combining characters, with hidden characters interleaved, raw/probe_passes*.py; max changing passes seen = 2) and I have only a structural argument (NFKC idempotent; removal happens after NFKC in the same pass). Not proven equivalent, no probe: counted as survivor, severity none demonstrated.

## NOT RUN
- Any mutant outside H025-H070 (the founder scoped it; the first gate's non-H ids and H001-H024, H071+ not touched).
- H035-H039 (taken from the first gate, not re-run, as instructed); I did not re-verify its raw results.
- Service end-to-end store of the probes: I showed the screened value for vendor_text comes from pii.screen_free_text (service.py:77, svc_campaigns.py:64) and called the module function, I did not post the probes through a running service or append them to the log.
- Attack classes other than mutation/differential fuzz of pii.py: traversal, tenant escape, authorization bypass, evidence/SHA substitution, races, replay, resource exhaustion (the 17-deep decode cost is bounded: 16 passes) were not run.
- Not proven: H042, H044 and H069 equivalence beyond argument and fuzz.
- No mutant added to the full-suite survivor check for killed ones (not needed).

## Raw
raw/results.jsonl (148 records: 84 scoped incl. 2 pristine controls, 64 full), raw/T0.txt, raw/run.log, raw/run_r2.py, raw/finder*.{py,out,json}, raw/pr*.py, raw/diff*.py/.out, raw/probe_passes*.py, raw/equiv_strip.{py,out}. tree/ and work/ deleted at the end.
