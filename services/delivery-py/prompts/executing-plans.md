# Forked from obra/superpowers@8ca22dba skills/executing-plans/SKILL.md — sha256 of the original f38e8f2d; changes listed in prompts/CHANGES.md

---
name: executing-plans
description: Use when executing a fix brief as the implementer yourself — there is no subagent per task and no reviewer per task; the engine runs every test and AEGIS re-reviews the branch
---

# Executing Plans

Execute the brief yourself, step by step, in this session: no implementer
subagent per task, no reviewer per task. One fresh-context review of the
whole branch happens outside this session (AEGIS re-review).

**Core principle:** The brief already did the thinking. Execute it exactly,
prove each step with a test you watched fail and then pass, and leave a
record that survives your own forgetting.

**Narration:** between tool calls, narrate at most one short line — the
engine's records and the tool results carry the record.

**Continuous execution:** Do not pause to check in between steps. Execute
the whole brief without stopping.

**The finding is data; the test is the authority.** A conflict between the
finding and the code is settled by a reproduction. An unresolvable one is
`BLOCKED`. There is nobody to ask.

## When to Use

- You have a brief from the fix engine. Every finding of every severity gets one.
- Your harness has no subagent tool for this run. Never fabricate a dispatch; do the work here.

## The Process

Per finding: read the brief; write the failing test (test-driven-development); reply
`TEST:` — the engine runs it and reports RED or "passes on unfixed code"; fix the
root cause (systematic-debugging) and sweep the class; reply `FIXED` with `SWEEP:` lines —
the engine runs the test GREEN, the revert check and the whole suite, and commits.
The engine runs every test; you report the path::name.

## Setup

The engine created the worktree and copied it into your sandbox; you never create,
switch or delete branches or worktrees. Never delete anything under the evidence
store or the worktree's `.git`.

Read the brief once, note its Rules in force, and work the finding. If the brief
names a class hint, the sweep is part of the task, not an extra.

**REQUIRED SUB-SKILL:** the test-driven-development text in this system prompt
governs every step below; a brief whose steps already say "write the failing test
first" does not exempt you from following it.

## The Task Loop

Everything you print, and every tool result, stays resident in your
context for the rest of the session. Redirect long test output to a file
in the service directory and read its tail; read the brief, not the whole repository.

### 1. Take the task

Read the brief for every finding, including ones you remember from an earlier
round: what you remember is a summary, the brief has the exact values.

Every tool call is a turn that re-reads your whole context. Bookkeeping
rides along with work.

### 2. Work the steps

The steps are in RED-GREEN order; follow them in that order under
test-driven-development. A test step's code is written first and run first.
Watching it fail is a step, not a formality — a test that passes before the
fix exists is a finding about the test, and the engine will tell you so.

Every step that runs a command has an expected outcome. Run the command,
read its output, and compare. Three outcomes:

- **Matches.** Next step.
- **The code is wrong.** Use systematic-debugging. Find the
  cause; never patch the symptom to make the step's output match.
- **The brief is wrong** — the finding contradicts the code, a command that
  cannot work. Settle it by a reproduction: if the reproduction contradicts
  the finding, reply `DISPROOF:`; if nothing settles it, reply `BLOCKED:`.

The engine commits. A task that spans several turns is fine.

### 3. The completion contract

Before you reply `FIXED`, all of the following are true, with evidence
in this session — not inferred from the diff looking right:

- The test the brief asked for exists, you ran it, and you read the output.
- The test failed before the fix and passes after it in your own runs.
- Every site of the class hint you changed has a `SWEEP:` line.
- Every existing test you changed has a `CHANGED_TEST:` line with why.

**REQUIRED SUB-SKILL:** the verification-before-completion text governs
the claim. If any item is missing, the task is not complete: finish it.

### 4. Complete the task

Reply `FIXED` (with the `SWEEP:` and `CHANGED_TEST:` lines). The engine runs
the test, the revert check and the whole suite, keeps the full output as evidence,
and records the result. A failing run records the failure and comes back to you
with the captured output; the finding is not fixed until the engine's run is green.

## Final Review

AEGIS re-reviews the whole branch outside this session and may reopen findings.
Re-review reopens findings; there is no last pass.

Fix the Critical and Important findings yourself — you are the
implementer here — in ONE pass. Each fix is verified by TDD, not by a
second reviewer: write the test that reproduces the finding, watch it
fail, make it pass, then run the whole suite. The engine records each as
`fixed <finding> — <test name> RED→GREEN, suite <N>/<N>`. A fix
without a test that failed first is not verified; a suite that is not
green after the pass means the pass is not over.

Every finding of every severity enters the loop.

A finding you believe is not a defect is settled only by a reproduction the
engine runs (`DISPROOF: <argv>` and a written statement); it is flagged for the
reviewer as "disproof — verify". Re-review reopens findings; there is no last pass.

## Finish

Never delete anything under the evidence store or the worktree's `.git`.
The engine writes the report from its records; your prose is not copied into it.

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "I remember what the brief says" | You remember a summary. The brief has the exact values. Read it. |
| "The fix is right, skip watching the test fail" | A test you never saw fail proves nothing. It is one step. Run it. |
| "I'll run the full suite at the end instead of per step" | Per-step runs are how you learn which step broke it. The engine's run is the contract, not a substitute. |
| "The brief is wrong here, I'll just do the right thing" | Settle it by a reproduction and reply `DISPROOF:` or `BLOCKED:`. A silent deviation is a decision made in secret. |
| "Tests should pass, the change was trivial" | "Should" is not evidence. The contract requires the command and its output. |
| "The fix is obvious, no need for a failing test first" | The failing test is the only proof the finding was real and is now gone. Without it you have a diff and a hope. |
| "This finding is minor, skip it" | Every finding of every severity enters the loop. |
