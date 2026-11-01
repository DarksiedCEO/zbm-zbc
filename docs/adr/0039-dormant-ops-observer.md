# ADR 0039: Dormant authenticated operational observations

Status: local implementation requiring independent review and CI. No notification activation.

The optional distinct ops_alerts caller reaches exactly POST /fin/v1/ops/observations/run (plus existing public
health). It does not acquire scheduler, founder, receipt or money permissions. FIN_OPS_OBSERVER_ENABLED defaults
to0; exactly1 enables observations only with a claimed Finance data directory. FIN_OPS_HEALTH_URLS optionally
maps only finance/legal/compliance/ledger aliases to trusted operator configured HTTP(S) roots, without credentials,
query or fragment. Invalid explicit configuration refuses startup. No new secret value, sender, dispatch interface,
provider, cron, Docker or Railway setting is introduced. Disabled calls return disabled and perform no observation I/O.

## Source truth and privacy

Health classifier comes from c09e5c15e7631d10319990260575852da2eaac8f; its URL variable is renamed, aliases restricted,
and module description updated for this consumer. Malformed/oversized reasons or warnings now classify unknown;
both supported reason arrays are combined rather than silently preferring one. Every supplied recognized field
is validated before selecting a definitive result, so a valid reason cannot hide malformed warnings/write status;
null/missing status and non-JSON error bodies yield unknown feed coverage. Actual Finance/Compliance/Legal
log_write_fault, reconcile_required and reconcile_mode fields are strict booleans when present; true values add
fixed unhealthy codes even if status says ok or another reason is present. Legal's warning status and its one
parameterized optional_caller_unset warning map to a fixed safe code, discarding the bounded caller suffix.
These classifications do not rewrite any upstream response or interpret other arbitrary service metadata. The observer uses immediate health readings, not the donor's
notification thresholds or dispatcher. The copied standalone track/run helpers retain their own regression tests;
they do not govern observer persistence or source removal. Five-second probe/30-second aggregate operation budgets
are not forced DNS/OS cancellation. No money lock is held during network or filesystem operations. Total Finance
outage cannot be detected by a route hosted in that same unavailable Finance process.

Snapshots carry source scope, acquisition time and COMPLETE/UNKNOWN/INCOMPLETE/UNCONFIGURED coverage. Only COMPLETE
removes that source's prior IDs. Unknown, stale (>300s), future, partial and removed sources retain prior observations
as stale. All seven fixed source scopes remain present, including unconfigured remote holds and expiry feeds. These
remote feeds have no complete-feed contract here; do not infer no holds/expiry from0conditions. Health failures are
fixed closed codes; local source consumes only validated random founder-alert break IDs and closed category/status
from the existing producer. Subject, client, note, amount, arbitrary severity, URL and response text never enter
state/preview. No sender imports or notification-success fields exist. Preview holds category counts; HTTP success
means the request was evaluated, not complete health or delivery. request_id is validated syntax, not send dedup.

## Same writer and private durability

An eager observer mutex serializes acquisition through persistence under the existing Finance directory claim.
Close takes observer mutex before the brief service lock; it never holds money lock while waiting for a poll.
Observer checks closed state and directory ownership. Local breaks are copied under service lock after polling;
subsequent source changes belong to a later observation, so this is a timestamped snapshot, not a live invariant.
Startup integrity remains prerequisite and no financial journal, ledger or money state is changed by observing.

State and initialized marker are fixed private files, opened without following symlinks; only owner regular files
with no group/other permissions are accepted. At most128KiB and512 total conditions, seven scopes and bounded integer
generations are retained. Capacity overflow refuses updates, never silently evicts open history. The initialized
marker is created and fsynced before first state; first initialization is not a health/recovery result. Missing
established state, corruption, unsafe permissions, unknown schema or conflicting state refuse reset. Deleting BOTH
marker and state externally cannot be distinguished from first initialization without a separate durable anchor.
That limitation is explicit; no financial anchoring or backup/restore authority is added for dormant previews.

Writes use exclusive private temporary files, file fsync, atomic replace, then directory fsync. Only after all finish
is the generation/preview identity published. A write/replace/fsync error latches unknown for the instance, including
post-replace fsync failure where bytes may already have moved. Prior known counts remain available as stale; no
in-memory dedup advancement or success is claimed. Restart reads the actual surviving bounded state; it does not
prove a previous failed call committed durably. No automatic deletion, replacement or repair of damaged state.

## Operator boundary

See the runbook for inspection and escalation. Do not activate a scheduler or send mail based on this slice. Separate
external trigger/transport, remote completeness contracts, independent review, CI and explicit activation approval
remain open. Persistence faults require operator inspection; the financial journal must never be rewritten to
clear an observation fault. No automatic retry or unattended repair is provided.
