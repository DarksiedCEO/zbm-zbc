# AEGIS SOURCE gate (Gate 1) - ooh-py delta 91ea9a1..0f5fbe3

- candidateSha: 0f5fbe33642a62068239f4df83edd8602730fd8e
- treeOid: ffc3bd9caa09782a69e91d7466ce1b30eef16516
- repo: DarksiedCEO/zbm-zbc, branch ooh-dept-15-wave2; frozen copy scratchpad/review-0f5fbe3 (HEAD and tree identical at start and end; `git status --porcelain` empty at end)
- Author: AEGIS-SOURCE (AG-AEGIS-SOURCE v1.0.0), fresh context, read-only on the candidate
- Verdict: **SOURCE_GREEN** (scoped to the delta; 0 CRITICAL, 0 HIGH; 2 MEDIUM, 4 LOW, none blocking). Ω note: M-1 is a residual of the F2 class (see below) and Ω/security may weigh it higher.
- Raw outputs: scratchpad/aegis/omega10/source/ (pytest-3.12.txt, pytest-3.13.txt, diff_names_a_card.py, diff2.py, diff-out-*.txt, diff2-out-*.txt, sweep.py, sweep-out.txt, probe.py, probe-out.txt, old/svc_campaigns_99569bd.py)

## Denominator (inclusion rule: every file in `git diff 91ea9a1 0f5fbe3 -- services`, plus the doc hunks in `git diff 91ea9a1 0f5fbe3 -- docs`)
Required 9 files (6 src+test files in services, 3 doc files); inspected 9. pii.py read in full; service.py request_sha/_screened/_idem/_req; store.py _read_at_start/_close_quietly/write open and surrounding; svc_campaigns.observed, names_a_card, client_approve; svc_inventory.parse_value; new test file in full; ADR section and OPEN.md OOH2-R9-* rows and OOH2-PII-1 row; test-counts row.

## Areas
| path | class | evidence |
|---|---|---|
| src/pii.py | REAL | strip_format/_norm/_SSN*/_DIGIT_RUN wired into has_personal_data/redact/card_spans; probes below |
| src/service.py (request_sha, _screened) | REAL | used by _idem (:273) and _req (:285); verified differing/equal hashes below |
| src/store.py (_read_at_start, _close_quietly, O_NONBLOCK) | REAL | called from RecordLog.__init__; FIFO/symlink/device/dir tests pass |
| src/svc_campaigns.py (observed) | REAL | strip_format applied before isprintable and the screen |
| tests/test_aegis_r9_pii_hardening.py | REAL (one tautological assert, L-2) | |
| tests/test_ooh2_pii1_card_ssn.py | REAL | test narrowed to the cases still true; removal of "A123-45-6789"/"SSN 12345678" glue case matches the new SSN rule |
| docs ADR 0022 / OPEN.md / test-counts.md | see M-2, L-1, L-3 | |

## Required checks
1. **Suite**: `python -m pytest -q tests` in the frozen copy: **924 passed, 1 warning on 3.12 (92.74 s) and 924 passed, 1 warning on 3.13 (95.11 s)**, 0 failed/skipped. Matches test-counts.md `python:ooh-py | 924`.
2. **names_a_card vs 99569bd (differential)**: old function from `git show 99569bd:.../svc_campaigns.py` vs candidate, 100,012 inputs each run (60,000 random over digits/separators/letters/NBSP/Arabic-Indic/fullwidth; 40,000 Luhn-valid cards with random IIN/length/grouping/trailing group/date prefix; 12 hand cases), then repeated with the alphabet widened by dotless i, dotted I, long s, Kelvin, sharp s, Dz-title, ZWSP, soft hyphen, Arabic-Indic zero and Devanagari one: **0 differences on 3.12 and 3.13**. names_a_card itself is unchanged in this delta; only `_DIGIT_RUN` (no re.I) differs from 91ea9a1, and card_spans does not call strip_format.
3. **Predecessor S-1** (non-ASCII-script digits): closed. Arabic-Indic, Devanagari, fullwidth, math-bold card strings are all redacted (probe-out.txt); `SSN: ١٢٣-٤٥-٦٧٨٩` redacted. **S-3** (SSN dot/underscore/slash/space/glue): `123.45.6789`, `123_45_6789`, `123/45/6789`, `123:45:6789`, `x123-45-6789`, `SSN 123 45 6789`, `TIN 123456789`, `SSN=123456789`, `social-security 123 45 6789` all redacted. Still passing: `123 - 45 - 6789`, `123  45  6789` (two spaces), `SSN`+25 spaces+digits (beyond 20-char window), bare `123456789`, `1 2 3 4 …` (see L-1).
4. **Three fields**: unit vendor_text -> observed(): strip_format, has_personal_data, redact; face text attribute -> parse_value -> personal_data_in (refuses, 422); client_approval_doc_ref -> client_approve:864 before `_idem`/`_commit`, type Ref is ASCII-only. Reachable bypass: M-1.
5. **`_screened` / hash collisions**: ids and references are hashed as sent (face_id differs -> hash differs, test). Collision is possible for vendor_text (M-2).

## Findings

### M-1 MEDIUM (non-blocking, Ω to weigh) - STUB_IN_RUNTIME_PATH/bypass: invisible non-Cf characters inside a digit group still hide a card or SSN
- Claim: pii.strip_format docstring and ADR "Hidden and other-script digits (F2...)": "removes zero-width and every other format character ... (they hide nothing once gone)"; OPEN.md OOH2-R9-SEC-F2 "fixed". Path: services/ooh-py/src/pii.py strip_format (only `category == "Cf"` and `_ZW` are removed); card rule pii.py `_card_from` (len(g) >= 3 on both ends, returns False at first short group); SSN rule `_SSN` needs exactly \d{3}/\d{2}/\d{4}.
- Evidence (probe-out.txt, sweep-out.txt): one invisible character of category Mn/Lo/So, e.g. U+034F, U+FE0F, U+3164, U+2800, U+17B5, U+E0100, U+180B, U+115F, inside a group leaves `observed(...)["personal_data_redacted"] == False` and the text stored verbatim: `"4111 11<U+034F>11 1111 1111"`, `"4111 1111 1111 11<U+FE0F>11"`, `"123-4<U+3164>5-6789"`; `si.personal_data_in` (face text attribute screen) also returns False for all of them (attr_screen=False). client_approval_doc_ref is not reachable this way (ASCII REF). The sweep over all assigned code points shows every printable character does the same (that is the accepted S-2 limit "groups shorter than three"), so the new part is only that the character is invisible to a reader, which is the F2 class the round claims to close.
- Reproducer: `venv3.12/bin/python -I scratchpad/aegis/omega10/source/probe.py` (first section) or from services/ooh-py/src: `import svc_campaigns as sc; sc.observed("123-4͏5-6789")`.
- Falsifier: personal_data_redacted True for those strings (e.g. Mn/Me/Lo-filler/blank-symbol characters removed or treated by the digit rule). Mitigating: same effect already accepted for visible short groups; threat model in the docs is a preparer's accident, not an adversary. Not fixed by me.

### M-2 MEDIUM (non-blocking) - DOC_DRIFT / collision: "two different requests never hash alike" is false for vendor_text
- Claim: ADR 0022 "The request hash (security F1)": "Ids and references are hashed as sent, so two different requests never hash alike"; service.py:_screened docstring same idea. Path: service.py:68-76 (`_screened` replaces vendor_text by `pii.redact(x)`), :81-85.
- Evidence (probe-out.txt): bodies differing only in vendor_text `pay 4111 1111 1111 1111` vs `pay 5555 5555 5555 4444` -> same hash True; `pay SSN 123-45-6789` vs `pay SSN 987-65-4321` -> same hash True; `pay 4111 …` vs literal `pay [redacted]` -> same hash True. Effect: with one request_id, a second request carrying a different card or SSN is answered as a replay of the first (`_idem`, service.py:273) instead of 409 REQUEST_ID_REUSED. No stored data is wrong (the redacted form is what is stored either way), so the behaviour is defensible, but the doc states otherwise. Also vendor_text carrying an invisible-char form (M-1) hashes raw (probe: `invisible-form vendor_text hashed raw: True`), so the F1 property holds only for forms the screen detects.
- Reproducer: probe.py last section. Falsifier: distinct hashes for those pairs, or the ADR wording limited to "except vendor text whose redacted forms are equal".

### L-1 LOW - DOC_DRIFT: pii.py module docstring not updated
- pii.py:1-31 still says "zero-width characters are removed", SSN "``ddd-dd-dddd`` with a hyphen or another dash, not glued to a word or a dash", words "SSN, social security (number), ITIN or tax id" and "nine-digit run with spaces and no such word is not seen". Code (pii.py `_SSEP`, `_SSN`, `_SSN_WORDS`) accepts dot/underscore/colon/slash/space, letters may touch, adds TIN, and does not strip only zero-width. Undocumented residual SSN limits: single-character separators only (`123  45  6789`, `123 - 45 - 6789` pass). Reproducer: probe-out.txt rows; falsifier: those refused.

### L-2 LOW - tautological assertion
- tests/test_aegis_r9_pii_hardening.py:59 `assert len(h.svc.log) == len(h.svc.log)` can never fail; it was meant to check that the replay appended nothing. Reproducer: `grep -n "len(h.svc.log) == len(h.svc.log)"`. Falsifier: n/a (comparison of a value with itself).

### L-3 LOW - dead code / misleading message
- `_ZW` removal in strip_format (pii.py) is now redundant: all six characters are category Cf and the next line strips Cf. `_read_at_start` maps every OSError of open other than ENOENT (e.g. EACCES, EMFILE) to "it must be a regular file, not a link" (store.py `_read_at_start`), a misleading reason; it still fails closed.

### L-4 LOW - OPEN.md/ADR status
- OPEN.md rows OOH2-R9-* say "fixed in the commit that adds this text" while the ADR heading says "at 91ea9a1"; consistent and marked "open until AEGIS confirms". OOH2-PII-1 still open with purge/re-key "not built"; no claim of closure. 13 other reference fields unscreened is stated (OOH2-R9-SEC-F4). Observed side effect not documented: vendor text is now stored with digits of other scripts converted to ASCII and format characters removed (altered stored data; ADR says "stored in that form").

No finding for: FAKE_SUCCESS, HARDCODED_RESULT, MOCK_ONLY, DUPLICATE_OWNERSHIP (card rule still only in pii.card_spans), UNSAFE_DEFAULT in store.py (start-up now fails closed on symlink/FIFO/device/directory; close error on a read-only fd ignored only there).

## NOT RUN / not inspected
- Mutation, security exploitation, reliability injection, runtime witnessing, provenance (peer gates); real FIFO/device behaviour on Linux (run on macOS); Python 3.14.
- HTTP-level end-to-end for M-1 (function-level via observed() and personal_data_in only); the 13 unscreened reference fields; other services.
- Review of the 91ea9a1..0f5fbe3 delta only; earlier code assumed per prior gates.

blocking: false
