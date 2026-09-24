import { fetchRecordedFindings } from "@/lib/api";
import { ledgerState } from "@/lib/ledger-status";
import { formatUsd, isPositiveMoneyString } from "@/lib/money";
import type { RecordedFinding, RecordedFindingsResult } from "@/types/finding";

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

function AmountCell({ finding }: { finding: RecordedFinding }) {
  if (finding.amount_out_of_contract) return <span style={{ color: "#e5534b" }}>out-of-contract amount</span>;
  if (finding.amount_usd === null) return <>—</>;
  // Display guard: only a valid positive money string is ever shown as dollars.
  if (!isPositiveMoneyString(finding.amount_usd)) return <span style={{ color: "#e5534b" }}>invalid amount</span>;
  return <>{formatUsd(finding.amount_usd)}</>;
}

function FindingRow({ finding }: { finding: RecordedFinding }) {
  return (
    <tr style={{ borderBottom: "1px solid #1f2328" }}>
      <td style={{ padding: "10px 12px", fontFamily: "monospace", fontSize: 13 }}>{finding.entity_id}</td>
      <td style={{ padding: "10px 12px" }}>{finding.leak_category.replace(/_/g, " ")}</td>
      <td style={{ padding: "10px 12px", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
        <AmountCell finding={finding} />
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
      </td>
    </tr>
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

export default async function Page() {
  let result: RecordedFindingsResult | undefined;
  let loadError: string | null = null;

  try {
    result = await fetchRecordedFindings();
  } catch (e) {
    loadError = e instanceof Error ? e.message : String(e);
  }

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

          <div style={{ display: "flex", gap: 16, marginBottom: 20, fontSize: 13, color: "#b7bcc4" }}>
            <span>{result.findings.length} distinct findings</span>
            <span>{result.finding_entries_total} finding entries recorded</span>
            <span>{result.ledger_entries_total} ledger entries in total</span>
            <span>{Object.keys(result.overlapping_claims).length} overlapping-claim entities</span>
          </div>

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
              {result.findings.map((f) => (
                <FindingRow key={f.finding_id} finding={f} />
              ))}
            </tbody>
          </table>
        </>
      )}
    </main>
  );
}
