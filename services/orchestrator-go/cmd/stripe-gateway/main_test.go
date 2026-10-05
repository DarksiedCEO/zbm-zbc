package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	svcTok    = "finance-service-token-for-tests-0123456789"
	callerTok = "finance-rail-gateway-caller-token-abcdefghij"
)

type seen struct {
	calls   atomic.Int32
	body    map[string]string
	headers http.Header
}

func financeStub(t *testing.T, status int, s *seen) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.calls.Add(1)
		if r.URL.Path != financePath || r.Method != http.MethodPost {
			t.Errorf("unexpected upstream call %s %s", r.Method, r.URL.Path)
		}
		s.headers = r.Header.Clone()
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &s.body)
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"detail":"internal finance detail that must not leak"}`))
	}))
}

func gw(t *testing.T, financeURL string) *httptest.Server {
	t.Helper()
	cfg, err := loadConfig(func(k string) string {
		return map[string]string{"STRIPE_GATEWAY_FINANCE_URL": financeURL, "STRIPE_GATEWAY_FINANCE_TOKEN": svcTok,
			"STRIPE_GATEWAY_CALLER_TOKEN": callerTok}[k]
	})
	if err != nil {
		t.Fatal(err)
	}
	return httptest.NewServer(newMux(newGateway(cfg)))
}

func post(t *testing.T, url, body, sig string) (*http.Response, string) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, url+"/webhooks/stripe", strings.NewReader(body))
	if sig != "" {
		req.Header.Set("Stripe-Signature", sig)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	return resp, string(b)
}

func TestForwardsTheRawBodyAndSignatureUntouched(t *testing.T) {
	var s seen
	fin := financeStub(t, 200, &s)
	defer fin.Close()
	g := gw(t, fin.URL)
	defer g.Close()
	body := "{\"id\":\"evt_1\",\n  \"type\":\"payment_intent.succeeded\", \"name\":\"José\"}"
	sig := "t=1700000000,v1=" + strings.Repeat("a", 64)
	resp, out := post(t, g.URL, body, sig)
	if resp.StatusCode != 200 || !strings.Contains(out, "received") {
		t.Fatalf("got %d %s", resp.StatusCode, out)
	}
	if s.body["payload"] != body || s.body["signature"] != sig {
		t.Fatalf("body or signature changed on the way: %+v", s.body)
	}
	if !strings.HasPrefix(s.body["request_id"], "stripe-") || len(s.body["request_id"]) != 7+48 {
		t.Fatalf("request_id %q", s.body["request_id"])
	}
	if s.headers.Get("Authorization") != "Bearer "+svcTok || s.headers.Get("X-FIN-Caller-Token") != callerTok {
		t.Fatalf("finance auth headers not set")
	}
	if strings.Contains(out, "internal finance detail") {
		t.Fatalf("finance's answer leaked to the internet")
	}
}

func TestStatusMapping(t *testing.T) {
	for fin, want := range map[int]int{200: 200, 201: 200, 409: 400, 422: 400, 403: 400, 500: 503, 503: 503} {
		var s seen
		f := financeStub(t, fin, &s)
		g := gw(t, f.URL)
		resp, out := post(t, g.URL, `{"id":"evt_1"}`, "t=1,v1=00")
		if resp.StatusCode != want || strings.Contains(out, "internal finance detail") {
			t.Errorf("finance %d -> got %d, want %d (%s)", fin, resp.StatusCode, want, out)
		}
		g.Close()
		f.Close()
	}
}

func TestFinanceDownMeansTryAgain(t *testing.T) {
	f := httptest.NewServer(http.NotFoundHandler())
	url := f.URL
	f.Close()
	g := gw(t, url)
	defer g.Close()
	resp, _ := post(t, g.URL, `{"id":"evt_1"}`, "t=1,v1=00")
	if resp.StatusCode != 503 {
		t.Fatalf("got %d", resp.StatusCode)
	}
}

func TestRefusedWithoutCallingFinance(t *testing.T) {
	var s seen
	fin := financeStub(t, 200, &s)
	defer fin.Close()
	g := gw(t, fin.URL)
	defer g.Close()
	cases := []struct {
		name, body, sig string
		want            int
	}{
		{"no signature", `{"id":"evt_1"}`, "", 400},
		{"oversized signature", `{"id":"evt_1"}`, strings.Repeat("v", maxSignatureLen+1), 400},
		{"empty body", "", "t=1,v1=00", 400},
		{"not utf-8", "{\"id\":\"\xff\xfe\"}", "t=1,v1=00", 400},
		{"too large", `{"x":"` + strings.Repeat("a", maxBodyBytes) + `"}`, "t=1,v1=00", 413},
	}
	for _, c := range cases {
		resp, _ := post(t, g.URL, c.body, c.sig)
		if resp.StatusCode != c.want {
			t.Errorf("%s: got %d want %d", c.name, resp.StatusCode, c.want)
		}
	}
	if n := s.calls.Load(); n != 0 {
		t.Fatalf("finance was called %d times for refused deliveries", n)
	}
	for _, m := range []string{http.MethodGet, http.MethodPut, http.MethodDelete} {
		req, _ := http.NewRequest(m, g.URL+"/webhooks/stripe", bytes.NewReader(nil))
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusMethodNotAllowed {
			t.Errorf("%s -> %d", m, resp.StatusCode)
		}
	}
}

func TestNeverFollowsARedirectWithTheTokens(t *testing.T) {
	var hit atomic.Int32
	evil := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hit.Add(1) }))
	defer evil.Close()
	fin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, evil.URL+"/steal", http.StatusTemporaryRedirect)
	}))
	defer fin.Close()
	g := gw(t, fin.URL)
	defer g.Close()
	resp, _ := post(t, g.URL, `{"id":"evt_1"}`, "t=1,v1=00")
	if hit.Load() != 0 {
		t.Fatalf("followed a redirect with Finance's tokens")
	}
	if resp.StatusCode != 400 {
		t.Fatalf("a 3xx from finance should not count as handled: %d", resp.StatusCode)
	}
}

func TestConfigFailsClosed(t *testing.T) {
	ok := map[string]string{"STRIPE_GATEWAY_FINANCE_URL": "http://127.0.0.1:8410", "STRIPE_GATEWAY_FINANCE_TOKEN": svcTok,
		"STRIPE_GATEWAY_CALLER_TOKEN": callerTok}
	c, err := loadConfig(func(k string) string { return ok[k] })
	if err != nil || c.addr != net.JoinHostPort("127.0.0.1", defaultPort) {
		t.Fatalf("defaults: %v %q", err, c.addr)
	}
	for name, over := range map[string]map[string]string{
		"no url":      {"STRIPE_GATEWAY_FINANCE_URL": ""},
		"bad scheme":  {"STRIPE_GATEWAY_FINANCE_URL": "ftp://x"},
		"short token": {"STRIPE_GATEWAY_FINANCE_TOKEN": "short"},
		"no caller":   {"STRIPE_GATEWAY_CALLER_TOKEN": ""},
		"same tokens": {"STRIPE_GATEWAY_CALLER_TOKEN": svcTok},
	} {
		env := map[string]string{}
		for k, v := range ok {
			env[k] = v
		}
		for k, v := range over {
			env[k] = v
		}
		if _, err := loadConfig(func(k string) string { return env[k] }); err == nil {
			t.Errorf("%s: started", name)
		}
	}
}

func TestServerLimits(t *testing.T) {
	s := newServer("127.0.0.1:0", http.NotFoundHandler())
	if s.ReadHeaderTimeout != 5*time.Second || s.ReadTimeout == 0 || s.WriteTimeout <= upstreamTimeout ||
		s.MaxHeaderBytes != maxHeaderBytes {
		t.Fatalf("limits not set: %+v", s)
	}
}
