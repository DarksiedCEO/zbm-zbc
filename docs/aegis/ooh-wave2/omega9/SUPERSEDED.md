# omega9 (91ea9a1): SUPERSEDED — no Ω verdict

candidateSha: 91ea9a1948253a3203150b38a8c2f2f839f232cd
treeOid: 482eb8983853a8bdff118c16446474b0b16537da
superseded by: 0f5fbe33642a62068239f4df83edd8602730fd8e (tree ffc3bd9caa09782a69e91d7466ce1b30eef16516)

Founder instruction (Oct 18 2026): 91ea9a1 is already blocked by security F1 and data D9-H1, and both fixes are in
0f5fbe3; do not run Ω on 91ea9a1; stop the mutation run; record 91ea9a1 as SUPERSEDED. Written by the builder.

| Gate | Verdict at 91ea9a1 | Report sha256 (as returned by the gate, checked by the builder) |
|---|---|---|
| SOURCE | SOURCE_GREEN (S-1 Medium, S-2 and S-3 Low) | source.md de9f94f51597f75c1bcb0a355b0c8801c169bf185759cfcc216640c3909ba674 |
| SECURITY | **BLOCK** (F1 High: request_sha of the raw body; F2, F3, F4 Medium; F5, F6 Low) | security.md ef0a20ca6a73d9f23e04e03e17a51461361115dff7c452d53849434a232e14b7 |
| RELIABILITY | RELIABILITY_GREEN (F1-F3 Low, F4 Info) | reliability.md 2402a7fb7bbe1bd14355655a2068fd23aad712f23fcf9b9e6da16121c7dc0c18 |
| RUNTIME | RUNTIME_GREEN_FOR_SCOPE (RT9-1 Medium, start-up read) | runtime.md 4b5f6fd5080be3dba0bc5c6f6d3efbd0597885bd4d2ec1759f3bea4e3315892f |
| DATA (run 2) | **BLOCK** (D9-H1 High: SSN and other-script card forms stored; D9-M1 Medium: request_sha recovery; M-2, M-3 unchanged; M-1 closed) | data.md 23dde80e68539029592c991f2b4b0fd566ce7784b499fad3024f7d0ec1ec14ff |
| DATA (run 1) | no verdict (report write cut off; raw outputs in data-run1/) | — |
| MUTATION | stopped on the founder's instruction before it finished; no verdict | — |
| TEST-VERIFICATION, PROVENANCE, RELEASE GUARDIAN, Ω | not run (superseded) | — |

Fixed in 0f5fbe3: security F1, F2, F3, F5, F6; source S-1 (S-2 listed as an accepted limit); data D9-H1, D9-M1;
runtime RT9-1; reliability F1, F2. Not fixed: security F4 (13 other reference fields; outside the founder's scope),
data M-2 and M-3 (carried), reliability F3.
