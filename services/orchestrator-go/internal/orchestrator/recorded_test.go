package orchestrator

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/client"
)

// fakeLedger serves GET /ledger/entries and GET /ledger/verify and records
// every request, so a test can prove the read path never writes.
type fakeLedger struct {
	mu         sync.Mutex
	requests   []string
	entries    string
	verify     string
	verifyCode int
}

func (f *fakeLedger) server(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.requests = append(f.requests, r.Method+" "+r.URL.Path)
		f.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/ledger/entries":
			_, _ = w.Write([]byte(f.entries))
		case r.Method == http.MethodGet && r.URL.Path == "/ledger/verify":
			if f.verifyCode != 0 {
				w.WriteHeader(f.verifyCode)
			}
			_, _ = w.Write([]byte(f.verify))
		default:
			t.Errorf("read path made a non-read ledger request: %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusTeapot)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func failingDetection(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("read path must not call detection-py (it would mean a scan): %s %s", r.Method, r.URL.Path)
		w.WriteHeader(http.StatusTeapot)
	}))
	t.Cleanup(srv.Close)
	return srv
}

// chain builds ledger entries (as GET /ledger/entries serves them) from
// events, numbering seq and chaining hashes.
type chain struct{ entries []map[string]any }

func (c *chain) event(ev client.EventInput) {
	b, _ := json.Marshal(ev)
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	c.raw(m, "event")
}

func (c *chain) raw(m map[string]any, kind string) {
	seq := len(c.entries)
	m["kind"], m["seq"], m["recorded_at"] = kind, seq, fmt.Sprintf("t%d", seq)
	m["prev_hash"], m["hash"] = fmt.Sprintf("h%d", seq-1), fmt.Sprintf("h%d", seq)
	c.entries = append(c.entries, m)
}

func (c *chain) json(t *testing.T) string {
	t.Helper()
	b, err := json.Marshal(c.entries)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func scanID(n int) string { return fmt.Sprintf("%032x", n) }

// writeScan appends one whole scan; mutate (optional) edits the event list
// before it is appended, to build broken scans.
func (c *chain) writeScan(t *testing.T, id string, fs []client.Finding, mutate func([]client.EventInput) []client.EventInput) {
	t.Helper()
	evs := []client.EventInput{startedEvent(startedPayload{ScanID: id, ClientID: FixtureClientID,
		AsOf: "2026-10-06T00:00:00Z", DataSource: "fixtures", Fixture: true, TenantDefaulted: true, Agents: []string{"x"}})}
	var manifest [][3]string
	for _, f := range fs {
		ev, err := findingEvent(id, f)
		if err != nil {
			t.Fatal(err)
		}
		evs = append(evs, ev)
		manifest = append(manifest, [3]string{ev.EventID, ev.PayloadSHA256, ev.Summary})
	}
	evs = append(evs, completedEvent(id, FixtureClientID, manifest))
	if mutate != nil {
		evs = mutate(evs)
	}
	for _, ev := range evs {
		c.event(ev)
	}
}

func readRecorded(t *testing.T, entries string) (*RecordedFindingsResult, *fakeLedger) {
	t.Helper()
	fl := &fakeLedger{entries: entries, verify: `{"valid":true,"entries":1}`}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
	res, err := o.RecordedFindings(context.Background())
	if err != nil {
		t.Fatalf("recorded findings: %v", err)
	}
	return res, fl
}

var (
	discO1  = validFinding("discount-misuse-v1", "order", "o1", "", "4.50")
	cartO1  = validFinding("abandoned-cart-coverage-v1", "order", "o1", "", "")
	renewS1 = validFinding("renewal-never-triggered-v1", "subscription", "s1", "2026-05-15", "39.00")
)

func TestRecordedFindings_ReadsLedgerOnlyAndNeverWritesOrScans(t *testing.T) {
	c := &chain{}
	c.raw(map[string]any{"finding_id": "disc-ord_1007", "amount_usd": "54.38"}, "finding") // pre-fix-wave
	c.raw(map[string]any{"event_id": "onb-1", "department": "onboarding", "event_type": "x", "actor": "a",
		"subject_id": "s", "payload_sha256": "00", "summary": "s"}, "event")
	c.writeScan(t, scanID(1), []client.Finding{discO1, cartO1, renewS1}, nil)
	res, fl := readRecorded(t, c.json(t))
	for _, req := range fl.requests {
		if req != "GET /ledger/entries" && req != "GET /ledger/verify" {
			t.Errorf("unexpected ledger request %q", req)
		}
	}
	if len(fl.requests) != 2 {
		t.Errorf("one read of each, got %v", fl.requests)
	}
	if res.LedgerEntriesTotal != 7 || res.FindingEntriesTotal != 3 || res.LegacyFindingEntriesIgnored != 1 {
		t.Errorf("totals: %+v", res)
	}
	if len(res.Scans) != 1 || res.Scans[0].Findings != 3 || !res.Scans[0].Fixture || len(res.ExcludedScans) != 0 {
		t.Fatalf("scans: %+v excluded %+v", res.Scans, res.ExcludedScans)
	}
	var renew *RecordedFinding
	for i := range res.Findings {
		if res.Findings[i].AgentID == "renewal-never-triggered-v1" {
			renew = &res.Findings[i]
		}
	}
	if renew == nil || renew.FindingID != renewS1.FindingID || renew.AmountUSD.String() != "39.00" ||
		*renew.PeriodLabel != "2026-05-15" || renew.EvidenceClass != "OBSERVED" || renew.ScanID != scanID(1) ||
		renew.ClientID != FixtureClientID || renew.TimesRecorded != 1 || !renew.PresentInLatestScan {
		t.Errorf("renewal row: %+v", renew)
	}
	if fs := res.OverlappingClaims["fixture-pool|order|o1"]; len(fs) != 2 || len(res.OverlappingClaims) != 1 {
		t.Errorf("overlaps: %v", res.OverlappingClaims)
	}
}

// E-2: only a completed, self-consistent scan counts. Every way a scan can
// be partial or inconsistent excludes it — and only it.
func TestRecordedFindings_PartialOrInconsistentScansAreExcluded(t *testing.T) {
	drop := func(i int) func([]client.EventInput) []client.EventInput {
		return func(evs []client.EventInput) []client.EventInput { return append(evs[:i:i], evs[i+1:]...) }
	}
	cases := map[string]func([]client.EventInput) []client.EventInput{
		"no completion (failed midway)": func(evs []client.EventInput) []client.EventInput { return evs[:2] },
		"no start":                      drop(0),
		"a finding missing":             drop(1),
		"aborted": func(evs []client.EventInput) []client.EventInput {
			return append(evs[:len(evs)-1:len(evs)-1], abortedEvent(scanID(2), FixtureClientID, "x"))
		},
		"finding after completion": func(evs []client.EventInput) []client.EventInput {
			n := len(evs)
			return append(append(evs[:1:1], evs[2:n]...), evs[1])
		},
		"payload hash altered": func(evs []client.EventInput) []client.EventInput {
			evs[1].PayloadSHA256 = strings.Repeat("0", 64)
			return evs
		},
		"amount altered": func(evs []client.EventInput) []client.EventInput {
			evs[1].Summary = strings.Replace(evs[1].Summary, "v=4.50", "v=45.00", 1)
			return evs
		},
		"entity altered (id no longer derived)": func(evs []client.EventInput) []client.EventInput {
			evs[1].Summary = strings.Replace(evs[1].Summary, "e=o1", "e=o2", 1)
			return evs
		},
		"count altered": func(evs []client.EventInput) []client.EventInput {
			evs[len(evs)-1].Summary = "rrc1 n=1"
			return evs
		},
		"other client on completion": func(evs []client.EventInput) []client.EventInput {
			evs[len(evs)-1].SubjectID = "someone-else"
			return evs
		},
	}
	for name, mutate := range cases {
		c := &chain{}
		c.writeScan(t, scanID(1), []client.Finding{renewS1}, nil)
		c.writeScan(t, scanID(2), []client.Finding{discO1, cartO1}, mutate)
		res, _ := readRecorded(t, c.json(t))
		if len(res.Scans) != 1 || res.Scans[0].ScanID != scanID(1) {
			t.Errorf("%s: counted scans %+v", name, res.Scans)
		}
		if len(res.ExcludedScans) != 1 || res.ExcludedScans[0].ScanID != scanID(2) {
			t.Errorf("%s: excluded %+v", name, res.ExcludedScans)
		}
		if len(res.Findings) != 1 || res.Findings[0].FindingID != renewS1.FindingID || len(res.OverlappingClaims) != 0 {
			t.Errorf("%s: a partial scan's findings are visible: %+v", name, res.Findings)
		}
	}
}

func TestRecordedFindings_LatestCompletedScanWinsAndCountsScans(t *testing.T) {
	disc2 := discO1
	disc2.RecoverableValue = &client.LabeledValue{AmountUSD: client.MustParseMoney("4.51"), Classification: "observed", Confidence: "high"}
	c := &chain{}
	c.writeScan(t, scanID(1), []client.Finding{discO1, renewS1}, nil)
	c.writeScan(t, scanID(2), []client.Finding{disc2}, func(evs []client.EventInput) []client.EventInput { return evs[:2] }) // failed
	c.writeScan(t, scanID(3), []client.Finding{disc2}, nil)
	res, _ := readRecorded(t, c.json(t))
	if len(res.Findings) != 2 || len(res.Scans) != 2 || len(res.ExcludedScans) != 1 {
		t.Fatalf("findings %d scans %d excluded %d", len(res.Findings), len(res.Scans), len(res.ExcludedScans))
	}
	for _, f := range res.Findings {
		switch f.FindingID {
		case discO1.FindingID:
			if f.TimesRecorded != 2 || f.AmountUSD.String() != "4.51" || !f.AmountsDifferAcrossRecords ||
				f.ScanID != scanID(3) || !f.PresentInLatestScan || f.FirstSeq >= f.Seq {
				t.Errorf("discount row: %+v", f)
			}
		case renewS1.FindingID:
			// Not found by the latest completed scan: shown, but flagged.
			if f.TimesRecorded != 1 || f.PresentInLatestScan {
				t.Errorf("renewal row: %+v", f)
			}
		}
	}
}

func TestRecordedFindings_EmptyLedgerIsNotAnError(t *testing.T) {
	res, _ := readRecorded(t, `[]`)
	if res.Findings == nil || len(res.Findings) != 0 || res.OverlappingClaims == nil || res.ExcludedScans == nil {
		t.Fatalf("empty ledger: %+v", res)
	}
}

func TestRecordedFindings_TamperedChainIsReportedNotHidden(t *testing.T) {
	fl := &fakeLedger{entries: `[]`, verify: `{"valid":false,"entries":7,"error":"ChainBroken { at_seq: 3 }"}`, verifyCode: http.StatusConflict}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
	res, err := o.RecordedFindings(context.Background())
	if err != nil {
		t.Fatalf("a failed verify verdict must be returned, not an error: %v", err)
	}
	if res.LedgerVerify == nil || res.LedgerVerify.Valid || !strings.Contains(res.LedgerVerify.Error, "ChainBroken") {
		t.Errorf("verify verdict not surfaced: %+v", res.LedgerVerify)
	}
}

func TestRecordedFindings_LedgerErrorIsAnError(t *testing.T) {
	fl := &fakeLedger{entries: `not json`, verify: `{"valid":true,"entries":0}`}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
	if _, err := o.RecordedFindings(context.Background()); err == nil {
		t.Fatal("an unreadable ledger must be an error")
	}
}
