package main

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/edge"
	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/orchestrator"
)

// requireAuth wraps a handler so it rejects any request without a valid
// `Authorization: Bearer <token>` header. Uses subtle.ConstantTimeCompare
// (Go's equivalent of Python's hmac.compare_digest) to avoid a timing
// side-channel on the token comparison. /health is deliberately NOT
// wrapped with this — see main().
func requireAuth(token string, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		const prefix = "Bearer "
		auth := r.Header.Get("Authorization")
		if !strings.HasPrefix(auth, prefix) {
			w.Header().Set("WWW-Authenticate", "Bearer")
			http.Error(w, `{"error":"missing or malformed Authorization header (expected: Bearer <token>)"}`, http.StatusUnauthorized)
			return
		}
		supplied := strings.TrimPrefix(auth, prefix)
		if subtle.ConstantTimeCompare([]byte(supplied), []byte(token)) != 1 {
			w.Header().Set("WWW-Authenticate", "Bearer")
			http.Error(w, `{"error":"invalid token"}`, http.StatusUnauthorized)
			return
		}
		next(w, r)
	}
}

// writeJSON writes v as a JSON response with the given status.
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// writeUpstreamError answers 502 for a failed scan/read (fix wave 3, AEGIS
// D3). The full error — which can contain upstream URLs, host:port and
// upstream response bodies — is logged server-side under a random
// correlation id; the caller (and the dashboard, which displays it) gets
// only orchestrator.PublicMessage(err) plus that id.
func writeUpstreamError(w http.ResponseWriter, route string, err error) {
	var b [8]byte
	id := "unavailable"
	if _, rerr := rand.Read(b[:]); rerr == nil {
		id = hex.EncodeToString(b[:])
	}
	log.Printf("correlation_id=%s route=%q error: %v", id, route, err)
	writeJSON(w, http.StatusBadGateway, map[string]string{
		"error":          orchestrator.PublicMessage(err),
		"correlation_id": id,
	})
}

// Server limits (fix wave 1, Sep 24 2026; ADR 0001 "Request limits").
// Finding: main() used http.ListenAndServe, i.e. an http.Server with no
// timeouts at all — a client sending its request head or body one byte at a
// time held a connection and goroutine forever — and no body limit.
const (
	// readHeaderTimeout: the whole request line + headers must arrive in 5s.
	readHeaderTimeout = 5 * time.Second
	// readTimeout: the whole request, body included, must arrive in 15s.
	readTimeout = 15 * time.Second
	// handlerTimeout is the budget for one request's work, i.e. one full scan
	// (every upstream call is additionally bounded by the client's own 10s).
	// Measured on the live 3-process stack (Sep 24 2026): a full fixture scan
	// (15 detection calls, 10 ledger appends, 2 verifies) took 25-100 ms;
	// 60s is several hundred times that, and still bounded.
	handlerTimeout = 60 * time.Second
	// writeTimeout must exceed handlerTimeout so the response of a scan that
	// used its whole budget (a 502 naming the step that timed out) can still
	// be written.
	writeTimeout = handlerTimeout + 15*time.Second
	idleTimeout  = 60 * time.Second
	// maxHeaderBytes: the request line (with its query string) and headers.
	// Callers send a few short headers (a bearer token is < 100 bytes).
	maxHeaderBytes = 16 << 10
	// maxRequestBodyBytes: no route takes a request body; anything a client
	// does send is read (up to this) and discarded before the handler runs.
	maxRequestBodyBytes = 64 << 10
)

// newServer is the orchestrator's http.Server with every limit set.
func newServer(addr string, h http.Handler) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           limitRequests(h),
		ReadHeaderTimeout: readHeaderTimeout,
		ReadTimeout:       readTimeout,
		WriteTimeout:      writeTimeout,
		IdleTimeout:       idleTimeout,
		MaxHeaderBytes:    maxHeaderBytes,
	}
}

// limitRequests runs before routing and auth: a body declared larger than
// maxRequestBodyBytes is refused with 413 without reading it; any other body
// is read through http.MaxBytesReader and discarded BEFORE the handler runs,
// so a scan never starts until its whole request has arrived (a slow body
// runs into readTimeout and is answered 408, an undeclared oversized one
// 413). The handler then gets a context bounded by handlerTimeout.
func limitRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ContentLength > maxRequestBodyBytes {
			w.Header().Set("Connection", "close")
			writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "request body too large"})
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodyBytes)
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			w.Header().Set("Connection", "close")
			var tooLarge *http.MaxBytesError
			var timeout interface{ Timeout() bool }
			switch {
			case errors.As(err, &tooLarge):
				writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "request body too large"})
			case errors.As(err, &timeout) && timeout.Timeout():
				writeJSON(w, http.StatusRequestTimeout, map[string]string{"error": "request body not received in time"})
			default:
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "could not read request body"})
			}
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), handlerTimeout)
		defer cancel()
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// Perimeter settings (ADR 0023 "Public edge hardening"). The orchestrator's Railway domain is public; Railway's edge
// is a reverse proxy, not a WAF, so the limits are enforced here, per client address (Railway's X-Real-IP).
const (
	clientIPSourceEnv   = "ORCHESTRATOR_CLIENT_IP_SOURCE"
	ratePerMinEnv       = "ORCHESTRATOR_RATE_PER_MIN"
	rateBurstEnv        = "ORCHESTRATOR_RATE_BURST"
	authFailMaxEnv      = "ORCHESTRATOR_AUTH_FAIL_MAX"
	authFailWindowEnv   = "ORCHESTRATOR_AUTH_FAIL_WINDOW_SECONDS"
	defaultRatePerMin   = 30
	defaultRateBurst    = 30
	defaultAuthFailMax  = 10
	defaultAuthFailSecs = 600
)

// isHealth: the one request that passes without a client address (Railway's health check reaches the container
// directly, without the edge's X-Real-IP); it answers a constant.
func isHealth(r *http.Request) bool {
	return (r.Method == http.MethodGet || r.Method == http.MethodHead) && r.URL.Path == "/health"
}

// loadGuard reads the perimeter settings: per client, a token bucket (ORCHESTRATOR_RATE_PER_MIN requests a minute,
// bursts of ORCHESTRATOR_RATE_BURST) and a failed-authentication limit (ORCHESTRATOR_AUTH_FAIL_MAX answers of 401
// inside ORCHESTRATOR_AUTH_FAIL_WINDOW_SECONDS, then 429 until the window ends). A request whose client address
// cannot be determined is refused 403 (except GET /health). Any malformed value refuses the start.
func loadGuard(getenv func(string) string) (*edge.Guard, error) {
	src, err := edge.ParseIPSource(clientIPSourceEnv, getenv(clientIPSourceEnv))
	if err != nil {
		return nil, err
	}
	perMin, err := edge.IntEnv(getenv, ratePerMinEnv, defaultRatePerMin, 1, 6000)
	if err != nil {
		return nil, err
	}
	burst, err := edge.IntEnv(getenv, rateBurstEnv, defaultRateBurst, 1, 6000)
	if err != nil {
		return nil, err
	}
	failMax, err := edge.IntEnv(getenv, authFailMaxEnv, defaultAuthFailMax, 1, 1000)
	if err != nil {
		return nil, err
	}
	failSecs, err := edge.IntEnv(getenv, authFailWindowEnv, defaultAuthFailSecs, 1, 86400)
	if err != nil {
		return nil, err
	}
	return &edge.Guard{Name: "orchestrator-go", Source: src, Open: isHealth,
		Limit:    edge.NewLimiter(perMin, burst, edge.DefaultMaxKeys),
		AuthFail: edge.NewFailureLimiter(failMax, time.Duration(failSecs)*time.Second, edge.DefaultMaxKeys)}, nil
}

// guardServer puts the guard in front of everything the server runs, so a refused client is answered before its
// body is read (limitRequests) and before any route or auth check.
func guardServer(srv *http.Server, g *edge.Guard) *http.Server {
	srv.Handler = g.Wrap(srv.Handler)
	return srv
}

// newMux builds the orchestrator's routes.
//
// Fix wave 1 (Sep 24 2026): the dashboard used to GET /revenue-recovery/scan
// on every page view, and the scan route answered any method — so every
// view ran all agents and appended ~10 duplicate findings to the evidence
// ledger (219 -> 229 entries for one page load). Now:
//   - POST /revenue-recovery/scan is the only way to run a scan (it writes
//     every finding to the ledger, by design). Any other method is 405 and
//     runs nothing.
//   - GET /revenue-recovery/findings is read-only: it returns the findings
//     already recorded in the ledger (GET /ledger/entries, kind "finding")
//     with the ledger's verify verdict. It never calls a ledger write
//     endpoint and never calls detection-py. The dashboard uses this.
//
// Both require the orchestrator bearer token; /health stays open.
// Method-qualified patterns (Go 1.22+) make the mux answer 405 for a wrong
// method before any handler (or upstream call) runs; "GET" also matches HEAD.
//
// AEGIS final go-live L-2: a scan of the fixture tenant (client_id omitted,
// which defaults to it, or client_id=fixture-pool) writes the fixture pool's
// findings into the append-only ledger. It is refused (403
// FIXTURE_SCAN_DISABLED, before the ledger or detection is called) unless
// allowFixtureScans is set, which main takes from
// ORCHESTRATOR_ALLOW_FIXTURE_SCANS=1 (dev, test and the live runs only; the
// Railway deploy never sets it).
func newMux(orch *orchestrator.Orchestrator, orchestratorToken string, allowFixtureScans bool) *http.ServeMux {
	mux := http.NewServeMux()

	// /health is intentionally open (no auth) — needed for basic
	// liveness/readiness checks without requiring a token.
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "service": "orchestrator-go"})
	})

	// POST /revenue-recovery/scan[?client_id=<tenant>][&as_of=<RFC 3339>]
	// (Revenue Recovery fix wave, Oct 6 2026). client_id omitted = the
	// fixture tenant (the only one with a data source; recorded as
	// tenant_defaulted). as_of omitted = now; it must carry an offset. One
	// scan at a time: a concurrent request is 409 + Retry-After and runs
	// nothing.
	mux.HandleFunc("POST /revenue-recovery/scan", requireAuth(orchestratorToken, func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		for k, vs := range q {
			if (k != "client_id" && k != "as_of") || len(vs) != 1 {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "only client_id and as_of may be given, once each"})
				return
			}
		}
		req := orchestrator.ScanRequest{ClientID: q.Get("client_id")}
		if q.Has("client_id") && req.ClientID == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "client_id must not be empty"})
			return
		}
		if !allowFixtureScans && (req.ClientID == "" || req.ClientID == orchestrator.FixtureClientID) {
			writeJSON(w, http.StatusForbidden, map[string]string{
				"error": "fixture scans are disabled on this deployment (they would write fixture findings to the " +
					"production ledger); give the client_id of a connected store",
				"reason": "FIXTURE_SCAN_DISABLED",
			})
			return
		}
		if v := q.Get("as_of"); v != "" {
			t, err := time.Parse(time.RFC3339Nano, v)
			if err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "as_of must be an RFC 3339 timestamp with a UTC offset"})
				return
			}
			req.AsOf = t
		}
		result, err := orch.RunFullScan(r.Context(), req)
		var reqErr *orchestrator.RequestError
		switch {
		case errors.Is(err, orchestrator.ErrScanInProgress):
			w.Header().Set("Retry-After", "5")
			writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
		case errors.As(err, &reqErr):
			body := map[string]string{"error": reqErr.Msg}
			if reqErr.Reason != "" {
				body["reason"] = reqErr.Reason // ADR 0020: the store connector's closed reason code
			}
			writeJSON(w, reqErr.Status, body)
		case err != nil:
			writeUpstreamError(w, "POST /revenue-recovery/scan", err)
		default:
			writeJSON(w, http.StatusOK, result)
		}
	}))

	mux.HandleFunc("GET /revenue-recovery/findings", requireAuth(orchestratorToken, func(w http.ResponseWriter, r *http.Request) {
		result, err := orch.RecordedFindings(r.Context())
		if err != nil {
			writeUpstreamError(w, "GET /revenue-recovery/findings", err)
			return
		}
		writeJSON(w, http.StatusOK, result)
	}))

	return mux
}

func main() {
	detectionURL := os.Getenv("DETECTION_SERVICE_URL")
	if detectionURL == "" {
		detectionURL = "http://localhost:8000"
	}
	ledgerURL := os.Getenv("LEDGER_SERVICE_URL")
	if ledgerURL == "" {
		ledgerURL = "http://localhost:8090"
	}
	// Same class of bug as ledger-rust's confirmed 0.0.0.0 finding: no host
	// in ":"+port means Go's net/http binds every network interface, not
	// just localhost. Fixed the same way — configurable, loopback default.
	bindAddr := os.Getenv("ORCHESTRATOR_BIND_ADDR")
	if bindAddr == "" {
		bindAddr = "127.0.0.1"
	}
	port := os.Getenv("ORCHESTRATOR_PORT")
	if port == "" {
		port = "8080"
	}

	// Fail closed, matching detection-py's own startup behavior: this
	// process refuses to start at all without both tokens configured,
	// rather than silently running unauthenticated or unable to reach
	// its upstream.
	detectionToken := os.Getenv("DETECTION_SERVICE_TOKEN")
	if detectionToken == "" {
		log.Fatal(
			"DETECTION_SERVICE_TOKEN is not set. orchestrator-go refuses to start " +
				"without it (fail closed, not open) — detection-py requires this exact " +
				"value as its ZBM_SERVICE_TOKEN, or every call to it will fail with 401.",
		)
	}
	orchestratorToken := os.Getenv("ORCHESTRATOR_SERVICE_TOKEN")
	if orchestratorToken == "" {
		log.Fatal(
			"ORCHESTRATOR_SERVICE_TOKEN is not set. orchestrator-go refuses to start " +
				"without it (fail closed, not open) — this is the token callers (e.g. the " +
				"dashboard) must present to reach /revenue-recovery/scan and /revenue-recovery/findings.",
		)
	}
	// Independent review, Sep 22 2026 (CONFIRMED): ledger-rust had zero
	// authentication and has now been fixed to fail closed on its own
	// LEDGER_SERVICE_TOKEN. This process must present that same token on
	// every call or every ledger write/read will fail with 401 — so this
	// fails closed here too, rather than starting and silently failing
	// every scan's ledger writes the first time one runs.
	ledgerToken := os.Getenv("LEDGER_SERVICE_TOKEN")
	if ledgerToken == "" {
		log.Fatal(
			"LEDGER_SERVICE_TOKEN is not set. orchestrator-go refuses to start " +
				"without it (fail closed, not open) — ledger-rust requires this exact " +
				"value, or every ledger write/read will fail with 401.",
		)
	}

	allowFixtureScans, err := fixtureScansFromEnv(os.Getenv("ORCHESTRATOR_ALLOW_FIXTURE_SCANS"))
	if err != nil {
		log.Fatalf("orchestrator-go: REFUSING TO START — %v", err)
	}
	if allowFixtureScans {
		log.Printf("orchestrator-go: ORCHESTRATOR_ALLOW_FIXTURE_SCANS=1 — fixture scans write fixture findings to the ledger (dev/test only)")
	}

	guard, err := loadGuard(os.Getenv)
	if err != nil {
		log.Fatalf("orchestrator-go: REFUSING TO START — %v", err)
	}

	orch := orchestrator.New(detectionURL, detectionToken, ledgerURL, ledgerToken)

	srv := guardServer(newServer(bindAddr+":"+port, newMux(orch, orchestratorToken, allowFixtureScans)), guard)
	// Bind first, then announce: ORCHESTRATOR_PORT=0 lets the kernel pick a free port, and ORCHESTRATOR_PORT_FILE
	// (optional) receives the port actually bound, so a caller (the tests) never has to guess a free port and race
	// another process for it (fix wave 25, scout C2-1 — the ledger-rust LEDGER_PORT_FILE pattern, ADR 0003 §11).
	ln, err := net.Listen("tcp", srv.Addr)
	if err != nil {
		log.Fatalf("orchestrator-go: cannot listen on %s: %v", srv.Addr, err)
	}
	bound := strconv.Itoa(ln.Addr().(*net.TCPAddr).Port)
	if pf := os.Getenv("ORCHESTRATOR_PORT_FILE"); pf != "" {
		if err := publishPortFile(pf, bound); err != nil {
			log.Fatalf("orchestrator-go: REFUSING TO START — ORCHESTRATOR_PORT_FILE=%q: %v", pf, err)
		}
		removeOnStop(pf)
	}
	log.Printf("orchestrator-go listening on %s:%s (detection service at %s, ledger at %s)", bindAddr, bound, detectionURL, ledgerURL)
	log.Fatal(srv.Serve(ln))
}

// fixtureScansFromEnv reads ORCHESTRATOR_ALLOW_FIXTURE_SCANS: unset, empty or
// "0" = refused (the production default), "1" = allowed. Anything else stops
// the start-up, so a typo cannot quietly leave fixture scans on or off.
func fixtureScansFromEnv(v string) (bool, error) {
	switch v {
	case "", "0":
		return false, nil
	case "1":
		return true, nil
	}
	return false, fmt.Errorf("ORCHESTRATOR_ALLOW_FIXTURE_SCANS must be unset, 0 or 1")
}

// publishPortFile writes the bound port to path atomically (a temp file in the same directory, then rename), so a
// reader sees either no file or the whole port. The file is a HINT, like ledger-rust's (ADR 0003 §11): a SIGKILL
// leaves it behind, so a reader checks the port (GET /health) and, if it started the process, that the process is
// still alive before trusting it.
func publishPortFile(path, port string) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	if _, err := fmt.Fprintf(tmp, "%s\n", port); err != nil {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	if err := os.Rename(tmp.Name(), path); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	return nil
}

// removeOnStop removes the port file on SIGINT/SIGTERM, then dies of the same signal (default disposition).
func removeOnStop(path string) {
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		sig := <-ch
		os.Remove(path)
		signal.Reset(sig)
		_ = syscall.Kill(os.Getpid(), sig.(syscall.Signal))
	}()
}
