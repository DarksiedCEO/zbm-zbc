// Package orchestrator implements the Revenue Recovery 1A scan pipeline:
// pull fixture data, call every registered detection agent, run the
// Decision 3 / Failure Mode #2 correlation check, and return the combined
// result. This is the "agent orchestration / workflow" component named in
// Decision 6 — Go was chosen here for low-overhead concurrency and
// operational simplicity, not because the logic itself is complex.
package orchestrator

import (
	"context"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/client"
)

type ScanResult struct {
	Findings             []client.Finding            `json:"findings"`
	OverlappingClaims    map[string][]client.Finding `json:"overlapping_claims"`
	AgentsRun            []string                    `json:"agents_run"`
	NonLiveDataSource    bool                        `json:"non_live_data_source"`
	LedgerEntriesWritten int                         `json:"ledger_entries_written"`
	LedgerVerify         *client.LedgerVerifyResult  `json:"ledger_verify"`
}

type Orchestrator struct {
	detection *client.DetectionClient
	ledger    *client.LedgerClient
}

func New(detectionBaseURL, detectionToken, ledgerBaseURL, ledgerToken string) *Orchestrator {
	return &Orchestrator{
		detection: client.NewDetectionClient(detectionBaseURL, detectionToken),
		ledger:    client.NewLedgerClient(ledgerBaseURL, ledgerToken),
	}
}

// RunFullScan pulls the shared fixture pool and runs every Tier 1
// detection agent against it, then correlates the combined findings.
// Explicitly non-live: this method only ever reads from detection-py's
// /fixtures/* endpoints (no real-store data source exists yet).
//
// Fail fast (fix wave 3): the ledger is checked FIRST (GET /ledger/verify).
// If it is unreachable, refuses the token, or its chain does not verify, the
// scan stops before a single detection call — it used to run every agent
// (15 detection-py calls) and only then fail at the first ledger append,
// having done the work for nothing. A scan whose findings cannot be
// recorded must not run.
func (o *Orchestrator) RunFullScan(ctx context.Context) (*ScanResult, error) {
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

	var all []client.Finding
	agentsRun := []string{}

	affiliate, err := o.detection.DetectAffiliateCouponExtension(ctx, orders)
	if err != nil {
		return nil, stepErr("affiliate-coupon-extension agent", err)
	}
	all = append(all, affiliate...)
	agentsRun = append(agentsRun, "affiliate-coupon-extension-v1")

	discount, err := o.detection.DetectDiscountMisuse(ctx, orders)
	if err != nil {
		return nil, stepErr("discount-misuse agent", err)
	}
	all = append(all, discount...)
	agentsRun = append(agentsRun, "discount-misuse-v1")

	cart, err := o.detection.DetectAbandonedCartCoverage(ctx, orders)
	if err != nil {
		return nil, stepErr("abandoned-cart-coverage agent", err)
	}
	all = append(all, cart...)
	agentsRun = append(agentsRun, "abandoned-cart-coverage-v1")

	renewal, err := o.detection.DetectRenewalNeverTriggered(ctx, subs)
	if err != nil {
		return nil, stepErr("renewal-never-triggered agent", err)
	}
	all = append(all, renewal...)
	agentsRun = append(agentsRun, "renewal-never-triggered-v1")

	// Tier 2 — independent data sources, each agent runs whether or not
	// the others find anything.
	ssEvents, err := o.detection.FixtureServerSideEvents(ctx)
	if err != nil {
		return nil, stepErr("fetch server-side event fixtures", err)
	}
	ssa, err := o.detection.DetectServerSideAttribution(ctx, ssEvents)
	if err != nil {
		return nil, stepErr("server-side-attribution agent", err)
	}
	all = append(all, ssa...)
	agentsRun = append(agentsRun, "server-side-attribution-v1")

	touchpoints, err := o.detection.FixtureChannelTouchpoints(ctx)
	if err != nil {
		return nil, stepErr("fetch channel touchpoint fixtures", err)
	}
	xchan, err := o.detection.DetectCrossChannelAttribution(ctx, touchpoints)
	if err != nil {
		return nil, stepErr("cross-channel-attribution agent", err)
	}
	all = append(all, xchan...)
	agentsRun = append(agentsRun, "cross-channel-attribution-v1")

	platforms, err := o.detection.FixturePlatformConnections(ctx)
	if err != nil {
		return nil, stepErr("fetch platform connection fixtures", err)
	}
	plat, err := o.detection.DetectPlatformIntegration(ctx, platforms)
	if err != nil {
		return nil, stepErr("platform-integration agent", err)
	}
	all = append(all, plat...)
	agentsRun = append(agentsRun, "platform-integration-v1")

	terms, err := o.detection.FixtureContractTerms(ctx)
	if err != nil {
		return nil, stepErr("fetch contract term fixtures", err)
	}
	drift, err := o.detection.DetectContractPricingTermDrift(ctx, terms)
	if err != nil {
		return nil, stepErr("contract-pricing-term-drift agent", err)
	}
	all = append(all, drift...)
	agentsRun = append(agentsRun, "contract-pricing-term-drift-v1")

	overlaps, err := o.detection.CorrelationOverlaps(ctx, all)
	if err != nil {
		return nil, stepErr("correlation check", err)
	}

	// Every finding gets a ledger entry regardless of overlap status — the
	// ledger records what was DETECTED, not a post-valuation-policy number.
	// Deciding how to value an overlapping claim (Decision 3) happens
	// upstream of billing, not upstream of the audit trail.
	written := 0
	for _, f := range all {
		if _, err := o.ledger.AppendFinding(ctx, f); err != nil {
			return nil, stepErr("ledger append for "+f.FindingID, err)
		}
		written++
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

	return &ScanResult{
		Findings:             all,
		OverlappingClaims:    overlaps,
		AgentsRun:            agentsRun,
		NonLiveDataSource:    true,
		LedgerEntriesWritten: written,
		LedgerVerify:         verify,
	}, nil
}
