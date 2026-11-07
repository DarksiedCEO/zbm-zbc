# Gate 5 provisioning inventory — names only, review draft

No values, provider inventory, execution or authority to apply. Derived from the current maintenance configuration and the private historical `zbm-week-20261031/reviews/pr44-rollout-script-20261101/configure.py`. Historical generation lists describe logical credentials, not evidence that any value is absent today. Existing matched values must be adopted, never silently regenerated. No secret references are permitted; only non-secret URL/domain/port references may remain.

## Historical generated logical roles

The old script generates at most eleven absent roles with OpenSSL, after validating all existing inputs. None may be generated again merely because it is sealed or unreadable.

| Service | Candidate generated names |
|---|---|
| legal | LEGAL_LINK_TAG_KEY; LEGAL_COMPLIANCE_CALLER_TOKEN |
| finance | FIN_COMPLIANCE_CALLER_TOKEN |
| seo | SEO_COMPLIANCE_CALLER_TOKEN |
| compliance | COMPLIANCE_SERVICE_TOKEN; COMPLIANCE_ANDRE_APPROVAL_TOKEN; COMPLIANCE_ONBOARDING_CALLER_TOKEN; COMPLIANCE_LEGAL_CALLER_TOKEN; LEDGER_SERVICE_TOKEN |
| client-portal | PORTAL_LEGAL_CALLER_TOKEN; PORTAL_EVIDENCE_KEY |

Additional newly generated logical role for the backup design: BACKUP_COORD_TOKEN, dedicated to coordination and never reused from an application token. One value is copied directly to each of ledger, legal, compliance, finance, seo, coordinator, observer and backup-trigger. OpenSSL generation is a future approved operator operation, not performed by this document.

BACKUP_RESTIC_PASSWORD, BACKUP_B2_KEY_ID and BACKUP_B2_APPLICATION_KEY are existing custody inputs, not newly generated application credentials. No second B2 key or master key is required. Existing backup password must remain unchanged to read existing snapshots.

## Exact direct-copy service-token and key groups

Each row is one logical value. Different rows must remain distinct, except where an explicitly listed destination is an alias of that row. All copies can be sealed separately.

| Logical source | Matching destinations |
|---|---|
| legal.LEGAL_SERVICE_TOKEN | finance.FIN_LEGAL_TOKEN; compliance.COMPLIANCE_LEGAL_TOKEN; client-portal.PORTAL_LEGAL_SERVICE_TOKEN |
| finance.FIN_SERVICE_TOKEN | seo.SEO_FINANCE_TOKEN; stripe-gateway.STRIPE_GATEWAY_FINANCE_TOKEN |
| compliance.COMPLIANCE_SERVICE_TOKEN | legal.LEGAL_COMPLIANCE_TOKEN; finance.FIN_COMPLIANCE_TOKEN; seo.SEO_COMPLIANCE_TOKEN |
| legal.LEGAL_LINK_TAG_KEY | client-portal.PORTAL_LINK_TAG_KEY |
| detection.ZBM_SERVICE_TOKEN | orchestrator.DETECTION_SERVICE_TOKEN |

The historical script does not populate Detection's Compliance pair. Revenue Recovery store connector remains excluded: do not invent or provision RR_STORE_CONNECTIONS, RR_STORE_SECRET_1 or RR_STORE_SECRET_2 for this launch. Existing disabled Detection configuration is preserved, not wiped. If a later approved launch enables Detection's Compliance calls, DETECTION_COMPLIANCE_TOKEN and DETECTION_COMPLIANCE_CALLER_TOKEN need their own reviewed matching grant; that is outside this inventory's generated-role list.

## Exact caller-map matching groups

Map notation denotes a JSON entry, not a separate Railway variable. Rebuild complete maps in memory only after validating every retained entry; never discard unknown existing Finance/SEO grants.

| Direct caller credential | Matching map entry |
|---|---|
| stripe-gateway.STRIPE_GATEWAY_CALLER_TOKEN | finance.FIN_CALLER_TOKENS.rail_gateway |
| seo.SEO_FINANCE_CALLER_TOKEN | finance.FIN_CALLER_TOKENS.seo_02 |
| finance.FIN_ONBOARDING_CALLER_TOKEN | finance.FIN_CALLER_TOKENS.onboarding |
| compliance.COMPLIANCE_ONBOARDING_CALLER_TOKEN | compliance.COMPLIANCE_CALLER_TOKENS.onboarding |
| finance.FIN_COMPLIANCE_CALLER_TOKEN | compliance.COMPLIANCE_CALLER_TOKENS.finance_31 |
| legal.LEGAL_COMPLIANCE_CALLER_TOKEN | compliance.COMPLIANCE_CALLER_TOKENS.legal_37 |
| seo.SEO_COMPLIANCE_CALLER_TOKEN | compliance.COMPLIANCE_CALLER_TOKENS.seo_02 |
| finance.FIN_LEGAL_CALLER_TOKEN | legal.LEGAL_CALLER_TOKENS.finance_31 |
| legal.LEGAL_ONBOARDING_CALLER_TOKEN | legal.LEGAL_CALLER_TOKENS.onboarding |
| compliance.COMPLIANCE_LEGAL_CALLER_TOKEN | legal.LEGAL_CALLER_TOKENS.compliance_38 |
| client-portal.PORTAL_LEGAL_CALLER_TOKEN | legal.LEGAL_CALLER_TOKENS.client_portal |

SEO_CALLER_TOKENS existing entries are preserved; the old script defines no new SEO inbound caller role. LEGAL_CALLER_TOKENS and COMPLIANCE_CALLER_TOKENS unsupported entries cause historical-script refusal rather than automatic deletion. Ledger LEDGER_CALLERS differs: it maps SHA256(token) to caller/department/write scope, not plaintext token. Compliance's new LEDGER_SERVICE_TOKEN must match the compliance-py/compliance grant digest. Existing individual Ledger caller credentials remain distinct: finance.LEDGER_SERVICE_TOKEN, legal.LEDGER_SERVICE_TOKEN, seo.LEDGER_SERVICE_TOKEN, orchestrator.LEDGER_SERVICE_TOKEN and detection.DETECTION_LEDGER_TOKEN must not be copied onto one another. Preserve their existing Ledger grants.

## Existing sealed inputs required when matching/provisioning

For each logical source or existing destination above that cannot be retrieved, the operator supplies its existing value through hidden input or explicitly approved Keychain access; never chat, argv, logs or plaintext files. Collect each logical value once in memory, compare all existing copies, refuse any conflict, and print names/status only. In particular the old script needs existing LEGAL_SERVICE_TOKEN, FIN_SERVICE_TOKEN, ZBM_SERVICE_TOKEN, STRIPE_GATEWAY_CALLER_TOKEN, SEO_FINANCE_CALLER_TOKEN, FIN_ONBOARDING_CALLER_TOKEN, FIN_LEGAL_CALLER_TOKEN and LEGAL_ONBOARDING_CALLER_TOKEN, plus any already-created eleven candidate roles and matching destinations.

Whole sealed caller maps FIN_CALLER_TOKENS, LEGAL_CALLER_TOKENS, COMPLIANCE_CALLER_TOKENS and SEO_CALLER_TOKENS need secure input when unreadable; retained extra grants cannot be reconstructed from guessed names. LEDGER_CALLERS must be available as the complete hash/grant map. Missing/omitted sealed-variable metadata is not proof that a secret is absent. A future script needs explicit existence/custody reconciliation rather than depending on null-versus-omitted CLI behavior.

Unchanged application inputs need not be re-entered merely to redeploy. Preserve service/approval tokens, existing Ledger caller tokens, SEO_LOG_HASH_KEY, FIN_STRIPE_SECRET_KEY, FIN_STRIPE_WEBHOOK_SECRET, existing SEO provider credentials and optional FIN_RECEIPT_RESEND_KEY. If building a complete replacement manifest instead of preserving settings, each required existing unreadable input becomes a secure custody prerequisite. Report-email credentials and BACKUP_RESEND_API_KEY are separate approved purposes; do not implicitly reuse a key just because both use Resend.

## Backup and alert distribution

| Names | Services holding them |
|---|---|
| BACKUP_COORD_TOKEN | all five owners; coordinator; observer; backup-trigger — eight direct copies of the newly dedicated token |
| BACKUP_RESTIC_PASSWORD; BACKUP_B2_KEY_ID; BACKUP_B2_APPLICATION_KEY | all five owners and coordinator — six direct copies per logical input |
| BACKUP_RESEND_API_KEY | observer only |
| BACKUP_ALERT_FROM; BACKUP_ALERTS_ENABLED; BACKUP_FIRST_DUE_UTC_SECONDS | observer; these are non-secret controls, not generated secrets |
| BACKUP_LAUNCH_ENABLED | each of eight roles; disabled until explicit rollout |
| BACKUP_CAPACITY_POLICY | identical canonical non-secret JSON on five owners and coordinator; compare the runtime SHA256 against the rollout manifest; no value is selected or applied here |
| BACKUP_MEMBER; BACKUP_VOLUME; BACKUP_RECOVERY_MODE; PORT; RAILWAY_RUN_UID | each owner as appropriate; fixed source path names remain LEDGER_LOG_PATH, LEGAL_DATA_DIR, COMPLIANCE_DATA_DIR, FIN_DATA_DIR, SEO_DATA_DIR |

Trigger must hold no B2, Restic, Resend or recovery/provider credentials. Observer need not hold B2/Restic credentials. Neither backup role requires Railway login credentials or SSH keys. Internal owner child-env filtering removes supervisor credentials; do not weaken it. Observer sends the primary to andre@zbestmedia.com and a hello@zbestmedia.com same-inbox copy, not an independent fallback. These recipients are compiled contract settings, not new secret variables.

Recovery-only roles, not normal nightly deployment: recover adds BACKUP_RECOVERY_STRIPE_KEY, FIN_SERVICE_TOKEN and FIN_RAIL_GATEWAY_CALLER_TOKEN to coordinator custody; the latter two match Finance's service token and FIN_CALLER_TOKENS.rail_gateway (therefore stripe-gateway.STRIPE_GATEWAY_CALLER_TOKEN). A read/verification purpose and test/live mode must be explicitly authorized before supplying the Stripe key. recover-trigger uses BACKUP_RECOVERY_REQUEST with coordination credentials and no provider secrets. BACKUP_RECOVERY_REQUEST contains a bounded recovery request, not a credential. Member recovery uses BACKUP_RECOVERY_MODE with the existing B2/password inputs. Do not provision these exceptional inputs for normal scheduled operation.

## URL and routing inventory

External consumers must enter coordinator's central intake, not owner/native ports. Non-secret URL references may resolve coordinator's private domain and fixed intake port; do not derive a route from an unrelated role's public port.

| Client variable | Central routing requirement |
|---|---|
| client-portal.PORTAL_LEGAL_URL | coordinator intake; /legal/ routes |
| stripe-gateway.STRIPE_GATEWAY_FINANCE_URL | coordinator intake; /fin/ routes; STRIPE_GATEWAY_FINANCE_PRIVATE_NAME must also identify coordinator |
| orchestrator.LEDGER_SERVICE_URL | coordinator intake; /ledger/ routes |
| detection.DETECTION_LEDGER_URL; DETECTION_COMPLIANCE_URL | coordinator intake if these existing clients are active; store connector remains excluded |
| Any dashboard/operator/custom direct department URL | inventory explicitly; coordinator intake for /ledger/, /legal/, /compliance/, /fin/, /seo/ |

Owner-internal dependency names remain LEDGER_SERVICE_URL on Legal/Compliance/Finance/SEO, LEGAL_COMPLIANCE_URL, COMPLIANCE_LEGAL_URL, FIN_LEGAL_URL, FIN_COMPLIANCE_URL, SEO_FINANCE_URL and SEO_COMPLIANCE_URL. owner_relay rewrites these internally to authenticated loopback relays, then destination private27003; do not route these through an external bypass or replace their caller credentials. Fixed ports: Ledger relay27110, Legal27111, Compliance27112, Finance27113. Native application is loopback27000; owner business27001, owner control27002; coordinator intake27001/control27002; observer monitoring27003.

PORTAL_PUBLIC_ORIGIN, FIN_STRIPE_SUCCESS_URL and FIN_STRIPE_CANCEL_URL are public-facing URLs with separate existing validations, not secrets or coordinator dependency routes. Preserve reviewed origins. No public domain may expose a control/relay/native listener.

## Why the old configure.py must not be applied

It is a historical review draft, not Gate5 apply-ready: it asserts Ledger8090/Legal8420, points portal directly at Legal, writes Compliance8380 and unsupervised bindings, assumes the old service list, has no coordinator/observer/trigger or backup custody, and does not bind exact wrapper image/volume/resource evidence. Its prior secret/direct-copy logic is useful source material, not authority to run it. --skip-deploys does not make its incompatible topology safe. Its sealed-variable and non-atomic partial-write assumptions still need explicit handling. Preserve the old artifact, do not silently mutate or execute it.

## Smallest remaining local implementation seam

A finite desired-state binding layer still must connect the reviewed topology plan to actual wrapper image digests, dedicated coordinator/observer volumes, admin entrypoints, trigger schedule and central external URL transition. It must retain existing volume identities/mounts, explicitly represent Compliance initialization, preserve all existing variables and match approved resource limits to actual runtime configuration. The current plan validator is intentionally input-only; it neither adds these IaC resources nor provisions secrets. The shortest safe next step is source-level SDK/type-checked IaC generation or a fixed staged configuration artifact with offline diff/refusal tests, not a general deployment framework and not a direct modification of the historical script.

That local wiring cannot create production evidence: actual sealed custody, service/volume IDs, source image builds, private DNS reachability, first initialization, shutdown/readiness behavior, schedule/alert/full restore and cost/rollout approvals remain independent checks. No new production configuration or apply capability is provided here.
