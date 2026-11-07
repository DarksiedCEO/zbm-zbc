# Offline Gate5 binding layer — HOLD, not an apply program

`binding_proposal.py` consumes a complete validated topology plus a separate names/identity-only baseline context. It rejects altered existing volume IDs/mounts/source paths and unknown context fields. Output is explicitly NOT Railway JSON. It binds wrapper image digests, admin storage, entrypoints, disabled settings, central route proposals and UTC schedule. Missing implementation evidence for production limits or Compliance initialization remains a blocker. Synthetic tests use invented evidence only.

`binding_sdk.mjs` translates that proposal using actual installed Railway3.13.1 `service`, `image` and `preserve` constructors. It additionally requires digest-qualified pullable registry references and the complete existing variable-NAME inventory. It refuses unresolved production-limit/Compliance bindings, preserves existing variable names and does not replace owner image ENTRYPOINT secret-file handling. Admin entrypoints are explicit start commands. Root-wrapper image provenance remains an external requirement; the module never builds/pushes images.

The SDK result is a PARTIAL service-node definition set plus exact identity sidecar and separately labelled cutover fragments. It is not a complete project and MUST NOT be passed to apply. A partial project could remove omitted resources. There is deliberately no project constructor, export-to-live wrapper, CLI invocation or provider access.

## Verified offline SDK behavior and precise remaining blockers

- Local SDK `/Users/andrelove/PycharmProjects/zbm-zbc/.railway/node_modules/railway` is3.13.1. It is absent from this detached worktree; no dependency installation was done. Constructors were evaluated offline; this is not a full CLI plan or live schema qualification.
- CPU/memory map to `deploy.limitOverride.containers.cpu`/`memoryBytes`. The SDK deploy type has no PID field. Requested PID limit remains explicit in the sidecar and BLOCKED pending a supported binding; it is not silently discarded as applied.
- SDK `service()` addresses resources by name. Existing service IDs remain in the sidecar and require authoritative live-plan reconciliation before any apply. The adapter does not claim to preserve identities through a provider call it has never made.
- Configured mounts use exact volume IDs as keys and mountPath with empty backupSchedules; no volume creation/resize is emitted. New dedicated volume provisioning remains separate and approved.
- SDK constructor prunes `cronSchedule:null`. The first constructor test failed on this real behavior. Therefore absence cannot be claimed to clear an existing schedule. `SDK_PRUNES_NULL_CRON_EXISTING_SCHEDULE_REMOVAL_UNPROVEN` remains a blocker. The trigger's desired cron is retained only in the disabled schedule sidecar; no schedule is installed.
- Existing variable preservation is an SDK graph node `{type:preserve}`, not the compiled API `preserveExisting` field. A second test expectation exposed that distinction and was corrected. No claim that graph nodes are direct API payloads.
- Every generated role keeps BACKUP_LAUNCH_ENABLED disabled. Observer alerts are disabled; routing fragments are withheld from disabled preparation and labelled for explicit cutover. Even complete evidence inputs cannot enable/apply anything.

The smallest unclosed production integration is authoritative identity/volume reconciliation plus supported PID and schedule-removal bindings, complete-project merge with existing resources/settings, and real image references/build provenance. This work stops here rather than inventing SDK APIs or claiming an executable rollout. No production effects or approvals were consumed.

## Capacity binding added during local preparation

The proposal now carries the canonical non-secret `BACKUP_CAPACITY_POLICY` on all five owners and coordinator, with its SHA256 sidecar. Observer and trigger receive no data policy. The topology validator uses the runtime's pure parser and derived limits, without initializing global runtime policy. Arbitrary positive `stage`/`repository` values no longer qualify. The SDK adapter checks the canonical policy, identical digests and application-recovery source ceilings before constructing nodes.

Larger policies supported by the data-only capture/upload/restore code are **not** rollout-qualified: native application recovery still limits Ledger/Compliance sources to 2 MiB each and the other three to 512 KiB each. The rollout validator refuses higher values even with rebound evidence descriptors. These are existing local qualification bounds, not approved production capacity or memory sizing. Actual source growth, file-count bounds and first platform recovery remain sitting prerequisites. This change does not remove any apply/identity/schedule/PID blockers above.

## Offline checks

```
python3 -B -m unittest discover -s devtools/gate5_tests -p binding_test.py
node devtools/gate5_tests/binding_sdk_test.mjs /Users/andrelove/PycharmProjects/zbm-zbc/.railway/node_modules/railway
```

Seven Python tests passed; installed-SDK constructor/refusal assertions passed. These are developer checks, not independent certification. No keys, provider calls, commits or pushes.
