# AEGIS PROVENANCE examiner (omega13): AG-EVIDENCE-PROV v1.1.0

- repoIdentity: DarksiedCEO/zbm-zbc (origin https://github.com/DarksiedCEO/zbm-zbc.git). S/ooh2 is a worktree of <HOME>/PycharmProjects/zbm-zbc, so both share one object store.
- candidateSha: cb708b924bb01aa5543b832573a8fa56bd041b80
- treeOid: eba95097622438b226169c88910c81cc9a999212
- baseSha (range base): 75b0662823b867df3325d676b1dcf69abe37a425
- main: 9531fc253710d8e8402fc13bebb5bb29abfcbd52
- scope: items 1–6 of the provenance brief. Read-only object queries only; no repo writes. S = <scratchpad>; my side files are in S/pv13.
- Fresh context. I did not build any of this work.

## 1. Identity, ancestry, remote state — VERIFIED
- `git rev-parse cb708b9 'cb708b9^{tree}' HEAD` (S/ooh2) gives cb708b924bb01aa5543b832573a8fa56bd041b80 / eba95097622438b226169c88910c81cc9a999212 / HEAD = the candidate. Branch is ooh-dept-15-wave2. `git cat-file -p cb708b9`: its tree is eba950…, its only parent is 5ac1909484b60e2a8e8c5c3524578d8ae0a6a2b2, and author/committer is Andre, 2026-10-18 16:59:21 -0700.
- `git rev-list --first-parent --reverse 75b0662..cb708b9` gives da7b960, eae7a86, 8b613e3, 99569bd, 010ef48, 49a7f3a, 91ea9a1, 0f5fbe3, cd1924c, f5ee168, 6daccf6, 1a90498, 5ac1909, cb708b9. That is 14 commits and **matches the expected list exactly**. `git rev-list --merges 75b0662..cb708b9` gives 0, so the history is linear.
- `git merge-base --is-ancestor X cb708b9` returns true for 99569bd85a791c102c6df293d7e987743813644b, 91ea9a1948253a3203150b38a8c2f2f839f232cd, 0f5fbe33642a62068239f4df83edd8602730fd8e, f5ee168c9e9148241b709ec93acc660c88d08ba7, 1a904983d6e064b912bbd95a7afa1668da6c50c9 and 75b0662823b867df3325d676b1dcf69abe37a425.
- main is unchanged:
  - In the main repo, `git rev-parse main origin/main` = 9531fc253710d8e8402fc13bebb5bb29abfcbd52 for both, and `git ls-remote origin` shows the same value for refs/heads/main.
  - `git merge-base cb708b9 main` = 9531fc25…, which is main itself. main..cb708b9 = 558 commits; cb708b9..main = 0.
- Not on any remote:
  - `git branch -r --contains cb708b9` returns nothing, in both S/ooh2 and the main repo.
  - Live `git ls-remote origin` returned 105 refs (103 unique objects, all present locally). None of the 14 range commits is a ref tip, and none of the 103 objects has da7b960 as an ancestor.
  - Remote has no refs/heads/ooh-dept-15-wave2. Only refs/heads/ooh-dept-15-wave0 exists (84a5377), and 75b0662 is not its ancestor.
- The worktree S/ooh2 is clean: `git status --porcelain` gives 0 lines.

## 2. Blob OIDs at cb708b9 and the frozen copy — VERIFIED
The four source files are under services/ooh-py/**src/** (the brief's services/ooh-py/<file> paths do not exist at cb708b9; `git rev-parse` gives "fatal: path … does not exist").

| path | blob (git rev-parse cb708b9:path) | lines | frozen copy (git hash-object) | S/ooh2 worktree |
|---|---|---|---|---|
| services/ooh-py/src/pii.py | b0f8530af7027da3d420969236105e71f8f2a4d5 | 394 | MATCH | MATCH |
| services/ooh-py/src/service.py | 6c7233e2fbe6d503bd31b9ffe32596a993e2e4af | 623 | MATCH | MATCH |
| services/ooh-py/src/svc_campaigns.py | f5291986d87b3207a95afff7a1e3d57407357039 | 1387 | MATCH | MATCH |
| services/ooh-py/src/store.py | 8d81ac82ea9d5252e3b529a5c7ac77ffabc2e032 | 508 | MATCH | MATCH |
| services/ooh-py/tests/test_ooh2_pii1_card_ssn.py | 2eac9190df1692620fa88a22915613c1da1c2d76 | 192 | MATCH | MATCH |
| services/ooh-py/tests/test_aegis_r9_pii_hardening.py | a1773069f40662a72c1f9a933c13f9babef6694d | 217 | MATCH | MATCH |
| services/ooh-py/tests/test_aegis_r10_screen_form.py | b5e703c7a9b4196968ef66a7f68b133ee213a46a | 111 | MATCH | MATCH |
| services/ooh-py/tests/test_ooh2_pii1_cluster_rule.py | 7cb0c99873ad574f56d4780017f15798bd02fa34 | 266 | MATCH | MATCH |
| services/ooh-py/tests/test_aegis_r12_mutation_gaps.py | f3e162a0051c8a1390d0d14e3cbf075b3ab649f7 | 76 | MATCH | MATCH |
| services/ooh-py/tests/test_aegis_h1_file_content.py | 0d19050c50c66d00eddcfdbc77d201db7c11e9c9 | 180 | MATCH | MATCH |
| services/ooh-py/tests/test_aegis_h1_edges.py | be032a2211b2b60ae4771c21169fd6a394e83682 | 265 | MATCH | MATCH |

Line counts come from `git cat-file -p <blob> | wc -l`.

The frozen copy S/review-cb708b9 is a git worktree (.git → <HOME>/PycharmProjects/zbm-zbc/.git/worktrees/review-cb708b9):
- `git rev-parse HEAD 'HEAD^{tree}'` = cb708b924bb0… / eba95097…
- `git status --porcelain` gives 0 lines.

## 3. Code delta and the patch files — VERIFIED (scoped to services/ooh-py)
- 1a90498..cb708b9 changes 363 files: 3 under services/ooh-py, all others docs (357 docs/aegis, plus docs/adr/0022…, docs/findings, docs/test-counts.md).
  - The 3 code files are src/pii.py (+88/−32, the only src change), tests/test_aegis_r12_mutation_gaps.py and tests/test_ooh2_pii1_cluster_rule.py.
  - services/ledger-rust is unchanged (`git diff --quiet` rc 0; subtree 4f1ab90e… at both ends).
  - Subtree services/ooh-py/src at cb708b9 = ff25b0f321148c19e28235eebdce7022c38ab804, which matches what runtime.md states.
- 99569bd..cb708b9 changes 2150 files: 13 under services/ooh-py, the rest docs.
  - The 13 are src/pii.py, service.py, store.py, svc_campaigns.py, svc_inventory.py, plus tests aegis_h1_edges, aegis_ooh_final, aegis_ooh_w234, aegis_r10_screen_form, aegis_r12_mutation_gaps, aegis_r9_pii_hardening, ooh2_pii1_card_ssn and ooh2_pii1_cluster_rule.
- sha256 of the patches (from `shasum -a 256` and `git diff … | shasum -a 256`):
  - code-delta-1a90498..cb708b9.patch = 61a3b137979c6a270d75a6dc2739cc87eaf2d88ce39a7f4ec344cee135eea395. This **equals** `git diff 1a90498 cb708b9 -- services/ooh-py`, and also `-- services/ooh-py/src services/ooh-py/tests`.
  - code-delta-99569bd..cb708b9.patch = 49ab1a8b69904c27dd6dbbcb35895b37a875b3f456f2da47214e29707116b431. This **equals** `git diff 99569bd cb708b9 -- services/ooh-py`.
  - Neither equals the unrestricted `git diff`, which includes docs: 27dcc9ee…acbd01 for 1a90498 and 31a9a499…508ba8 for 99569bd. See P-L1.

## 4. Path safety — VERIFIED with Low notes
- 75b0662..cb708b9 changes 3230 paths. Modes (`git diff --raw`): 3213 `:000000 100644 A` and 17 `:100644 100644 M`. That means no symlinks (120000), no submodules (160000), no 100755 and no mode changes. Deletions: none.
- Outside services/ooh-py/** and docs/**: one file, **claude/handoff-2026-10-18-ooh-wave2.md**, added in eae7a86. It is not in 1a90498..cb708b9 or 99569bd..cb708b9, which touch only services/ooh-py/** and docs/** (0 outside).
- Size: nothing over 1 MB (`git cat-file -s` on every added/modified path). The three largest:
  - docs/aegis/ooh-wave2/omega12/runtime/runs/pii_c1a90498_3.13/result.json, 954947 bytes
  - docs/aegis/ooh-wave2/omega12/runtime/runs/pii_c1a90498_3.12/result.json, 954938 bytes
  - docs/aegis/ooh-wave2/omega8/security/diff-8b613e3.patch, 879121 bytes
- Names: none absolute, none contain `..`, none touch `.git/`, no control characters. Five contain a space: docs/aegis/ooh-wave2/omega5/runtime/disclosed/wr_zsh_split/"wr_3.12 13_.out", "wr_3.12 1_.out", "wr_3.12 7_.out", "wr_3.13 1_.out" and "wr_3.13 7_.out".

## 5. Evidence-bundle digests and subject binding
`shasum -a 256 S/aegis/omega13/*.md`:

| report | sha256 | expected | header candidate / tree | mtime (Oct 18) |
|---|---|---|---|---|
| source.md | 6b23dc73c218081aca46c120e29c73d48b8f5cb59ab513a4d97fdce31b117b7f | MATCH | cb708b924bb0…b80 / eba95097… (lines 3–4) | 17:43:32 |
| security.md | a6a219d5ba24aa87c0e93172a0917f4c13fefde268c9e081a890f2f311dae8ff | MATCH | full SHA / tree (lines 4–5) | 17:50:54 |
| reliability.md | a682fb3a996e060f8f494ec7b8c23d69a914264e03f06283943a34db5e4e534f | MATCH | full SHA / tree (lines 2–3) | 18:11:13 |
| data.md | bf2a6a4ce60fae4fc1b87548a55f5768cb22e9bbf28ff7944ee0ce29978e5453 | MATCH | full SHA / tree (lines 3–4) | 18:34:04 |
| runtime.md | de07e8538a46517721b580131425f3a13b0a7c04909762c44411774b62d6d6df | MATCH | full SHA / tree (line 3) | 18:49:06 |
| mutation.md | faa98c2e4bd4bb6abeb9fb126e5bbb8a9ab4c9afb54a8d214ee874b5d52a50b3 | MATCH | full SHA / tree (lines 4–5) | 18:41:50 |
| mutation-r2.md | c87a1a347a1d93303fca86ae772aed16f3f1eaf18bb6ba7da56b1b9fec5669c1 | MATCH | full SHA / tree (lines 2–3) | 20:42:55 |
| test-verification.md | — | — | **NOT PRESENT** at examination time. Only test-verification/raw (full_3.12.txt, full_3.13.txt, 20:46) and work/ exist. NOT CHECKED. | — |

- Every present report names the full candidate SHA and the full tree in its header. None names a different SHA as its subject. Other SHAs appear in the headers only as labelled controls or comparisons: 1a90498, 75b0662, 99569bd, f5ee168, 0f5fbe3 and 91ea9a1, all of which are ancestors (see §1).
- Cross-binding: mutation-r2.md line 5 cites mutation.md sha256 faa98c2e…50b3, which equals the current file. So mutation.md has not changed since mutation-r2 used it.
- Every report mtime is after the candidate's commit time (16:59:21). I found no sign of post-hoc rewrite. This rests on digest equality with the caller's expected values plus mtimes; mtimes alone cannot prove a negative.
- Checks of each claim's denominator, collection method and execution conditions inside the reports: **NOT CHECKED** (outside the brief's items).

## 6. gitleaks 8.30.1 on 75b0662..cb708b9 — 0 findings
- Binary: S/gl/gitleaks reports `version` 8.30.1, sha256 ba52fb1b…eb6a5. S/gl/gl.tgz sha256 b40ab0ae…6a5 equals the darwin_arm64 line in S/gl/sums.txt. I did not extract the tgz again to tie it to the binary.
- Run A, the repo config. The repo's .gitleaks.toml was used automatically: useDefault plus exact-secret allowlists only, and it is unchanged in the range.
  - Command: `gitleaks git --log-opts="75b0662..cb708b9" --no-banner --redact --report-format json --report-path S/pv13/gl-repo-config.json S/ooh2`
  - Result: rc 0, "14 commits scanned", ~42.63 MB, "no leaks found", report holds 0 entries.
- Run B, default rules only, with no repo allowlist (`--config S/pv13/default-only.toml` = `[extend] useDefault = true`, plus `-v`).
  - Result: rc 0, 14 commits, "no leaks found", 0 entries.
- The committed test card/SSN data is **not flagged**, because gitleaks' default ruleset has no card-number or SSN rule. So gitleaks cannot list it by file or rule. I did not inventory it here (NOT CHECKED). It remains test data by founder ruling.

## Findings
- **P-L1 (Low)**: the "code-delta" patches are path-limited to `-- services/ooh-py`. They do not equal the unrestricted `git diff` at those ranges, which also includes docs.
  - Reproducer: run `git diff 1a90498 cb708b9 | shasum -a 256`, then the same with `-- services/ooh-py`.
  - Falsifier: if the unrestricted digest equaled the file's digest, this finding would be wrong.
  - The pathspec is not stated in the filename.
- **P-L2 (Low)**: claude/handoff-2026-10-18-ooh-wave2.md (eae7a86) is outside services/ooh-py/** and docs/** in the 75b0662..cb708b9 range. It is not in the 1a90498 or 99569bd code deltas.
  - Reproducer: `git diff --name-only 75b0662 cb708b9 | grep -vE '^(services/ooh-py/|docs/)'`.
  - Falsifier: empty output.
- **P-L3 (Low)**: five evidence filenames under docs/aegis/ooh-wave2/omega5/runtime/disclosed/wr_zsh_split/ contain spaces. That is harmless to git but fragile for unquoted shell tooling.
- **P-L4 (Low/info)**: two committed result.json files are 954,9xx bytes, which is within 5% of the 1 MB limit.
- **NOT CHECKED**:
  - test-verification.md (not yet written). AEGIS must bind it separately and recompute its digest.
  - Denominator, method and conditions for each claim inside the reports.
  - An inventory of card/SSN test data.
  - Re-extracting gl.tgz to compare it with the gitleaks binary.

No Critical, High or Medium findings. No MISMATCH and no MISSING among the checked items.

## Verdict
**PROVENANCE_GREEN** for the scoped claim: items 1–6 at {DarksiedCEO/zbm-zbc, cb708b924bb01aa5543b832573a8fa56bd041b80, eba95097622438b226169c88910c81cc9a999212, base 75b0662}, covering the seven reports listed in §5. It excludes test-verification.md, which was not present. blocking: false. Any change to the SHA or tree, or to any listed report digest, voids this verdict.
