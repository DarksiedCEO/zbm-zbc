package main

import (
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"testing"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/edge"
)

// ADR 0023: Stripe source-IP allowlist and the client address behind Railway's edge.

func edgeEnv(extra map[string]string) func(string) string {
	m := map[string]string{"STRIPE_GATEWAY_FINANCE_URL": "http://127.0.0.1:9", "STRIPE_GATEWAY_FINANCE_TOKEN": svcTok,
		"STRIPE_GATEWAY_CALLER_TOKEN": callerTok}
	for k, v := range extra {
		m[k] = v
	}
	return func(k string) string { return m[k] }
}

func guardedGW(t *testing.T, financeURL string, extra map[string]string) *httptest.Server {
	t.Helper()
	env := map[string]string{"STRIPE_GATEWAY_FINANCE_URL": financeURL}
	for k, v := range extra {
		env[k] = v
	}
	cfg, err := loadConfig(edgeEnv(env))
	if err != nil {
		t.Fatal(err)
	}
	return httptest.NewServer(newHandler(cfg, newGateway(cfg)))
}

func postFrom(t *testing.T, url, realIP string) *http.Response {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, url+"/webhooks/stripe", strings.NewReader(`{"id":"evt_1"}`))
	req.Header.Set("Stripe-Signature", "t=1700000000,v1="+strings.Repeat("a", 64))
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

func TestEmbeddedStripeListParsesAndIsTheDocsList(t *testing.T) {
	a := edge.MustParseAllowlist(stripeWebhookIPs)
	if a.Len() != 15 {
		t.Fatalf("%d entries", a.Len())
	}
	for _, ip := range strings.Split(stripeWebhookIPs, ",") {
		if !a.Contains(netip.MustParseAddr(ip)) {
			t.Fatal(ip)
		}
	}
	if !strings.Contains(stripeWebhookIPsVersion, "docs.stripe.com/ips") || !strings.Contains(stripeWebhookIPsVersion,
		"2026-10-30") {
		t.Fatal("the list must name its source and retrieval date")
	}
}

func TestDefaultsAreAllowlistOnAndXRealIP(t *testing.T) {
	cfg, err := loadConfig(edgeEnv(nil))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.allow == nil || cfg.ipSource != edge.SourceXRealIP || !strings.Contains(cfg.allowFrom, "2026-10-30") {
		t.Fatalf("%+v", cfg)
	}
}

func TestNonStripeSourceIsRefusedBeforeFinanceIsCalled(t *testing.T) {
	var s seen
	fin := financeStub(t, 200, &s)
	defer fin.Close()
	g := guardedGW(t, fin.URL, nil)
	defer g.Close()
	for _, ip := range []string{"1.2.3.4", "3.18.12.64", "127.0.0.1", "10.0.0.1", "2001:db8::1", ""} {
		if r := postFrom(t, g.URL, ip); r.StatusCode != http.StatusForbidden {
			t.Errorf("%q: %d", ip, r.StatusCode)
		}
	}
	if s.calls.Load() != 0 {
		t.Fatalf("Finance was called %d times for refused deliveries", s.calls.Load())
	}
	for _, ip := range []string{"3.18.12.63", "54.187.216.72", "::ffff:3.120.168.93"} {
		if r := postFrom(t, g.URL, ip); r.StatusCode != http.StatusOK {
			t.Errorf("Stripe address %s: %d", ip, r.StatusCode)
		}
	}
	if s.calls.Load() != 3 {
		t.Fatalf("Finance calls: %d", s.calls.Load())
	}
}

// The leftmost X-Forwarded-For entry is client-supplied: it must never make a delivery look like Stripe's.
func TestForwardedForIsNeverTrusted(t *testing.T) {
	var s seen
	fin := financeStub(t, 200, &s)
	defer fin.Close()
	g := guardedGW(t, fin.URL, nil)
	defer g.Close()
	req, _ := http.NewRequest(http.MethodPost, g.URL+"/webhooks/stripe", strings.NewReader(`{}`))
	req.Header.Set("Stripe-Signature", "t=1,v1=x")
	req.Header.Set("X-Forwarded-For", "3.18.12.63")
	req.Header.Set("X-Envoy-External-Address", "3.18.12.63")
	req.Header.Set("Forwarded", "for=3.18.12.63")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden || s.calls.Load() != 0 {
		t.Fatalf("%d, finance calls %d", resp.StatusCode, s.calls.Load())
	}
}

func TestHealthIsOpenWithoutAClientAddress(t *testing.T) {
	g := guardedGW(t, "http://127.0.0.1:9", nil)
	defer g.Close()
	resp, err := http.Get(g.URL + "/health")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("%d", resp.StatusCode)
	}
}

func TestAllowlistOverrideReplacesTheEmbeddedList(t *testing.T) {
	var s seen
	fin := financeStub(t, 200, &s)
	defer fin.Close()
	g := guardedGW(t, fin.URL, map[string]string{allowedIPsEnv: "198.51.100.0/24, 203.0.113.9"})
	defer g.Close()
	if r := postFrom(t, g.URL, "198.51.100.77"); r.StatusCode != 200 {
		t.Fatalf("override range: %d", r.StatusCode)
	}
	if r := postFrom(t, g.URL, "3.18.12.63"); r.StatusCode != 403 {
		t.Fatalf("embedded list still in force under an override: %d", r.StatusCode)
	}
}

func TestKillSwitchTurnsTheAllowlistOff(t *testing.T) {
	var s seen
	fin := financeStub(t, 200, &s)
	defer fin.Close()
	g := guardedGW(t, fin.URL, map[string]string{allowlistDisabledEnv: "1"})
	defer g.Close()
	if r := postFrom(t, g.URL, "1.2.3.4"); r.StatusCode != 200 {
		t.Fatalf("%d", r.StatusCode)
	}
	// the address must still be determinable: the kill switch turns off the list, not the client-address check
	if r := postFrom(t, g.URL, ""); r.StatusCode != 403 {
		t.Fatalf("no address with the kill switch: %d", r.StatusCode)
	}
}

func TestRemoteAddrSourceForLocalRuns(t *testing.T) {
	var s seen
	fin := financeStub(t, 200, &s)
	defer fin.Close()
	g := guardedGW(t, fin.URL, map[string]string{clientIPSourceEnv: "remote-addr", allowedIPsEnv: "127.0.0.1"})
	defer g.Close()
	if r := postFrom(t, g.URL, "1.2.3.4"); r.StatusCode != 200 {
		t.Fatalf("loopback peer with remote-addr: %d", r.StatusCode)
	}
}

func TestAllowlistSettingsFailClosed(t *testing.T) {
	for name, env := range map[string]map[string]string{
		"kill switch typo":      {allowlistDisabledEnv: "true"},
		"kill switch yes":       {allowlistDisabledEnv: "yes"},
		"both off and override": {allowlistDisabledEnv: "1", allowedIPsEnv: "1.2.3.4"},
		"override garbage":      {allowedIPsEnv: "stripe"},
		"override allow all":    {allowedIPsEnv: "0.0.0.0/0"},
		"override trailing":     {allowedIPsEnv: "1.2.3.4,"},
		"ip source typo":        {clientIPSourceEnv: "x-forwarded-for"},
		"ip source wrong case":  {clientIPSourceEnv: "X-Real-IP"},
	} {
		if _, err := loadConfig(edgeEnv(env)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}
