package main

import (
	"crypto/subtle"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strings"

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
func newMux(orch *orchestrator.Orchestrator, orchestratorToken string) *http.ServeMux {
	mux := http.NewServeMux()

	// /health is intentionally open (no auth) — needed for basic
	// liveness/readiness checks without requiring a token.
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "service": "orchestrator-go"})
	})

	mux.HandleFunc("POST /revenue-recovery/scan", requireAuth(orchestratorToken, func(w http.ResponseWriter, r *http.Request) {
		result, err := orch.RunFullScan(r.Context())
		if err != nil {
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, result)
	}))

	mux.HandleFunc("GET /revenue-recovery/findings", requireAuth(orchestratorToken, func(w http.ResponseWriter, r *http.Request) {
		result, err := orch.RecordedFindings(r.Context())
		if err != nil {
			writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
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

	orch := orchestrator.New(detectionURL, detectionToken, ledgerURL, ledgerToken)

	mux := newMux(orch, orchestratorToken)
	log.Printf("orchestrator-go listening on %s:%s (detection service at %s, ledger at %s)", bindAddr, port, detectionURL, ledgerURL)
	log.Fatal(http.ListenAndServe(bindAddr+":"+port, mux))
}
