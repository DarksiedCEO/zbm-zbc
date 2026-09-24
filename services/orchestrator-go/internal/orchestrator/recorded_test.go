package orchestrator

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
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

const twoScansOfEntries = `[
 {"kind":"finding","seq":0,"finding_id":"aff-ord_1007","agent_id":"affiliate-coupon-extension-v1","entity_id":"ord_1007","leak_category":"affiliate_coupon_extension","amount_usd":"150.00","value_classification":"attributed","decision_confidence":"high","recorded_at":"t0","prev_hash":"g","hash":"h0"},
 {"kind":"finding","seq":1,"finding_id":"disc-ord_1007","agent_id":"discount-misuse-v1","entity_id":"ord_1007","leak_category":"discount_misuse","amount_usd":"54.38","value_classification":"observed","decision_confidence":"very_high","recorded_at":"t1","prev_hash":"h0","hash":"h1"},
 {"kind":"event","seq":2,"event_id":"e","department":"onboarding","event_type":"x","actor":"a","subject_id":"s","payload_sha256":"00","summary":"s","recorded_at":"t2","prev_hash":"h1","hash":"h2"},
 {"kind":"finding","seq":3,"finding_id":"aff-ord_1007","agent_id":"affiliate-coupon-extension-v1","entity_id":"ord_1007","leak_category":"affiliate_coupon_extension","amount_usd":"150.00","value_classification":"attributed","decision_confidence":"high","recorded_at":"t3","prev_hash":"h2","hash":"h3"},
 {"kind":"finding","seq":4,"finding_id":"disc-ord_1007","agent_id":"discount-misuse-v1","entity_id":"ord_1007","leak_category":"discount_misuse","amount_usd":"54.39","value_classification":"observed","decision_confidence":"very_high","recorded_at":"t4","prev_hash":"h3","hash":"h4"},
 {"kind":"finding","seq":5,"finding_id":"renew-sub_1","agent_id":"renewal-never-triggered-v1","entity_id":"sub_1","leak_category":"renewal_never_triggered","amount_usd":"39.00","value_classification":"observed","decision_confidence":"high","recorded_at":"t5","prev_hash":"h4","hash":"h5"},
 {"kind":"finding","seq":6,"finding_id":"renew-sub_1","agent_id":"renewal-never-triggered-v1","entity_id":"sub_1","leak_category":"renewal_never_triggered","amount_usd":"39.00","value_classification":"observed","decision_confidence":"high","recorded_at":"t6","prev_hash":"h5","hash":"h6"}
]`

func TestRecordedFindings_ReadsLedgerOnlyAndNeverWritesOrScans(t *testing.T) {
	fl := &fakeLedger{entries: twoScansOfEntries, verify: `{"valid":true,"entries":7}`}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")

	res, err := o.RecordedFindings(context.Background())
	if err != nil {
		t.Fatalf("recorded findings: %v", err)
	}
	for _, req := range fl.requests {
		if req != "GET /ledger/entries" && req != "GET /ledger/verify" {
			t.Errorf("unexpected ledger request %q", req)
		}
	}
	if res.LedgerEntriesTotal != 7 || res.FindingEntriesTotal != 6 {
		t.Errorf("totals: entries %d findings %d", res.LedgerEntriesTotal, res.FindingEntriesTotal)
	}
	if res.LedgerVerify == nil || !res.LedgerVerify.Valid || res.LedgerVerify.Entries != 7 {
		t.Errorf("verify not carried through: %+v", res.LedgerVerify)
	}
	if !res.NonLiveDataSource {
		t.Error("non_live_data_source must be true (fixture-only pipeline)")
	}
}

func TestRecordedFindings_OneRowPerFindingIDLatestRecordWins(t *testing.T) {
	fl := &fakeLedger{entries: twoScansOfEntries, verify: `{"valid":true,"entries":7}`}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
	res, err := o.RecordedFindings(context.Background())
	if err != nil {
		t.Fatalf("recorded findings: %v", err)
	}
	if len(res.Findings) != 3 {
		t.Fatalf("want 3 distinct findings, got %d: %+v", len(res.Findings), res.Findings)
	}
	want := []struct {
		id      string
		seq     uint64
		first   uint64
		times   int
		amount  string
		differs bool
	}{
		{"aff-ord_1007", 3, 0, 2, "150.00", false},
		{"disc-ord_1007", 4, 1, 2, "54.39", true},
		{"renew-sub_1", 6, 5, 2, "39.00", false},
	}
	for i, w := range want {
		f := res.Findings[i]
		if f.FindingID != w.id || f.Seq != w.seq || f.FirstSeq != w.first || f.TimesRecorded != w.times ||
			f.AmountUSD == nil || f.AmountUSD.String() != w.amount || f.AmountsDifferAcrossRecords != w.differs {
			t.Errorf("row %d = %+v, want %+v", i, f, w)
		}
	}
}

func TestRecordedFindings_OverlapsAreDistinctAgentsOnOneEntityNotRepeatedScans(t *testing.T) {
	fl := &fakeLedger{entries: twoScansOfEntries, verify: `{"valid":true,"entries":7}`}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
	res, err := o.RecordedFindings(context.Background())
	if err != nil {
		t.Fatalf("recorded findings: %v", err)
	}
	if len(res.OverlappingClaims) != 1 || len(res.OverlappingClaims["ord_1007"]) != 2 {
		t.Fatalf("want exactly ord_1007 with 2 claims, got %+v", res.OverlappingClaims)
	}
	if _, ok := res.OverlappingClaims["sub_1"]; ok {
		t.Error("sub_1 was recorded twice by ONE agent (two scans) — that is not an overlapping claim")
	}
}

func TestRecordedFindings_EmptyLedgerIsNotAnError(t *testing.T) {
	fl := &fakeLedger{entries: `[]`, verify: `{"valid":false,"error":"Empty"}`, verifyCode: http.StatusConflict}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
	res, err := o.RecordedFindings(context.Background())
	if err != nil {
		t.Fatalf("empty ledger: %v", err)
	}
	if len(res.Findings) != 0 || res.Findings == nil || res.OverlappingClaims == nil || res.LedgerEntriesTotal != 0 {
		t.Errorf("empty result should be empty (non-nil) collections: %+v", res)
	}
}

func TestRecordedFindings_TamperedChainIsReportedNotHidden(t *testing.T) {
	fl := &fakeLedger{entries: twoScansOfEntries, verify: `{"valid":false,"error":"HashMismatch { seq: 3 }"}`, verifyCode: http.StatusConflict}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
	res, err := o.RecordedFindings(context.Background())
	if err != nil {
		t.Fatalf("recorded findings: %v", err)
	}
	if res.LedgerVerify == nil || res.LedgerVerify.Valid || res.LedgerVerify.Error == "" {
		t.Fatalf("tamper verdict must be carried to the caller: %+v", res.LedgerVerify)
	}
}

func TestRecordedFindings_LedgerErrorIsAnError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()
	o := New(failingDetection(t).URL, "d", srv.URL, "l")
	if _, err := o.RecordedFindings(context.Background()); err == nil {
		t.Fatal("expected an error when the ledger refuses the read")
	}
}
