# Dormant one-shot housekeeping runner

This script is disabled by default and has no deployed schedule. Independent review of this slice and its fairness
base, CI/image validation, operator ownership and explicit activation authorization remain required.

Local executable entry: `python -I -B services/finance-py/src/housekeeping_runner.py`. No arguments are accepted
for operator use. It prints one sanitized JSON result and exits. The private worker flag is an internal protocol,
not an alternative invocation or deadline bypass supported for operators. No Finance volume is opened or required.

An eventual separately authorized configuration would provide:

- `FIN_HOUSEKEEPING_ENABLED`: exactly1 to enable; absent or0 yields `disabled` without a process/network attempt.
- `FIN_HOUSEKEEPING_ORIGIN`: trusted Finance origin only, with no path/query/userinfo. HTTPS or an explicit private
  Railway hostname; literal loopback is for owned local service/testing. Syntax alone does not authenticate Finance.
- `FIN_HOUSEKEEPING_SERVICE_TOKEN` and `FIN_HOUSEKEEPING_CALLER_TOKEN`: distinct service bearer and dedicated ops_cron
  token, matching the separately managed Finance caller mapping. Never use the broad scheduler/founder token.
- `FIN_HOUSEKEEPING_DEADLINE_SECONDS`: optional integer1–120(default60); total worker work deadline, including DNS/read.

No values are provisioned by this change. Do not place secrets in command arguments or logs. Do not use the Finance
service's volume-opening entrypoint to launch this script. Container packaging/entrypoint/scheduling is separate work;
no standalone runner image or hosted command was built or activated here.

`completed` means one validated response with no failed/unknown counters, not all pages gone or one-time execution.
`degraded`, `refused`, `invalid_settings` and `outcome_unknown` exit nonzero. `disabled` exits0 and must not be counted
as successful maintenance. The output contains only job, UTC-hour request ID, status, attempts and fixed counters.
Attempts means the one permitted worker request attempt; a DNS failure may have sent no bytes. Never infer no effect.

All503s, disconnects, invalid responses and timeouts are unknown. No automatic retry is allowed, including an
inherited Retry-After header. Inspect authorized local/provider evidence before any new invocation. A repeated UTC
hour ID does not deduplicate server work. Missing ticks leave durable work, but there is no catch-up or one-hour SLA;
finite-backlog fairness still depends on local commits and future invocations. Arrange overlap prevention and failed/
missing-tick notification ownership in a separate deployment procedure before activation. See stripe-housekeeping.md
for failure, ordinal and recovery limits. This runner neither restores state nor bypasses receipt rollback gates.
