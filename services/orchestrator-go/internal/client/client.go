package client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
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
	service     string // "detection-py" or "ledger-rust"
	baseURL     string
	token       string // bearer token sent as Authorization; empty means "send nothing"
	httpClient  *http.Client
	maxResponse int64 // largest response body accepted from this service
}

// Outbound limits (fix wave 1, Sep 24 2026; ADR 0001 "Request limits").
// Response bodies used to be read with an unbounded io.ReadAll and the
// transport accepted Go's default 10 MB of response headers.
const (
	// upstreamCallTimeout bounds one whole call: connect, request, response
	// headers and body. (It existed before this fix; unchanged.)
	upstreamCallTimeout = 10 * time.Second
	// maxDetectionResponseBytes: detection-py accepts at most 1000 items per
	// request (2 MiB of JSON), and its largest response — one finding
	// (~650 bytes) per item, or the correlation map of those findings — is
	// about 1 MiB. 8 MiB is that with generous headroom.
	maxDetectionResponseBytes = 8 << 20
	// maxLedgerResponseBytes: GET /ledger/entries returns the WHOLE ledger
	// (ledger-rust has no pagination). A finding entry is ~470-500 bytes, so
	// 64 MiB is ~130,000 entries (~13,000 fixture scans). Past that, reads
	// fail closed with a clear "exceeds" error in the log (502 to the
	// caller) instead of buffering without bound: a known limit until the
	// ledger paginates.
	maxLedgerResponseBytes = 64 << 20
	// maxResponseHeaderBytes: every upstream sends a handful of short headers.
	maxResponseHeaderBytes = 64 << 10
	// maxLoggedBodyBytes: how much of an upstream body an error carries into
	// the server log.
	maxLoggedBodyBytes = 2 << 10
)

func newTransport() *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.DialContext = (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	t.TLSHandshakeTimeout = 5 * time.Second
	t.ResponseHeaderTimeout = upstreamCallTimeout
	t.MaxResponseHeaderBytes = maxResponseHeaderBytes
	return t
}

// readBody reads at most c.maxResponse bytes of a response body and fails
// (without reading further) on a longer one.
func (c *DetectionClient) readBody(method, path string, resp *http.Response) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(resp.Body, c.maxResponse+1))
	if err != nil {
		return nil, c.upstreamErr(UpstreamBadResponse, method, path, resp.StatusCode, "read response body: "+err.Error())
	}
	if int64(len(b)) > c.maxResponse {
		return nil, c.upstreamErr(UpstreamBadResponse, method, path, resp.StatusCode,
			fmt.Sprintf("response body exceeds %d bytes", c.maxResponse))
	}
	return b, nil
}

// snippet is the part of an upstream body that goes into an error (and so
// the server log): at most maxLoggedBodyBytes.
func snippet(b []byte) string {
	if len(b) <= maxLoggedBodyBytes {
		return string(b)
	}
	return fmt.Sprintf("%s... (%d bytes total)", b[:maxLoggedBodyBytes], len(b))
}

// NewDetectionClient builds a client for detection-py. token must match
// that service's ZBM_SERVICE_TOKEN — detection-py fails closed and
// rejects every non-/health request without it. Pass "" only for a
// service (like ledger-rust, via NewLedgerClient) that does not require
// auth; passing "" against a token-requiring service will fail every
// call with 401, loudly, not silently.
func NewDetectionClient(baseURL, token string) *DetectionClient {
	return newServiceClient("detection-py", baseURL, token, maxDetectionResponseBytes)
}

func newServiceClient(service, baseURL, token string, maxResponse int64) *DetectionClient {
	return &DetectionClient{
		service: service,
		baseURL: baseURL,
		token:   token,
		httpClient: &http.Client{
			Timeout:   upstreamCallTimeout,
			Transport: newTransport(),
		},
		maxResponse: maxResponse,
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

	respBody, err := c.readBody(method, path, resp)
	if err != nil {
		return err
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return c.upstreamErr(UpstreamStatus, method, path, resp.StatusCode, snippet(respBody))
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
					fmt.Sprintf("unmarshal response: %v (body=%s)", err, snippet(respBody))),
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
