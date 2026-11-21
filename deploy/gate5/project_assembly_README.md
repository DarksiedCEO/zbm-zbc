# Observed project conservation — HOLD, not an SDK apply graph

`project_assembly.mjs` consumes the explicitly supplied names/IDs/settings-only baseline shape produced by the approved `describe_environment` plus per-service `describe_service` read. It does not query Railway, import the SDK, compile a project, discover identities, provision resources or execute changes. It is a pure module with no command-line/main path.

This is substantive progress over a partial role list: it cross-checks the environment inventory against every service description, binds every observed mount in both directions to the global volume inventory, and conserves the entire supplied observation including domains, unrelated services, unknown fields, limitations, deployment metadata and variable names. The baseline is **not** a complete SDK graph. Omitted settings remain unknown, not defaults. Name-only variables become explicit preservation **obligations**, not invented SDK kinds, values or sealed status.

## Input and output

Call `assemble(baseline, request)` with a plain JSON baseline and request:

```js
{
  schema: 'gate5-observed-deltas-v1', status: 'HOLD', executable: false,
  baseline_sha256: digest(baseline),
  changes: [{
    service_name: 'ledger', service_id: 'ID_FROM_SUPPLIED_BASELINE',
    path: ['config', 'deploy', 'drainingSeconds'],
    before: {present: true, value: 15}, after: 45
  }],
  new_role_intents: [{name: 'client-portal', service_id: null,
                     mode: 'unprovisioned-proposal-only'}]
}
```

Only a small enumerated set of scalar source/deploy leaf fields can be proposed. Paths, identity, domains, mounts, variables, whole settings objects and unknown fields cannot be replaced or deleted. An unexposed leaf can be proposed with `before:{present:false}`; the artifact labels its prior observation `NOT_EXPOSED_UNKNOWN`, never "unset". Exact observed before-values must match. Duplicate paths refuse.

The result retains the entire original baseline and a separate projected copy; only the exact requested leaves change in the latter. The original observation is never relabelled as live desired state. The request is a proposal, not evidence of founder deployment authorization. A caller must not pass any portion of this artifact to the SDK or API as an apply payload. In particular, source changes can retain old selector fields: no normalization, removal or SDK compatibility is inferred.

`verifyConservation(baseline, request, artifact)` recomputes the expected artifact and refuses any altered or omitted content, including a tampered HOLD flag or lost domain/unknown setting. Counts and per-untouched-service hashes support review; they do not establish provider snapshot completeness or freshness. No caller-supplied success boolean bypasses checks.

New roles are sidecar intents only, with `service_id:null`. They are not appended to the observed resource inventory. Only the known prospective Compliance/coordinator/observer/backup-trigger/client-portal roles can be listed, without conflicting existing names or duplicates. They remain unprovisioned. No SDK lock binding or volume is fabricated.

Inputs and outputs can contain private infrastructure identifiers: keep real baseline/request/proposal files in the private preparation directory, never in public source. Tests contain invented names/IDs only. Inputs must already meet the approved names/settings-only collection scope; this tool is not a general secret scrubber. Explicit variable-value containers refuse, while other unknown metadata is preserved rather than silently dropped.

## Focused tests

```sh
node devtools/gate5_tests/project_assembly_test.mjs
python3 -B -m unittest discover -s devtools/gate5_tests -p binding_test.py
```

The assembly tests cover lossless conservation, missing descriptions, baseline-hash mismatch, duplicate/conflicting identities, mount conflicts, dropped domains/unknown fields/resources, unapproved changes, forbidden paths, stale before-values, duplicate changes, absent role IDs, unknown fields and staged/value-bearing baseline refusal. The binding tests also cover the observed seven-existing-service model: client-portal can be explicitly unprovisioned with no invented ID; other missing, unknown or duplicate identities refuse.

Builder evidence only, pending independent review. No provider action, SDK plan, live validation, credentials, image build, spending, deployment, commit or push is performed. The inspected SDK3.13.1 constructors are intentionally not used because their normalization prunes fields and cannot preserve a supplied observation losslessly. Real ID/lock reconciliation, exact SDK/apply semantics, new provisioning, supported PID/schedule controls, later baseline freshness and production approval remain HOLD blockers.
