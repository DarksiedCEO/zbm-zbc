package main

// ADR 0021: every tenant at once (scope=all) is the operator's view only.
// AEGIS tenant review MED-3: a credential that is not the operator's can be
// mounted only bound to its own tenant (tenantFindings): scope=all is refused
// 403 ALL_TENANTS_FORBIDDEN, another tenant's client_id 403 TENANT_MISMATCH,
// both before the ledger is read; its own client_id works. No such credential
// exists today: newMux mounts the operator token only.

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/orchestrator"
)

func TestFindingsScope_TenantBoundCredentialReadsOnlyItsOwnTenant(t *testing.T) {
	ledgerRec, detRec := &recorder{}, &recorder{}
	orch := orchestrator.New(noDetection(t, detRec).URL, "d", readOnlyLedger(t, ledgerRec).URL, "l")
	const tenantToken = "tenant-bound-test-token"
	mux := http.NewServeMux()
	mux.HandleFunc("GET /revenue-recovery/findings", requireAuth(tenantToken, findingsHandler(orch, tenantFindings(tenant))))
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	code, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings?scope=all", tenantToken)
	if code != http.StatusForbidden || !strings.Contains(body, `"reason":"ALL_TENANTS_FORBIDDEN"`) {
		t.Fatalf("scope=all without the operator credential = %d %s", code, body)
	}
	for _, other := range []string{"acme", tenant + "x", strings.ToUpper(tenant)} {
		code, body = do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings?client_id="+other, tenantToken)
		if code != http.StatusForbidden || !strings.Contains(body, `"reason":"TENANT_MISMATCH"`) || strings.Contains(body, other) {
			t.Fatalf("another tenant's client_id %q = %d %s", other, code, body)
		}
	}
	if n := len(ledgerRec.all()); n != 0 {
		t.Fatalf("a refused request reached the ledger (%d calls)", n)
	}
	code, body = do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings?client_id="+tenant, tenantToken)
	if code != http.StatusOK || !strings.Contains(body, `"scope":"client"`) {
		t.Fatalf("its own client_id = %d %s", code, body)
	}
}

// A non-operator credential that may name any tenant cannot be mounted.
func TestFindingsScope_UnboundAccessCannotBeMounted(t *testing.T) {
	orch := orchestrator.New("http://127.0.0.1:1", "d", "http://127.0.0.1:1", "l")
	for name, a := range map[string]findingsAccess{
		"zero (any tenant, not the operator)": {},
		"operator and a tenant at once":       {allTenants: true, tenant: "acme"},
		"empty tenant":                        tenantFindings(""),
		"invalid tenant":                      tenantFindings("a b"),
	} {
		func() {
			defer func() {
				if recover() == nil {
					t.Errorf("%s: findingsHandler mounted", name)
				}
			}()
			findingsHandler(orch, a)
		}()
	}
}

// Today exactly one credential is accepted — the operator token newMux is
// given — on both authenticated routes; any other bearer is 401 before a
// handler runs. A second credential must arrive with its own tenant binding
// (see the comment at the findings mount in newMux and ADR 0021).
func TestNewMux_OnlyTheOperatorCredentialIsMounted(t *testing.T) {
	srv, ledgerRec, detRec := newTestServer(t)
	for _, tok := range []string{"", "tenant-bound-test-token", routeTestToken + "x", strings.ToUpper(routeTestToken)} {
		for _, r := range [][2]string{
			{http.MethodGet, "/revenue-recovery/findings?client_id=" + tenant},
			{http.MethodGet, "/revenue-recovery/findings?scope=all"},
			{http.MethodPost, "/revenue-recovery/scan?client_id=" + tenant},
		} {
			if code, body := do(t, r[0], srv.URL+r[1], tok); code != http.StatusUnauthorized {
				t.Errorf("%s %s with token %q = %d %s", r[0], r[1], tok, code, body)
			}
		}
	}
	if n, m := len(ledgerRec.all()), len(detRec.all()); n != 0 || m != 0 {
		t.Fatalf("an unauthorised request reached the ledger (%d) or detection (%d)", n, m)
	}
	if code, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings?scope=all", routeTestToken); code != http.StatusOK {
		t.Fatalf("operator scope=all = %d %s", code, body)
	}
}
