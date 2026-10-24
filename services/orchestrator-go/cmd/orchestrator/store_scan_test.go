package main

// ADR 0020: a scan of a connected Shopify store reads detection-py's held
// snapshot; the orchestrator never calls a store. Fakes only (no network
// beyond loopback httptest servers, no wall-clock assertions).

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

const snapID = "snap-0123456789abcdef01234567"

func storeSnapshotBody(client string, n int) string {
	var orders []string
	for i := 0; i < n; i++ {
		orders = append(orders, `{"order_id":"o`+string(rune('0'+i))+`","customer_id":"not-collected",`+
			`"placed_at":"2026-10-20T00:00:00Z","status":"paid","line_items":[],"discounts":[],"affiliate":null,`+
			`"source_platform":"shopify","recovery_attempted":false}`)
	}
	return `{"client_id":"` + client + `","snapshot_id":"` + snapID + `","data_source":"shopify",` +
		`"orders":[` + strings.Join(orders, ",") + `],` +
		`"coverage":{"data_source":"shopify","agents":{"discount-misuse-v1":{"covered":true,"why":"x"}}}}`
}

func TestStore_NotConnectedIsRefusedWithNothingWritten(t *testing.T) {
	det, led := twoLeakDetection(), newMemLedger()
	srv := fixWaveServer(t, det, led)
	code, _, body := scan(t, srv, "?client_id=acme")
	if code != http.StatusConflict || !strings.Contains(body, `"reason":"NOT_CONNECTED"`) {
		t.Fatalf("scan = %d %s", code, body)
	}
	if got := det.requests(); len(got) != 1 || got[0] != "POST /stores/snapshot" {
		t.Fatalf("detection calls %v", got)
	}
	if n := len(led.snapshot()); n != 0 {
		t.Fatalf("ledger entries written: %d", n)
	}
}

func TestStore_RefusalsMapToStatuses(t *testing.T) {
	for reason, want := range map[string]int{
		"NOT_SYNCED": 409, "SNAPSHOT_STALE": 409, "STORE_READS_DISABLED": 503, "STORE_CONFIG_INVALID": 503,
		"STORE_BUDGET_EXHAUSTED": 429,
	} {
		det, led := twoLeakDetection(), newMemLedger()
		status := "409"
		switch want {
		case 503:
			status = "503"
		case 429:
			status = "429"
		}
		det.stores["acme"] = [2]string{status, `{"detail":{"reason":"` + reason + `","message":"m"}}`}
		srv := fixWaveServer(t, det, led)
		code, _, body := scan(t, srv, "?client_id=acme")
		if code != want || !strings.Contains(body, `"reason":"`+reason+`"`) {
			t.Errorf("%s: scan = %d %s", reason, code, body)
		}
		if n := len(led.snapshot()); n != 0 {
			t.Errorf("%s: ledger entries written: %d", reason, n)
		}
	}
}

func TestStore_ConnectedSnapshotIsScannedAndRecorded(t *testing.T) {
	det, led := twoLeakDetection(), newMemLedger()
	det.stores["acme"] = [2]string{"200", storeSnapshotBody("acme", 3)}
	srv := fixWaveServer(t, det, led)
	code, _, body := scan(t, srv, "?client_id=acme")
	if code != http.StatusOK {
		t.Fatalf("scan = %d %s", code, body)
	}
	var res struct {
		ClientID        string          `json:"client_id"`
		DataSource      string          `json:"data_source"`
		NonLive         bool            `json:"non_live_data_source"`
		StoreSnapshotID string          `json:"store_snapshot_id"`
		StoreCoverage   json.RawMessage `json:"store_coverage"`
		Findings        []struct {
			ClientID string `json:"client_id"`
			AgentID  string `json:"agent_id"`
		} `json:"findings"`
		AgentsRun []string `json:"agents_run"`
	}
	if err := json.Unmarshal([]byte(body), &res); err != nil {
		t.Fatal(err)
	}
	if res.ClientID != "acme" || res.DataSource != "shopify:"+snapID || res.NonLive || res.StoreSnapshotID != snapID {
		t.Fatalf("result header wrong: %s", body)
	}
	if !strings.Contains(string(res.StoreCoverage), `"discount-misuse-v1"`) {
		t.Fatalf("coverage not passed through: %s", res.StoreCoverage)
	}
	if len(res.AgentsRun) != 8 || len(res.Findings) == 0 {
		t.Fatalf("agents %v findings %d", res.AgentsRun, len(res.Findings))
	}
	for _, f := range res.Findings {
		if f.ClientID != "acme" {
			t.Fatalf("finding for %s", f.ClientID)
		}
	}
	for _, r := range det.requests() {
		if strings.HasPrefix(r, "GET /fixtures/") {
			t.Fatalf("a store scan read fixtures: %v", det.requests())
		}
	}
	if got := det.batchSizes("/agents/discount-misuse/detect"); len(got) != 1 || got[0] != 3 {
		t.Fatalf("discount batch sizes %v", got)
	}
	if got := det.batchSizes("/agents/renewal-never-triggered/detect"); len(got) != 1 || got[0] != 0 {
		t.Fatalf("renewal batch sizes %v (the store source feeds no subscriptions)", got)
	}
	started := ""
	for _, e := range led.snapshot() {
		if e["event_type"] == "rr_scan_started" {
			started = e["summary"].(string)
		}
	}
	if !strings.Contains(started, "src=shopify:"+snapID+" fixture=0 defaulted=0") {
		t.Fatalf("started record: %q", started)
	}
	code, fbody := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings", routeTestToken)
	if code != 200 || !strings.Contains(fbody, `"non_live_data_source":false`) ||
		!strings.Contains(fbody, `"data_source":"shopify:`+snapID+`"`) {
		t.Fatalf("findings view: %d %s", code, fbody)
	}
}

func TestStore_SnapshotForAnotherTenantFailsTheScan(t *testing.T) {
	for name, body := range map[string]string{
		"other tenant":  storeSnapshotBody("bravo", 1),
		"bad id":        strings.Replace(storeSnapshotBody("acme", 1), snapID, "snap-XYZ", 1),
		"no coverage":   strings.Replace(storeSnapshotBody("acme", 1), `"coverage":{`, `"coverage_x":{`, 1),
		"other source":  strings.Replace(storeSnapshotBody("acme", 1), `"data_source":"shopify",`, `"data_source":"amazon",`, 1),
		"not json":      "<html>",
		"500 no reason": "",
	} {
		det, led := twoLeakDetection(), newMemLedger()
		status := "200"
		if name == "500 no reason" {
			status = "500"
		}
		det.stores["acme"] = [2]string{status, body}
		srv := fixWaveServer(t, det, led)
		code, _, out := scan(t, srv, "?client_id=acme")
		if code != http.StatusBadGateway {
			t.Errorf("%s: scan = %d %s", name, code, out)
		}
		if n := len(led.snapshot()); n != 0 {
			t.Errorf("%s: ledger entries written: %d", name, n)
		}
	}
}

func TestStore_FixtureScanUnchanged(t *testing.T) {
	det, led := twoLeakDetection(), newMemLedger()
	srv := fixWaveServer(t, det, led)
	code, _, body := scan(t, srv, "")
	if code != 200 || !strings.Contains(body, `"data_source":"fixtures"`) || !strings.Contains(body, `"non_live_data_source":true`) ||
		strings.Contains(body, "store_snapshot_id") {
		t.Fatalf("fixture scan: %d %s", code, body)
	}
	for _, r := range det.requests() {
		if r == "POST /stores/snapshot" {
			t.Fatal("the fixture tenant asked for a store snapshot")
		}
	}
}
