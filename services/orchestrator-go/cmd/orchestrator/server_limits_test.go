package main

// Fix wave 1 (Sep 24 2026), finding "orchestrator-go uses http.ListenAndServe
// with no timeouts" (LOW, PLAUSIBLE). With no ReadHeaderTimeout/ReadTimeout a
// client that sends its request head or body one byte at a time holds a
// connection (and a goroutine) forever; with no MaxBytesReader and a 1 MB
// default MaxHeaderBytes, heads and bodies were bounded only loosely or not at
// all. These tests run the REAL compiled binary on a real socket.

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// countingLedger answers every call like an empty, valid ledger and counts
// them: a count above zero means a scan was started.
func countingLedger(t *testing.T) (*httptest.Server, *atomic.Int64) {
	t.Helper()
	var n atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"valid":true,"entries":0}`))
	}))
	t.Cleanup(srv.Close)
	return srv, &n
}

func startLimitedOrchestrator(t *testing.T) (port string, ledgerCalls *atomic.Int64) {
	t.Helper()
	ledger, calls := countingLedger(t)
	port, _ = startOrchestrator(t, "", "LEDGER_SERVICE_URL="+ledger.URL, "DETECTION_SERVICE_URL="+deadURL(t))
	return port, calls
}

// healthOK fails the test unless GET /health answers 200 while the slow client is connected. The client timeout is
// a hang guard (10 s), not a latency bound: the property is "others are served", and a 1 s bound (the old value)
// measured scheduling on a loaded box rather than the server (fix wave 25, scout C2-7).
func healthOK(t *testing.T, port string) {
	t.Helper()
	c := &http.Client{Timeout: 10 * time.Second}
	resp, err := c.Get("http://127.0.0.1:" + port + "/health")
	if err != nil {
		t.Errorf("/health failed while a slow client was connected: %v", err)
		return
	}
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Errorf("/health = %d while a slow client was connected", resp.StatusCode)
	}
}

// trickle writes one byte of payload every interval until the server closes
// the connection (a write or read fails) or `limit` passes. It returns how
// long the connection stayed open and whatever the server sent back.
func trickle(t *testing.T, port, head string, payload byte, interval, limit time.Duration, onTick func()) (time.Duration, string) {
	t.Helper()
	conn, err := net.Dial("tcp", "127.0.0.1:"+port)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	start := time.Now()
	if _, err := conn.Write([]byte(head)); err != nil {
		t.Fatal(err)
	}
	var got strings.Builder
	closed := make(chan struct{})
	go func() {
		b, _ := io.ReadAll(conn) // returns when the server closes the connection
		got.Write(b)
		close(closed)
	}()
	tick := time.NewTicker(interval)
	defer tick.Stop()
	for {
		select {
		case <-closed:
			return time.Since(start), got.String()
		case <-tick.C:
			if time.Since(start) > limit {
				return time.Since(start), "STILL OPEN"
			}
			_, _ = conn.Write([]byte{payload})
			if onTick != nil {
				onTick()
			}
		}
	}
}

// Fix wave 25 (scout C2-7): the server cutting the connection is the asserted EVENT; the trickle's limit is only a
// hang guard. The old upper bound (timeout + 2 s, under t.Parallel and -race) measured the box, not the server; the
// configured timeouts themselves are pinned exactly by TestServerHasExplicitTimeoutsAndLimits, and a LOWER bound
// (the cut came no earlier than the timeout, i.e. the timeout is what cut it) cannot be broken by load.
func TestSlowlorisHeaderIsCutOffAndOthersAreServed(t *testing.T) {
	t.Parallel()
	port, _ := startLimitedOrchestrator(t)
	// A request head that never ends: one more header byte every 300ms.
	open, got := trickle(t, port, "GET /health HTTP/1.1\r\nHost: t\r\nX-Slow: ", 'a',
		300*time.Millisecond, 4*readHeaderTimeout, func() { healthOK(t, port) })
	t.Logf("slow-header connection closed by the server after %v; it sent %q", open, got)
	if got == "STILL OPEN" {
		t.Fatalf("the server never cut a request head that does not end (hang guard %v)", 4*readHeaderTimeout)
	}
	if open < readHeaderTimeout {
		t.Fatalf("the slow head was cut after %v, before readHeaderTimeout %v: something else closed it", open, readHeaderTimeout)
	}
	if strings.Contains(got, "200 OK") {
		t.Fatalf("an unfinished request head was answered 200: %q", got)
	}
}

func TestSlowBodyIsCutOffBeforeAnyScanAndOthersAreServed(t *testing.T) {
	t.Parallel()
	port, ledgerCalls := startLimitedOrchestrator(t)
	head := "POST /revenue-recovery/scan HTTP/1.1\r\nHost: t\r\nAuthorization: Bearer test-orchestrator-token\r\n" +
		"Content-Type: application/json\r\nContent-Length: 4096\r\n\r\n"
	open, got := trickle(t, port, head, ' ', 300*time.Millisecond, 3*readTimeout, func() { healthOK(t, port) })
	t.Logf("slow-body connection closed by the server after %v; it sent %q", open, firstLine(got))
	if got == "STILL OPEN" {
		t.Fatalf("the server never cut a body that trickles (hang guard %v)", 3*readTimeout)
	}
	if open < readTimeout {
		t.Fatalf("the slow body was cut after %v, before readTimeout %v: something else closed it", open, readTimeout)
	}
	if n := ledgerCalls.Load(); n != 0 {
		t.Fatalf("a scan started (%d ledger calls) before its request body had arrived", n)
	}
	if got != "" && !strings.HasPrefix(got, "HTTP/1.1 408") {
		t.Fatalf("slow body answered with something other than 408: %q", got)
	}
	// And a real scan still runs afterwards.
	req, _ := http.NewRequest(http.MethodPost, "http://127.0.0.1:"+port+"/revenue-recovery/scan", nil)
	req.Header.Set("Authorization", "Bearer test-orchestrator-token")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if ledgerCalls.Load() == 0 {
		t.Fatalf("a normal scan after the slow client did not reach the ledger (status %d)", resp.StatusCode)
	}
}

func rawRequest(t *testing.T, port, req string) string {
	t.Helper()
	conn, err := net.Dial("tcp", "127.0.0.1:"+port)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	go func() { _, _ = conn.Write([]byte(req)) }()
	line, err := bufio.NewReader(conn).ReadString('\n')
	if err != nil {
		t.Fatalf("no response within 5s: %v", err)
	}
	return line
}

func TestOversizedBodyIs413BeforeAnyScan(t *testing.T) {
	t.Parallel()
	port, ledgerCalls := startLimitedOrchestrator(t)
	// Declared too large: refused from Content-Length, no body byte sent.
	line := rawRequest(t, port, fmt.Sprintf("POST /revenue-recovery/scan HTTP/1.1\r\nHost: t\r\n"+
		"Authorization: Bearer test-orchestrator-token\r\nContent-Length: %d\r\n\r\n", 10<<20))
	if !strings.HasPrefix(line, "HTTP/1.1 413") {
		t.Fatalf("declared oversized body: got %q, want 413", line)
	}
	// Chunked, no Content-Length: refused once the running total passes the cap.
	chunk := strings.Repeat(" ", 32<<10)
	var b strings.Builder
	b.WriteString("POST /revenue-recovery/scan HTTP/1.1\r\nHost: t\r\nAuthorization: Bearer test-orchestrator-token\r\nTransfer-Encoding: chunked\r\n\r\n")
	for i := 0; i < (maxRequestBodyBytes/len(chunk))+4; i++ {
		fmt.Fprintf(&b, "%x\r\n%s\r\n", len(chunk), chunk)
	}
	b.WriteString("0\r\n\r\n")
	line = rawRequest(t, port, b.String())
	if !strings.HasPrefix(line, "HTTP/1.1 413") {
		t.Fatalf("chunked oversized body: got %q, want 413", line)
	}
	if n := ledgerCalls.Load(); n != 0 {
		t.Fatalf("an oversized request started a scan (%d ledger calls)", n)
	}
}

func TestOversizedHeaderIsRefused(t *testing.T) {
	t.Parallel()
	port, _ := startLimitedOrchestrator(t)
	line := rawRequest(t, port, "GET /health HTTP/1.1\r\nHost: t\r\nX-Big: "+strings.Repeat("a", 100<<10)+"\r\n\r\n")
	if !strings.HasPrefix(line, "HTTP/1.1 431") {
		t.Fatalf("100 KiB header: got %q, want 431", line)
	}
	// A long query string is part of the request head: same bound.
	line = rawRequest(t, port, "GET /health?q="+strings.Repeat("a", 100<<10)+" HTTP/1.1\r\nHost: t\r\n\r\n")
	if !strings.HasPrefix(line, "HTTP/1.1 431") {
		t.Fatalf("100 KiB query string: got %q, want 431", line)
	}
}

func TestServerHasExplicitTimeoutsAndLimits(t *testing.T) {
	srv := newServer("127.0.0.1:0", http.NotFoundHandler())
	// Exact values (fix wave 25): the live slow-client tests assert the cut as an event and a lower bound only.
	if srv.ReadHeaderTimeout != 5*time.Second || srv.ReadTimeout != 15*time.Second || srv.MaxHeaderBytes != 16<<10 {
		t.Fatalf("limits changed without updating ADR 0001 \"Request limits\": %+v", srv)
	}
	if srv.ReadHeaderTimeout <= 0 || srv.ReadTimeout <= 0 || srv.WriteTimeout <= 0 || srv.IdleTimeout <= 0 || srv.MaxHeaderBytes <= 0 {
		t.Fatalf("server missing a timeout/limit: %+v", srv)
	}
	// The response to a scan that used its whole budget must still be writable.
	if srv.WriteTimeout <= handlerTimeout {
		t.Fatalf("WriteTimeout %v must exceed the handler budget %v", srv.WriteTimeout, handlerTimeout)
	}
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return s[:i]
	}
	return s
}
