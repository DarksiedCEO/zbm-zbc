package main

// AEGIS final go-live L-2: without ORCHESTRATOR_ALLOW_FIXTURE_SCANS=1 a scan of
// the fixture tenant (client_id omitted, or client_id=fixture-pool) is refused
// before the ledger or detection-py is called, so fixture findings cannot land
// in the production ledger through the public orchestrator domain.

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/orchestrator"
)

func gatedServer(t *testing.T) (*httptest.Server, *recorder, *recorder) {
	t.Helper()
	ledgerRec, detRec := &recorder{}, &recorder{}
	orch := orchestrator.New(noDetection(t, detRec).URL, "d", readOnlyLedger(t, ledgerRec).URL, "l")
	srv := httptest.NewServer(newMux(orch, routeTestToken, false))
	t.Cleanup(srv.Close)
	return srv, ledgerRec, detRec
}

func TestFixtureScanRefusedWithoutTheFlag(t *testing.T) {
	srv, ledgerRec, detRec := gatedServer(t)
	for _, path := range []string{
		"/revenue-recovery/scan",
		"/revenue-recovery/scan?client_id=" + orchestrator.FixtureClientID,
		"/revenue-recovery/scan?as_of=2026-10-01T00:00:00Z",
	} {
		code, body := do(t, http.MethodPost, srv.URL+path, routeTestToken)
		if code != http.StatusForbidden || !strings.Contains(body, `"reason":"FIXTURE_SCAN_DISABLED"`) {
			t.Fatalf("POST %s = %d %s, want 403 FIXTURE_SCAN_DISABLED", path, code, body)
		}
	}
	if got := ledgerRec.all(); len(got) != 0 {
		t.Fatalf("the ledger was called: %v", got)
	}
	if got := detRec.all(); len(got) != 0 {
		t.Fatalf("detection-py was called: %v", got)
	}
}

func TestFixtureGateStillNeedsTheTokenAndLetsAConnectedTenantThrough(t *testing.T) {
	srv, _, _ := gatedServer(t)
	if code, _ := do(t, http.MethodPost, srv.URL+"/revenue-recovery/scan", ""); code != http.StatusUnauthorized {
		t.Fatalf("no token = %d, want 401 (auth before the fixture gate)", code)
	}
	// A real tenant is not the fixture tenant: the gate does not refuse it. What happens next is the scan's own
	// business: here detection-py answers every call 503, so the scan reaches detection (which is the point).
	var reached atomic.Int64
	det := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached.Add(1)
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	t.Cleanup(det.Close)
	orch := orchestrator.New(det.URL, "d", readOnlyLedger(t, &recorder{}).URL, "l")
	srv2 := httptest.NewServer(newMux(orch, routeTestToken, false))
	t.Cleanup(srv2.Close)
	code, body := do(t, http.MethodPost, srv2.URL+"/revenue-recovery/scan?client_id=acme", routeTestToken)
	if reached.Load() == 0 {
		t.Fatalf("client_id=acme never reached detection-py: %d %s", code, body)
	}
	if strings.Contains(body, "FIXTURE_SCAN_DISABLED") {
		t.Fatalf("client_id=acme refused by the fixture gate: %d %s", code, body)
	}
}

func TestFixtureScansFromEnv(t *testing.T) {
	for v, want := range map[string]bool{"": false, "0": false, "1": true} {
		got, err := fixtureScansFromEnv(v)
		if err != nil || got != want {
			t.Fatalf("fixtureScansFromEnv(%q) = %v, %v; want %v", v, got, err, want)
		}
	}
	for _, v := range []string{"true", "yes", " 1", "2", "on"} {
		if _, err := fixtureScansFromEnv(v); err == nil {
			t.Fatalf("fixtureScansFromEnv(%q) accepted", v)
		}
	}
}

func TestRealBinaryRefusesFixtureScansByDefault(t *testing.T) {
	ledger, calls := countingLedger(t)
	port, _ := startOrchestrator(t, "", "LEDGER_SERVICE_URL="+ledger.URL, "DETECTION_SERVICE_URL="+deadURL(t))
	code, body := do(t, http.MethodPost, "http://127.0.0.1:"+port+"/revenue-recovery/scan", "test-orchestrator-token")
	if code != http.StatusForbidden || !strings.Contains(body, "FIXTURE_SCAN_DISABLED") {
		t.Fatalf("default binary, fixture scan = %d %s; want 403 FIXTURE_SCAN_DISABLED", code, body)
	}
	if n := calls.Load(); n != 0 {
		t.Fatalf("the ledger was called %d times", n)
	}
}

func TestRealBinaryRefusesToStartOnABadFixtureFlag(t *testing.T) {
	if port, err := tryStartOrchestrator(t, "", "ORCHESTRATOR_ALLOW_FIXTURE_SCANS=yes"); err == nil {
		t.Fatalf("started on port %s with ORCHESTRATOR_ALLOW_FIXTURE_SCANS=yes", port)
	}
}
