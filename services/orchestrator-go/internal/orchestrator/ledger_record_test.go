package orchestrator

import (
	"strings"
	"testing"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/client"
)

// Cross-language vectors: the same three are pinned in detection-py
// tests/test_revrec_fix_wave.py, and the first was checked with
// `printf 'rrf1\nfixture-pool\ndiscount-misuse-v1\norder\nord_1007\n' | sha256sum`.
func TestComputeFindingID_CrossLanguageVectors(t *testing.T) {
	for _, v := range []struct{ client, agent, etype, eid, period, want string }{
		{"fixture-pool", "discount-misuse-v1", "order", "ord_1007", "", "rrf1-5b3a1a510e08d4f61650cebebf008903eb533767"},
		{"client_b2", "contract-pricing-term-drift-v1", "contract_term", "term_b2_min_1", "2026-06", "rrf1-7ffe4cd8e48f5047025947ef37f5fdc688fa0fca"},
		{"gid-tenant", "renewal-never-triggered-v1", "subscription", "gid://shopify/Subscription/9", "2026-05-15", "rrf1-2710bcc11b6ee6f5770fb721fd6b92c91b4c4642"},
	} {
		if got := ComputeFindingID(v.client, v.agent, v.etype, v.eid, v.period); got != v.want {
			t.Errorf("%v: got %s", v, got)
		}
	}
	// E-3, probe P2: concatenation no longer collides.
	if ComputeFindingID("a-b", "platform-integration-v1", "platform", "c", "") ==
		ComputeFindingID("a", "platform-integration-v1", "platform", "b-c", "") {
		t.Error("client/platform split collides")
	}
}

func strp(s string) *string { return &s }

// validFinding is a finding checkFinding accepts.
func validFinding(agent, etype, eid, period, amount string) client.Finding {
	f := client.Finding{
		ClientID: FixtureClientID, AgentID: agent, LeakCategory: "discount_misuse", EntityType: etype, EntityID: eid,
		CustomerID: "c1", CauseCertainty: "named", CauseDescription: "d", EvidenceClass: "UNKNOWN",
		MethodologyID: "m", Methodology: "how", DetectedAt: "2026-10-06T00:00:00Z",
	}
	if period != "" {
		f.PeriodLabel = strp(period)
	}
	if amount != "" {
		f.RecoverableValue = &client.LabeledValue{AmountUSD: client.MustParseMoney(amount), Classification: "observed", Confidence: "high"}
		f.EvidenceClass = "OBSERVED"
	}
	f.FindingID = ComputeFindingID(f.ClientID, agent, etype, eid, period)
	return f
}

// The ledger summary of the largest legal finding fits ledger-rust's 280
// characters (detection-py zbm_schema/limits.py sets the bounds).
func TestFindingSummaryWorstCaseFits(t *testing.T) {
	f := validFinding(strings.Repeat("a", maxAgentIDChars), "contract_term", strings.Repeat("e", maxIDChars),
		strings.Repeat("p", maxPeriodChars), "999999999999999.99")
	f.ClientID = strings.Repeat("c", maxIDChars)
	f.FindingID = ComputeFindingID(f.ClientID, f.AgentID, f.EntityType, f.EntityID, *f.PeriodLabel)
	f.LeakCategory = "cross_channel_misattribution_risk"
	// The longest labels; since AEGIS M2 (Oct 7 2026) they need OBSERVED
	// evidence (ESTIMATED is one character longer, but cannot carry them).
	f.EvidenceClass = "OBSERVED"
	f.RecoverableValue.Classification, f.RecoverableValue.Confidence = "financially_verified", "very_high"
	f.MethodologyID = strings.Repeat("m", maxMethodologyIDChars)
	if err := checkFinding(f, f.ClientID, f.AgentID); err != nil {
		t.Fatalf("worst-case finding refused: %v", err)
	}
	s, err := encodeFindingSummary(f)
	if err != nil {
		t.Fatal(err)
	}
	if len(s) != 271 || len(s) > maxSummaryChars {
		t.Errorf("worst-case summary is %d characters (documented 271, max %d)", len(s), maxSummaryChars)
	}
}

// Exact pass-through: the amount string detection-py emitted is the exact
// string in the ledger summary — no float64 in between — and reads back
// identical.
func TestFindingSummaryCarriesTheAmountExactly(t *testing.T) {
	for _, amt := range []string{"0.01", "0.30", "2.01", "49.99", "54.38", "999999999999999.99"} {
		f := validFinding("discount-misuse-v1", "order", "o1", "", amt)
		s, err := encodeFindingSummary(f)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(s, " v="+amt+" ") {
			t.Errorf("summary does not carry %s exactly: %s", amt, s)
		}
		r, err := decodeFindingSummary(s)
		if err != nil || r.AmountUSD == nil || r.AmountUSD.String() != amt {
			t.Errorf("%s did not round-trip: %+v %v", amt, r, err)
		}
	}
}

func TestFindingSummaryRoundTrip(t *testing.T) {
	f := validFinding("renewal-never-triggered-v1", "subscription", "gid://shopify/Subscription/9", "2026-05-15", "")
	s, err := encodeFindingSummary(f)
	if err != nil {
		t.Fatal(err)
	}
	want := "rrf1 a=renewal-never-triggered-v1 l=discount_misuse t=subscription e=gid://shopify/Subscription/9 p=2026-05-15 v=- x=UNKNOWN k=- n=- m=m"
	if s != want {
		t.Fatalf("summary\n got %s\nwant %s", s, want)
	}
	r, err := decodeFindingSummary(s)
	if err != nil || r.AmountUSD != nil || r.PeriodLabel == nil || *r.PeriodLabel != "2026-05-15" || r.EvidenceClass != "UNKNOWN" {
		t.Fatalf("decoded %+v, %v", r, err)
	}
}

func TestDecodeFindingSummaryIsStrict(t *testing.T) {
	ok := "rrf1 a=discount-misuse-v1 l=discount_misuse t=order e=o1 p=- v=4.50 x=OBSERVED k=observed n=high m=disc_excess_best_code"
	if _, err := decodeFindingSummary(ok); err != nil {
		t.Fatalf("valid summary refused: %v", err)
	}
	for name, s := range map[string]string{
		"version":            strings.Replace(ok, "rrf1", "rrf2", 1),
		"field order":        strings.Replace(ok, "a=discount-misuse-v1 l=discount_misuse", "l=discount_misuse a=discount-misuse-v1", 1),
		"extra field":        ok + " z=1",
		"float amount":       strings.Replace(ok, "v=4.50", "v=4.5", 1),
		"zero amount":        strings.Replace(ok, "v=4.50", "v=0.00", 1),
		"amount but UNKNOWN": strings.Replace(ok, "x=OBSERVED", "x=UNKNOWN", 1),
		"no amount, labels":  strings.Replace(ok, "v=4.50", "v=-", 1),
		"unknown evidence":   strings.Replace(ok, "x=OBSERVED", "x=GUESSED", 1),
		"entity type":        strings.Replace(ok, "t=order", "t=invoice", 1),
		"bad entity":         strings.Replace(ok, "e=o1", "e=-o1", 1),
		"double space":       strings.Replace(ok, " e=o1", "  e=o1", 1),
	} {
		if _, err := decodeFindingSummary(s); err == nil {
			t.Errorf("%s: accepted %q", name, s)
		}
	}
}

func TestCheckFindingRefusesWhatCannotBeCountedOrRecorded(t *testing.T) {
	base := validFinding("discount-misuse-v1", "order", "o1", "", "4.50")
	if err := checkFinding(base, FixtureClientID, "discount-misuse-v1"); err != nil {
		t.Fatalf("valid finding refused: %v", err)
	}
	for name, mutate := range map[string]func(*client.Finding){
		"other tenant":         func(f *client.Finding) { f.ClientID = "other" },
		"other agent":          func(f *client.Finding) { f.AgentID = "abandoned-cart-coverage-v1" },
		"id not derived":       func(f *client.Finding) { f.FindingID = "disc-o1" },
		"period changes id":    func(f *client.Finding) { f.PeriodLabel = strp("2026-06") },
		"unknown entity type":  func(f *client.Finding) { f.EntityType = "invoice" },
		"entity too long":      func(f *client.Finding) { f.EntityID = strings.Repeat("e", 65) },
		"entity with space":    func(f *client.Finding) { f.EntityID = "o 1" },
		"empty entity":         func(f *client.Finding) { f.EntityID = "" },
		"no methodology":       func(f *client.Finding) { f.Methodology = "" },
		"methodology id long":  func(f *client.Finding) { f.MethodologyID = strings.Repeat("m", 25) },
		"evidence vs value":    func(f *client.Finding) { f.EvidenceClass = "UNKNOWN" },
		"value missing":        func(f *client.Finding) { f.RecoverableValue = nil },
		"bad evidence":         func(f *client.Finding) { f.EvidenceClass = "observed" },
		"label with space":     func(f *client.Finding) { f.RecoverableValue.Confidence = "very high" },
		"leak category length": func(f *client.Finding) { f.LeakCategory = strings.Repeat("l", 34) },
	} {
		f := base
		rv := *base.RecoverableValue
		f.RecoverableValue = &rv
		mutate(&f)
		if err := checkFinding(f, FixtureClientID, "discount-misuse-v1"); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestStartedAndCompletedSummariesRoundTrip(t *testing.T) {
	ev := startedEvent(startedPayload{ScanID: strings.Repeat("a", 32), ClientID: FixtureClientID,
		AsOf: "2026-10-06T12:00:00Z", DataSource: "fixtures", Fixture: true, TenantDefaulted: true, Agents: []string{"x", "y"}})
	info, err := decodeStartedSummary(ev.Summary)
	if err != nil || !info.Fixture || !info.TenantDefaulted || info.AsOf != "2026-10-06T12:00:00Z" || info.DataSource != "fixtures" {
		t.Fatalf("%q -> %+v %v", ev.Summary, info, err)
	}
	c := completedEvent(strings.Repeat("a", 32), FixtureClientID, 2, [][3]string{{"b", "1", "s"}, {"a", "2", "t"}})
	if n, v, err := decodeCompletedSummary(c.Summary); err != nil || n != 2 || v != 2 || !strings.HasPrefix(c.Summary, "rrc2 ") {
		t.Fatalf("%q -> %d %v", c.Summary, n, err)
	}
	// The manifest does not depend on write order.
	if c.PayloadSHA256 != manifestHash(strings.Repeat("a", 32), FixtureClientID, [][3]string{{"a", "2", "t"}, {"b", "1", "s"}}) {
		t.Error("manifest hash depends on order")
	}
	if n, v, err := decodeCompletedSummary("rrc1 n=3"); err != nil || n != 3 || v != 1 {
		t.Errorf("an 8bdebde rrc1 record no longer reads: %d %d %v", n, v, err)
	}
	for _, bad := range []string{"rrc1 n=", "rrc1 n=-1", "rrc1 n=01", "rrc1 n=1 x", "rrc2 n=", "rrc0 n=1", "rrc n=1", "rrc3 n=1"} {
		if _, _, err := decodeCompletedSummary(bad); err == nil {
			t.Errorf("accepted %q", bad)
		}
	}
	if a := abortedEvent("s", "c", "x\ny\u0085"+strings.Repeat("z", 400)); strings.ContainsAny(a.Summary, "\n\u0085") || len([]rune(a.Summary)) > maxSummaryChars {
		t.Errorf("aborted summary not sanitized: %q", a.Summary)
	}
}
