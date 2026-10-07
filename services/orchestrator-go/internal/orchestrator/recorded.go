package orchestrator

import (
	"context"
	"sort"
	"strings"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/client"
)

// RecordedFinding is one distinct finding as the evidence ledger recorded
// it, across every COMPLETED scan (E-2): the record from the latest
// completed scan that contains its finding_id, plus how many completed scans
// recorded it. Field names the dashboard already reads are unchanged
// (apps/dashboard-ts src/types/finding.ts); the rest is new in the Oct 6 2026
// fix wave.
type RecordedFinding struct {
	Seq                 uint64        `json:"seq"`
	FindingID           string        `json:"finding_id"`
	ClientID            string        `json:"client_id"`
	AgentID             string        `json:"agent_id"`
	LeakCategory        string        `json:"leak_category"`
	EntityType          string        `json:"entity_type"`
	EntityID            string        `json:"entity_id"`
	PeriodLabel         *string       `json:"period_label"`
	AmountUSD           *client.Money `json:"amount_usd"`
	ValueClassification *string       `json:"value_classification"`
	DecisionConfidence  *string       `json:"decision_confidence"`
	EvidenceClass       string        `json:"evidence_class"`
	MethodologyID       string        `json:"methodology_id"`
	ScanID              string        `json:"scan_id"`
	PayloadSHA256       string        `json:"payload_sha256"`
	RecordedAt          string        `json:"recorded_at"`
	PrevHash            string        `json:"prev_hash"`
	Hash                string        `json:"hash"`
	// AmountOutOfContract is kept for the dashboard's contract and is always
	// false: a record whose amount is not valid money makes its whole scan
	// excluded (ExcludedScans), it is never shown.
	AmountOutOfContract bool   `json:"amount_out_of_contract"`
	FirstSeq            uint64 `json:"first_seq"`
	// TimesRecorded: how many completed scans recorded this finding. Retries
	// and concurrent requests can no longer inflate it (E-2).
	TimesRecorded              int  `json:"times_recorded"`
	AmountsDifferAcrossRecords bool `json:"amounts_differ_across_records"`
	// PresentInLatestScan: the client's latest completed scan still found
	// it. A quote should use only findings that are.
	PresentInLatestScan bool `json:"present_in_latest_scan"`
}

// ScanSummary is one completed, consistent scan.
type ScanSummary struct {
	ScanID          string `json:"scan_id"`
	ClientID        string `json:"client_id"`
	DataSource      string `json:"data_source"`
	Fixture         bool   `json:"fixture"`
	TenantDefaulted bool   `json:"tenant_defaulted"`
	AsOf            string `json:"as_of"`
	Findings        int    `json:"findings"`
	StartedSeq      uint64 `json:"started_seq"`
	CompletedSeq    uint64 `json:"completed_seq"`
}

// ExcludedScan is a scan that is in the ledger but does not count: it never
// completed (failed, aborted, or still running), or what the ledger holds
// for it does not match its own completion record.
type ExcludedScan struct {
	ScanID        string `json:"scan_id"`
	ClientID      string `json:"client_id"`
	FindingEvents int    `json:"finding_events"`
	Reason        string `json:"reason"`
}

// RecordedFindingsResult is the read-only view served by
// GET /revenue-recovery/findings.
type RecordedFindingsResult struct {
	Findings []RecordedFinding `json:"findings"`
	// Entities ("client_id|entity_type|entity_id") claimed by MORE THAN ONE
	// distinct agent (Decision 3 / Failure Mode #2) among the distinct
	// recorded findings. The same agent re-recording a finding on a later
	// scan is not an overlap.
	OverlappingClaims map[string][]RecordedFinding `json:"overlapping_claims"`
	Scans             []ScanSummary                `json:"scans"`
	ExcludedScans     []ExcludedScan               `json:"excluded_scans"`
	// LedgerEntriesTotal: every ledger entry. FindingEntriesTotal: finding
	// events of counted scans. LegacyFindingEntriesIgnored: pre-Oct-6
	// "kind":"finding" entries (no scan, no tenant, pre-E-4 amounts).
	LedgerEntriesTotal          int                        `json:"ledger_entries_total"`
	FindingEntriesTotal         int                        `json:"finding_entries_total"`
	LegacyFindingEntriesIgnored int                        `json:"legacy_finding_entries_ignored"`
	LedgerVerify                *client.LedgerVerifyResult `json:"ledger_verify"`
	NonLiveDataSource           bool                       `json:"non_live_data_source"`
}

type scanEvents struct {
	id        string
	started   *client.LedgerEvent
	completed *client.LedgerEvent
	aborted   bool
	findings  []client.LedgerEvent
	firstSeq  uint64
}

// ledgerFold accumulates the revenue_recovery events of the ledger, page by
// page, in seq order.
type ledgerFold struct {
	scans   map[string]*scanEvents
	order   []string
	entries int
	legacy  int
}

func (lf *ledgerFold) scan(id string, seq uint64) *scanEvents {
	s, ok := lf.scans[id]
	if !ok {
		s = &scanEvents{id: id, firstSeq: seq}
		lf.scans[id] = s
		lf.order = append(lf.order, id)
	}
	return s
}

// add folds one page. Events of other departments are skipped; a
// revenue_recovery event with an id this code did not write is excluded
// with its scan (it cannot be attributed to a scan otherwise, so it is
// simply not counted).
func (lf *ledgerFold) add(page *client.LedgerEntriesPage) {
	lf.entries += page.Entries
	lf.legacy += page.LegacyFindings
	for i := range page.Events {
		ev := page.Events[i]
		if ev.Department != ledgerDepartment {
			continue
		}
		rest, ok := strings.CutPrefix(ev.EventID, "rr.")
		if !ok || len(rest) < 33 || rest[32] != '.' || !scanIDRE.MatchString(rest[:32]) {
			continue
		}
		s := lf.scan(rest[:32], ev.Seq)
		switch suffix := rest[33:]; {
		case ev.EventType == eventTypeScanStarted && suffix == "started":
			s.started = &ev
		case ev.EventType == eventTypeScanCompleted && suffix == "completed":
			s.completed = &ev
		case ev.EventType == eventTypeScanAborted && suffix == "aborted":
			s.aborted = true
		case ev.EventType == eventTypeFinding && strings.HasPrefix(suffix, "f."):
			s.findings = append(s.findings, ev)
		}
	}
}

// entryPages feeds every ledger page to fn. ledger-rust has no pagination
// yet, so today this is one page (the whole ledger). When paginated reads
// land (?after_seq=&department=revenue_recovery), this loop asks for the
// next page after the last seq until the ledger says there are no more —
// the fold does not change (E-8).
func (o *Orchestrator) entryPages(ctx context.Context, fn func(*client.LedgerEntriesPage)) error {
	page, err := o.ledger.Entries(ctx)
	if err != nil {
		return err
	}
	fn(page)
	return nil
}

// validate decides whether a scan counts, and decodes its findings if so.
func (s *scanEvents) validate() (*ScanSummary, []RecordedFinding, string) {
	switch {
	case s.started == nil:
		return nil, nil, "no scan_started event"
	case s.completed == nil && s.aborted:
		return nil, nil, "aborted before completion"
	case s.completed == nil:
		return nil, nil, "not completed (failed, or still running)"
	case s.aborted:
		return nil, nil, "has both a completed and an aborted event"
	}
	tenant := s.started.SubjectID
	if !ValidClientID(tenant) || s.completed.SubjectID != tenant {
		return nil, nil, "scan events disagree on the client"
	}
	info, err := decodeStartedSummary(s.started.Summary)
	if err != nil {
		return nil, nil, "unreadable scan_started record: " + err.Error()
	}
	n, err := decodeCompletedSummary(s.completed.Summary)
	if err != nil {
		return nil, nil, "unreadable scan_completed record: " + err.Error()
	}
	if n != len(s.findings) {
		return nil, nil, "finding count does not match the completion record"
	}
	manifest := make([][3]string, 0, len(s.findings))
	rows := make([]RecordedFinding, 0, len(s.findings))
	seen := map[string]bool{}
	for _, ev := range s.findings {
		if ev.Seq < s.started.Seq || ev.Seq > s.completed.Seq {
			return nil, nil, "a finding event lies outside its scan's start and completion"
		}
		if !findingRE.MatchString(ev.SubjectID) || ev.EventID != findingEventID(s.id, ev.SubjectID) || seen[ev.SubjectID] {
			return nil, nil, "a finding event's id does not match its finding"
		}
		seen[ev.SubjectID] = true
		f, err := decodeFindingSummary(ev.Summary)
		if err != nil {
			return nil, nil, "unreadable finding record: " + err.Error()
		}
		period := ""
		if f.PeriodLabel != nil {
			period = *f.PeriodLabel
		}
		if ComputeFindingID(tenant, f.AgentID, f.EntityType, f.EntityID, period) != ev.SubjectID {
			return nil, nil, "a finding id is not derived from its recorded fields"
		}
		manifest = append(manifest, [3]string{ev.EventID, ev.PayloadSHA256, ev.Summary})
		rows = append(rows, RecordedFinding{
			Seq: ev.Seq, FindingID: ev.SubjectID, ClientID: tenant, AgentID: f.AgentID,
			LeakCategory: f.LeakCategory, EntityType: f.EntityType, EntityID: f.EntityID,
			PeriodLabel: f.PeriodLabel, AmountUSD: f.AmountUSD, ValueClassification: f.ValueClassification,
			DecisionConfidence: f.DecisionConfidence, EvidenceClass: f.EvidenceClass,
			MethodologyID: f.MethodologyID, ScanID: s.id, PayloadSHA256: ev.PayloadSHA256,
			RecordedAt: ev.RecordedAt, PrevHash: ev.PrevHash, Hash: ev.Hash,
		})
	}
	if s.completed.Seq < s.started.Seq || manifestHash(s.id, tenant, manifest) != s.completed.PayloadSHA256 {
		return nil, nil, "the scan's findings do not match its completion record"
	}
	return &ScanSummary{
		ScanID: s.id, ClientID: tenant, DataSource: info.DataSource, Fixture: info.Fixture,
		TenantDefaulted: info.TenantDefaulted, AsOf: info.AsOf, Findings: n,
		StartedSeq: s.started.Seq, CompletedSeq: s.completed.Seq,
	}, rows, ""
}

func sameAmount(a, b *client.Money) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return a.String() == b.String()
}

// RecordedFindings returns the findings of every completed scan in the
// evidence ledger, with the ledger's own verify verdict. Strictly read-only:
// it calls only GET /ledger/entries and GET /ledger/verify, never a ledger
// write endpoint, and never detection-py.
//
// Only completed, self-consistent scans count (E-2): a scan that failed
// midway, was aborted, is still running, or whose findings do not match its
// completion record is listed in ExcludedScans and contributes nothing.
//
// A verify verdict of valid=false is returned to the caller, not turned into
// an error: the caller must be able to SEE that the chain failed
// verification. A failed request to the ledger is an error.
func (o *Orchestrator) RecordedFindings(ctx context.Context) (*RecordedFindingsResult, error) {
	fold := &ledgerFold{scans: map[string]*scanEvents{}}
	if err := o.entryPages(ctx, fold.add); err != nil {
		return nil, stepErr("ledger entries", err)
	}
	verify, err := o.ledger.Verify(ctx)
	if err != nil {
		return nil, stepErr("ledger verify", err)
	}

	type counted struct {
		sum  *ScanSummary
		rows []RecordedFinding
	}
	var scans []counted
	excluded := []ExcludedScan{}
	for _, id := range fold.order {
		s := fold.scans[id]
		sum, rows, reason := s.validate()
		if reason != "" {
			cl := ""
			if s.started != nil {
				cl = s.started.SubjectID
			}
			excluded = append(excluded, ExcludedScan{ScanID: id, ClientID: cl, FindingEvents: len(s.findings), Reason: reason})
			continue
		}
		scans = append(scans, counted{sum, rows})
	}
	// Completion order: a later-completed scan's record is the latest.
	sort.SliceStable(scans, func(i, j int) bool { return scans[i].sum.CompletedSeq < scans[j].sum.CompletedSeq })

	latestScan := map[string]string{} // client -> latest completed scan id
	for _, c := range scans {
		latestScan[c.sum.ClientID] = c.sum.ScanID
	}

	findings := []RecordedFinding{}
	index := map[string]int{}
	summaries := []ScanSummary{}
	total := 0
	for _, c := range scans {
		summaries = append(summaries, *c.sum)
		total += len(c.rows)
		for _, rec := range c.rows {
			i, seen := index[rec.FindingID]
			if !seen {
				index[rec.FindingID] = len(findings)
				rec.FirstSeq, rec.TimesRecorded = rec.Seq, 1
				findings = append(findings, rec)
				continue
			}
			prev := &findings[i]
			differs := prev.AmountsDifferAcrossRecords || !sameAmount(prev.AmountUSD, rec.AmountUSD)
			first, times := min(prev.FirstSeq, rec.Seq), prev.TimesRecorded+1
			*prev = rec // latest completed scan wins
			prev.FirstSeq, prev.TimesRecorded, prev.AmountsDifferAcrossRecords = first, times, differs
		}
	}
	for i := range findings {
		findings[i].PresentInLatestScan = findings[i].ScanID == latestScan[findings[i].ClientID]
	}

	byEntity := map[string][]RecordedFinding{}
	agents := map[string]map[string]bool{}
	for _, f := range findings {
		k := CorrelationKey(f.ClientID, f.EntityType, f.EntityID)
		byEntity[k] = append(byEntity[k], f)
		if agents[k] == nil {
			agents[k] = map[string]bool{}
		}
		agents[k][f.AgentID] = true
	}
	overlaps := map[string][]RecordedFinding{}
	for k, fs := range byEntity {
		if len(agents[k]) > 1 {
			overlaps[k] = fs
		}
	}

	return &RecordedFindingsResult{
		Findings:                    findings,
		OverlappingClaims:           overlaps,
		Scans:                       summaries,
		ExcludedScans:               excluded,
		LedgerEntriesTotal:          fold.entries,
		FindingEntriesTotal:         total,
		LegacyFindingEntriesIgnored: fold.legacy,
		LedgerVerify:                verify,
		NonLiveDataSource:           true,
	}, nil
}
