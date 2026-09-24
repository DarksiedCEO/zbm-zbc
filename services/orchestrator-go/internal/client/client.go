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
//
// The same plumbing also serves ledger-rust (NewLedgerClient); `service`
// names which upstream a client talks to, so every error names the service
// that actually failed (fix wave 3, AEGIS D3: ledger failures used to be
// labeled "detection-py").
type DetectionClient struct {
	service    string // "detection-py" or "ledger-rust"
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
	return newServiceClient("detection-py", baseURL, token)
}

func newServiceClient(service, baseURL, token string) *DetectionClient {
	return &DetectionClient{
		service: service,
		baseURL: baseURL,
		token:   token,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// upstreamErr builds the error for a failed call to this client's service.
// Its Error() text (logged) may contain the base URL; Public() never does.
func (c *DetectionClient) upstreamErr(kind UpstreamFailure, method, path string, status int, detail string) *UpstreamError {
	return &UpstreamError{Service: c.service, Method: method, Path: path, StatusCode: status, Kind: kind, Detail: detail}
}

func (c *DetectionClient) doJSON(ctx context.Context, method, path string, body any, out any) error {
	var reqBody io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("marshal request body for %s %s: %w", c.service, path, err)
		}
		reqBody = bytes.NewReader(b)
	}

	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reqBody)
	if err != nil {
		return c.upstreamErr(UpstreamBadRequest, method, path, 0, err.Error())
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return c.upstreamErr(UpstreamUnreachable, method, path, 0, err.Error())
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return c.upstreamErr(UpstreamBadResponse, method, path, resp.StatusCode, "read response body: "+err.Error())
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return c.upstreamErr(UpstreamStatus, method, path, resp.StatusCode, string(respBody))
	}

	if out != nil {
		// UseNumber: loosely-typed pass-through payloads (Order,
		// Subscription and the Tier 2 maps are map[string]any) must never
		// round-trip a JSON number through float64 on their way back to
		// detection-py. json.Number keeps the original literal text.
		dec := json.NewDecoder(bytes.NewReader(respBody))
		dec.UseNumber()
		if err := dec.Decode(out); err != nil {
			return &wrappedUpstreamError{
				UpstreamError: c.upstreamErr(UpstreamBadResponse, method, path, resp.StatusCode,
					fmt.Sprintf("unmarshal response: %v (body=%s)", err, string(respBody))),
				cause: err,
			}
		}
	}

	return nil
}

// wrappedUpstreamError keeps the decode error reachable through errors.Is /
// errors.As (e.g. ErrInvalidMoney from a finding with a float amount).
type wrappedUpstreamError struct {
	*UpstreamError
	cause error
}

func (e *wrappedUpstreamError) Unwrap() []error { return []error{e.UpstreamError, e.cause} }

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
