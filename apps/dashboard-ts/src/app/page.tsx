import { headers } from "next/headers";

import {
  BADGE_TEXT,
  EXCLUDED_STATUS_TEXT,
  findingBadges,
  findingCounts,
  orderFindings,
  quoteVerdict,
  scanAsOfLabel,
  type FindingBadge,
} from "@/lib/finding-view";
import { decodeHandoff, HANDOFF_HEADER } from "@/lib/handoff";
import { ledgerState } from "@/lib/ledger-status";
import { formatUsd, isPositiveMoneyString } from "@/lib/money";
import type { ExcludedScan, LegacyFinding, RecordedFinding, RecordedFindingsResult, ScanSummary } from "@/types/finding";

// Always render per request, never at build time (fix wave 1). Without this,
// Next.js prerendered "/" as a static page during `next build`: built without
// ORCHESTRATOR_SERVICE_TOKEN, the "token is not set" error was baked into the
// HTML and served forever; built with it, a build-time snapshot would be.
export const dynamic = "force-dynamic";

const CONFIDENCE_COLOR: Record<string, string> = {
  low: "#8a8f98",
  medium: "#d4a017",
  high: "#3fa34d",
  very_high: "#2e7d32",
};

function ConfidenceBadge({ value }: { value: string }) {
  const color = CONFIDENCE_COLOR[value] ?? "#8a8f98";
  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: 12,
        fontSize: 12,
        fontWeight: 600,
        color: "#0b0d10",
        background: color,
        textTransform: "uppercase",
        letterSpacing: 0.3,
      }}
    >
      {value.replace("_", " ")}
    </span>
  );
}

// AEGIS M1 (Oct 7 2026): evidence class and staleness are badges on every
// row, with text (never colour alone), so an ESTIMATED figure or a finding
// the latest scan no longer found can never pass for a current OBSERVED one.
const BADGE_STYLE: Record<FindingBadge, { color: string; background: string; border: string }> = {
  stale: { color: "#f0b429", background: "#2a2410", border: "1px dashed #d4a017" },
  observed: { color: "#0b0d10", background: "#3fa34d", border: "1px solid #3fa34d" },
  estimated: { color: "#d4a017", background: "transparent", border: "1px solid #d4a017" },
  modeled: { color: "#b392f0", background: "transparent", border: "1px solid #b392f0" },
  "no-figure": { color: "#8a8f98", background: "transparent", border: "1px solid #4a4f58" },
  overclaim: { color: "#ffffff", background: "#a3302a", border: "1px solid #e5534b" },
};

function Badge({ kind }: { kind: FindingBadge }) {
  const st = BADGE_STYLE[kind];
  return (
    <span
      data-badge={kind}
      style={{
        display: "inline-block",
        padding: "1px 6px",
        marginRight: 4,
        marginBottom: 2,
        borderRadius: 4,
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.3,
        whiteSpace: "nowrap",
        ...st,
      }}
    >
      {BADGE_TEXT[kind]}
    </span>
  );
}

function AmountCell({ finding }: { finding: RecordedFinding }) {
  if (finding.amount_out_of_contract) return <span style={{ color: "#e5534b" }}>out-of-contract amount</span>;
  if (finding.amount_usd === null) return <>—</>;
  // Display guard: only a valid positive money string is ever shown as dollars.
  if (!isPositiveMoneyString(finding.amount_usd)) return <span style={{ color: "#e5534b" }}>invalid amount</span>;
  const shown = formatUsd(finding.amount_usd);
  // An ESTIMATED/MODELED figure is marked on the number itself ("est.").
  const estimated = finding.evidence_class === "ESTIMATED" || finding.evidence_class === "MODELED";
  return (
    <span style={{ textDecoration: finding.present_in_latest_scan ? undefined : "line-through" }}>
      {estimated ? <>est. {shown}</> : shown}
    </span>
  );
}

// AEGIS N4: orchestrator-go decides quotability; a disagreement with the
// local restatement of the rule is shown and treated as not quotable.
function QuoteCell({ finding }: { finding: RecordedFinding }) {
  const v = quoteVerdict(finding);
  const text =
    v === "quotable" ? "QUOTABLE" : v === "mismatch" ? "QUOTE CHECK MISMATCH — not quotable" : "not quotable";
  const color = v === "quotable" ? "#3fa34d" : v === "mismatch" ? "#e5534b" : "#8a8f98";
  return (
    <div data-quote={v} style={{ fontSize: 11, fontWeight: v === "not-quotable" ? 400 : 700, color, marginTop: 2 }}>
      {text}
    </div>
  );
}

// AEGIS N3: every counted scan with its as_of; a backdated one is flagged.
function Scans({ scans }: { scans: ScanSummary[] }) {
  if (scans.length === 0) return null;
  return (
    <section id="scans" style={SECTION_BOX}>
      <h2 style={{ fontSize: 15, margin: "0 0 10px" }}>Completed scans ({scans.length})</h2>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ textAlign: "left", color: "#8a8f98", fontSize: 12 }}>
            <th style={{ padding: "4px 8px" }}>Scan</th>
            <th style={{ padding: "4px 8px" }}>Client</th>
            <th style={{ padding: "4px 8px" }}>As of</th>
            <th style={{ padding: "4px 8px" }}>Findings</th>
            <th style={{ padding: "4px 8px" }}>Source</th>
          </tr>
        </thead>
        <tbody>
          {scans.map((x) => (
            <tr key={x.scan_id} data-backdated={x.backdated ? "true" : "false"} style={{ borderTop: "1px solid #1f2328" }}>
              <td style={{ padding: "6px 8px", fontFamily: "monospace" }}>{x.scan_id}</td>
              <td style={{ padding: "6px 8px" }}>{x.client_id}</td>
              <td style={{ padding: "6px 8px", color: x.backdated ? "#f0b429" : undefined, fontWeight: x.backdated ? 700 : 400 }}>
                {scanAsOfLabel(x)}
              </td>
              <td style={{ padding: "6px 8px", fontVariantNumeric: "tabular-nums" }}>{x.findings}</td>
              <td style={{ padding: "6px 8px", color: "#8a8f98" }}>
                {x.data_source}
                {x.fixture ? " (fixture)" : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function FindingRow({ finding }: { finding: RecordedFinding }) {
  const stale = !finding.present_in_latest_scan;
  const vb = finding.value_basis;
  return (
    <tr
      data-stale={stale ? "true" : "false"}
      data-evidence={finding.evidence_class}
      style={{ borderBottom: "1px solid #1f2328", opacity: stale ? 0.6 : 1 }}
    >
      <td style={{ padding: "10px 12px" }}>
        {findingBadges(finding).map((b) => (
          <Badge key={b} kind={b} />
        ))}
        {finding.labels_exceed_evidence && (
          <div style={{ fontSize: 11, color: "#e5534b", marginTop: 2 }}>{finding.labels_exceed_evidence}</div>
        )}
        <QuoteCell finding={finding} />
      </td>
      <td style={{ padding: "10px 12px", fontFamily: "monospace", fontSize: 13 }}>
        {finding.entity_id}
        {finding.period_label && <div style={{ fontSize: 11, color: "#8a8f98" }}>period {finding.period_label}</div>}
        <div style={{ fontSize: 11, color: "#8a8f98" }}>client {finding.client_id}</div>
      </td>
      <td style={{ padding: "10px 12px" }}>{finding.leak_category.replace(/_/g, " ")}</td>
      <td style={{ padding: "10px 12px", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
        <AmountCell finding={finding} />
        {vb && (
          <div style={{ fontSize: 11, color: "#8a8f98" }}>
            {vb.rate_percent}% of {formatUsd(vb.base_usd) ?? "invalid base"}
          </div>
        )}
        {finding.amounts_differ_across_records && (
          <div style={{ fontSize: 11, color: "#d4a017" }}>earlier records differ</div>
        )}
      </td>
      <td style={{ padding: "10px 12px" }}>
        {finding.decision_confidence ? (
          <ConfidenceBadge value={finding.decision_confidence} />
        ) : (
          <span style={{ fontSize: 12, color: "#8a8f98" }}>n/a</span>
        )}
      </td>
      <td style={{ padding: "10px 12px", fontSize: 12, color: "#b7bcc4" }}>
        {finding.value_classification?.replace("_", " ") ?? "n/a"}
      </td>
      <td style={{ padding: "10px 12px", fontSize: 12, color: "#8a8f98" }}>{finding.agent_id}</td>
      <td style={{ padding: "10px 12px", fontSize: 12, color: "#8a8f98", fontVariantNumeric: "tabular-nums" }}>
        #{finding.seq}
        {finding.times_recorded > 1 && ` (×${finding.times_recorded})`}
        <div style={{ fontFamily: "monospace", fontSize: 11 }} title={finding.scan_id}>
          scan {finding.scan_id.slice(0, 8)}
        </div>
      </td>
    </tr>
  );
}

const SECTION_BOX = {
  padding: 16,
  background: "#15181d",
  border: "1px solid #2b2f36",
  borderRadius: 8,
  marginTop: 24,
  fontSize: 13,
} as const;

// AEGIS M4/L3: scans in the ledger that do not count, each with its status.
function ExcludedScans({ scans }: { scans: ExcludedScan[] }) {
  if (scans.length === 0) return null;
  return (
    <section id="excluded-scans" style={SECTION_BOX}>
      <h2 style={{ fontSize: 15, margin: "0 0 4px" }}>Excluded scans ({scans.length}) — not counted</h2>
      <p style={{ color: "#8a8f98", margin: "0 0 10px" }}>
        In the ledger but never completed or not self-consistent. None of their findings appear above or count
        toward anything.
      </p>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ textAlign: "left", color: "#8a8f98", fontSize: 12 }}>
            <th style={{ padding: "4px 8px" }}>Status</th>
            <th style={{ padding: "4px 8px" }}>Scan</th>
            <th style={{ padding: "4px 8px" }}>Client</th>
            <th style={{ padding: "4px 8px" }}>Started</th>
            <th style={{ padding: "4px 8px" }}>Finding events</th>
            <th style={{ padding: "4px 8px" }}>Reason</th>
          </tr>
        </thead>
        <tbody>
          {scans.map((x) => (
            <tr key={x.scan_id} data-excluded-status={x.status} style={{ borderTop: "1px solid #1f2328" }}>
              <td style={{ padding: "6px 8px", fontWeight: 700, color: x.status === "running" ? "#3fa34d" : "#e5534b" }}>
                {EXCLUDED_STATUS_TEXT[x.status] ?? x.status}
              </td>
              <td style={{ padding: "6px 8px", fontFamily: "monospace" }}>{x.scan_id}</td>
              <td style={{ padding: "6px 8px" }}>{x.client_id || "—"}</td>
              <td style={{ padding: "6px 8px" }}>{x.started_at || "—"}</td>
              <td style={{ padding: "6px 8px", fontVariantNumeric: "tabular-nums" }}>{x.finding_events}</td>
              <td style={{ padding: "6px 8px", color: "#b7bcc4" }}>{x.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function LegacyAmount({ row }: { row: LegacyFinding }) {
  if (row.amount_out_of_contract) return <span style={{ color: "#e5534b" }}>out-of-contract amount</span>;
  if (row.amount_usd === null || !isPositiveMoneyString(row.amount_usd)) return <>—</>;
  return <>{formatUsd(row.amount_usd)}</>;
}

// AEGIS M4: pre-Oct-6-2026 ledger findings — shown for the record, labelled,
// never counted and never quotable.
function LegacyFindings({ rows, count }: { rows: LegacyFinding[]; count: number }) {
  if (count === 0 && rows.length === 0) return null;
  return (
    <section id="legacy-findings" style={SECTION_BOX}>
      <h2 style={{ fontSize: 15, margin: "0 0 4px" }}>Legacy ledger findings ({count}) — LEGACY, not counted</h2>
      <p style={{ color: "#8a8f98", margin: "0 0 10px" }}>
        Recorded before the Oct 6 2026 fix wave: no scan, no client, and amounts from before the evidence-class
        corrections. Kept in the ledger as history; never part of the findings above or of any quote.
      </p>
      <table style={{ width: "100%", borderCollapse: "collapse", opacity: 0.75 }}>
        <thead>
          <tr style={{ textAlign: "left", color: "#8a8f98", fontSize: 12 }}>
            <th style={{ padding: "4px 8px" }}>Ledger seq</th>
            <th style={{ padding: "4px 8px" }}>Entity</th>
            <th style={{ padding: "4px 8px" }}>Leak Category</th>
            <th style={{ padding: "4px 8px", textAlign: "right" }}>Recorded value (legacy)</th>
            <th style={{ padding: "4px 8px" }}>Agent</th>
            <th style={{ padding: "4px 8px" }}>Recorded at</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.seq}`} data-legacy="true" style={{ borderTop: "1px solid #1f2328" }}>
              <td style={{ padding: "6px 8px", fontVariantNumeric: "tabular-nums" }}>#{r.seq}</td>
              <td style={{ padding: "6px 8px", fontFamily: "monospace" }}>{r.entity_id}</td>
              <td style={{ padding: "6px 8px" }}>{r.leak_category.replace(/_/g, " ")}</td>
              <td style={{ padding: "6px 8px", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                <LegacyAmount row={r} />
              </td>
              <td style={{ padding: "6px 8px", color: "#8a8f98" }}>{r.agent_id}</td>
              <td style={{ padding: "6px 8px", color: "#8a8f98" }}>{r.recorded_at}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function LedgerStatus({ result }: { result: RecordedFindingsResult }) {
  const v = result.ledger_verify;
  // The verdict is checked first (fix wave 3): an empty ledger is a valid
  // chain, so "no findings yet" is only shown for a ledger that verified.
  const state = ledgerState(result);
  if (state === "invalid") {
    return (
      <div style={{ padding: 16, background: "#3a1f1f", border: "1px solid #6b2b2b", borderRadius: 8, marginBottom: 20 }}>
        <strong>LEDGER INTEGRITY FAILURE:</strong> the evidence ledger did not verify
        {v?.error ? <> (<code>{v.error}</code>)</> : null}. Do not rely on the findings below.
      </div>
    );
  }
  if (state === "empty") {
    return (
      <div style={{ padding: 16, background: "#1f2328", border: "1px solid #2b2f36", borderRadius: 8, marginBottom: 20, fontSize: 13 }}>
        Evidence ledger verified and empty — no findings recorded yet. Run a scan with{" "}
        <code>POST /revenue-recovery/scan</code> on orchestrator-go; this page only reads what the ledger has recorded.
      </div>
    );
  }
  return (
    <div style={{ fontSize: 13, color: "#3fa34d", marginBottom: 12 }}>
      Evidence ledger hash chain verified ({v?.entries} entries).
    </div>
  );
}

// LOW-A (fix wave 1): the findings are read by src/proxy.ts, which also sets
// the HTTP status (200/502/503) from the same outcome and hands it here — a
// page component cannot set a 5xx status itself (src/lib/handoff.ts). The
// page renders exactly that outcome and does not call the orchestrator.
export default async function Page() {
  const outcome = decodeHandoff((await headers()).get(HANDOFF_HEADER));
  if (!outcome) {
    // Fail closed: without the proxy's outcome the status on the wire could
    // not match the page (the LOW-A bug). Throwing makes Next answer 500.
    throw new Error("dashboard: no findings handoff from src/proxy.ts — the proxy did not run for this request");
  }
  const result: RecordedFindingsResult | undefined = outcome.ok ? outcome.result : undefined;
  const loadError: string | null = outcome.ok ? null : `${outcome.message} (correlation id ${outcome.correlationId})`;
  const counts = findingCounts(result?.findings ?? []);

  return (
    <main style={{ maxWidth: 1100, margin: "0 auto", padding: "32px 24px" }}>
      <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 4 }}>Revenue Recovery — Recorded Findings</h1>
      <p style={{ color: "#8a8f98", fontSize: 13, marginBottom: 20 }}>
        Tier 1A · fixture data only, no live client connected · read from the evidence ledger (viewing this page
        never runs a scan or writes to the ledger)
      </p>

      {loadError && (
        <div style={{ padding: 16, background: "#3a1f1f", border: "1px solid #6b2b2b", borderRadius: 8, marginBottom: 20 }}>
          Could not load recorded findings from the orchestrator at the configured ORCHESTRATOR_URL:{" "}
          <code>{loadError}</code>. Start <code>services/ledger-rust</code> and <code>services/orchestrator-go</code>{" "}
          first.
        </div>
      )}

      {result && (
        <>
          <LedgerStatus result={result} />

          <div style={{ display: "flex", flexWrap: "wrap", gap: 16, marginBottom: 12, fontSize: 13, color: "#b7bcc4" }}>
            <span>{result.findings.length} distinct findings</span>
            <span>{counts.current} current</span>
            <span style={{ color: counts.stale > 0 ? "#f0b429" : undefined }}>{counts.stale} stale</span>
            <span>
              {counts.observed} observed · {counts.estimatedOrModeled} estimated/modeled · {counts.noFigure} no figure
            </span>
            <span>{result.finding_entries_total} finding entries recorded</span>
            <span>{result.ledger_entries_total} ledger entries in total</span>
            <span>{Object.keys(result.overlapping_claims).length} overlapping-claim entities</span>
            <span style={{ color: result.excluded_scans.length > 0 ? "#e5534b" : undefined }}>
              {result.excluded_scans.length} excluded scans
            </span>
            <span>{result.legacy_finding_entries_ignored} legacy entries</span>
          </div>
          <p style={{ fontSize: 12, color: "#8a8f98", marginBottom: 20 }}>
            STALE: the client&apos;s latest completed scan no longer found it — not current, never quote it.
            ESTIMATED / MODELED figures rest on a stated assumption (&quot;est.&quot;); only OBSERVED figures are
            exact arithmetic on recorded data.
          </p>
          {Object.keys(result.latest_scan_uncounted).length > 0 && (
            <div
              id="latest-uncounted"
              style={{ padding: 16, background: "#3a1f1f", border: "1px solid #6b2b2b", borderRadius: 8, marginBottom: 20, fontSize: 13 }}
            >
              <strong>The latest scan could not be counted</strong> for{" "}
              {Object.entries(result.latest_scan_uncounted)
                .map(([client, scan]) => `${client} (scan ${scan})`)
                .join(", ")}
              . No finding of that client is current or quotable until it is resolved — see Excluded scans. If its
              status is UNSUPPORTED FORMAT, this orchestrator-go is older than the one that wrote it: roll forward.
            </div>
          )}
          {counts.overclaim > 0 && (
            <div
              style={{ padding: 16, background: "#3a1f1f", border: "1px solid #6b2b2b", borderRadius: 8, marginBottom: 20, fontSize: 13 }}
            >
              <strong>{counts.overclaim} recorded finding(s) carry labels their evidence does not support</strong>{" "}
              (recorded before the Oct 7 2026 evidence-class invariant). Their figures must not be quoted as
              recorded; a new scan records them with corrected labels.
            </div>
          )}

          {Object.keys(result.overlapping_claims).length > 0 && (
            <div
              style={{
                padding: 16,
                background: "#2a2410",
                border: "1px solid #6b5b1f",
                borderRadius: 8,
                marginBottom: 20,
                fontSize: 13,
              }}
            >
              <strong>Decision 3 safeguard fired:</strong>{" "}
              {Object.keys(result.overlapping_claims).join(", ")} — more than one agent claimed the same
              entity. Values are NOT summed automatically; this needs a valuation-policy decision before
              either figure is presented to a client.
            </div>
          )}

          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "2px solid #2b2f36", color: "#8a8f98", fontSize: 12 }}>
                <th style={{ padding: "8px 12px" }}>Status / Evidence</th>
                <th style={{ padding: "8px 12px" }}>Entity</th>
                <th style={{ padding: "8px 12px" }}>Leak Category</th>
                <th style={{ padding: "8px 12px", textAlign: "right" }}>Value</th>
                <th style={{ padding: "8px 12px" }}>Confidence</th>
                <th style={{ padding: "8px 12px" }}>Classification</th>
                <th style={{ padding: "8px 12px" }}>Agent</th>
                <th style={{ padding: "8px 12px" }}>Ledger seq</th>
              </tr>
            </thead>
            <tbody>
              {orderFindings(result.findings).map((f) => (
                <FindingRow key={f.finding_id} finding={f} />
              ))}
            </tbody>
          </table>

          <Scans scans={result.scans} />
          <ExcludedScans scans={result.excluded_scans} />
          <LegacyFindings rows={result.legacy_findings} count={result.legacy_finding_entries_ignored} />
        </>
      )}
    </main>
  );
}
