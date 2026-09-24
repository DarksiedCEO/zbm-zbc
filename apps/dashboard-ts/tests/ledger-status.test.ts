// Fix wave 3: ledger-rust now answers an empty ledger with
// {"valid":true,"entries":0} (it used to be {"valid":false,"error":"Empty"},
// which the page special-cased by checking the entry count BEFORE the
// verdict — so an empty-but-invalid verdict was shown as "no findings yet").
//
// Run: npm test

import { test } from "node:test";
import assert from "node:assert/strict";

import { ledgerState } from "../src/lib/ledger-status.ts";
import type { RecordedFindingsResult } from "../src/types/finding.ts";

const base: RecordedFindingsResult = {
  findings: [],
  overlapping_claims: {},
  ledger_entries_total: 0,
  finding_entries_total: 0,
  ledger_verify: { valid: true, entries: 0, error: "" },
  non_live_data_source: true,
};

test("empty and valid -> empty", () => {
  assert.equal(ledgerState(base), "empty");
});

test("entries and valid -> verified", () => {
  assert.equal(ledgerState({ ...base, ledger_entries_total: 3, ledger_verify: { valid: true, entries: 3, error: "" } }), "verified");
});

test("an invalid verdict is always an integrity failure, even with zero entries", () => {
  assert.equal(ledgerState({ ...base, ledger_verify: { valid: false, entries: 0, error: "Empty" } }), "invalid");
  assert.equal(ledgerState({ ...base, ledger_entries_total: 2, ledger_verify: { valid: false, entries: 0, error: "x" } }), "invalid");
  assert.equal(ledgerState({ ...base, ledger_verify: null }), "invalid");
});
