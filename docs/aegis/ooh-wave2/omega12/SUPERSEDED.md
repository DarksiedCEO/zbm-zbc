# omega12 (1a90498): SUPERSEDED — no Ω verdict

candidateSha: 1a904983d6e064b912bbd95a7afa1668da6c50c9
treeOid: 83356dd062ff3e668358785f5e6d963cdb3950c2

Founder decision (Oct 18 2026): supersede 1a90498 (source, reliability, data and mutation BLOCK; the pending fix covers
all of them) and commit the fix; URL and JS/CSS escapes (security S12-2) are a documented limit, not decoded; the next
mutation gate covers only the lines changed in the new commit plus the 13 named mutants and the 18 confirmed survivors
of this run. Written by the builder.

| Gate | Verdict at 1a90498 | Report sha256 (as returned by the gate, checked by the builder) |
|---|---|---|
| SOURCE | **BLOCK** (S-1 High: entity decoding stopped after 3 passes; S-2 High: one stage on the decoded text, screen_form not idempotent; S-3 Medium docs; S-4 Low; S-5 Info) | source.md 1904a8b96c9edd91c63bd4689edf5d89763633120091d6e54aecb882f69415fe |
| SECURITY | SECURITY_GREEN (S12-1 Medium: non-decimal digits not counted, a regression; S12-2 Medium: URL/JS/CSS escapes; S12-3, S12-4 Low; S11-1, S11-2 closed) | security.md 6859b4b0c3a65cc9547974546f5bf2fc3ea5c0eeb9ddee7c7b998b20d50cb021 |
| DATA | **BLOCK** (D12-H1 High: card/SSN in non-decimal digits stored, same as S12-1; D12-L1 Low; INFO-7..9; D11-L1, D11-L2 closed) | data.md 3c83987f7a988c3628d42274ee58fefdb74ca94351a4f30f2094e2b013a0005a |
| RUNTIME | RUNTIME_GREEN_FOR_SCOPE (O12-1 Low nesting depth; O12-2, O12-3 Info; O11-1 closed at runtime) | runtime.md b91a39bc645a28ba7b7135007fe66122ad7effdc86417da24f5efff88da2af3f |
| RELIABILITY | **BLOCK** (R12-1 High: entity decoding not to a fixed point; R12-2 Medium: `\w` follows the Unicode version; N3 Low; N2 closed) | reliability.md 37b871da88826a7fc039eab928140c01b42542170f0fb1993754e7e31ed86342 |
| MUTATION | **BLOCK** (83 of 891 run, 808 UNRUN; 13 named mutants killed; confirmed survivors N146, N156, F001, F002 High, F015, F008 Medium/Low, and Lows) | mutation.md 3881b74a3326ae42a54c19ba5e798286b3e56a0ce041094195398f9c48df5db8 |
| TEST-VERIFICATION, PROVENANCE, RELEASE GUARDIAN, Ω | not run (superseded) | — |
