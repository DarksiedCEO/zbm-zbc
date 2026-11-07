# Gate 5 desired-state input plan (no apply capability)

`topology_plan.json` intentionally lacks deployment identities, approvals and evidence. It must fail validation. `topology_validator.py` only reads JSON and reports fixed blocker names; it cannot call Railway, build images, change variables or deploy. Even a fully populated plan is **not deployment authority**. `executable` must remain false.

Run locally:

```
python3 -B deploy/gate5/topology_validator.py deploy/gate5/topology_plan.json
python3 -B -m unittest discover -s devtools/gate5_tests -p topology_test.py
```

Exit2 means blocked/invalid; exit0 means only input completeness and fixed contracts passed. The offline tests use invented identities and evidence hashes; none constitutes platform qualification.

## Evidence and custody

Provide the exact future reviewed source commit, immutable built image digests, seven distinct bound volume IDs and separately approved resources. Four existing department volumes retain identity and /data mounts; Compliance has a new volume requiring explicit initialization proof. Owner native paths stay /data/<member>, with Ledger /data/ledger/ledger.jsonl; never move business data to /volume. Two dedicated admin volumes mount /volume. Confirm source/control/backup collisions absent before enabling.

Each evidence descriptor has `source_sha`, `artifact_sha256`, and `inputs_sha256`. The last is calculated by `inputs_digest(plan)` over all actual input fields excluding evidence descriptors. Changing limits, schedule, routes or identities invalidates earlier descriptors. A digest is only a binding, not a signature, authorization or proof of truthful contents: an independent reviewer/operator must inspect the referenced private artifacts, source CI and approvals. This tool cannot validate live volume ownership, DNS connectivity, initialized application contents or a human decision. Do not populate descriptors with invented hashes. Keep source evidence outside the public repository.

Only variable NAMES appear. Runtime provisioning must preserve all existing application variables in addition to listed maintenance names. The same newly generated dedicated coordination token is copied directly to all eight roles. No secret references, token values, credentials or provider responses belong in this manifest. URL references are non-secret only. The normal existing Docker ENTRYPOINT flags remain inherited from the approved base image; each owner contract lists that executable and its exact maintenance handoff/child command, not a replacement shell command. Do not skip its secret-file materialization by executing the handoff directly.

## Fixed topology

Five root-profile owners supervise native applications at loopback27000 and expose business27001, control27002 and dependency27003 privately. Approved supervisor wrapper images inherit their native application base. Coordinator uses the admin launcher `coordinator` role and central intake27001/control27002. Observer uses `observer`, private27003 and durable monitor/outbox. The `trigger` role is a scheduled, stateless, provider-credential-free command, not a persistent business process. Canonical private service names are fixed by existing code; no new provider simulator or email worker is proposed.

Start applications Ledger → Legal → Compliance → Finance → SEO, verify all five, then publish. Stop reverse order. Provision/start observer and closed owners before coordinator first startup. External consumers route all departmental paths through coordinator27001. Stripe gateway private-name allowlist must be coordinator, not finance. Any additional external consumers require explicit inventory and the same central route. Internal owner dependencies instead use the existing authenticated dependency relay. Do not route clients straight to owner27001, native ports or control paths. Coordinator /health signals supervisor liveness, not all-five readiness.

Schedule policy is deliberately fixed UTC with86400-second cadence and900-second grace; both cron and first due must align. This does not promise 03:00 Los Angeles across DST. Trigger success only proves submission/no-op, not backup completion.

## Remaining implementation and approval blockers

- These files do not install admin roles, root wrapper images, schedules or routes. The separate partial SDK adapter and nonexecutable conservation proposal remain preparation artifacts, not an apply-ready project. Current live application ports must not be mistaken for this desired state.
- Capture limits are mandatory inputs, bound to the coherent runtime capacity policy. Native application recovery/rollout reject source limits above the qualified ceilings: Ledger and Compliance 2 MiB; Finance, Legal and SEO 512 KiB. Larger data-only limits are not application qualification. No production limit is applied by this plan. There is currently no advance capacity warning; over-limit preflight refuses and uses the failure-alert path.
- Existing image/root-profile prerequisites, startup/drain/restart policies, private DNS/address-family connectivity and full five-service readiness require exact-source checks and observed evidence.
- Compliance initialization, actual volume identities, existing source contents and absence of directory collisions require independent evidence.
- Resource approval and reviewed cost estimate, exact CI/review, founder merge/deploy approval, credential provisioning, scheduled backup/alert/restore sitting and safe rollback remain separate gates.
- Report sending remains an explicit operator operation, not this backup topology. Revenue Recovery store connector remains excluded.

No provider effects or production changes are authorized or performed by this artifact.
