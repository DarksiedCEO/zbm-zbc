package edge

import (
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net/http"
	"strconv"
	"sync"
	"time"
)

// Guard applies the perimeter controls to every request, in this order, before the wrapped handler runs (so before
// any route, auth check, body read or upstream call):
//
//  1. the client address (Source); undeterminable -> 403, except for Open requests (a platform health check
//     reaches the container directly, without the edge's header). An address inside Cloudflare's published ranges
//     (WarnRanges) is logged as a warning: X-Real-IP then names Cloudflare, not the client (AEGIS edge review M-3);
//  2. Allow, when set: a client outside it -> 403 (before its bucket is touched, so spoofed traffic from outside the
//     list cannot fill the limiter's table);
//  3. Trusted, when set and true (the request carries the valid bearer token, compared in constant time): only
//     TrustedLimit applies, a separate table no unauthenticated client can fill or drain, and the request goes to the
//     handler. The failed-auth lockout never refuses a valid token (AEGIS edge review H-1, M-1);
//  4. otherwise AuthFail, when set: a client that has used up its failed authentications -> 429 + Retry-After;
//  5. Limit: the client's token bucket -> 429 + Retry-After;
//  6. the handler; an answer of 401 counts one failed authentication against the client (AuthFail).
//
// Tables never refuse a client because they are full: they evict their least recently used entry (limit.go).
// Refusals carry `Connection: close` (the unread body is not drained) and a fixed JSON body. Every refusal is one
// structured log line, `edge event=<Event> level=<WARN|ALERT> reason=<code> client=<ip> method=<m> source=<src>`,
// throttled to one a second per reason with the number left out; never a header value or the body.
type Guard struct {
	Name     string // log prefix, e.g. "stripe-gateway"
	Source   IPSource
	Allow    *Allowlist      // nil: no source allowlist
	Limit    *Limiter        // nil: no rate limit
	AuthFail *FailureLimiter // nil: failed authentications are not counted
	// Trusted reports a request that carries the valid credential (constant-time compare). Nil: none is trusted.
	Trusted func(*http.Request) bool
	// TrustedLimit bounds trusted requests per client in their own table. Nil: no limit for them.
	TrustedLimit *Limiter
	// Open reports requests that may pass without a determinable client address (GET /health). Nil: none.
	Open func(*http.Request) bool
	// WarnRanges: a derived client address inside these networks is logged as a warning (Cloudflare's ranges).
	WarnRanges *Allowlist
	// Event names refusal log lines (default "<Name>_refused"); AlertOn403 marks the 403 refusals level=ALERT, for a
	// log alert (the gateway: a refused Stripe delivery is alert-worthy).
	Event      string
	AlertOn403 bool

	logMu sync.Mutex
	logs  map[string]*throttle
}

type throttle struct {
	last       time.Time
	suppressed int
}

// Refusal reason codes (the `reason=` field of the log line).
const (
	ReasonNoClientAddress = "no_client_address"
	ReasonNotAllowlisted  = "not_allowlisted"
	ReasonAuthLockout     = "auth_lockout"
	ReasonRateLimited     = "rate_limited"
	ReasonCloudflareRange = "client_in_cloudflare_range"
)

// Wrap returns next behind the guard.
func (g *Guard) Wrap(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		open := g.Open != nil && g.Open(r)
		ip, why := ClientIP(r, g.Source)
		if !ip.IsValid() {
			if open {
				next.ServeHTTP(w, r)
				return
			}
			g.event(ReasonNoClientAddress, g.level403(), "-", r.Method, why)
			refuse(w, http.StatusForbidden, "client address could not be determined", 0)
			return
		}
		if g.WarnRanges != nil && g.WarnRanges.Contains(ip) {
			g.event(ReasonCloudflareRange, "WARN", ip.String(), r.Method,
				"X-Real-IP is a Cloudflare address: the edge did not pass the client's address through")
		}
		if g.Allow != nil && !open && !g.Allow.Contains(ip) {
			g.event(ReasonNotAllowlisted, g.level403(), ip.String(), r.Method, "")
			refuse(w, http.StatusForbidden, "source address not allowed", 0)
			return
		}
		if g.Trusted != nil && g.Trusted(r) {
			if g.TrustedLimit != nil {
				if ok, wait := g.TrustedLimit.Allow(ip); !ok {
					g.event(ReasonRateLimited, "WARN", ip.String(), r.Method, "authenticated")
					refuse(w, http.StatusTooManyRequests, "rate limit exceeded", wait)
					return
				}
			}
			next.ServeHTTP(w, r)
			return
		}
		if g.AuthFail != nil {
			if blocked, wait := g.AuthFail.Blocked(ip); blocked {
				g.event(ReasonAuthLockout, "WARN", ip.String(), r.Method, "")
				refuse(w, http.StatusTooManyRequests, "too many failed authentications", wait)
				return
			}
		}
		if g.Limit != nil {
			if ok, wait := g.Limit.Allow(ip); !ok {
				g.event(ReasonRateLimited, "WARN", ip.String(), r.Method, "")
				refuse(w, http.StatusTooManyRequests, "rate limit exceeded", wait)
				return
			}
		}
		if g.AuthFail == nil {
			next.ServeHTTP(w, r)
			return
		}
		sw := &statusWriter{ResponseWriter: w}
		next.ServeHTTP(sw, r)
		if sw.status == http.StatusUnauthorized {
			g.AuthFail.Fail(ip)
		}
	})
}

func (g *Guard) level403() string {
	if g.AlertOn403 {
		return "ALERT"
	}
	return "WARN"
}

// RetryAfterSeconds is the Retry-After value for a wait: whole seconds, rounded up, at least 1.
func RetryAfterSeconds(d time.Duration) int {
	s := int(math.Ceil(d.Seconds()))
	if s < 1 {
		return 1
	}
	return s
}

func refuse(w http.ResponseWriter, status int, msg string, wait time.Duration) {
	h := w.Header()
	h.Set("Content-Type", "application/json")
	h.Set("Connection", "close")
	if status == http.StatusTooManyRequests {
		h.Set("Retry-After", strconv.Itoa(RetryAfterSeconds(wait)))
	}
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

// event writes one structured line per refusal or warning, at most one a second per reason (one a minute for the
// Cloudflare-range warning, which repeats on every request while it holds); the next line says how many were left
// out. detail is fixed text from this package, never a header value.
func (g *Guard) event(reason, level, client, method, detail string) {
	every := time.Second
	if reason == ReasonCloudflareRange {
		every = time.Minute
	}
	g.logMu.Lock()
	if g.logs == nil {
		g.logs = map[string]*throttle{}
	}
	t := g.logs[reason]
	if t == nil {
		t = &throttle{}
		g.logs[reason] = t
	}
	now := time.Now()
	if !t.last.IsZero() && now.Sub(t.last) < every {
		t.suppressed++
		g.logMu.Unlock()
		return
	}
	n := t.suppressed
	t.suppressed, t.last = 0, now
	g.logMu.Unlock()
	ev := g.Event
	if ev == "" {
		ev = g.Name + "_refused"
	}
	if reason == ReasonCloudflareRange {
		ev = g.Name + "_client_address_suspect"
	}
	msg := fmt.Sprintf("%s edge event=%s level=%s reason=%s client=%s method=%s source=%s", g.Name, ev, level,
		reason, client, method, g.Source)
	if n > 0 {
		msg += fmt.Sprintf(" not_logged_since_last=%d", n)
	}
	if detail != "" {
		msg += fmt.Sprintf(" detail=%q", detail)
	}
	log.Print(msg)
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	if s.status == 0 {
		s.status = code
	}
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusWriter) Write(b []byte) (int, error) {
	if s.status == 0 {
		s.status = http.StatusOK
	}
	return s.ResponseWriter.Write(b)
}

// Unwrap lets http.ResponseController reach the underlying writer.
func (s *statusWriter) Unwrap() http.ResponseWriter { return s.ResponseWriter }

// IntEnv reads a positive decimal integer setting in [lo, hi]; unset/empty gives def. Anything else refuses the start.
func IntEnv(getenv func(string) string, name string, def, lo, hi int) (int, error) {
	raw := getenv(name)
	if raw == "" {
		return def, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < lo || n > hi || strconv.Itoa(n) != raw {
		return 0, fmt.Errorf("%s must be a whole number from %d to %d (unset = %d)", name, lo, hi, def)
	}
	return n, nil
}
