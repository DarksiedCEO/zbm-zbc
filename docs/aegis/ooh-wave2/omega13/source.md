# AEGIS SOURCE gate (Gate 1), omega13

- candidateSha: cb708b924bb01aa5543b832573a8fa56bd041b80
- treeOid: eba95097622438b226169c88910c81cc9a999212
- repo: DarksiedCEO/zbm-zbc, branch ooh-dept-15-wave2
- frozen copy: .../scratchpad/review-cb708b9 (HEAD, tree matched and `git status --porcelain` empty at start and at end)
- author line: AEGIS-SOURCE (AG-AEGIS-SOURCE v1.0.0), fresh context, read-only; raw output and scripts in .../scratchpad/aegis/omega13/source/
- scope: `git diff 1a90498 cb708b9 -- services` (only src/pii.py in src; 2 test files), ADR 0022 r12 bullets, OPEN.md OOH2-R12-*, test-counts.md. pii.py read in full (394 lines).
- verdict: **SOURCE_GREEN** (no CRITICAL/HIGH; one MEDIUM, three LOW, none blocking). Void if SHA or tree changes.

## Areas (denominator: 6 required, 6 inspected; rule = every file in the services delta + the callers of the changed functions found by grep `screen_free_text|free_text_has_personal_data|has_personal_data|names_a_card|screen_form|_INTL`)
| path | class | evidence |
|---|---|---|
| services/ooh-py/src/pii.py | REAL | read in full; probes and fuzz below |
| src/service.py (_screened :77) | REAL | not in the delta; vendor_text hashed as `screen_free_text(x)[0]` (kept text only) |
| src/svc_campaigns.py (observed :64, names_a_card :156, name :279, approval :863) | REAL | not in the delta; names_a_card differential below |
| src/svc_inventory.py (parse_value :73/:75) | REAL | not in the delta |
| tests (test_ooh2_pii1_cluster_rule.py, test_aegis_r12_mutation_gaps.py) | REAL | diff read; they assert behaviour, no placeholder/TODO/skip added (grep of the patch) |
| ADR 0022 r12 bullets, OPEN.md OOH2-R12-*, test-counts.md | REAL | compared with code; see F-3, F-4 |

## Results of the checks asked for
1. **Property fuzz** (`source/fuzz13.py`, 600,000 strings per interpreter, seed 20261019, pool of 110 items: digits, separators, entities incl. nested up to 12 and `&amp;#x34;`, %40, [at]/<at>, ZW/format/invisible/fillers, combining marks, unassigned and CJK Ext I code points, Arabic/Devanagari/full-width/circled/dingbat/Ethiopic digits). Forms checked on the kept text: as kept, screen_form, _norm, fully decoded (to a fixed point), screen_form/_norm of decoded, norm of norm, screen_form of norm, screen_form of screen_form, and decode of those.
   - 3.12 and 3.13 identical: 0 violations in every form the caller listed. 5 strings violate only one extra form of my own, `decode(_norm(kept))` (decode applied after _norm, not screen_form after it): each rests on `_norm` turning "[at]"/"<at>" into "@" (no letter) while the decoded text keeps a literal "%40" or a "℆" that screen_form would turn into letters. That order is not in the caller's property and not a reader the code describes; reported as an explained residue, not a finding (`fuzz13-out-*.txt`).
   - Dense pool (`fuzz13b.py`, 400,000 per interpreter, seed 77, digit-heavy, length 8-60): 0 violations on both.
   - Predecessor's own scripts re-run unchanged: `omega12/source/fuzz.py` seed 5 (600k), `fuzz3.py` seed 20261018 (600k), `fuzz2.py` seed 31 (600k, nesting up to 4+), on 3.12 and 3.13: all 0 violations (`fuzzpred-out-*`, `fuzz3pred-out-*`, `fuzz2pred-out-*`).
2. **S-1 / S-2 probes** (`probes13.py`, `probes13-out-3.12/3.13.txt`; the two outputs differ only in how 3.13 prints U+2FFC): `123456&amp;amp;amp;amp;nbsp;789`, the nested ndash SSN, `1234567&nbsp;%40`, `1234567&amp;%40`, `1234567&#x25;40`, `12345a&zwj;&#769;6789`, `...&#8205;&#x301;...`, `...&shy;&#769;...`, `...&#x200b;&#769;...`, the raw U+200B/U+0301 form: all `[redacted]` with a kind. Benign text ("ok text", "[redacted] 12345678") is kept. Predecessor S-1 and S-2 are closed at function level.
3. **Decode bound and redact-whole fallback**: `_fully_decoded` (pii.py:318-327) loops 16 times; nesting of 15 converges and is screened; nesting of 16, 17 and 40 returns None and the value is `[redacted]` with kinds `['number']` (probe lines 17-20, both interpreters). A text that would settle on pass 17 is redacted whole: conservative, matches the ADR wording ("at most 16 passes").
4. **Backstop on every return path**: `screen_free_text` (pii.py:352-369) has two returns. The early one (`d is None`) returns the constant `[redacted]`; the final one runs `_still_holds_personal_data(kept)` first. `_still_holds_personal_data` tests `number_cluster_spans` and `_free_text_spans(_norm(f))` over `_forms` = kept, screen_form, _norm of the kept and of its fully decoded text, and fails closed when decoding does not settle. Fuzz above is the witness; no path returns unchecked text.
5. **screen_form idempotence**: 300,000 random strings over all code points plus 400,000 strings over a hostile alphabet (ZWJ, SHY, CGJ, Hangul fillers and jamo, Indic vowel signs, marks, line separators, Arabic-Indic digits), 3.12 and 3.13: 0 non-idempotent (`digits13-out-*`, `idem13-out-*`). The 5-pass cap is not reached by anything found; no proof that no input needs more than 5.
6. **Every digit-valued character counted** (`digits13.py`): all 798 code points >= U+0080 with `isdigit`, `isdecimal`, `unicodedata.digit`, `decimal` or category Nd (nine repeats in "note ... end"), plus every code point whose NFKC is one ASCII digit: 0 missed on 3.12 and 3.13 (the delta's own test does the same loop). Characters with only a numeric value (〇, Suzhou, ½) stay as typed, disclosed as OOH2-R12-LOW.
7. **Version-independent word class**: `ver13.py` runs 8 contexts per code point over all 1,112,064 scalar values (e-mail tight, " at " shape, intl number after the character, cluster, face text, nine repeats), results hashed: 3.12 and 3.13 produce the same sha256 `4be53779cbdc15a13e9e7cb23a4e9fb72cbf2d0bbd5d850e7a87d9160257a081`. The `_W` class is `[A-Za-z0-9_]` or any non-ASCII non-space. But see F-1: the same change widened a negative lookbehind in `_INTL` the wrong way.
8. **names_a_card vs 99569bd** (differential, `gen_diff2.py` from omega12, 150,000 inputs per seed, seeds 3 and 77, both interpreters, old = 99569bd tree in `omega12/source/old99`, new = cb708b9): identical result hashes, 40000 and 39876 true respectively (`names_a_card_diff13.txt`). `svc_campaigns.py` and `card_spans`/`luhn_card`/`aba_routing` are untouched since 1a90498.
9. **Performance** (`perf13.py`, inputs up to 160k characters although the model limit is DOC_MAX = 4000 and the body limit 64 KiB): worst case 1.24 s for 40k characters (`"x"*60 + " at " + "é"*30000`-type input); nothing quadratic at the real limits.
10. **Full suite in the frozen copy** (services/ooh-py, `-p no:cacheprovider`, one pytest process at a time): 3.12 = 1226 passed in 122.45 s; 3.13 = 1226 passed in 153.29 s (`pytest-3.12.txt`, `pytest-3.13.txt`). `docs/test-counts.md` says 1226: matches.
11. **Docs vs code**: ADR 0022 r12 bullets match the code for the 16-pass bound, the fail-closed check, the fixed-point screen_form, digit-valued characters, ASCII-letter clusters and the unassigned-characters rule. The escape limit is real: `4111%2D1111%2D1111%2D1111` is kept (probe). Two drifts: F-2, F-3.
12. **Dead code**: `redact` and `_redact_once` still have no caller in src (tests only); `_ZW` overlaps `_INVISIBLE`/Cf removal. Both were already disclosed (OOH2-R11-SRC-L). No TODO/FIXME/placeholder/fake-success marker in the delta.

## Findings

### F-1 MEDIUM (UNSAFE_DEFAULT, fail-open regression introduced by the delta): an international number after a non-ASCII punctuation or symbol character is no longer found by the phone shape
- path: services/ooh-py/src/pii.py:80, `_INTL = re.compile(r"(?<![A-Za-z0-9_+])(?<![^\x00-\x7f\s])\+\d{1,3}...")`; the comment at :70-72 says the new class is "wider than `\w`: fails closed", which is true for the domain/local-part classes but false for a lookbehind (a wider forbidden set means fewer matches).
- evidence: at 1a90498 (`aegis/omega13/source/old1a/pii.py`) the lookbehind was `(?<![\w+])`, so `“`, `–`, `«`, `¿`, `‘`, `€` before the plus did not block the match. At cb708b9: `has_personal_data("Call “+44 20 7946 0958” now")` False (was True), same for `«+44 ...»`, `¿`, `‘`, `–+44`, `€+52 55 1234`; `free_text_has_personal_data("“+44 20 7946")` False (was True). With a 9+ digit number the cluster rule still catches it in the three free-text fields, so what is lost there is only a number of exactly 8 digits (`+<cc> ` plus 5-7 digits). Output: `source/intl13-out.txt` (old1a first, candidate second; scripts `intl13.py`, `intl13b.py`). Not caught by the delta's tests (`test_r12_r12_2_...` only pins the letter side).
- reachability: the other users of `has_personal_data` are campaign `name` (svc_campaigns.py:279) and `ref` attributes (svc_inventory.py:75); models.py:56 limits the name to `^[A-Za-z0-9 ._&'-]{2,120}$` and `_REF` (svc_inventory.py:43) to ASCII, so neither can carry the character today. Latent at function level; bites if a pattern is ever widened, or for an 8-digit number in the three free-text fields.
- reproducer: `cd services/ooh-py/src && python -B -c "import pii; print(pii.has_personal_data('Call “+44 20 7946 0958” now'))"` -> False; the same with `pii` taken from `git show 1a90498:services/ooh-py/src/pii.py` -> True.
- falsifier: the call returning True; or a lookbehind that excludes only word characters (`(?<![A-Za-z0-9_+])(?<![^\x00-\x7f\s\W])`-style) with the probes passing on both interpreters.
- not blocking: Medium; the founder cluster rule and the three fields' API reach are intact.

### F-2 LOW (DOC_DRIFT): the module comment and ADR claim the new word class "fails closed" without exception
- path: pii.py:70-73 (comment), ADR 0022 r12 bullet "the e-mail and phone shapes use a word class that is the same on every Unicode version". Wrong for `_INTL` (F-1). Falsifier: wording limited to the e-mail shapes, or F-1 fixed.

### F-3 LOW (DOC_DRIFT): OOH2-R11-SRC-L says `redact()`/`_redact_once` are "kept for campaign names and tests"
- the campaign name uses `has_personal_data` only (svc_campaigns.py:279); `redact` has no src caller (`grep -rn "redact(" services/ooh-py/src`). Carried over from r11, unchanged; truth is "tests only".

### F-4 LOW (test honesty): the r12 backstop property test is weaker than the property it names
- test_ooh2_pii1_cluster_rule.py `test_r12_backstop_property_on_every_form` draws 20,000 strings from a 49-item pool and checks six forms; it does not check `screen_form(_norm(decoded))`-type compositions or non-decimal digits. It passes, and my wider fuzz (above) finds no violation, so this is not a defect of the code. Falsifier: a test over the forms in fuzz13.py passing at this SHA.

### Info (not findings)
- Known limit, founder decision: URL escapes other than %40 and JS/CSS escapes (OOH2-R12-SEC-S2). Not blocked on.
- False positives are accepted by decision (OOH2-PII-1-FP).
- 5-pass cap in `screen_form` is a bound, not a proof (no input found that needs more).
- The 4000-character limit means the large-input timings are above any reachable size.

## NOT RUN
No HTTP/end-to-end witness (function level; call chain read); no mutation run; no Python 3.14; no security exploitation beyond the probes; other services not reviewed; ledger/store/integrity code not reviewed (outside the delta); no check that a consumer decodes entities; docs/aegis history files not reviewed; no exhaustive search for an input that defeats the 5-pass cap or the 16-pass cap beyond the random and structured fuzz stated.

## Falsifiers for the verdict
SOURCE_GREEN is wrong if any of the following is shown: a kept text from `screen_free_text` holding 9+ digits without an ASCII letter in one of the caller's forms (fuzz13.py is the harness); a code path returning kept text without the backstop; a pytest failure in the frozen copy on either interpreter; or Ω rules that F-1 is HIGH (it is then BLOCK).
