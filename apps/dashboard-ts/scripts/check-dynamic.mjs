// Fails if `next build` prerendered "/" as a static page (fix wave 1).
// A static "/" bakes live orchestrator data — or, when built without
// ORCHESTRATOR_SERVICE_TOKEN, the "token is not set" error — into HTML that
// is then served forever. Run after `npm run build`: npm run check:dynamic
import { readFileSync } from "node:fs";

const manifest = JSON.parse(readFileSync(new URL("../.next/prerender-manifest.json", import.meta.url), "utf8"));
// /healthz (LOW-A, fix wave 1) reports live upstream health: a build-time
// snapshot of it would be the same bug.
const prerendered = Object.keys(manifest.routes ?? {});
for (const route of ["/", "/healthz"]) {
  if (prerendered.includes(route)) {
    console.error(`FAIL: "${route}" was prerendered at build time (static routes: ${prerendered.join(", ")})`);
    process.exit(1);
  }
}
console.log(`OK: "/" and "/healthz" are dynamic (prerendered routes: ${prerendered.join(", ") || "none"})`);
