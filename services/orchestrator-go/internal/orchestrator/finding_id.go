package orchestrator

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

// FindingIDPrefix versions the finding identity scheme.
const FindingIDPrefix = "rrf1-"

// ComputeFindingID is zbm_schema.compute_finding_id (detection-py), byte for
// byte (E-3, Oct 6 2026): "rrf1-" + the first 40 hex digits of
// SHA-256("rrf1\n" + client_id + "\n" + agent_id + "\n" + entity_type + "\n" +
// entity_id + "\n" + period_label), period_label "" when absent. No field's
// charset allows a newline, so the preimage is unambiguous. The orchestrator
// recomputes it for every finding it receives and every finding it reads back
// from the ledger, and refuses a mismatch. Cross-language vectors:
// finding_id_test.go and detection-py tests/test_revrec_fix_wave.py.
func ComputeFindingID(clientID, agentID, entityType, entityID, periodLabel string) string {
	pre := strings.Join([]string{"rrf1", clientID, agentID, entityType, entityID, periodLabel}, "\n")
	sum := sha256.Sum256([]byte(pre))
	return FindingIDPrefix + hex.EncodeToString(sum[:])[:40]
}

// CorrelationKey is the entity a finding claims, (client_id, entity_type,
// entity_id), as one string — the same key detection-py's
// /correlation/overlaps returns. "|" is in none of the three charsets.
func CorrelationKey(clientID, entityType, entityID string) string {
	return clientID + "|" + entityType + "|" + entityID
}
