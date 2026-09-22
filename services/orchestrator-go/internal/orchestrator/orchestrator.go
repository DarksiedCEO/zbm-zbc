// Package orchestrator implements the Revenue Recovery 1A scan pipeline:
// pull fixture data, call every registered detection agent, run the
// Decision 3 / Failure Mode #2 correlation check, and return the combined
// result. This is the "agent orchestration / workflow" component named in
// Decision 6 — Go was chosen here for low-overhead concurrency and
// operational simplicity, not because the logic itself is complex.
package orchestrator

import (
	"context"
	"fmt"

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

func New(detectionBaseURL, ledgerBaseURL string) *Orchestrator {
	return &Orchestrator{
		detection: client.NewDetectionClient(detectionBaseURL),
		ledger:    client.NewLedgerClient(ledgerBaseURL),
	}
}

// RunFullScan pulls the shared fixture pool and runs every Tier 1
// detection agent against it, then correlates the combined findings.
// Explicitly non-live: this method only ever reads from detection-py's
// /fixtures/* endpoints (no real-store data source exists yet).
func (o *Orchestrator) RunFullScan(ctx context.Context) (*ScanResult, error) {
	orders, err := o.detection.FixtureOrders(ctx)
	if err != nil {
		return nil, fmt.Errorf("fetch fixture orders: %w", err)
	}
	subs, err := o.detection.FixtureSubscriptions(ctx)
	if err != nil {
		return nil, fmt.Errorf("fetch fixture subscriptions: %w", err)
	}

	var all []client.Finding
	agentsRun := []string{}

	affiliate, err := o.detection.DetectAffiliateCouponExtension(ctx, orders)
	if err != nil {
		return nil, fmt.Errorf("affiliate-coupon-extension agent: %w", err)
	}
	all = append(all, affiliate...)
	agentsRun = append(agentsRun, "affiliate-coupon-extension-v1")

	discount, err := o.detection.DetectDiscountMisuse(ctx, orders)
	if err != nil {
		return nil, fmt.Errorf("discount-misuse agent: %w", err)
	}
	all = append(all, discount...)
	agentsRun = append(agentsRun, "discount-misuse-v1")

	cart, err := o.detection.DetectAbandonedCartCoverage(ctx, orders)
	if err != nil {
		return nil, fmt.Errorf("abandoned-cart-coverage agent: %w", err)
	}
	all = append(all, cart...)
	agentsRun = append(agentsRun, "abandoned-cart-coverage-v1")

	renewal, err := o.detection.DetectRenewalNeverTriggered(ctx, subs)
	if err != nil {
		return nil, fmt.Errorf("renewal-never-triggered agent: %w", err)
	}
	all = append(all, renewal...)
	agentsRun = append(agentsRun, "renewal-never-triggered-v1")

	// Tier 2 — independent data sources, each agent runs whether or not
	// the others find anything.
	ssEvents, err := o.detection.FixtureServerSideEvents(ctx)
	if err != nil {
		return nil, fmt.Errorf("fetch server-side event fixtures: %w", err)
	}
	ssa, err := o.detection.DetectServerSideAttribution(ctx, ssEvents)
	if err != nil {
		return nil, fmt.Errorf("server-side-attribution agent: %w", err)
	}
	all = append(all, ssa...)
	agentsRun = append(agentsRun, "server-side-attribution-v1")

	touchpoints, err := o.detection.FixtureChannelTouchpoints(ctx)
	if err != nil {
		return nil, fmt.Errorf("fetch channel touchpoint fixtures: %w", err)
	}
	xchan, err := o.detection.DetectCrossChannelAttribution(ctx, touchpoints)
	if err != nil {
		return nil, fmt.Errorf("cross-channel-attribution agent: %w", err)
	}
	all = append(all, xchan...)
	agentsRun = append(agentsRun, "cross-channel-attribution-v1")

	platforms, err := o.detection.FixturePlatformConnections(ctx)
	if err != nil {
		return nil, fmt.Errorf("fetch platform connection fixtures: %w", err)
	}
	plat, err := o.detection.DetectPlatformIntegration(ctx, platforms)
	if err != nil {
		return nil, fmt.Errorf("platform-integration agent: %w", err)
	}
	all = append(all, plat...)
	agentsRun = append(agentsRun, "platform-integration-v1")

	terms, err := o.detection.FixtureContractTerms(ctx)
	if err != nil {
		return nil, fmt.Errorf("fetch contract term fixtures: %w", err)
	}
	drift, err := o.detection.DetectContractPricingTermDrift(ctx, terms)
	if err != nil {
		return nil, fmt.Errorf("contract-pricing-term-drift agent: %w", err)
	}
	all = append(all, drift...)
	agentsRun = append(agentsRun, "contract-pricing-term-drift-v1")

	overlaps, err := o.detection.CorrelationOverlaps(ctx, all)
	if err != nil {
		return nil, fmt.Errorf("correlation check: %w", err)
	}

	// Every finding gets a ledger entry regardless of overlap status — the
	// ledger records what was DETECTED, not a post-valuation-policy number.
	// Deciding how to value an overlapping claim (Decision 3) happens
	// upstream of billing, not upstream of the audit trail.
	written := 0
	for _, f := range all {
		if _, err := o.ledger.AppendFinding(ctx, f); err != nil {
			return nil, fmt.Errorf("ledger append for %s: %w", f.FindingID, err)
		}
		written++
	}

	verify, err := o.ledger.Verify(ctx)
	if err != nil {
		return nil, fmt.Errorf("ledger verify: %w", err)
	}
	if !verify.Valid {
		// This is exactly the failure mode the ledger exists to catch —
		// surface it as a hard error, never silently continue.
		return nil, fmt.Errorf("LEDGER INTEGRITY FAILURE after scan: %s", verify.Error)
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
