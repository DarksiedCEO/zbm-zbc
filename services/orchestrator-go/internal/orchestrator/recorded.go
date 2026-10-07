package orchestrator

import (
	"context"
	"errors"
	"sort"
	"strings"
	"time"

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
	// ValueBasis (AEGIS L1): the base and rate a rate-derived amount was
	// computed from, as the scan recorded them; nil otherwise.
	ValueBasis *client.ValueBasis `json:"value_basis"`
	// LabelsExceedEvidence (AEGIS M2): non-null when the recorded labels
	// claim more than the recorded evidence class supports — a record
	// written before the Oct 7 2026 invariant (8bdebde's renewal finding was
	// ESTIMATED but labeled observed/high). Shown with the reason, never
	// silently relabelled; such a figure must not be quoted as recorded.
	LabelsExceedEvidence *string `json:"labels_exceed_evidence"`
	// Quotable (AEGIS N4): this recorded figure may be put in a client
	// quote as recorded — isQuotable(). The dashboard uses this field.
	Quotable bool `json:"quotable"`
}

// isQuotable is the one rule for whether a recorded figure may go in a quote:
// it has a valid amount, the client's latest scan still found it, its
// evidence is OBSERVED (exact arithmetic on recorded data — ESTIMATED,
// MODELED and UNKNOWN figures are not quoted as recorded), its labels are
// known and within that evidence, and any value basis it carries reproduces
// it. (Overlapping claims are a separate gate, Decision 3.)
func (f *RecordedFinding) isQuotable() bool {
	if f.AmountUSD == nil || !f.AmountUSD.IsPositive() || f.AmountOutOfContract || !f.PresentInLatestScan {
		return false
	}
	if f.EvidenceClass != "OBSERVED" || f.LabelsExceedEvidence != nil || f.ValueClassification == nil || f.DecisionConfidence == nil {
		return false
	}
	if labelsExceedEvidence(f.EvidenceClass, *f.ValueClassification, *f.DecisionConfidence) != "" {
		return false
	}
	return f.ValueBasis == nil || checkValueBasis(f.ValueBasis, f.AmountUSD) == nil
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
	// Backdated (AEGIS N3): its as_of is earlier than the as_of of the same
	// client's previous completed scan (completion order) — a scan "as of"
	// an older instant recorded after a newer one.
	Backdated bool `json:"backdated"`
}

// ExcludedScan is a scan that is in the ledger but does not count: it never
// completed (failed, aborted, abandoned, or still running), or what the
// ledger holds for it does not match its own completion record.
type ExcludedScan struct {
	ScanID        string `json:"scan_id"`
	ClientID      string `json:"client_id"`
	FindingEvents int    `json:"finding_events"`
	Reason        string `json:"reason"`
	// Status (AEGIS L3, Oct 7 2026) labels why it does not count:
	//   running       this process is writing it right now
	//   incomplete    no completion or abort record yet, started less than
	//                 ScanAbandonedAfter ago (or its start time is unknown):
	//                 it may still be running in another process
	//   abandoned     no completion or abort record, started at least
	//                 ScanAbandonedAfter ago: it will never complete
	//   aborted       the scan recorded that it failed
	//   inconsistent  the ledger's records of it disagree with each other
	//   unsupported_format  completed by a newer orchestrator-go in a format
	//                 this one cannot read (N1: a rollback)
	Status string `json:"status"`
	// StartedAt: when the ledger recorded its start ("" if it has none).
	StartedAt string `json:"started_at"`
}

// Excluded-scan statuses (ExcludedScan.Status).
const (
	ScanStatusRunning      = "running"
	ScanStatusIncomplete   = "incomplete"
	ScanStatusAbandoned    = "abandoned"
	ScanStatusAborted      = "aborted"
	ScanStatusInconsistent = "inconsistent"
	// AEGIS N1: completed in a completion-record format newer than this
	// reader (written by a later orchestrator-go — this one was rolled back).
	ScanStatusUnsupportedFormat = "unsupported_format"
)

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
	// LedgerEntriesTotal: every entry in the WHOLE ledger, every department —
	// from GET /ledger/head, or from the verify verdict when the ledger has
	// no head route (LedgerTotalSource says which). Never the size of this
	// process's own read, which a filtered or caller-scoped read makes a
	// subset (ledger review, Oct 7 2026). LedgerEntriesRead: what that read
	// returned. FindingEntriesTotal: finding events of counted scans.
	// LegacyFindingEntriesIgnored: pre-Oct-6 "kind":"finding" entries (no
	// scan, no tenant, pre-E-4 amounts); LegacyFindings lists them (AEGIS
	// M4) so they can be shown, labelled, never counted.
	LedgerEntriesTotal          int                         `json:"ledger_entries_total"`
	LedgerTotalSource           string                      `json:"ledger_total_source"`
	LedgerEntriesRead           int                         `json:"ledger_entries_read"`
	FindingEntriesTotal         int                         `json:"finding_entries_total"`
	LegacyFindingEntriesIgnored int                         `json:"legacy_finding_entries_ignored"`
	LegacyFindings              []client.LegacyFindingEntry `json:"legacy_findings"`
	LedgerVerify                *client.LedgerVerifyResult  `json:"ledger_verify"`
	NonLiveDataSource           bool                        `json:"non_live_data_source"`
	// LatestScanUncounted (AEGIS N1): client -> a scan that completed AFTER
	// that client's latest counted scan but cannot be counted (a newer
	// format after a rollback, or records that disagree). For such a client
	// no finding is present_in_latest_scan or quotable: an older scan is
	// never presented as the latest.
	LatestScanUncounted map[string]string `json:"latest_scan_uncounted"`
}

type scanEvents struct {
	id        string
	started   *client.LedgerEvent
	completed *client.LedgerEvent
	aborted   bool
	findings  []client.LedgerEvent
	bases     []client.LedgerEvent // rr_value_basis (L1)
	firstSeq  uint64
}

// ledgerFold accumulates the revenue_recovery events of the ledger, page by
// page, in seq order.
type ledgerFold struct {
	scans      map[string]*scanEvents
	order      []string
	entries    int
	legacy     int
	legacyRows []client.LegacyFindingEntry
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
	lf.legacyRows = append(lf.legacyRows, page.LegacyFindingRows...)
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
		case ev.EventType == eventTypeValueBasis && strings.HasPrefix(suffix, "b."):
			s.bases = append(s.bases, ev)
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
// The second string is the excluded status: ScanStatusAborted,
// ScanStatusInconsistent, or "" for a scan with no completion and no abort
// record (the caller decides running / incomplete / abandoned).
func (s *scanEvents) validate() (*ScanSummary, []RecordedFinding, string, string) {
	sum, rows, reason := s.check()
	if reason == "" {
		return sum, rows, "", ""
	}
	if s.completed != nil {
		if _, _, err := decodeCompletedSummary(s.completed.Summary); errors.Is(err, ErrNewerCompletionFormat) {
			return nil, nil, reason, ScanStatusUnsupportedFormat
		}
	}
	switch {
	case s.completed == nil && s.aborted:
		return nil, nil, reason, ScanStatusAborted
	case s.completed == nil && s.started != nil:
		return nil, nil, reason, ""
	default:
		return nil, nil, reason, ScanStatusInconsistent
	}
}

func (s *scanEvents) check() (*ScanSummary, []RecordedFinding, string) {
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
	n, version, err := decodeCompletedSummary(s.completed.Summary)
	if err != nil {
		return nil, nil, "unreadable scan_completed record: " + err.Error()
	}
	if version == 1 && len(s.bases) > 0 {
		// rrc1 predates value-basis events: one in an rrc1 scan was not
		// written by any orchestrator-go.
		return nil, nil, "an rrc1 scan cannot have value basis events"
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
		row := RecordedFinding{
			Seq: ev.Seq, FindingID: ev.SubjectID, ClientID: tenant, AgentID: f.AgentID,
			LeakCategory: f.LeakCategory, EntityType: f.EntityType, EntityID: f.EntityID,
			PeriodLabel: f.PeriodLabel, AmountUSD: f.AmountUSD, ValueClassification: f.ValueClassification,
			DecisionConfidence: f.DecisionConfidence, EvidenceClass: f.EvidenceClass,
			MethodologyID: f.MethodologyID, ScanID: s.id, PayloadSHA256: ev.PayloadSHA256,
			RecordedAt: ev.RecordedAt, PrevHash: ev.PrevHash, Hash: ev.Hash,
		}
		if f.AmountUSD != nil {
			if why := labelsExceedEvidence(f.EvidenceClass, *f.ValueClassification, *f.DecisionConfidence); why != "" {
				row.LabelsExceedEvidence = &why
			}
		}
		rows = append(rows, row)
	}
	// L1: value-basis events, each for a finding of this scan that has an
	// amount, at most one per finding, reproducing that amount exactly.
	rowOf := map[string]int{}
	for i := range rows {
		rowOf[rows[i].FindingID] = i
	}
	for _, ev := range s.bases {
		if ev.Seq < s.started.Seq || ev.Seq > s.completed.Seq {
			return nil, nil, "a value basis event lies outside its scan's start and completion"
		}
		i, ok := rowOf[ev.SubjectID]
		if !ok || ev.EventID != basisEventID(s.id, ev.SubjectID) || rows[i].ValueBasis != nil {
			return nil, nil, "a value basis event does not match a finding of its scan"
		}
		vb, err := decodeBasisSummary(ev.Summary)
		if err != nil {
			return nil, nil, "unreadable value basis record: " + err.Error()
		}
		if err := checkValueBasis(vb, rows[i].AmountUSD); err != nil {
			return nil, nil, "a value basis record disagrees with its finding: " + err.Error()
		}
		rows[i].ValueBasis = vb
		manifest = append(manifest, [3]string{ev.EventID, ev.PayloadSHA256, ev.Summary})
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
// it calls only GET /ledger/entries, GET /ledger/verify and GET
// /ledger/head, never a ledger write endpoint, and never detection-py.
//
// Only completed, self-consistent scans count (E-2): a scan that failed
// midway, was aborted, is still running, or whose findings do not match its
// completion record is listed in ExcludedScans and contributes nothing.
//
// A verify verdict of valid=false is returned to the caller, not turned into
// an error: the caller must be able to SEE that the chain failed
// verification. A failed request to the ledger is an error.
func (o *Orchestrator) RecordedFindings(ctx context.Context) (*RecordedFindingsResult, error) {
	fold := &ledgerFold{scans: map[string]*scanEvents{}, legacyRows: []client.LegacyFindingEntry{}}
	if err := o.entryPages(ctx, fold.add); err != nil {
		return nil, stepErr("ledger entries", err)
	}
	verify, err := o.ledger.Verify(ctx)
	if err != nil {
		return nil, stepErr("ledger verify", err)
	}
	// The ledger's size, from the ledger itself — not len() of our read
	// (ledger review: with a department-filtered or caller-scoped read that
	// is the findings count, and disagreed with /ledger/verify). A ledger
	// without GET /ledger/head (before ledger-rust sweep F) gives it in the
	// verify verdict; an invalid chain's verdict has no count, so then the
	// total is what was read, and says so.
	total, totalSource := fold.entries, "entries_read"
	if head, err := o.ledger.Head(ctx); err == nil {
		total, totalSource = int(head.Entries), "head"
	} else if !errors.Is(err, client.ErrHeadUnsupported) {
		return nil, stepErr("ledger head", err)
	} else if verify.Valid {
		total, totalSource = verify.Entries, "verify"
	}
	now := o.now()
	running := o.runningScan()

	type counted struct {
		sum  *ScanSummary
		rows []RecordedFinding
	}
	var scans []counted
	excluded := []ExcludedScan{}
	countedIDs := map[string]struct{}{}
	supersededBy := map[string]string{} // client -> the uncountable later scan
	for _, id := range fold.order {
		s := fold.scans[id]
		sum, rows, reason, status := s.validate()
		if reason != "" {
			cl, startedAt := "", ""
			if s.started != nil {
				cl, startedAt = s.started.SubjectID, s.started.RecordedAt
			}
			if status == "" {
				status = unfinishedStatus(id, startedAt, running, now)
			}
			excluded = append(excluded, ExcludedScan{ScanID: id, ClientID: cl, FindingEvents: len(s.findings),
				Reason: reason, Status: status, StartedAt: startedAt})
			continue
		}
		scans = append(scans, counted{sum, rows})
		countedIDs[id] = struct{}{}
	}
	// Completion order: a later-completed scan's record is the latest.
	sort.SliceStable(scans, func(i, j int) bool { return scans[i].sum.CompletedSeq < scans[j].sum.CompletedSeq })

	latestScan := map[string]string{} // client -> latest completed scan id
	for _, c := range scans {
		latestScan[c.sum.ClientID] = c.sum.ScanID
	}
	// AEGIS N1: a scan that HAS a completion record but cannot be counted
	// (a newer format, or records that disagree) and completed after the
	// latest counted scan of its client means that counted scan is NOT the
	// latest: nothing of that client is current. Never fall back to an
	// older scan as "latest".
	latestSeq := map[string]uint64{}
	for _, c := range scans {
		latestSeq[c.sum.ClientID] = c.sum.CompletedSeq
	}
	for _, id := range fold.order {
		sc := fold.scans[id]
		if sc.started == nil || sc.completed == nil {
			continue
		}
		cl := sc.started.SubjectID
		if _, counted := latestScan[cl]; !counted || sc.completed.Seq <= latestSeq[cl] {
			continue
		}
		if _, ok := countedIDs[id]; !ok {
			latestScan[cl] = id // an uncounted scan: no counted finding carries its id
			supersededBy[cl] = id
		}
	}

	findings := []RecordedFinding{}
	index := map[string]int{}
	summaries := []ScanSummary{}
	findingTotal := 0
	prevAsOf := map[string]time.Time{} // client -> as_of of its previous completed scan
	for _, c := range scans {
		if t, err := time.Parse(time.RFC3339Nano, c.sum.AsOf); err == nil {
			if p, ok := prevAsOf[c.sum.ClientID]; ok && t.Before(p) {
				c.sum.Backdated = true
			}
			prevAsOf[c.sum.ClientID] = t
		}
		summaries = append(summaries, *c.sum)
		findingTotal += len(c.rows)
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
		findings[i].Quotable = findings[i].isQuotable()
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
		LedgerEntriesTotal:          total,
		LedgerTotalSource:           totalSource,
		LedgerEntriesRead:           fold.entries,
		FindingEntriesTotal:         findingTotal,
		LegacyFindingEntriesIgnored: fold.legacy,
		LegacyFindings:              fold.legacyRows,
		LedgerVerify:                verify,
		NonLiveDataSource:           true,
		LatestScanUncounted:         supersededBy,
	}, nil
}

// unfinishedStatus labels a scan with a start but no completion or abort
// record (AEGIS L3): running in this process, abandoned once it is older
// than any scan can run, otherwise incomplete (it may still be running in
// another process, or its start time cannot be read).
func unfinishedStatus(id, startedAt, running string, now time.Time) string {
	if id == running {
		return ScanStatusRunning
	}
	t, err := time.Parse(time.RFC3339Nano, startedAt)
	if err != nil {
		return ScanStatusIncomplete
	}
	if now.Sub(t) >= ScanAbandonedAfter {
		return ScanStatusAbandoned
	}
	return ScanStatusIncomplete
}
