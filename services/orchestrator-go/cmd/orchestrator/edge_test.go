package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/edge"
	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/orchestrator"
)

// ADR 0023: rate limit and failed-authentication limit on the public orchestrator.

func envOf(m map[string]string) func(string) string { return func(k string) string { return m[k] } }

func guardedOrchestrator(t *testing.T, env map[string]string) (*httptest.Server, *recorder) {
	t.Helper()
	g, err := loadGuard(envOf(env))
	if err != nil {
		t.Fatal(err)
	}
	ledgerRec, detRec := &recorder{}, &recorder{}
	orch := orchestrator.New(noDetection(t, detRec).URL, "d", readOnlyLedger(t, ledgerRec).URL, "l")
	srv := guardServer(newServer("127.0.0.1:0", newMux(orch, routeTestToken, false)), g)
	ts := httptest.NewServer(srv.Handler)
	t.Cleanup(ts.Close)
	return ts, ledgerRec
}

func call(t *testing.T, method, url, token, realIP string) *http.Response {
	t.Helper()
	req, _ := http.NewRequest(method, url, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if realIP != "" {
		req.Header.Set("X-Real-IP", realIP)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	return resp
}

func TestGuardDefaults(t *testing.T) {
	g, err := loadGuard(envOf(nil))
	if err != nil {
		t.Fatal(err)
	}
	if g.Source != edge.SourceXRealIP || g.Limit == nil || g.AuthFail == nil || g.Allow != nil {
		t.Fatalf("%+v", g)
	}
}

func TestGuardSettingsFailClosed(t *testing.T) {
	for _, env := range []map[string]string{
		{clientIPSourceEnv: "xff"}, {ratePerMinEnv: "0"}, {ratePerMinEnv: "lots"}, {rateBurstEnv: "-1"},
		{authFailMaxEnv: "0"}, {authFailWindowEnv: "10m"}, {authFailWindowEnv: "999999"},
	} {
		if _, err := loadGuard(envOf(env)); err == nil {
			t.Errorf("%v accepted", env)
		}
	}
}

func TestTenFailedAuthsThen429EvenWithTheRightToken(t *testing.T) {
	srv, _ := guardedOrchestrator(t, nil)
	url := srv.URL + "/revenue-recovery/findings"
	for i := 0; i < 10; i++ {
		if r := call(t, "GET", url, "wrong-token", "198.51.100.4"); r.StatusCode != http.StatusUnauthorized {
			t.Fatalf("attempt %d: %d", i, r.StatusCode)
		}
	}
	r := call(t, "GET", url, routeTestToken, "198.51.100.4")
	if r.StatusCode != http.StatusTooManyRequests || r.Header.Get("Retry-After") != "600" {
		t.Fatalf("11th from the same client: %d Retry-After %q", r.StatusCode, r.Header.Get("Retry-After"))
	}
	// a missing Authorization header counts too (it is also a 401)
	if r := call(t, "GET", url, "", "198.51.100.4"); r.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("%d", r.StatusCode)
	}
	// another client is not affected
	if r := call(t, "GET", url, routeTestToken, "198.51.100.5"); r.StatusCode == http.StatusTooManyRequests ||
		r.StatusCode == http.StatusUnauthorized {
		t.Fatalf("other client: %d", r.StatusCode)
	}
}

func TestPerClientRateLimit(t *testing.T) {
	srv, _ := guardedOrchestrator(t, map[string]string{ratePerMinEnv: "30", rateBurstEnv: "3"})
	for i := 0; i < 3; i++ {
		if r := call(t, "GET", srv.URL+"/health", "", "192.0.2.10"); r.StatusCode != 200 {
			t.Fatalf("%d: %d", i, r.StatusCode)
		}
	}
	r := call(t, "POST", srv.URL+"/revenue-recovery/scan?client_id=acme", routeTestToken, "192.0.2.10")
	if r.StatusCode != http.StatusTooManyRequests || r.Header.Get("Retry-After") != "2" {
		t.Fatalf("%d %q", r.StatusCode, r.Header.Get("Retry-After"))
	}
	if r := call(t, "GET", srv.URL+"/health", "", "192.0.2.11"); r.StatusCode != 200 {
		t.Fatal("another client was limited")
	}
}

func TestNoClientAddressIs403ExceptHealth(t *testing.T) {
	srv, ledger := guardedOrchestrator(t, nil)
	if r := call(t, "GET", srv.URL+"/revenue-recovery/findings", routeTestToken, ""); r.StatusCode != http.StatusForbidden {
		t.Fatalf("%d", r.StatusCode)
	}
	if r := call(t, "GET", srv.URL+"/health", "", ""); r.StatusCode != 200 {
		t.Fatalf("health: %d", r.StatusCode)
	}
	if n := len(ledger.all()); n != 0 {
		t.Fatalf("the ledger was called %d times for a refused request", n)
	}
}

// The guard runs before limitRequests: a refused client's body is never read.
func TestGuardRunsBeforeTheBodyIsRead(t *testing.T) {
	srv, _ := guardedOrchestrator(t, nil)
	req, _ := http.NewRequest("POST", srv.URL+"/revenue-recovery/scan", strings.NewReader(strings.Repeat("x", 1<<20)))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("%d (want 403 from the guard, not 413 from the body limit)", resp.StatusCode)
	}
}

// The real binary, with the production default (no ORCHESTRATOR_CLIENT_IP_SOURCE): a request without Railway's
// X-Real-IP is refused before routing; /health still answers (Railway's health check has no X-Real-IP).
func TestRealBinaryDefaultRefusesWithoutXRealIP(t *testing.T) {
	ledger := httptest.NewServer(http.NotFoundHandler())
	defer ledger.Close()
	port, _ := startOrchestrator(t, "", "LEDGER_SERVICE_URL="+ledger.URL, "DETECTION_SERVICE_URL="+deadURL(t),
		"ORCHESTRATOR_CLIENT_IP_SOURCE=")
	base := "http://127.0.0.1:" + port
	if r := call(t, "GET", base+"/revenue-recovery/findings", "test-orchestrator-token", ""); r.StatusCode != 403 {
		t.Fatalf("no X-Real-IP: %d", r.StatusCode)
	}
	if r := call(t, "GET", base+"/health", "", ""); r.StatusCode != 200 {
		t.Fatalf("health: %d", r.StatusCode)
	}
	if r := call(t, "GET", base+"/revenue-recovery/findings", "wrong", "198.51.100.7"); r.StatusCode != 401 {
		t.Fatalf("with X-Real-IP: %d", r.StatusCode)
	}
}
