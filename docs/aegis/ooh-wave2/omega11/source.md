# AEGIS SOURCE gate (Gate 1), omega11

- candidateSha: f5ee168c9e9148241b709ec93acc660c88d08ba7
- treeOid: da5fc085a0669ba5f92df726cfefcb28bd505cab
- repo: DarksiedCEO/zbm-zbc, branch ooh-dept-15-wave2
- frozen copy: .../scratchpad/review-f5ee168 (HEAD and tree matched at start and end; `git status --porcelain` empty at both)
- author line: AEGIS-SOURCE (AG-AEGIS-SOURCE v1.0.0), fresh context, read-only; raw output in .../scratchpad/aegis/omega11/source/
- scope: `git diff 0f5fbe3 f5ee168 -- services`; pii.py, svc_campaigns.observed + approval screen, svc_inventory.parse_value, service._screened/request_sha read in full; ADR 0022 two sections, OPEN.md rows, test-counts
- verdict: **BLOCK** (one HIGH, see S-1)

## Areas (denominator: 11 required, 11 inspected; inclusion rule = every src file changed in the delta + every caller of the screens found by grep `has_personal_data|screen_free_text|free_text_has|observed(|pii.redact`)
| path | class | evidence |
|---|---|---|
| services/ooh-py/src/pii.py | REAL | read in full; screen_free_text, number_cluster_spans, _label, strip_format, screen_form |
| src/svc_campaigns.py (observed, approval screen :863, names_a_card) | REAL | read; names_a_card differential below |
| src/svc_inventory.py (parse_value :73-75, observed :114) | REAL | read |
| src/service.py (_screened, request_sha, cache) | REAL | read |
| src/store.py (errno, _close_quietly) | REAL | read diff |
| src/models.py REF (client_approval_doc_ref) | REAL | `^[A-Za-z0-9._:-]{1,100}$`, so the field cannot carry hidden characters |
| src/svc_contracts.py:97, svc_campaigns.py:416/615/773, svc_inventory.py:217 (vendor_text callers) | REAL | all go through observed() |
| tests (7 files in delta) | REAL | diff read |
| docs ADR 0022, OPEN.md, test-counts | REAL | diff read |

## Suite
`pytest -q` in the frozen copy services/ooh-py: 3.12 = 1137 passed, 3.13 = 1137 passed (source/pytest-3.12.txt, pytest-3.13.txt). docs/test-counts.md says 1137: matches.

## Findings

### S-1 HIGH (FAKE_SUCCESS / DOC_DRIFT against the founder rule): a 9+ digit letterless cluster is stored when an "@" spelling carries digits
- path: services/ooh-py/src/pii.py:276 (`t = _norm(text)`) and :295 (`return screen_form(text), []`); counts digits on the `_norm` form, keeps the `screen_form` form.
- claim broken: ADR 0022 "The rule"/"Effect" and the founder's words: any cluster of 9+ digits joined by non-letters is redacted or refused, whatever the separators.
- evidence: `_norm` turns `%40`, `&#64;`, `&#x40;` into "@", which removes two digits from the count. The cluster is counted on that text. When nothing else is found the kept text is `screen_form(text)`, the form that still holds "40"/"64". Observed output (source/probe3-out.txt):
  - `'1234567%40'` -> `('1234567%40', [])` (9 digits stored)
  - `'1234567&#64;'`, `'12345678&#x40;'`, `'note 1234567%40 end'` -> unchanged, no kinds.
  Fuzz, 600,000 random strings on 3.12: 1090 outputs held a letterless run of 9+ digits; 1090 of 1090 contain such a spelling, 0 unexplained (probe3.py). Same on 3.13 for probe2 (16 violations, all this class).
  Reaches vendor_text (observed -> stored), the face text attribute (accepted, then stored via observed), and the hash (`_screened` uses the same function). Not reachable for client_approval_doc_ref (REF pattern forbids "%", "&", "#", ";").
- impact: real digit content cannot hide this way (only the 2 digits of the spelling are uncounted, so a real 9-digit number still counts 9); this is an exactness failure of the control whose purpose was to end spelling-by-spelling holes, and it is a new spelling hole. I grade it HIGH because the acceptance criterion given was exactness of the founder rule; the privacy exposure itself is small. Ω may re-grade it.
- reproducer: `cd services/ooh-py/src && python -B -c "import pii; print(pii.screen_free_text('1234567%40'))"` (prints `('1234567%40', [])`).
- falsifier: that call returning kinds != [] or text with fewer than 9 digits; or the fuzz in source/probe3.py reporting 0.

### S-2 MEDIUM (UNSAFE_DEFAULT / DOC_DRIFT): the three screens no longer read a hidden character as a space; ADR still says they do
- path: pii.py:272-299 (`screen_free_text` uses only the removed form; `_screened_form` two-form logic stays only for `has_personal_data`/`redact`); docs/adr/0022-ooh-buy-and-contract-controls.md:346 ("A hidden character is read both ways ... personal data in either form counts").
- evidence (source/probe1-out.txt): `'jane​at​example.com'` and `'janeㅤatㅤexample.com'` -> `screen_free_text` = `('janeatexample.com', [])`; the retained `has_personal_data` says True for both. Stored form is "janeatexample.com", which is not an address, so no address is stored; the loss is a screen that the ADR says exists and does not. Digit-based rules are unaffected (separator or not, a cluster forms).
- reproducer: `python -B -c "import pii; t='jane​at​example.com'; print(pii.screen_free_text(t), pii.has_personal_data(t))"`.
- falsifier: the second form being read in screen_free_text, or the ADR sentence being scoped to the non-cluster fields.

### S-3 LOW (DOC_DRIFT): request_sha docstring says "a body without personal data hashes as before (logs written earlier still replay)"
- path: services/ooh-py/src/service.py:89-90. Now vendor_text always hashes as `screen_form` (service.py:77). Measured: `'a  b'`, `'trailing '`, `'٣ lights'`, decomposed "café" hash differently from the raw body; only already-normal text hashes as before (source/probe4-out.txt). The ADR (design change, "The request hash") admits a possible 409 after the change; the docstring does not.
- reproducer: `service.request_sha({"request_id":"r","vendor_text":"a  b"}) == ledger.payload_sha256(same)` is False. Falsifier: True.

### S-4 LOW (DOC_DRIFT): OPEN.md rows
- docs/findings/OPEN.md:114 (OOH2-R10-DATA-H1) ends with a literal unfilled placeholder `{fix}`.
- OOH2-R10-SRC-L says "`_ZW` now redundant": `_ZW` is still applied (pii.py:40, :88), now overlapping `_INVISIBLE`/Cf removal; not removed.
- OOH2-R10-SEC-F1 / ADR round-10 text say hidden characters are read both ways and "observed() screens the raw text exactly as the attribute and the hash do"; see S-2.
- reproducer: `grep -n '{fix}' docs/findings/OPEN.md`. Falsifier: no match.

### S-5 INFO (design residue, documented partly): "letter" is `str.isalpha()`
- pii.py:246. Any category L* character separates clusters, including look-alikes of punctuation and spelled numerals: `'123ǀ456ǀ789'` (U+01C0, Lo) and `'123一456一789'` pass unchanged (probe1-out.txt). This follows the founder's literal rule ("not letters") and the module docstring says letters/spelled digits cannot be detected, but neither the ADR nor OPEN.md PII-1-FP names letter look-alike separators. 881 numeric characters (Nl/No: Bengali currency numerators, cuneiform and similar) are neither digits nor letters for the screen and so are treated as separators, not counted (probe2). Unicode digits of category Nd are all read as ASCII (0 misses over all code points); ❶-style `isdigit` characters are counted.

### S-6 INFO: dead code
- `pii.redact` and `_redact_once` have no caller in src (grep: only tests). `_screened_form` is live (has_personal_data -> ref attribute, campaign name). `_ZW` is live but redundant (S-4). Not defects.

## Checks the caller asked for
- Rule implemented for every path of the three fields? vendor_text: all five callers use observed(), and `_screened` covers the hash for key "vendor_text" at any depth. Text attribute: parse_value:73 (kind "text") refuses, then observed() stores. client_approval_doc_ref: svc_campaigns.py:863 refuses before `rk`/`_idem`/any write. Other kinds of attribute ("ref") keep the old screen, as ADR says. Only gap: S-1.
- Hash uses redacted/kept text only: yes (service.py:77 `screen_free_text(x)[0]`; test `request_sha == payload_sha256({**body, "vendor_text": vt["text"]})` passes). Cache keyed by sha256 of canonical raw body, value is the post-redaction hash, memory only, bounded 64: no PII persisted. Note the hash covers `screen_form`, not `observed()` truncation.
- Kinds are labels only: `_label` is called after spans are chosen; refusal/redaction decided by `_free_text_spans` (all old spans + clusters). Yes.
- Fuzz 300,000 strings x2 interpreters (probe2): output invariants violated only by the S-1 class (16/16 contain %40).
- names_a_card unchanged vs 99569bd: diff of 60,000 mixed inputs between the 99569bd module and f5ee168: identical result hash 0cc17819...e80df, 16011 True each (source/names_a_card_diff.txt). Code moved to pii.card_spans/aba_routing/luhn_card; `break` became `return False` per start index, equivalent.
- Test changes honest and complete: controls moved from "must pass" to "refused" are exactly those with a 9+ digit letterless cluster (GPS, ZIP+4, "routing 021000021", "Austin 123456789", "permit 123-456-789", "4111-1111-1111-1112", Luhn/prefix negatives, `..` date range) and each is re-pinned as refused/redacted in test_ooh2_pii1_cluster_rule.py FALSE_POSITIVES/CAUGHT; one edited ordinary text was reworded ("..", to "to"); one self-comparison assertion fixed. I found no control weakened without being re-pinned. Weakness: test_r10_storage_redacts_exactly_when_the_screen_finds_personal_data now compares against the old `has_personal_data`, not the new rule, and no test covers an "@" spelling beside digits (why S-1 passed).
- ADR/OPEN/docstring vs code: match on rule, scope, kinds, false-positive list, test count (1137); mismatches in S-2, S-3, S-4.

## NOT RUN
- No security exploitation beyond the above probes, no runtime/HTTP witnessing of S-1 end to end (function-level only; the call chain was read), no 3.14, no mutation run, no other services, no review of docs/aegis/* history files, no ledger/store reliability testing of the r10 store.py change beyond the suite.

## Falsifiers for the verdict
BLOCK would be wrong if `pii.screen_free_text('1234567%40')` redacts, or if Ω rules S-1 below HIGH. Everything here is void if the SHA or tree changes.
