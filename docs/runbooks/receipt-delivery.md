# Receipt delivery preparation (mock-only)

No real mail is sent by this version. `NotBuiltClientMail` remains the production default. Do not treat a queued receipt as delivered.

1. Founder authentication is required to PUT a client's billing contact. Use one bounded ASCII mailbox; `email: null` disables it. The API returns enabled/revision only. Protect the private Finance journal and backups as recipient PII.
2. The existing scheduler-only receipt send route is unchanged. A missing contact or disabled adapter does not consume a claim. No credentials or configuration in this increment enable delivery.
3. Future adapters must consume the saved envelope unchanged and support its key throughout the entire retry interval. Before connecting one, independently validate acceptance versus retry semantics, ambiguous timeouts, provider deduplication, safe network behavior and deployment credentials. The 23-hour local bound is not provider certification.
4. After a first attempt, never edit an envelope, reset attempts/deadline, or generate another key. Changed contact, clock rollback, seven claims or expiry require founder review. Previously attempted receipts without an envelope cannot be safely retried by this increment.
5. For `delivery_uncertain` or `delivery_failed`, investigate externally and record an evidence ref/hash at the founder-only `delivery-review` route. This terminal acknowledgement records operator handling only. It neither marks delivery nor enables a resend. If a manual external action is necessary, manage duplicate risk outside this automated path; no automatic recovery from uncertainty is provided.
6. Check the private journal reader compatibility before any deployment rollback. This change adds collections; it does not implement restore or backward schema migration. Do not replace or edit durable state to bypass a refusal.

Receipt GET/send responses and ledger audit payloads omit recipient/body. Delivery status does not affect or book money. A withdrawn receipt cannot start delivery; an already accepted in-flight message may require a separate manual client correction.

## Reader compatibility preflight (local diagnostic only)

Before planning any reader transition, stop all Finance writers for the owned volume and preserve its ordinary backup/evidence bindings. Do not copy production secrets into a local check. On an authorized local volume, from `services/finance-py/src`, run:

```
python -B -m receipt_compatibility --data-dir /absolute/owned/data --target 774e46b2ca5fc9b5758eaba909450156b27d88c9
```

The other recognized source profile is `8e632340b70f32389a4fa3149023b0ed68dbe7c0`. An unrecognized target is refused. These exact source profiles do not verify a built image. Output is JSON: nonempty `reasons` means refusal (exit 2); empty reasons means only that this stopped local snapshot passes the bounded receipt capability check (exit 0). `snapshot_only` remains true; `artifact_verified`, `ledger_verified` and `deployment_authorized` remain false. No success output authorizes deployment or says an old image is safe to launch.

The check holds Finance's exclusive directory lock while reading and assessing the complete locally hash-verified history. A busy directory, missing/unreadable journal, nonregular file, torn/corrupt log, or log over 256 MiB refuses. Resolve the cause without deleting records or increasing limits casually; use a separately reviewed bounded reader for larger journals. Report only sanitized reason codes. A failed current startup releases its service claim, so a supported reader can be retried after the incompatible target is corrected; the process-level flock remains owned until that process exits, as before.

The CLI lock is released on return and the snapshot immediately expires. Never use its output as a reusable authorization token or launch an old reader afterward on its strength. The current reader repeats its check under the same claim held through replay and startup; a standalone scan followed by another process has no such guarantee.

**Deployment gate remains open:** an old image launched directly through Railway does not contain this guard. Before receipt activation, independently verify the exact compatible rollback image and enforce its selection outside this CLI, including GUI/manual rollback paths. No such external control is supplied here. Keep receipt provider activation blocked until that enforcement and the remaining credential/provider/CI gates are reviewed. Do not reset delivery uncertainty, regenerate envelope content or keys, drop contacts/envelopes, rewrite hashes, truncate logs, or restore Finance alone as a downgrade workaround. Use a compatible reader and preserve frozen state.
