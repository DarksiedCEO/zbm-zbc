// Package orchestrator implements the Revenue Recovery 1A scan pipeline:
// pull fixture data, call every registered detection agent, run the
// Decision 3 / Failure Mode #2 correlation check, record the scan in the
// evidence ledger, and return the combined result. This is the "agent
// orchestration / workflow" component named in Decision 6 — Go was chosen
// here for low-overhead concurrency and operational simplicity, not because
// the logic itself is complex.
//
// Revenue Recovery fix wave (Oct 6 2026), from the backend bug sweep:
//   - E-1: a store with no leaks is a successful scan with zero findings.
//     Lists are never sent as JSON null, and the correlation call is skipped
//     when nothing could overlap.
//   - E-2: every scan has an id and is recorded as started -> findings ->
//     completed ledger events with deterministic ids, each append retried
//     idempotently; only completed scans count (ledger_record.go,
//     recorded.go). One scan runs at a time: a concurrent request gets
//     ErrScanInProgress (409).
//   - E-3: every scan has a tenant (client_id). Only the fixture tenant has a
//     data source today; a scan with no client_id defaults to it and records
//     that it did. Every finding is checked to belong to the tenant, to come
//     from the agent that was called, and to carry its derived id.
//   - E-6: detect calls are batched under detection-py's 1,000-item cap, and
//     correlation is batched by correlation key so no entity's findings are
//     ever split across calls.
package orchestrator

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/client"
)

// FixtureClientID is the tenant the shared fixture pool belongs to —
// detection-py fixtures_loader.FIXTURE_CLIENT_ID. It is the only tenant with
// a data source until a real store connector exists.
const FixtureClientID = "fixture-pool"

// DataSourceFixtures names the only data source that exists.
const DataSourceFixtures = "fixtures"

// Batch sizes (E-6). detection-py refuses more than 1,000 items per request
// (api.MAX_BATCH_ITEMS). Detect batches are half that, so one batch's
// response stays well under maxDetectionResponseBytes. /correlation/overlaps
// echoes the findings it is sent: 700 worst-case findings (every string at
// its limit, all escaped) are ~7.1 MiB, under the 8 MiB response cap that
// 1,000 of them (~10.1 MiB) would exceed. Real findings are ~1.3 KB.
const (
	detectBatchSize      = 500
	correlationBatchSize = 700
)

// ErrScanInProgress: another scan holds the scan lock. Answered 409 with
// Retry-After; nothing was run or written.
var ErrScanInProgress = errors.New("a Revenue Recovery scan is already running; one scan runs at a time")

// RequestError is a scan request the orchestrator refuses before doing any
// work. Status is the HTTP status (400 or 422); Msg is safe to show.
type RequestError struct {
	Status int
	Msg    string
}

func (e *RequestError) Error() string { return e.Msg }

// ScanRequest is what a caller may choose about a scan.
type ScanRequest struct {
	// ClientID is the tenant. Empty means the fixture tenant (recorded as
	// tenant_defaulted). Any other tenant is refused until a live data source
	// exists — fixture data is never attributed to a real client.
	ClientID string
	// AsOf is the scan's instant (renewals due at or before it are missed).
	// Zero means now.
	AsOf time.Time
}

type ScanResult struct {
	ScanID          string `json:"scan_id"`
	ClientID        string `json:"client_id"`
	TenantDefaulted bool   `json:"tenant_defaulted"`
	DataSource      string `json:"data_source"`
	AsOf            string `json:"as_of"`
	// Findings is never null: a clean store is [] (E-1).
	Findings []client.Finding `json:"findings"`
	// OverlappingClaims: correlation key "client_id|entity_type|entity_id" ->
	// the findings of more than one distinct agent on that entity.
	OverlappingClaims map[string][]client.Finding `json:"overlapping_claims"`
	AgentsRun         []string                    `json:"agents_run"`
	NonLiveDataSource bool                        `json:"non_live_data_source"`
	// LedgerEntriesWritten: finding events recorded for this scan (one per
	// finding). LedgerEventsCreated / LedgerEventsAlreadyPresent split every
	// event of the scan (started, findings, completed) by what the ledger
	// answered; "already present" is a retried append the ledger had already
	// committed — recorded once, never twice.
	LedgerEntriesWritten       int                        `json:"ledger_entries_written"`
	LedgerEventsCreated        int                        `json:"ledger_events_created"`
	LedgerEventsAlreadyPresent int                        `json:"ledger_events_already_present"`
	LedgerAppendRetries        int                        `json:"ledger_append_retries"`
	LedgerVerify               *client.LedgerVerifyResult `json:"ledger_verify"`
}

type Orchestrator struct {
	detection *client.DetectionClient
	ledger    *client.LedgerClient

	scanMu    sync.Mutex
	newScanID func() (string, error)
	now       func() time.Time
}

func New(detectionBaseURL, detectionToken, ledgerBaseURL, ledgerToken string) *Orchestrator {
	return &Orchestrator{
		detection: client.NewDetectionClient(detectionBaseURL, detectionToken),
		ledger:    client.NewLedgerClient(ledgerBaseURL, ledgerToken),
		newScanID: randomScanID,
		now:       time.Now,
	}
}

func randomScanID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(b[:]), nil
}

// agentCall is one detection agent: its id (every finding it returns must
// carry it) and how to run it on the scan's data.
type agentCall struct {
	id  string
	run func(ctx context.Context) ([]client.Finding, error)
}

// batched runs call over items in batches of at most size.
func batched[T any](ctx context.Context, items []T, size int,
	call func(context.Context, []T) ([]client.Finding, error)) ([]client.Finding, error) {
	out := []client.Finding{}
	if len(items) == 0 {
		// One call with an empty batch: the agent still runs (and must
		// answer an empty list), exactly as for a store with data.
		fs, err := call(ctx, []T{})
		return append(out, fs...), err
	}
	for start := 0; start < len(items); start += size {
		end := min(start+size, len(items))
		fs, err := call(ctx, items[start:end])
		if err != nil {
			if len(items) > size {
				return nil, fmt.Errorf("batch %d-%d of %d: %w", start, end-1, len(items), err)
			}
			return nil, err
		}
		out = append(out, fs...)
	}
	return out, nil
}

// touchpointBatches packs touchpoints into batches of at most size, never
// splitting one order's touchpoints: the cross-channel agent reasons over an
// order's whole journey.
func touchpointBatches(tps []client.ChannelTouchpoint, size int) ([][]client.ChannelTouchpoint, error) {
	var order []string
	groups := map[string][]client.ChannelTouchpoint{}
	for _, tp := range tps {
		k := fmt.Sprint(tp["order_id"])
		if _, ok := groups[k]; !ok {
			order = append(order, k)
		}
		groups[k] = append(groups[k], tp)
	}
	batches := [][]client.ChannelTouchpoint{}
	cur := []client.ChannelTouchpoint{}
	for _, k := range order {
		g := groups[k]
		if len(g) > size {
			return nil, fmt.Errorf("order %.64q has %d touchpoints, more than one detection batch (%d)", k, len(g), size)
		}
		if len(cur)+len(g) > size {
			batches = append(batches, cur)
			cur = []client.ChannelTouchpoint{}
		}
		cur = append(cur, g...)
	}
	if len(cur) > 0 || len(batches) == 0 {
		batches = append(batches, cur)
	}
	return batches, nil
}

// RunFullScan pulls the shared fixture pool and runs every Tier 1 and Tier 2
// detection agent against it, correlates the combined findings and records
// the scan in the ledger. Explicitly non-live: it only ever reads from
// detection-py's /fixtures/* endpoints (no real-store data source exists).
//
// Fail fast (fix wave 3): the ledger is checked FIRST (GET /ledger/verify).
// If it is unreachable, refuses the token, or its chain does not verify, the
// scan stops before a single detection call. Every finding is validated and
// every ledger event built BEFORE the first ledger write, so a bad finding
// fails the scan with nothing written.
func (o *Orchestrator) RunFullScan(ctx context.Context, req ScanRequest) (*ScanResult, error) {
	if !o.scanMu.TryLock() {
		return nil, ErrScanInProgress
	}
	defer o.scanMu.Unlock()

	tenant, defaulted := req.ClientID, false
	if tenant == "" {
		tenant, defaulted = FixtureClientID, true
	}
	if !ValidClientID(tenant) {
		return nil, &RequestError{Status: 400, Msg: "client_id must be 1-64 characters of [A-Za-z0-9._:-], starting with a letter or digit"}
	}
	if tenant != FixtureClientID {
		return nil, &RequestError{Status: 422, Msg: fmt.Sprintf(
			"no live data source exists yet: only the fixture tenant (client_id=%s, the default) can be scanned", FixtureClientID)}
	}
	asOf := req.AsOf
	if asOf.IsZero() {
		asOf = o.now()
	}
	asOfWire := asOf.UTC().Format(time.RFC3339Nano)

	pre, err := o.ledger.Verify(ctx)
	if err != nil {
		return nil, stepErr("ledger check before scan", err)
	}
	if !pre.Valid {
		// Appending to a chain that already fails verification would bury
		// new evidence behind a break nobody can vouch for.
		return nil, &IntegrityError{When: "before scan (nothing was run or written)", Verdict: pre.Error}
	}

	orders, err := o.detection.FixtureOrders(ctx)
	if err != nil {
		return nil, stepErr("fetch fixture orders", err)
	}
	subs, err := o.detection.FixtureSubscriptions(ctx)
	if err != nil {
		return nil, stepErr("fetch fixture subscriptions", err)
	}
	ssEvents, err := o.detection.FixtureServerSideEvents(ctx)
	if err != nil {
		return nil, stepErr("fetch server-side event fixtures", err)
	}
	touchpoints, err := o.detection.FixtureChannelTouchpoints(ctx)
	if err != nil {
		return nil, stepErr("fetch channel touchpoint fixtures", err)
	}
	platforms, err := o.detection.FixturePlatformConnections(ctx)
	if err != nil {
		return nil, stepErr("fetch platform connection fixtures", err)
	}
	terms, err := o.detection.FixtureContractTerms(ctx)
	if err != nil {
		return nil, stepErr("fetch contract term fixtures", err)
	}
	tpBatches, err := touchpointBatches(touchpoints, detectBatchSize)
	if err != nil {
		return nil, stepErr("batch channel touchpoints", err)
	}

	d := o.detection
	agents := []agentCall{
		{"affiliate-coupon-extension-v1", func(ctx context.Context) ([]client.Finding, error) {
			return batched(ctx, orders, detectBatchSize, func(ctx context.Context, b []client.Order) ([]client.Finding, error) {
				return d.DetectAffiliateCouponExtension(ctx, tenant, b)
			})
		}},
		{"discount-misuse-v1", func(ctx context.Context) ([]client.Finding, error) {
			return batched(ctx, orders, detectBatchSize, func(ctx context.Context, b []client.Order) ([]client.Finding, error) {
				return d.DetectDiscountMisuse(ctx, tenant, b)
			})
		}},
		{"abandoned-cart-coverage-v1", func(ctx context.Context) ([]client.Finding, error) {
			return batched(ctx, orders, detectBatchSize, func(ctx context.Context, b []client.Order) ([]client.Finding, error) {
				return d.DetectAbandonedCartCoverage(ctx, tenant, b)
			})
		}},
		{"renewal-never-triggered-v1", func(ctx context.Context) ([]client.Finding, error) {
			return batched(ctx, subs, detectBatchSize, func(ctx context.Context, b []client.Subscription) ([]client.Finding, error) {
				return d.DetectRenewalNeverTriggered(ctx, tenant, asOfWire, b)
			})
		}},
		// Tier 2 — independent data sources, each agent runs whether or not
		// the others find anything.
		{"server-side-attribution-v1", func(ctx context.Context) ([]client.Finding, error) {
			return batched(ctx, ssEvents, detectBatchSize, func(ctx context.Context, b []client.ServerSideEvent) ([]client.Finding, error) {
				return d.DetectServerSideAttribution(ctx, tenant, b)
			})
		}},
		{"cross-channel-attribution-v1", func(ctx context.Context) ([]client.Finding, error) {
			return batched(ctx, tpBatches, 1, func(ctx context.Context, b [][]client.ChannelTouchpoint) ([]client.Finding, error) {
				return d.DetectCrossChannelAttribution(ctx, tenant, b[0])
			})
		}},
		{"platform-integration-v1", func(ctx context.Context) ([]client.Finding, error) {
			return batched(ctx, platforms, detectBatchSize, func(ctx context.Context, b []client.PlatformConnectionStatus) ([]client.Finding, error) {
				return d.DetectPlatformIntegration(ctx, tenant, b)
			})
		}},
		{"contract-pricing-term-drift-v1", func(ctx context.Context) ([]client.Finding, error) {
			return batched(ctx, terms, detectBatchSize, func(ctx context.Context, b []client.ContractTerm) ([]client.Finding, error) {
				return d.DetectContractPricingTermDrift(ctx, tenant, b)
			})
		}},
	}

	all := []client.Finding{} // never nil: a clean store is [] on the wire (E-1)
	agentsRun := []string{}
	seen := map[string]string{} // finding_id -> its content (detected_at aside)
	for _, a := range agents {
		fs, err := a.run(ctx)
		if err != nil {
			return nil, stepErr(a.id+" agent", err)
		}
		for _, f := range fs {
			if err := checkFinding(f, tenant, a.id); err != nil {
				return nil, stepErr(a.id+" agent", fmt.Errorf("%w: %v", ErrBadFinding, err))
			}
			key := contentKey(f)
			if prior, dup := seen[f.FindingID]; dup {
				// The same row in two batches (or sent twice) is the same
				// finding: recorded once. Two different findings under one
				// id are two input rows sharing an identifier: refused.
				if prior != key {
					return nil, stepErr(a.id+" agent", fmt.Errorf("%w: finding %s was returned twice with different content", ErrBadFinding, f.FindingID))
				}
				continue
			}
			seen[f.FindingID] = key
			all = append(all, f)
		}
		agentsRun = append(agentsRun, a.id)
	}

	overlaps, err := o.correlate(ctx, all)
	if err != nil {
		return nil, stepErr("correlation check", err)
	}

	// Build every ledger event before writing any, so an unencodable finding
	// fails the scan with nothing written.
	scanID, err := o.newScanID()
	if err != nil {
		return nil, stepErr("scan id", err)
	}
	started := startedEvent(startedPayload{
		ScanID: scanID, ClientID: tenant, AsOf: asOfWire, DataSource: DataSourceFixtures,
		Fixture: true, TenantDefaulted: defaulted, Agents: agentsRun,
	})
	findingEvents := make([]client.EventInput, 0, len(all))
	manifest := make([][3]string, 0, len(all))
	for _, f := range all {
		ev, err := findingEvent(scanID, f)
		if err != nil {
			return nil, stepErr("ledger record for "+f.FindingID, fmt.Errorf("%w: %v", ErrBadFinding, err))
		}
		findingEvents = append(findingEvents, ev)
		manifest = append(manifest, [3]string{ev.EventID, ev.PayloadSHA256, ev.Summary})
	}
	completed := completedEvent(scanID, tenant, manifest)

	res := &ScanResult{
		ScanID: scanID, ClientID: tenant, TenantDefaulted: defaulted, DataSource: DataSourceFixtures,
		AsOf: asOfWire, Findings: all, OverlappingClaims: overlaps, AgentsRun: agentsRun, NonLiveDataSource: true,
	}
	write := func(ev client.EventInput) error {
		r, err := o.ledger.AppendEvent(ctx, ev)
		if err != nil {
			return err
		}
		if r.Created {
			res.LedgerEventsCreated++
		} else {
			res.LedgerEventsAlreadyPresent++
		}
		res.LedgerAppendRetries += r.Attempts - 1
		return nil
	}
	if err := write(started); err != nil {
		o.abort(ctx, scanID, tenant, "ledger append scan started", err)
		return nil, stepErr("ledger append scan started", err)
	}
	for i, ev := range findingEvents {
		if err := write(ev); err != nil {
			step := "ledger append for " + all[i].FindingID
			o.abort(ctx, scanID, tenant, step, err)
			return nil, stepErr(step, err)
		}
		res.LedgerEntriesWritten++
	}
	if err := write(completed); err != nil {
		o.abort(ctx, scanID, tenant, "ledger append scan completed", err)
		return nil, stepErr("ledger append scan completed", err)
	}

	verify, err := o.ledger.Verify(ctx)
	if err != nil {
		return nil, stepErr("ledger verify", err)
	}
	if !verify.Valid {
		// This is exactly the failure mode the ledger exists to catch —
		// surface it as a hard error, never silently continue.
		return nil, &IntegrityError{When: "after scan", Verdict: verify.Error}
	}
	res.LedgerVerify = verify
	return res, nil
}

// ErrBadFinding: detection-py returned a finding the orchestrator will not
// count or record (wrong tenant, wrong agent, id not derived, fields out of
// contract, duplicate id with different content).
var ErrBadFinding = errors.New("detection returned a finding that fails the scan contract")

func contentKey(f client.Finding) string {
	f.DetectedAt = ""
	b, _ := json.Marshal(f)
	return string(b)
}

// abort records, best effort, that a scan which started writing did not
// complete. It never changes the scan's own error. The scan is uncounted
// either way: only a completed event makes a scan count.
func (o *Orchestrator) abort(ctx context.Context, scanID, tenant, step string, cause error) {
	actx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	_, _ = o.ledger.AppendEvent(actx, abortedEvent(scanID, tenant, step+" failed: "+PublicMessage(cause)))
}

// correlate runs the Decision 3 overlap check (E-6, E-10). Findings are
// grouped by correlation key here; only keys claimed by more than one
// distinct agent can overlap, and only those are sent, in batches that never
// split a key, so detection-py's 1,000-findings cap never fails a scan and
// the answer equals one call over everything. detection-py remains the
// safeguard's implementation: its answer must name exactly the candidate
// keys with all their findings, or the scan fails.
func (o *Orchestrator) correlate(ctx context.Context, all []client.Finding) (map[string][]client.Finding, error) {
	overlaps := map[string][]client.Finding{}
	var keys []string
	groups := map[string][]client.Finding{}
	agents := map[string]map[string]bool{}
	for _, f := range all {
		k := CorrelationKey(f.ClientID, f.EntityType, f.EntityID)
		if _, ok := groups[k]; !ok {
			keys = append(keys, k)
			agents[k] = map[string]bool{}
		}
		groups[k] = append(groups[k], f)
		agents[k][f.AgentID] = true
	}
	var batches [][]string
	var cur []string
	n := 0
	for _, k := range keys {
		if len(agents[k]) < 2 {
			continue
		}
		g := len(groups[k])
		if g > correlationBatchSize {
			return nil, fmt.Errorf("entity %s has %d findings, more than one correlation batch", k, g)
		}
		if n+g > correlationBatchSize {
			batches, cur, n = append(batches, cur), nil, 0
		}
		cur, n = append(cur, k), n+g
	}
	if len(cur) > 0 {
		batches = append(batches, cur)
	}
	// No candidates (a clean store, or no entity with two agents): nothing
	// can overlap, so detection-py is not called (E-1).
	for _, batch := range batches {
		var send []client.Finding
		for _, k := range batch {
			send = append(send, groups[k]...)
		}
		got, err := o.detection.CorrelationOverlaps(ctx, send)
		if err != nil {
			return nil, err
		}
		if len(got) != len(batch) {
			return nil, fmt.Errorf("detection-py named %d overlapping entities in a batch of %d candidates", len(got), len(batch))
		}
		for _, k := range batch {
			if len(got[k]) != len(groups[k]) {
				return nil, fmt.Errorf("detection-py's overlap answer for %s disagrees with the findings sent", k)
			}
			overlaps[k] = got[k]
		}
	}
	return overlaps, nil
}
