# One-time Compliance commissioning — builder preparation

This command seeds a **newly empty** Compliance store through the actual native application and Ledger client. It does not approve the seed, grant client activation, run a scheduler, expose an API, repair an old log or change the cold owner's existing-store requirement. No manual journal/empty-log seed is created.

Before execution, an operator must establish that external ingress is closed and existing Compliance processes are stopped. `--confirm-ingress-closed` is an **operator acknowledgment, not evidence or enforcement of routing closure**. The command itself starts no listener. A private deployment/mount/job binding and real Ledger validation still require separate integration/review.

Run from a checkout containing the current native Compliance source and pinned seed, using a Python environment with Compliance dependencies. The store must already exist, be canonical/absolute, owned by the executing UID, not group/world writable, and contain no entries (even an empty old log or old lock refuses). Do not target an existing or uncertain store.

Default offline preview:

```sh
python -B deploy/gate5/compliance_commission.py --store /absolute/owned/empty-compliance-store
```

The preview validates the local empty path and shipped seed hash; it creates no lock, log, service, scheduler or network connection.

Only after separate authorization, add `--execute --confirm-empty-store --confirm-ingress-closed --ledger-phase legacy-pre-supervisor`. This phase must precede switching Ledger to its supervised topology. `BACKUP_LAUNCH_ENABLED` must be unset or `0`. The command accepts only `LEDGER_SERVICE_URL=http://ledger.railway.internal:8090` and a configured Ledger token from `LEDGER_SERVICE_TOKEN`; credentials are not arguments or output. There is no endpoint override or CLI fake-Ledger option. This development task did not execute that network path or read real credentials.

Execution holds the actual native DataDirLock, verifies that only its newly created lock exists, constructs ComplianceService with default seed provisioning and the fixed seed SHA256 `bc6d371aa2703a231e03180b1c09907fc25a20dc09f44cdeb5a82f854df070f5`, requires the persisted seed proposal and clean native assessment, then closes. It constructs the native service again with `require_existing_seed=True`, verifies the same proposal and clean assessment, then closes and releases the lock. Normal instance-lease writes are expected on both constructions. This is an in-process close/reconstruction check, not an independent process boot or deployed cold-owner startup test.

On success the JSON output says `COMMISSIONED_CLOSED`, reports both construction passes, seed/log hashes, and explicitly says `seed_approved:false`, `listener_started:false`, `scheduler_started:false`. Save that output as operator evidence. The stored proposal still needs the normal authorized founder decision; this command never bypasses it.

On any failure, retain the partial native store and Ledger evidence. Do not wipe it or retry this command. Its strict empty-only preflight deliberately refuses partial attempts, old locks and existing logs. No automatic rollback/reconciliation is supplied.

## Builder tests and source binding

Executed against this worktree's actual source with the existing dependency environment:

```sh
/Users/andrelove/.codex/private/zbm-week-20261031/reviews/pr10-money/venv/bin/python -B \
  -m unittest discover -s devtools/gate5_tests -p 'test_compliance_*.py' -v
```

Six tests passed in 0.164 seconds. The private commissioning evidence retains the original output and hashes binding the command, tests and native source/seed files. Tests use actual ComplianceService/RecordLog/DataDirLock and the existing test-only FakeLedgerClient. They cover provisioning, clean close/existing-store reconstruction, one seed_loaded, retained native lease writes, empty-file/nonempty refusal, Ledger failure, seed mismatch, default no effects, explicit acknowledgments, canonical-path refusal and released lock. The api module is never imported, so its eager application construction is not triggered.

The default system python lacked httpx; no package was installed. The existing pr10-money venv supplied dependencies and loaded source explicitly from this checkout. No provider, Docker, real Ledger, secrets, keychain, production, commit or push was used. This is implementer evidence only and requires a separate reviewer; it is not AEGIS certification.

## Packaged invocation and rollout order

The Compliance Dockerfile copies the dormant script to `/app/deploy/gate5/compliance_commission.py`. Its repository-relative lookup then resolves `/app/services/compliance-py/src` and the already-packaged seed. The default image USER, ENTRYPOINT, native API command and disabled maintenance setting remain unchanged. Build-time syntax/path assertions do not run commissioning. No image build was performed by this task.

During the separately authorized sitting:

1. Close ingress and stop existing application writers. **Keep the legacy Ledger running** at its current internal8090; it must accept native seed and lease writes. Do not claim all writers including Ledger are stopped.
2. Provision the new Compliance Ledger grant on that existing Ledger, validate its identity/read/write permission in the authorized sitting, and supply the matching token securely. Provision only the approved empty Compliance volume; preserve all existing `/data` volumes, no migration.
3. Run the one-shot command from the new Compliance image with maintenance disabled, the new volume mounted at `/data`, and `COMPLIANCE_DATA_DIR=/data/compliance`. For a root-owned platform mount, the existing entrypoint prepares the directory and drops to UID10001. The exact command contract is:

```sh
/usr/local/bin/zbm-entrypoint --port-var COMPLIANCE_PORT \
  --state-dir-var COMPLIANCE_DATA_DIR -- \
  /opt/venv/bin/python -B /app/deploy/gate5/compliance_commission.py \
  --store /data/compliance --execute --confirm-empty-store \
  --confirm-ingress-closed --ledger-phase legacy-pre-supervisor
```

The deployment command override must replace the normal API command, not run alongside it; verify that binding before activation. No shell, scheduler or deployment job is installed by this patch. This is not a command for an already-running supervised owner.

4. Retain the successful native commissioning output and clean process exit. Then clean-stop legacy Ledger and switch to the approved five-owner topology. The initialized Compliance store now follows existing-store startup. Ledger27001 and relay27003 are **not** alternative commissioning endpoints; the command rejects both. A supervised commissioning adapter would require separate authenticated integration and is not supplied here.

The source-relative image test checks copied-script lookup and offline preview in a synthetic `/app` layout. The route test mocks HttpLedgerClient construction: it proves fixed8090/explicit-phase selection and27001/27003/maintenance-enabled refusal, not real connectivity or grant provisioning. Eight focused tests pass; The private commissioning evidence retains these results and the unchanged earlier six-test log.
