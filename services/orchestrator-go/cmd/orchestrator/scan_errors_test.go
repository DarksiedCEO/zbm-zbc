package main

// Fix wave 3 (AEGIS D3 + fail-fast):
//   - errors must name the upstream that actually failed (the shared doJSON
//     used to label every failure "detection-py", ledger ones included);
//   - an error returned to an HTTP caller (and shown by the dashboard) must
//     not carry internal URLs / host:port or upstream response bodies: the
//     details are logged server-side under a correlation id, the caller gets
//     a generic message plus that id;
//   - with the ledger down, a scan used to make every detection call first
//     and only fail at the first ledger append. It now checks the ledger
//     (GET /ledger/verify) before any detection call and fails fast with 502.

import (
	"bytes"
	"encoding/json"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/orchestrator"
)

// countingDetection records every request and serves empty fixture lists,
// empty finding lists and an empty overlap map, so a scan can complete.
func countingDetection(t *testing.T, rec *recorder) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec.add(r.Method + " " + r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasPrefix(r.URL.Path, "/fixtures/"):
			_, _ = w.Write([]byte(`[]`))
		case strings.HasSuffix(r.URL.Path, "/detect"):
			_, _ = w.Write([]byte(`{"findings":[]}`))
		case r.URL.Path == "/correlation/overlaps":
			_, _ = w.Write([]byte(`{}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// deadURL is the URL of a server that has been shut down: connecting to it
// is refused, exactly like a ledger process that is not running.
func deadURL(t *testing.T) string {
	t.Helper()
	srv := httptest.NewServer(http.NotFoundHandler())
	u := srv.URL
	srv.Close()
	return u
}

func hostPort(t *testing.T, raw string) string {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u.Host
}

// captureLog redirects the standard logger for the duration of the test.
func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev, prevFlags := log.Writer(), log.Flags()
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(prev); log.SetFlags(prevFlags) })
	return &buf
}

func scanServer(t *testing.T, detectionURL, ledgerURL string) *httptest.Server {
	t.Helper()
	orch := orchestrator.New(detectionURL, "d", ledgerURL, "l")
	srv := httptest.NewServer(newMux(orch, routeTestToken))
	t.Cleanup(srv.Close)
	return srv
}

type errorBody struct {
	Error         string `json:"error"`
	CorrelationID string `json:"correlation_id"`
}

func assertNoInternalAddress(t *testing.T, body string, hosts ...string) {
	t.Helper()
	for _, h := range hosts {
		if strings.Contains(body, h) {
			t.Errorf("response leaks internal address %q: %s", h, body)
		}
	}
	for _, s := range []string{"127.0.0.1", "localhost", "http://", "dial tcp"} {
		if strings.Contains(body, s) {
			t.Errorf("response leaks %q: %s", s, body)
		}
	}
}

func TestScanRoute_LedgerDown_FailsFastWith502AndMakesNoDetectionCalls(t *testing.T) {
	logs := captureLog(t)
	detRec := &recorder{}
	det := countingDetection(t, detRec)
	ledger := deadURL(t)
	srv := scanServer(t, det.URL, ledger)

	start := time.Now()
	code, body := do(t, http.MethodPost, srv.URL+"/revenue-recovery/scan", routeTestToken)
	elapsed := time.Since(start)

	if code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d: %s", code, body)
	}
	if calls := detRec.all(); len(calls) != 0 {
		t.Errorf("ledger is down, yet the scan made %d detection call(s): %v", len(calls), calls)
	}
	if elapsed > 2*time.Second {
		t.Errorf("fail-fast took %v", elapsed)
	}
	var eb errorBody
	if err := json.Unmarshal([]byte(body), &eb); err != nil {
		t.Fatalf("error body is not JSON: %s", body)
	}
	if !strings.Contains(eb.Error, "ledger-rust") || strings.Contains(eb.Error, "detection-py") {
		t.Errorf("error must name ledger-rust (and not detection-py): %q", eb.Error)
	}
	if eb.CorrelationID == "" {
		t.Errorf("error response has no correlation_id: %s", body)
	}
	assertNoInternalAddress(t, body, hostPort(t, ledger), hostPort(t, det.URL))
	// The details are logged server-side, under the same correlation id.
	if !strings.Contains(logs.String(), eb.CorrelationID) || !strings.Contains(logs.String(), hostPort(t, ledger)) {
		t.Errorf("server log must carry the correlation id and the real detail; log:\n%s", logs.String())
	}
}

func TestScanRoute_TamperedLedgerFailsBeforeAnyDetectionCall(t *testing.T) {
	captureLog(t)
	detRec := &recorder{}
	det := countingDetection(t, detRec)
	ledger := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/ledger/verify" {
			t.Errorf("tampered ledger must not be written to: %s %s", r.Method, r.URL.Path)
		}
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"valid":false,"error":"ChainBroken { at_seq: 3, reason: \"stored hash does not match\" }"}`))
	}))
	t.Cleanup(ledger.Close)
	srv := scanServer(t, det.URL, ledger.URL)

	code, body := do(t, http.MethodPost, srv.URL+"/revenue-recovery/scan", routeTestToken)
	if code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d: %s", code, body)
	}
	if len(detRec.all()) != 0 {
		t.Errorf("scan ran detection against a ledger that fails verification: %v", detRec.all())
	}
	if !strings.Contains(body, "LEDGER INTEGRITY FAILURE") || !strings.Contains(body, "ChainBroken") {
		t.Errorf("the integrity verdict must reach the caller: %s", body)
	}
	assertNoInternalAddress(t, body, hostPort(t, ledger.URL), hostPort(t, det.URL))
}

// An upstream's own error body (which may carry its internal addresses or
// data) is logged, never relayed; the error names the right service.
func TestScanRoute_DetectionErrorNamesDetectionAndDoesNotRelayItsBody(t *testing.T) {
	logs := captureLog(t)
	det := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"detail":"upstream at http://10.9.8.7:8000 exploded"}`))
	}))
	t.Cleanup(det.Close)
	ledger := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"valid":true,"entries":0}`))
	}))
	t.Cleanup(ledger.Close)
	srv := scanServer(t, det.URL, ledger.URL)

	code, body := do(t, http.MethodPost, srv.URL+"/revenue-recovery/scan", routeTestToken)
	if code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d: %s", code, body)
	}
	if !strings.Contains(body, "detection-py") || strings.Contains(body, "ledger-rust") {
		t.Errorf("error must name detection-py: %s", body)
	}
	assertNoInternalAddress(t, body, "10.9.8.7", hostPort(t, ledger.URL), hostPort(t, det.URL))
	if !strings.Contains(logs.String(), "10.9.8.7") {
		t.Errorf("the upstream body must still be logged server-side; log:\n%s", logs.String())
	}
}

// With the ledger failing mid-scan (after the pre-check), the error is
// labeled ledger-rust — it used to say "detection-py returned 500".
func TestScanRoute_LedgerAppendFailureIsLabeledLedger(t *testing.T) {
	captureLog(t)
	det := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/fixtures/orders":
			_, _ = w.Write([]byte(`[{"order_id":"o1"}]`))
		case strings.HasPrefix(r.URL.Path, "/fixtures/"):
			_, _ = w.Write([]byte(`[]`))
		case r.URL.Path == "/agents/platform-integration/detect":
			_, _ = w.Write([]byte(`{"findings":[{"finding_id":"platform-c-x","agent_id":"platform-integration-v1","leak_category":"platform_integration_gap","entity_type":"platform","entity_id":"c:x","customer_id":"c","cause_certainty":"named","cause_description":"d","recoverable_value":null,"detected_at":"2026-09-24T00:00:00Z"}]}`))
		case strings.HasSuffix(r.URL.Path, "/detect"):
			_, _ = w.Write([]byte(`{"findings":[]}`))
		default:
			_, _ = w.Write([]byte(`{}`))
		}
	}))
	t.Cleanup(det.Close)
	ledger := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/ledger/verify" {
			_, _ = w.Write([]byte(`{"valid":true,"entries":0}`))
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"error":"failed to persist entry: disk full at /srv/ledger"}`))
	}))
	t.Cleanup(ledger.Close)
	srv := scanServer(t, det.URL, ledger.URL)

	code, body := do(t, http.MethodPost, srv.URL+"/revenue-recovery/scan", routeTestToken)
	if code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d: %s", code, body)
	}
	if !strings.Contains(body, "ledger-rust") || strings.Contains(body, "detection-py") {
		t.Errorf("a ledger append failure must be labeled ledger-rust: %s", body)
	}
	if strings.Contains(body, "/srv/ledger") {
		t.Errorf("upstream body relayed to the caller: %s", body)
	}
}

// An empty ledger is a valid chain (ledger-rust answers 200 valid:true,
// entries:0): the pre-check passes and a scan runs to completion.
func TestScanRoute_EmptyLedgerIsValidAndTheScanRuns(t *testing.T) {
	detRec := &recorder{}
	det := countingDetection(t, detRec)
	ledger := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/ledger/verify" {
			t.Errorf("unexpected ledger call %s %s", r.Method, r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"valid":true,"entries":0}`))
	}))
	t.Cleanup(ledger.Close)
	srv := scanServer(t, det.URL, ledger.URL)

	code, body := do(t, http.MethodPost, srv.URL+"/revenue-recovery/scan", routeTestToken)
	if code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", code, body)
	}
	if len(detRec.all()) == 0 {
		t.Error("scan made no detection calls")
	}
}

// The read route sanitizes its errors the same way.
func TestFindingsRoute_LedgerDownIsGeneric502WithCorrelationID(t *testing.T) {
	captureLog(t)
	detRec := &recorder{}
	det := countingDetection(t, detRec)
	ledger := deadURL(t)
	srv := scanServer(t, det.URL, ledger)
	code, body := do(t, http.MethodGet, srv.URL+"/revenue-recovery/findings", routeTestToken)
	if code != http.StatusBadGateway {
		t.Fatalf("want 502, got %d: %s", code, body)
	}
	var eb errorBody
	_ = json.Unmarshal([]byte(body), &eb)
	if eb.CorrelationID == "" || !strings.Contains(eb.Error, "ledger-rust") {
		t.Errorf("want a ledger-rust error with a correlation id: %s", body)
	}
	assertNoInternalAddress(t, body, hostPort(t, ledger), hostPort(t, det.URL))
	if len(detRec.all()) != 0 {
		t.Errorf("read route called detection: %v", detRec.all())
	}
}
