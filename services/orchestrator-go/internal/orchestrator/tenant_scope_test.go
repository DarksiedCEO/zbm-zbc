package orchestrator

// AEGIS tenant-scope review (139ac79), MED-1 and MED-2. A tenant's view of
// the recorded findings (ADR 0021) must hold only that tenant's excluded
// scans and latest-scan-uncounted entry — tested here on a ledger whose scans
// of two tenants interleave — and every field of the tenant result must be a
// deliberate decision: per tenant, or ledger-wide.

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/client"
)

// tenantFinding is validFinding recorded for tenant (its id derived from it).
func tenantFinding(tenant, agent, etype, eid, period, amount string) client.Finding {
	f := validFinding(agent, etype, eid, period, amount)
	f.ClientID = tenant
	f.FindingID = ComputeFindingID(tenant, agent, etype, eid, period)
	return f
}

// readScoped reads entries once per scope, through RecordedFindings.
func readScoped(t *testing.T, entries string, scope FindingsScope) *RecordedFindingsResult {
	t.Helper()
	var all []json.RawMessage
	_ = json.Unmarshal([]byte(entries), &all)
	fl := &fakeLedger{entries: entries, verify: fmt.Sprintf(`{"valid":true,"entries":%d}`, len(all))}
	o := New(failingDetection(t).URL, "d", fl.server(t).URL, "l")
	res, err := o.RecordedFindings(context.Background(), scope)
	if err != nil {
		t.Fatalf("recorded findings %+v: %v", scope, err)
	}
	return res
}

func excludedIDs(res *RecordedFindingsResult) string {
	var ids []string
	for _, s := range res.ExcludedScans {
		ids = append(ids, s.ClientID+":"+s.ScanID)
	}
	return strings.Join(ids, ",")
}

// MED-1: two tenants whose scans interleave in the ledger, each with scans
// that do not count. acme: a counted scan, then a later scan that completed
// but whose finding count disagrees with its completion record (excluded as
// inconsistent, and acme's latest_scan_uncounted). bravo: a counted scan, a
// scan that never completed, and an aborted one — bravo has no
// latest_scan_uncounted. Each tenant's view must hold exactly its own
// excluded scans and its own latest_scan_uncounted entry (none for bravo).
// Kills mutation M6 (excluded_scans unfiltered) and M7
// (latest_scan_uncounted unfiltered).
func TestForClient_ExcludedAndUncountedScansStayWithTheirTenant(t *testing.T) {
	acmeOld := tenantFinding("acme", "discount-misuse-v1", "order", "o1", "", "4.50")
	acmeNew := tenantFinding("acme", "discount-misuse-v1", "order", "o2", "", "5.00")
	bravoF := tenantFinding("bravo", "renewal-never-triggered-v1", "subscription", "s1", "2026-05-15", "39.00")
	bravoLost := tenantFinding("bravo", "discount-misuse-v1", "order", "o9", "", "9.00")

	a1 := buildScan(t, scanID(1), "acme", []client.Finding{acmeOld}, nil)
	b2 := buildScan(t, scanID(2), "bravo", []client.Finding{bravoF}, nil)
	b3 := buildScan(t, scanID(3), "bravo", []client.Finding{bravoLost}, func(evs []client.EventInput) []client.EventInput {
		return evs[:2] // never completed
	})
	a4 := buildScan(t, scanID(4), "acme", []client.Finding{acmeNew}, func(evs []client.EventInput) []client.EventInput {
		evs[len(evs)-1].Summary = strings.Replace(evs[len(evs)-1].Summary, "n=1", "n=2", 1) // count disagrees
		return evs
	})
	b5 := buildScan(t, scanID(5), "bravo", []client.Finding{bravoLost}, func(evs []client.EventInput) []client.EventInput {
		return append(evs[:len(evs)-1:len(evs)-1], abortedEvent(scanID(5), "bravo", "x"))
	})
	// Interleave: every scan's events are kept in their own order, but the
	// scans overlap in the ledger (round robin over the five scans).
	c := &chain{}
	for lists := [][]client.EventInput{a1, b2, b3, a4, b5}; ; {
		more := false
		for i := range lists {
			if len(lists[i]) > 0 {
				c.event(lists[i][0])
				lists[i], more = lists[i][1:], true
			}
		}
		if !more {
			break
		}
	}
	entries := c.json(t)

	all := readScoped(t, entries, FindingsScope{AllTenants: true})
	want := "bravo:" + scanID(3) + ",acme:" + scanID(4) + ",bravo:" + scanID(5)
	if got := excludedIDs(all); got != want {
		t.Fatalf("operator view excluded %s, want %s", got, want)
	}
	if len(all.LatestScanUncounted) != 1 || all.LatestScanUncounted["acme"] != scanID(4) || len(all.Scans) != 2 {
		t.Fatalf("operator view: uncounted %v scans %+v", all.LatestScanUncounted, all.Scans)
	}

	acme := readScoped(t, entries, FindingsScope{ClientID: "acme"})
	if got := excludedIDs(acme); got != "acme:"+scanID(4) {
		t.Errorf("acme's excluded scans: %s", got)
	}
	if len(acme.LatestScanUncounted) != 1 || acme.LatestScanUncounted["acme"] != scanID(4) {
		t.Errorf("acme's latest_scan_uncounted: %v", acme.LatestScanUncounted)
	}
	if len(acme.Findings) != 1 || acme.Findings[0].FindingID != acmeOld.FindingID || acme.Findings[0].PresentInLatestScan {
		t.Errorf("acme's findings (the older scan is not current): %+v", acme.Findings)
	}

	bravo := readScoped(t, entries, FindingsScope{ClientID: "bravo"})
	if got := excludedIDs(bravo); got != "bravo:"+scanID(3)+",bravo:"+scanID(5) {
		t.Errorf("bravo's excluded scans: %s", got)
	}
	if bravo.LatestScanUncounted == nil || len(bravo.LatestScanUncounted) != 0 {
		t.Errorf("bravo's latest_scan_uncounted holds another tenant's entry: %v", bravo.LatestScanUncounted)
	}
	if len(bravo.Findings) != 1 || bravo.Findings[0].FindingID != bravoF.FindingID || !bravo.Findings[0].PresentInLatestScan {
		t.Errorf("bravo's findings: %+v", bravo.Findings)
	}

	// Neither view names a scan or a tenant of the other, in any field.
	for name, v := range map[string]*RecordedFindingsResult{"acme": acme, "bravo": bravo} {
		b, err := json.Marshal(v)
		if err != nil {
			t.Fatal(err)
		}
		other, foreign := "bravo", []int{2, 3, 5}
		if name == "bravo" {
			other, foreign = "acme", []int{1, 4}
		}
		if strings.Contains(string(b), `"`+other+`"`) || strings.Contains(string(b), other+"|") {
			t.Errorf("%s's view names %s: %s", name, other, b)
		}
		for _, n := range foreign {
			if strings.Contains(string(b), scanID(n)) {
				t.Errorf("%s's view holds scan %d of %s", name, n, other)
			}
		}
	}
}

// tenantViewFields classifies every field of RecordedFindingsResult for the
// tenant view (MED-2, ADR 0021). A field added to the struct and not listed
// here fails TestForClient_EveryResultFieldIsClassified: decide whether it is
//
//	scope       set from the request (scope, scope_client_id)
//	perTenant   filtered to the tenant or recomputed from its rows; with no
//	            rows for the tenant it is empty/zero (non_live_data_source:
//	            true, "nothing live is shown")
//	ledgerWide  describes the whole ledger and is shown to every tenant as is
//	            (ADR 0021 "Residuals" lists these; adding one is a decision
//	            for Andre, not a default)
//
// and write it in forClient.
var tenantViewFields = map[string]string{
	"Scope":                       "scope",
	"ScopeClientID":               "scope",
	"Findings":                    "perTenant",
	"OverlappingClaims":           "perTenant",
	"Scans":                       "perTenant",
	"ExcludedScans":               "perTenant",
	"FindingEntriesTotal":         "perTenant",
	"LegacyFindingEntriesIgnored": "perTenant",
	"LegacyFindings":              "perTenant",
	"NonLiveDataSource":           "perTenant",
	"LatestScanUncounted":         "perTenant",
	"LedgerEntriesTotal":          "ledgerWide",
	"LedgerTotalSource":           "ledgerWide",
	"LedgerEntriesRead":           "ledgerWide",
	"LedgerVerify":                "ledgerWide",
}

// fill sets every settable field reachable from v to a non-zero value, with
// every string "bravo" (so every row belongs to bravo). Fields this test has
// never seen are filled too: that is what makes an unclassified field leak
// visibly instead of silently.
func fill(v reflect.Value, depth int) {
	if depth > 4 {
		return
	}
	switch v.Kind() {
	case reflect.String:
		v.SetString("bravo")
	case reflect.Bool:
		v.SetBool(true)
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		v.SetInt(7)
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		v.SetUint(7)
	case reflect.Float32, reflect.Float64:
		v.SetFloat(7)
	case reflect.Pointer:
		p := reflect.New(v.Type().Elem())
		fill(p.Elem(), depth+1)
		v.Set(p)
	case reflect.Slice:
		s := reflect.MakeSlice(v.Type(), 1, 1)
		fill(s.Index(0), depth+1)
		v.Set(s)
	case reflect.Map:
		m := reflect.MakeMap(v.Type())
		k, e := reflect.New(v.Type().Key()).Elem(), reflect.New(v.Type().Elem()).Elem()
		fill(k, depth+1)
		fill(e, depth+1)
		m.SetMapIndex(k, e)
		v.Set(m)
	case reflect.Struct:
		for i := 0; i < v.NumField(); i++ {
			if v.Field(i).CanSet() {
				fill(v.Field(i), depth+1)
			}
		}
	}
}

// MED-2: every field of the tenant result is classified, and forClient
// honours the classification on a result where EVERY field (known or not)
// holds another tenant's data: ledger-wide fields pass through unchanged,
// every other field is empty for a tenant with no rows. A field added later
// and not written in forClient's allowlist arrives zero — and this test fails
// on it until it is classified.
func TestForClient_EveryResultFieldIsClassified(t *testing.T) {
	typ := reflect.TypeOf(RecordedFindingsResult{})
	seen := map[string]bool{}
	for i := 0; i < typ.NumField(); i++ {
		f := typ.Field(i)
		seen[f.Name] = true
		if _, ok := tenantViewFields[f.Name]; !ok {
			t.Errorf("RecordedFindingsResult.%s (json %q) is not classified for the tenant view: add it to "+
				"tenantViewFields as perTenant or ledgerWide, and write it in forClient", f.Name, f.Tag.Get("json"))
		}
	}
	for name := range tenantViewFields {
		if !seen[name] {
			t.Errorf("tenantViewFields lists %s, which RecordedFindingsResult no longer has", name)
		}
	}

	var in RecordedFindingsResult
	fill(reflect.ValueOf(&in).Elem(), 0)
	out := reflect.ValueOf(in.forClient("acme")).Elem()
	src := reflect.ValueOf(in)
	for i := 0; i < typ.NumField(); i++ {
		name, got := typ.Field(i).Name, out.Field(i)
		switch tenantViewFields[name] {
		case "ledgerWide":
			if !reflect.DeepEqual(got.Interface(), src.Field(i).Interface()) {
				t.Errorf("ledger-wide %s changed in the tenant view: %v", name, got.Interface())
			}
		case "perTenant":
			empty := got.IsZero()
			switch got.Kind() {
			case reflect.Slice, reflect.Map:
				empty = !got.IsNil() && got.Len() == 0 // [] / {} on the wire, never null
			}
			if name == "NonLiveDataSource" {
				empty = got.Bool() // no counted scan: reported non-live
			}
			if !empty {
				t.Errorf("per-tenant %s carries another tenant's data into acme's view: %v", name, got.Interface())
			}
		case "scope":
		default: // unclassified: must at least not leak
			if !got.IsZero() {
				t.Errorf("unclassified %s reaches the tenant view: %v", name, got.Interface())
			}
		}
	}
	if in.forClient("acme").Scope != FindingsScopeClient || *in.forClient("acme").ScopeClientID != "acme" {
		t.Errorf("scope fields not set from the request")
	}
}
