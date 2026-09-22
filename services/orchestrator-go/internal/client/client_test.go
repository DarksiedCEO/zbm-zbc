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
// see build log) — not a guessed shape.
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
        "amount_usd": 120.0,
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

	c := NewDetectionClient(srv.URL)
	findings, err := c.DetectAffiliateCouponExtension(context.Background(), []Order{{"order_id": "ord_1002"}})
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
	if f.RecoverableValue == nil || f.RecoverableValue.AmountUSD != 120.0 {
		t.Errorf("expected recoverable value 120.0, got %+v", f.RecoverableValue)
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

	c := NewDetectionClient(srv.URL)
	_, err := c.DetectDiscountMisuse(context.Background(), nil)
	if err == nil {
		t.Fatal("expected error on 500 response, got nil")
	}
}

func TestCorrelationOverlaps_ParsesMapOfFindings(t *testing.T) {
	body := `{
		"ord_1007": [
			{"finding_id":"aff-ord_1007","agent_id":"affiliate-coupon-extension-v1","leak_category":"affiliate_coupon_extension","entity_type":"order","entity_id":"ord_1007","customer_id":"cust_3","cause_certainty":"named","cause_description":"x","recoverable_value":{"amount_usd":150.0,"classification":"attributed","confidence":"high"},"detected_at":"2026-09-22T04:00:00Z"},
			{"finding_id":"disc-ord_1007","agent_id":"discount-misuse-v1","leak_category":"discount_misuse","entity_type":"order","entity_id":"ord_1007","customer_id":"cust_3","cause_certainty":"named","cause_description":"y","recoverable_value":{"amount_usd":55.0,"classification":"observed","confidence":"very_high"},"detected_at":"2026-09-22T04:00:01Z"}
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

	c := NewDetectionClient(srv.URL)
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

	c := NewDetectionClient(srv.URL)
	orders, err := c.FixtureOrders(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(orders) != 1 || orders[0]["order_id"] != "ord_1001" {
		t.Fatalf("unexpected orders: %+v", orders)
	}
}
