// AEGIS M1/M4/L3 (Oct 7 2026): the findings page must make staleness, the
// evidence class, over-claiming labels, excluded scans and legacy entries
// visible. These pin the view decisions (src/lib/finding-view.ts) and the
// contract check that refuses a body without the fields (src/lib/api.ts);
// tests/status.live.test.mjs checks the rendered page on the wire.
//
// Run: npm test

import { test } from "node:test";
import assert from "node:assert/strict";

import { loadRecordedFindings } from "../src/lib/api.ts";
import {
  BADGE_TEXT,
  EXCLUDED_STATUS_TEXT,
  findingBadges,
  findingCounts,
  localQuotable,
  orderFindings,
  quoteVerdict,
  scanAsOfLabel,
} from "../src/lib/finding-view.ts";
import type { RecordedFinding, RecordedFindingsResult } from "../src/types/finding.ts";

console.error = () => {};

const ROW: RecordedFinding = {
  seq: 1, finding_id: "rrf1-" + "a".repeat(40), client_id: "fixture-pool", agent_id: "discount-misuse-v1",
  leak_category: "discount_misuse", entity_type: "order", entity_id: "o1", period_label: null, amount_usd: "4.50",
  value_classification: "observed", decision_confidence: "high", evidence_class: "OBSERVED", methodology_id: "m",
  scan_id: "1".repeat(32), payload_sha256: "0".repeat(64), recorded_at: "t", prev_hash: "p", hash: "h",
  amount_out_of_contract: false, first_seq: 1, times_recorded: 1, amounts_differ_across_records: false,
  present_in_latest_scan: true, value_basis: null, labels_exceed_evidence: null, quotable: true,
};
const STALE_EST: RecordedFinding = {
  ...ROW, seq: 0, entity_id: "o2", evidence_class: "ESTIMATED", value_classification: "attributed",
  decision_confidence: "medium", amount_usd: "12.00", present_in_latest_scan: false,
  value_basis: { base_usd: "120.00", rate_percent: "10" }, quotable: false,
};
const NO_FIGURE: RecordedFinding = {
  ...ROW, seq: 2, amount_usd: null, value_classification: null, decision_confidence: null, evidence_class: "UNKNOWN",
  quotable: false,
};
const OVER: RecordedFinding = { ...ROW, seq: 3, evidence_class: "ESTIMATED", labels_exceed_evidence: "classification observed needs OBSERVED evidence", quotable: false };

test("M1: a stale finding is badged stale; a current one is not", () => {
  assert.deepEqual(findingBadges(STALE_EST), ["stale", "estimated"]);
  assert.deepEqual(findingBadges(ROW), ["observed"]);
  assert.match(BADGE_TEXT.stale, /STALE/);
});

test("M1: every evidence class has its own visible label", () => {
  assert.deepEqual(findingBadges({ ...ROW, evidence_class: "MODELED" }), ["modeled"]);
  assert.deepEqual(findingBadges(NO_FIGURE), ["no-figure"]);
  assert.deepEqual(findingBadges(OVER), ["estimated", "overclaim"]);
  const texts = new Set(Object.values(BADGE_TEXT));
  assert.equal(texts.size, Object.keys(BADGE_TEXT).length, "two badges share a label");
});

test("M1: stale findings are listed after every current one", () => {
  const ordered = orderFindings([STALE_EST, OVER, ROW, NO_FIGURE]);
  assert.deepEqual(ordered.map((f) => f.present_in_latest_scan), [true, true, true, false]);
  assert.deepEqual(ordered.slice(0, 3).map((f) => f.seq), [1, 2, 3]);
});

test("M1: counts split current/stale and observed/estimated/no figure", () => {
  assert.deepEqual(findingCounts([ROW, STALE_EST, NO_FIGURE, OVER]), {
    current: 3, stale: 1, observed: 1, estimatedOrModeled: 2, noFigure: 1, overclaim: 1,
  });
});

test("N4: the served quotable flag decides; the local rule is only a consistency check", () => {
  assert.equal(quoteVerdict(ROW), "quotable");
  for (const f of [STALE_EST, NO_FIGURE, OVER]) assert.equal(quoteVerdict(f), "not-quotable");
  // An ESTIMATED current figure is not quotable as recorded (same rule as orchestrator-go).
  const est = { ...STALE_EST, present_in_latest_scan: true };
  assert.equal(localQuotable(est), false);
  // Served true but the local rule disagrees: shown as a mismatch, never as quotable.
  assert.equal(quoteVerdict({ ...est, quotable: true }), "mismatch");
  assert.equal(quoteVerdict({ ...ROW, present_in_latest_scan: false }), "mismatch");
  // Served false where the local rule would allow it: also a mismatch, shown as not quotable.
  assert.equal(quoteVerdict({ ...ROW, quotable: false }), "mismatch");
});

test("N3: a backdated scan's as_of is flagged", () => {
  const scan = { scan_id: "s", client_id: "c", data_source: "fixtures", fixture: true, tenant_defaulted: true,
    as_of: "2026-09-01T00:00:00Z", findings: 1, started_seq: 0, completed_seq: 2, backdated: false };
  assert.equal(scanAsOfLabel(scan), "2026-09-01T00:00:00Z");
  assert.match(scanAsOfLabel({ ...scan, backdated: true }), /^2026-09-01T00:00:00Z — BACKDATED/);
});

test("L3: every excluded-scan status has a label, abandoned included", () => {
  for (const s of ["running", "incomplete", "abandoned", "aborted", "inconsistent", "unsupported_format"] as const) {
    assert.ok(EXCLUDED_STATUS_TEXT[s].startsWith(s.toUpperCase().replace("_", " ")), s);
  }
});

// --- the contract check (api.ts) ---------------------------------------------------

const ENV = { ORCHESTRATOR_URL: "http://127.0.0.1:1", ORCHESTRATOR_SERVICE_TOKEN: "t" };
const BODY: RecordedFindingsResult = {
  findings: [ROW, STALE_EST, NO_FIGURE],
  overlapping_claims: {},
  scans: [],
  excluded_scans: [{ scan_id: "2".repeat(32), client_id: "fixture-pool", finding_events: 1, reason: "r", status: "abandoned", started_at: "t" }],
  ledger_entries_total: 9,
  ledger_total_source: "head",
  ledger_entries_read: 9,
  finding_entries_total: 3,
  legacy_finding_entries_ignored: 1,
  legacy_findings: [{ seq: 0, finding_id: "x", agent_id: "a", entity_id: "e", leak_category: "l", amount_usd: "1.00",
    amount_out_of_contract: false, value_classification: "observed", decision_confidence: "high", recorded_at: "t", hash: "h" }],
  ledger_verify: { valid: true, entries: 9, error: "" },
  non_live_data_source: true,
  latest_scan_uncounted: {},
};
function respond(body: unknown): typeof fetch {
  return (async () => new Response(JSON.stringify(body), { status: 200 })) as typeof fetch;
}

test("M1/M4: the new fields are consumed from the API", async () => {
  const o = await loadRecordedFindings(ENV, respond(BODY));
  assert.equal(o.ok, true);
  if (!o.ok) return;
  assert.equal(o.result.findings[1].present_in_latest_scan, false);
  assert.equal(o.result.findings[1].evidence_class, "ESTIMATED");
  assert.equal(o.result.findings[1].value_basis?.base_usd, "120.00");
  assert.equal(o.result.excluded_scans[0].status, "abandoned");
  assert.equal(o.result.legacy_findings.length, 1);
});

test("M1: a body whose findings lack staleness / evidence / tenant / scan is refused (502), not shown as current", async () => {
  for (const drop of ["present_in_latest_scan", "evidence_class", "client_id", "scan_id", "period_label", "quotable"] as const) {
    const row: Record<string, unknown> = { ...ROW };
    delete row[drop];
    const o = await loadRecordedFindings(ENV, respond({ ...BODY, findings: [row] }));
    assert.equal(o.ok, false, `finding without ${drop} accepted`);
    if (!o.ok) assert.equal(o.kind, "bad_response");
  }
  // An amount with evidence UNKNOWN (or none with OBSERVED) disagrees with itself.
  for (const row of [{ ...ROW, evidence_class: "UNKNOWN" }, { ...NO_FIGURE, evidence_class: "OBSERVED" }, { ...ROW, evidence_class: "GUESSED" }]) {
    assert.equal((await loadRecordedFindings(ENV, respond({ ...BODY, findings: [row] }))).ok, false, JSON.stringify(row));
  }
});

test("M4: a body without excluded_scans or legacy_findings is refused", async () => {
  for (const drop of ["excluded_scans", "legacy_findings", "legacy_finding_entries_ignored", "scans", "latest_scan_uncounted"] as const) {
    const body: Record<string, unknown> = { ...BODY };
    delete body[drop];
    assert.equal((await loadRecordedFindings(ENV, respond(body))).ok, false, drop);
  }
});

test("N3: scans without as_of/backdated are refused", async () => {
  const scan = { scan_id: "s", client_id: "c", data_source: "f", fixture: true, tenant_defaulted: true,
    as_of: "2026-10-01T00:00:00Z", findings: 1, started_seq: 0, completed_seq: 2, backdated: false };
  assert.equal((await loadRecordedFindings(ENV, respond({ ...BODY, scans: [scan] }))).ok, true);
  const { backdated: _b, ...noFlag } = scan;
  assert.equal((await loadRecordedFindings(ENV, respond({ ...BODY, scans: [noFlag] }))).ok, false);
});
