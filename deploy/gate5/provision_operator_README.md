# Gate 5 operator provisioning — dormant, sitting approval required

`provision_operator.py` defaults to HOLD without CLI access, generation, prompts or writes. Do not run its apply mode during preparation. It is not a deploy command and does not make the release ready.

The only accepted target is the pinned `zbm-zbc / production` project/environment pair in `AUTHORIZED_TARGET`. Both names and IDs are checked before CLI access. Each service ID and name must then match Railway's identity readback. The protected old backend and all other environments refuse. Complete service IDs and variable-name inventories must come from the approved sitting; do not fabricate missing service identities or infer absence from a sealed variable omitted by the CLI.

## Sitting prerequisites

- Explicit approval for this exact script and its production credential changes. It writes no URLs: URL cutover belongs to the staged routing phase (D02).
- All target services exist, their identities are reconciled, and auto-deploy is confirmed off. Nobody else edits variables during the operation.
- A current, complete name inventory for every service. It records existence, never values. Existing sealed values and whole caller maps must be available through the user's hidden prompt when CLI reads omit them. Only required credential-plan inputs are prompted when omitted or sealed; unrelated unchanged variables are never requested.
- Exact CLI compatibility and credentials checked during the sitting. The local tests use fake transports; they do not certify live CLI behavior.
- Approval for manual sealing afterwards. The script does not claim to set Sealed status.

The five explicit confirmation flags are operator attestations, not programmatic proof of those prerequisites. No ready-to-run production binding is bundled.

## Behavior and stopping rule

All inventories and direct-copy/caller-map relationships are checked before writes. Existing matching values are retained. Newly absent logical roles use OpenSSL-generated values in memory; all matching destinations receive the same direct value. No value it writes may contain a Railway reference; URLs are not written here. Credentials are sent to `railway variable set` through standard input, with `--skip-deploys`; they are never command-line arguments. CLI output and errors are captured, not echoed. Secret inputs and generated values are not written to disk.

The separately supplied state path stores **names and statuses only**. It is created exclusively with mode 0600, and file and parent directory are synced before writes. Each attempted change is marked UNKNOWN durably before sending it. Inventory readbacks occur before and after changes. This is not an atomic remote transaction: a simultaneous external edit or an uncertain CLI outcome can still leave partial changes.

Any refusal or interruption stops the run. Never delete its state file, change its path, or rerun to bypass the stop. Reconcile the names-only record against authorized fresh remote evidence, preserve completed and uncertain changes, then obtain approval for any separately prepared continuation. Rollout remains HOLD even when the script reports COMPLETE_HOLD. Deploying, sealing and runtime health checks are separate sitting steps.

## Offline evidence boundary

The focused tests cover default dormancy, fixed target rejection before CLI access, missing confirmations, identity and inventory mismatches, sealed inputs, conflict refusal, stdin-only writes, interruption and duplicate-run refusal. Independent review and historical failed probes live outside the public source tree. Their CONDITIONAL verdict does not authorize execution or certify provider behavior.

### Minimal hidden input scope

Only names in `credential_plan.REQUIRED` are materialized or requested through hidden input. A manifest-present required value omitted/null in the CLI remains present/sealed, never absent or eligible for regeneration. Unrelated unchanged variables—including sealed omissions—are not prompted, copied or rewritten. Their names still participate in the complete manifest check and their raw CLI entries remain in every before/after drift snapshot. Hidden content absent from CLI cannot itself be compared; the complete current presence manifest and exclusive operator sitting remain external requirements.

Before filtering, the complete backup-trigger name manifest rejects every non-RAILWAY name except BACKUP_COORD_TOKEN and the fixed nonsecret BACKUP_LAUNCH_ENABLED control, including sealed extras. If that control is present in the manifest, its raw CLI value must be exactly the visible string `0`; omitted, null or nonzero values refuse without prompting or changing it. This deliberately preserves the existing strict trigger isolation policy rather than hiding extra credentials from the planner. Exact project/environment restrictions, full preflight, UNKNOWN behavior and no automatic retry are unchanged.

## Offline qualification (2026-11-07, synthetic transports only)

`devtools/gate5_tests/test_provision_operator_qualification.py` adds Q1–Q7. No CLI, network, real binding or real credential was used.

- **Q1 fixed:** a forbidden name in the manifest (for example a sealed Restic password on the observer) previously triggered a hidden prompt for that secret before refusing. It now refuses by name before any prompt.
- **Q2 fixed, residual remains:** hidden input now requires two matching entries. That catches typos only. A consistently wrong value for a sealed non-Ledger source is still copied to every destination; there is no remote oracle for it. Ledger-registered roles do refuse a wrong value through their digest.
- **Q3 narrowed:** it is still not verified whether `railway variable list --json` returns reference variables raw (`${{...}}`) or resolved. Since D02 the writer writes no references, so a resolving CLI no longer stops it mid-run; existing references are compared only against themselves. The question remains open for the routing readback (D05).
- **Q4–Q7 pass:** nothing printed to stdout/stderr by `execute`; state holds names and statuses only; one generated value reaches every direct destination; all reads and prompts precede the first write; a failed CLI write is checkpointed UNKNOWN, its error text is not echoed, the value goes only through stdin, and a rerun is refused.

Two older refusal tests use a raising prompt that the writer's catch-all swallows, so their "no prompt" half is not falsifiable. Q1 records prompts instead.
