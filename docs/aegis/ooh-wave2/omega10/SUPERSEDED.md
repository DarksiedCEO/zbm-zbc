# omega10 (0f5fbe3): SUPERSEDED — no Ω verdict

candidateSha: 0f5fbe33642a62068239f4df83edd8602730fd8e
treeOid: ffc3bd9caa09782a69e91d7466ce1b30eef16516

Founder instruction (Oct 18 2026): stop the mutation run on 0f5fbe3 (superseded) and record 0f5fbe3 as SUPERSEDED;
change the OOH2-PII-1 design (any cluster of 9 or more digits joined by non-letter separators is redacted in the three
screened free-text fields; the card and SSN rules only label what was redacted; the request hash uses the redacted
text only). Written by the builder.

| Gate | Verdict at 0f5fbe3 | Report sha256 (as returned by the gate, checked by the builder) |
|---|---|---|
| SOURCE | SOURCE_GREEN (M-1 invisible non-format characters, M-2 hash-collision docs; L-1..L-4) | source.md 2b8d9d7aa469581ecf018b439f00235f79a5d88d4f7ca68df9138b51034cb4fc |
| SECURITY | **BLOCK** (F1 High residual: raw-text screen vs storage screen, "123  45  6789"; F2, F3, F5, F6 closed; F4 open) | security/security.md b81b12715ff6feedb731af9c970d5350c364d7302953d8f6ccdeac4109f8a47e (written one folder lower than asked) |
| DATA | **BLOCK** (D10-H1 High: SSN spellings with multi-character or other separators, a combining mark in a digit group; D10-M1 Medium request_sha residual; D9-H1 closed; M-2, M-3 open) | data.md 45416606f022b465b7b6b45e508d064210933f5eb15b7000496ca5c1632c2620 |
| RUNTIME | RUNTIME_GREEN_FOR_SCOPE (RT9-1 closed; O10-1, O10-2 Low) | runtime.md a6b3125b59b0af8c7811d3b7a865581a103985ea473c52f094321396cda6ff09 |
| RELIABILITY | RELIABILITY_GREEN (F1 write-path close, F2 hash cost: Medium; F3-F5 Low; F6-F8 Info) | reliability.md 957963450caa67d5d0b40af2a9f7b4faf46169e2e3911c367fded5feb411111e |
| MUTATION | stopped on the founder's instruction before it finished; no verdict | — |
| TEST-VERIFICATION, PROVENANCE, RELEASE GUARDIAN, Ω | not run (superseded) | — |

The successor commit addresses: security F1 residual; data D10-H1, D10-M1; source M-1, M-2 (docs), L-1, L-2;
reliability F1, F2, F4, F5, F8. Not fixed: security F4 (13 other reference fields, outside the founder's scope),
data M-2 and M-3 (carried), reliability F3, F7, runtime O10-1, O10-2.
