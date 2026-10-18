# omega11 (f5ee168): SUPERSEDED — no Ω verdict

candidateSha: f5ee168c9e9148241b709ec93acc660c88d08ba7
treeOid: da5fc085a0669ba5f92df726cfefcb28bd505cab

Founder instruction (Oct 18 2026): stop the scoped mutation run on f5ee168 (pii.py is changing) and record f5ee168 as
SUPERSEDED (source S-1). Founder decisions folded into the successor: (1) stop removing unassigned characters
(reliability N1); (2) only ASCII letters A–Z/a–z end a digit cluster, every other character joins it (runtime O11-1,
security S11-1, source S-5); (3) decode HTML entities before the check and screen both the raw and the decoded text
(security S11-2, data D11-L2); (4) accept the false-positive rate for now, with an OPEN.md item for structured vendor
fields. Written by the builder.

| Gate | Verdict at f5ee168 | Report sha256 (as returned by the gate, checked by the builder) |
|---|---|---|
| SOURCE | **BLOCK** (S-1 High: "1234567%40" kept nine digits; S-2 Medium; S-3, S-4 Low; S-5, S-6 Info) | source.md 96bdd411ddf7007ae3a34a73b60bc8585d8a77404ad6ddca514e1e0620c0791f |
| SECURITY | SECURITY_GREEN (S11-1, S11-2 Medium; S11-3..S11-5 Low; omega10 F1 closed) | security.md 8eb1ddc251b888b1a4f1c18975449dba0882a2ba3c18dd9efe0b428219c3ca40 |
| DATA | DATA_GREEN (D10-H1, D10-M1 closed; D11-L1, D11-L2 Low; M-2, M-3 open) | data.md 4174a63288e36cba3984fd687f02ca268fb0231bd57ce684e6bbf3950c5a446f |
| RUNTIME | RUNTIME_GREEN_FOR_SCOPE (O11-1 Medium, design boundary; O11-2, O11-3 Info) | runtime.md 9b7001849511b7518078ebeb263cbd2a864b259d08315df9fa315907d98ef92d |
| RELIABILITY | RELIABILITY_GREEN (N1 Medium: Unicode-version-dependent hash; N2, N3 Low; N4, N5 Info) | reliability.md 6525fedd8c3698e2fe0151b3d9f47fb22f1faa862ee0fb0e6d627c14259022e4 |
| MUTATION | run 1 stopped (full suite per mutant, founder: too slow); run 2 (scoped) stopped on the founder's instruction; no verdict | — |
| TEST-VERIFICATION, PROVENANCE, RELEASE GUARDIAN, Ω | not run (superseded) | — |
