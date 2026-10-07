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
  orderFindings,
  quotable,
} from "../src/lib/finding-view.ts";
import type { RecordedFinding, RecordedFindingsResult } from "../src/types/finding.ts";

console.error = () => {};

const ROW: RecordedFinding = {
  seq: 1, finding_id: "rrf1-" + "a".repeat(40), client_id: "fixture-pool", agent_id: "discount-misuse-v1",
  leak_category: "discount_misuse", entity_type: "order", entity_id: "o1", period_label: null, amount_usd: "4.50",
  value_classification: "observed", decision_confidence: "high", evidence_class: "OBSERVED", methodology_id: "m",
  scan_id: "1".repeat(32), payload_sha256: "0".repeat(64), recorded_at: "t", prev_hash: "p", hash: "h",
  amount_out_of_contract: false, first_seq: 1, times_recorded: 1, amounts_differ_across_records: false,
  present_in_latest_scan: true, value_basis: null, labels_exceed_evidence: null,
};
const STALE_EST: RecordedFinding = {
  ...ROW, seq: 0, entity_id: "o2", evidence_class: "ESTIMATED", value_classification: "attributed",
  decision_confidence: "medium", amount_usd: "12.00", present_in_latest_scan: false,
  value_basis: { base_usd: "120.00", rate_percent: "10" },
};
const NO_FIGURE: RecordedFinding = {
  ...ROW, seq: 2, amount_usd: null, value_classification: null, decision_confidence: null, evidence_class: "UNKNOWN",
};
const OVER: RecordedFinding = { ...ROW, seq: 3, evidence_class: "ESTIMATED", labels_exceed_evidence: "classification observed needs OBSERVED evidence" };

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

test("only a current, non-over-claiming figure is quotable", () => {
  assert.equal(quotable(ROW), true);
  assert.equal(quotable(STALE_EST), false);
  assert.equal(quotable(NO_FIGURE), false);
  assert.equal(quotable(OVER), false);
  assert.equal(quotable({ ...ROW, amount_out_of_contract: true }), false);
});

test("L3: every excluded-scan status has a label, abandoned included", () => {
  for (const s of ["running", "incomplete", "abandoned", "aborted", "inconsistent"] as const) {
    assert.ok(EXCLUDED_STATUS_TEXT[s].startsWith(s.toUpperCase()), s);
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
  for (const drop of ["present_in_latest_scan", "evidence_class", "client_id", "scan_id", "period_label"] as const) {
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
  for (const drop of ["excluded_scans", "legacy_findings", "legacy_finding_entries_ignored"] as const) {
    const body: Record<string, unknown> = { ...BODY };
    delete body[drop];
    assert.equal((await loadRecordedFindings(ENV, respond(body))).ok, false, drop);
  }
});
