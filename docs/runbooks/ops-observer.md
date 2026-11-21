# Dormant operational observer

Default FIN_OPS_OBSERVER_ENABLED=0. No observer, sender or cron is activated by this source change.
After separate review/CI/operator approval, the optional ops_alerts caller must have its own distinct token in
FIN_CALLER_TOKENS, plus the existing service bearer. It reaches only POST /fin/v1/ops/observations/run with the existing
request_id body. FIN_OPS_OBSERVER_ENABLED=1 requires the existing owned FIN_DATA_DIR. FIN_OPS_HEALTH_URLS accepts
only finance/legal/compliance/ledger HTTP(S) root aliases, no credentials/query/fragment. These are trusted operator
endpoints, not caller-provided URLs. Do not point tests at live services. No notification credential is read.

Inspect fixed response status, persisted, persistence, generation, preview_id, coverage, open_count, stale_count and
preview category counts together. Remote holds/expiry are UNCONFIGURED. Zero counts or HTTP200 never establishes
complete operational health. A disabled result means no observation occurred. Repeated requests observe anew; no
sent/delivered/notification-success semantics exist. Health codes are immediate observations, not paging thresholds. Known log_write_fault/reconcile_required/
reconcile_mode true flags remain unhealthy even with status ok; malformed flags are UNKNOWN. Legal's optional
caller warning is retained only as a fixed optional_caller_unset code; no caller name is exposed.

Only COMPLETE scope coverage can remove its prior conditions. Removed sources retain stale conditions. Source data
older than300seconds/future timestamps does not clear. Clock rollback, bounds or persistence refusal must not be
interpreted as recovery. Polls do not hold Finance money lock, but DNS/OS operations are not forcibly cancellable;
shutdown can wait for such an in-flight observation. An independent monitor is still needed for total Finance outage.

State files are ops-observer.json and ops-observer.initialized in the already claimed data directory. Never delete
these to make a green result. Preserve both and sanitized diagnostics for owner review if missing/corrupt/oversized,
permissions invalid or persistence unknown. No automatic repair is implemented. On a transient filesystem failure,
correct the underlying condition and inspect surviving files before restarting the same compatible reader; a failed
post-replace fsync may have moved bytes, so restart is inspection of surviving state, not proof of prior commit.
Missing established state requires separate reviewed recovery rather than silently recreating empty state. External
destruction of both files is indistinguishable from first initialization here. They are not a financial journal,
backup mechanism, rollback guard or mail-delivery ledger. Do not modify financial journals for observer recovery.
