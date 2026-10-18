# AEGIS SECURITY gate — omega8 (H-1 fix re-review)

- repoIdentity: DarksiedCEO/zbm-zbc, local branch ooh-dept-15-wave2
- candidateSha: 99569bd85a791c102c6df293d7e987743813644b
- treeOid: 7d08f34836a7b4188ecd8d46906cfa38ecfd23ae
- author: AG-AEGIS-SECURITY v1.0.0 (Claude Opus 5.5, fresh context, read-only on the candidate), 2026-10-18
- Scope: `git diff 75b0662 99569bd -- services` (services/ooh-py/src/store.py, service.py, tests/test_aegis_h1_file_content.py); gitleaks over 84a5377..99569bd and the frozen tree; personal-data check of 8b613e3 and 99569bd.
- Frozen worktree review-99569bd: HEAD 99569bd…, tree 7d08f348…, `git status --porcelain` empty at start and at end (0 lines both times). All experiments ran in a copy (`git archive 99569bd | tar -x` into scratchpad/sec8/tree; 75b0662 copy in sec8/tree75). PoC tests were added to the copies only.
- Raw outputs, PoC scripts: scratchpad/aegis/omega8/security/

## Verdict: SECURITY_GREEN (scoped claim). Non-blocking findings: 1 MEDIUM, 2 LOW, 2 INFO. No CRITICAL or HIGH.

H-1 holds for every file change I tried: no write committed over a file that differed from memory, and file_problem never said ok on a file that differed from memory. The one exception is the race window inside append_prepared (L-1). It comes from the design, it is not worse in kind than before, and the next write or check catches it.

## Attack matrix (H-1 boundary): a 3-line log, mutate, then `file_problem()` and `append()`
Command: `venv3.12/bin/python -I security/matrix.py <copy>/services/ooh-py/src <sandbox data>` (repeated on venv3.13: identical). Output `security/poc-matrix.txt`, run as euid 501:

```
same_size_edit         file!=mem=True  file_problem=PROBLEM write=REFUSED(disagree)
grow_junk              file!=mem=True  file_problem=PROBLEM write=REFUSED(disagree)
shrink                 file!=mem=True  file_problem=PROBLEM write=REFUSED(disagree)
prefix_edit            file!=mem=True  file_problem=PROBLEM write=REFUSED(disagree)
tail_edit              file!=mem=True  file_problem=PROBLEM write=REFUSED(disagree)
tail_append_fake_next  file!=mem=True  file_problem=PROBLEM write=REFUSED(disagree)
symlink_same_bytes     file!=mem=False file_problem=ok      write=REFUSED(open failed: OSError ELOOP)   <- L-2
symlink_diff_bytes     file!=mem=True  file_problem=PROBLEM write=REFUSED(open failed: OSError)
hardlink_edit          file!=mem=True  file_problem=PROBLEM write=REFUSED(disagree)
replaced_inode_same    file!=mem=False file_problem=ok      write=COMMITTED (bytes equal: correct)
replaced_inode_diff    file!=mem=True  file_problem=PROBLEM write=REFUSED(disagree)
deleted                file!=mem=True  file_problem=PROBLEM write=REFUSED(disagree)
truncated_to_0         file!=mem=True  file_problem=PROBLEM write=REFUSED(disagree)
chmod_000_file         file!=mem=True  file_problem=PROBLEM write=REFUSED(open failed: PermissionError)
chmod_000_dir          file!=mem=True  file_problem=PROBLEM write=REFUSED(open failed: PermissionError)
edit_then_restore      file!=mem=False file_problem=ok      write=COMMITTED (bytes equal: correct)
```
At the service level the H-1 tests in the candidate pass: `tests/test_aegis_h1_file_content.py` and `test_aegis_store_write_path.py`, 28 passed on 3.12 and 28 on 3.13 (`security/pytest-h1.txt`).

## Findings

### M-1 (MEDIUM, NEW in this delta, non-blocking): the integrity check opens the log without a type check and reads it with no bound, while holding both locks. A FIFO hangs the whole service; a symlink to /dev/zero runs memory without limit. Class: DoS (PATH_ESCAPE family; the same class as the V5-I1 FIFO precedent)
- Path: services/ooh-py/src/store.py:237-242 (`file_problem`: `with self.lock:` then `open(self.path, "rb")` and `fh.read()`; no O_NOFOLLOW, no O_NONBLOCK, no S_ISREG check, no size cap). It is called from services/ooh-py/src/service.py:336 inside `verify_integrity`'s `with self.lock:` (service.py:310). The callers are the scheduler `POST /ooh/v1/jobs/integrity/run` and the dashboard/compliance `GET /ooh/v1/audit/integrity` (api.py:673-681).
- Why it is new: at 75b0662 no runtime path opened the log for reading. `RecordLog.verify()` was never called from src (`grep -rn "log.verify" tree75/services/ooh-py/src`: no output). The write path uses O_NOFOLLOW and checks the size before it reads, so it was not affected.
- PoC 1 (service level, sandbox copy): `security/test_zz_aegis_sec8_poc.py`. It makes 2 log lines, runs `os.unlink(log); os.mkfifo(log)`, starts the scheduler integrity job, and 1 s later a POST /tenants and a GET /audit/integrity. Command: `cd sec8/tree/services/ooh-py && venv3.12/bin/python -m pytest -q -s -p no:cacheprovider -W ignore tests/test_zz_aegis_sec8_poc.py`
  - Observed at 99569bd: `after 4 s: job alive True | write alive True | audit alive True {}`. After a writer opened and closed the FIFO: `{'write': 503, 'job': 200, 'audit': 200}`. Result: 1 passed, on both 3.12 and 3.13 (`poc-fifo-service.txt`, `poc-fifo-service-py313.txt`). The hang has no end: it lasts until something opens the FIFO for writing.
  - Same test at 75b0662: `job alive False | write alive False | audit alive False {'job': 200, 'audit': 200, 'write': 503}`, so the test failed there, as expected (`poc-fifo-service-75b0662.txt`). The FIFO regression is therefore shown.
- PoC 2 (store level): `security/dos.py devzero`. It replaces the log with a symlink to /dev/zero and calls `file_problem()`. Observed: `file_problem reading /dev/zero still running after 4 s: True maxrss MiB 16465.25`, about 16 GiB RSS in 4 s, with the store lock held (`poc-dos.txt`).
- PoC 3: a sparse file of 1 GiB (no disk used). `file_problem` read it in 0.12 s, RSS 20 -> 1044 MiB; append refused in 0.0001 s because the size check comes first (`poc-dos.txt`).
- Precondition: write access to OOH_DATA_DIR, the same actor as H-1. Such an actor can already make the service fail closed, so the extra power is: an indefinite hang of every route that takes the service lock instead of a 503, and memory exhaustion on the host, where before there was fail-closed behaviour. Graded MEDIUM, not HIGH: no integrity lie and no write over a changed file. The startup read in `RecordLog.__init__` (store.py:115-116) has the same unbounded open, but that was already there before.
- Reproducer: the commands above.
- Falsifier: `file_problem` opens with `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`, rejects anything that is not S_ISREG via fstat, and compares `st_size` with `len(memory)` before it reads. Then PoC 1 shows `job alive False`, PoC 2 returns a problem string at once, and PoC 3 shows no RSS growth.

### L-1 (LOW, THEORETICAL in nature, shown by deterministic injection): a TOCTOU between the compare read and the pwrite. Class: TOCTOU
- Path: store.py:201 (`_pread_exact(fd, expected, 0) != mem`) to store.py:209 (`os.pwrite`). The store lock is a thread lock inside the process. It does not bind another process that has write access to the data directory, and no advisory lock would bind such a process either.
- PoC: `security/toctou.py`. It wraps `store._pread_exact` so that an external same-size edit of line 1 happens right after the whole-file compare read returns. Observed (3.12 and 3.13, `poc-toctou.txt`):
  ```
  write committed seq 4 | file == memory: False
  file_problem after the race: the local log file differs from the lines this service holds: an operator must inspect it
  next write REFUSED: the log file and memory disagree; refusing to write
  ```
- Assessment: one write can commit over a file that was just changed, but the next integrity check or the next write reports it. Compared with before: the window is now wider, because the whole file is read inside it (about 8 ms at 20k lines / 23.6 MiB, `poc-cost.txt`), where before it was an fstat. But before the fix the same edit was never caught while the service ran, so the net effect is a large improvement. A real concurrent race (no injection) was NOT RUN.
- Falsifier: a real cross-process race in which the edit lands in the window and is still not reported by the next `file_problem` or append. Not observed.

### L-2 (LOW): integrity says ok while the log is a symlink. file_problem follows the link; the write path refuses it. Class: PATH_ESCAPE (consistency)
- Path: store.py:241 (`open()` follows symlinks) against store.py:195 (`O_NOFOLLOW`).
- PoC: matrix row `symlink_same_bytes`. The log was replaced by a symlink to a copy with the same bytes: `file_problem=ok`, `write=REFUSED(local log open failed: OSError)`. So the audit and job routes report `integrity.ok: true` while every write answers 503 STORE_UNAVAILABLE. Writes fail closed, and file_problem still compares the target's content, so this is not a case of ok on a file that differs from memory.
- Falsifier: file_problem opens with O_NOFOLLOW, and `symlink_same_bytes` then reports a problem.

### INFO-1: cost of a whole-file read per write. Not graded as a finding.
`security/cost.py`, 1 KB records: 1k lines / 1.2 MiB took 0.31 ms per append; 5k / 5.9 MiB took 2.11 ms; 20k / 23.6 MiB took 8.50 ms (file_problem about 7.8 ms). The cost is linear in log size and the docstring says so. Only callers who can write can add lines, and the integrity routes are limited by FORCED_MIN_S=10.

### INFO-2: a deleted log comes back as an empty file
When the log is deleted while lines are in memory, `append_prepared` still `O_CREAT`s an empty 0600 file before it refuses (store.py:195). This was already the case at 75b0662. The ledger anchors catch it at restart (the documented design). Not exercised past one refusal.

## Boundaries tested (denominator: 13 declared / 13 tested)
| boundary | tested | method |
|---|---|---|
| same-size in-place edit (first/middle/last, prefix, tail) | yes | matrix + candidate H-1 tests |
| different-size (grow, shrink, junk tail, fake next line) | yes | matrix |
| symlink (same and different bytes) | yes | matrix |
| hard-link edit | yes | matrix |
| replaced inode via rename (same and different bytes) | yes | matrix |
| deleted file | yes | matrix |
| truncated to 0 | yes | matrix |
| FIFO / special file | yes | dos.py fifo + service-level pytest PoC (99569bd and 75b0662) |
| /dev/zero symlink, sparse file | yes | dos.py |
| permissions (file 000, dir 000) | yes | matrix (euid 501, not root) |
| TOCTOU compare->pwrite | yes (injected) | toctou.py |
| whole-file-read DoS | yes | cost.py |
| secrets / personal data in the delta and history | yes | gitleaks git/dir, git grep, regex scan |

## gitleaks 8.30.1 (repo .gitleaks.toml)
- `gitleaks git --config .gitleaks.toml --log-opts="84a5377..99569bd" --redact -v .`: 30 commits scanned, about 11.96 MB, **no leaks found**, exit 0 (`gitleaks-git.txt/.json`). `git rev-list --count 84a5377..99569bd` = 30.
- `gitleaks dir --config .gitleaks.toml --redact review-99569bd`: about 30.80 MB, **no leaks found**, exit 0 (`gitleaks-dir.txt/.json`).

## Personal data (8b613e3, 99569bd)
- `git grep -i <USER> 99569bd -- docs claude`: no matches (exit 1). Whole tree at 99569bd: no matches (exit 1). `<HOME>` is present in 332 files under docs/ and claude/. The only remaining `/Users/` string is `docs/ci.md:277` `/Users/runner/work/_temp`, a GitHub runner path, not personal (`pii-username.txt`).
- Regex scan of the lines each commit adds (8b613e3: 829 lines; 99569bd: 220 lines; `pii-diff-scan.txt`):
  - e-mail: 8b613e3 none. 99569bd one hit, `@pytest.mark.parametrize`, a false positive.
  - phone: none in either.
  - SSN shape: 8b613e3 has 3 lines, all the text `SSN 123-45-6789` inside docs prose: the evidence note and the OOH2-PII-1 row in OPEN.md. That number is the well-known reserved/advertising test SSN, so this is **a test fixture, not real data**. 99569bd: none.
  - card shape (13-19 digits): none in either.
- Commit identities in the range: 30 x `Andre <<OWNER-EMAIL>>` as both author and committer, the owner's own identity.
- Note for whoever pushes (not a finding of this gate): the commit tree at HEAD is clean, but the earlier commits da7b960 (330 files) and eae7a86 (332 files) still contain the home path, and so do 7 files in each of 84a5377..75b0662. Per the local remote refs (not fetched), 84a5377 is already on origin/integration-2026-09-24 and origin/ooh-dept-15-wave0, so those 7 docs/aegis/round-26b files are already public in history. da7b960 and eae7a86 are not on any remote ref, so pushing the branch as it is publishes them in history (`pii-history.txt`).

## NOT RUN
- A real cross-process race for L-1 without injection; only a deterministic interleaving was run.
- Linux (all PoCs ran on macOS / Darwin 25.6); FIFO and O_RDWR semantics differ on Linux.
- A process running as root (the permission rows do not hold for root).
- The live uvicorn server: the service-level PoC used the repo's TestClient harness.
- The full ooh-py test suite (only the H-1 and store-write-path files ran) and any other service: H-1 in the 5 other services is out of scope.
- OOH2-PII-1, SEC L-1 and L-3: not re-litigated, per the instructions.
- The lock-file (ooh.lock) path: not in the delta.

## Gate object
{candidateSha: 99569bd85a791c102c6df293d7e987743813644b, treeOid: 7d08f34836a7b4188ecd8d46906cfa38ecfd23ae, gate: SECURITY, verdict: SECURITY_GREEN, blocking: false, findings: [M-1 DoS store.py:237-242 (NEW), L-1 TOCTOU store.py:201-209, L-2 PATH_ESCAPE store.py:241 vs 195], denominator: {boundariesDeclared: 13, boundariesTested: 13}}
