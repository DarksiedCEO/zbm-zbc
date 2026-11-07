# Gate 5 cutover contract — preparation only

No command in this document is permission to deploy. The rollout is HOLD until the exact final source, image digests, offline review, CI, production readback and founder approvals are recorded. `deploy/gate5/topology_plan.json` is a desired-state input contract, not an apply plan. Existing `.railway/railway.ts` does not yet implement the backup topology and must not be applied as a complete cutover.

## Mandatory sitting hard stops

1. **Before any production write:** read the production environment's own `createdAt` and record its exact project/environment identity. It must be later than October 16, 2025. Missing/ambiguous dates or an older environment stop rollout with the IPv4-only wrappers. Repository creation dates and the new throwaway environment are not substitutes.
2. **Steps 4/5, traffic reopening:** after all five stores are locally sealed and verified, restart the applications, verify all-five readiness, then exercise the coordinator's existing publish/reopen operation and prove an authorized business request crosses the front door. Supervisor `/health`, `READY_CLOSED`, or the platform test alone do not prove traffic reopened. Keep new client admission disabled until the full backup/restore gate passes.
3. **Nightly schedule:** set the explicitly approved UTC cron, first-due timestamp and observer cadence; read them back from the exact production trigger/observer. Confirm the launch enablement and absence of unintended schedules. A local proposal or successful submission is not schedule readback or backup completion. Do not apply the partial SDK adapter to remove an unknown existing schedule.
4. **First real B2 backup and restore:** complete the first Railway backup, verify all five remote snapshots and catalog, then restore from B2 alone into separately approved empty targets. Check application recovery, money reconciliation and acceptance replay; prove target deletion and report posted cost with billing lag. Stop/rollback on failure. No clients before this passes.

The accepted November 7 throwaway run proved image pulls, `/data`, networking, native start/stop, encrypted local seal and restart into `READY_CLOSED`. It did not explicitly test forced kill/CAP_KILL, OS PID limits, schedules, traffic reopening or remote upload/restore. No more standalone platform runs are authorized. Owner listeners on 27001/27002 now explicitly use dual-stack sockets; the founder-requested environment-date check remains a sitting hard stop.

## Production image source and platform-backup removal

Production uses **GHCR image sources pinned by immutable digest, built from the exact final reviewed release commit**, with scan evidence and OCI revision labels tied to that commit. Do not mix GitHub branch builds with these images, or reuse qualification tags as production references. The legacy `.railway/railway.ts` GitHub-source definitions are not the cutover source. Record the per-service digest manifest outside the source commit after builds to avoid a self-referential commit hash.

Preserve all existing platform `backupSchedules` through deployment and the first real B2 backup **and B2-only restore**. Only after both pass at the sitting may the separately approved removal set `backupSchedules: []`; read back the result. The empty arrays in the partial SDK/legacy configuration are deferred removal proposals, never an initial-cutover apply payload. If B2 backup or restore fails, do not remove existing schedules. This does not authorize creating paid platform backups or upgrading plans.

## Fixed decisions

- Preserve the four existing volume identities, region, size and `/data` paths. Do not migrate, replace or clear any existing store.
- No Railway platform backup schedules, Pro upgrade or secret references. Copy matching secret values directly; use references only for non-secret URLs.
- Keep automatic deployment off. Merge alone is not deployment authorization. Stage no production variables before the approved sitting.
- Run the five owners with their existing application entrypoint beneath the approved supervisor wrapper; never bypass secret-file materialization.
- Keep report delivery operator-run. Do not deploy the Gate 4 recovery harness or synthetic fixture injection.
- The first platform cold backup and remote restore are mandatory before client admission. Prior Mac/B2 proof does not establish Railway behavior.

## Deployment sequence to bind in the final manifest

1. Record rollback image/deployment IDs and settings for every affected service; verify preservation snapshot and its password-custody evidence. Keep ingress closed and scheduled trigger disabled. Do not assume that an old image remains available indefinitely.
2. Before switching Ledger to supervision, stop the existing application writers while leaving the existing Ledger available at its legacy private port 8090. Keep ingress closed and the trigger disabled. Provision the approved new Compliance volume and matching Compliance Ledger grant on that existing Ledger; validate the grant in the separately authorized commissioning sitting. Run the packaged one-shot Compliance commissioning command in explicit `legacy-pre-supervisor` phase against the newly empty `/data/compliance` store, then retain its clean close/existing-store reconstruction evidence. This seeds a proposal, not founder approval or client activation. Do not commission through supervised Ledger27001 or dependency27003. On failure retain the partial store and Ledger evidence; never wipe or retry. After successful commissioning, clean-stop Ledger before replacing its deployment. Provision the remaining approved control volumes/observer and bind existing owner volumes at `/data` without migration; supply matched credentials through the reviewed secure provisioner. See `deploy/gate5/compliance_README.md` for the exact dormant image command.
3. Deploy closed owner supervisors, then start/check native applications Ledger → Legal → Compliance → Finance → SEO. Native application health and all-five readiness must be separately proven: coordinator `/health` alone is insufficient.
4. Start coordinator against the five owners and observer. Verify authenticated control paths and dependency relays; reject private DNS/address-family mismatches. Route external departmental consumers through coordinator, not directly to owner/native ports.
5. Deploy gateway, portal and any affected external consumers against the approved routes. Keep client admission closed; validate acceptance and payment-webhook refusal/replay handling without introducing a new charge.
6. Arm the approved UTC schedule and first-due timestamp; execute the initial cold backup and restore its B2 snapshots into explicitly approved isolated resources. Check application state, balances, acceptance and hashes; delete temporary resources and capture cost/absence evidence.
7. Confirm alert delivery, operator report procedure, live billing configuration and SOW price. Open client admission only on a separate founder GO after every preceding gate passes.

These are sequencing requirements, not evidence that the current IaC/provisioner can execute them. Missing initialization, routing, limits or image bindings remain implementation blockers.

## Rollback and failure rules

Before any client data, stop on the first failed service gate. Close ingress, disable the trigger, retain the failure and sealed copies, and restore the recorded application image/configuration only after checking state compatibility. A code rollback does not roll back a volume. Never overwrite a nonempty data path or blindly copy the preservation files onto a running store.

If state compatibility cannot be demonstrated, remain closed and use the supported remote-only restore into an empty approved target. Verify application recovery and reconciliation before switching traffic. Restore/replacement resources, spending, provider effects and production changes require the sitting's explicit scope. No automatic retry beyond that scope.

The preservation repository is excluded from operational repository destinations and deletion commands by the current application guard. This is not provider-side immutability: the scoped credential can still permit manual operations. Preserve the archive independently of rotating operational slots.

## Capacity and cost qualification

The local full-owner S3 diagnostic observed Ledger 214.54 MiB under 256 MiB and other owners 270.44–283.87 MiB under 512 MiB, with no OOM in that run. These are measured test peaks, not approved production limits or monthly average usage. Existing finite capture quotas are Mac-fixture quotas and require explicit production sizing plus runtime integration. Do not silently raise Railway limits.

Official Railway documentation checked during preparation:

- [Container pricing and Hobby billing](https://docs.railway.com/pricing/plans): RAM $10/GB-month, CPU $20/vCPU-month, egress $0.05/GB, storage $0.15/GB-month; Hobby's $5 minimum includes $5 usage. Removed Hobby deployment images have a 72-hour retention policy; bind rollback artifacts before the sitting.
- [Volume limits](https://docs.railway.com/volumes/reference): Hobby defaults to 5 GB per volume and 10 volumes per project; storage includes filesystem overhead. Proposed seven volumes fit the documented count, but actual entitlement and existing project occupancy still need fresh readback.
- [Private networking](https://docs.railway.com/networking/private-networking/how-it-works): environments created after October 16, 2025 have IPv4 and IPv6 DNS; legacy environments are IPv6-only. Check the actual target before relying on IPv4 listeners.

No live account, billing or production refresh is performed by this document. No fixed total bill, unlimited growth, uptime or deployment success is promised.
