package main

// ADR 0021: every tenant at once (scope=all) is the operator's view only. A
// credential that is not the operator's mounts findingsHandler with
// allowAllTenants=false: scope=all is refused 403 before the ledger is read,
// and its own client_id still works.

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/orchestrator"
)

func TestFindingsScope_AllTenantsOnlyForTheOperator(t *testing.T) {
	ledgerRec, detRec := &recorder{}, &recorder{}
	orch := orchestrator.New(noDetection(t, detRec).URL, "d", readOnlyLedger(t, ledgerRec).URL, "l")
	const tenantToken = "tenant-bound-test-token"
	mux := http.NewServeMux()
	mux.HandleFunc("GET /revenue-recovery/findings", requireAuth(tenantToken, findingsHandler(orch, false)))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	code, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings?scope=all", tenantToken)
	if code != http.StatusForbidden || !strings.Contains(body, `"reason":"ALL_TENANTS_FORBIDDEN"`) {
		t.Fatalf("scope=all without the operator credential = %d %s", code, body)
	}
	if n := len(ledgerRec.all()); n != 0 {
		t.Fatalf("a refused scope=all reached the ledger (%d calls)", n)
	}
	code, body = do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings?client_id="+tenant, tenantToken)
	if code != http.StatusOK || !strings.Contains(body, `"scope":"client"`) {
		t.Fatalf("client_id with a non-operator credential = %d %s", code, body)
	}
}
