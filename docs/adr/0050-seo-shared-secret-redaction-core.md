# ADR 0050 (v2) — SEO (Dept 2): the shared redaction core (`secret_guard`): WITHHOLD THE WHOLE FIELD UNLESS IT IS CLEARLY SAFE

Status: v2 built Nov 9 2026 on `claude/seo-callum-engines-20261108`, superseding the v1 text of this ADR (core v1 at
`dfac59f9` was REJECTED by review). **Not in force.** Fixtures and synthetic data only: no provider has been called, no
key exists, nothing has been spent. Extends ADR 0046; its rules apply unless this ADR says otherwise. Priya's query
classifier (ADR 0047, PR #50) and TNZ's `text_redact` (ADR 0049, PR #53) are to fold into this core when they land;
they are not edited here. Idris (ADR 0048) is not edited here.

## Context

v1 was a denylist: find secret shapes, mask them. Hostile review found thousands of leaks in its own corpus (7 of 21
end-to-end into `data/seo_log.jsonl`) and 8.4% over-redaction at the same time. Every denylist so far has been beaten
by one more shape. The owner approved (Nov 9 2026, 07:32 PT) the opposite design: a field bound for storage, a log or
client output is kept ONLY when it is clearly safe; otherwise the WHOLE field is withheld. Nothing is ever cut out of
the middle of a string.

## Decision

### 1. Normalise (inspection only; the original is what is kept when it passes)
On a bounded input (hard cap `MAX_INPUT` = 65,536 characters; above it the field is withheld unread; URLs are capped
at `URL_MAX_INPUT` = 8,192 characters):
- NFKC; then every code point of category Cf (zero-width, bidi, BOM, soft hyphen, TAG characters U+E0000-E007F),
  Cc, Mn and Me (combining grapheme joiner U+034F, variation selectors U+FE00-FE0F and U+E0100-E01EF), Co, Cn and Cs
  removed; Hangul fillers (U+115F, U+1160, U+3164, U+FFA0) removed; every Unicode space and line / paragraph separator
  (NBSP, U+0085, U+2028, U+2029, braille blank, Mongolian vowel separator, `\n`, `\t`) read as one space; NFKC again;
  runs of spaces collapsed.
- Markdown emphasis / backticks / strike / escapes and HTML-like tags stripped (a separate view).
- Bounded decoding, each round re-normalised: percent (up to `PERCENT_ROUNDS` = 3), HTML entities, `\uXXXX` /
  `\xXX` escapes; base64- and hex-looking runs decoded when they decode to readable text (at most 64 runs of at most
  4,096 characters). Confusables are folded (`lookalikes`, Unicode confusables) for the label check.

### 2. Safe check (on EVERY view; the field passes only if ALL hold)
- **(a) no credential label word** from the closed multilingual list: password / passwd / pwd / pw / pass, passcode,
  passphrase, PIN, OTP / TOTP, secret(s), token(s), api key / apikey / api_key, access key / token / code, security
  code, auth / authorization / bearer, credential(s), cookie(s), session id / sid / session token, seed / recovery
  phrase, mnemonic, private key, client secret, connection string, account key, refresh token, `sig=`, camelCase and
  snake forms (authToken, userPassword, DB_PASS), "key" followed by `:`/`=` or a token-shaped value, and contraseña,
  mot de passe, Passwort / Kennwort, senha, пароль, 密码 / 密碼 / 口令 / 密钥, パスワード / 暗証番号, 비밀번호 / 비번 / 암호,
  wachtwoord, lösenord, hasło, şifre, kata sandi, mật khẩu, clave, chiave, Schlüssel, clé, parola, salasana, heslo,
  jelszó, lozinka, adgangskode, كلمة المرور, رمز عبور, पासवर्ड, รหัสผ่าน, סיסמה, κωδικός πρόσβασης. Long labels are matched
  with every non-letter removed (`p a s s w o r d`, `pass_word`); short ones as whole words (a label split in two by a
  space / NBSP is joined back; `sid` / `sig` / `pw` / `cle` only when an explicit `:` / `=` follows the split).
  **Generic prose holding a label word is withheld** ("Best password managers: Bitwarden, Dashlane"; "Victoria's
  Secret"; "pass the savings on"): counted in the utility number, never special-cased.
- **(b) no `scheme://user:pass@`** (any scheme, protocol-relative too, spaces removed).
- **(c) no strong-key prefix**: AWS ids, OpenAI / Anthropic `sk-`, GitHub, Stripe, Slack (+ webhooks), Google `AIza` /
  `ya29.` / `GOCSPX-` / refresh `1//0`, Perplexity, GitLab, npm / PyPI, Hugging Face, SendGrid, JWT, Shopify
  `shpat_ shpss_ shpca_ shppa_`, Amazon LWA `amzn1.oa2-cs` / `amzn1.application-oa2-client`, Azure `AccountKey=` /
  `SharedAccessSignature` / `sv=…&` SAS, Square `sq0atp- sq0csp- sq0idp- EAAA`, Vault `hvs. hvb. hvr.`, Discord bot,
  Mapbox `sk.eyJ`/`pk.eyJ`, Twilio `SK`/`AC`+32 hex, Mailgun, DigitalOcean, a misc SaaS set, DB / broker URIs with
  credentials (postgres, mysql, mariadb, mongodb(+srv), redis(s), amqp(s), mssql, jdbc), PEM / PGP / OpenSSH headers.
  Matched glued, with spaces removed (distinctive families), with separators removed after the prefix, and at the start
  of a separator-joined token; host labels also upper-cased.
- **(d) no key-shaped token**: a run (separators `. - _ / :` removed where pieces are not plain words) of 16+
  characters mixing upper case, lower case and digits; 32+ hex characters with a digit; 32+ base64 characters with
  letters and digits interleaved (6+ changes); 24+ mixed-case characters with case changing in 35%+ of positions;
  fragments split by spaces / line breaks (consecutive key-like pieces, or 3+ equal pieces of up to 4 characters).
- **(e) no digit run of 9+ digits** once spaces, `. - / ( )` and any 3 or fewer other characters between digits are
  removed (cards, SSNs, phones, account numbers in any disguise).
- **(f) no e-mail address** (plain, spaced, split across words with a common TLD, `[at]` / `(at)` forms, "name at
  domain dot com").
- A URL written inside the text is judged by the URL rules (3): if any part fails, the text fails.

**The safe-shape allowlist (small, disclosed)** — occurrences are masked before (d) and (e) only; (a), (b), (c), (f)
still run: canonical UUID; Amazon order id `###-#######-#######`; ISO-8601 date / date-time; `YYYY/MM/DD`; ISBN-13
(978/979, checksum) and ISBN-10 (after "ISBN", checksum); EAN-13 (checksum); a 7-40 hex commit SHA right after the
word commit / commits / revision / sha.

### 3. Withhold
Any failing check withholds the WHOLE field: an excerpt becomes `[withheld]` (`withheld: "credential"`) or is dropped
with `withheld: "personal_data:<kind>"`; an engine hint / title / candidate name is dropped; a URL keeps scheme + host
(each host label checked; a failing label withholds the whole URL) + only the path segments that pass on their own
(`{secret}` for a credential check, `{withheld}` for personal data), user info never, tracking parameters dropped, the
rest of the query only when every key and value passes, the fragment only when it passes. Path segments are also read
together (a key or number split across `/`): if the joined path fails, the joined segments are withheld. A URL part holding a raw bidi / invisible / control character is withheld
(`{secret}`; a host with one withholds the whole URL). IPv6 literal hosts keep their brackets. A URL with any
credential failure in any part (tracking parameters included) is never fetched.

**Disclosed URL positions** (`POSITIONS` in the code): on these hosts and positions ONLY checks (d) and (e) are waived
for a segment of the exact shape; (a), (b), (c), (f) always run, and a 13-16 digit Luhn-valid number is never waived:
commit SHA (7-12 or 40 hex) after commit/tree/blob/raw/compare/blame on code hosts; gist ids; Google Drive / Docs ids
(33 chars from "1", 28 from "0B", 44 from "1"; never 40 characters, never all hex); Figma file keys; Spotify ids; AP
News article ids; Notion page ids; Loom ids; Facebook `pfbid`; LinkedIn activity slugs; Wix / Contentful /
Squarespace / Google Fonts asset names; Next.js build ids; Google Maps paths; Gemini grounding redirect ids; hashed
asset file names; Medium post ids; BBB profile ids; Amazon help node ids; Canva design / share / template ids; numeric
item / post ids on X, TikTok, Pinterest, eBay, Walmart, Etsy, Vimeo, AliExpress, Stack Overflow, Facebook, DOI, arXiv,
Hacker News, Chewy and listed retailers; numeric values of query keys `variant`, `id`, `page`, `p`, `t`… Each is a
residual risk (below).

### 4. guard_record (fail closed)
Recursive over dict KEYS and values, lists, tuples and sets (returned as lists), bytes (decoded then checked), any
other object (`str()` then checked); a failing value becomes `[withheld]` (None under a `url` / `*_url` key, as
`stored_url` writes a withheld URL), a failing key `[withheld]#<n>`. Numbers,
booleans and None are kept. Bounded (`GUARD_MAX_DEPTH` 16, `GUARD_MAX_NODES` 200,000; beyond them `[withheld]`). Any
internal exception withholds the WHOLE record (the return is the string `[withheld]`), and
`svc_citations._execute_citation_run` then closes the run as `RUN_FAILED` instead of storing it. Exact constants the
service itself emits (`VOCAB`: `secret_redacted`, `SECRET_REDACTED`, `NOT_FETCHED_SECRET`, the placeholders…) pass;
values under `*_sha256` / `*_at` / `*_due` / `*_id` keys pass only when they fullmatch the service's own machine shape
(64 hex; ISO date-time; `seo-<kind>-<40 hex>`); placeholders are blanked before a kept URL is checked.

### 5. API (names kept so callers do not break)
`redact_text(text) -> (text, 0) | ("[withheld]", 1)`; `is_clearly_safe(text) -> bool`; `check(text) -> failing check
or None`; `safe_url(url) -> str`; `inspect_url(url)`; `url_secret_segments(url)`, `url_has_secret(url)`;
`guard_record(obj)`; v1 helpers kept (`strong_secret`, `has_secret`, `decodings`, `_strong_spans` for TNZ, `redact`,
`canonical_key`, `fetch_url`, `CODE_HOSTS`, `ID_HOSTS`).

### 6. Wiring
Callum: `excerpt` checks the kept span plus 48 characters before / 24 after (whole words) and withholds on any failure;
`_safe_hint` keeps a hint only when the whole hint passes; `stored_url` keeps only passing parts; card / SSN / phone
screening goes through check (e) (the v1 per-shape regexes are removed); an IP address is still withheld by the prompt
guard. `svc_citations._execute_citation_run` keeps `guard_record` on the whole result before `_commit`.

## Bars and what was measured (Nov 9 2026, this branch; the reviewer re-measures)
Measured with `tests/test_secret_guard_core.py` and the scripts behind it; every number is from a run on this
branch.

| Bar | Measured | Met? |
|---|---|---|
| Zero leaks, generated corpus >= 2,000 | `tests/leak_corpus_v2.py`: 3,494 text cases (44 key / personal-data families x (37 character / encoding / markdown disguises + 14 pairs), 47 labels x 19 label forms + 5 disguised-label forms, PIN and passphrase forms, escaped / base64 labels, homoglyph labels) and 400 URL cases (path, split path, query, tracking parameter, fragment, double encoding, Docs / commit positions, host label, hex path). Leaks: `check` 0, `redact_text` 0, `guard_record` as value / key / list / set / bytes / object 0 each, `callum.excerpt` 0, `callum._safe_hint` 0, `safe_url` 0, `stored_url` 0, credential URL not blocked from fetch 0, `classify` (in memory) 0, **service path** (41 real citation runs, 3,894 engine answers, everything written to the data directory incl. `data/seo_log.jsonl`, the ledger payloads and the API response) 0. The v1 corpus (`tests/secret_corpus.py`, >= 1,000) also 0. | yes |
| Excerpts withheld <= 5% (>= 300 realistic) | 22 / 312 = **7.05%** (`tests/utility_corpus_v2.py`: 106 hand-written answers cut by `callum.excerpt` per brand). Every one is rule 2a (label words in generic prose: password managers, "pass", Pinterest "pin", "cookie", "token-gated", "API keys", "Victoria's Secret", "secret to a landing page"); no other check withholds any excerpt of the corpus. | **no (missed)** |
| URLs reduced <= 2.5% (>= 300 legitimate) | `safe_url`: 1 / 306 = 0.33% (a Shopify `variant` id that is Luhn-valid by chance); Callum `stored_url` (path only): 0 / 306. v1's corpus (`tests/url_corpus.py`, 1,180): 21 = 1.78% (Zoom meeting ids, one Luhn-valid AliExpress id). | yes |
| Linear time; < 200 ms at 64 KB; hard cap | `check` on 20/40/80/160 KB (cap lifted): worst series 33 / 59 / 114 / 255 ms (NBSP), every series roughly doubles. At 64 KB: `check` worst 94 ms, `guard_record` worst 101 ms; `safe_url` at its 8 KB cap worst 41 ms. Above 65,536 characters `check` returns `cap` unread. | yes |
| Tests bite | 37 mutations (plus the URL control-character rule, bitten by its own test) (each check 2a-2f and the URL-in-text rule; NFKC, the character map, space mapping, Hangul fillers, each dropped category Cf / Mn / Cc / Me / Co / Cn, space collapsing, markup strip, confusable fold; percent, percent rounds, entities, escapes, base64 runs, hex runs; each `guard_record` branch incl. dict keys, sets, bytes, objects, fail-closed; Callum's field check and URL inspection; the `guard_record` call before `_commit`): each makes at least one test OTHER than the in-suite mutation tests fail. | yes |
| Ruff | 0 new findings vs `7c254074` with ruff 0.15.11 (repo settings), 0.15.11 `--isolated` and 0.17.4 `--isolated`. No raw invisible characters in the sources written here (`\u` escapes). | yes |


## Residual risks (disclosed)
- A value of a disclosed URL position's exact shape (a 40-hex token in a commit position, a random 33-character id in
  a Drive position, a 22-character token in a Canva / Figma / Spotify position, a non-card numeric value in a listed
  id position) is kept: checks (d) / (e) are waived there by design.
- A 13-digit card number that happens to pass the EAN-13 checksum, or a number written as an ISO date / UUID / order
  id shape, is masked by the allowlist before check (e).
- Split-key detection for lower-case-plus-digit keys cut into word-like pieces is heuristic (plain-word pieces are set
  aside before judging the rest).
- A credential with a value that is short and word-like and NO label word near it (e.g. a bare PIN) cannot be told
  from prose; only a label makes it a credential.
- `EXCERPT_MARGIN` covers a label up to 48 characters before the kept span; a label further away with its value inside
  the span is not seen (the value alone is checked by (b)-(f)).
- The excerpt utility bar (5%) is missed on the disclosed corpus, entirely because of rule 2a (label words in generic
  prose); see the measurement.
