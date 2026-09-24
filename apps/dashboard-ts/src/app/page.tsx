import { fetchScanResult } from "@/lib/api";
import { formatUsd } from "@/lib/money";
import type { Finding } from "@/types/finding";

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

function FindingRow({ finding }: { finding: Finding }) {
  return (
    <tr style={{ borderBottom: "1px solid #1f2328" }}>
      <td style={{ padding: "10px 12px", fontFamily: "monospace", fontSize: 13 }}>{finding.entity_id}</td>
      <td style={{ padding: "10px 12px" }}>{finding.leak_category.replace(/_/g, " ")}</td>
      <td style={{ padding: "10px 12px", maxWidth: 420, fontSize: 13, color: "#b7bcc4" }}>
        {finding.cause_description}
      </td>
      <td style={{ padding: "10px 12px", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
        {finding.recoverable_value
          ? formatUsd(finding.recoverable_value.amount_usd) ?? "invalid amount"
          : "—"}
      </td>
      <td style={{ padding: "10px 12px" }}>
        {finding.recoverable_value ? (
          <ConfidenceBadge value={finding.recoverable_value.confidence} />
        ) : (
          <span style={{ fontSize: 12, color: "#8a8f98" }}>n/a</span>
        )}
      </td>
      <td style={{ padding: "10px 12px", fontSize: 12, color: "#8a8f98" }}>{finding.agent_id}</td>
    </tr>
  );
}

export default async function Page() {
  let result;
  let loadError: string | null = null;

  try {
    result = await fetchScanResult();
  } catch (e) {
    loadError = e instanceof Error ? e.message : String(e);
  }

  return (
    <main style={{ maxWidth: 1100, margin: "0 auto", padding: "32px 24px" }}>
      <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 4 }}>Revenue Recovery — Detection Findings</h1>
      <p style={{ color: "#8a8f98", fontSize: 13, marginBottom: 20 }}>
        Tier 1A · fixture data only, no live client connected
      </p>

      {loadError && (
        <div style={{ padding: 16, background: "#3a1f1f", border: "1px solid #6b2b2b", borderRadius: 8, marginBottom: 20 }}>
          Could not reach the orchestrator at the configured ORCHESTRATOR_URL: <code>{loadError}</code>. Start
          <code> services/orchestrator-go</code> and <code>services/detection-py</code> first.
        </div>
      )}

      {result && (
        <>
          <div style={{ display: "flex", gap: 16, marginBottom: 20, fontSize: 13, color: "#b7bcc4" }}>
            <span>{result.findings.length} findings</span>
            <span>{result.agents_run.length} agents run</span>
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
                <th style={{ padding: "8px 12px" }}>Cause</th>
                <th style={{ padding: "8px 12px", textAlign: "right" }}>Value</th>
                <th style={{ padding: "8px 12px" }}>Confidence</th>
                <th style={{ padding: "8px 12px" }}>Agent</th>
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
