# ADR 0050 — SEO (Dept 2): the shared secret-redaction core (`secret_guard`) and its closed threat list

Status: built Nov 8 2026 on `claude/seo-callum-engines-20261108` (Callum round 4). **Not in force.** Fixtures and
synthetic data only: no provider has been called, no key exists, nothing has been spent. Extends ADR 0046; its rules
apply unless this ADR says otherwise. Priya's query classifier (ADR 0047, PR #50) and TNZ's `text_redact` (ADR 0049,
PR #53) are to fold into this one table when they land; until then they are not edited here.

## Context

Callum keeps bounded excerpts of answer text, engine hints/titles, and the URLs an engine cited, and writes the whole
run result to `data/seo_log.jsonl`. None of it may ever carry a credential — the engine's own API key included. The
war room and the round-3 review found secrets reaching storage through gaps spread across several modules (labelled
credentials with no separator/short values, glued/fullwidth/invisible/line-break obfuscations, keys hidden in URL host
labels or crossing `/` in a path, and un-swept Callum excerpts reaching the log). The decision (recorded in the
project) is ONE shared core, `agents/secret_guard.py`, with a CLOSED written threat list it is certified against, a
stable public API, and a fail-closed sweep of anything bound for storage/logs/output.

## Decision

### API (one rule table, three entry points)
- `redact_text(text) -> (redacted, findings: int)` — every secret-shaped span replaced by `[redacted]`; a URL written
  in the text that carries a secret is replaced whole. Never raises; `("", 0)` on empty.
- `url_secret_segments(url) -> (bad_path_indexes, other)` — ONE scan over the whole URL (host and every host label,
  userinfo, every path segment and the whole decoded path for AWS windows that cross `/`, every query key and value,
  the fragment), at every decode level. `url_has_secret` is the yes/no form; a URL with one is never fetched.
- `guard_record(obj)` — FAIL-CLOSED sweep of any `dict` / `list` / `str` before it is stored or logged: every string
  is run through `redact_text`, and if a strong rule still fires on the result the whole string is replaced. Wired
  into `svc_citations._execute_citation_run` on the run result before `_commit`.

### Normalisation (before every match, bounded)
NFKC fold after zero-width / bidi / soft-hyphen / word-joiner / BOM are stripped; percent- and HTML-entity decoding
repeated until stable (≤ `DECODE_ROUNDS`); a line break between two token characters rejoined. Each input is checked
as written and after each form.

### CLOSED threat list (certified; outside the list = documented residual risk)

Strong-key families (always redacted, no host or URL-shape exemption):
- AWS access key id (`AKIA`/`ASIA`/`AGPA`/`AIDA`/`AROA`/`AIPA`/`ANPA`/`ANVA`/`APKA`/`ABIA`/`ACCA` + 16), including
  glued to a word or capitals, and the one-space-between-every-character form (with a digit).
- AWS secret access key: a 40-char `[A-Za-z0-9/+]` run with a digit + upper + lower, OR any 40-char run (digits
  optional — the no-digit secret) when labelled with `aws`/`secret`/`key`/`access` nearby, OR after a credential label.
- Slack `xox[a-z]-` (any letter) and the refresh forms `xoxe.xox[a-z]-` and `xoxe-<digits>-`.
- GitHub `gh[pousr]_` and `github_pat_`; GitLab `glpat-`; OpenAI/Anthropic `sk-`, `sk-proj-`, `sk-svcacct-`,
  `sk-ant-…`; Perplexity `pplx-`; Stripe `sk|rk|pk_live|test_`, `whsec_`; Google `AIza…`; npm `npm_`; HuggingFace
  `hf_`; SendGrid `SG.…`; JWT `eyJ….….…`; PEM / PGP `-----BEGIN … PRIVATE KEY(-BLOCK)-----` blocks (unterminated
  body redacted to the base64 run).

Labelled credentials: any value ≥ 6 characters after a credential label and an explicit separator (`:` `=` `->` `→`
`—` `` is `` `` - ``, JSON `": "`), with no "tokenish" gate — labels include `password`/`passwd`/`pwd`/`passcode`/
`passphrase`/`PIN`, `secret`/`client_secret`, `api key`/`apikey`/`api-key`/`x-api-key`/`x-goog-api-key`,
`access token`/`access code`/`refresh token`, `auth`/`authorization`/`bearer`/`token`/`session id`/`cookie`, and
common non-English labels (`contraseña`, `mot de passe`, `schlüssel`, `senha`, `clé`, `clave`, `пароль`, `ключ`,
`密码`, `密碼`, `パスワード`, `비밀번호`). A bare-whitespace separator keeps a token-shape gate so `key product` stays prose.

Obfuscations (each, and pairs of them): glued to words/capitals; fullwidth / NFKC compatibility forms; invisible-char
and bidi splits; line-break splits; percent-encoding at every level; inside a URL userinfo, host label, path, query
or fragment.

### Bars (certified in tests)
- Zero strong-key and zero labelled-credential leaks across a synthetic corpus of ≥ 1,000 cases covering every list
  entry and pairs of obfuscations (`tests/secret_corpus.py`, `tests/test_secret_guard_core.py`).
- Legit over-redaction ≤ 2.5% on a disclosed corpus of ≥ 200 legitimate phrases and URLs; measured 1.69% on the
  1,180-URL corpus and 0 on the legit phrases. There are NO shape exemptions on the strong rules: the former
  path-word skip is removed because it let real AWS secret keys through — security over precision.
- Each guard (pattern family, normalisation step, decode step, line-break rejoin, `guard_record`) is load-bearing:
  disabling any one leaks a representative input (demonstrated in `test_each_guard_is_load_bearing` and the wiring
  test).
- Worst-case runtime is bounded (no catastrophic backtracking): ≤ 2 s on 40 KB adversarial inputs, measured.

### Documented residual risk (outside the closed list)
A bare, shapeless 40-character AWS secret (no prefix, no label) glued into the MIDDLE of a word with no delimiter is
indistinguishable from random base64 and is not reliably detectable without unacceptable over-redaction; it is
accepted as residual. Labelled and delimited renderings of the same secret are covered.

## Consequences

One rule set for text and URLs; one place to extend. A handful of Drive/Canva ids and Title-case product paths that
coincidentally fit the AWS 40-character format across `/` are now withheld (counted in the 1.69%); the host and the
other path segments are kept, and the URL is still identified. Personal-data detection stays per-agent (Callum screens
excerpts, titles/hints and URL segments with the base screen plus a Unicode-separator reading) until Priya certifies a
shared screen.
