# AEGIS MUTATION gate at 4a0a4b4: re-ruling under R-GATE (transcribed)

> Transcription note: the text below the line is the mutation gate agent's final hand-back of 2026-10-18 12:22 UTC,
> copied by the builder without edits. The gate did not write this file. Its original report, `mutation.md` in
> this folder, says BLOCK (narrow) under the earlier push rule; this hand-back is its re-ruling after the founder
> applied R-GATE (docs/findings/OPEN.md header: Critical/High block, Medium/Low are tracked there) to this round.
>
> Builder's factual note (not the gate's words): "T222" names a mutant, not the shipped code. At 4a0a4b4, and at
> 75b0662 (store.py is byte-identical), store.py:261-262 re-raises an unlink OSError as StoreWriteError. Mutant
> T222 replaces that raise with a swallow; at 4a0a4b4 no test killed it. Commit ce665c5 (a child of 4a0a4b4) added
> two test_t222_* tests in tests/test_aegis_omega5_mutation.py that kill it.

candidateSha: 4a0a4b478a88fbaaeb864a7535779e586b39f64a
treeOid: e0640db37e4a40f58e0a291fd21d90a56f45d1ec

VERDICT (gate's words): MUTATION_GREEN for 4a0a4b4, with open items.

---

Under the R-GATE rule you just gave, my verdict changes from BLOCK to **MUTATION_GREEN for 4a0a4b4, with open items**. Nothing I found is Critical or High.

I wrote nothing to the repo, so `docs/findings/OPEN.md` is untouched. I made no commit and no push, and I did not run gitleaks, the personal-data scan or Ω. Those are for the main session, as is the card/SSN question, which this gate has no data on. The IDs below are ready for whoever writes OPEN.md.

**Blocking**
- None. I found no survivor in money, purchase authorization, approvals/HOLD_CONFIRMED, funds verification or tenant isolation.
- I found no way to commit a write without its ledger anchor, and no tampered log that passes the integrity check.
- **T222 (Medium)** is the one item that touches the anchoring that protects those areas.
  - It is store.py:262, where `clear_pending` swallows an unlink failure.
  - Under it, a certain ledger refusal answers maybe=false while the pending line stays on disk, and the write takes effect if its anchor later appears.
  - It needs an unlink failure plus an anchor appearing after a "certain" refusal. Ω must decide whether it counts as in scope.
  - Medium severity means no code commit this round either way.

**OPEN.md, non-blocking**
- **V001 (Medium):** the anchor payload key name `line_sha256` is not pinned by any test.
- **H198, H229, H399, H448 (Low):**
  - These are the four mutants the builder called equivalent but are not.
  - ADR 0022's "21 equivalent" wording is wrong for them.
- **Aside-branch gap (Low/Medium):** in service.py:397-404, the discarded-line branch ignores the problem `_roll_forward` returns.
- **Audit routes:** B065-B071, B046, B057 and related survivors in `/audit/evidence` and `/audit/export`, including that "unanchored evidence = attempted" has no test.
- **Dual-writer guards:** A053/A049 and T157/T159 mask each other, and nothing tests either.
- **Closed-store guards:** A058, T126-T128, T212, T218, T223 and T247 are undefended.
- **Remaining survivors:** the cosmetic and boundary-tie ones from the vacuity run, and the 390 legacy survivors.
- Detail and probe files for all of these are in `mutation.md` and `raw/probes/`.

For future waves, the full matrix I ran is `run5.py` and `run5x.py` in `aegis/omega5/mutation/`. They already take a mutant file, so you can feed them mutants generated only for the changed files.
