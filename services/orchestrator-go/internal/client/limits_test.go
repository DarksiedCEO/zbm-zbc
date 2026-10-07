package client

// Fix wave 1 (Sep 24 2026), sweep of "orchestrator-go has no timeouts":
// every upstream response body used to be read with an unbounded
// io.ReadAll, and the transport accepted up to 10 MB of response headers.
// A misbehaving (or impersonated) upstream could make the orchestrator
// buffer an arbitrarily large response and then log it whole.

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func paddedJSON(prefix string, size int) string {
	return prefix + strings.Repeat(" ", size)
}

func TestDetectionResponseOverTheLimitIsRefused(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(paddedJSON(`{"findings":[]}`, maxDetectionResponseBytes)))
	}))
	defer srv.Close()
	c := NewDetectionClient(srv.URL, "t")
	c.httpClient.Timeout = 5 * time.Minute // E-15: a size test, not a timeout test (see below)
	_, err := c.DetectDiscountMisuse(context.Background(), "fixture-pool", nil)
	var ue *UpstreamError
	if !errors.As(err, &ue) || ue.Kind != UpstreamBadResponse || !strings.Contains(ue.Detail, "exceeds") {
		t.Fatalf("oversized detection response: got err %v, want a bad_response 'exceeds' error", err)
	}
}

func TestDetectionResponseAtTheLimitIsAccepted(t *testing.T) {
	body := `{"findings":[]}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(paddedJSON(body, maxDetectionResponseBytes-len(body))))
	}))
	defer srv.Close()
	c := NewDetectionClient(srv.URL, "t")
	c.httpClient.Timeout = 5 * time.Minute // E-15: a size test, not a timeout test
	if _, err := c.DetectDiscountMisuse(context.Background(), "fixture-pool", nil); err != nil {
		t.Fatalf("response of exactly the limit must be accepted: %v", err)
	}
}

func TestLedgerVerifyResponseOverTheLimitIsRefused(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(paddedJSON(`{"valid":true,"entries":0}`, maxLedgerResponseBytes)))
	}))
	defer srv.Close()
	l := NewLedgerClient(srv.URL, "t")
	// E-15 (Oct 6 2026): this test is about the SIZE limit. Moving 64 MiB over
	// loopback took longer than the 10 s call timeout on a loaded box (the
	// sweep run failed with "context deadline exceeded"), so the call budget
	// is lifted here; TestClientHasExplicitTimeouts covers the timeout.
	l.base.httpClient.Timeout = 5 * time.Minute
	_, err := l.Verify(context.Background())
	var ue *UpstreamError
	if !errors.As(err, &ue) || ue.Kind != UpstreamBadResponse || !strings.Contains(ue.Detail, "exceeds") {
		t.Fatalf("oversized verify response: got err %v, want a bad_response 'exceeds' error", err)
	}
}

func TestUpstreamErrorBodyIsTruncatedInTheLog(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(strings.Repeat("x", 1<<20)))
	}))
	defer srv.Close()
	err := NewDetectionClient(srv.URL, "t").Health(context.Background())
	if err == nil || len(err.Error()) > 8<<10 {
		t.Fatalf("a 1 MiB upstream error body must not be carried whole into the error/log (len %d)", len(err.Error()))
	}
}

func TestOversizedResponseHeadersAreRefused(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Big", strings.Repeat("a", 1<<20))
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()
	if err := NewDetectionClient(srv.URL, "t").Health(context.Background()); err == nil {
		t.Fatalf("a 1 MiB response header was accepted")
	}
}

func TestClientHasExplicitTimeouts(t *testing.T) {
	c := NewDetectionClient("http://127.0.0.1:1", "t")
	if c.httpClient.Timeout <= 0 || c.httpClient.Timeout > 30*time.Second {
		t.Fatalf("client timeout %v", c.httpClient.Timeout)
	}
	tr, ok := c.httpClient.Transport.(*http.Transport)
	if !ok || tr.ResponseHeaderTimeout <= 0 || tr.MaxResponseHeaderBytes <= 0 || tr.MaxResponseHeaderBytes > 1<<20 {
		t.Fatalf("transport lacks explicit header timeout / size limit: %#v", c.httpClient.Transport)
	}
}
