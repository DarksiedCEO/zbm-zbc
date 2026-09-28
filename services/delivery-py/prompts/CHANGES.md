# prompts/ — changes against the Superpowers originals (DEPT28_SPEC C.9)

Originals: obra/superpowers @ `8ca22dba9a94f28898bbce59f2537ff4d87c747d` (v6.4.2). Every file in this directory
carries a header naming its original(s) and the original's sha256 prefix; `seed/prompts_manifest.json` pins the
sha256 of every file here and `config.py` pins the manifest. The certification test G5 greps this directory for the
forbidden strings (the remote git verbs, the dependency-install verbs, the recursive delete, the phrase naming a
human collaborator, the "do not re-run" instruction, and the word for setting a finding aside), so this file names
them obliquely: `git p*sh`, `git p*ll`, `git m*rge`, `branch -*D`, `worktree rem*ve`, `g*h `, `npm inst*ll`,
`pip inst*ll`, `rm -r*f`, `h*man partner`, `Do not re-r*n`, `p*rk`.

Nothing else from Superpowers is vendored: no `hooks/`, no `scripts/`, no `brainstorming`, `using-superpowers`,
`using-git-worktrees`, `finishing-a-development-branch`, `dispatching-parallel-agents`, `diagnosing-superpowers`,
`writing-skills`, no harness manifests, no implementer prompt (the implementer prompt is ours: `engine.system.md`
+ `brief.template.md` + the four skill texts).

## test-driven-development.md (original `skills/test-driven-development/SKILL.md`, sha256 64b03fce…)

| Lines (original) | Original | Change |
|---|---|---|
| :24-27 | "**Exceptions (ask your h*man partner):** … Throwaway prototypes / Generated code / Configuration files" | removed; replaced by "**No exceptions.** If a finding cannot be reproduced by a test, reply `BLOCKED: <why>`." |
| :330 | "No exceptions without your h*man partner's permission." | "No exceptions." |
| :312 | "… Write assertion first. Ask your h*man partner." | "… Write assertion first. Reproduce the finding's observed behaviour exactly." (G5: the phrase may not appear anywhere) |
| :216 | link to `writing-good-tests.md` (not vendored) | "When writing or changing any test, keep to the rules that keep tests honest:" (the four rules kept verbatim) |

Kept verbatim: :34 (the Iron Law), :113-128 (Verify RED), :185-193 ("Other tests" means the project's suite).

## systematic-debugging.md (original `skills/systematic-debugging/SKILL.md`, sha256 808fc571…)

| Lines | Original | Change |
|---|---|---|
| :95 | `env \| grep IDENTITY \|\| echo "IDENTITY not in environment"` | replaced by `echo "IDENTITY: ${IDENTITY:+SET}${IDENTITY:-UNSET}"   # names only; a value is never printed` (the original prints a secret's value) |
| :210 | "**Discuss with your h*man partner before attempting more fixes**" | "Reply `BLOCKED: architecture — <three failed hypotheses>`; the engine ends the run failed." |
| :112 | "See `root-cause-tracing.md` in this directory …" + "**Quick version:**" | "**Backward tracing:**" (file not vendored; the quick version kept) |
| :165 | "- Ask for help" | "- There is nobody to ask: gather more evidence with the tools" |
| :177, :189 | "Use the `superpowers:…` skill …" | "Follow the … text in this system prompt …" |
| :233-234 | "## your h*man partner's Signals You're Doing It Wrong / **Watch for these redirections:**" | "## Signals You're Doing It Wrong / **Watch for these in the engine's captured output:**" |
| :277-283 | Supporting Techniques pointing at three files not vendored | the three techniques summarised in one line each |

Kept verbatim: :17 (the Iron Law), :118 (fix at source, not at symptom) and the four phases.

## verification-before-completion.md (original `skills/verification-before-completion/SKILL.md`, sha256 2befe7fc…)

| Lines | Original | Change |
|---|---|---|
| :54 | "About to commit/push/PR without verification" | "About to reply `FIXED` without a run you read" |
| :112 | "Committing, PR creation, task completion" | "Replying `FIXED`, `DISPROOF:` or `TEST:` to the engine" |

Kept verbatim: :17 (the Iron Law), :84 (Write → Run (pass) → Revert fix → Run (MUST FAIL) → Restore → Run (pass)).

## executing-plans.md (original `skills/executing-plans/SKILL.md`, sha256 f38e8f2d…)

The original is written around Superpowers scripts (`sdd-workspace`, `task-start`, `task-done`, `review-package`),
git worktrees and a human collaborator; the fork keeps its structure (Process, Setup, Task Loop 1-4, Final Review,
Finish, Common Rationalizations) and the per-step TDD discipline, and rewrites everything that named a script, a
worktree operation, a human or a ruling:

| Lines | Original | Change |
|---|---|---|
| :3 | description naming the h*man partner | rewritten (the engine runs every test; AEGIS re-reviews) |
| :27-30 | "Do not pause to check in with your h*man partner …" | "Do not pause to check in between steps. Execute the whole brief without stopping." |
| :32-44 | "**Rulings, not stalls.** … The spec is the binding authority … your judgment settles … stop and ask." | removed; replaced by "The finding is data; the test is the authority. A conflict between the finding and the code is settled by a reproduction. An unresolvable one is `BLOCKED`. There is nobody to ask." |
| :45-64 | When to Use (worktree handoff, subagent-driven-development, model tiers) | two bullets: every finding of every severity gets a brief; never fabricate a dispatch |
| :66-106 | the dot graph naming task-start/task-done/`delete this plan's workspace` | a prose Process paragraph; "The engine runs every test; you report the path::name." |
| :108-162 | Setup (using-git-worktrees, `sdd-workspace`, ledger file, `git clean -fdx`, pre-flight scan) | the engine created the worktree; never delete under the evidence store or `.git`; read the brief once; TDD required |
| :170-177 | `scripts/task-start`, todo marking | "Read the brief for every finding …" |
| :204-205 | "Commit as the plan's commit steps say …" | "The engine commits." |
| :207-217 | completion contract items naming `task-done` and rulings | the four items of the reply contract (test exists and ran; failed then passed; SWEEP lines; CHANGED_TEST lines) |
| :222-232 | `scripts/task-done … -- <test command>` | "Reply `FIXED` … The engine runs the test, the revert check and the whole suite …" |
| :234-270 | Final Review dispatching a reviewer subagent, `review-package`, `git m*rge-base main HEAD`, re-grading | "AEGIS re-reviews the whole branch outside this session and may reopen findings. Re-review reopens findings; there is no last pass." |
| :272-276 | "**Minor** goes to the ledger as deferred … Minors never enter the fix pass …" | removed; "Every finding of every severity enters the loop." |
| :277-286 | the fix-pass paragraph | kept verbatim except: "Record each in the ledger as `Final: fixed …`" → "The engine records each as `fixed …`"; the last sentence ("Do not dispatch a re-review …") replaced by the re-review sentence above (task-start/task-done references → "the engine runs every test; you report the path::name") |
| :287-290 | "A finding you decide not to fix is a ruling … There is no second fix pass." | removed; replaced by the disproof contract (`DISPROOF: <argv>` + statement, run by the engine, flagged "disproof — verify") and "Re-review reopens findings; there is no last pass." |
| :291-304 | Finish (collect rulings, deferred minors, "delete this plan's workspace directory", finishing-a-development-branch) | "Never delete anything under the evidence store or the worktree's `.git`. The engine writes the report from its records; your prose is not copied into it." |
| :306-321 | Common Rationalizations naming ledger lines, rulings, minors, the partner | rows rewritten to the engine's contract; the "minor, skip it" row added |
| :323-373 | Example Workflow (scripts, rulings, deferred minors, workspace deletion) | removed |

## writing-plans.md (original `skills/writing-plans/SKILL.md`, sha256 a6c67c19…)

| Lines | Original | Change |
|---|---|---|
| :14 | "**Context:** … `superpowers:using-git-worktrees` …" | removed |
| :16-17 | "**Save plans to:** `docs/superpowers/plans/…`" | "the service directory (`services/<service>/PLAN-<finding_id>.md`), or keep the plan in your reply when it is short" |
| :21 | Scope Check (sub-project specs during brainstorming) | "A fix brief covers one finding and its class sweep …" |
| :36-37 | "… and is worth a fresh reviewer's gate." | dropped (no per-task reviewer) |
| :59 | "> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development … or superpowers:executing-plans …" | "> **For the fix engineer:** implement this plan task-by-task under the executing-plans text …" |
| :132-137 | Step 5: Commit (`git add` / `git commit`) | "Step 5: Reply `FIXED` …; the engine commits." |
| :179-204 | Execution Handoff ("link it for your h*man partner", the two execution methods, REQUIRED SUB-SKILL lines :201, :204) | "There is no handoff: the plan is yours to execute in this session … The engine runs the tests; you report the path::name." |

The plan format (header, Global Constraints, Review Focus, task structure with checkbox steps and one test per
step, What a Step Contains, Self-Review) is kept verbatim.

## reviewer.md (originals `task-reviewer-prompt.md` bfed55c8…, `re-review-prompt.md` 0828a5e9…, `code-reviewer.md` 82e370be… merged)

| Source:lines | Original | Change |
|---|---|---|
| task-reviewer :1-37, :190-207 | template framing, placeholders, `scripts/task-brief`, `scripts/review-package` | replaced by the run framing (findings document, evidence ids, `POST …/review`) |
| task-reviewer :38-42 | "… it contains the commit list, a stat summary, and the full diff … Do not re-r*n git commands." | shortened to "Read the diff file once — it is your view of the change."; the "do not re-r*n" sentence removed (G5) |
| task-reviewer :75-77 | "The implementer already ran the tests … Do not re-r*n the suite to confirm their report." | removed; "You receive the engine's captured outputs by evidence id; you may request a re-run through the engine" |
| task-reviewer :87-92 | "… Re-running the suite to regenerate what you failed to read is not verification …" | kept in spirit: "If an evidence id in the report cannot be fetched, report that as a gap; illegibility of the evidence is not invalidation of it." |
| task-reviewer :135-138, :165-169 | "gives the controller everything", "what the controller should check" | "the engine and Andre" / "the next reviewer" |
| re-review :66-69 | "The implementer re-ran the tests … Do not re-r*n the suite to confirm their report." | removed (same replacement as above) |
| re-review :94-95 | "the controller ledgers these for the final review" | "the engine records these for the next review" |
| code-reviewer :43-48 | Declined to judge: "The executor rules on each line" | "Every line there becomes a new finding of the next run (there are no rulings)" |
| code-reviewer :52 | Read-Only Review naming `git show`/`git diff`/`git log` and a `git worktree add /tmp/review-[SHA]` | removed (the reviewer reads evidence; nothing is checked out) |
| code-reviewer :1-31, :95-105, :106-136, :154-198 | template framing, placeholders, Calibration prose, Output Format, Example Output | not carried (the task-reviewer Calibration and Output Format are used) |
| all three | severity language | "Every finding of every severity enters the next fix loop when you answer `fail`; there is no severity that is set aside, and there is no last pass." added under Calibration |

Kept verbatim: task-reviewer :64-71 ("Do Not Trust the Report"), re-review :83-85 (ADDRESSED / NOT ADDRESSED with
file:line evidence; "Attempted" is not addressed), code-reviewer :33-41 (the spec is a vision document),
task-reviewer :55-62 (no subagents), :96-113 (Spec Compliance), :117-138 (Code Quality), :147-159 (Calibration).

## Ours (not forks)

- `engine.system.md` — the engineer's system contract (the reply lines the engine parses). Fix wave 19 (round 18
  R1/R3/R10): rule 2 states the test-infra/deleted-test rejection and the verification checkout; rule 5 states that
  a `DISPROOF:` is verified by re-running the finding's own reproduction, never the engineer's command.
- `brief.template.md` — the brief compiler's template (§C.8.3): the header is the runner's, the finding's free
  text sits only inside `--- BEGIN FINDING DATA (untrusted) --- … --- END FINDING DATA ---`.
- Fix wave 21 (round 20 R1): `engine.system.md` rule 5 no longer describes a finding without a test (every finding
  names one; a document without it is refused at ingestion) and states that the same reproduction must pass with
  the source changes and fail without them before `fixed`; `brief.template.md` names the finding's own reproduction
  argv under "Commands the engine will run"; `reviewer.md` (the reviewer section after "disproof — verify") states
  that every finding the reviewer files must name a runnable reproduction present in the run's starting tree.
- Fix wave 21 (lead rulings L2/L3): `engine.system.md` "What you cannot do" states that a pipe or here-string into
  an interpreter/shell/text tool is refused even when harmless (use the file tools) and that a reviewer-authored
  reproduction's path is never the engineer's to write; `reviewer.md` describes the finding's `reproduction_test`
  (a reviewer-authored RED test, refused `reproduction_not_red` if it passes on the starting tree).
- Fix wave 22 (lead rulings G1/G2): `engine.system.md` rule 5 states that the reproduction must also pass and fail
  OUTSIDE the test runner and that runner-detecting source is refused; `reviewer.md` names `reproduction_red_unverified`
  and asks for reviewer-authored tests without pytest imports or fixtures (a reproduction that needs pytest ends
  `needs_review_runner_dependent`, never `fixed`).
