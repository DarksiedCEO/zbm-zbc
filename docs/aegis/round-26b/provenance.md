# PROVENANCE GATE REPORT — AEGIS round 26b re-adjudication

**Gate result: PROVENANCE: PASS** for the tuple below. Every requested fact was confirmed by a direct git or GitHub query; nothing mismatched. Two things you should weigh are flagged under "Observations", and the items I could not verify are listed near the end.

## Subject tuple

| Field | Value |
|---|---|
| repoIdentity | `DarksiedCEO/zbm-zbc` (`origin` = `https://github.com/DarksiedCEO/zbm-zbc.git`, fetch and push) |
| candidateSha | `d80adc5528ddad4f27427098efe2beee32ee5d6a` |
| treeOid | `5c059f868fe4426859c737170ae75ea9a94f351e` |
| parent | `2fc0e82af7f636c7becc3e0e928cddecae172791` |
| baseSha | `a6aee4e9fe78bb58a94243acce406d2e7b702387` (integration-2026-09-24) |
| previous AEGIS candidate | `4c3a21abdb1e777cc6ed1f0ca8a822c4b26d2196` |
| runtimePin | not supplied in the inputs; not checked |

Mode was read-only. All commands ran from `/Users/andrelove/PycharmProjects/zbm-zbc` unless stated. I made no edits, commits, pushes, resets or checkouts.

---

## 1. Remote refs and ancestry — VERIFIED

`git ls-remote origin` (63 refs returned; relevant lines):
```
9531fc253710d8e8402fc13bebb5bb29abfcbd52	HEAD
d80adc5528ddad4f27427098efe2beee32ee5d6a	refs/heads/fix26b
a6aee4e9fe78bb58a94243acce406d2e7b702387	refs/heads/integration-2026-09-24
9531fc253710d8e8402fc13bebb5bb29abfcbd52	refs/heads/main
```

`git rev-parse fix26b origin/fix26b origin/integration-2026-09-24 origin/main`:
```
08a8b8d14e69eef2b8acce74b6568881a5cafea0     (local fix26b, one ahead, not the subject)
d80adc5528ddad4f27427098efe2beee32ee5d6a
a6aee4e9fe78bb58a94243acce406d2e7b702387
9531fc253710d8e8402fc13bebb5bb29abfcbd52
```

`git rev-parse 'd80adc5^{commit}' 'd80adc5^{tree}'`:
```
d80adc5528ddad4f27427098efe2beee32ee5d6a
5c059f868fe4426859c737170ae75ea9a94f351e
```

`git cat-file -p d80adc5 | head`:
```
tree 5c059f868fe4426859c737170ae75ea9a94f351e
parent 2fc0e82af7f636c7becc3e0e928cddecae172791
author Andre <darksiedllc@gmail.com> 1791042594 -0700
committer Andre <darksiedllc@gmail.com> 1791042594 -0700
```

GitHub's own view of the commit and branch agrees:
- `gh api repos/DarksiedCEO/zbm-zbc/commits/d80adc5528ddad4f27427098efe2beee32ee5d6a` returned `{"parents":["2fc0e82af7f636c7becc3e0e928cddecae172791"],"sha":"d80adc5528ddad4f27427098efe2beee32ee5d6a","tree":"5c059f868fe4426859c737170ae75ea9a94f351e"}`
- `gh api repos/DarksiedCEO/zbm-zbc/branches/fix26b` returned `{"name":"fix26b","protected":false,"sha":"d80adc5528ddad4f27427098efe2beee32ee5d6a"}`

Ancestry:
```
git merge-base --is-ancestor a6aee4e d80adc5      -> rc=0
git merge-base a6aee4e d80adc5                    -> a6aee4e9fe78bb58a94243acce406d2e7b702387
git rev-list --count a6aee4e..d80adc5             -> 37
git rev-list --count d80adc5..a6aee4e             -> 0
git rev-list --merges --count a6aee4e..d80adc5    -> 0
git merge-base --is-ancestor 4c3a21a… d80adc5     -> rc=0
git merge-base --is-ancestor 9531fc2 d80adc5      -> rc=0
```
integration-2026-09-24 is a strict ancestor of the candidate with nothing on its side, so a merge into it is fast-forwardable (37 linear commits, no merge commits).

Main is untouched. `gh api 'repos/DarksiedCEO/zbm-zbc/activity?ref=refs/heads/main'` returned its complete history:
```
2026-09-23T00:07:55Z	push	8fc9f5855e2c	9531fc253710	DarksiedCEO
2026-09-22T04:33:12Z	branch_creation	000000000000	8fc9f5855e2c	DarksiedCEO
```
The last ref update on main was 2026-09-23. `gh pr list --state all` returned `[]`.

Activity on the other two refs:
```
integration-2026-09-24:
2026-10-03T04:25:51Z	push	fc19ce790f67	a6aee4e9fe78
2026-10-02T17:53:09Z	push	924eeb411d3b	fc19ce790f67
2026-09-28T04:14:19Z	push	31bbf6fdd973	924eeb411d3b
2026-09-26T18:25:44Z	branch_creation	000000000000	31bbf6fdd973

fix26b:
2026-10-03T16:00:16Z	push	b24ffde28d1c	d80adc5528dd
2026-10-03T14:22:37Z	push	d40de721c799	b24ffde28d1c
2026-10-03T13:09:55Z	branch_creation	000000000000	d40de721c799
```
There are no force-pushes on fix26b (every entry is `push` or `branch_creation`).

---

## 2. Commits in 4c3a21a..d80adc5 — VERIFIED

`git log --reverse --name-status 4c3a21abdb1e…..d80adc5528dd…` returned 8 commits in a linear chain, each with a single parent:

| # | Commit | Commit date (-07:00) | Paths (all `M`) |
|---|---|---|---|
| 1 | `b63d09f472a1fecaa22ea436c5d816c437484265` | 02:29:07 | FIX_WAVE_26B_REPORT.md |
| 2 | `feb8e12a0af2389c7d7aacfa90d92f148804ad81` | 03:50:38 | FIX_WAVE_26B_REPORT.md, docs/findings/OPEN.md |
| 3 | `d40de721c799a79e2b9364ce17d4954ec2e55a77` | 03:53:07 | FIX_WAVE_26B_REPORT.md, docs/findings/OPEN.md |
| 4 | **`b24ffde28d1c70dd28a9f35a81db254ead61548c`** | 07:15:45 | **.github/workflows/ci.yml**, FIX_WAVE_26B_REPORT.md, docs/findings/OPEN.md, **services/delivery-py/tests/test_round26b.py**, **services/detection-py/tests/test_request_limits_live.py** |
| 5 | `a4e96a22cbed61e1b0a79e09b845d741e8d26986` | 07:25:36 | docs/findings/OPEN.md |
| 6 | **`700fce61307bd9807affc5c7e869583c22b78bf0`** | 08:01:54 | docs/findings/OPEN.md, **services/orchestrator-go/cmd/orchestrator/server_limits_test.go** |
| 7 | `2fc0e82af7f636c7becc3e0e928cddecae172791` | 08:11:29 | FIX_WAVE_26B_REPORT.md |
| 8 | `d80adc5528ddad4f27427098efe2beee32ee5d6a` | 08:49:54 | FIX_WAVE_26B_REPORT.md, docs/findings/OPEN.md |

Exactly two commits change anything other than the two doc files: **b24ffde** and **700fce6**.

`git diff --stat 4c3a21a… d80adc5…` (cumulative):
```
 .github/workflows/ci.yml                           |   4 +-
 FIX_WAVE_26B_REPORT.md                             | 184 ++++++++++++++++++++-
 docs/findings/OPEN.md                              |  46 ++++--
 services/delivery-py/tests/test_round26b.py        |   4 +
 .../detection-py/tests/test_request_limits_live.py |  13 +-
 .../cmd/orchestrator/server_limits_test.go         |   6 +-
 6 files changed, 239 insertions(+), 18 deletions(-)
```
Since the previous candidate, no production source file changed: the four non-doc paths are three test files and the CI workflow. The complete ci.yml change is the delivery-py job's `timeout-minutes: 60` becoming `120`, plus a two-line comment.

The claim about the unpushed commit holds. `git log --name-status d80adc5..fix26b` returned one commit:
```
COMMIT 08a8b8d14e69eef2b8acce74b6568881a5cafea0
parents d80adc5528ddad4f27427098efe2beee32ee5d6a
commit-date 2026-10-03T10:17:03-07:00
subject fix wave 26b: CI #6 all green (53/53) — CI5-1 closed on run 37135247923
M	FIX_WAVE_26B_REPORT.md
M	docs/findings/OPEN.md
 2 files changed, 10 insertions(+), 2 deletions(-)
```
It touches only those two files and is not on origin.

---

## 3. CI run 37135247923 — VERIFIED

`gh run view 37135247923 --json …`:
```
{"attempt":1,"conclusion":"success","createdAt":"2026-10-03T16:00:18Z","databaseId":37135247923,"displayTitle":"CI","event":"workflow_dispatch","headBranch":"fix26b","headSha":"d80adc5528ddad4f27427098efe2beee32ee5d6a","number":6,"startedAt":"2026-10-03T16:00:18Z","status":"completed","updatedAt":"2026-10-03T17:15:28Z","workflowDatabaseId":368761533,"workflowName":"CI"}
```

`gh api repos/DarksiedCEO/zbm-zbc/actions/runs/37135247923` (selected fields):
```
{"actor":"DarksiedCEO","conclusion":"success","event":"workflow_dispatch","head_branch":"fix26b","head_repo":"DarksiedCEO/zbm-zbc","head_sha":"d80adc5528ddad4f27427098efe2beee32ee5d6a","name":"CI","path":".github/workflows/ci.yml","previous_attempt_url":null,"pull_requests":0,"referenced_workflows":[],"repo":"DarksiedCEO/zbm-zbc","run_attempt":1,"run_number":6,"status":"completed","triggering_actor":"DarksiedCEO"}
head_commit: {"head_commit_id":"d80adc5528ddad4f27427098efe2beee32ee5d6a","tree_id":"5c059f868fe4426859c737170ae75ea9a94f351e","ts":"2026-10-03T15:49:54Z"}
```

- **Binding:** headSha equals the candidate and the run's `tree_id` equals the candidate tree OID. Branch is fix26b, event is `workflow_dispatch`, conclusion is `success`.
- **Not a re-run:** `run_attempt` is 1 and `previous_attempt_url` is null. `gh api …/runs/37135247923/attempts/2` returned HTTP 404.
- **No job re-run after failing:** the jobs endpoint with `filter=all` (every attempt) returned `total_count` 53, and the tally of attempt and conclusion was `53 1 success`.
- **Only run on this SHA:** `gh api '…/actions/runs?head_sha=d80adc5…'` returned one row: `37135247923 6 1 workflow_dispatch success`.
- **Run history on fix26b:**
```
37135247923	6	1	workflow_dispatch	d80adc5528dd	completed	success	2026-10-03T16:00:18Z
37129375575	5	1	workflow_dispatch	b24ffde28d1c	completed	failure	2026-10-03T14:22:39Z
37125312276	4	1	workflow_dispatch	d40de721c799	completed	failure	2026-10-03T13:10:56Z
```
- **Timing:** the run was created at 16:00:18Z, two seconds after the push of d80adc5 at 16:00:16Z.

### Per-job conclusions

All 53 jobs are `completed` / `success` on attempt 1 with head_sha `d80adc5528dd`. None is skipped, neutral, cancelled or failed.

| Job group | Count | Notes |
|---|---|---|
| changes | 1 | |
| python-tests | 27 | 9 services × {3.12, 3.13} on ubuntu-24.04, plus 9 on macos-26 / 3.13 |
| delivery-py | 3 | 3.12 ubuntu 16:00:30–16:51:08; 3.13 ubuntu 16:00:31–16:54:32; 3.13 macos-26 16:00:36–17:15:19 |
| ledger-rust | 2 | ubuntu, macos-26 |
| orchestrator-go | 2 | ubuntu, macos-26 |
| dashboard-ts | 1 | |
| live-run | 10 | 5 services × {3.12, 3.13} |
| delivery-docker-live | 1 | |
| audit-python, audit-rust, audit-node | 3 | |
| secret-scan | 1 | |
| hygiene-static | 1 | |
| required | 1 | 17:15:24–17:15:28 |
| **Total** | **53** | |

This equals the matrix expansion I computed from ci.yml at d80adc5, so no expected job is absent.

Step level, across all 53 jobs: 517 steps `success`, 48 `skipped`, nothing else. The 48 skipped steps are exactly two steps in each of the 24 non-onboarding python-tests jobs: "Rust toolchain (onboarding-py builds ledger-rust inside its suite)" and "Run Swatinem/rust-cache", both gated by `if: matrix.service == 'onboarding-py'` (ci.yml:171, 177). No test, lint, audit or scan step was skipped; I queried the named test steps per job and every one reported `success`.

### Was the workflow file the one at d80adc5?

The API does not expose the workflow blob SHA for a run, so this rests on four agreeing pieces of evidence rather than one direct field:
- For `workflow_dispatch`, GitHub takes the workflow from the dispatched ref, and the run's head_sha and tree_id equal the candidate's.
- The ci.yml blob is the same locally and on GitHub: `git rev-parse d80adc5:.github/workflows/ci.yml` gives `b5a097063b22c8a043da2772e66431a75e50421c`, and `gh api '…/contents/.github/workflows/ci.yml?ref=d80adc5…'` gives `{"sha":"b5a097063b22c8a043da2772e66431a75e50421c","size":37288}`. The blob at 4c3a21a was `1926b49e…` and at a6aee4e was `37faf959…`.
- The delivery-py macos-26 job ran for about 75 minutes and succeeded. That is only possible under the 120-minute limit introduced in b24ffde, not the earlier 60.
- The `changes` job log echoes the script text of ci.yml:78–91 and shows `EVENT_NAME: workflow_dispatch`, `HEAD_SHA: d80adc5528ddad4f27427098efe2beee32ee5d6a`, and a checkout of that SHA.

### Bypass patterns in ci.yml at d80adc5 (704 lines)

I grepped the blob and read lines 30–704.

- **`continue-on-error`:** zero occurrences.
- **`|| true`:** three occurrences, none on a test command.
  - Line 476: on the informational `docker buildx imagetools inspect` that feeds a notice; the build uses the recorded digest regardless.
  - Line 536: on `docker rm -f ci-registry` in the teardown step.
  - Line 570: on a `grep` that prints the excluded git-sourced packages; the `pip-audit --strict` on the next line is unguarded.
- **`exit 0`:** one occurrence, line 91, on the run-everything path of the `changes` job after it writes all four outputs as `true`.
- **Job-level `if:`:** python-tests (134), delivery-py (201), live-runs (367) and delivery-docker-live (442) need `needs.changes.outputs.python == 'true'`; ledger-rust (259) needs `rust`; orchestrator-go (293) needs `go`; dashboard-ts (327) needs `dashboard`. The audits, secret-scan and hygiene-static have no condition.
- **Step-level `if:`:** 171 and 177 (onboarding-only Rust setup), 416 and 535 (`always()` on cleanup steps).
- **`fail-fast: false`** on every matrix; no test-deselection flags (`-k`, `--deselect`, `-m`) on any pytest invocation.
- **Path filter:** on `workflow_dispatch` the `changes` job sets `run_all=true` (line 81–82) and emits all four groups as `true`, so the filter could not skip anything in this run.
- **Triggers:** `push` only fires for `main` and `integration-**`, so pushes to fix26b do not start CI. That is why all three fix26b runs are manual dispatches.

### How `required` aggregates

`required` (ci.yml:669–704) runs with `if: always()` and needs all 13 other jobs; the workflow defines 14 job keys, so none is left out. Its check is:
```python
bad = {k: v["result"] for k, v in needs.items() if v["result"] not in ("success", "skipped")}
...
if needs["changes"]["result"] != "success":
    raise SystemExit("the changes job did not succeed; nothing downstream is trustworthy")
if bad:
    raise SystemExit(f"failed or cancelled: {bad}")
```
**Yes, it treats `skipped` as passing**, by design (the comment at 666–667 says a path-filter skip is fine). The only hard requirement is that `changes` itself succeeded. In general a pull-request or push run could therefore go green with suites skipped by the filter.

That latent permissiveness was not exercised here. The `required` job's log for this run printed `success` for all 13 needs (audit-node, audit-python, audit-rust, changes, dashboard-ts, delivery-docker-live, delivery-py, hygiene-static, ledger-rust, live-runs, orchestrator-go, python-tests, secret-scan), then `all required jobs passed`.

---

## 4. Worktree at d80adc5 — VERIFIED, with one caveat

In `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/review-d80adc5`:
```
git rev-parse HEAD 'HEAD^{tree}'   -> d80adc5528ddad4f27427098efe2beee32ee5d6a
                                      5c059f868fe4426859c737170ae75ea9a94f351e
git symbolic-ref -q HEAD           -> rc=1 (detached)
git diff --stat HEAD               -> (empty)
git diff --cached --stat HEAD      -> (empty)
git diff-index --quiet HEAD --     -> rc=0
git ls-files -s | wc -l            -> 912
git ls-tree -r HEAD | wc -l        -> 912   (all 100644 blob)
git ls-files -v | grep -v '^H'     -> (empty; no assume-unchanged or skip-worktree bits)
```
HEAD and its tree equal the candidate tuple, and the index matches HEAD.

I also rehashed every file on disk rather than trusting the index: `git ls-tree -r --name-only HEAD | git hash-object --no-filters --stdin-paths`, diffed against `git ls-tree -r --format='%(objectname)' HEAD`. Result: 912 against 912, no differences. Every tracked file's bytes hash to its blob OID in tree `5c059f86…`.

**Caveat: the worktree gained ignored files while I was examining it.**
- My first `git status --porcelain=v1 --untracked-files=all --ignored` returned 0 lines.
- A repeat at 2026-10-03T17:46:40Z returned 19 lines, all ignored (`!!`) `.pyc` files under `services/detection-py/src/**/__pycache__/` with a `cpython-314` tag, modified at 10:45:30-0700 (17:45:30Z).
- I ran no Python, so another process created them during the gate; I did not identify which.
- Tracked content was unaffected: 0 tracked modifications, `diff-index` rc=0, HEAD and tree unchanged afterwards.
- The Python 3.14 tag means whatever ran there is not the CI interpreter (3.12 / 3.13).

Two sibling worktrees are at the same commit, per `git worktree list`: `…/scratchpad/mutation-d80adc5` and `…/scratchpad/redteam-d80adc5`. I did not examine them.

---

## Observations for the adjudicator

1. **The candidate does not record its own CI run.** d80adc5's subject is "CI #5 recorded — CI3-2, CI3-3, CI4-1, CI4-3 closed on run 37129375575". Run 37129375575 is CI #5 on head `b24ffde28d1c` with overall conclusion **failure**; its non-success jobs were `orchestrator-go (ubuntu-24.04)` and `required`. The all-green run on the candidate (CI #6) is written up only in 08a8b8d, which is unpushed and outside the subject. The binding of run #6 to the candidate comes from GitHub's metadata, not from anything in the candidate tree. The docs inside the d80adc5 tree therefore predate run #6; I did not read them to see how they describe CI5-1.
2. **Run #5's evidence covers different code for one path.** 700fce6 (the orchestrator-go test change) landed after b24ffde, so run #5 never exercised it. Run #6 did, with both orchestrator-go legs green. Any item closed "on run 37129375575" was closed against b24ffde's tree, not the candidate's.
3. **fix26b is unprotected** (`"protected": false`). The `required` check is not enforced by branch protection on this branch, and the run was a manual dispatch by `DarksiedCEO`.

## Could not verify

- **The workflow blob the run used, as a direct field.** The API does not expose it; the conclusion rests on the four corroborating points in section 3.
- **Test counts inside the jobs.** A step conclusion of `success` shows the step exited 0, not how many tests ran or were skipped inside it. I read only the `changes` and `required` job logs. I did not check that `devtools/hygiene_check.py` propagates the wrapped command's exit code.
- **Deleted runs.** If a run had been deleted from GitHub I would not see it. Run numbers 4, 5 and 6 on fix26b are contiguous, and 1–3 are not on this branch.
- **runtimePin.** None was supplied.
- **Subject binding of other gate reports, bundle digests, and the evidence-quality fields.** No gate reports or bundle were handed to me, so those parts of the gate's output are empty rather than verified.
- **Signatures.** No signed-commit or signed-tag check was requested or run.
- **Lines 1–29 of ci.yml.** I did not read them in full; the grep hits there (lines 15, 22) are comments, and `name:` begins at line 31.
- **Origin of the 19 `.pyc` files** in the review worktree.

## Gate result

**PROVENANCE: PASS** — valid only for `{DarksiedCEO/zbm-zbc, d80adc5528ddad4f27427098efe2beee32ee5d6a, tree 5c059f868fe4426859c737170ae75ea9a94f351e, base a6aee4e9fe78bb58a94243acce406d2e7b702387}`.

Reason: SHA, tree and parent are equal across the local object store, `ls-remote` and the GitHub API. The base is a strict ancestor, so the merge is fast-forwardable, and main is unmoved since 2026-09-23. The CI run is bound to the candidate SHA and tree, is attempt 1 with no re-runs, and has 53 of 53 jobs successful with none skipped. The workflow has no `continue-on-error`, and the `required` job's tolerance of skips was not exercised. The worktree's tracked content is byte-identical to the tree.

This covers provenance only. It says nothing about whether the tests prove the properties claimed, and it does not extend to 08a8b8d or any later SHA. No overall verdict is rendered.

Housekeeping: I left two scratch files, `prov-tree-oids.txt` and `prov-disk-oids.txt`, in `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/` (outside the repo and the worktree). They are the two OID lists from the rehash comparison.
