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

const PRIVATE = (name: string, port: number) => `http://${name}.railway.internal:${port}`;

export default defineRailway(() => {
  // One volume per stateful service, mounted at /data (each service keeps its state in its own subdirectory).
  // Size: the plan's default (shrinking a volume is destructive, so it is not set lower here).
  const ledgerData = volume("ledger-data");
  const legalData = volume("legal-data");
  const financeData = volume("finance-data");
  const seoData = volume("seo-data");

  // ------------------------------------------------------------------------------------------------ ledger (first)
  const ledger = service("ledger", {
    source: source(),
    build: dockerfile("services/ledger-rust/Dockerfile", ["services/ledger-rust/**"]),
    deploy: deploy("/health", true),
    volumeMounts: { "/data": ledgerData },
    env: {
      PORT: "8090",
      LEDGER_BIND_ADDR: "[::]",
      LEDGER_LOG_PATH: "/data/ledger/ledger.jsonl",
      RAILWAY_RUN_UID: "0", // the entrypoint hands /data/ledger to uid 10001, then runs the ledger as 10001
      LEDGER_CALLERS: preserve(), // JSON map of sha256(token) -> caller scope; written to LEDGER_CALLERS_FILE
    },
  });

  // ------------------------------------------------------------------------------------------------ detection
  const detection = service("detection", {
    source: source(),
    build: dockerfile("services/detection-py/Dockerfile", ["services/detection-py/**", "deploy/python/**", "fixtures/*.json"]),
    deploy: deploy("/health", false),
    env: {
      PORT: "8000",
      DETECTION_BIND_ADDR: "::",
      ZBM_SERVICE_TOKEN: preserve(),
    },
  });

  // ------------------------------------------------------------------------------------------------ legal
  const legal = service("legal", {
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
      LEGAL_CALLER_TOKENS: '{"finance_31": "${{finance.FIN_LEGAL_CALLER_TOKEN}}"}',
    },
  });

  // ------------------------------------------------------------------------------------------------ finance
  const finance = service("finance", {
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
      FIN_CALLER_TOKENS:
        '{"rail_gateway": "${{stripe-gateway.STRIPE_GATEWAY_CALLER_TOKEN}}", "seo_02": "${{seo.SEO_FINANCE_CALLER_TOKEN}}"}',
      FIN_LEGAL_URL: PRIVATE("legal", 8420),
      FIN_LEGAL_TOKEN: legal.env.LEGAL_SERVICE_TOKEN,
      FIN_LEGAL_CALLER_TOKEN: preserve(),
      FIN_STRIPE_INCOMING: "1",
      FIN_STRIPE_LIVE: "0", // test mode first; "1" only together with live keys (docs/deploy/railway.md, step 9)
      FIN_STRIPE_SECRET_KEY: preserve(), // written to FIN_STRIPE_SECRET_KEY_FILE by the entrypoint
      FIN_STRIPE_WEBHOOK_SECRET: preserve(), // written to FIN_STRIPE_WEBHOOK_SECRET_FILE by the entrypoint
      FIN_STRIPE_SUCCESS_URL: preserve(),
      FIN_STRIPE_CANCEL_URL: preserve(),
    },
  });

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
      STRIPE_GATEWAY_FINANCE_TOKEN: finance.env.FIN_SERVICE_TOKEN,
      STRIPE_GATEWAY_CALLER_TOKEN: preserve(),
    },
  });

  // ------------------------------------------------------------------------------------------------ seo
  const seo = service("seo", {
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
      SEO_FINANCE_URL: PRIVATE("finance", 8410),
      SEO_FINANCE_TOKEN: finance.env.FIN_SERVICE_TOKEN,
      SEO_FINANCE_CALLER_TOKEN: preserve(),
    },
  });

  return project("zbm-zbc", {
    resources: [ledgerData, legalData, financeData, seoData, ledger, detection, legal, finance, orchestrator, stripeGateway, seo],
  });
});
