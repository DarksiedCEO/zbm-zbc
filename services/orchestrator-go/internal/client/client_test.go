package client

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// This fixture is the REAL captured response body from the Python
// detection-py /agents/affiliate-coupon-extension/detect endpoint
// (captured via FastAPI TestClient against the actual agent + fixtures,
// see build log) — not a guessed shape. Updated Sep 24 2026 for README
// gap #6: amount_usd is now the two-decimal JSON string detection-py
// actually emits ("120.00"), not the old float 120.0.
const realAffiliateFindingsJSON = `{
  "findings": [
    {
      "finding_id": "aff-ord_1002",
      "agent_id": "affiliate-coupon-extension-v1",
      "leak_category": "affiliate_coupon_extension",
      "entity_type": "order",
      "entity_id": "ord_1002",
      "customer_id": "cust_2",
      "cause_certainty": "named",
      "cause_description": "Affiliate commission honored 265.0h after click, 11.0x the stated 24h attribution window.",
      "recoverable_value": {
        "amount_usd": "120.00",
        "classification": "attributed",
        "confidence": "high"
      },
      "detected_at": "2026-09-22T03:59:20.375950Z"
    }
  ]
}`

func TestDetectAffiliateCouponExtension_ParsesRealContractShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/agents/affiliate-coupon-extension/detect" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(realAffiliateFindingsJSON))
	}))
	defer srv.Close()

	c := NewDetectionClient(srv.URL, "test-token")
	findings, err := c.DetectAffiliateCouponExtension(context.Background(), "fixture-pool", []Order{{"order_id": "ord_1002"}})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(findings) != 1 {
		t.Fatalf("expected 1 finding, got %d", len(findings))
	}
	f := findings[0]
	if f.EntityID != "ord_1002" || f.AgentID != "affiliate-coupon-extension-v1" {
		t.Errorf("unexpected finding contents: %+v", f)
	}
	if f.RecoverableValue == nil || f.RecoverableValue.AmountUSD.String() != "120.00" {
		t.Errorf("expected recoverable value \"120.00\", got %+v", f.RecoverableValue)
	}
	if f.RecoverableValue.Classification == "" || f.RecoverableValue.Confidence == "" {
		t.Errorf("Decision 2 violation: LabeledValue missing classification/confidence in %+v", f.RecoverableValue)
	}
}

func TestDetectionClient_NonOKStatusReturnsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"detail":"boom"}`))
	}))
	defer srv.Close()

	c := NewDetectionClient(srv.URL, "test-token")
	_, err := c.DetectDiscountMisuse(context.Background(), "fixture-pool", nil)
	if err == nil {
		t.Fatal("expected error on 500 response, got nil")
	}
}

func TestCorrelationOverlaps_ParsesMapOfFindings(t *testing.T) {
	body := `{
		"ord_1007": [
			{"finding_id":"aff-ord_1007","agent_id":"affiliate-coupon-extension-v1","leak_category":"affiliate_coupon_extension","entity_type":"order","entity_id":"ord_1007","customer_id":"cust_3","cause_certainty":"named","cause_description":"x","recoverable_value":{"amount_usd":"150.00","classification":"attributed","confidence":"high"},"detected_at":"2026-09-22T04:00:00Z"},
			{"finding_id":"disc-ord_1007","agent_id":"discount-misuse-v1","leak_category":"discount_misuse","entity_type":"order","entity_id":"ord_1007","customer_id":"cust_3","cause_certainty":"named","cause_description":"y","recoverable_value":{"amount_usd":"54.38","classification":"observed","confidence":"very_high"},"detected_at":"2026-09-22T04:00:01Z"}
		]
	}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/correlation/overlaps" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	defer srv.Close()

	c := NewDetectionClient(srv.URL, "test-token")
	overlaps, err := c.CorrelationOverlaps(context.Background(), []Finding{})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	claims, ok := overlaps["ord_1007"]
	if !ok || len(claims) != 2 {
		t.Fatalf("expected 2 overlapping claims on ord_1007, got %+v", overlaps)
	}
	agentIDs := map[string]bool{}
	for _, f := range claims {
		agentIDs[f.AgentID] = true
	}
	if !agentIDs["affiliate-coupon-extension-v1"] || !agentIDs["discount-misuse-v1"] {
		t.Errorf("expected both agents represented in overlap, got %+v", agentIDs)
	}
}

func TestFixtureOrders_DecodesGenericOrderShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		out, _ := json.Marshal([]map[string]any{
			{"order_id": "ord_1001", "source_platform": "shopify"},
		})
		_, _ = w.Write(out)
	}))
	defer srv.Close()

	c := NewDetectionClient(srv.URL, "test-token")
	orders, err := c.FixtureOrders(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(orders) != 1 || orders[0]["order_id"] != "ord_1001" {
		t.Fatalf("unexpected orders: %+v", orders)
	}
}

// Proves the client actually sends the configured token — not just that
// call sites compile with a token argument. Without this, a future edit
// that silently drops the Authorization header would go undetected: every
// other test's fake server ignores the header entirely.
func TestDetectionClient_SendsBearerTokenOnEveryRequest(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}))
	defer srv.Close()

	c := NewDetectionClient(srv.URL, "my-real-secret")
	if err := c.Health(context.Background()); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if gotAuth != "Bearer my-real-secret" {
		t.Fatalf("expected Authorization header 'Bearer my-real-secret', got %q", gotAuth)
	}
}

// A client constructed with an empty token must NOT send an Authorization
// header at all — sending "Bearer " with an empty value would be a
// malformed header, not "no auth". (Historical note: this used to be how
// NewLedgerClient was constructed, back when ledger-rust did not enforce
// auth. ledger-rust now requires a token — see
// TestLedgerClient_SendsBearerTokenOnAppend below — but this generic
// empty-token behavior on the shared doJSON plumbing is still worth
// covering on its own.)
func TestDetectionClient_EmptyTokenSendsNoAuthorizationHeader(t *testing.T) {
	var authHeaderPresent bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, authHeaderPresent = r.Header["Authorization"]
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}))
	defer srv.Close()

	c := NewDetectionClient(srv.URL, "")
	if err := c.Health(context.Background()); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if authHeaderPresent {
		t.Fatal("expected no Authorization header when token is empty, but one was sent")
	}
}

// Regression test for the Sep 22 2026 independent review fix: ledger-rust
// now requires a bearer token on every route except /health (it had none
// before). NewLedgerClient's signature changed to take one — this proves
// AppendEvent actually sends it, not just that the constructor compiles
// with a token argument. Without this, a future edit reverting
// NewLedgerClient to send no header would go undetected until the first
// real scan failed every ledger write with 401 in production.
func TestLedgerClient_SendsBearerTokenOnAppend(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"seq":1,"hash":"h","prev_hash":"p"}`))
	}))
	defer srv.Close()

	l := NewLedgerClient(srv.URL, "ledger-real-secret")
	_, err := l.AppendEvent(context.Background(), sampleEvent())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if gotAuth != "Bearer ledger-real-secret" {
		t.Fatalf("expected Authorization header 'Bearer ledger-real-secret', got %q", gotAuth)
	}
}

// Regression test for a bug caught live (Sep 22 2026) by an actual 3-process
// end-to-end run, not by any unit test: Verify() builds its own request
// instead of going through doJSON (needed for its 409-is-not-an-error
// handling) and was missed by the ledger-auth fix above — it sent no
// Authorization header at all and 401'd against the newly-authenticated
// ledger on every real scan.
func TestLedgerClient_SendsBearerTokenOnVerify(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"valid":true,"entries":3}`))
	}))
	defer srv.Close()

	l := NewLedgerClient(srv.URL, "ledger-real-secret")
	result, err := l.Verify(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.Valid || result.Entries != 3 {
		t.Fatalf("unexpected verify result: %+v", result)
	}
	if gotAuth != "Bearer ledger-real-secret" {
		t.Fatalf("expected Authorization header 'Bearer ledger-real-secret', got %q", gotAuth)
	}
}
