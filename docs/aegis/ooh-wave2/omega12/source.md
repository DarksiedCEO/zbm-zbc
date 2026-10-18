# AEGIS SOURCE gate (Gate 1), omega12

- candidateSha: 1a904983d6e064b912bbd95a7afa1668da6c50c9
- treeOid: 83356dd062ff3e668358785f5e6d963cdb3950c2
- repo: DarksiedCEO/zbm-zbc, branch ooh-dept-15-wave2
- frozen copy: .../scratchpad/review-1a90498 (HEAD and tree matched, `git status --porcelain` empty, at start and at end)
- author line: AEGIS-SOURCE (AG-AEGIS-SOURCE v1.0.0), fresh context, read-only; raw output in .../scratchpad/aegis/omega12/source/
- scope: `git diff f5ee168 1a90498 -- services` (pii.py, service.py, 3 test files) + ADR 0022 decisions bullets, OPEN.md OOH2-R11-*, OOH2-PII-1-STRUCT/-FP, test-counts. Read in full: pii.py, svc_campaigns.observed (:60) / names_a_card (:155) / approval screen (:863), svc_inventory.parse_value (:73), service._screened / request_sha.
- verdict: **BLOCK** (two HIGH: S-1, S-2; both store nine real digits in clear)

## Areas (denominator: 8 required, 8 inspected; inclusion rule = every src file in the delta + every caller of the screens, grep `screen_free_text|free_text_has_personal_data|has_personal_data|pii.redact|names_a_card|screen_form`)
| path | class | evidence |
|---|---|---|
| services/ooh-py/src/pii.py | REAL | read in full; findings S-1..S-3 |
| src/service.py (_screened :70, request_sha :88) | REAL | hash = payload_sha256(_screened(body)), vendor_text replaced by screen_free_text(x)[0]; cache keyed by sha256 of canonical body, memory only |
| src/svc_campaigns.py (observed :60, names_a_card :155, approval :863) | REAL | not in the delta; names_a_card unchanged (differential below) |
| src/svc_inventory.py (parse_value :73) | REAL | not in the delta |
| tests (3 files in delta) | REAL | diff read; see S-4 |
| ADR 0022 decisions bullets | REAL | compared with code; S-3 |
| OPEN.md OOH2-R11-*, -STRUCT, -FP | REAL | present, decision 4 recorded |
| docs/test-counts.md | REAL | says 1190; suite collects/passes 1190 on both interpreters |

## Rule and decisions against the code
- Rule (cluster of 9+ digits, normalised, any separator): implemented in number_cluster_spans (pii.py ~:245) on `screen_form(text)` then on `_norm(t)`; labels (_label) are applied after the spans are chosen; hash uses the kept text (service.py:77). OK for text without entities.
- Decision 1 (stop removing unassigned): `_REMOVED = ("Cf","Co")`, screen_form keeps category Cn. OK. Checked the 3.12 vs 3.13 character data over all 0x110000 code points: 627 differ, all Cn -> Lo/So (622 + 5), no digit, no NFKC change, so kept text and hash do not depend on the build (source/ucd-*.pkl comparison; tests pin four code points).
- Decision 2 (only ASCII letters end a cluster): `_ASCII_LETTERS` frozenset, digits `"0"<=ch<="9"`. OK; look-alike probes ('1234ʼ56789', '12ー34ー56ー789', '123ǀ456ǀ789', tatweel, '123一456一789') all redacted on 3.12 and 3.13.
- Decision 3 (decode entities, screen raw and decoded, like S-1): **not met exactly**, see S-1 and S-2.
- Decision 4 (accept FP, OPEN item for structured fields): OOH2-PII-1-STRUCT and -FP present (OPEN.md:123-124). OK.

## Probes
- S-1 probe from f5ee168 (source/probe1-out.txt, pred_probe3-out-*.txt): `1234567%40` -> `('[redacted]', ['number'])`, `1234567&#64;` -> `[redacted];`, `note 1234567%40 end` redacted, on 3.12 and 3.13. Predecessor's fuzz (600,000 strings, seed 5, property with ASCII-only letters): 0 violations, 0 explained, on both. **Predecessor S-1 closed for the raw path.**
- Property fuzz (fuzz3.py, 600,000 strings, seed 20261018, pool incl. entities, %40, [at], Unicode digits, hidden characters, unassigned code points; forms: kept, screen_form(kept), _norm(kept), fully-decoded, and the compositions of those): 3.12 and 3.13 identical: **kept, screen_form(kept), _norm(kept): 0 violations. Fully-decoded-derived forms: 4948 violations** (4931 where the decoded text, read by screen_form, holds 9+ digits that `_norm` counts as fewer, i.e. the %40 class; 17 where a hidden character recomposes a letter, S-2). With deeper entity nesting added to the pool (fuzz2): 1756 more where nesting is 4 or more (S-1). Raw: source/fuzz3-out-3.12.txt, fuzz3-out-3.13.txt, fuzz2-out-*.txt.
- names_a_card vs 99569bd (differential, svc_campaigns.names_a_card; 150,000 mixed inputs per seed, seeds 3 and 77, Unicode digits and hidden characters in the pool): identical result hash old vs new in both seeds (4898af90...06134 / 40000 true; 9e398cf5...6124b / 39876 true). The delta f5ee168..1a90498 does not touch svc_campaigns.py or card_spans/luhn_card/aba_routing (source/names_a_card_diff.txt). Unchanged.
- Suite in frozen copy, services/ooh-py: 3.12 = 1190 passed (93.6 s), 3.13 = 1190 passed (111.7 s) (pytest-3.12.txt, pytest-3.13.txt). test-counts.md says 1190: matches.

## Findings

### S-1 HIGH (FAKE_SUCCESS / DOC_DRIFT against founder rule): nine real digits are stored when the separators are entities nested four or more deep
- path: services/ooh-py/src/pii.py:314-320 (`for _ in range(3)` decode loop in `screen_free_text`); ADR 0022:384 ("repeatedly, for "&amp;nbsp;"") does not state the cap.
- evidence (source/probe3-out.txt, identical on 3.12 and 3.13): `'123456&amp;amp;amp;amp;nbsp;789'` -> `('123456&amp;amp;amp;amp;nbsp;789', [])`; `'123&amp;amp;amp;amp;ndash;45&amp;amp;amp;amp;ndash;6789'` -> unchanged, no kinds. Both are 9 real digits (an SSN shape) and a full decode gives `123456 789` / `123-45-6789` with `html.unescape` applied 4 times. Three levels are redacted. Reaches vendor_text (stored, hashed in clear) and the face text attribute (accepted); not the approval document reference (REF pattern forbids `&`, `;`). Tests mirror the cap: `_unescape` in test_ooh2_pii1_cluster_rule.py:153-157 also loops 3 times, so the "fully decoded" assertion cannot see it.
- reproducer: `cd services/ooh-py/src && python -B -c "import pii; print(pii.screen_free_text('123456&amp;amp;amp;amp;nbsp;789'))"`
- falsifier: that call returning kinds != [] (or fewer than 9 digits in the text); or the property fuzz (source/fuzz2.py) reporting 0 in the nested>=4 class. Not wrong if Ω rules that only a consumer decoding four times counts (the caller's property says "fully HTML-decoded", so I graded it against that).
- NOT shown: that any consumer in the repo decodes entities; function level only.

### S-2 HIGH (FAKE_SUCCESS): the decoded text is screened once, by `_norm`, not by the two-stage (screen_form cluster, then `_norm`) that decision 3 says to copy from S-1; two residues
- path: services/ooh-py/src/pii.py:319-320 (`if decoded != t and _free_text_spans(_norm(decoded))`); non-idempotence of `screen_form` at pii.py:93 (NFKC runs before `_ZW.sub`, so a letter and a combining mark that a removed character separated recompose only on the next pass).
- (a) the %40 class again, through an entity: `'1234567&nbsp;%40'`, `'1234567&amp;%40'`, `'1234567&#x25;40'` -> kept unchanged, kinds []. The decoded text `1234567 %40` has 9 digits joined by non-ASCII-letters (founder's S-1 reading counts the "40"); `_norm(decoded)` turns %40 into "@" and counts 7. 4931 of 600,000 fuzz strings (identical 3.12 / 3.13). Same exactness failure as the predecessor's S-1; privacy exposure small (two of the nine are the spelling's own digits).
- (b) real digits: `'12345a&zwj;&#769;6789'`, `'12345a&#8205;&#x301;6789'`, `'12345a&shy;&#769;6789'`, `'12345a&#x200b;&#769;6789'` -> kept unchanged, kinds [] (nine real digits, a SSN length). Decoded and normalised twice it reads `12345á6789`: "a" + a combining mark that a hidden character separated recomposes to "á", which under decision 2 joins the cluster (nine digits, no ASCII letter). `screen_form(screen_form(x)) != screen_form(x)` for these (probe3-out.txt: "sf idempotent: False"). The same text without entities (`12345a<U+200B><U+0301>6789`) is redacted, because the second pass re-normalises; the entity path normalises the decoded text once. 17 of 600,000 fuzz strings hit this by chance (forms `decoded_norm_sf`), the constructed probes hit it every time.
- reproducer: `python -B -c "import pii; print(pii.screen_free_text('12345a&zwj;&#769;6789')); print(pii.screen_free_text('1234567&nbsp;%40'))"` in services/ooh-py/src.
- falsifier: either call returning kinds != []; or screen_form being idempotent over the fuzz pool.
- note: ADR 0022:386-388 and OPEN.md OOH2-R11-SRC-S1 say "such a spelling cannot hide digits"; (a) shows the spelling still counts differently on the decoded path.

### S-3 MEDIUM (DOC_DRIFT): stale statements that unassigned characters are removed and that letters, not ASCII letters, end a cluster
- pii.py:7 (module docstring: "unassigned and invisible filler characters removed"), pii.py:30 and :241 ("joined by characters that are not letters"), ADR 0022:344 ("`screen_form` also removes private-use, unassigned and invisible filler characters") and ADR 0022:362 ("invisible, format, private-use and unassigned characters removed") and :364 ("characters that are not letters") describe the superseded behaviour. Code: pii.py:80-84 keeps Cn, :243-247 uses ASCII letters only. The new decisions bullets (ADR 0022:378-392) are right; the older text next to them contradicts them.
- reproducer: `grep -n "unassigned\|are not letters" services/ooh-py/src/pii.py docs/adr/0022-ooh-buy-and-contract-controls.md`
- falsifier: those lines reworded to the new behaviour, or marked superseded.

### S-4 LOW (test honesty): the new property tests cannot see S-1 or S-2
- test_ooh2_pii1_cluster_rule.py:131 and :149 check `kept`, `screen_form(kept)`, `_norm(kept)` and `_norm(_unescape(kept))` with the 3-level `_unescape`; none checks `screen_form(decoded)` or deeper nesting, and the 20,000-string pool has no combining mark or ZWJ. My fuzz found the two classes with the form `screen_form(full_decode(kept))`.
- reproducer: read lines 125-160; falsifier: a test over those forms passing at this SHA.

### S-5 INFO: other residues, already disclosed or by design
- Non-decimal digit numerals that NFKC leaves alone (e.g. dingbat circled digits U+2776-277E, "❶❷❸❹❺❻❼❽❾") are `str.isdigit()` but not converted; nine of them are kept (probe1-out.txt). OPEN.md OOH2-R11-SEC-L names this class ("non-decimal numerals ... kept as typed"); the founder wording was "all Unicode digits".
- Dead code unchanged from r11: `pii.redact` and `_redact_once` have no caller in src (tests only; OPEN.md OOH2-R11-SRC-L names it). `_ZW` overlaps `_INVISIBLE`/Cf removal (OPEN.md says harmless; true). In `screen_free_text` the decoded form is computed twice (`_norm(decoded)` at :319 and :320): no defect.
- `request_sha` docstring (service.py:89-93) now matches the code (vendor text in screen form hashes as before, otherwise not): predecessor S-3 closed. Predecessor S-2 (hidden char read both ways) closed by the ADR rewording at :345-347. S-4 `{fix}` placeholder gone (grep).

## Checks the caller asked for
- Rule + four decisions implemented exactly? 1, 2, 4 yes; 3 only partly (S-1, S-2). The rule holds for kept, screen_form(kept), _norm(kept) in 1,200,000 fuzz strings (both runs) and the predecessor's S-1 fuzz (0/600,000 x 2); it does not hold for fully decoded forms.
- Hash uses only the redacted text: yes, `_screened` replaces vendor_text by `screen_free_text(x)[0]` at any depth; test `request_sha == payload_sha256({... kept})` passes; hence the S-1/S-2 residues are hashed in clear too.
- Docs vs code: S-3, and the "cannot hide digits" claim in S-2.
- Unreachable / dead code: as S-5. No TODO/placeholder/fake-success marker added in the delta (grep of the patch).

## NOT RUN
No HTTP/end-to-end witness (function level only; call chain read); no security exploitation beyond the probes; no Python 3.14; no mutation run; other services not reviewed; docs/aegis/* history files not reviewed; no review of ledger/store reliability; no check whether any consumer decodes entities (S-1 rests on the founder property "fully HTML-decoded").

## Falsifiers for the verdict
BLOCK is wrong if `pii.screen_free_text('123456&amp;amp;amp;amp;nbsp;789')` and `pii.screen_free_text('12345a&zwj;&#769;6789')` both redact, or if Ω rules that consumer-side multi-round decoding and re-normalisation is outside the founder's property. Everything is void if the SHA or tree changes.
