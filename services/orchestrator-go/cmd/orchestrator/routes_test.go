package main

// Fix wave 1 (Sep 24 2026): every dashboard page view used to call the
// scan route, which runs all agents and appends every finding to the
// evidence ledger — one GET / took a live ledger from 219 to 229 entries.
// Viewing must not write. GET /revenue-recovery/findings reads what the
// ledger already recorded; a scan is only ever an explicit POST.

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/orchestrator"
)

const routeTestToken = "orch-route-test-token"

type recorder struct {
	mu   sync.Mutex
	reqs []string
}

func (r *recorder) add(s string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.reqs = append(r.reqs, s)
}

func (r *recorder) all() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.reqs...)
}

// A fake ledger that FAILS THE TEST if anything calls a write endpoint
// (POST /ledger/append, POST /ledger/events) or uses any method but GET.
func readOnlyLedger(t *testing.T, rec *recorder) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec.add(r.Method + " " + r.URL.Path)
		if r.Method != http.MethodGet || r.URL.Path == "/ledger/append" || r.URL.Path == "/ledger/events" {
			t.Errorf("ledger WRITE attempted by a read route: %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusTeapot)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/ledger/entries":
			_, _ = w.Write([]byte(`[{"kind":"finding","seq":0,"finding_id":"disc-ord_1007","agent_id":"discount-misuse-v1","entity_id":"ord_1007","leak_category":"discount_misuse","amount_usd":"54.38","value_classification":"observed","decision_confidence":"very_high","recorded_at":"2026-09-24T10:00:00Z","prev_hash":"g","hash":"h0"}]`))
		case "/ledger/verify":
			_, _ = w.Write([]byte(`{"valid":true,"entries":1}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func noDetection(t *testing.T, rec *recorder) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec.add(r.Method + " " + r.URL.Path)
		t.Errorf("detection-py called (a scan was run): %s %s", r.Method, r.URL.Path)
		w.WriteHeader(http.StatusTeapot)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func newTestServer(t *testing.T) (*httptest.Server, *recorder, *recorder) {
	t.Helper()
	ledgerRec, detRec := &recorder{}, &recorder{}
	orch := orchestrator.New(noDetection(t, detRec).URL, "d", readOnlyLedger(t, ledgerRec).URL, "l")
	srv := httptest.NewServer(newMux(orch, routeTestToken))
	t.Cleanup(srv.Close)
	return srv, ledgerRec, detRec
}

func do(t *testing.T, method, url, token string) (int, string) {
	t.Helper()
	req, _ := http.NewRequest(method, url, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, url, err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

func TestFindingsRoute_ReturnsRecordedFindingsAndNeverWrites(t *testing.T) {
	srv, ledgerRec, _ := newTestServer(t)
	for i := 0; i < 3; i++ {
		code, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings", routeTestToken)
		if code != http.StatusOK {
			t.Fatalf("GET findings = %d: %s", code, body)
		}
		for _, want := range []string{`"finding_id":"disc-ord_1007"`, `"amount_usd":"54.38"`, `"ledger_verify":{"valid":true`, `"times_recorded":1`} {
			if !strings.Contains(body, want) {
				t.Errorf("response missing %s: %s", want, body)
			}
		}
	}
	for _, r := range ledgerRec.all() {
		if r != "GET /ledger/entries" && r != "GET /ledger/verify" {
			t.Errorf("unexpected ledger call %q", r)
		}
	}
}

func TestFindingsRoute_RequiresAuth(t *testing.T) {
	srv, ledgerRec, _ := newTestServer(t)
	for _, tok := range []string{"", "wrong"} {
		code, _ := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings", tok)
		if code != http.StatusUnauthorized {
			t.Errorf("token %q: want 401, got %d", tok, code)
		}
	}
	if n := len(ledgerRec.all()); n != 0 {
		t.Errorf("unauthenticated request reached the ledger (%d calls)", n)
	}
}

func TestFindingsRoute_OnlyGET(t *testing.T) {
	srv, ledgerRec, _ := newTestServer(t)
	for _, m := range []string{http.MethodPost, http.MethodPut, http.MethodDelete, http.MethodPatch} {
		code, _ := do(t, m, srv.URL+"/revenue-recovery/findings", routeTestToken)
		if code != http.StatusMethodNotAllowed {
			t.Errorf("%s findings: want 405, got %d", m, code)
		}
	}
	if n := len(ledgerRec.all()); n != 0 {
		t.Errorf("rejected methods reached the ledger (%d calls)", n)
	}
}

// Viewing must never trigger a scan: the scan route only answers POST, so
// a GET (what the dashboard used to send on every page view) runs nothing.
func TestScanRoute_GETDoesNotScanOrWrite(t *testing.T) {
	srv, ledgerRec, detRec := newTestServer(t)
	for _, m := range []string{http.MethodGet, http.MethodHead, http.MethodPut, http.MethodDelete} {
		code, _ := do(t, m, srv.URL+"/revenue-recovery/scan", routeTestToken)
		if code != http.StatusMethodNotAllowed {
			t.Errorf("%s scan: want 405, got %d", m, code)
		}
	}
	if len(ledgerRec.all()) != 0 || len(detRec.all()) != 0 {
		t.Errorf("non-POST scan reached upstream: ledger %v detection %v", ledgerRec.all(), detRec.all())
	}
}

func TestScanRoute_POSTStillRequiresAuth(t *testing.T) {
	srv, _, detRec := newTestServer(t)
	code, _ := do(t, http.MethodPost, srv.URL+"/revenue-recovery/scan", "")
	if code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated POST scan: want 401, got %d", code)
	}
	if len(detRec.all()) != 0 {
		t.Fatal("unauthenticated scan reached detection-py")
	}
}

func TestHealthStaysOpen(t *testing.T) {
	srv, _, _ := newTestServer(t)
	if code, _ := do(t, http.MethodGet, srv.URL+"/health", ""); code != http.StatusOK {
		t.Fatalf("health: %d", code)
	}
}
