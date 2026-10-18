# AEGIS Release Guardian at 4a0a4b4 (transcribed)

> Transcription note: Release Guardian has no write access. This is its final hand-back of 2026-10-18, copied by
> the builder without edits to its findings.

candidateSha: 4a0a4b478a88fbaaeb864a7535779e586b39f64a
treeOid: e0640db37e4a40f58e0a291fd21d90a56f45d1ec
base: 84a5377df4892efcb4cb1c221a6862075fe0e018

RECOMMENDATION: NOT_READY (HOLD), blocking = true. Ω gives the final verdict.

## Binding
- `git rev-parse` re-run in the ooh2 worktree; both values match.
- src tree 6d8a4442… and store.py blob bff84c6780b1fcbc8fed57379d23c4235c22eee1 identical at 4a0a4b4 and 75b0662 (checked by the guardian).

## Per gate (aegis/omega5/)
| Gate | Verdict as read | Bound? | Who wrote it | Raw evidence on disk |
|---|---|---|---|---|
| SOURCE | SOURCE_GREEN | yes, SHA + tree | builder's transcription | source/pytest-3.12.txt, pytest-3.13.txt, headed with SHA and tree, 767 passed each |
| SECURITY | SECURITY_GREEN | yes | gate itself | security/: two gitleaks JSONs (`[]`), pytest EXIT=0 on both, poc/*.out, matrix.tsv |
| DATA | DATA_GREEN | yes | gate itself | data/out, data/live, data/poc |
| RELIABILITY | RELIABILITY_GREEN | yes | builder's transcription | reliability/runs: 30 files (22 with SHA header; 2 deliberate control runs on d1b0de9; 3 "done" markers; 3 war-room outputs with no header) |
| RUNTIME | RUNTIME_GREEN_FOR_SCOPE | yes, plus subtree OIDs | gate itself | runtime/: cargo_build.log, cw_*/iw_*/live/wr outputs, fals/, disclosed/ |
| MUTATION | mutation.md BLOCK (narrow), replaced by mutation-rgate.md MUTATION_GREEN with open items | yes, both | mutation.md does not say; mutation-rgate.md builder's transcription | mutation/, mutation/raw/ (T222_late_demo.txt, killers_234.tsv, probes/, *.jsonl) |
| PROVENANCE | PROVENANCE_PASS | yes | builder's transcription | four listing hashes; path checks |

- Runtime is effectively superseded on one point: the 4a0a4b4 runtime GREEN never tested a same-size in-place edit; the omega7 runtime run on the same source BLOCKs on it.
- Transcription caveat: source, reliability, mutation-rgate and provenance are builder transcriptions; git cannot show they match what the gates said. The raw outputs back the counts checked (767 passed, 8/8 SIGKILL RESULT PASS, T222 demo file).
- omega7 at 75b0662, reference only: source, security, data, reliability GREEN; runtime BLOCK; mutation stopped before finishing.
- CI: none for 4a0a4b4. `git branch -r --contains 4a0a4b4` is empty; consistent with no push; CI NOT_PROVEN.

## H-1: an unresolved blocker for 4a0a4b4? Yes.
1. Source identical. store.py:180-184 at 4a0a4b4 compares only size (`elif size != expected`). No src caller of `RecordLog.verify()`; git grep finds only `self.rec.client.verify()` at service.py:359 (the ledger's verify).
2. Already observed at 4a0a4b4: DATA check C11 (data/poc/runtime_tamper.py) in data/out/runtime-tamper-3.12.json: write after the disk edit → 200; integrity ok true; disk_verify false; restart refused. DATA rated it M-1 MEDIUM (4a0a4b4 and 75b0662); RUNTIME rated the same defect H-1 HIGH. Worst verdict wins until settled.
3. store.py:159-160 docstring at 4a0a4b4: "The file must be exactly the in-memory lines before the write (else refused)". The ADR 0022:225 sentence H-1 cites appears only after 4a0a4b4. The store.py:11-12 docstring leaves in-place edits to the start-up check, which the runtime gate says Ω may weigh.
4. Under R-GATE, Highs block whatever the area; only Ω or a founder ruling can grade H-1 down.

## Findings register
| ID | Sev | Source | Class |
|---|---|---|---|
| H-1 | HIGH | runtime omega7 (identical src) | BLOCKING (unresolved) |
| T222 | MEDIUM | mutation | needs Ω ruling; guardian treats as blocking by default (note 2) |
| V001 | MEDIUM | mutation (anchor key `line_sha256` not pinned) | OPEN.md per the gate; Ω to confirm outside anchoring scope |
| DATA M-1 | MEDIUM | data (same defect as H-1) | goes with H-1 |
| SEC M-1 | MEDIUM | security: card/SSN stored and readable internally, against ADR 0022:151 | OPEN.md |
| DATA M-2 | MEDIUM | data: reads serve altered figures after a consistent rewrite | OPEN.md |
| REL F2 | MEDIUM | reliability: slow-drip ledger holds the service lock ~88 s | OPEN.md |
| SRC F1, F2 | MEDIUM | source: doc drift, stale comments | OPEN.md |
| Aside-branch gap (service.py:397-404) | LOW/MEDIUM | mutation | OPEN.md |
| H198, H229, H399, H448 | LOW | mutation | OPEN.md |
| SEC L-1, L-3 | LOW | security | OPEN.md |
| DATA L-1, L-2, L-3 | LOW | data | OPEN.md |
| REL fd leak, timing offsets, F3 ReDoS | LOW | reliability | OPEN.md |
| SRC F3, F4, F5, F5-class at service.py:452 | LOW | source | OPEN.md |
| RT O-1 (O-2 from omega7, same src) | LOW | runtime | OPEN.md |
| A053/A049 + T157/T159, A058/T126-T128/T212/T218/T223/T247, B046/B057/B065-B071, 390 legacy survivors | survivors | mutation | OPEN.md under R-GATE; T157/T159 is the undefended file-vs-memory guard, same area as H-1 |
| DATA INFO-2 to INFO-5 | INFO | data | not tracked |

Note 2, T222: survivors block in "the ledger anchoring that protects those" areas; T222 sits there (store.py:262 unlink failure swallowed under the mutant; the 503 answers maybe=false; the write takes effect later). Gate rated Medium and left scope to Ω. Killing tests exist only from ce665c5 on.

Tracking gap: at 4a0a4b4 and 75b0662 OPEN.md has no ooh-py lines; none of the Medium/Low items is tracked yet. Adding them means a new commit, which would void this tuple.

## READINESS: NOT_READY
1. H-1 HIGH on byte-identical source; DATA rated the same behaviour MEDIUM; unresolved, worst wins.
2. T222 survivor on the anchoring path behind money and approvals; no Ω scope ruling yet.
3. No Medium/Low finding is recorded in OPEN.md at the candidate.
4. No CI for 4a0a4b4 (NOT_PROVEN, not failed).
5. Four of seven gate reports are builder transcriptions; raw outputs back their numbers, authorship unverifiable.

If Ω grades H-1 down to Medium and rules T222 out of scope, the remaining gates read GREEN with tracked findings, and only the OPEN.md tracking gap and missing CI remain for Ω to weigh.
