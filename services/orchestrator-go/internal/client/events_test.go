package client

// Revenue Recovery fix wave (Oct 6 2026): POST /ledger/events with an
// idempotent retry (E-2), and lists never sent as JSON null (E-1).

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func sampleEvent() EventInput {
	return EventInput{
		EventID: "rr.0123456789abcdef0123456789abcdef.started", Department: "revenue_recovery",
		EventType: "rr_scan_started", Actor: "orchestrator_go", SubjectID: "fixture-pool",
		PayloadSHA256: strings.Repeat("ab", 32), Summary: "rrs1 src=fixtures fixture=1 defaulted=1 asof=2026-10-06T00:00:00Z agents=8",
	}
}

// scriptedLedger answers POST /ledger/events with the next scripted answer
// and records every body it received.
type scriptedLedger struct {
	mu      sync.Mutex
	answers []func(w http.ResponseWriter)
	bodies  []string
}

func (s *scriptedLedger) server(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		s.mu.Lock()
		i := len(s.bodies)
		s.bodies = append(s.bodies, string(b))
		s.mu.Unlock()
		if r.Method != http.MethodPost || r.URL.Path != "/ledger/events" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		if i >= len(s.answers) {
			t.Errorf("unexpected extra attempt %d", i+1)
			w.WriteHeader(http.StatusTeapot)
			return
		}
		s.answers[i](w)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func status(code int, body string) func(w http.ResponseWriter) {
	return func(w http.ResponseWriter) {
		w.WriteHeader(code)
		_, _ = w.Write([]byte(body))
	}
}

func TestAppendEvent_CreatedAndAlreadyPresent(t *testing.T) {
	for _, tc := range []struct {
		code    int
		created bool
	}{{201, true}, {200, false}} {
		s := &scriptedLedger{answers: []func(http.ResponseWriter){status(tc.code, `{"seq":7,"hash":"h"}`)}}
		res, err := NewLedgerClient(s.server(t).URL, "t").AppendEvent(context.Background(), sampleEvent())
		if err != nil || res.Seq != 7 || res.Created != tc.created || res.Attempts != 1 {
			t.Fatalf("%d: got %+v, %v", tc.code, res, err)
		}
	}
}

// A lost or failed answer is retried with the IDENTICAL body, so the ledger's
// event_id idempotency makes the retry a no-op if the first attempt committed.
func TestAppendEvent_RetriesA5xxWithTheIdenticalBody(t *testing.T) {
	s := &scriptedLedger{answers: []func(http.ResponseWriter){
		status(500, `{"error":"injected"}`), status(503, ``), status(200, `{"seq":3}`),
	}}
	res, err := NewLedgerClient(s.server(t).URL, "t").AppendEvent(context.Background(), sampleEvent())
	if err != nil || res.Attempts != 3 || res.Created {
		t.Fatalf("got %+v, %v", res, err)
	}
	if len(s.bodies) != 3 || s.bodies[0] != s.bodies[1] || s.bodies[1] != s.bodies[2] {
		t.Fatalf("retries must resend the identical body: %q", s.bodies)
	}
}

// Sweep live probe L3: the ledger commits, but its answer arrives after the
// client's timeout. The retry gets 200 (already present) — one entry, not two.
func TestAppendEvent_RetriesATimedOutAnswer(t *testing.T) {
	s := &scriptedLedger{answers: []func(http.ResponseWriter){
		func(w http.ResponseWriter) { time.Sleep(600 * time.Millisecond); status(201, `{"seq":1}`)(w) },
		status(200, `{"seq":1}`),
	}}
	l := NewLedgerClient(s.server(t).URL, "t")
	l.base.httpClient.Timeout = 200 * time.Millisecond
	res, err := l.AppendEvent(context.Background(), sampleEvent())
	if err != nil || res.Attempts != 2 || res.Created || res.Seq != 1 {
		t.Fatalf("got %+v, %v", res, err)
	}
}

func TestAppendEvent_ConflictIsNeverRetried(t *testing.T) {
	s := &scriptedLedger{answers: []func(http.ResponseWriter){status(409, `{"error":"different content"}`)}}
	_, err := NewLedgerClient(s.server(t).URL, "t").AppendEvent(context.Background(), sampleEvent())
	var ue *UpstreamError
	if !errors.Is(err, ErrEventConflict) || !errors.As(err, &ue) || ue.StatusCode != 409 || len(s.bodies) != 1 {
		t.Fatalf("want one attempt and ErrEventConflict, got %v after %d attempts", err, len(s.bodies))
	}
}

func TestAppendEvent_ClientErrorIsNotRetried(t *testing.T) {
	s := &scriptedLedger{answers: []func(http.ResponseWriter){status(400, `{"error":"invalid event"}`)}}
	_, err := NewLedgerClient(s.server(t).URL, "t").AppendEvent(context.Background(), sampleEvent())
	if err == nil || len(s.bodies) != 1 {
		t.Fatalf("want one attempt and an error, got %v after %d", err, len(s.bodies))
	}
}

func TestAppendEvent_GivesUpAfterTheAttemptLimit(t *testing.T) {
	var answers []func(http.ResponseWriter)
	for range eventAppendAttempts {
		answers = append(answers, status(500, ``))
	}
	s := &scriptedLedger{answers: answers}
	_, err := NewLedgerClient(s.server(t).URL, "t").AppendEvent(context.Background(), sampleEvent())
	if err == nil || len(s.bodies) != eventAppendAttempts {
		t.Fatalf("want %d attempts then an error, got %v after %d", eventAppendAttempts, err, len(s.bodies))
	}
}

// E-1 (sweep probe_nil_findings.py): a Go nil slice marshals as JSON null and
// detection-py answers 422 to {"findings":null} / {"orders":null}. Every list
// the client sends is [] when empty, never null.
func TestNilListsAreSentAsEmptyArraysNeverNull(t *testing.T) {
	var mu sync.Mutex
	var bodies []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		bodies = append(bodies, string(b))
		mu.Unlock()
		if strings.Contains(string(b), "null") {
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"detail":"null list"}`))
			return
		}
		if r.URL.Path == "/correlation/overlaps" {
			_, _ = w.Write([]byte(`{}`))
			return
		}
		_, _ = w.Write([]byte(`{"findings":[]}`))
	}))
	defer srv.Close()
	c, ctx := NewDetectionClient(srv.URL, "t"), context.Background()
	calls := map[string]error{}
	_, calls["orders"] = c.DetectDiscountMisuse(ctx, "fixture-pool", nil)
	_, calls["subscriptions"] = c.DetectRenewalNeverTriggered(ctx, "fixture-pool", "2026-10-01T00:00:00Z", nil)
	_, calls["events"] = c.DetectServerSideAttribution(ctx, "fixture-pool", nil)
	_, calls["touchpoints"] = c.DetectCrossChannelAttribution(ctx, "fixture-pool", nil)
	_, calls["statuses"] = c.DetectPlatformIntegration(ctx, "fixture-pool", nil)
	_, calls["terms"] = c.DetectContractPricingTermDrift(ctx, "fixture-pool", nil)
	ov, err := c.CorrelationOverlaps(ctx, nil)
	calls["findings"] = err
	for name, err := range calls {
		if err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
	if ov == nil {
		t.Error("an empty overlap answer must be an empty map, not nil")
	}
	for _, b := range bodies {
		if strings.Contains(b, "null") {
			t.Errorf("request body carries null: %s", b)
		}
	}
	if !strings.Contains(bodies[0], `"client_id":"fixture-pool","orders":[]`) {
		t.Errorf("orders request: %s", bodies[0])
	}
}
