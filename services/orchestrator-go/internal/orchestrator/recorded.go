package orchestrator

import (
	"context"
	"fmt"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/client"
)

// RecordedFinding is one distinct finding as the evidence ledger recorded
// it: the LATEST ledger entry for its finding_id, plus how many times that
// finding_id has been recorded (every scan re-records every finding it
// detects, so a finding seen by three scans has three ledger entries).
type RecordedFinding struct {
	client.LedgerFindingRecord
	FirstSeq      uint64 `json:"first_seq"`
	TimesRecorded int    `json:"times_recorded"`
	// AmountsDifferAcrossRecords is true when earlier records of this
	// finding_id carried a different amount_usd than the latest one — shown
	// rather than hidden, since only the latest amount is displayed.
	AmountsDifferAcrossRecords bool `json:"amounts_differ_across_records"`
}

// RecordedFindingsResult is the read-only view served by
// GET /revenue-recovery/findings.
type RecordedFindingsResult struct {
	Findings []RecordedFinding `json:"findings"`
	// Entities claimed by MORE THAN ONE distinct agent (Decision 3 /
	// Failure Mode #2) among the distinct recorded findings. The same agent
	// re-recording a finding on a later scan is not an overlap.
	OverlappingClaims   map[string][]RecordedFinding `json:"overlapping_claims"`
	LedgerEntriesTotal  int                          `json:"ledger_entries_total"`
	FindingEntriesTotal int                          `json:"finding_entries_total"`
	LedgerVerify        *client.LedgerVerifyResult   `json:"ledger_verify"`
	NonLiveDataSource   bool                         `json:"non_live_data_source"`
}

func sameAmount(a, b *client.Money) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return a.String() == b.String()
}

// RecordedFindings returns the findings already recorded in the evidence
// ledger, with the ledger's own verify verdict. It is strictly read-only:
// it calls only GET /ledger/entries and GET /ledger/verify, never a ledger
// write endpoint, and never detection-py (it does not run a scan).
//
// A verify verdict of valid=false is returned to the caller, not turned
// into an error: the caller must be able to SEE that the chain failed
// verification (or is empty). A failed request to the ledger is an error.
func (o *Orchestrator) RecordedFindings(ctx context.Context) (*RecordedFindingsResult, error) {
	entries, err := o.ledger.Entries(ctx)
	if err != nil {
		return nil, fmt.Errorf("ledger entries: %w", err)
	}
	verify, err := o.ledger.Verify(ctx)
	if err != nil {
		return nil, fmt.Errorf("ledger verify: %w", err)
	}

	findings := []RecordedFinding{}
	index := map[string]int{}
	for _, rec := range entries.Findings {
		i, seen := index[rec.FindingID]
		if !seen {
			index[rec.FindingID] = len(findings)
			findings = append(findings, RecordedFinding{LedgerFindingRecord: rec, FirstSeq: rec.Seq, TimesRecorded: 1})
			continue
		}
		prev := &findings[i]
		differs := prev.AmountsDifferAcrossRecords ||
			!sameAmount(prev.AmountUSD, rec.AmountUSD) || prev.AmountOutOfContract != rec.AmountOutOfContract
		prev.LedgerFindingRecord = rec // entries arrive in seq order: latest wins
		prev.TimesRecorded++
		prev.AmountsDifferAcrossRecords = differs
	}

	byEntity := map[string][]RecordedFinding{}
	agents := map[string]map[string]bool{}
	for _, f := range findings {
		byEntity[f.EntityID] = append(byEntity[f.EntityID], f)
		if agents[f.EntityID] == nil {
			agents[f.EntityID] = map[string]bool{}
		}
		agents[f.EntityID][f.AgentID] = true
	}
	overlaps := map[string][]RecordedFinding{}
	for entity, fs := range byEntity {
		if len(agents[entity]) > 1 {
			overlaps[entity] = fs
		}
	}

	return &RecordedFindingsResult{
		Findings:            findings,
		OverlappingClaims:   overlaps,
		LedgerEntriesTotal:  entries.TotalEntries,
		FindingEntriesTotal: len(entries.Findings),
		LedgerVerify:        verify,
		NonLiveDataSource:   true,
	}, nil
}
