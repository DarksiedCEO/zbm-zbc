# AEGIS PROVENANCE at 4a0a4b4 (transcribed)

> Transcription note: the provenance examiner has no write tool. This is its final hand-back of 2026-10-18,
> copied by the builder without edits to its findings.

candidateSha: 4a0a4b478a88fbaaeb864a7535779e586b39f64a
treeOid: e0640db37e4a40f58e0a291fd21d90a56f45d1ec
base: 84a5377df4892efcb4cb1c221a6862075fe0e018 (integration-2026-09-24 tip, local and origin)

RESULT: PROVENANCE_PASS (items 1-6 all PASS; none failing)

## 1. Commit, tree and ancestry: PASS
- `git rev-parse 4a0a4b4 4a0a4b4^{tree}` → 4a0a4b478a88fbaaeb864a7535779e586b39f64a, e0640db37e4a40f58e0a291fd21d90a56f45d1ec.
- `git cat-file -p 4a0a4b4`: tree e0640db…, single parent 5116917b852f6b6df60424ceef33b9df925f03ae, committer 2026-10-17T20:53:17-07:00.
- `git merge-base --is-ancestor 84a5377 4a0a4b4` and `--is-ancestor 4a0a4b4 75b0662` both succeed.
- `git rev-list --parents 84a5377..75b0662`: 26 commits, each 1 parent; `--merges` gives 0. Top of chain 4a0a4b4 → ce665c5b3aef1bcd56aacee9523559150dd8cb7b → 75b0662. Linear.
- `git merge-base 84a5377 4a0a4b4` = 84a5377df489… (fork point).
- Frozen copy review-4a0a4b4: HEAD/tree 4a0a4b47…/e0640db3…, porcelain 0 lines.

## 2. Header binding: PASS
| Report | Candidate | Tree | Who wrote it |
|---|---|---|---|
| source.md | full 4a0a4b47… | e0640db… | Transcribed by the builder (line 5) |
| security.md | full | e0640db… | The gate itself (line 3) |
| data.md | full | e0640db… | The gate itself (line 3) |
| reliability.md | full | e0640db… | Transcribed by the builder (line 5) |
| runtime.md | full | e0640db… | The gate itself (line 15) |
| mutation.md | full | e0640db… | Says only "Author: AEGIS-MUTATION-FUZZ (independent reviewer, fresh context)"; does not say who wrote the file |
| mutation-rgate.md | full | e0640db… | Transcribed by the builder; contains a separately marked builder's factual note |

- runtime.md subtree OIDs ledger-rust 4f1ab90e2f81245130d6eb87091745146f6398e6 and ooh-py/src 6d8a444233eed6ac29bcbdca37417c70e5002acc match `git rev-parse 4a0a4b4:<path>`.
- No report binds to a different candidate. Other SHAs (84a5377, 5116917, e680f98, d1b0de9, ce665c5, 75b0662) are labelled history/base references, each prefix unambiguous. f264db5b63779e8863b5ca4111e8 in security.md is an event_id, not a git object.

## 3. Diff digests and listings: PASS
- full-diff-84a5377..4a0a4b4.patch sha256 5ba46009c6217e7c990e5f76e736579bb0c8e872a00a2925209ed5db5de5f854 = fresh `git diff 84a5377 4a0a4b4`.
- delta-5116917..4a0a4b4.patch sha256 ee71cd6f24cf576d8d4322775b73f225f7070914b3317d48820e0f004d79a691 = fresh `git diff 5116917 4a0a4b4`.
- commits.txt (24 lines) byte-identical to `git log --format='%H %T %s' 84a5377..4a0a4b4` (sha256 7cbc5314…ebb72).
- diff-stat.txt byte-identical to `git diff --stat 84a5377 4a0a4b4` (sha256 e176c5f7…673c): 41 files, 6891 insertions, 179 deletions.

## 4. Source identity 4a0a4b4 vs 75b0662: PASS
- `git diff --stat 4a0a4b4 75b0662 -- services/ooh-py/src` empty; src tree 6d8a444233eed6ac29bcbdca37417c70e5002acc at both.
- `git diff --name-status 4a0a4b4 75b0662`: M docs/adr/0022-ooh-buy-and-contract-controls.md, M docs/test-counts.md, M services/ooh-py/tests/test_aegis_integrity_path.py, A services/ooh-py/tests/test_aegis_omega5_mutation.py, A services/ooh-py/tests/test_aegis_store_write_path.py (475+, 6-).
- store.py blob bff84c6780b1fcbc8fed57379d23c4235c22eee1 at 4a0a4b4, ce665c5 and 75b0662. The omega7 runtime HIGH on store.py at 75b0662 is about byte-identical content at 4a0a4b4; whether it holds is for Ω.

## 5. Line references: PASS
store.py at 4a0a4b4 has 444 lines.
```
180:                     size = os.fstat(fd).st_size
181:                     if size == expected + len(line) + 1 and os.pread(fd, len(line) + 1, expected) == line + b"\n":
182:                         os.fsync(fd)                  # already on disk: adopt it
183:                     elif size != expected:
184:                         raise StoreWriteError("the log file and memory disagree; refusing to write")
...
261:                 except OSError as exc:
262:                     raise StoreWriteError(f"pending line could not be removed: {type(exc).__name__}") from exc
```
Lines 253-262 are inside clear_pending's `with self.lock:`: unlink pending_path, FileNotFoundError ignored, any other OSError raised as StoreWriteError.

## 6. Path safety: PASS
728 symlinks, all pytest "current" links under data/tmp-3.12 and data/tmp-3.13, all resolving inside the scratchpad; none broken; omega5 itself is not reached through a symlink.

## Notes for Ω (not failures)
- source.md, reliability.md and mutation-rgate.md are builder transcriptions and mutation.md does not say who wrote it; git cannot show a transcription matches what the gate said. The gates' raw outputs are in omega5/source/, omega5/reliability/runs/, omega5/mutation/.
- Timestamps fit the order of events (candidate 20:53 Oct 17; reports 20:57-21:22; mutation.md 00:40; mutation-rgate.md 05:40 local = 12:40 UTC, after the 12:22 UTC hand-back). File mtimes cannot rule out a later rewrite.
- No digest of every file in the folder was taken; only the four listing files were hashed.
