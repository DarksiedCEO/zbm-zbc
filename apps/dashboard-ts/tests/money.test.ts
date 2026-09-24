// F15 (fix wave 1, Sep 24 2026): the shared money wire-format vectors in
// fixtures/money_vectors.json. The same file is loaded by detection-py
// (tests/test_money_vectors.py) and orchestrator-go
// (internal/client/money_vectors_test.go), so the three cannot drift.
//
// Run: npm test  (node's built-in test runner; Node >= 22.18 strips the
// TypeScript types itself — no extra dependency).

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { MAX_MONEY, MONEY_PATTERN, formatUsd, isMoneyString, isPositiveMoneyString } from "../src/lib/money.ts";

type Verdict = "accept" | "reject";
interface Vectors {
  contract: { pattern: string; max: string };
  string_vectors: { input: string; money: Verdict; positive_money: Verdict; note: string }[];
  json_vectors: { json: string; verdict: Verdict; note: string }[];
}

const here = dirname(fileURLToPath(import.meta.url));
const vectors: Vectors = JSON.parse(
  readFileSync(join(here, "..", "..", "..", "fixtures", "money_vectors.json"), "utf8")
);
const show = (s: string) => JSON.stringify(s.length > 32 ? s.slice(0, 32) + "..." : s);

test("vector file is present and non-trivial", () => {
  assert.ok(vectors.string_vectors.length >= 50);
  assert.ok(vectors.json_vectors.length >= 5);
});

test("contract block matches the dashboard implementation", () => {
  assert.equal(MONEY_PATTERN.source, vectors.contract.pattern);
  assert.equal(MAX_MONEY, vectors.contract.max);
});

test("isMoneyString / formatUsd agree with every string vector (money, zero allowed)", () => {
  for (const [i, v] of vectors.string_vectors.entries()) {
    const ok = v.money === "accept";
    assert.equal(isMoneyString(v.input), ok, `#${i} ${show(v.input)} ${v.note}`);
    assert.equal(formatUsd(v.input), ok ? `$${v.input}` : null, `#${i} ${show(v.input)}`);
  }
});

test("isPositiveMoneyString agrees with every string vector (positive-only)", () => {
  for (const [i, v] of vectors.string_vectors.entries()) {
    assert.equal(isPositiveMoneyString(v.input), v.positive_money === "accept", `#${i} ${show(v.input)} ${v.note}`);
  }
});

test("a JSON number (or any non-string) is never displayed as money", () => {
  for (const [i, v] of vectors.json_vectors.entries()) {
    const value: unknown = JSON.parse(v.json);
    assert.equal(isPositiveMoneyString(value), v.verdict === "accept", `#${i} ${v.json} ${v.note}`);
    assert.equal(formatUsd(value) !== null, v.verdict === "accept", `#${i} ${v.json}`);
  }
});
