// Railway infrastructure as code for the services needed for first revenue (go-live plan, Days 1-2).
// Runbook, every variable and every secret: docs/deploy/railway.md. Nothing here holds a secret value.
//
// Why this file and not railway.json / railway.toml: Railway deprecated Config as Code. New services cannot opt into
// it, and existing railway.json / railway.toml files stop being read on 2026-12-01 (docs.railway.com/config-as-code).
// This is its replacement, applied by the Railway CLI (`railway config plan`, then `railway config apply`); a git
// push alone does not apply it. The SDK version is pinned in .railway/package.json.
//
// Secrets: every secret is `preserve()` — declared here so a plan never deletes it, its value set once in the
// Railway dashboard and kept there. A value used by two services is set on ONE service and referenced by the other
// (`finance.env.FIN_SERVICE_TOKEN`), or interpolated with Railway's `${{service.VARIABLE}}` syntax inside a JSON map.
// Never use ctx.randomString for a secret: it is a hash of the environment name and a label, not random.
import { defineRailway, github, preserve, project, service, volume } from "railway/iac";
import type { ServiceNode, VolumeConfig, VolumeMount } from "railway/iac";

const REPO = "DarksiedCEO/zbm-zbc";
const BRANCH = "main"; // the branch Railway deploys from

// Build from the repository root (every Dockerfile's build context) with one service's Dockerfile; wait for the
// GitHub checks (CI) on the commit before deploying it.
const source = () => github(REPO, { branch: BRANCH, checkSuites: true });
const dockerfile = (path: string, watch: string[]) => ({
  builder: "DOCKERFILE" as const,
  dockerfilePath: path,
  watchPatterns: [...watch, "deploy/docker/**", ".railway/**"],
});

// One replica for everything: the ledger and the three departments hold a single-writer lock (flock) on their volume
// (Railway does not allow replicas with a volume anyway), and the orchestrator's one-scan-at-a-time lock is
// in-process (ADR 0001). ON_FAILURE, not ALWAYS: a refusal to start (a token missing, the ledger's integrity check)
// exits 1 and is retried at most 10 times; an operator override that exits 0 (LEDGER_ALLOW_RESET,
// LEDGER_MIGRATE_LEGACY) is not restarted with the override still set.
const deploy = (healthcheckPath: string, withVolume: boolean) => ({
  numReplicas: 1,
  restartPolicyType: "ON_FAILURE" as const,
  restartPolicyMaxRetries: 10,
  healthcheckPath,
  healthcheckTimeout: 120,
  // SIGTERM, then this long before SIGKILL: lets an in-flight append finish (the entrypoint forwards SIGTERM)
  drainingSeconds: 15,
  // no deploy without the volume at /data: never run a ledger or a department on the container's throwaway disk
  ...(withVolume ? { requiredMountPath: "/data" } : {}),
});

// Daily backups of every volume (AEGIS L6). The SDK's field is `backupSchedules` on a service's volume attachment
// (railway 3.13.1 .d.ts: `VolumeMount["backupSchedules"]`, `Array<"DAILY" | "WEEKLY" | "MONTHLY">`, also typed on
// `ServiceNode.volumeAttachments[name]`). service() builds the attachment from `volumeMounts: { "/data": vol }` but
// takes no schedule there, so it is set on the attachment it built; a service passed here without a volume
// attachment fails the evaluation rather than silently having no backup.
const DAILY_BACKUPS: VolumeMount["backupSchedules"] = ["DAILY"];

// The live data volumes as deployed (railway config plan, 2026-10-27). Changing either is a destructive volume change.
const VOLUME_AS_DEPLOYED: VolumeConfig = { region: "us-west2", sizeMB: 5000 };
const withDailyBackups = <T extends ServiceNode>(svc: T): T => {
  const attachments = Object.values(svc.volumeAttachments ?? {});
  if (attachments.length === 0) throw new Error(`${svc.name}: no volume attachment to give a backup schedule`);
  for (const a of attachments) a.backupSchedules = DAILY_BACKUPS;
  return svc;
};

const PRIVATE = (name: string, port: number) => `http://${name}.railway.internal:${port}`;

export default defineRailway(() => {
  // One volume per stateful service, mounted at /data (each service keeps its state in its own subdirectory).
  // Region and size state exactly what is deployed: left unset, `railway config plan` showed region "us-west2" -> null
  // and sizeMB 5000 -> null on all four as destructive changes. Never set sizeMB lower (shrinking is destructive).
  const ledgerData = volume("ledger-data", VOLUME_AS_DEPLOYED);
  const legalData = volume("legal-data", VOLUME_AS_DEPLOYED);
  const financeData = volume("finance-data", VOLUME_AS_DEPLOYED);
  const seoData = volume("seo-data", VOLUME_AS_DEPLOYED);
  // ADR 0024: new. Declared with the same region and size as the four live volumes, so the plan CREATES it and touches
  // nothing else (docs/deploy/railway.md, "Compliance (38): the hard gate").
  const complianceData = volume("compliance-data", VOLUME_AS_DEPLOYED);

  // ------------------------------------------------------------------------------------------------ ledger (first)
  const ledger = withDailyBackups(service("ledger", {
    source: source(),
    build: dockerfile("services/ledger-rust/Dockerfile", ["services/ledger-rust/**"]),
    deploy: deploy("/health", true),
    volumeMounts: { "/data": ledgerData },
    env: {
      PORT: "8090",
      LEDGER_BIND_ADDR: "[::]",
      LEDGER_LOG_PATH: "/data/ledger/ledger.jsonl",
      RAILWAY_RUN_UID: "0", // the entrypoint hands /data/ledger to uid 10001, then runs the ledger as 10001
      LEDGER_CALLERS: preserve(), // JSON map of sha256(token) -> caller scope (hashes only, set NOT Sealed); written to LEDGER_CALLERS_FILE
    },
  }));

  // ------------------------------------------------------------------------------------------------ legal
  const legal = withDailyBackups(service("legal", {
    source: source(),
    build: dockerfile("services/legal-py/Dockerfile", ["services/legal-py/**", "deploy/python/**"]),
    deploy: deploy("/health", true),
    volumeMounts: { "/data": legalData },
    env: {
      PORT: "8420",
      LEGAL_BIND_ADDR: "::",
      LEGAL_DATA_DIR: "/data/legal",
      RAILWAY_RUN_UID: "0",
      LEDGER_SERVICE_URL: PRIVATE("ledger", 8090),
      LEDGER_SERVICE_TOKEN: preserve(), // legal's own ledger caller token
      LEGAL_SERVICE_TOKEN: preserve(),
      LEGAL_ANDRE_APPROVAL_TOKEN: preserve(),
      // `onboarding`: Andre's operator-held token until an onboarding service exists (contract terms, obligations;
      // never a client's acceptance: ADR 0028); `client_portal`: the client acceptance page, which reaches only
      // Legal's two portal routes (its value is set once, on client-portal); `${{VARIABLE}}` is Railway's same-service
      // reference
      LEGAL_CALLER_TOKENS:
        '{"finance_31": "${{finance.FIN_LEGAL_CALLER_TOKEN}}", "onboarding": "${{LEGAL_ONBOARDING_CALLER_TOKEN}}", "compliance_38": "${{compliance.COMPLIANCE_LEGAL_CALLER_TOKEN}}", "client_portal": "${{client-portal.PORTAL_LEGAL_CALLER_TOKEN}}"}',
      LEGAL_ONBOARDING_CALLER_TOKEN: preserve(),
      // ADR 0024: Legal delivers counsel-memo register proposals to Compliance (caller legal_37). Compliance's service
      // token is set once, on compliance; Legal is declared before it (compliance reads Legal's token as code), so it
      // takes it through Railway's own reference syntax
      LEGAL_COMPLIANCE_URL: PRIVATE("compliance", 8380),
      LEGAL_COMPLIANCE_TOKEN: "${{compliance.COMPLIANCE_SERVICE_TOKEN}}",
      LEGAL_COMPLIANCE_CALLER_TOKEN: preserve(),
    },
  }));

  // ------------------------------------------------------------------------------------------------ compliance
  // ADR 0024: the hard gate before a client is served (activation), with the service-area geofence. PRIVATE: no
  // domain; Finance, SEO, detection and Legal reach it at compliance.railway.internal:8380. Its own scheduler runs the
  // internal controls every 6 h and the Change Watcher daily (in process: no `scheduler` token exists anywhere).
  // Every gate stays closed until control C-11 (evidence integrity) has passed. Declared after legal (it reads Legal's
  // service token) and before detection, finance and seo (they read its service token).
  const compliance = withDailyBackups(service("compliance", {
    source: source(),
    build: dockerfile("services/compliance-py/Dockerfile", ["services/compliance-py/**", "deploy/python/**"]),
    deploy: deploy("/health", true),
    volumeMounts: { "/data": complianceData },
    env: {
      PORT: "8380",
      COMPLIANCE_BIND_ADDR: "::",
      COMPLIANCE_DATA_DIR: "/data/compliance",
      RAILWAY_RUN_UID: "0",
      LEDGER_SERVICE_URL: PRIVATE("ledger", 8090),
      LEDGER_SERVICE_TOKEN: preserve(), // compliance's own ledger caller token (compliance-py, department compliance)
      COMPLIANCE_SERVICE_TOKEN: preserve(),
      COMPLIANCE_ANDRE_APPROVAL_TOKEN: preserve(),
      // `onboarding`: Andre's operator-held token until an onboarding service exists (runs a client's activation,
      // POST /compliance/v1/rule); the others are each caller's own variable on its own service
      COMPLIANCE_CALLER_TOKENS:
        '{"onboarding": "${{COMPLIANCE_ONBOARDING_CALLER_TOKEN}}", "finance_31": "${{finance.FIN_COMPLIANCE_CALLER_TOKEN}}", "legal_37": "${{legal.LEGAL_COMPLIANCE_CALLER_TOKEN}}", "seo_02": "${{seo.SEO_COMPLIANCE_CALLER_TOKEN}}", "revenue_recovery": "${{detection.DETECTION_COMPLIANCE_CALLER_TOKEN}}"}',
      COMPLIANCE_ONBOARDING_CALLER_TOKEN: preserve(),
      // Legal (37): a client's documents are current and accepted (HR-15)
      COMPLIANCE_LEGAL_URL: PRIVATE("legal", 8420),
      COMPLIANCE_LEGAL_TOKEN: legal.env.LEGAL_SERVICE_TOKEN,
      COMPLIANCE_LEGAL_CALLER_TOKEN: preserve(),
      COMPLIANCE_SCHEDULER_ENABLED: "1",
      COMPLIANCE_WATCHER_ENABLED: "1",
    },
  }));

  // ------------------------------------------------------------------------------------------------ detection
  // No volume (it keeps no state: snapshots are in memory, evidence on the ledger). The Revenue Recovery store
  // connector's three inputs are files the entrypoint writes from variables (--secret-file in the image), which needs
  // root at start: RAILWAY_RUN_UID=0, the service itself still runs as uid 10001 (runbook, "Revenue Recovery:
  // connecting a real store"). Every one of them unset = the connector stays off (NOT_CONNECTED / LEDGER_UNAVAILABLE).
  const detection = service("detection", {
    source: source(),
    build: dockerfile("services/detection-py/Dockerfile", ["services/detection-py/**", "deploy/python/**", "fixtures/*.json"]),
    deploy: deploy("/health", false),
    env: {
      PORT: "8000",
      DETECTION_BIND_ADDR: "::",
      RAILWAY_RUN_UID: "0", // secret files only: the entrypoint writes them as root, then runs detection as 10001
      ZBM_SERVICE_TOKEN: preserve(),
      DETECTION_LEDGER_URL: PRIVATE("ledger", 8090),
      DETECTION_LEDGER_TOKEN: preserve(), // detection's own ledger caller (rr_store); written to DETECTION_LEDGER_TOKEN_FILE
      RR_STORE_CONNECTIONS: preserve(), // Andre's approved connections JSON; written to RR_STORE_CONNECTIONS_FILE
      RR_STORE_SECRET_1: preserve(), // a store credential; written to /run/zbm-secrets/rr-store-secret-1-file
      RR_STORE_SECRET_2: preserve(), // a store credential; written to /run/zbm-secrets/rr-store-secret-2-file
      // Revenue Recovery store kill switches (runbook env table): set by hand to stop store reads; declared so a later
      // apply cannot release them. Unset/empty = off.
      RR_STORE_READS_DISABLED: preserve(),
      RR_STORE_KILL_SWITCH_FILE: preserve(),
      // ADR 0024: every store sync and every snapshot handed to the scan path needs Compliance's current ALLOW for the
      // client. Both tokens are written to files by the entrypoint (--secret-file in the image).
      DETECTION_COMPLIANCE_URL: PRIVATE("compliance", 8380),
      DETECTION_COMPLIANCE_TOKEN: compliance.env.COMPLIANCE_SERVICE_TOKEN,
      DETECTION_COMPLIANCE_CALLER_TOKEN: preserve(), // detection's `revenue_recovery` caller token at Compliance
    },
  });

  // ------------------------------------------------------------------------------------------------ finance
  const finance = withDailyBackups(service("finance", {
    source: source(),
    build: dockerfile("services/finance-py/Dockerfile", ["services/finance-py/**", "deploy/python/**"]),
    deploy: deploy("/health", true),
    volumeMounts: { "/data": financeData },
    env: {
      PORT: "8410",
      FIN_BIND_ADDR: "::",
      FIN_DATA_DIR: "/data/finance",
      RAILWAY_RUN_UID: "0",
      LEDGER_SERVICE_URL: PRIVATE("ledger", 8090),
      LEDGER_SERVICE_TOKEN: preserve(), // finance's own ledger caller token
      FIN_SERVICE_TOKEN: preserve(),
      FIN_ANDRE_APPROVAL_TOKEN: preserve(),
      // `onboarding`: Andre's operator-held token until an onboarding service exists (drafts invoices,
      // POST /fin/v1/invoices); `${{VARIABLE}}` is Railway's same-service reference
      FIN_CALLER_TOKENS:
        '{"rail_gateway": "${{stripe-gateway.STRIPE_GATEWAY_CALLER_TOKEN}}", "seo_02": "${{seo.SEO_FINANCE_CALLER_TOKEN}}", "onboarding": "${{FIN_ONBOARDING_CALLER_TOKEN}}"}',
      FIN_ONBOARDING_CALLER_TOKEN: preserve(),
      FIN_LEGAL_URL: PRIVATE("legal", 8420),
      FIN_LEGAL_TOKEN: legal.env.LEGAL_SERVICE_TOKEN,
      FIN_LEGAL_CALLER_TOKEN: preserve(),
      // ADR 0024: invoice approval, billing readiness and Stripe pages need Compliance's current ALLOW for the client;
      // payouts stay blocked by their other gates
      FIN_COMPLIANCE_URL: PRIVATE("compliance", 8380),
      FIN_COMPLIANCE_TOKEN: compliance.env.COMPLIANCE_SERVICE_TOKEN,
      FIN_COMPLIANCE_CALLER_TOKEN: preserve(),
      FIN_STRIPE_INCOMING: "1",
      FIN_STRIPE_LIVE: "0", // test mode first; "1" only together with live keys (docs/deploy/railway.md, step 9)
      FIN_STRIPE_SECRET_KEY: preserve(), // written to FIN_STRIPE_SECRET_KEY_FILE by the entrypoint
      FIN_STRIPE_WEBHOOK_SECRET: preserve(), // written to FIN_STRIPE_WEBHOOK_SECRET_FILE by the entrypoint
      FIN_STRIPE_SUCCESS_URL: preserve(),
      FIN_STRIPE_CANCEL_URL: preserve(),
    },
  }));

  // ------------------------------------------------------------------------------------------------ orchestrator
  // Public: the dashboard (Vercel) calls GET /revenue-recovery/findings over its Railway domain.
  const orchestrator = service("orchestrator", {
    source: source(),
    build: dockerfile("services/orchestrator-go/Dockerfile.orchestrator", ["services/orchestrator-go/**"]),
    deploy: deploy("/health", false),
    env: {
      PORT: "8080",
      ORCHESTRATOR_BIND_ADDR: "[::]",
      ORCHESTRATOR_SERVICE_TOKEN: preserve(),
      DETECTION_SERVICE_URL: PRIVATE("detection", 8000),
      DETECTION_SERVICE_TOKEN: detection.env.ZBM_SERVICE_TOKEN,
      LEDGER_SERVICE_URL: PRIVATE("ledger", 8090),
      LEDGER_SERVICE_TOKEN: preserve(), // the orchestrator's own ledger caller token
    },
  });

  // ------------------------------------------------------------------------------------------------ stripe-gateway
  // Public: Stripe POSTs webhooks to https://<its Railway domain>/webhooks/stripe.
  const stripeGateway = service("stripe-gateway", {
    source: source(),
    build: dockerfile("services/orchestrator-go/Dockerfile.stripe-gateway", ["services/orchestrator-go/**"]),
    deploy: deploy("/health", false),
    env: {
      PORT: "8470",
      STRIPE_GATEWAY_BIND_ADDR: "::",
      STRIPE_GATEWAY_FINANCE_URL: PRIVATE("finance", 8410),
      STRIPE_GATEWAY_ALLOW_RAILWAY_PRIVATE_HTTP: "1",
      // the opt-in reaches only http://<this name>.railway.internal[:port] (AEGIS L1): the finance service above
      STRIPE_GATEWAY_FINANCE_PRIVATE_NAME: "finance",
      STRIPE_GATEWAY_FINANCE_TOKEN: finance.env.FIN_SERVICE_TOKEN,
      STRIPE_GATEWAY_CALLER_TOKEN: preserve(),
    },
  });

  // ------------------------------------------------------------------------------------------------ seo
  const seo = withDailyBackups(service("seo", {
    source: source(),
    build: dockerfile("services/seo-py/Dockerfile", ["services/seo-py/**", "deploy/python/**"]),
    deploy: deploy("/health", true),
    volumeMounts: { "/data": seoData },
    env: {
      PORT: "8500",
      SEO_BIND_ADDR: "::",
      SEO_DATA_DIR: "/data/seo",
      RAILWAY_RUN_UID: "0",
      LEDGER_SERVICE_URL: PRIVATE("ledger", 8090),
      LEDGER_SERVICE_TOKEN: preserve(), // seo's own ledger caller token
      SEO_SERVICE_TOKEN: preserve(),
      SEO_ANDRE_APPROVAL_TOKEN: preserve(),
      SEO_CALLER_TOKENS: preserve(),
      SEO_LOG_HASH_KEY: preserve(), // written to SEO_LOG_HASH_KEY_FILE by the entrypoint
      // Optional: the Search Console service-account JSON key, written to SEO_GSC_SERVICE_ACCOUNT_FILE by the entrypoint.
      // Unset or empty = no file, Search Console NOT_CONNECTED (runbook, Secrets table).
      SEO_GSC_SERVICE_ACCOUNT: preserve(),
      SEO_FINANCE_URL: PRIVATE("finance", 8410),
      SEO_FINANCE_TOKEN: finance.env.FIN_SERVICE_TOKEN,
      SEO_FINANCE_CALLER_TOKEN: preserve(),
      // ADR 0024: a client tenant's paid run, every scheduled slot and every client report need Compliance's current
      // ALLOW for the client (no override)
      SEO_COMPLIANCE_URL: PRIVATE("compliance", 8380),
      SEO_COMPLIANCE_TOKEN: compliance.env.COMPLIANCE_SERVICE_TOKEN,
      SEO_COMPLIANCE_CALLER_TOKEN: preserve(),
      // Set by hand in the dashboard (runbook, Secrets table): declared so a later apply cannot delete them. Unset or
      // 0 keeps the engine NOT_CONNECTED / OFF, exactly as before (seo-py config, ADR 0017 W4).
      SEO_PERPLEXITY_API_KEY: preserve(),
      SEO_PERPLEXITY_DAILY_CALLS: preserve(),
      SEO_PERPLEXITY_DAILY_SPEND_USD: preserve(),
      SEO_PERPLEXITY_TENANT_DAILY_CALLS: preserve(),
      SEO_PERPLEXITY_TENANT_DAILY_SPEND_USD: preserve(),
      // Operator settings changed by hand during an incident (ADR 0017 W4-8 rollback; sticky kill switches): declared so
      // a later apply cannot silently undo them. Unset/empty = the documented default.
      SEO_PERPLEXITY_PROTOCOL: preserve(),
      SEO_PERPLEXITY_MODEL: preserve(),
      SEO_PERPLEXITY_RESERVE_USD_PER_CALL: preserve(),
      SEO_KILL_GLOBAL: preserve(),
      SEO_KILLED_CAPABILITIES: preserve(),
      SEO_KILLED_PROVIDERS: preserve(),
    },
  }));

  // ------------------------------------------------------------------------------------------------ client-portal
  // ADR 0028: the client acceptance page. Public: Andre generates its Railway domain once (runbook, "Client acceptance
  // page (ADR 0028)") and sets PORTAL_PUBLIC_ORIGIN to it; nothing else of this project gains a domain. Stateless: no
  // volume, no ledger; it holds Legal's service token (a reference), its own client_portal caller token at Legal and
  // its evidence key (HMAC of the client's IP address and user agent), and reaches Legal only, over the private network.
  // Declared after legal (it reads Legal's service token).
  const clientPortal = service("client-portal", {
    source: source(),
    build: dockerfile("services/client-portal-py/Dockerfile", ["services/client-portal-py/**", "deploy/python/**"]),
    deploy: deploy("/health", false),
    env: {
      PORT: "8480",
      PORTAL_BIND_ADDR: "::",
      PORTAL_LEGAL_URL: PRIVATE("legal", 8420),
      PORTAL_LEGAL_SERVICE_TOKEN: legal.env.LEGAL_SERVICE_TOKEN,
      PORTAL_LEGAL_CALLER_TOKEN: preserve(), // Legal's client_portal caller token (read by legal's LEGAL_CALLER_TOKENS map)
      PORTAL_EVIDENCE_KEY: preserve(), // HMAC key: client IP and user agent, form signatures; set once, rotate on suspicion
      PORTAL_PUBLIC_ORIGIN: preserve(), // https://<this service's Railway domain>, set by hand after the domain exists
      PORTAL_TRUSTED_PROXY_HOPS: "1", // Railway's edge adds the client to X-Forwarded-For (UNVERIFIED, runbook)
    },
  });

  return project("zbm-zbc", {
    resources: [ledgerData, legalData, financeData, seoData, complianceData, ledger, detection, legal, compliance, finance,
      orchestrator, stripeGateway, seo, clientPortal],
  });
});
