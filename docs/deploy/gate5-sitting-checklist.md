# Gate 5 sitting — HOLD; approval checklist

Preparation source and images remain separately pinned. This checklist is not
permission to write, spend, merge or admit clients. Estimates exclude CI queues
and unexpected failures; stop on a failed gate, with no automatic retry.

1. **Read-only baseline / rollback (10–15 min).** Confirm exact project,
   environment and existing service/volume IDs, creation after 2025-10-16,
   no staged changes, current rollback artifacts and preservation restore.
   Record current source bytes and measured per-client growth; admission stays
   closed if growth is unknown or projected usage reaches 50%.
2. **Decisions (10–15 min).** Approve exact reviewed commit/CI, digest manifest,
   new-role/volume monthly cost, CPU/RAM, UTC nightly cron and first due.
   PID decision remains open: accept platform-managed PID limits for client #1
   without claiming the Mac cap applies, or HOLD for a supported explicit cap.
   Approve the exact production delta and, separately, merge at rollout start.
3. **Secrets / closed preparation (15–25 min).** Hidden input only: dedicated
   new coordination token; scoped B2 keyID/applicationKey and existing Restic
   password; alert sending key; approved service-local matched credentials.
   Generate new matched credentials in memory once, copy directly to required
   services, preserve existing ones. URLs alone use references. No SSH token.
   Commission Compliance against legacy Ledger before its supervisor switch.
4. **Controlled rollout (20–30 min).** Approve new Compliance/control resources.
   Bind returned IDs; prepare scoped staged payloads, compare readback and obtain
   approval before committing each phase. Preserve all four existing /data
   volume identities, sizes, regions, domains and schedules. Keep ingress and
   client admission closed, trigger disabled until deliberate schedule activation.
5. **First backup / restore (20–35 min).** Set/read back approved cron, first due
   and observer cadence. Verify all-five seal, restart and actual traffic reopen.
   Verify B2 snapshots/catalog, restore into approved empty isolated targets and
   check application balances/acceptance. Delete targets and report cost.
   Only after both backup and restore pass may separately approved platform
   backup-schedule removal occur. Missing schedule observations are not empty.
6. **Portal (15–25 min).** Packages-only token session; publish approved standalone
   image, verify registry/scanned identity and anonymous pull. On denial stop;
   founder must inspect/approve any package deletion. Obtain explicit approval
   for new Railway service, public domain and monthly cost. Check public health
   and acceptance flow; no accepting on a client's behalf. Logout/revoke token.
7. **Admission (5–10 min).** Confirm alert inbox evidence, operator-run report,
   live Stripe configuration, real audit price in SOW 4.1, capacity report and
   explicit founder GO. Gate 5 remains HOLD until every preceding check passes.

## Dry-run command (no Railway access)

Run locally with the private names/settings-only baseline:

```sh
node deploy/gate5/apply_plan.mjs /absolute/private/production-baseline.json deploy/gate5/release_images.json
```

Output is a review artifact, not an API payload or deployment authorization.
It currently binds the four existing owner image/settings updates only.
New resources, secure variables, routing, resource limits and schedule activation
remain separate phases needing their concrete approved inputs and returned IDs.
The targeted tools support staged environment-scoped changes. Never use live
connect_service_source: its default scope includes every environment. Never apply
this repository's partial SDK graph. Before committing any stage, compare the
actual staged diff and settings to the baseline and approved fields; concurrent
changes invalidate the plan. No provider atomic compare-and-swap is claimed.
