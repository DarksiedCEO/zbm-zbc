package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/orchestrator"
)

func main() {
	detectionURL := os.Getenv("DETECTION_SERVICE_URL")
	if detectionURL == "" {
		detectionURL = "http://localhost:8000"
	}
	ledgerURL := os.Getenv("LEDGER_SERVICE_URL")
	if ledgerURL == "" {
		ledgerURL = "http://localhost:8090"
	}
	port := os.Getenv("ORCHESTRATOR_PORT")
	if port == "" {
		port = "8080"
	}

	orch := orchestrator.New(detectionURL, ledgerURL)

	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok", "service": "orchestrator-go"})
	})

	mux.HandleFunc("/revenue-recovery/scan", func(w http.ResponseWriter, r *http.Request) {
		result, err := orch.RunFullScan(r.Context())
		if err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadGateway)
			_ = json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(result)
	})

	log.Printf("orchestrator-go listening on :%s (detection service at %s, ledger at %s)", port, detectionURL, ledgerURL)
	log.Fatal(http.ListenAndServe(":"+port, mux))
}
