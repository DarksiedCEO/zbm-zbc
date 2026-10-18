# AEGIS SECURITY gate — omega12

- repoIdentity: DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2)
- candidateSha: 1a904983d6e064b912bbd95a7afa1668da6c50c9
- treeOid: 83356dd062ff3e668358785f5e6d963cdb3950c2 (`git rev-parse 1a90498^{tree}`; my `git archive 1a90498` copy at omega12/security/wt, docs/ included, is `diff -r` identical to the frozen copy review-1a90498 apart from that copy's own `.git` directory)
- scope: OOH2-PII-1 after the founder decisions (`git diff f5ee168 1a90498 -- services`: pii.py, service.py, two test files) in the three screened fields (unit vendor_text redacted; face text attribute and client_approval_doc_ref refused); the request hash; S11-1/S11-2 re-run; gitleaks; personal data in docs/ and claude/. F4 out of scope.
- author: AG-AEGIS-SECURITY (fresh context, read-only on the candidate). Experiments ran only in my own archive copies (omega12/security/wt = 1a90498, omega12/security/f5ee168 = `git archive f5ee168 services/ooh-py` for regression comparison), with in-process TestClient, FakeLedger and temp data dirs under omega12/security/poc/tmp. Nothing live. Public test values only (4111…1111, 4242…, 5555…4444, 4000…0002, 123-45-6789, 219-09-9999, 078-05-1120). Every PoC ran on venv3.12 (3.12.14, Unicode 15.0) and venv3.13 (3.13.9, Unicode 15.1); outputs identical except where noted (category labels of the 15.1 code points; random unit ids).

Paths: `$O` = <scratchpad>/aegis/omega12/security; `$S` = <scratchpad>.

## Verdict: SECURITY_GREEN (for the scoped claim). No CRITICAL or HIGH. Two MEDIUM (one a regression), three LOW, two open LOW carried. Non-blocking.

- S11-1 (look-alike letters) **CLOSED**. S11-2 (HTML entities) **CLOSED** for every real entity form tested, with a LOW residue (S12-3).
- The request hash covers only the kept text. Different secrets of one shape give one hash. Replay and 409 are correct. The kept text and the hash are identical between 3.12 and 3.13 for all 1,112,064 code points.
- New:
  - **S12-1 (MEDIUM, regression)**: 69 non-decimal Unicode digits (dingbat/double-circled ❶ ➀ ⓵, Ethiopic ፩, Kharoshthi, Rumi, Brahmi) no longer count as digits. They were redacted at f5ee168 and are stored in clear at 1a90498.
  - **S12-2 (MEDIUM)**: URL, JS and CSS escapes whose hex contains A–F (`%2D`, `%2E`, `%2F`, `%3A`, `%C2%A0`, `-`, `\x2d`, `\2d`) end a cluster, so `4111%2D1111%2D1111%2D1111` is stored.
  - S12-3 (LOW): HTML entity residue: ≥3 levels of `&amp;` nesting, plus non-entities such as `&NBSP;`, `&ndash` and `&#x2d1111`.
  - S12-4 (LOW/INFO): 1130 code points whose NFKC form is an ASCII letter (Ｘ, ℹ, ⅰ, Ⅰ) end a cluster. They are stored as ASCII letters, which follows the rule.
- Still open: S11-3 (LOW), S11-4 (LOW). S11-5 (LOW) is partly changed: Cn characters now answer 409.
- gitleaks: no leaks (git 84a5377..1a90498, 38 commits; dir on the frozen copy). No personal data introduced.

---

## Item 1 — attack on the three fields (pii.py:251-264 `number_cluster_spans`, :301-323 `screen_free_text`)

Executed:
- `$O/poc/pii_attack.py` → `$O/poc-pii-attack-3.12.txt` / `-3.13.txt` (226 rows, pii level).
- The same script on f5ee168 → `poc-pii-attack-AT-f5ee168-*.txt`, diff in `diff-attack-f5ee168-vs-1a90498-3.12.txt`.
- `$O/poc/digit_population.py` → `poc-digit-population.txt` (all 798 non-ASCII `isdigit` code points, at both SHAs and on both Pythons).
- `$O/poc/rule_routes12.py` → `poc-rule-routes-*.txt`. This is route level: the exact form, its NFKC form and its JSON-escaped form are searched in the log and pending files (every line re-dumped ensure_ascii=False), in the ledger events, in /audit/export and in the unit view.

| class | outcome | reaches a persisted byte? |
|---|---|---|
| look-alike Unicode letters ʼ ː ˈ ʹ ـ ー ｰ ǀ 々, Greek/Cyrillic/CJK/Hebrew/Arabic letters, any grouping | redacted / 422 | no (S11-1 closed) |
| HTML entities: named (`&nbsp;` `&ndash;` `&hyphen;` `&dash;` `&colon;` `&sol;` `&MediumSpace;` `&NoBreak;` `&zwnj;` `&shy;` `&lrm;`), decimal/hex with and without `;`, upper-case `&#X2D;`, zero-padded, `&amp;nbsp;`, `&amp;amp;nbsp;`, `&#38;nbsp;`, `&sup1;`, `&half;`, fullwidth-digit entities | redacted / 422 | no (S11-2 closed) |
| `&amp;amp;amp;nbsp;` (3+ levels), `&NBSP;` `&Nbsp;` `& nbsp;` (not entities), `&ndash` without `;` (not decoded without `;`), `&#x2d1111` (greedy hex) | KEPT | yes → **S12-3** |
| URL percent-encoding: `%20`, `%09`, `%25%32%44`, `%34%31…` | redacted | no |
| URL percent-encoding with a hex letter: `%2D` `%2d` `%2E` `%2F` `%3A` `%2C` `%7C` `%5F` `%C2%A0`; `%E2%80%93` leaves `4111%E` (4 digits) | KEPT | yes → **S12-2** |
| JS `-` `\x2d` ` `, CSS `\2d` `\2D`; whole number as `4…`/`\x34…` | KEPT | yes → **S12-2** (digits encoded as escapes: deliberate encoding, the same class as S11-3) |
| CSS `\20 ` `\55 `, CSS digits `\34\31…` | redacted | no |
| bidi/format controls U+202A–202E, U+2066–2069, U+200E/F, U+061C as separators; RLO-reversed card | redacted (Cf removed) | no |
| NFKC to an ASCII letter: Ｘ ｘ ℹ ⅰ Ⅰ ⅼ ℓ Ⓐ ⓐ 𝐗 𝔸 ᵢ ˣ ᵃ ™ № ㎗ 🄰 ㎡ | KEPT, stored as ASCII (`123I45I6789`) | yes → S12-4 (by rule) |
| ASCII letters (`x`, `o`, `O`, `l`, `I`, ` and `) | KEPT | yes (by design) |
| non-decimal digits ❶ ➀ ➊ ⓵ ፩ 𐩀 (Numeric_Type=Digit, not Nd, no NFKC digit) | **KEPT** (f5ee168: redacted) | yes → **S12-1** |
| `〇`, Suzhou `〡`, counting rods (not `isdigit`) | KEPT | yes → S11-3 (open) |
| circled ①, superscripts, fullwidth, Arabic-Indic | redacted | no |
| unassigned (Cn) U+0378, U+2FFC, U+31EF, U+2EBF0, U+E0080 as separators; private use U+10FFFD | redacted (Cn joins the cluster) | no |
| split across two requests (`ssn part 12345` + `ssn part 6789`) | each stored | yes → S11-4 (open) |

Route results (`poc-rule-routes-3.12.txt`, identical on 3.13):
- For every KEPT form, vendor_text gives HTTP 201 and `reached=log/pending,export,unit-view`, and the face attribute `location_description` gives HTTP 200, `reached=log/pending,export`.
- For every redacted form, the stored text is `[redacted]` (`reached=-`) and the face attribute gets 422.
- client_approval_doc_ref: its pattern `^[A-Za-z0-9._:-]{1,100}$` refuses `%`, `&`, `\` and every non-ASCII form with 422 before the screen (`url %2D card` → 422 string_pattern_mismatch). Only ASCII-letter-separated forms pass: `4111x1111x1111x1111`, `123x45x6789`, `123D45D6789` → 200, `reached=log/pending,ledger,export` (by design). This field reaches the ledger, and vendor_text and attributes do not.
- The cost at the 4000-character cap is ≤ 0.017 s on 3.13 (`poc-cost12.txt`: nested `&amp;`, entity floods, 4000 Cn, 4000 ❶, `%2D1` × 1333).

### S12-1 — MEDIUM (non-blocking; regression): non-decimal Unicode digits stopped counting as digits. Class: SECRET_LEAK
- Path: services/ooh-py/src/pii.py:261 `elif "0" <= ch <= "9":`. At f5ee168 the same branch was `elif ch.isdigit():`. strip_format (pii.py:103) maps only category Nd to ASCII, so No-category digits without an NFKC digit decomposition reach the counter unchanged and are now skipped. The card/SSN label rules use `[0-9]` (pii.py:138, :199) and do not cover them either. No test pins this class (`grep` of tests/ for U+2776/2780/24F5/1369: 0 hits).
- Commit 1a90498 announces only the letter change ("only ASCII letters end a cluster"). The digit narrowing comes with it unannounced.
- Population (`poc-digit-population.txt`): out of 798 non-ASCII `isdigit` code points, 9 in a row are KEPT for **69 at 1a90498 versus 0 at f5ee168**, on both 3.12 and 3.13. The 69 are:
  - U+1369–1371 Ethiopic
  - U+19DA
  - U+24F5–24FD and U+24FF (double/negative circled)
  - U+2776–277E, U+2780–2788, U+278A–2792 (dingbats)
  - U+10A40–10A43 Kharoshthi
  - U+10E60–10E68 Rumi
  - U+11052–1105A Brahmi
  All Nd code points are still redacted (0 kept).
- PoC (executed): `$S/venv3.12/bin/python -I $O/poc/rule_routes12.py $O/wt/services/ooh-py`. vendor_text `contact ❹❶❶❶ ❶❶❶❶ ❶❶❶❶ ❶❶❶❶ please` gives HTTP 201, `reached=log/pending,export,unit-view`. The same holds for `❶❷❸-❹❺-❻❼❽❾`, the Ethiopic SSN `፩፪፫-፬፭-፮፯፰፱` and the mixed `4111 1111 ❶❶❶❶ ❶❶❶❶`. The face attribute gives HTTP 200, `reached=log/pending,export`. At pii level (`poc-pii-attack-3.12.txt` §F) these are `KEPT kinds=- isdigitKept=16`. The omega11 script itself, re-run on 1a90498 (`rerun11-rule-attack-3.12.txt` vs omega11 `poc-rule-attack-3.12.txt`), turns "double circled / dingbat negative circled: refused/redacted" into "stored".
- Why MEDIUM, not HIGH: it needs a deliberate encoding (no keyboard or paste path produces a card in dingbat digits). Its sibling S11-3 (〇/Suzhou) was rated LOW. It is rated higher than S11-3 because it is a regression of a control that was verified at f5ee168, and because the stored glyphs read directly as the number.
- Reproducer: the commands above. Also `$S/venv3.12/bin/python -I $O/poc/digit_population.py $O/wt/services/ooh-py/src`.
- Falsifier: count `ch.isdigit()` (or `unicodedata.digit(ch, None) is not None`) at pii.py:261, or map every `unicodedata.digit` character to ASCII in strip_format. `digit_population.py` should then print `KEPT: 0` and the four route rows should print `[redacted]` / 422.

### S12-2 — MEDIUM (non-blocking): percent-encoding and JS/CSS escapes with hex letters end a cluster. Class: SECRET_LEAK
- Path: pii.py:119 (`_norm` decodes `%40` only), pii.py:257 (the A–F hex letters of the escape are ASCII letters and end the cluster), pii.py:138/:199 (the label rules break on them too).
- PoC (executed, `poc-rule-routes-3.12.txt`):
  - vendor_text `contact 4111%2D1111%2D1111%2D1111 please`, `123%2D45%2D6789`, `123%C2%A045%C2%A06789`, `4111-1111-1111-1111` (JS) and `123\2d 45\2d 6789` (CSS) each give HTTP 201, `reached=log/pending,export,unit-view`, and the stored request_sha equals sha(raw body), so the text is kept as typed.
  - The face attribute gives HTTP 200, `reached=log/pending,export`.
  - At pii level (§A, §B), `%2E %2F %3A %2C %7C %5F`, `\x2d` and ` ` are also KEPT. All 16 card digits and all 9 SSN digits are legible in the stored text.
- Why MEDIUM: under the founder rule as written, ASCII letters end a cluster, so these forms conform. They are plausible, though, when a query string is pasted (`?ref=4111%2D…`), which is the same reasoning that rated S11-2 MEDIUM. `%20` and `%09` (digits only) are caught. client_approval_doc_ref is not affected (its pattern refuses `%` and `\`).
- Falsifier: URL-decode (`urllib.parse.unquote`, repeated like the HTML loop) and decode `\uXXXX`/`\xXX`/CSS `\HH` before the cluster count, screening both forms. The rows above should then print `[redacted]` / 422.

### S12-3 — LOW: HTML entity residue. Class: SECRET_LEAK
- Path: pii.py:313-318. The decode loop stops after 3 `html.unescape` passes, so `4111&amp;amp;amp;nbsp;1111…` keeps `nbsp` letters and the number is KEPT (route: HTTP 201/200, reached log/pending, export, unit view). `&NBSP;`, `&Nbsp;`, `& nbsp;`, `&ndash` (no `;`) and `&#x2d1111…` are not entities to html.unescape or to a browser either, so they are letter-separated text and KEPT by the rule.
- Severity LOW: these need three levels of nested escaping, or they are not entities at all, and the letters are visible.
- Falsifier: loop `html.unescape` until it reaches a fixed point (it is bounded by the length). The `&amp;amp;amp;nbsp;` rows should then print `[redacted]`.

### S12-4 — LOW/INFO: NFKC letterlike characters end a cluster. Class: UNKNOWN (by rule)
- The founder asked whether characters such as Ｘ, ℹ and ⅰ end a cluster. They do: the cluster rule runs on the NFKC text (pii.py:308). 1130 non-ASCII code points have an ASCII letter in their NFKC form, and `1234<c>56789` is KEPT for all 1130 (`poc-extra12.txt`). The stored bytes are ASCII (`123I45I6789`, `123x45x6789`), so what is persisted is identical to a typed ASCII-letter separator, which is accepted. Note: U+2160 Ⅰ and U+217C ⅼ render like `|`, but their stored form shows `I`/`l`. No falsifier is needed unless Ω rules that letters must come from the raw text.

### Carried
- S11-3 LOW, **OPEN**: `4〇〇〇…2` and Suzhou are KEPT (route 201/200, reached log, export, unit view).
- S11-4 LOW, **OPEN**: split requests are stored in parts.
- S11-5 LOW, **partly changed**:
  - `Face A  west`, `Face A west`, the fullwidth form and the ZWSP form still replay under one request_id (201 ×4, `poc-extra12.txt`).
  - Cn characters are now kept, so `faces ͸west` vs `faces west` answers 409 (`poc-hash12-*`).

### Bidi
All explicit bidi controls are Cf and are removed before the count and from the stored text (§D rows redacted). Implicit reordering with strong RTL letters cannot hide a separator, because RTL letters join the cluster (Hebrew row redacted). A display trick that moves an ASCII letter visually out from between digit groups was **not** constructed: THEORETICAL, NOT RUN.

## Item 2 — the request hash (service.py:70-103)
- **Kept text only**: `_screened` replaces every `vendor_text` with `screen_free_text(x)[0]` (service.py:77).
  - `$O/poc/hash12.py` → `poc-hash12-*.txt`: 16 shapes (10 card, 6 SSN), covering the new decisions (&nbsp;, &amp;nbsp;, U+02BC 2-digit, U+30FC, Cn U+0378, U+2FFC, hex entities, &zwnj;, %20, Hebrew, &ndash;, &#45;), × 4 PANs or 3 SSNs.
  - For every body, request_sha == sha(body with kept text), != sha(raw body), and the kept text holds no digit. **Failures: 0.**
  - Route level (`poc-rule-routes-*`): for every redacted row, the stored request_sha == sha(kept-text body) is True and == sha(raw body) is False.
- **No recovery by search**: within each shape, different secrets give **1 distinct hash** (kept text `contact [redacted] now`), so a guess has no oracle.
- **Unicode-version stability**: `$O/poc/unicode_sweep.py` ran every code point except surrogates (1,112,064) through 3 templates (`faces west <c> lit`, `4111<c>1111<c>1111<c>1111`, `1234<c>5678x`), recording the kept texts and the request_sha. 3.12 and 3.13 outputs are **byte-identical** (sha256 4436caaf1b736a6c8e7ea885e521419483911e825b463d48590c79ec145990b1 for both; `unicode-sweep-diff.txt` is empty; outputs under poc/tmp/sweep-*.txt). This covers the five 15.1 code points U+2FFC–2FFF and U+31EF and the 622 CJK Ext-I code points, which are Cn in 3.12 and assigned in 3.13.
- **Idempotency** (`poc-hash12-*`):

  | case | result |
  |---|---|
  | same body twice | 201/201, same unit |
  | card A vs card B, both redacted | 201/201 replay (by design, ADR 0022) |
  | ʼ-SSN vs literal `[redacted]` | 201/201 replay (by design) |
  | faces west vs faces east | 409 REQUEST_ID_REUSED |
  | Cn kept vs removed | 409 |
  | `&amp;` vs `&` | 409 |
  | dingbat card (kept) vs ASCII card (redacted) | 409 |
  | redacted text with a different flight_end | 409 |
  | replay after restart | 201, same unit_id |
- Cache: memory only, as at omega11 (service.py:84-103, unchanged except the docstring).

## Item 3 — S11-1 and S11-2 PoCs re-run (omega11 scripts copied to `$O/rerun11/`, run on 1a90498, both Pythons identical)
| finding | status | evidence |
|---|---|---|
| S11-1 look-alike letters (MEDIUM) | **CLOSED** | `rerun11-rule-routes-*`: `1234ʼ56789`, `12ー345ー6789` and `1234ـ56789` give 201 `reached=-` (stored `[redacted]`, sha != raw) and face attribute 422. `rerun11-card-short-groups-*`: all 14 rows redacted, including 1- and 2-digit groups with ʼ ː ー ـ ǀ. `rerun11-rule-attack-*`: all 23 look-alike/other-script rows that were KEPT at f5ee168 are now refused/redacted |
| S11-2 HTML entities (MEDIUM) | **CLOSED** (residue S12-3, LOW) | `rerun11-rule-routes-*`: `&nbsp;` card, `&ndash;` SSN and hex-entity card give 201 `reached=-` and face 422. `rerun11-rule-attack-*`: the 4 entity rows go from stored/KEPT to refused/redacted |
| S11-3 | OPEN, LOW | `rerun11-rule-routes-*` ideographic zero and Suzhou: reached log, export, unit view |
| S11-4 | OPEN, LOW | the same file, split-request rows |
| regression seen in the same re-run | **S12-1** | `rerun11-rule-attack-*` lines 93-96: double-circled/dingbat go from redacted to stored |
| ASCII-letter rows | KEPT, by design | 12 rows, unchanged |

Test suite in my copy (`$O/wt/services/ooh-py`, docs/ present, one pytest at a time, `pgrep -f pytest` < 4 checked before each): venv3.12 **1190 passed, 0 failed** (`pytest-suite-3.12.txt`, junit `pytest-3.12.xml`); venv3.13 **1190 passed, 0 failed** (`pytest-suite-3.13.txt`). An earlier 3.12 run with `-qq` lost its summary line to `tail` and was discarded.

## Item 4 — gitleaks 8.30.1 and personal data
- Candidate `.gitleaks.toml` sha256 40410abac2315fc3f0ea7c44676236d1ded64ecae0acf005ed0a1bfb7478e43d, the same as the frozen copy's and as omega11's.
- `gitleaks git --log-opts=84a5377..1a90498` (38 commits, 32.56 MB): **no leaks**, exit 0, `gitleaks-git.json` = `[]`.
- `gitleaks dir` on the frozen copy review-1a90498 (50.99 MB): **no leaks**, exit 0, `gitleaks-dir.json` = `[]`.
- Personal data at 1a90498 (`git grep`):
  - `<USER>` and `thereal<USER>`: 0 files.
  - `/Users/<name>` in docs/ or claude/: 0 (except `/Users/runner`).
  - Scratchpad paths in docs/aegis/ooh-wave2/** are written as `/private/tmp/claude-501/-<HOME>-…`.
  - E-mails added in f5ee168..1a90498 under docs/ and claude/ are fixtures only: example.com, b.coa, b.example, acom123.co, y.com, `@pytest`, and the fictional `kid.name@gmail.com` quoted from ADR 0007 WR-F005.
- INFO: `<OWNER-EMAIL>` appears only in docs/aegis/round-26b/provenance.md:49-50. It is the public commit identity and predates the range, as at omega11.

## Boundaries (denominator 9 declared / 9 tested)
| boundary | tested | method |
|---|---|---|
| unit vendor_text: 9+ card/SSN digits not stored in clear | yes | pii_attack (226 rows), digit_population (798 code points × 2 SHAs), rule_routes12 (24 forms) → S12-1, S12-2, S12-3 |
| face text attribute refused | yes | route level, 24 forms |
| client_approval_doc_ref refused | yes | route level, 9 forms (pattern + screen) |
| log/pending, ledger payloads/summaries, /audit/export, unit view | yes | searched for the exact form, its NFKC form and its JSON-escaped form in every route row |
| request_sha over the kept text only; no recovery | yes | hash12 (58 bodies, 16 shapes), route rows |
| 3.12 vs 3.13 hash stability | yes | unicode_sweep over all code points, byte-identical |
| replay / 409 / restart | yes | hash12, 9 cases |
| S11-1 / S11-2 re-run | yes | omega11 scripts re-run |
| secrets and personal data in history and tree | yes | gitleaks git + dir, git grep |

## NOT RUN
- No implicit-bidi display construction (THEORETICAL; see Bidi above).
- No Python 3.14 / Unicode 16 check: only the 3.12 and 3.13 venvs exist. Unicode 16 adds Nd scripts (e.g., Garay) that 3.12 sees as Cn, so the kept text could differ under a 3.14 build.
- No hash search or brute force, deliberately. Independence was shown directly instead.
- F4 (13 other Ref fields): out of scope, not exercised.
- No production, live tenant or third-party target was touched.
