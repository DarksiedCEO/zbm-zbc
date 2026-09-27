import assert from "node:assert/strict";
import { test } from "node:test";

import { add, clamp, percent } from "../src/calc.ts";

test("clamp_bounds", () => {
  assert.equal(clamp(5, 0, 3), 3);
  assert.equal(clamp(-1, 0, 3), 0);
});

test("percent_basic", () => {
  assert.equal(percent(1, 4), 25.0);
});

test("add_returns_sum", () => {
  assert.equal(add(2, 3), 5);
});

test("skipped_placeholder", { skip: "placeholder" }, () => {});
