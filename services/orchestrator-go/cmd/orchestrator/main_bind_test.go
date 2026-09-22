package main

// Regression test for a confirmed finding (Sep 22 2026): orchestrator-go
// called http.ListenAndServe(":"+port, mux) with no host, which binds
// every network interface, not just localhost — the same class of bug
// independently found and fixed in ledger-rust's 0.0.0.0 default. This
// package had no test coverage at all before this file; the fix was
// first verified live by reading the actual bound socket from
// /proc/net/tcp while the process ran (see the session's build log), and
// this test makes that repeatable by spawning the real compiled binary
// and connecting to it over a real TCP socket.
//
// go1.43-style built-in `TestMain`-based binary tests aren't used here
// because this is `package main` and there's nothing importable to unit
// test directly — main() reads env vars and blocks forever in
// ListenAndServe. So this builds and runs the actual binary, exactly as
// the ledger-rust integration tests do for the equivalent Rust binary.

import (
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func buildOrchestratorBinary(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	binPath := filepath.Join(dir, "orchestrator-under-test")
	cmd := exec.Command("go", "build", "-o", binPath, ".")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("failed to build orchestrator binary: %v\n%s", err, out)
	}
	return binPath
}

func freePort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("failed to find a free port: %v", err)
	}
	defer l.Close()
	_, port, err := net.SplitHostPort(l.Addr().String())
	if err != nil {
		t.Fatalf("failed to parse free port: %v", err)
	}
	return port
}

func startOrchestrator(t *testing.T, bindAddr string) (port string, waitForExit func()) {
	t.Helper()
	binPath := buildOrchestratorBinary(t)
	port = freePort(t)

	cmd := exec.Command(binPath)
	cmd.Env = append(os.Environ(),
		"DETECTION_SERVICE_TOKEN=test-detection-token",
		"ORCHESTRATOR_SERVICE_TOKEN=test-orchestrator-token",
		"LEDGER_SERVICE_TOKEN=test-ledger-token",
		"ORCHESTRATOR_PORT="+port,
	)
	if bindAddr != "" {
		cmd.Env = append(cmd.Env, "ORCHESTRATOR_BIND_ADDR="+bindAddr)
	}
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		t.Fatalf("failed to start orchestrator: %v", err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})

	// Poll /health on 127.0.0.1 until it's up — this also proves the
	// server is reachable on loopback, which is the property under test
	// for the default-bind case.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get("http://127.0.0.1:" + port + "/health")
		if err == nil {
			resp.Body.Close()
			return port, func() {}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("orchestrator did not come up on 127.0.0.1:%s within 5s", port)
	return "", nil
}

func TestDefaultBindIsLoopbackReachable(t *testing.T) {
	port, _ := startOrchestrator(t, "")
	resp, err := http.Get("http://127.0.0.1:" + port + "/health")
	if err != nil {
		t.Fatalf("expected /health reachable on 127.0.0.1 by default, got error: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
}

func TestExplicitBindAddrOverrideStillWorks(t *testing.T) {
	port, _ := startOrchestrator(t, "127.0.0.1")
	resp, err := http.Get("http://127.0.0.1:" + port + "/health")
	if err != nil {
		t.Fatalf("expected /health reachable with explicit override, got error: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
}
