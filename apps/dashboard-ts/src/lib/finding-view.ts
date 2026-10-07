import type { ExcludedScan, RecordedFinding, ScanSummary } from "../types/finding.ts";

// AEGIS M1/M4/L3 (Oct 7 2026): what the findings page must make visible, as
// pure functions the tests pin. The page used to show every recorded finding
// alike: a finding the latest scan no longer found looked current, an
// ESTIMATED figure looked like an OBSERVED one, and legacy entries and
// excluded (failed, abandoned, inconsistent) scans were not shown at all.

// Every badge a recorded finding row carries, in display order.
export type FindingBadge =
  | "stale" // the client's latest completed scan no longer found it
  | "observed" // evidence OBSERVED: exact arithmetic on recorded data
  | "estimated" // evidence ESTIMATED: observed inputs + a stated assumption
  | "modeled" // evidence MODELED: from a statistical/attribution model
  | "no-figure" // evidence UNKNOWN: no defensible dollar figure
  | "overclaim"; // recorded labels claim more than the evidence supports

export function findingBadges(f: RecordedFinding): FindingBadge[] {
  const out: FindingBadge[] = [];
  if (!f.present_in_latest_scan) out.push("stale");
  switch (f.evidence_class) {
    case "OBSERVED":
      out.push("observed");
      break;
    case "ESTIMATED":
      out.push("estimated");
      break;
    case "MODELED":
      out.push("modeled");
      break;
    default:
      out.push("no-figure");
  }
  if (f.labels_exceed_evidence) out.push("overclaim");
  return out;
}

export const BADGE_TEXT: Record<FindingBadge, string> = {
  stale: "STALE — not in latest scan",
  observed: "OBSERVED",
  estimated: "ESTIMATED",
  modeled: "MODELED",
  "no-figure": "NO FIGURE",
  overclaim: "LABELS EXCEED EVIDENCE",
};

// AEGIS N4: whether a figure may go in a quote is orchestrator-go's decision
// (the served `quotable`). localQuotable restates the same rule only as a
// consistency check: the served flag is what the page shows, and a served
// "quotable" the local rule cannot confirm is shown as NOT quotable with a
// mismatch flag (fail closed). Overlaps are a separate gate (Decision 3).
export function localQuotable(f: RecordedFinding): boolean {
  return (
    f.amount_usd !== null &&
    !f.amount_out_of_contract &&
    f.present_in_latest_scan &&
    f.evidence_class === "OBSERVED" &&
    !f.labels_exceed_evidence &&
    f.value_basis === null // an OBSERVED figure is never rate-derived today
  );
}

export type QuoteVerdict = "quotable" | "not-quotable" | "mismatch";

export function quoteVerdict(f: RecordedFinding): QuoteVerdict {
  if (f.quotable !== localQuotable(f)) return "mismatch";
  return f.quotable ? "quotable" : "not-quotable";
}

// Current findings first (by seq), then stale ones (by seq): a stale row is
// never interleaved with current ones.
export function orderFindings(findings: RecordedFinding[]): RecordedFinding[] {
  return [...findings].sort((a, b) => {
    if (a.present_in_latest_scan !== b.present_in_latest_scan) return a.present_in_latest_scan ? -1 : 1;
    return a.seq - b.seq;
  });
}

export interface FindingCounts {
  current: number;
  stale: number;
  observed: number;
  estimatedOrModeled: number;
  noFigure: number;
  overclaim: number;
}

export function findingCounts(findings: RecordedFinding[]): FindingCounts {
  const c: FindingCounts = { current: 0, stale: 0, observed: 0, estimatedOrModeled: 0, noFigure: 0, overclaim: 0 };
  for (const f of findings) {
    if (f.present_in_latest_scan) c.current++;
    else c.stale++;
    if (f.evidence_class === "OBSERVED") c.observed++;
    else if (f.evidence_class === "ESTIMATED" || f.evidence_class === "MODELED") c.estimatedOrModeled++;
    else c.noFigure++;
    if (f.labels_exceed_evidence) c.overclaim++;
  }
  return c;
}

export const EXCLUDED_STATUS_TEXT: Record<ExcludedScan["status"], string> = {
  running: "RUNNING — being written now",
  incomplete: "INCOMPLETE — not finished (may still be running)",
  abandoned: "ABANDONED — never finished",
  aborted: "ABORTED — the scan failed",
  inconsistent: "INCONSISTENT — ledger records disagree",
  unsupported_format: "UNSUPPORTED FORMAT — written by a newer orchestrator-go (rolled back?)",
};

// AEGIS N3: a scan's as_of, and whether it is backdated (earlier than the
// previous scan's as_of).
export function scanAsOfLabel(s: ScanSummary): string {
  return s.backdated ? `${s.as_of} — BACKDATED (earlier than the previous scan's as_of)` : s.as_of;
}
