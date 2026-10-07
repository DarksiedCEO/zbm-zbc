package client

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// Real entry shapes served by ledger-rust GET /ledger/entries (build
// contract section 2): findings carry "kind":"finding", events "kind":"event".
const ledgerEntriesBody = `[
 {"kind":"finding","seq":0,"finding_id":"disc-ord_1007","agent_id":"discount-misuse-v1","entity_id":"ord_1007","leak_category":"discount_misuse","amount_usd":"54.38","value_classification":"observed","decision_confidence":"very_high","recorded_at":"2026-09-24T10:00:00Z","prev_hash":"g","hash":"h0"},
 {"kind":"event","seq":1,"event_id":"onb-1","department":"onboarding","event_type":"x","actor":"a","subject_id":"s","payload_sha256":"00","summary":"s","recorded_at":"2026-09-24T10:00:01Z","prev_hash":"h0","hash":"h1"},
 {"kind":"finding","seq":2,"finding_id":"legacy-big","agent_id":"old-agent-v1","entity_id":"ord_big","leak_category":"discount_misuse","amount_usd":12.5,"value_classification":"observed","decision_confidence":"high","recorded_at":"2026-09-24T10:00:03Z","prev_hash":"h1","hash":"h2"},
 {"kind":"event","seq":3,"event_id":"rr.x","department":"revenue_recovery","event_type":"rr_finding","actor":"orchestrator_go","subject_id":"f","payload_sha256":"11","summary":"rrf1 ...","recorded_at":"2026-09-24T10:00:04Z","prev_hash":"h2","hash":"h3"}
]`

func TestLedgerClient_EntriesDecodesEventsCountsLegacyFindingsAndSendsAuthWithGET(t *testing.T) {
	var gotAuth, gotMethod, gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth, gotMethod, gotPath = r.Header.Get("Authorization"), r.Method, r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(ledgerEntriesBody))
	}))
	defer srv.Close()

	res, err := NewLedgerClient(srv.URL, "ledger-secret").Entries(context.Background())
	if err != nil {
		t.Fatalf("entries: %v", err)
	}
	if gotAuth != "Bearer ledger-secret" || gotMethod != http.MethodGet || gotPath != "/ledger/entries" {
		t.Fatalf("request was %s %s auth=%q", gotMethod, gotPath, gotAuth)
	}
	if res.Entries != 4 || res.LegacyFindings != 2 || len(res.Events) != 2 || res.LastSeq != 3 {
		t.Fatalf("want 4 entries / 2 legacy findings / 2 events / last seq 3, got %+v", res)
	}
	e := res.Events[1]
	if e.Seq != 3 || e.EventID != "rr.x" || e.Department != "revenue_recovery" || e.SubjectID != "f" ||
		e.PayloadSHA256 != "11" || e.Summary != "rrf1 ..." || e.Hash != "h3" || e.PrevHash != "h2" {
		t.Errorf("event decoded wrong: %+v", e)
	}
}

func TestLedgerClient_EntriesFailsClosedOnUnexpectedShapes(t *testing.T) {
	for name, body := range map[string]string{
		"unknown kind":     `[{"kind":"memo","seq":0}]`,
		"missing kind":     `[{"seq":0,"finding_id":"f"}]`,
		"missing seq":      `[{"kind":"event","event_id":"e"}]`,
		"event bad fields": `[{"kind":"event","seq":0,"summary":7}]`,
		"not an object":    `[1]`,
	} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte(body))
		}))
		_, err := NewLedgerClient(srv.URL, "t").Entries(context.Background())
		srv.Close()
		if err == nil {
			t.Errorf("%s: expected an error, got none", name)
		}
	}
}

func TestLedgerClient_EntriesNon2xxIsAnError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":"bad token"}`))
	}))
	defer srv.Close()
	_, err := NewLedgerClient(srv.URL, "t").Entries(context.Background())
	if err == nil || !strings.Contains(err.Error(), "401") {
		t.Fatalf("expected a 401 error, got %v", err)
	}
}

// Sweep finding (fix wave 1): Verify used to parse ANY response body as a
// verdict, so a 401 (wrong token) or 500 came back as {valid:false} with an
// empty error and was reported as "LEDGER INTEGRITY FAILURE".
func TestLedgerClient_VerifyNon200Non409IsARequestErrorNotAVerdict(t *testing.T) {
	for _, code := range []int{http.StatusUnauthorized, http.StatusNotFound, http.StatusInternalServerError} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(code)
			_, _ = w.Write([]byte(`{"error":"x"}`))
		}))
		res, err := NewLedgerClient(srv.URL, "t").Verify(context.Background())
		srv.Close()
		if err == nil {
			t.Errorf("status %d: want error, got verdict %+v", code, res)
		}
	}
}

// AEGIS D3 (fix wave 3): LedgerClient reuses DetectionClient's plumbing, and
// every ledger failure used to be labeled "detection-py" — sending whoever
// read the error to the wrong service.
func TestLedgerClient_ErrorsNameLedgerRustNotDetection(t *testing.T) {
	failing := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"error":"x"}`))
	}))
	defer failing.Close()
	dead := httptest.NewServer(http.NotFoundHandler())
	deadURL := dead.URL
	dead.Close()

	for name, base := range map[string]string{"500": failing.URL, "unreachable": deadURL} {
		l := NewLedgerClient(base, "t")
		_, errAppend := l.AppendEvent(context.Background(), sampleEvent())
		_, errEntries := l.Entries(context.Background())
		_, errVerify := l.Verify(context.Background())
		for op, err := range map[string]error{"append": errAppend, "entries": errEntries, "verify": errVerify} {
			if err == nil {
				t.Errorf("%s/%s: want an error", name, op)
				continue
			}
			if !strings.Contains(err.Error(), "ledger-rust") || strings.Contains(err.Error(), "detection-py") {
				t.Errorf("%s/%s: error must name ledger-rust only: %v", name, op, err)
			}
		}
	}
	d := NewDetectionClient(deadURL, "t")
	if _, err := d.FixtureOrders(context.Background()); err == nil || !strings.Contains(err.Error(), "detection-py") {
		t.Errorf("detection errors still name detection-py: %v", err)
	}
}
