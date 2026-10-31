package main

// AEGIS A2 M-1 / ADR 0021: GET /revenue-recovery/findings used to return
// every tenant's findings in one response. Findings are now per tenant:
// ?client_id=<tenant> returns that tenant only, ?scope=all is the operator's
// explicit every-tenant view, and a request naming neither is refused before
// the ledger is read. Fakes only (loopback httptest servers).

import (
	"encoding/json"
	"net/http"
	"net/url"
	"strings"
	"testing"
)

type scopedBody struct {
	Scope    string `json:"scope"`
	Findings []struct {
		FindingID string `json:"finding_id"`
		ClientID  string `json:"client_id"`
	} `json:"findings"`
	OverlappingClaims map[string][]struct {
		ClientID string `json:"client_id"`
	} `json:"overlapping_claims"`
	Scans []struct {
		ClientID string `json:"client_id"`
		Findings int    `json:"findings"`
	} `json:"scans"`
	ExcludedScans []struct {
		ClientID string `json:"client_id"`
	} `json:"excluded_scans"`
	FindingEntriesTotal int               `json:"finding_entries_total"`
	LegacyIgnored       int               `json:"legacy_finding_entries_ignored"`
	LegacyFindings      []json.RawMessage `json:"legacy_findings"`
	LatestScanUncounted map[string]string `json:"latest_scan_uncounted"`
	NonLive             bool              `json:"non_live_data_source"`
}

// twoTenantServer: two connected stores, acme and bravo, each scanned once.
func twoTenantServer(t *testing.T) string {
	t.Helper()
	det, led := twoLeakDetection(), newMemLedger()
	det.stores["acme"] = [2]string{"200", storeSnapshotBody("acme", 3)}
	det.stores["bravo"] = [2]string{"200", storeSnapshotBody("bravo", 3)}
	srv := fixWaveServer(t, det, led)
	for _, c := range []string{"acme", "bravo"} {
		if code, _, body := scan(t, srv, "?client_id="+c); code != http.StatusOK {
			t.Fatalf("scan %s = %d %s", c, code, body)
		}
	}
	return srv.URL
}

func getScoped(t *testing.T, base, query string) (int, scopedBody, string) {
	t.Helper()
	code, body := do(t, http.MethodGet, base+"/revenue-recovery/findings"+query, routeTestToken)
	var b scopedBody
	if code == http.StatusOK {
		if err := json.Unmarshal([]byte(body), &b); err != nil {
			t.Fatalf("body: %v: %s", err, body)
		}
	}
	return code, b, body
}

func TestFindingsScope_ClientIDReturnsOnlyThatTenant(t *testing.T) {
	base := twoTenantServer(t)
	_, all, _ := getScoped(t, base, "?scope=all")
	for _, c := range []string{"acme", "bravo"} {
		code, b, body := getScoped(t, base, "?client_id="+c)
		if code != http.StatusOK || b.Scope != "client" {
			t.Fatalf("%s: GET = %d %s", c, code, body)
		}
		other := map[string]string{"acme": "bravo", "bravo": "acme"}[c]
		if strings.Contains(body, other) {
			t.Fatalf("%s's view names %s: %s", c, other, body)
		}
		if len(b.Findings) == 0 || len(b.Scans) != 1 || len(b.OverlappingClaims) == 0 {
			t.Fatalf("%s: findings %d scans %d overlaps %d", c, len(b.Findings), len(b.Scans), len(b.OverlappingClaims))
		}
		for _, f := range b.Findings {
			if f.ClientID != c {
				t.Fatalf("%s's view holds a finding of %s", c, f.ClientID)
			}
		}
		for k, fs := range b.OverlappingClaims {
			if !strings.HasPrefix(k, c+"|") || fs[0].ClientID != c {
				t.Fatalf("%s's view holds claim %s", c, k)
			}
		}
		if b.FindingEntriesTotal != b.Scans[0].Findings {
			t.Fatalf("%s: finding_entries_total %d, its scan has %d", c, b.FindingEntriesTotal, b.Scans[0].Findings)
		}
		// The tenant's view is exactly the operator view's rows for that tenant.
		var want []string
		for _, f := range all.Findings {
			if f.ClientID == c {
				want = append(want, f.FindingID)
			}
		}
		var got []string
		for _, f := range b.Findings {
			got = append(got, f.FindingID)
		}
		if strings.Join(got, ",") != strings.Join(want, ",") {
			t.Fatalf("%s: tenant view %v, operator view for it %v", c, got, want)
		}
	}
	if len(all.Scans) != 2 || all.Scope != "all_tenants" {
		t.Fatalf("operator view: scope %q scans %d", all.Scope, len(all.Scans))
	}
}

func TestFindingsScope_UnknownTenantGetsNothing(t *testing.T) {
	base := twoTenantServer(t)
	code, b, body := getScoped(t, base, "?client_id=charlie")
	if code != http.StatusOK || len(b.Findings) != 0 || len(b.Scans) != 0 || len(b.OverlappingClaims) != 0 ||
		b.FindingEntriesTotal != 0 || !b.NonLive || strings.Contains(body, "acme") || strings.Contains(body, "bravo") {
		t.Fatalf("GET = %d %s", code, body)
	}
}

func TestFindingsScope_NoScopeIsRefusedBeforeTheLedger(t *testing.T) {
	srv, ledgerRec, _ := newTestServer(t)
	code, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings", routeTestToken)
	if code != http.StatusBadRequest || !strings.Contains(body, `"reason":"FINDINGS_SCOPE_REQUIRED"`) {
		t.Fatalf("GET without a scope = %d %s", code, body)
	}
	if n := len(ledgerRec.all()); n != 0 {
		t.Fatalf("an unscoped request reached the ledger (%d calls)", n)
	}
}

func TestFindingsScope_BadParametersAreRefusedBeforeTheLedger(t *testing.T) {
	srv, ledgerRec, _ := newTestServer(t)
	for _, q := range []string{
		"?client_id=",
		"?client_id=" + url.QueryEscape("a b"),
		"?client_id=" + url.QueryEscape("../acme"),
		"?client_id=-acme",
		"?client_id=" + strings.Repeat("a", 65),
		"?client_id=" + url.QueryEscape("acme\n"),
		"?client_id=acme&client_id=bravo",
		"?client_id=acme&scope=all",
		"?scope=",
		"?scope=ALL",
		"?scope=all&scope=all",
		"?tenant=acme",
		"?client_id=acme&as_of=2026-10-01T00:00:00Z",
	} {
		code, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings"+q, routeTestToken)
		if code != http.StatusBadRequest || !strings.Contains(body, `"error"`) {
			t.Errorf("%s: want 400, got %d %s", q, code, body)
		}
	}
	if n := len(ledgerRec.all()); n != 0 {
		t.Fatalf("a refused request reached the ledger (%d calls)", n)
	}
	// The longest valid id is accepted.
	if code, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings?client_id="+strings.Repeat("a", 64), routeTestToken); code != http.StatusOK {
		t.Fatalf("64-character client_id: %d %s", code, body)
	}
}

// Legacy (pre-Oct-6) ledger findings carry no tenant: a tenant's view never
// shows them; the operator's view still lists them, labelled.
func TestFindingsScope_LegacyFindingsOnlyInTheOperatorView(t *testing.T) {
	srv, _, _ := newTestServer(t)
	code, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings?client_id="+tenant, routeTestToken)
	if code != http.StatusOK || !strings.Contains(body, `"legacy_finding_entries_ignored":0`) ||
		!strings.Contains(body, `"legacy_findings":[]`) || strings.Contains(body, "disc-ord_1007") {
		t.Fatalf("tenant view: %d %s", code, body)
	}
	code, body = do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings?scope=all", routeTestToken)
	if code != http.StatusOK || !strings.Contains(body, `"legacy_finding_entries_ignored":1`) ||
		!strings.Contains(body, "disc-ord_1007") {
		t.Fatalf("operator view: %d %s", code, body)
	}
}
