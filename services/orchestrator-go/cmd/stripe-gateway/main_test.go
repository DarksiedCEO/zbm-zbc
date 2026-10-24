package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
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
		"no url":       {"STRIPE_GATEWAY_FINANCE_URL": ""},
		"bad scheme":   {"STRIPE_GATEWAY_FINANCE_URL": "ftp://x"},
		"short token":  {"STRIPE_GATEWAY_FINANCE_TOKEN": "short"},
		"no caller":    {"STRIPE_GATEWAY_CALLER_TOKEN": ""},
		"same tokens":  {"STRIPE_GATEWAY_CALLER_TOKEN": svcTok},
		"http off-box": {"STRIPE_GATEWAY_FINANCE_URL": "http://finance.internal"},
		"no host":      {"STRIPE_GATEWAY_FINANCE_URL": "https://"},
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

// Go-live Days 1-2: on Railway the gateway reaches Finance at http://finance.railway.internal (private network,
// WireGuard-encrypted, not routed from the internet). Plain http there needs the explicit opt-in and is limited to
// the ONE service named by STRIPE_GATEWAY_FINANCE_PRIVATE_NAME (default "finance", AEGIS L1); every other
// plain-http host, including other services on the same private network, is still refused.
func loadPrivate(url, optIn, name string) (config, error) {
	env := map[string]string{"STRIPE_GATEWAY_FINANCE_URL": url, allowRailwayPrivateHTTPEnv: optIn,
		financePrivateNameEnv: name, "STRIPE_GATEWAY_FINANCE_TOKEN": svcTok, "STRIPE_GATEWAY_CALLER_TOKEN": callerTok}
	return loadConfig(func(k string) string { return env[k] })
}

func TestRailwayPrivateHTTPOptIn(t *testing.T) {
	if _, err := loadPrivate("http://finance.railway.internal:8410", "", ""); err == nil {
		t.Fatal("plain http to a private-network host started without the opt-in")
	}
	for _, u := range []string{"http://finance.railway.internal:8410", "http://FINANCE.railway.internal",
		"http://finance.railway.internal:8410/"} {
		c, err := loadPrivate(u, "1", "")
		if err != nil || !c.privateHTTP {
			t.Fatalf("%s with the opt-in: %v (privateHTTP=%v)", u, err, c.privateHTTP)
		}
	}
	for _, u := range []string{"http://finance.internal", "http://railway.internal", "http://.railway.internal",
		"http://a.b.railway.internal", "http://finance.railway.internal.evil.example", "http://10.0.0.5:8410",
		"http://[fd12::1]:8410", "http://-x.railway.internal", "http://x_y.railway.internal",
		"http://finance-31.railway.internal:8410", "http://finance.railway.internal:8410/fin",
		"http://finance.railway.internal:8410?x=1", "http://finance.railway.internal:8410#f",
		"http://finance.railway.internal:8410/?", "http://finance.railway.internal.:8410"} {
		if _, err := loadPrivate(u, "1", ""); err == nil {
			t.Errorf("%s started with the opt-in: only http://finance.railway.internal[:port] is accepted", u)
		}
	}
	for _, v := range []string{"true", "yes", "2", " 1"} {
		if _, err := loadPrivate("http://finance.railway.internal", v, ""); err == nil {
			t.Errorf("opt-in value %q started (only 1 or 0/unset)", v)
		}
	}
	if c, err := loadPrivate("http://127.0.0.1:8410", "1", ""); err != nil || c.privateHTTP {
		t.Fatalf("loopback with the opt-in: %v %v", err, c.privateHTTP)
	}
	if c, err := loadPrivate("https://finance.example", "0", ""); err != nil || c.privateHTTP {
		t.Fatalf("https with the opt-in off: %v %v", err, c.privateHTTP)
	}
}

// AEGIS L1: the opt-in reaches one named host, not any service on the private network.
func TestRailwayPrivateHTTPIsPinnedToTheFinanceName(t *testing.T) {
	for _, u := range []string{"http://ledger.railway.internal:8090", "http://localhost.railway.internal:8410",
		"http://legal.railway.internal:8420", "http://seo.railway.internal"} {
		if _, err := loadPrivate(u, "1", ""); err == nil {
			t.Errorf("%s started with the opt-in and the default name finance", u)
		}
	}
	// a configured name is the only one accepted
	if c, err := loadPrivate("http://finance-31.railway.internal:8410", "1", "finance-31"); err != nil || !c.privateHTTP {
		t.Fatalf("the configured name was refused: %v (privateHTTP=%v)", err, c.privateHTTP)
	}
	if _, err := loadPrivate("http://finance.railway.internal:8410", "1", "finance-31"); err == nil {
		t.Error("the default name was accepted although another name is configured")
	}
	if _, err := loadPrivate("http://ledger.railway.internal:8090", "1", "finance-31"); err == nil {
		t.Error("ledger accepted with a configured Finance name")
	}
	for _, name := range []string{"localhost", "Finance", "finance.railway.internal", "a.b", "-x", "x-", "x_y",
		" finance", strings.Repeat("a", 64)} {
		if _, err := loadPrivate("http://finance.railway.internal", "1", name); err == nil {
			t.Errorf("%s=%q accepted: must be one lowercase DNS label, not localhost", financePrivateNameEnv, name)
		}
	}
}

// AEGIS L4: a Finance URL with userinfo is refused in every mode, and no credential reaches the error or the log.
func TestFinanceURLWithUserinfoIsRefused(t *testing.T) {
	for _, tc := range []struct{ url, optIn string }{
		{"http://user:hunter2-secret@127.0.0.1:8410", ""},
		{"http://user@localhost:8410", ""},
		{"http://@127.0.0.1:8410", ""},
		{"http://user:hunter2-secret@finance.railway.internal:8410", "1"},
		{"http://user@finance.railway.internal", "1"},
		{"https://user:hunter2-secret@finance.example", ""},
	} {
		_, err := loadPrivate(tc.url, tc.optIn, "")
		if err == nil {
			t.Errorf("%s (opt-in %q) started: userinfo must be refused", tc.url, tc.optIn)
			continue
		}
		if strings.Contains(err.Error(), "hunter2") || strings.Contains(err.Error(), "user:") {
			t.Errorf("the refusal leaks the credential: %v", err)
		}
	}
	if got := redactedURL("http://user:hunter2-secret@127.0.0.1:8410"); strings.Contains(got, "hunter2") ||
		strings.Contains(got, "user") {
		t.Errorf("redactedURL kept the userinfo: %s", got)
	}
	if got := redactedURL("http://finance.railway.internal:8410"); got != "http://finance.railway.internal:8410" {
		t.Errorf("redactedURL changed a clean URL: %s", got)
	}
}

// AEGIS R2-1: a non-ASCII host is refused before any case folding, in every mode. strings.ToLower folds U+0130 'İ'
// to 'i' and U+212A (Kelvin sign) to 'k', so these would otherwise match the pinned name (or "localhost") while the
// client dials a different, punycode host.
func TestNonASCIIHostIsRefused(t *testing.T) {
	for _, tc := range []struct{ url, optIn, name string }{
		{"http://F\u0130NANCE.railway.internal:8410", "1", ""},    // U+0130 -> "finance"
		{"http://f\u0130nance.railway.internal", "1", ""},         // lowercase context too
		{"http://F%C4%B0NANCE.railway.internal:8410", "1", ""},    // the same, percent-encoded
		{"http://\u212Aafka.railway.internal:8410", "1", "kafka"}, // U+212A -> "kafka", a configured name
		{"http://LOCALHOST\u212A:8410", "", ""},                   // never mistaken for a loopback name
		{"http://loc\u0130lhost:8410", "", ""},                    // nor "localhost" via U+0130
		{"https://f\u0130nance.example", "", ""},                  // https too
	} {
		if _, err := loadPrivate(tc.url, tc.optIn, tc.name); err == nil {
			t.Errorf("%q (opt-in %q, name %q) started: a non-ASCII host must be refused", tc.url, tc.optIn, tc.name)
		}
	}
	// the ASCII spellings still work
	if _, err := loadPrivate("http://kafka.railway.internal:8410", "1", "kafka"); err != nil {
		t.Fatalf("ASCII configured name refused: %v", err)
	}
	if _, err := loadPrivate("http://FINANCE.railway.internal:8410", "1", ""); err != nil {
		t.Fatalf("ASCII upper-case name refused: %v", err)
	}
}

// AEGIS R2-2: no '?' or '#' in the base URL, even empty ones, in any mode; and the delivery URL is built with
// url.JoinPath, so the Finance path can never land in a fragment or query.
func TestQueryOrFragmentInTheBaseURLIsRefused(t *testing.T) {
	for _, tc := range []struct{ url, optIn string }{
		{"http://finance.railway.internal:8410#", "1"}, {"http://finance.railway.internal:8410?", "1"},
		{"http://finance.railway.internal:8410/#", "1"}, {"http://finance.railway.internal:8410#x", "1"},
		{"http://127.0.0.1:8410#", ""}, {"http://127.0.0.1:8410?", ""}, {"http://localhost:8410?a=b", ""},
		{"https://finance.example#", ""}, {"https://finance.example/base?x=1", ""},
	} {
		if _, err := loadPrivate(tc.url, tc.optIn, ""); err == nil {
			t.Errorf("%s (opt-in %q) started: '?' and '#' must be refused", tc.url, tc.optIn)
		}
	}
	for base, want := range map[string]string{
		"http://finance.railway.internal:8410":  "http://finance.railway.internal:8410" + financePath,
		"http://finance.railway.internal:8410/": "http://finance.railway.internal:8410" + financePath,
		"http://127.0.0.1:8410":                 "http://127.0.0.1:8410" + financePath,
		"https://finance.example/base/":         "https://finance.example/base" + financePath,
	} {
		optIn := ""
		if strings.Contains(base, "railway") {
			optIn = "1"
		}
		c, err := loadPrivate(base, optIn, "")
		if err != nil || c.financeEndpoint != want {
			t.Errorf("%s: endpoint %q (%v), want %q", base, c.financeEndpoint, err, want)
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

func TestHTTPSOffBoxAndNoProxy(t *testing.T) {
	for _, u := range []string{"https://finance.internal", "http://localhost", "http://[::1]:9"} {
		env := map[string]string{"STRIPE_GATEWAY_FINANCE_URL": u, "STRIPE_GATEWAY_FINANCE_TOKEN": svcTok,
			"STRIPE_GATEWAY_CALLER_TOKEN": callerTok}
		c, err := loadConfig(func(k string) string { return env[k] })
		if err != nil {
			t.Fatalf("%s refused: %v", u, err)
		}
		if tr, ok := newGateway(c).client.Transport.(*http.Transport); !ok || tr.Proxy != nil {
			t.Fatalf("the Finance client must not use an HTTP proxy")
		}
	}
}

func TestEscapedBodyOverFinanceLimitIsRefused(t *testing.T) {
	var s seen
	fin := financeStub(t, 200, &s)
	defer fin.Close()
	g := gw(t, fin.URL)
	defer g.Close()
	// ~200 KiB of escaped quotes: under the 256 KiB body cap, about 400 KiB once JSON-escaped
	resp, _ := post(t, g.URL, `{"x":"`+strings.Repeat(`\"`, 100<<10)+`"}`, "t=1,v1=00")
	if resp.StatusCode != http.StatusRequestEntityTooLarge || s.calls.Load() != 0 {
		t.Fatalf("got %d, finance calls %d", resp.StatusCode, s.calls.Load())
	}
}

func TestDeliveriesBeyondTheCapAreTryAgain(t *testing.T) {
	release := make(chan struct{})
	var once sync.Once
	free := func() { once.Do(func() { close(release) }) }
	defer free() // never leave a held request blocking the test binary
	var inFinance atomic.Int32
	fin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		inFinance.Add(1)
		<-release
	}))
	defer fin.Close()
	g := gw(t, fin.URL)
	defer g.Close()
	send := func(id string) (int, error) { // goroutine-safe: no t.Fatal off the test goroutine
		req, _ := http.NewRequest(http.MethodPost, g.URL+"/webhooks/stripe", strings.NewReader(`{"id":"`+id+`"}`))
		req.Header.Set("Stripe-Signature", "t=1,v1=00")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return 0, err
		}
		resp.Body.Close()
		return resp.StatusCode, nil
	}
	type result struct {
		code int
		err  error
	}
	done := make(chan result, maxInFlight)
	for i := 0; i < maxInFlight; i++ {
		go func() {
			c, err := send("evt_held")
			done <- result{c, err}
		}()
	}
	deadline := time.Now().Add(10 * time.Second)
	for inFinance.Load() < maxInFlight {
		if time.Now().After(deadline) {
			free()
			t.Fatalf("only %d of %d deliveries reached Finance in time", inFinance.Load(), maxInFlight)
		}
		time.Sleep(10 * time.Millisecond)
	}
	code, err := send("evt_over_cap")
	if err != nil || code != http.StatusServiceUnavailable {
		free()
		t.Fatalf("delivery beyond the cap: got %d (%v)", code, err)
	}
	free()
	for i := 0; i < maxInFlight; i++ {
		if r := <-done; r.err != nil || r.code != 200 {
			t.Fatalf("held delivery got %d (%v)", r.code, r.err)
		}
	}
}
