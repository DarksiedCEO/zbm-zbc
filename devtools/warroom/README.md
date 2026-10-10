# War room — the red-team launch gate

Department 41 (Adversarial Testing & Red Team) runs a war room for every department that deals with the public: it
throws as much chaos at the department as it can, in a sandbox, before the department may go live, and every failure
it ever finds stays in the room as a permanent case. Approved by Andre on 2026-10-07; the design record is
[ADR 0018](../../docs/adr/0018-war-room-red-team-launch-gate.md).

Standard library only. The services' own dependencies are used inside the workers, which run each service's own
in-process test harness.

## Run it

```bash
# the gate, as CI runs it (every seeded department, seed 1); exit 1 on any new MUST failure or ERROR
python3 devtools/warroom/run.py --all --seed 1

# one department, another seed (exploration: other seeds make other chaos variants)
python3 devtools/warroom/run.py --department service-py --seed 7

# reports to a directory (warroom-report.json + warroom-report.md; the markdown is printed either way)
python3 devtools/warroom/run.py --all --seed 1 --out /some/dir

# replay one case exactly and print everything it saw (a seeded id, or a replay-library id)
python3 devtools/warroom/run.py --replay 'service-py/email-clear-opt-out#017.2@1'
python3 devtools/warroom/run.py --replay service-py/R0001

# what would run
python3 devtools/warroom/run.py --list --seed 1
```

`--python` names the interpreter that has the services' pinned requirements (default: the one running `run.py`).
`--variants N` overrides the libraries' variants per seed phrase. `--jobs` runs departments in parallel (one worker
process each; the outcome does not depend on it).

## How it works

| Part | File | What it does |
|---|---|---|
| Scenario libraries | `scenarios/<service>.json` | Versioned data. Per scenario: a persona, seed phrases (extracted by AST from the service's pinned tests or source, never retyped), ordered steps, the chaos transforms allowed, invariants with severities |
| Seed extraction | `corpus.py` | Reads a named tuple, the n-th `for` loop over a literal, a `parametrize` table or a module constant; a reference that no longer resolves is an ERROR, never an empty scenario |
| Chaos | `chaos.py` | Seeded, reproducible transforms (below); each case records its transform chain |
| Engine | `engine.py` | Seeds x variants -> cases; one worker per service; invariants; per-department gate; JSON / markdown report; promotion |
| Worker | `worker.py` | Runs inside one service's directory: seals the network, clears the service's env, builds a fresh harness per case, runs the steps, reads the resulting state back |
| Drivers | `drivers/<service>.py` | The steps (actions) and state read-back for one service, on that service's own `tests/helpers.py` harness (onboarding-py: `tests/conftest.py`) |
| Invariants | `invariants.py` | Pure predicates over what a case saw (responses and state) |
| Replay library | `replay/<service>.json` | Every failure ever promoted, materialised (the exact input sent); runs on every run |
| Findings | `findings.md` | The triaged MUST failures, with their replay ids |
| LLM personas | `personas.py` | A port that answers `NOT_CONNECTED`. Nothing is generated or faked |

**Outcomes.** PASS (every MUST invariant held), FAIL (a MUST invariant did not), ERROR (the harness or a predicate
crashed, or the worker never answered — never counted as a pass). SHOULD invariants are scored and reported, never
gate-blocking.

**Gate.** A department passes when every case PASSes except replay-library cases marked with a finding id
(`known_failure`), and no case ERRORs. The MUST pass rate in the report excludes known failures. The run exits 1 if
any department fails.

**Sandbox.** Nothing real is sent, charged or revoked: each case runs in a fresh in-memory harness with fake ledgers,
fixed clocks and recording (or the services' fail-closed stand-in) ports; the worker makes every socket connect raise
before any service code is imported; each case's temporary directory is removed when the case ends.

**Determinism.** A case id is `<service>/<scenario>#<seed index>.<variant>@<global seed>`; the case's input is a pure
function of that id and the library. Variant 0 of every seed phrase is the unmutated phrase (the base case, the same
for every global seed). Two runs with one seed produce the same report except its `timing` section (measured,
reported, never asserted).

**Runtime.** Each case builds its own harness, so a case costs a fraction of a second; the libraries' variants per
seed are set so that `--all` stays within a few minutes on one machine (the CI job's timeout is 25 minutes). Raise
`--variants` for a deeper exploration run, not in CI.

## Chaos catalogue

| Transform | What it does |
|---|---|
| `case_flip` | upper / lower / title / random case |
| `zero_width`, `soft_hyphen` | invisible characters inside words |
| `fullwidth` | full-width letters and digits (NFKC folds them) |
| `homoglyph` | core lookalikes: the Cyrillic / Greek letters BOTH repo tables list (creative-py `shared/text.py` CONFUSABLES and sales-py `i10_replies._CONFUSABLE`) |
| `homoglyph_wide` | any lookalike in creative-py's table (Cherokee, insular, small capitals, IPA) |
| `leetspeak` | o->0, e->3, a->4, s->5, t->7, i->1 on some letters |
| `letter_spacing`, `letter_punct` | one word spelled out with spaces ("S T O P"), or with dots / underscores / hyphens |
| `diacritic_toggle` | accents added or removed |
| `html_wrap` | `<br>`, `<p>`, `<div>`, a `<blockquote>` below |
| `quoted_reply` | the words above a `>` quote, with or without an "On ... wrote:" header (English, Spanish, French) |
| `quoted_tail` | the words below an unmarked Outlook "Original Message" / "From: Date:" block |
| `signature` | "Sent from my iPhone", "Thanks,", a `--` block with a phone number |
| `mixed_language` | a Spanish / French / Portuguese line around the words (the opt-out words in those languages are seeds of their own, from the services' term lists) |
| `injection` | a prompt-injection line ("ignore previous instructions...", fake `SYSTEM:` / `assistant:` roles, an HTML comment) asking for other people's data or privileges |
| `long_body`, `long_body_middle` | 25k-40k characters of filler before the words (past the text caps), or around them |
| `punct_variants`, `whitespace_runs` | apostrophe / dash lookalikes, runs of spaces (legal names) |
| `plus_tag`, `dash_tag`, `gmail_dots`, `googlemail_swap`, `email_case`, `email_homoglyph(_wide)` | email address variants |
| `url_*` | scheme case, host case, `www.` / `m.` / `mobile.`, tracking query, fragment, trailing slash, no scheme, percent-encoding, full-width |
| params | a scenario may also vary parameters (platform, time gap, replay count, creator) |

Character-level transforms leave HTML tags and entities alone (a person's words get disguised, not the markup a mail
client wrote). A scenario may name a transform whose effect lies outside what the department's documents promise in
`downgrade`: under that transform the named MUST invariants are scored as SHOULD (the library says why in
`downgrade_basis`). The war room checks the documented, AEGIS-certified guarantees; it does not invent policy.

## When a case fails

1. `--replay <case id>` shows exactly what was sent and seen.
2. `--promote` appends every new MUST failure of the run to `replay/<service>.json` with `known_failure: null`.
   Promotion only appends; nothing removes or rewrites an entry. A null entry still blocks the gate.
3. Triage: write the finding in `findings.md` (`## WR-Fnnn`, with the replay ids), then put the finding id in the
   entries' `known_failure`. The self-test fails while any entry is untriaged or names a finding `findings.md` lacks.
4. When the service is fixed, the case passes and the report says the known failure now passes: set its
   `known_failure` back to null and record the finding in `fixed` (`"fixed": "WR-F005"`), and mark the finding
   `Status: FIXED` in `findings.md` with the fixing commit. The case stays in the library for good as a regression
   case: it now blocks the gate like any other case if it ever fails again.

## Adding a department

Write `drivers/<service>.py` (`prepare_env`, `import_harness`, `setup`, `ACTIONS`, `observe`, `teardown`: see
`drivers/service_py.py`) on the service's own harness, and `scenarios/<service>.json` with seeds taken from the
service's pinned tests or source and invariants from its ADR and tests. Run the base cases (variant 0) first: every
base case must PASS, or the library's expectation is wrong. Then add the service's requirements to the `warroom` CI
job's install step.
