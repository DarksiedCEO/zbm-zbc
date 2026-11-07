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
   PID policy accepted: Railway-managed limits for client #1. Record each
   service process count; explicit per-container caps are post-launch hardening.
   Do not claim Mac caps apply on Railway.
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

## Remaining-phase templates (offline, review required)

```sh
python3 -B deploy/gate5/phase_templates.py
```

This generates resource, disabled configuration, credential-custody, URL routing,
activation, schedule and portal templates using the pinned f5fa44b images.
It performs no provider calls and never accepts or emits credential values.
Returned IDs fill named slots; secret values belong only in the future qualified
in-memory writer. No template is itself safe to submit as an API payload.

**Approved inputs:** `approved_decisions.json` records 256 MB Ledger, 512 MB
Python services, 1 vCPU each; new 5 GB volumes in us-west2; $35/month for the
whole workspace; 11:00 UTC nightly, starting the night after the sitting.
These are decimal units; verify effective Railway byte limits before activation,
without claiming equivalence to Docker MiB caps. The budget is not a hard
provider cap. Verify the workspace forecast before spending. Derive the first
due timestamp from the sitting completion date in America/Los_Angeles and
require it to be in the future. Never silently start an overdue run.

**Pre-sitting HOLDs:** qualify the secure production variable writer and complete
portal-specific settings/port preflight. These are not resolved by generating
templates and must not become unplanned live engineering at the sitting.
Only then freeze the filled non-secret plan for Claude review. Each stage must
have exact readback and explicit commit approval; never commit unrelated staged
changes. Existing schedules remain untouched until remote backup/restore PASS
and separate removal approval. No additional platform run is authorized.

## Source scope and crash policy clarification

The Railway `connect_service_source` tool contract explicitly says that
`staged: true` together with `environmentId` stages the source in **that one
 environment**, with no build until `accept-deploy`. Every generated source
operation supplies both fields. The prohibited form is live/default mode,
which changes all environments. Do not strip either field. Verify the staged
patch's environment and image before committing; having one environment is
not a safety argument. This is tool-contract evidence, not a new live test.

`NEVER` intentionally prevents the platform from automatically restarting a
failed owner and bypassing the repair decision. It is NOT proof of complete
crash alerting, nor founder acceptance of unattended downtime. The observer's
`cold_alerts.py` maps missing trigger, missed completion, backup failure,
archive full and recovery lock events to email. It does not monitor every
service process continuously. `admin_entrypoint.py` runs that dispatcher in
the observer process: an observer crash cannot send its own alert. The earlier
LOCKED inbox PASS proves transport delivery only, not arbitrary crash detection.
**HOLD before rollout:** settle outage/restart policy and independent notification
coverage. Do not silently enable automatic restart or promise 2 AM alerts.

Portal template now fixes port 8480, IPv6 bind, /health, origin from the returned
public domain, Pacific display timezone and the existing one-proxy/x-real-ip
contract. Domain creation precedes filling the origin; stage settings before
first application deploy. Verify proxy behavior and acceptance at the sitting.
Four portal secrets remain in the existing direct-copy credential manifest;
no volume, secret references or backup wrapper are introduced.

Local CLI compatibility observation: installed Railway 5.71.0 advertises
`variable set KEY --stdin --skip-deploys` plus explicit project/environment/service
selectors, matching `provision_operator.py`. This is credential-free local help,
not proof of sealed-value readback or a production write. Those require the
complete sitting inventory and guarded operator preflight; no write was made.

## Railway crash-notification sitting check (5–10 minutes; not yet proven)

Railway's [2025-11-21 notification announcement](https://railway.com/changelog/2025-11-21-notifications)
lists crashed deployments and out-of-memory kills under Build & deployment
failures, with Email & In-App delivery and project overrides. Founder confirms
that delivery mode and the target project's effective override at the sitting;
configuration alone is not a PASS.

Before client admission, obtain approval for the exact synthetic crash target,
recovery steps and any incremental cost. No new platform run or intentional
production crash is authorized by this checklist. Record the healthy deployment
ID, induced process exit and Railway's resulting deployment status, then confirm
the matching service/environment email actually arrives in andre's inbox.
A successful normal crash test does not certify OOM delivery or child-process
failure while a supervisor remains alive. Record those separately as unproven.
On missing email, stop admission and report; do not repeat automatically.

The observer is stateful: `/volume/control/monitor.sqlite` and
`/volume/control/outbox` persist schedule and delivery state. Restart-on-failure
is a proposal only, requiring safe replay/recovery evidence and explicit policy
approval. Keep the current template unchanged; no blanket stateless claim.
