# AEGIS PROVENANCE at 99569bd (transcribed)

> Transcription note: the provenance examiner's contract forbids writes. This is its final hand-back of 2026-10-18,
> copied by the builder without edits to its findings.

candidateSha: 99569bd85a791c102c6df293d7e987743813644b
treeOid: 7d08f34836a7b4188ecd8d46906cfa38ecfd23ae
base: 84a5377df4892efcb4cb1c221a6862075fe0e018

RESULT: PROVENANCE_PASS (items 1-6 all PASS). Gate verdicts as written (for AEGIS, not the examiner's judgement):
source, security, reliability, runtime GREEN; mutation and test-verification BLOCK (about the code tested, not
provenance).

## 1. Identity and ancestry: PASS
- ooh2 worktree: `git rev-parse HEAD ooh-dept-15-wave2 99569bd^{tree}` → 99569bd85a79…, 99569bd85a79…, 7d08f34836a7…; porcelain 0. Frozen review-99569bd: same HEAD/tree, porcelain 0.
- `git rev-list --parents 84a5377..99569bd`: 30 commits, each single-parent, `--merges` 0. Chain 99569bd → 8b613e3b → eae7a86a → da7b960f → 75b06628 → ce665c5b → … → 58eb7de5 → 84a5377d.
- `merge-base 99569bd integration-2026-09-24` = 84a5377df4892efcb4cb1c221a6862075fe0e018 (= its tip); `--is-ancestor` OK.
- ooh-pii-fix at 010ef48 is not the candidate; none of the six reports mentions it.

## 2. Header binding: PASS for all six
| Report | Author line |
|---|---|
| source.md | AEGIS-SOURCE reviewer agent |
| security.md | AG-AEGIS-SECURITY v1.0.0 |
| reliability.md | AG-RELIABILITY (Claude Sonnet 5.5) |
| runtime.md | AG-AEGIS-RUNTIME |
| test-verification.md | AG-TEST-VER |
| mutation.md | AEGIS MUTATION gate (omega8) |
All name the full candidate SHA and tree. Other SHAs appear only as history or controls (75b0662, 84a5377, da7b960, eae7a86, 8b613e3, ce665c5). runtime.md object ids checked: ooh-py/src subtree bf1cb95f8a8c… = `git rev-parse 99569bd:services/ooh-py/src`; ledger-rust 4f1ab90e2f81… (same at 99569bd and 75b0662); store.py blob 35a439c8…; 75b0662 tree 67c15180 and src 6d8a4442 match.

## 3. Report hashes: PASS
- source.md cd692c62051079da66c6ad9e661ac68e74539165c3720f231c3004b559957bad (matches hand-back)
- security.md 95de610f8b0d5feb921f749ecda827c2ce6c7dd86a9d1f982fc4515faaff62fa (matches)
- runtime.md 00166c217398569f6124d0237bf2fc3c162c99ccbeb6e924d002ccca4f48c659 (matches)
- mutation.md 75948357afe2f2f642162469716d2891871f6c409dcb4d3d22ea6fc3278e4a76 (matches)
- reliability.md 792fc54956bee5ace69f8edcd8d746436bb70d28e1e4f7367133155a591e63f5 (the garbled hand-back prefix 792fc54956bee5ace69f8edcd8 matches)
- test-verification.md now 0f85f2b119be3b154b5c42d3b3c041dce311dd9591857f3101cf2e48656be18e: a section "## Update after the mutation report" was appended at line 75 (file created 06:27:43, modified 07:27:27); `head -n 73 | shasum -a 256` = bb3690222d2440be04168269fe2b4e61cc9a58c5806b42d3a440989f8a31b68b, the original hand-back value, so the original findings are unchanged; append only, requested; postHocRewrite=false.

## 4. Diff digests: PASS
- full-diff-84a5377..99569bd.patch 204eb2cd590bd9d01518a67c352bd759c6e1b9a31850fe7d16314ee7a1b1c56b = `git diff 84a5377 99569bd`
- delta-75b0662..99569bd.patch 0cbdf6745b5a74dc7aa3f12f317a63ab7d6139e2e0bd6cc36daeef9671bc8aae = `git diff 75b0662 99569bd`
- code-delta-75b0662..99569bd.patch 5bf24f4d337937ade4cc0e98f0e5f67df83dfc4cc6d89fd275f604dda20189a6 = `git diff 75b0662 99569bd -- services` (same with `-- services/ooh-py`)
- commits.txt 6936aadafb9a89e17626e3830f97e4ee2c1d40be307823d5ddb32a6623b02334 = `git log --format='%H %T %s' 84a5377..99569bd` (30 lines)
- diff-stat.txt d644ea866b360f0c09677d638ed8a2eca027f2ec57219f927cd83ee7253c5cae = `git diff --stat 84a5377 99569bd` (1124 files, +167940/−225)

## 5. Scope: PASS
- `git diff --stat 75b0662 99569bd -- services`: services/ooh-py/src/service.py (+2), store.py (69), tests/test_aegis_h1_file_content.py (+180, new); 226+, 25−.
- Per commit: da7b960 1071 files, all docs; eae7a86 1 claude + 1 docs; 8b613e3 1 claude + 332 docs; 99569bd those 3 services files + ADR 0022, OPEN.md, test-counts.md.
- Blobs at 99569bd: service.py a81ab0a8…, store.py 35a439c8…, test 0d19050c….

## 6. Path safety: PASS
`find aegis/omega8 -type l`: 0 symlinks across about 4500 files.

subjectBinding: all six match. postHocRewrite: false. blocking: false.

PROVENANCE_PASS
