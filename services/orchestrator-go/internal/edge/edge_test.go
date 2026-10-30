package edge

import (
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func req(realIP []string, remote string) *http.Request {
	r := httptest.NewRequest(http.MethodPost, "/x", nil)
	r.RemoteAddr = remote
	for _, v := range realIP {
		r.Header.Add("X-Real-IP", v)
	}
	return r
}

func TestClientIPFromXRealIP(t *testing.T) {
	cases := []struct {
		vals []string
		want string // "" = undeterminable
	}{
		{[]string{"3.18.12.63"}, "3.18.12.63"},
		{[]string{" 3.18.12.63 "}, "3.18.12.63"},
		{[]string{"::ffff:3.18.12.63"}, "3.18.12.63"},
		{[]string{"2001:db8::1"}, "2001:db8::1"},
		{nil, ""},
		{[]string{""}, ""},
		{[]string{"3.18.12.63", "1.2.3.4"}, ""},  // repeated header
		{[]string{"1.2.3.4, 3.18.12.63"}, ""},    // a list is never trusted
		{[]string{"3.18.12.63:443"}, ""},         // not an address
		{[]string{"fe80::1%eth0"}, ""},           // zone
		{[]string{"0.0.0.0"}, ""},                // unspecified
		{[]string{"evil.example"}, ""},           // name
		{[]string{strings.Repeat("1", 100)}, ""}, // oversized
	}
	for _, c := range cases {
		// RemoteAddr is a Stripe address: it must never be used in x-real-ip mode
		ip, why := ClientIP(req(c.vals, "54.187.174.169:5555"), SourceXRealIP)
		got := ""
		if ip.IsValid() {
			got = ip.String()
		}
		if got != c.want {
			t.Errorf("%q: got %q want %q", c.vals, got, c.want)
		}
		if c.want == "" && why == "" {
			t.Errorf("%q: no reason given", c.vals)
		}
	}
}

func TestClientIPFromRemoteAddrIgnoresHeaders(t *testing.T) {
	ip, _ := ClientIP(req([]string{"3.18.12.63"}, "127.0.0.1:0"), SourceRemoteAddr)
	if ip.String() != "127.0.0.1" {
		t.Fatalf("got %v", ip)
	}
	ip, _ = ClientIP(req(nil, "[::ffff:10.0.0.1]:80"), SourceRemoteAddr)
	if ip.String() != "10.0.0.1" {
		t.Fatalf("got %v", ip)
	}
	if ip, _ := ClientIP(req(nil, "garbage"), SourceRemoteAddr); ip.IsValid() {
		t.Fatal("garbage remote address accepted")
	}
}

func TestParseIPSource(t *testing.T) {
	for raw, want := range map[string]IPSource{"": SourceXRealIP, "x-real-ip": SourceXRealIP,
		"remote-addr": SourceRemoteAddr} {
		if got, err := ParseIPSource("X", raw); err != nil || got != want {
			t.Errorf("%q: %v %v", raw, got, err)
		}
	}
	for _, raw := range []string{"X-Real-IP", "xff", "x-forwarded-for", "remote", " x-real-ip"} {
		if _, err := ParseIPSource("X", raw); err == nil {
			t.Errorf("%q accepted", raw)
		}
	}
}

func TestParseAllowlist(t *testing.T) {
	a, err := ParseAllowlist(" 3.18.12.63 ,198.51.100.0/24, 2001:db8::/48,::ffff:192.0.2.1")
	if err != nil || a.Len() != 4 {
		t.Fatalf("%v %d", err, a.Len())
	}
	for ip, want := range map[string]bool{"3.18.12.63": true, "3.18.12.64": false, "198.51.100.200": true,
		"2001:db8:0:5::1": true, "2001:db9::1": false, "192.0.2.1": true, "::ffff:3.18.12.63": true} {
		if got := a.Contains(netip.MustParseAddr(ip)); got != want {
			t.Errorf("%s: %v", ip, got)
		}
	}
	for _, bad := range []string{"", " ", "1.2.3.4,", ",1.2.3.4", "1.2.3", "0.0.0.0/0", "10.0.0.0/8", "::/0",
		"2001:db8::/16", "fe80::1%eth0", "1.2.3.4/33", "host.example"} {
		if _, err := ParseAllowlist(bad); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

type clock struct{ t time.Time }

func (c *clock) now() time.Time { return c.t }

func TestLimiterBurstThenRefillWithRetryAfter(t *testing.T) {
	c := &clock{time.Unix(1_700_000_000, 0)}
	l := NewLimiter(60, 3, 100)
	l.now = c.now
	ip := netip.MustParseAddr("192.0.2.7")
	for i := 0; i < 3; i++ {
		if ok, _ := l.Allow(ip); !ok {
			t.Fatalf("request %d refused inside the burst", i)
		}
	}
	ok, wait := l.Allow(ip)
	if ok || wait <= 0 || wait > time.Second {
		t.Fatalf("4th: %v %v", ok, wait)
	}
	if ok, _ := l.Allow(netip.MustParseAddr("192.0.2.8")); !ok {
		t.Fatal("another client was refused")
	}
	c.t = c.t.Add(time.Second)
	if ok, _ := l.Allow(ip); !ok {
		t.Fatal("no refill after a second at 60/min")
	}
}

func TestLimiterCountsAnIPv6Slash64AsOneClient(t *testing.T) {
	l := NewLimiter(60, 2, 100)
	l.Allow(netip.MustParseAddr("2001:db8:1:2::1"))
	l.Allow(netip.MustParseAddr("2001:db8:1:2::2"))
	if ok, _ := l.Allow(netip.MustParseAddr("2001:db8:1:2:ffff::9")); ok {
		t.Fatal("rotating inside one /64 escaped the limit")
	}
	if ok, _ := l.Allow(netip.MustParseAddr("2001:db8:1:3::1")); !ok {
		t.Fatal("a different /64 was refused")
	}
}

func TestLimiterTableIsBoundedAndFailsClosed(t *testing.T) {
	c := &clock{time.Unix(1_700_000_000, 0)}
	l := NewLimiter(60, 5, 2)
	l.now = c.now
	l.Allow(netip.MustParseAddr("192.0.2.1"))
	l.Allow(netip.MustParseAddr("192.0.2.2"))
	if ok, _ := l.Allow(netip.MustParseAddr("192.0.2.3")); ok {
		t.Fatal("a full table admitted a new client")
	}
	if len(l.buckets) != 2 {
		t.Fatalf("table grew to %d", len(l.buckets))
	}
	c.t = c.t.Add(time.Hour) // every bucket idle long enough to be full again: swept
	if ok, _ := l.Allow(netip.MustParseAddr("192.0.2.3")); !ok {
		t.Fatal("idle entries were not swept")
	}
}

func TestFailureLimiterBlocksForTheRestOfTheWindow(t *testing.T) {
	c := &clock{time.Unix(1_700_000_000, 0)}
	f := NewFailureLimiter(3, 10*time.Minute, 100)
	f.now = c.now
	ip := netip.MustParseAddr("198.51.100.9")
	for i := 0; i < 3; i++ {
		if b, _ := f.Blocked(ip); b {
			t.Fatalf("blocked after %d failures", i)
		}
		f.Fail(ip)
	}
	b, wait := f.Blocked(ip)
	if !b || wait != 10*time.Minute {
		t.Fatalf("%v %v", b, wait)
	}
	c.t = c.t.Add(9 * time.Minute)
	if b, wait := f.Blocked(ip); !b || wait != time.Minute {
		t.Fatalf("%v %v", b, wait)
	}
	c.t = c.t.Add(time.Minute)
	if b, _ := f.Blocked(ip); b {
		t.Fatal("still blocked after the window")
	}
}

func TestFailureLimiterFullTableFailsClosed(t *testing.T) {
	f := NewFailureLimiter(3, time.Minute, 1)
	f.Fail(netip.MustParseAddr("192.0.2.1"))
	if b, _ := f.Blocked(netip.MustParseAddr("192.0.2.2")); !b {
		t.Fatal("an untracked client passed while the table was full")
	}
}

func TestIntEnv(t *testing.T) {
	get := func(v string) func(string) string { return func(string) string { return v } }
	if n, err := IntEnv(get(""), "N", 30, 1, 100); n != 30 || err != nil {
		t.Fatal(n, err)
	}
	if n, err := IntEnv(get("45"), "N", 30, 1, 100); n != 45 || err != nil {
		t.Fatal(n, err)
	}
	for _, bad := range []string{"0", "101", "-1", "+5", "05", "1e2", " 5", "off", "5.0"} {
		if _, err := IntEnv(get(bad), "N", 30, 1, 100); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestRetryAfterSeconds(t *testing.T) {
	for d, want := range map[time.Duration]int{0: 1, time.Millisecond: 1, time.Second: 1, 1500 * time.Millisecond: 2,
		10 * time.Minute: 600} {
		if got := RetryAfterSeconds(d); got != want {
			t.Errorf("%v: %d", d, got)
		}
	}
}

func guarded(g *Guard, status int, calls *atomic.Int32) *httptest.Server {
	return httptest.NewServer(g.Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(status)
	})))
}

func do(t *testing.T, url, method, realIP string) *http.Response {
	t.Helper()
	r, _ := http.NewRequest(method, url, strings.NewReader("body"))
	if realIP != "" {
		r.Header.Set("X-Real-IP", realIP)
	}
	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	return resp
}

func TestGuardRefusesUndeterminableAndNotAllowedBeforeTheHandler(t *testing.T) {
	var calls atomic.Int32
	allow := MustParseAllowlist("3.18.12.63")
	g := &Guard{Name: "t", Source: SourceXRealIP, Allow: &allow, Limit: NewLimiter(60, 60, 100),
		Open: func(r *http.Request) bool { return r.Method == http.MethodGet && r.URL.Path == "/health" }}
	srv := guarded(g, 200, &calls)
	defer srv.Close()
	if r := do(t, srv.URL+"/webhooks/stripe", "POST", ""); r.StatusCode != 403 || !r.Close {
		t.Fatalf("no X-Real-IP: %d", r.StatusCode)
	}
	if r := do(t, srv.URL+"/webhooks/stripe", "POST", "1.2.3.4"); r.StatusCode != 403 {
		t.Fatalf("not allowlisted: %d", r.StatusCode)
	}
	if calls.Load() != 0 {
		t.Fatal("the handler ran for a refused request")
	}
	if r := do(t, srv.URL+"/webhooks/stripe", "POST", "3.18.12.63"); r.StatusCode != 200 {
		t.Fatalf("allowlisted: %d", r.StatusCode)
	}
	// a platform health check reaches the container without the edge's header
	if r := do(t, srv.URL+"/health", "GET", ""); r.StatusCode != 200 {
		t.Fatalf("health without X-Real-IP: %d", r.StatusCode)
	}
	if r := do(t, srv.URL+"/health", "GET", "1.2.3.4"); r.StatusCode != 200 {
		t.Fatalf("health from outside the allowlist: %d", r.StatusCode)
	}
	if r := do(t, srv.URL+"/health", "POST", ""); r.StatusCode != 403 {
		t.Fatalf("POST /health is not open: %d", r.StatusCode)
	}
}

func TestGuardRateLimitAnswers429WithRetryAfter(t *testing.T) {
	var calls atomic.Int32
	g := &Guard{Name: "t", Source: SourceXRealIP, Limit: NewLimiter(60, 2, 100)}
	srv := guarded(g, 200, &calls)
	defer srv.Close()
	do(t, srv.URL, "GET", "192.0.2.1")
	do(t, srv.URL, "GET", "192.0.2.1")
	r := do(t, srv.URL, "GET", "192.0.2.1")
	if r.StatusCode != 429 || r.Header.Get("Retry-After") != "1" {
		t.Fatalf("%d %q", r.StatusCode, r.Header.Get("Retry-After"))
	}
	if calls.Load() != 2 {
		t.Fatalf("handler ran %d times", calls.Load())
	}
	if r := do(t, srv.URL, "GET", "192.0.2.2"); r.StatusCode != 200 {
		t.Fatal("another client was limited")
	}
}

func TestGuardCountsFailedAuthenticationsAndBlocks(t *testing.T) {
	var calls atomic.Int32
	g := &Guard{Name: "t", Source: SourceXRealIP, Limit: NewLimiter(1000, 1000, 100),
		AuthFail: NewFailureLimiter(3, 10*time.Minute, 100)}
	srv := guarded(g, 401, &calls)
	defer srv.Close()
	for i := 0; i < 3; i++ {
		if r := do(t, srv.URL, "GET", "203.0.113.5"); r.StatusCode != 401 {
			t.Fatalf("attempt %d: %d", i, r.StatusCode)
		}
	}
	r := do(t, srv.URL, "GET", "203.0.113.5")
	if r.StatusCode != 429 || r.Header.Get("Retry-After") != "600" {
		t.Fatalf("%d %q", r.StatusCode, r.Header.Get("Retry-After"))
	}
	if calls.Load() != 3 {
		t.Fatalf("handler ran %d times", calls.Load())
	}
	if r := do(t, srv.URL, "GET", "203.0.113.6"); r.StatusCode != 401 {
		t.Fatal("another client was blocked")
	}
}

func TestGuardDoesNotCountSuccessOrOtherErrorsAsAuthFailures(t *testing.T) {
	for _, st := range []int{200, 403, 404, 405, 502} {
		var calls atomic.Int32
		g := &Guard{Name: "t", Source: SourceXRealIP, AuthFail: NewFailureLimiter(1, time.Minute, 100)}
		srv := guarded(g, st, &calls)
		do(t, srv.URL, "GET", "203.0.113.5")
		if r := do(t, srv.URL, "GET", "203.0.113.5"); r.StatusCode != st {
			t.Errorf("status %d counted as a failed authentication", st)
		}
		srv.Close()
	}
}
