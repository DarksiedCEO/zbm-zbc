package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

// AEGIS edge review H-1 / M-1, kept as probes: nothing an unauthenticated client does may lock out the valid token.
// They use only the default settings and the server the binary builds (guardedOrchestrator).

func serve(h http.Handler, method, url, token, realIP string) int {
	r := httptest.NewRequest(method, url, nil)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	r.Header.Set("X-Real-IP", realIP)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w.Code
}

// H-1: one bad-token request from each of 16,384 /64s inside one IPv6 /48 fills both tables; afterwards a NEW client
// with the correct token is served, and a new anonymous client is not refused for a full table.
func TestProbeIPv6SweepDoesNotLockOutTheValidToken(t *testing.T) {
	srv, _ := guardedOrchestrator(t, nil)
	h := srv.Config.Handler
	url := "http://orch/revenue-recovery/findings"
	for i := 0; i < 16384; i++ {
		ip := fmt.Sprintf("2001:db8:1:%x::1", i)
		if c := serve(h, "GET", url, "wrong-token", ip); c != http.StatusUnauthorized {
			t.Fatalf("sweep request %d from %s: %d", i, ip, c)
		}
	}
	if c := serve(h, "GET", url, routeTestToken, "203.0.113.77"); c == http.StatusTooManyRequests ||
		c == http.StatusUnauthorized || c == http.StatusForbidden {
		t.Fatalf("a new client with the valid token after the sweep: %d", c)
	}
	if c := serve(h, "GET", url, "wrong-token", "203.0.113.78"); c != http.StatusUnauthorized {
		t.Fatalf("a new anonymous client after the sweep: %d (want 401, never a table-full refusal)", c)
	}
}

// M-1: 10 bad tokens from the dashboard's egress address (a shared address) must not block the correct token there.
func TestProbeBadTokensFromTheDashboardsAddressDoNotBlockTheValidToken(t *testing.T) {
	srv, _ := guardedOrchestrator(t, nil)
	h := srv.Config.Handler
	url := "http://orch/revenue-recovery/findings"
	const dashboard = "44.200.1.1"
	for i := 0; i < 10; i++ {
		serve(h, "GET", url, "wrong-token", dashboard)
	}
	for i := 0; i < 5; i++ {
		if c := serve(h, "GET", url, routeTestToken, dashboard); c == http.StatusTooManyRequests ||
			c == http.StatusUnauthorized {
			t.Fatalf("valid token from the dashboard's address after 10 bad tokens (request %d): %d", i, c)
		}
	}
}
