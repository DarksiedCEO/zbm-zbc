package orchestrator

// AEGIS approved-with-conditions review of fix-revrec 8bdebde (Oct 7 2026),
// orchestrator side: M2 (labels never exceed evidence), M4 (legacy findings
// listed), L1 (value basis recorded), L3 (unfinished scans labelled), and
// the ledger-review item (ledger_entries_total from the ledger, not from the
// size of this process's read).

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/client"
)

// --- M2 ---------------------------------------------------------------------------

func TestM2_LabelsNeverExceedEvidenceForAnyCombination(t *testing.T) {
	for _, ev := range []string{"OBSERVED", "ESTIMATED", "MODELED"} {
		for _, cl := range []string{"observed", "attributed", "incremental", "financially_verified"} {
			for _, cf := range []string{"low", "medium", "high", "very_high"} {
				over := ev != "OBSERVED" && (cl == "observed" || cl == "financially_verified" || cf == "high" || cf == "very_high")
				if got := labelsExceedEvidence(ev, cl, cf) != ""; got != over {
					t.Errorf("%s %s %s: overclaim=%v, want %v", ev, cl, cf, got, over)
				}
				f := validFinding("renewal-never-triggered-v1", "subscription", "s1", "2026-05-15", "39.00")
				f.EvidenceClass, f.RecoverableValue.Classification, f.RecoverableValue.Confidence = ev, cl, cf
				err := checkFinding(f, FixtureClientID, f.AgentID)
				if (err != nil) != over {
					t.Errorf("%s %s %s: checkFinding err=%v, want refused=%v", ev, cl, cf, err, over)
				}
			}
		}
	}
}

// A record written before the invariant (8bdebde's renewal: ESTIMATED,
// observed/high) is still shown, but flagged with the reason.
func TestM2_PreInvariantRecordIsFlaggedOnRead(t *testing.T) {
	old := validFinding("renewal-never-triggered-v1", "subscription", "s1", "2026-05-15", "39.00")
	old.EvidenceClass = "ESTIMATED" // classification observed, confidence high (validFinding)
	fixed := validFinding("discount-misuse-v1", "order", "o1", "", "4.50")
	c := &chain{}
	c.writeScan(t, scanID(1), []client.Finding{old, fixed}, nil)
	res, _ := readRecorded(t, c.json(t))
	for _, f := range res.Findings {
		flagged := f.LabelsExceedEvidence != nil
		if flagged != (f.FindingID == old.FindingID) {
			t.Errorf("%s flagged=%v (%v)", f.AgentID, flagged, f.LabelsExceedEvidence)
		}
		if flagged && !strings.Contains(*f.LabelsExceedEvidence, "needs OBSERVED evidence") {
			t.Errorf("reason: %s", *f.LabelsExceedEvidence)
		}
	}
}

// --- L1 ---------------------------------------------------------------------------

func basisFinding(base, rate, amount string) client.Finding {
	f := validFinding("affiliate-coupon-extension-v1", "order", "o1", "", amount)
	f.EvidenceClass = "ESTIMATED"
	f.RecoverableValue.Classification, f.RecoverableValue.Confidence = "attributed", "medium"
	f.ValueBasis = &client.ValueBasis{BaseUSD: client.MustParseMoney(base), RatePercent: rate}
	return f
}

// Same half-up cent rounding as detection-py zbm_schema.money.percent_of
// (vectors computed there).
func TestL1_ValueBasisAmountMatchesDetectionPy(t *testing.T) {
	for _, v := range []struct{ base, rate, want string }{
		{"120.00", "10", "12.00"}, {"400.00", "10", "40.00"}, {"0.15", "10", "0.02"}, {"0.05", "10", "0.01"},
		{"0.04", "10", "0.00"}, {"19.99", "12.5", "2.50"}, {"999999999999999.99", "100", "999999999999999.99"},
		{"1.00", "0.00001", "0.00"}, {"33.33", "33.333", "11.11"},
	} {
		got, ok := valueBasisAmount(client.MustParseMoney(v.base), v.rate)
		if v.want == "0.00" {
			if ok {
				t.Errorf("%s x %s%%: %s, want no positive amount", v.base, v.rate, got)
			}
			continue
		}
		if !ok || got != v.want {
			t.Errorf("%s x %s%% = %s (%v), want %s", v.base, v.rate, got, ok, v.want)
		}
	}
}

func TestL1_CheckFindingRefusesABasisThatDoesNotReproduceTheAmount(t *testing.T) {
	if err := checkFinding(basisFinding("120.00", "10", "12.00"), FixtureClientID, "affiliate-coupon-extension-v1"); err != nil {
		t.Fatalf("valid basis refused: %v", err)
	}
	for name, f := range map[string]client.Finding{
		"amount differs": basisFinding("400.00", "10", "12.00"),
		"rate zero":      basisFinding("120.00", "0", "12.00"),
		"rate over 100":  basisFinding("120.00", "100.5", "12.00"),
		"rate exponent":  basisFinding("120.00", "1e1", "12.00"),
		"rate too long":  basisFinding("120.00", "10."+strings.Repeat("0", 32), "12.00"),
	} {
		if err := checkFinding(f, FixtureClientID, "affiliate-coupon-extension-v1"); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	noValue := validFinding("affiliate-coupon-extension-v1", "order", "o1", "", "")
	noValue.ValueBasis = &client.ValueBasis{BaseUSD: client.MustParseMoney("1.00"), RatePercent: "10"}
	if err := checkFinding(noValue, FixtureClientID, noValue.AgentID); err == nil {
		t.Error("a basis without a value was accepted")
	}
}

func TestL1_ValueBasisIsRecordedAndReadBack(t *testing.T) {
	f := basisFinding("120.00", "10", "12.00")
	ev, err := basisEvent(scanID(1), f)
	if err != nil {
		t.Fatal(err)
	}
	if ev.Summary != "rrb1 base=120.00 rate=10" || ev.EventType != "rr_value_basis" || ev.SubjectID != f.FindingID ||
		ev.EventID != "rr."+scanID(1)+".b."+f.FindingID || len(ev.EventID) > 128 {
		t.Fatalf("basis event: %+v", ev)
	}
	c := &chain{}
	c.writeScan(t, scanID(1), []client.Finding{f, validFinding("discount-misuse-v1", "order", "o2", "", "4.50")}, nil)
	res, _ := readRecorded(t, c.json(t))
	if len(res.Scans) != 1 || res.Scans[0].Findings != 2 {
		t.Fatalf("scan with a basis event not counted: %+v %+v", res.Scans, res.ExcludedScans)
	}
	for _, r := range res.Findings {
		if (r.FindingID == f.FindingID) != (r.ValueBasis != nil) {
			t.Errorf("%s value basis %+v", r.AgentID, r.ValueBasis)
		}
		if r.ValueBasis != nil && (r.ValueBasis.BaseUSD.String() != "120.00" || r.ValueBasis.RatePercent != "10") {
			t.Errorf("basis read back as %+v", r.ValueBasis)
		}
	}
	b, _ := json.Marshal(res.Findings)
	if !strings.Contains(string(b), `"value_basis":{"base_usd":"120.00","rate_percent":"10"}`) {
		t.Errorf("value_basis not on the wire: %s", b)
	}
}

func TestL1_TamperedOrMisplacedBasisExcludesTheScan(t *testing.T) {
	f := basisFinding("120.00", "10", "12.00")
	other := validFinding("discount-misuse-v1", "order", "o2", "", "4.50")
	for name, mutate := range map[string]func([]client.EventInput) []client.EventInput{
		"base altered": func(evs []client.EventInput) []client.EventInput {
			evs[2].Summary = "rrb1 base=400.00 rate=10"
			return evs
		},
		"basis dropped (manifest)": func(evs []client.EventInput) []client.EventInput {
			return append(evs[:2:2], evs[3:]...)
		},
		"basis for a finding without one": func(evs []client.EventInput) []client.EventInput {
			evs[2].SubjectID, evs[2].EventID = other.FindingID, basisEventID(scanID(1), other.FindingID)
			return evs
		},
	} {
		c := &chain{}
		c.writeScan(t, scanID(1), []client.Finding{f, other}, mutate)
		res, _ := readRecorded(t, c.json(t))
		if len(res.Scans) != 0 || len(res.ExcludedScans) != 1 || res.ExcludedScans[0].Status != ScanStatusInconsistent {
			t.Errorf("%s: scans %+v excluded %+v", name, res.Scans, res.ExcludedScans)
		}
	}
}

// --- M4 / L3 ----------------------------------------------------------------------

func TestL3_UnfinishedScansAreLabelled(t *testing.T) {
	now := time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)
	c := &chain{}
	cut := func(evs []client.EventInput) []client.EventInput { return evs[:2] } // started + 1 finding, no completion
	disc := validFinding("discount-misuse-v1", "order", "o1", "", "4.50")
	c.writeScan(t, scanID(1), []client.Finding{disc}, cut) // seq 0-1: abandoned (old)
	c.writeScan(t, scanID(2), []client.Finding{disc}, cut) // seq 2-3: incomplete (recent)
	c.writeScan(t, scanID(3), []client.Finding{disc}, cut) // seq 4-5: running (this process)
	c.writeScan(t, scanID(4), []client.Finding{disc}, func(evs []client.EventInput) []client.EventInput {
		return append(evs[:len(evs)-1:len(evs)-1], abortedEvent(scanID(4), FixtureClientID, "x"))
	})
	c.writeScan(t, scanID(5), []client.Finding{disc}, func(evs []client.EventInput) []client.EventInput {
		evs[len(evs)-1].Summary = "rrc1 n=7"
		return evs
	})
	c.writeScan(t, scanID(6), []client.Finding{disc}, cut) // unparseable start time: incomplete
	c.entries[0]["recorded_at"] = now.Add(-ScanAbandonedAfter).Format(time.RFC3339Nano)
	c.entries[2]["recorded_at"] = now.Add(-ScanAbandonedAfter + time.Second).Format(time.RFC3339Nano)
	c.entries[4]["recorded_at"] = now.Add(-time.Hour).Format(time.RFC3339Nano)

	fl := &fakeLedger{entries: c.json(t), verify: `{"valid":true,"entries":0}`}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
	o.now = func() time.Time { return now }
	o.setRunning(scanID(3))
	res, err := o.RecordedFindings(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{scanID(1): ScanStatusAbandoned, scanID(2): ScanStatusIncomplete, scanID(3): ScanStatusRunning,
		scanID(4): ScanStatusAborted, scanID(5): ScanStatusInconsistent, scanID(6): ScanStatusIncomplete}
	if len(res.ExcludedScans) != len(want) {
		t.Fatalf("excluded %+v", res.ExcludedScans)
	}
	for _, x := range res.ExcludedScans {
		if x.Status != want[x.ScanID] || x.Reason == "" || x.ClientID != FixtureClientID {
			t.Errorf("scan %s: status %q (want %q), %+v", x.ScanID, x.Status, want[x.ScanID], x)
		}
	}
	if res.ExcludedScans[0].StartedAt != now.Add(-ScanAbandonedAfter).Format(time.RFC3339Nano) {
		t.Errorf("started_at: %q", res.ExcludedScans[0].StartedAt)
	}
	if len(res.Findings) != 0 {
		t.Errorf("an excluded scan's findings are counted: %+v", res.Findings)
	}
}

func TestM4_LegacyFindingsAreListedWithOutOfContractAmountsMarked(t *testing.T) {
	c := &chain{}
	c.raw(map[string]any{"finding_id": "disc-ord_1007", "agent_id": "discount-misuse-v1", "entity_id": "ord_1007",
		"leak_category": "discount_misuse", "amount_usd": "54.38", "value_classification": "observed",
		"decision_confidence": "very_high"}, "finding")
	c.raw(map[string]any{"finding_id": "neg", "agent_id": "a", "entity_id": "e", "leak_category": "l",
		"amount_usd": "-5.00", "value_classification": "observed", "decision_confidence": "high"}, "finding")
	c.raw(map[string]any{"finding_id": "none", "agent_id": "a", "entity_id": "e", "leak_category": "l",
		"amount_usd": nil, "value_classification": nil, "decision_confidence": nil}, "finding")
	res, _ := readRecorded(t, c.json(t))
	if res.LegacyFindingEntriesIgnored != 3 || len(res.LegacyFindings) != 3 || len(res.Findings) != 0 {
		t.Fatalf("legacy: %+v", res)
	}
	l := res.LegacyFindings
	if l[0].AmountUSD.String() != "54.38" || l[0].AmountOutOfContract || l[0].EntityID != "ord_1007" || l[0].Seq != 0 {
		t.Errorf("legacy 0: %+v", l[0])
	}
	if l[1].AmountUSD != nil || !l[1].AmountOutOfContract {
		t.Errorf("a negative legacy amount must be marked, never shown: %+v", l[1])
	}
	if l[2].AmountUSD != nil || l[2].AmountOutOfContract {
		t.Errorf("legacy without amount: %+v", l[2])
	}
}

// --- ledger total (ledger review, client/ledger.go:167) ---------------------------

func TestLedgerTotal_ComesFromTheLedgerNotFromTheRead(t *testing.T) {
	c := &chain{}
	c.writeScan(t, scanID(1), []client.Finding{validFinding("discount-misuse-v1", "order", "o1", "", "4.50")}, nil)
	read := c.json(t) // 3 entries: what a revenue_recovery-scoped read returns
	for name, tc := range map[string]struct {
		head, verify, source string
		want                 int
	}{
		"head route (ledger-rust sweep F)": {`{"entries":40,"head_seq":39,"head_hash":"` + strings.Repeat("a", 64) + `"}`,
			`{"valid":true,"entries":40}`, "head", 40},
		"no head route: the verify count": {"", `{"valid":true,"entries":40}`, "verify", 40},
		"empty ledger head":               {`{"entries":0,"head_seq":null,"head_hash":"` + strings.Repeat("0", 64) + `"}`, `{"valid":true,"entries":0}`, "head", 0},
	} {
		fl := &fakeLedger{entries: read, verify: tc.verify, head: tc.head}
		o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
		res, err := o.RecordedFindings(context.Background())
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if res.LedgerEntriesTotal != tc.want || res.LedgerTotalSource != tc.source || res.LedgerEntriesRead != 3 {
			t.Errorf("%s: total %d (%s), read %d", name, res.LedgerEntriesTotal, res.LedgerTotalSource, res.LedgerEntriesRead)
		}
		if tc.source == "head" || tc.source == "verify" {
			if res.LedgerEntriesTotal != res.LedgerVerify.Entries {
				t.Errorf("%s: total %d disagrees with verify %d", name, res.LedgerEntriesTotal, res.LedgerVerify.Entries)
			}
		}
	}
}

func TestLedgerTotal_BadHeadIsAnErrorAndInvalidChainFallsBackToTheRead(t *testing.T) {
	for _, head := range []string{`{"entries":2,"head_seq":5,"head_hash":"` + strings.Repeat("a", 64) + `"}`,
		`{"entries":2,"head_seq":1,"head_hash":"xyz"}`, `{"entries":0,"head_seq":0,"head_hash":"` + strings.Repeat("a", 64) + `"}`, `[]`} {
		fl := &fakeLedger{entries: `[]`, verify: `{"valid":true,"entries":0}`, head: head}
		o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
		if _, err := o.RecordedFindings(context.Background()); err == nil {
			t.Errorf("head %s accepted", head)
		}
	}
	fl := &fakeLedger{entries: `[]`, verify: `{"valid":false,"error":"ChainBroken"}`, verifyCode: 409}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
	res, err := o.RecordedFindings(context.Background())
	if err != nil || res.LedgerTotalSource != "entries_read" || res.LedgerVerify.Valid {
		t.Fatalf("invalid chain without head: %+v %v", res, err)
	}
}

// --- AEGIS re-review of 45bc33c (N1-N4) -------------------------------------------

// N1: scans are completed as rrc2. A reader meets a NEWER completion format
// only after a rollback: it must refuse that scan loudly (unsupported_format,
// explicit reason) and must not present the older counted scan as latest.
func TestN1_NewerCompletionFormatIsRefusedLoudlyAndNeverFallsBack(t *testing.T) {
	old := validFinding("discount-misuse-v1", "order", "o1", "", "4.50")
	c := &chain{}
	c.writeScan(t, scanID(1), []client.Finding{old}, nil)
	c.writeScan(t, scanID(2), []client.Finding{validFinding("discount-misuse-v1", "order", "o2", "", "5.00")},
		func(evs []client.EventInput) []client.EventInput {
			evs[len(evs)-1].Summary = "rrc3 n=1" // written by a later orchestrator-go
			return evs
		})
	res, _ := readRecorded(t, c.json(t))
	if len(res.ExcludedScans) != 1 || res.ExcludedScans[0].Status != ScanStatusUnsupportedFormat ||
		!strings.Contains(res.ExcludedScans[0].Reason, "cannot be rolled back") {
		t.Fatalf("excluded: %+v", res.ExcludedScans)
	}
	if len(res.Findings) != 1 || res.Findings[0].PresentInLatestScan || res.Findings[0].Quotable {
		t.Errorf("the older scan's finding is presented as current: %+v", res.Findings)
	}
	if res.LatestScanUncounted[FixtureClientID] != scanID(2) {
		t.Errorf("latest_scan_uncounted: %v", res.LatestScanUncounted)
	}
}

// N1: this version writes rrc2 and still reads 8bdebde's rrc1 scans; an rrc1
// scan carrying a value-basis event is inconsistent (no rrc1 writer made one).
func TestN1_WritesRrc2ReadsRrc1(t *testing.T) {
	f := validFinding("discount-misuse-v1", "order", "o1", "", "4.50")
	ev, _ := findingEvent(scanID(1), f)
	if c := completedEvent(scanID(1), FixtureClientID, 1, [][3]string{{ev.EventID, ev.PayloadSHA256, ev.Summary}}); !strings.HasPrefix(c.Summary, "rrc2 n=1") {
		t.Fatalf("completion summary %q", c.Summary)
	}
	c := &chain{}
	c.writeScan(t, scanID(1), []client.Finding{f}, func(evs []client.EventInput) []client.EventInput {
		evs[len(evs)-1].Summary = "rrc1 n=1" // an 8bdebde scan: same manifest rule, no basis events
		return evs
	})
	b := basisFinding("120.00", "10", "12.00")
	c.writeScan(t, scanID(2), []client.Finding{b}, func(evs []client.EventInput) []client.EventInput {
		evs[len(evs)-1].Summary = "rrc1 n=1"
		return evs
	})
	res, _ := readRecorded(t, c.json(t))
	if len(res.Scans) != 1 || res.Scans[0].ScanID != scanID(1) {
		t.Errorf("rrc1 scan not read: %+v", res.Scans)
	}
	if len(res.ExcludedScans) != 1 || res.ExcludedScans[0].Status != ScanStatusInconsistent ||
		!strings.Contains(res.ExcludedScans[0].Reason, "rrc1 scan cannot have value basis") {
		t.Errorf("rrc1 + basis: %+v", res.ExcludedScans)
	}
	// An inconsistent scan completed after the counted one: the counted one is not "latest" either.
	if res.Findings[0].PresentInLatestScan || res.LatestScanUncounted[FixtureClientID] != scanID(2) {
		t.Errorf("fell back to the older scan: %+v %v", res.Findings[0], res.LatestScanUncounted)
	}
}

// N2: unknown labels are refused on write and flagged on read.
func TestN2_UnknownLabelsRefusedOnWriteFlaggedOnRead(t *testing.T) {
	for _, lbl := range [][2]string{{"verified", "medium"}, {"observed", "certain"}, {"verified", "certain"}} {
		f := validFinding("discount-misuse-v1", "order", "o1", "", "4.50") // OBSERVED evidence
		f.RecoverableValue.Classification, f.RecoverableValue.Confidence = lbl[0], lbl[1]
		if err := checkFinding(f, FixtureClientID, f.AgentID); err == nil || !strings.Contains(err.Error(), "not a known value label") {
			t.Errorf("%v accepted on write: %v", lbl, err)
		}
		c := &chain{}
		c.writeScan(t, scanID(1), []client.Finding{f}, nil) // a record some other writer made
		res, _ := readRecorded(t, c.json(t))
		if len(res.Findings) != 1 || res.Findings[0].LabelsExceedEvidence == nil ||
			!strings.Contains(*res.Findings[0].LabelsExceedEvidence, "not a known value label") || res.Findings[0].Quotable {
			t.Errorf("%v not flagged on read: %+v", lbl, res.Findings)
		}
	}
}

// N4: the served quotable flag — current, OBSERVED, known labels within
// evidence, valid value basis.
func TestN4_QuotableIsServedPerFinding(t *testing.T) {
	obs := validFinding("discount-misuse-v1", "order", "o1", "", "4.50")
	est := basisFinding("120.00", "10", "12.00")
	none := validFinding("abandoned-cart-coverage-v1", "order", "o3", "", "")
	over := validFinding("renewal-never-triggered-v1", "subscription", "s1", "2026-05-15", "39.00")
	over.EvidenceClass = "ESTIMATED"
	staleF := validFinding("discount-misuse-v1", "order", "o9", "", "7.00")
	c := &chain{}
	c.writeScan(t, scanID(1), []client.Finding{staleF}, nil)
	c.writeScan(t, scanID(2), []client.Finding{obs, est, none, over}, nil)
	res, _ := readRecorded(t, c.json(t))
	want := map[string]bool{obs.FindingID: true, est.FindingID: false, none.FindingID: false, over.FindingID: false, staleF.FindingID: false}
	for _, f := range res.Findings {
		if f.Quotable != want[f.FindingID] {
			t.Errorf("%s %s: quotable %v", f.AgentID, f.EntityID, f.Quotable)
		}
	}
	b, _ := json.Marshal(res.Findings[0])
	if !strings.Contains(string(b), `"quotable":`) {
		t.Errorf("quotable not on the wire: %s", b)
	}
	// A value basis that no longer reproduces the amount is never quotable.
	amt := client.MustParseMoney("4.50")
	r := RecordedFinding{AmountUSD: &amt, PresentInLatestScan: true, EvidenceClass: "OBSERVED",
		ValueClassification: strp("observed"), DecisionConfidence: strp("high"),
		ValueBasis: &client.ValueBasis{BaseUSD: client.MustParseMoney("400.00"), RatePercent: "10"}}
	if r.isQuotable() {
		t.Error("a bad value basis is quotable")
	}
	r.ValueBasis = nil
	if !r.isQuotable() {
		t.Error("a plain observed figure is not quotable")
	}
}

// N3: a scan whose as_of is earlier than the previous scan's as_of is backdated.
func TestN3_BackdatedScanIsFlagged(t *testing.T) {
	c := &chain{}
	f := validFinding("discount-misuse-v1", "order", "o1", "", "4.50")
	asOf := func(v string) func([]client.EventInput) []client.EventInput {
		return func(evs []client.EventInput) []client.EventInput {
			id := strings.Split(evs[0].EventID, ".")[1]
			evs[0] = startedEvent(startedPayload{ScanID: id, ClientID: FixtureClientID, AsOf: v, DataSource: "fixtures",
				Fixture: true, TenantDefaulted: true, Agents: []string{"x"}})
			return evs
		}
	}
	c.writeScan(t, scanID(1), []client.Finding{f}, asOf("2026-10-05T00:00:00Z"))
	c.writeScan(t, scanID(2), []client.Finding{f}, asOf("2026-10-06T00:00:00Z"))
	c.writeScan(t, scanID(3), []client.Finding{f}, asOf("2026-09-01T00:00:00Z"))
	c.writeScan(t, scanID(4), []client.Finding{f}, asOf("2026-10-06T00:00:00Z"))
	res, _ := readRecorded(t, c.json(t))
	got := []bool{}
	for _, s := range res.Scans {
		got = append(got, s.Backdated)
	}
	if len(got) != 4 || got[0] || got[1] || !got[2] || got[3] {
		t.Errorf("backdated flags %v", got)
	}
}
