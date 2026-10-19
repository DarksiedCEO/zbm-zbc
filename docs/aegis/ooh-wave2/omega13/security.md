# AEGIS SECURITY gate — omega13

- repoIdentity: DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2)
- candidateSha: cb708b924bb01aa5543b832573a8fa56bd041b80
- treeOid: eba95097622438b226169c88910c81cc9a999212. Checked with `git rev-parse cb708b9^{tree}`. My `git archive cb708b9` copy, omega13/security/wt (docs/ included), is `diff -r` identical to the frozen copy review-cb708b9 except for that copy's own `.git` directory.
- scope: OOH2-PII-1 after the r12 fixes, in the three screened fields: unit vendor_text (redacted), and the face text attribute and client_approval_doc_ref (both refused). The code delta is `git diff 1a90498 cb708b9 -- services`: pii.py plus two test files; service.py is unchanged. Also in scope: the request hash; the S12-1, S12-2 and S12-3 re-runs; gitleaks; personal data in docs/ and claude/. F4 (the 13 other Ref fields) is out of scope.
- author: AG-AEGIS-SECURITY (fresh context, independent of the builder, read-only on the candidate). Experiments ran only in my own archive copies:
  - omega13/security/wt = cb708b9.
  - omega12/security/wt = 1a90498, the predecessor's archive, used read-only for regression comparison.
  - In-process TestClient, FakeLedger, and temp data dirs under omega13/security/poc/tmp.

  Nothing live was touched. Only public test values were used (4111…1111, 4242…, 5555…4444, 4000…0002, 123-45-6789). Every PoC ran on venv3.12 (3.12.14, Unicode 15.0) and venv3.13 (3.13.9, Unicode 15.1), unless a row says otherwise.

Paths: `$O` = <scratchpad>/aegis/omega13/security, `$S` = <scratchpad>. Scripts are in `$O/poc/`, outputs in `$O/`.

## Verdict: SECURITY_GREEN (for the scoped claim). No CRITICAL or HIGH. Non-blocking.

| id | severity | status |
|---|---|---|
| S12-1 non-decimal digits | MEDIUM (r12) | **CLOSED** |
| S12-2 URL / JS / CSS escapes | MEDIUM | **documented limit, confirmed to be exactly that class** (founder decision; not blocking) |
| S12-3 entity residue | LOW (r12) | **nesting CLOSED**. Non-entity residue (`&NBSP;`, `&ndash` without `;`, `&#x2d1111`) still kept, within the rule |
| S12-4 NFKC letters | LOW (r12) | unchanged, within the rule (OPEN.md OOH2-R12-LOW) |
| S13-1 entity behind a fullwidth or small ampersand or a hidden character | LOW (new, already present at 1a90498) | open |
| S13-2 Unicode 16 digits (Cn in 3.12 and 3.13) | LOW (new) | open |
| S13-3 e-mail with a non-ASCII TLD | LOW (new, already present at 1a90498) | open |
| S13-4 screen cost ×6.6–12 under the service lock | LOW (new, regression) | open |
| S11-3, S11-4 | LOW | still open (documented OOH2-R11-SEC-L) |

---

## Item 1 — S12-1, S12-2, S12-3 re-run (omega12 scripts copied unchanged to `$O/poc/`, run on cb708b9)

Commands:
- `$S/venv3.1x/bin/python -I $O/poc/pii_attack.py $O/wt/services/ooh-py/src` → `poc-pii-attack-3.1x.txt`. The two Pythons differ only in the category label printed for U+2FFC, U+31EF and U+2EBF0.
- `digit_population.py` → `poc-digit-population-3.1x.txt`.
- `rule_routes12.py $O/wt/services/ooh-py` → `poc-rule-routes-3.1x.txt`. Identical on both Pythons apart from the venv path in a deprecation warning.
- Regression check against r12 (`diff-attack-1a90498-vs-cb708b9-3.12.txt`): **19 rows went from KEPT to redacted, and 0 rows became newly KEPT.**

### S12-1 (non-decimal digits) — **CLOSED**
- `poc-digit-population-*`: 798 non-ASCII `isdigit` code points, of which **0** are kept as a 9-run (r12: 69). Nd code points kept: 0.
- `poc-attack13-*` §D: all 798 digit-valued code points (No 128, Nd 670) give 0 kept as a 9-run, 0 kept as a 3-2-4 SSN, and 0 kept when 8 ASCII digits and 8 of the code point are mixed.
- Route (`poc-rule-routes-*`):
  - Dingbat card, dingbat SSN, Ethiopic SSN, and 8 ASCII + 8 dingbat: vendor_text 201 `reached=-`, stored request_sha == sha(kept body) and != sha(raw body).
  - The face attribute gives **422**.
- Code: pii.py:113-114 (`unicodedata.digit` for every non-ASCII character).

### S12-2 (URL / JS / CSS escapes) — **exactly the documented limit**
- Kept escape forms (`poc-pii-attack-*` §A/§B), all in the founder's class:
  - URL `%XX` with a hex letter A–F: `%2D %2d %2E %2F %3A %2C %7C %5F %C2%A0`
  - the IIS form `%uXXXX`
  - JS `\uXXXX` and `\xXX`, including ` `
  - CSS `\2d`, `\2D`
- The same file also redacts:
  - URL escapes made only of digits: `%20`, `%09`, `%25%32%44`, `%34%31…`. Their digits count toward the cluster.
  - CSS escapes made only of digits: `\20 `, `\55 `, `\34\31…`.
  - `%E2%80%93`.
- E-mail through the same class: `jane@example%2Ecom` and `jane%2540example.com` are KEPT (`poc-attack13-*` §E). This is the same "URL escapes other than %40 are not decoded" limit.
- Route: `4111%2D1111%2D1111%2D1111`, `123%2D45%2D6789`, `123%C2%A045%C2%A06789`, `4111-…` and `123\2d 45\2d 6789`:
  - vendor_text: 201, `reached=log/pending,export,unit-view`.
  - face attribute: 200, `reached=log/pending,export`.
  - client_approval_doc_ref `url %2D card`: **422** `string_pattern_mismatch` (its pattern refuses `%` and `\`). This matches OPEN.md.
- **Documentation confirmed** at cb708b9:
  - pii.py:33-34: "Known limit (founder decision): URL escapes other than %40 and JS/CSS escapes are not decoded…".
  - docs/adr/0022-ooh-buy-and-contract-controls.md:403-405.
  - docs/findings/OPEN.md:137 row OOH2-R12-SEC-S2 (Medium, known limit).

  Wording and behaviour agree. Every kept escape is a URL or JS/CSS escape with a hex letter. The one HTML-entity class kept outside the limit is S13-1, a separate class that was already present at r12.

### S12-3 (entity residue) — **nesting CLOSED**; non-entity residue kept by rule
- `poc-attack13-*` §A: `&amp;`×n and `&#38;`×n chains around `&nbsp;`/`&ndash;`, n = 0..20, card and SSN:
  - n ≤ 14: 42 of 42 rows redacted in place.
  - n ≥ 15: redacted **whole** (`'[redacted]'`, kinds `number`), because decoding has not settled after 16 passes.
- Benign text with n ≥ 15 (`fine &amp;…amp; dandy`) is also redacted whole. This false positive is accepted.
- Route (`poc-routes13-*`): nest 14/15/20 card in vendor_text gives 201 `reached=-`, sha != raw; face attribute 422.
- Still KEPT: `&NBSP;`, `&Nbsp;`, `& nbsp;`, `&ndash` without `;`, and `&#x2d1111` (greedy hex, which decodes to U+FFFD). Neither html.unescape nor an HTML5 parser treats these as the separator, and their ASCII letters are visible, so they fall within the rule. Route: 201/200, reached log/pending, export, unit view. INFO: OPEN.md does not name this residue; OOH2-R12-LOW lists only "entity leftovers with three or fewer leading digits".

## Item 2 — attack on the rest (`$O/poc/attack13.py`, `reader13.py`, `routes13.py`; outputs `poc-attack13-*`, `poc-reader13-*`, `poc-routes13-*`)

| class | result | reaches a persisted byte? |
|---|---|---|
| entity nesting n=0..20 (amp, #38, mixed `&amp;#38;amp;#x26;…`) | redacted; n ≥ 15 whole | no |
| fullwidth `＆#x34;`/`＆#52;`/`＆nbsp;` (NFKC turns them into an entity); raw ZWJ or soft hyphen inside an entity name (`&nb‍sp;`) | redacted (the backstop decodes the kept text, pii.py:351-353) | no |
| `a`+`&zwj;`+`&#769;`, `a`+ZWJ+U+0301, `x`+`&zwj;`+`&#x307;` (accent joins with a letter only after normalising) | redacted (fixed point, pii.py:124-130) | no |
| entity → fullwidth X / fullwidth digits / dingbat / Ethiopic digits | redacted | no |
| `&#xFF06;…`, `&#xFE60;…` (an ampersand appears only after decode + NFKC); `&zwj;` or `&shy;` inside an entity name (an entity appears only after decode + hidden-character removal) | **KEPT** | yes → **S13-1** |
| `1234&#x78;56789`, `123&lt;b&gt;45…`, `&́nbsp;` | KEPT; the decoded form has an ASCII letter / tag | by rule |
| NFKC → ASCII letter (1130 code points) | 1130 KEPT; the stored bytes hold the ASCII form (0 rows keep the original character) | by rule (S12-4) |
| digit-valued characters (798) | 0 KEPT | no (S12-1 closed) |
| numeric value but no digit value: 1104 (3.12) / 1114 (3.13) | 9-run KEPT for 1021 / 1031 (No 704, Nl 236, Lo 81/91; e.g. ৴ ୲ 〇 Ⅻ 一) | yes → S11-3 (open, documented OOH2-R11-SEC-L / OOH2-R12-LOW) |
| Unicode 16 digits (Garay, Outlined, Sunuwar, Kirat Rai, Gurung Khema, Myanmar Pao; Cn in both Pythons) | **KEPT** | yes → **S13-2** |
| e-mail word class: non-ASCII local part and domain, CJK Ext I (U+31350), Cn, U+2FFC, ʼ inside the domain; ` at ` forms; fullwidth ＠．; entity / nested-entity @; spaced / bracket / `dot` | redacted, identical on 3.12 and 3.13 | no |
| e-mail with a non-ASCII TLD (`jane@example.рф`, `jane@例え.テスト`, `jane@пример.испытание`) | **KEPT** (already at 1a90498) | yes → **S13-3** |
| `jane@localhost`, `jane@[192.0.2.1]` | KEPT | INFO (not internet addresses) |
| `+1 555 1234` after a letter or a non-ASCII/Cn character; `555 1234`, `555 12 34` (7 digits, space) | KEPT | INFO: matches the documented phone shapes (7-digit local number needs a dash; `+` must not follow a word character); fewer than 9 digits |
| split across two fields of one face (location_description `ssn part 12345` + route_description `ssn part 6789`) | 200 / 200, both stored | yes → S11-4 (open) |
| random fuzz, 20,000 strings (digits, a/x, ZWJ, ZWSP, SHY, U+0301/0307/0338, CGJ, Hangul fillers and jamo, `&`, `#`, `amp;`, `nbsp;`, `#38;`, `#x34;`, ＆, WJ, VS16, ❶, U+3164) | screen_form not idempotent: **0**. Kept values whose 20-round reader form (html.unescape + screen_form) has a 9-digit cluster: **0** | no |

### S13-1 — LOW (new; already present at 1a90498, not a regression). Class: SECRET_LEAK
- Path: pii.py:321-329 `_fully_decoded` and :339-353 `_forms` / `_still_holds_personal_data`. Decoding happens before normalising and is not repeated after it. A `&` that only appears after a decode followed by NFKC (`&#xFF06;` → `＆` → `&`, `&#xFE60;` → `﹠` → `&`), or an entity name that is only rejoined after a decode followed by hidden-character removal (`&nb&zwj;sp;` → `&nb‍sp;` → `&nbsp;`), is never decoded a second time.
- PoC (executed): `$S/venv3.12/bin/python -I $O/poc/routes13.py $O/wt/services/ooh-py`. vendor_text `contact 4111&#xFF06;nbsp;1111&#xFF06;nbsp;1111&#xFF06;nbsp;1111 please`:
  - **HTTP 201**, `reached=log/pending,export,unit-view`, request_sha == sha(raw body).
  - The face attribute gives **HTTP 200**, `reached=log/pending,export`.
  - These forms behave the same: `&#xFF06;#x34;…` (all 16 digits as hex entities behind a fullwidth ampersand), `&#xFE60;nbsp;`, `&nb&zwj;sp;`, and the SSN `123&#xFF06;ndash;45&#xFF06;ndash;6789`.
  - client_approval_doc_ref refuses `&` by pattern (422).
- Reader (`poc-reader13-*`, identical at cb708b9 and 1a90498):
  - A single fixed-point HTML decode (a browser) shows **no** cluster.
  - `html → NFKC → html` and `html → screen_form → html` give `4111 1111 1111 1111` / `4111-1111-1111-1111` / `123–45–6789`.
- Why LOW: it needs deliberate encoding plus a reader that normalises between two HTML decodes, which no browser does. As typed, the separators contain ASCII letters (`xFF`, `nbsp`), the same visible class as `4111x1111…`.
- Reproducer: the command above, or `python -I $O/poc/reader13.py $O/wt/services/ooh-py/src`.
- Falsifier: in `_fully_decoded`, alternate `html.unescape` and `screen_form` until both reach a joint fixed point, and screen that form as well. The five rows should then print `[redacted]` / 422 and `reader13` should print `redacted`.

### S13-2 — LOW (new evidence; follows from the founder's "unassigned characters kept" decision). Class: SECRET_LEAK
- Path: pii.py:89-93 (Cn kept as typed), :113 (`unicodedata.digit` is None for Cn), :278 (only `0-9` counts).
- Unicode 16 decimal digits are Cn in Python 3.12 (Unicode 15.0) and 3.13 (15.1). A card or SSN typed in them is stored as typed. Blocks: Garay U+10D40–49, Outlined U+1CCF0–F9, Sunuwar U+11BF0–F9, Kirat Rai U+16D70–79, Gurung Khema U+16130–39, Myanmar Pao U+116D0–D9.
- PoC (executed, `poc-attack13-*` §D; `poc-routes13-*`):
  - At pii level, the card, the SSN, and `1234`+5 such digits are each `KEPT kinds=-` for all 6 blocks.
  - Route: vendor_text Garay card and Outlined SSN give **201**, `reached=log/pending,export,unit-view`; face attribute **200**, `reached=log/pending,export`.
- Why LOW: it needs deliberate encoding, and it is legible only with Unicode 16 fonts. It is a sibling of S11-3. The kept text and the hash are the same on both target Pythons.
- Falsifier: a static, version-independent table of the Unicode ≥ 16 Nd ranges, read as digits by strip_format. The rows should then print `[redacted]` / 422.

### S13-3 — LOW (new; already present at 1a90498, e-mail rule unchanged except the word class). Class: SECRET_LEAK (personal data)
- Path: pii.py:64, :66, :67. The TLD is `[a-z]{2,}`, which is ASCII only.
- PoC (executed): vendor_text `contact jane@example.рф please` and `jane@例え.テスト` give **201**, `reached=log/pending,export,unit-view`; face attribute **200**. At pii level `jane@пример.испытание` is also KEPT. `reader13` shows the same result at 1a90498.
- Falsifier: let the TLD be `(?:[a-z]|[^\x00-\x7f\s]){2,}`. The rows should then print `[redacted]` / 422.

### S13-4 — LOW (new; regression). Class: UNKNOWN (availability)
- Path: pii.py:124-130 (screen_form up to 5 passes), :339-353 (`_forms` / the backstop run screen_form and `_norm` four more times), :366-379. The screen runs under the service lock: `with self.lock:` at svc_campaigns.py:391 contains `observed()` at :416, and svc_inventory.py:198 contains :217.
- PoC (executed; one process, 3.13):

  | body (4000 characters) | pii-level, cb708b9 | pii-level, 1a90498 | `POST …/inventory`, cb708b9 | `POST …/inventory`, 1a90498 |
  |---|---|---|---|---|
  | 4000 × U+FDFA | 0.579 s | 0.086 s | 1.153–1.168 s | 0.174–0.176 s |
  | 4000 Cn | 0.175 s | 0.013 s | 0.350 s | 0.030 s |
  | `1a` × 2000 | 0.131 s | 0.014 s | 0.267 s | 0.031 s |

  Sources: `poc-cost13-3.13.txt`, `poc-cost13-AT-1a90498-3.13.txt`, `poc-cost-route13-3.13.txt`.
- Why LOW: it needs an authenticated caller, the cost is bounded by DOC_MAX = 4000, and it is about 1.2 s of lock time per request. The same class at r11 (OOH2-R11-REL-L N3) was Low. Route this to reliability.
- Falsifier: a screen whose worst case at DOC_MAX stays at or below the 1a90498 figures (for example, running screen_form once per distinct form). The table rows should then return to the 1a90498 figures.

### Bidi
Unchanged from omega12: Cf controls are removed and RTL letters join the cluster. A visual-reordering construction was not built (THEORETICAL, NOT RUN).

## Item 3 — the request hash (service.py:70-103, unchanged in this commit; pii.py changes what "kept" means)
- **Kept text only**:
  - `poc-hash12-*` (omega12 script re-run): 16 shapes × 4 cards or 3 SSNs. request_sha == sha(kept body), != sha(raw body), and no digit is kept: **0 failures**. Each shape has **1 distinct hash** across its secrets.
  - `poc-hash13-*`, the new paths: nest-16 and nest-15 (whole-value redaction), fullwidth-`＆` hex digits (the backstop), dingbat digits, and `a`+ZWJ+U+0301 (fixed point). Again 0 failures and 1 distinct hash per shape.
  - Route rows in `poc-rule-routes-*` and `poc-routes13-*`: for every redacted row, stored sha == sha(kept body) is True and == sha(raw body) is False.
  - Ledger: for `contact 123-45-6789 please` the event has `vendor_text_sha256` == sha256(`contact [redacted] please`). Neither sha(raw) nor the raw digits are in the ledger (`poc-routes13-*`, last row).
- **No recovery by search**: different secrets of one shape give one hash, so a guess has no oracle. No brute force was run (not needed).
- **3.12 vs 3.13**: `$O/poc/sweep13.py` ran every code point except surrogates (**1,112,064**) through 7 templates:
  - the 3 omega12 templates;
  - `a@b<c>d.com`, `jane<c> at example.com`, `x<c>+1 555 1234`, for the new word class;
  - `1234&#x<cp>;56789`, for the decoded path.

  It recorded (kept text, kinds) and the request_sha of a body holding templates 1, 4 and 7.
  - request_sha differs for **0** code points.
  - The kept fingerprint differed for 627 code points, exactly the Unicode 15.0→15.1 set (U+2FFC–2FFF, U+31EF and CJK Ext I). That difference came from `repr()` escaping non-printable characters. Re-run with `ascii()` (`sweep13_check.py`), **0 differ** (`sweep13-check-diff.txt` is empty).
  - Raw outputs: `poc/tmp/sweep13-3.12.txt` (sha256 5b96710c…), `-3.13.txt` (76aec762…); diff in `sweep13-diff.txt`.
- **Idempotency** (`poc-hash12-*`, `poc-hash13-*`, identical on both Pythons apart from unit ids):

  | case | result |
  |---|---|
  | same body twice | 201 / 201, same unit |
  | card A vs card B | 201 / 201 replay (by design) |
  | ʼ-SSN vs literal `[redacted]` | replay (by design) |
  | faces west vs faces east | 409 |
  | Cn kept vs removed | 409 |
  | `&amp;` vs `&` | 409 |
  | redacted text with another field changed | 409 |
  | replay after restart | 201, same unit_id |
  | nest-16 card A vs nest-16 card B | replay |
  | nest-16 card vs plain card (whole vs partial redaction) | 409 |
  | dingbat card vs ASCII card (both redacted now) | replay |
  | nest-16 card vs a *benign* nest-16 text | **replay**: both are kept as `[redacted]` |

  INFO on the last row: whole-value redaction makes any two whole-redacted vendor texts a replay under one request_id. This is consistent with ADR 0022 ("a retry whose vendor text differs only inside a redacted cluster is a replay"), now applied to the whole value.

## Item 4 — gitleaks 8.30.1 and personal data
- Candidate `.gitleaks.toml`: sha256 40410abac2315fc3f0ea7c44676236d1ded64ecae0acf005ed0a1bfb7478e43d. It is the same in `git show cb708b9:.gitleaks.toml`, the frozen copy and the archive, and as at omega12.
- `gitleaks git --log-opts=84a5377..cb708b9` (**40 commits**, 43.05 MB): **no leaks**, exit 0, `gitleaks-git.json` = `[]`.
- `gitleaks dir` on the frozen copy review-cb708b9 (61.32 MB): **no leaks**, exit 0, `gitleaks-dir.json` = `[]`.
- Personal data (`git grep` / `git diff` at cb708b9):
  - `<USER>`: 0 files.
  - `/Users/<name>` in docs/ or claude/: 0.
  - Scratchpad paths are written as `/private/tmp/claude-501/-<HOME>-…` (7774 occurrences in the range, all redacted).
  - E-mails added in 1a90498..cb708b9: fixtures only (`*@example.com`, `a@b.example`, and the fictional `kid.name@gmail.com` quoted from ADR 0007).
  - 13-19-digit runs added: public test PANs, `4111111111111112`, fixture runs (`9999…`, `1000…`), GPS / ZIP / percent-encoded false-positive fixtures (`34.0522,-118.2437`, `90210-1234 90211-5678`, `123%2045%206789`) and a timing (`4503.2814819812775`).
  - SSN-shaped values added: 123-45-6789, 219-09-9999, 078-05-1120, 987-65-4321, ITIN 912-70-1234. All public or fictional test values.
- INFO (unchanged): `<OWNER-EMAIL>` appears only in docs/aegis/round-26b/provenance.md. It is the public commit identity.

## Boundaries (denominator: 11 declared / 11 tested)
| boundary | tested | method |
|---|---|---|
| vendor_text: 9+ card/SSN digits never stored in clear | yes | pii_attack (226 rows), attack13 (≈190 rows + 3 populations + 20k fuzz), routes (24 + 20 forms) |
| face text attribute refused | yes | route level, 44 forms + split across fields |
| client_approval_doc_ref refused | yes | route level, 8 forms (pattern + screen) |
| log/pending, ledger payloads/summaries, /audit/export, unit view | yes | exact, NFKC and JSON-escaped form searched in every route row; ledger `vendor_text_sha256` row |
| S12-1 / S12-2 / S12-3 re-run | yes | omega12 scripts unchanged + regression diff |
| S12-2 = documented limit only | yes | kept-set classification + pii.py:33, ADR 0022:403, OPEN.md:137 |
| entity nesting past the 16-pass bound | yes | n = 0..20 × 2 chains + benign |
| NFKC / combining / hidden + entities | yes | attack13 §B, reader13 at both SHAs, fuzz |
| digit-valued / numeric populations | yes | all 1,112,064 code points filtered by `digit`/`numeric` |
| e-mail/phone word class | yes | attack13 §E + full sweep templates 4-6 |
| request hash: kept only, no recovery, 3.12 = 3.13, replay/409 | yes | hash12, hash13, sweep13 (1,112,064 × 7) |

## NOT RUN
- **pytest suite**: not run. `pgrep -f pytest | wc -l` returned 5, and then 4, at both checks. That is at the founder's machine limit, so no pytest process was started. Test status at cb708b9 is not established by this gate.
- Python 3.14 / Unicode 16 build (no venv). S13-2 was shown only from the 3.12/3.13 side.
- Implicit-bidi display construction (THEORETICAL).
- Hash search or brute force: deliberately not run. Independence was shown directly.
- F4 (13 other Ref fields): out of scope.
- No production, live tenant, real credential or third-party target was touched.

## Gate object
```
{ candidateSha: cb708b924bb01aa5543b832573a8fa56bd041b80, treeOid: eba95097622438b226169c88910c81cc9a999212,
  gate: SECURITY, verdict: SECURITY_GREEN, blocking: false,
  findings: [S13-1 LOW SECRET_LEAK pii.py:321-353, S13-2 LOW SECRET_LEAK pii.py:89-93,113,278,
             S13-3 LOW SECRET_LEAK pii.py:64,66,67, S13-4 LOW UNKNOWN(availability) pii.py:124-130,339-379 + svc_campaigns.py:391/416,
             carried: S12-2 MEDIUM documented limit (OOH2-R12-SEC-S2), S12-3 residue LOW by rule, S12-4 LOW by rule, S11-3 LOW, S11-4 LOW],
  closed: [S12-1, S12-3 nesting],
  denominator: { boundariesDeclared: 11, boundariesTested: 11 } }
```
