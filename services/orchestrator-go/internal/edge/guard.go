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
//     reaches the container directly, without the edge's header);
//  2. Allow, when set: a client outside it -> 403 (before its bucket is touched, so spoofed traffic from outside the
//     list cannot fill the limiter's table);
//  3. AuthFail, when set: a client that has used up its failed authentications -> 429 + Retry-After;
//  4. Limit: the client's token bucket -> 429 + Retry-After;
//  5. the handler; an answer of 401 counts one failed authentication against the client (AuthFail).
//
// Refusals carry `Connection: close` (the unread body is not drained) and a fixed JSON body; the log line names the
// reason and the client address, never a header value or the body.
type Guard struct {
	Name     string // log prefix, e.g. "stripe-gateway"
	Source   IPSource
	Allow    *Allowlist      // nil: no source allowlist
	Limit    *Limiter        // nil: no rate limit
	AuthFail *FailureLimiter // nil: failed authentications are not counted
	// Open reports requests that may pass without a determinable client address (GET /health). Nil: none.
	Open func(*http.Request) bool

	logMu      sync.Mutex
	lastLog    time.Time
	suppressed int
}

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
			g.logf("refused %s: client address undeterminable (%s; source %s)", r.Method, why, g.Source)
			refuse(w, http.StatusForbidden, "client address could not be determined", 0)
			return
		}
		if g.Allow != nil && !open && !g.Allow.Contains(ip) {
			g.logf("refused %s from %s: source address not on the allowlist", r.Method, ip)
			refuse(w, http.StatusForbidden, "source address not allowed", 0)
			return
		}
		if g.AuthFail != nil {
			if blocked, wait := g.AuthFail.Blocked(ip); blocked {
				g.logf("refused %s from %s: too many failed authentications", r.Method, ip)
				refuse(w, http.StatusTooManyRequests, "too many failed authentications", wait)
				return
			}
		}
		if g.Limit != nil {
			if ok, wait := g.Limit.Allow(ip); !ok {
				g.logf("refused %s from %s: rate limit", r.Method, ip)
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

// logf writes at most one refusal line a second (a flood must not turn into a log flood); the next line written
// says how many were left out.
func (g *Guard) logf(format string, args ...any) {
	g.logMu.Lock()
	now := time.Now()
	if now.Sub(g.lastLog) < time.Second {
		g.suppressed++
		g.logMu.Unlock()
		return
	}
	n := g.suppressed
	g.suppressed, g.lastLog = 0, now
	g.logMu.Unlock()
	msg := fmt.Sprintf(format, args...)
	if n > 0 {
		msg += fmt.Sprintf(" (+%d refusals not logged in the last second)", n)
	}
	log.Printf("%s edge: %s", g.Name, msg)
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
