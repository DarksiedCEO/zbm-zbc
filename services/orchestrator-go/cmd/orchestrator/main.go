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
				"dashboard) must present to reach /revenue-recovery/scan.",
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

	mux := http.NewServeMux()

	// /health is intentionally open (no auth) — needed for basic
	// liveness/readiness checks without requiring a token.
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok", "service": "orchestrator-go"})
	})

	mux.HandleFunc("/revenue-recovery/scan", requireAuth(orchestratorToken, func(w http.ResponseWriter, r *http.Request) {
		result, err := orch.RunFullScan(r.Context())
		if err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadGateway)
			_ = json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(result)
	}))

	log.Printf("orchestrator-go listening on %s:%s (detection service at %s, ledger at %s)", bindAddr, port, detectionURL, ledgerURL)
	log.Fatal(http.ListenAndServe(bindAddr+":"+port, mux))
}
