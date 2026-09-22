package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// DetectionClient calls the Python detection-py REST service. Plain
// REST/JSON per the locked decision (voice session, Sep 21 2026) — no
// gRPC, no generated stubs, deliberately simple for a solo-builder team.
type DetectionClient struct {
	baseURL    string
	token      string // bearer token sent as Authorization; empty means "send nothing"
	httpClient *http.Client
}

// NewDetectionClient builds a client for detection-py. token must match
// that service's ZBM_SERVICE_TOKEN — detection-py fails closed and
// rejects every non-/health request without it. Pass "" only for a
// service (like ledger-rust, via NewLedgerClient) that does not require
// auth; passing "" against a token-requiring service will fail every
// call with 401, loudly, not silently.
func NewDetectionClient(baseURL, token string) *DetectionClient {
	return &DetectionClient{
		baseURL: baseURL,
		token:   token,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

func (c *DetectionClient) doJSON(ctx context.Context, method, path string, body any, out any) error {
	var reqBody io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("marshal request body: %w", err)
		}
		reqBody = bytes.NewReader(b)
	}

	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reqBody)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("detection-py request failed (%s %s): %w", method, path, err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("read response body: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("detection-py returned %d for %s %s: %s", resp.StatusCode, method, path, string(respBody))
	}

	if out != nil {
		if err := json.Unmarshal(respBody, out); err != nil {
			return fmt.Errorf("unmarshal response from %s: %w (body=%s)", path, err, string(respBody))
		}
	}

	return nil
}

func (c *DetectionClient) Health(ctx context.Context) error {
	var out map[string]any
	return c.doJSON(ctx, http.MethodGet, "/health", nil, &out)
}

func (c *DetectionClient) FixtureOrders(ctx context.Context) ([]Order, error) {
	var out []Order
	err := c.doJSON(ctx, http.MethodGet, "/fixtures/orders", nil, &out)
	return out, err
}

func (c *DetectionClient) FixtureSubscriptions(ctx context.Context) ([]Subscription, error) {
	var out []Subscription
	err := c.doJSON(ctx, http.MethodGet, "/fixtures/subscriptions", nil, &out)
	return out, err
}

func (c *DetectionClient) detectOrders(ctx context.Context, path string, orders []Order) ([]Finding, error) {
	var out findingsResponse
	err := c.doJSON(ctx, http.MethodPost, path, ordersRequest{Orders: orders}, &out)
	return out.Findings, err
}

func (c *DetectionClient) DetectAffiliateCouponExtension(ctx context.Context, orders []Order) ([]Finding, error) {
	return c.detectOrders(ctx, "/agents/affiliate-coupon-extension/detect", orders)
}

func (c *DetectionClient) DetectDiscountMisuse(ctx context.Context, orders []Order) ([]Finding, error) {
	return c.detectOrders(ctx, "/agents/discount-misuse/detect", orders)
}

func (c *DetectionClient) DetectAbandonedCartCoverage(ctx context.Context, orders []Order) ([]Finding, error) {
	return c.detectOrders(ctx, "/agents/abandoned-cart-coverage/detect", orders)
}

func (c *DetectionClient) DetectRenewalNeverTriggered(ctx context.Context, subs []Subscription) ([]Finding, error) {
	var out findingsResponse
	err := c.doJSON(ctx, http.MethodPost, "/agents/renewal-never-triggered/detect", subscriptionsRequest{Subscriptions: subs}, &out)
	return out.Findings, err
}

// CorrelationOverlaps calls the Decision 3 / Failure Mode #2 safeguard
// endpoint: given a combined set of findings from multiple agents, returns
// entity_id -> findings for every entity more than one agent claimed.
func (c *DetectionClient) CorrelationOverlaps(ctx context.Context, findings []Finding) (map[string][]Finding, error) {
	var out map[string][]Finding
	err := c.doJSON(ctx, http.MethodPost, "/correlation/overlaps", findingsRequest{Findings: findings}, &out)
	return out, err
}
