// Fails if `next build` prerendered "/" as a static page (fix wave 1).
// A static "/" bakes live orchestrator data — or, when built without
// ORCHESTRATOR_SERVICE_TOKEN, the "token is not set" error — into HTML that
// is then served forever. Run after `npm run build`: npm run check:dynamic
import { readFileSync } from "node:fs";

const manifest = JSON.parse(readFileSync(new URL("../.next/prerender-manifest.json", import.meta.url), "utf8"));
const prerendered = Object.keys(manifest.routes ?? {});
if (prerendered.includes("/")) {
  console.error(`FAIL: "/" was prerendered at build time (static routes: ${prerendered.join(", ")})`);
  process.exit(1);
}
console.log(`OK: "/" is dynamic (prerendered routes: ${prerendered.join(", ") || "none"})`);
