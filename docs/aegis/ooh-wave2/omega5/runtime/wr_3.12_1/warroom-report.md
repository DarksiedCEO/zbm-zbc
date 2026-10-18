# War room report (seed 1)

Gate: **PASS**. LLM personas: NOT_CONNECTED (deterministic personas only).

| Department | Gate | Cases | PASS | FAIL | ERROR | MUST pass rate (excl. known) | SHOULD score | Known failures |
|---|---|---|---|---|---|---|---|---|
| ooh-py | PASS | 156 | 156 | 0 | 0 | 100.0% | n/a | 0 |

## ooh-py

| Transform | Cases | MUST failures | SHOULD misses |
|---|---|---|---|
| (base) | 64 | 0 | 0 |
| case_flip | 16 | 0 | 0 |
| fullwidth | 31 | 0 | 0 |
| homoglyph | 17 | 0 | 0 |
| homoglyph_wide | 2 | 0 | 0 |
| html_wrap | 1 | 0 | 0 |
| injection | 2 | 0 | 0 |
| leetspeak | 7 | 0 | 0 |
| letter_punct | 11 | 0 | 0 |
| long_body | 2 | 0 | 0 |
| quoted_reply | 4 | 0 | 0 |
| signature | 2 | 0 | 0 |
| soft_hyphen | 23 | 0 | 0 |
| zero_width | 23 | 0 | 0 |

| Scenario | Cases | PASS | FAIL | ERROR | known failure |
|---|---|---|---|---|---|
| amount-off-the-quote | 8 | 8 | 0 | 0 | 0 |
| duplicate-submission | 1 | 1 | 0 | 0 | 0 |
| face-clash-across-tenants | 5 | 5 | 0 | 0 | 0 |
| face-registered-twice | 19 | 19 | 0 | 0 | 0 |
| funds-of-nothing | 1 | 1 | 0 | 0 | 0 |
| funds-reference-respelt | 27 | 27 | 0 | 0 | 0 |
| funds-zero-padded | 15 | 15 | 0 | 0 | 0 |
| one-unit-two-quotes | 1 | 1 | 0 | 0 | 0 |
| outcome-during-dispute | 2 | 2 | 0 | 0 | 0 |
| someone-else-approves-a-contract | 3 | 3 | 0 | 0 | 0 |
| someone-else-authorizes | 4 | 4 | 0 | 0 | 0 |
| someone-else-confirms-a-hold | 3 | 3 | 0 | 0 | 0 |
| vendor-spoofing | 32 | 32 | 0 | 0 | 0 |
| vendor-substitution | 19 | 19 | 0 | 0 | 0 |
| vendor-text-injection | 16 | 16 | 0 | 0 | 0 |

## Slowest cases (measured, not asserted)

- `ooh-py/funds-reference-respelt#000.1@1` 339.4 ms
- `ooh-py/funds-reference-respelt#001.1@1` 284.7 ms
- `ooh-py/funds-reference-respelt#000.2@1` 245.6 ms
- `ooh-py/funds-reference-respelt#003.2@1` 238.4 ms
- `ooh-py/face-clash-across-tenants#001.0@1` 236.4 ms
- `ooh-py/funds-zero-padded#005.1@1` 231.4 ms
- `ooh-py/face-clash-across-tenants#000.0@1` 231.2 ms
- `ooh-py/funds-reference-respelt#001.0@1` 227.8 ms
- `ooh-py/face-clash-across-tenants#004.0@1` 225.7 ms
- `ooh-py/face-clash-across-tenants#002.0@1` 225.6 ms
