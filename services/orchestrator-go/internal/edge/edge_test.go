package edge

import (
	"log"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"strings"
	"sync"
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
		"2001:db8::/16", "2001:db8::/47", "fe80::1%eth0", "1.2.3.4/33", "host.example"} {
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

// AEGIS edge review H-1: a full table evicts its least recently used entry; it never refuses a client it has not
// seen (that let anyone lock every new client out by filling it).
func TestLimiterTableIsBoundedAndEvictsLeastRecentlyUsed(t *testing.T) {
	c := &clock{time.Unix(1_700_000_000, 0)}
	l := NewLimiter(60, 1, 2)
	l.now = c.now
	a, b, n := netip.MustParseAddr("192.0.2.1"), netip.MustParseAddr("192.0.2.2"), netip.MustParseAddr("192.0.2.3")
	l.Allow(a)
	l.Allow(b) // both buckets now empty
	l.Allow(a) // a used most recently (refused, but touched)
	if ok, _ := l.Allow(n); !ok {
		t.Fatal("a full table refused a new client")
	}
	if l.buckets.len() != 2 {
		t.Fatalf("table grew to %d", l.buckets.len())
	}
	if ok, _ := l.Allow(a); ok {
		t.Fatal("the most recently used entry was evicted (a got a fresh burst)")
	}
	if ok, _ := l.Allow(b); !ok {
		t.Fatal("the least recently used entry (b) was not the one evicted")
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

func TestFailureLimiterFullTableEvictsAndNeverBlocksAnUntrackedClient(t *testing.T) {
	f := NewFailureLimiter(1, time.Minute, 2)
	x, y := netip.MustParseAddr("192.0.2.1"), netip.MustParseAddr("192.0.2.2")
	f.Fail(x)
	f.Fail(y)
	if b, _ := f.Blocked(netip.MustParseAddr("192.0.2.9")); b {
		t.Fatal("an untracked client was blocked while the table was full")
	}
	f.Fail(netip.MustParseAddr("192.0.2.9")) // evicts x, the least recently used
	if b, _ := f.Blocked(x); b {
		t.Fatal("x should have been evicted")
	}
	if b, _ := f.Blocked(y); !b {
		t.Fatal("y should still be blocked")
	}
	if f.entries.len() != 2 {
		t.Fatalf("table grew to %d", f.entries.len())
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

// H-1/M-1: a trusted request (the valid credential) passes a lockout and a drained anonymous bucket, and is counted
// in its own table.
func TestGuardTrustedRequestsBypassTheLockoutButNotTheirOwnLimit(t *testing.T) {
	var calls atomic.Int32
	g := &Guard{Name: "t", Source: SourceXRealIP, Limit: NewLimiter(60, 1, 100),
		AuthFail:     NewFailureLimiter(1, 10*time.Minute, 100),
		Trusted:      func(r *http.Request) bool { return r.Header.Get("Authorization") == "Bearer good" },
		TrustedLimit: NewLimiter(60, 2, 100)}
	srv := httptest.NewServer(g.Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer good" {
			w.WriteHeader(401)
		}
	})))
	defer srv.Close()
	get := func(auth string) int {
		r, _ := http.NewRequest("GET", srv.URL, nil)
		r.Header.Set("X-Real-IP", "198.51.100.1")
		if auth != "" {
			r.Header.Set("Authorization", auth)
		}
		resp, err := http.DefaultClient.Do(r)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if c := get("Bearer bad"); c != 401 {
		t.Fatal(c)
	}
	if c := get("Bearer bad"); c != 429 {
		t.Fatalf("locked out: %d", c)
	}
	if c := get("Bearer good"); c != 200 {
		t.Fatalf("valid credential while the address is locked out: %d", c)
	}
	if c := get("Bearer good"); c != 200 {
		t.Fatalf("2nd valid: %d", c)
	}
	if c := get("Bearer good"); c != 429 {
		t.Fatalf("3rd valid over its own burst of 2: %d", c)
	}
}

// M-3: an address in Cloudflare's ranges is logged as a distinct warning (and the request is otherwise unchanged);
// L-4: refusals are structured lines, ALERT level where configured.
func TestGuardLogsCloudflareAddressesAndStructuredRefusals(t *testing.T) {
	var buf strings.Builder
	var mu sync.Mutex
	log.SetOutput(writerFunc(func(p []byte) (int, error) { mu.Lock(); defer mu.Unlock(); return buf.Write(p) }))
	log.SetFlags(0)
	defer func() { log.SetOutput(os.Stderr); log.SetFlags(log.LstdFlags) }()
	cf, ver := CloudflareRanges()
	if !strings.Contains(ver, "cloudflare.com/ips") || !strings.Contains(ver, "2026-10-30") {
		t.Fatal(ver)
	}
	allow := MustParseAllowlist("3.18.12.63")
	var calls atomic.Int32
	g := &Guard{Name: "gw", Source: SourceXRealIP, Allow: &allow, WarnRanges: cf, Event: "stripe_delivery_refused",
		AlertOn403: true}
	srv := guarded(g, 200, &calls)
	defer srv.Close()
	do(t, srv.URL, "POST", "104.16.0.9")   // Cloudflare (IPv4) -> warning, then refused: not on the allowlist
	do(t, srv.URL, "POST", "2606:4700::1") // Cloudflare (IPv6): same reason, inside the 1-minute throttle
	do(t, srv.URL, "POST", "3.18.12.63")   // allowed, no warning
	mu.Lock()
	out := buf.String()
	mu.Unlock()
	for _, want := range []string{
		"gw edge event=gw_client_address_suspect level=WARN reason=client_in_cloudflare_range client=104.16.0.9",
		"gw edge event=stripe_delivery_refused level=ALERT reason=not_allowlisted client=",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
	if strings.Count(out, "client_in_cloudflare_range") != 1 {
		t.Errorf("the Cloudflare warning is not throttled:\n%s", out)
	}
	if strings.Contains(out, "client=3.18.12.63") {
		t.Errorf("an allowed request was logged:\n%s", out)
	}
	if !cf.Contains(netip.MustParseAddr("172.64.1.1")) || cf.Contains(netip.MustParseAddr("3.18.12.63")) {
		t.Fatal("Cloudflare ranges wrong")
	}
}

type writerFunc func([]byte) (int, error)

func (f writerFunc) Write(p []byte) (int, error) { return f(p) }
