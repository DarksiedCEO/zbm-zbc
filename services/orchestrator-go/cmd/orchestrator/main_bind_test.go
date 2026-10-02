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
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// The binary is built ONCE per test process (fix wave 25, scout C2-10: it was rebuilt by every test, six full
// builds per run), with the go tool of the toolchain running these tests (not whatever `go` is first on PATH),
// into a directory TestMain removes.
var (
	buildOnce sync.Once
	buildDir  string
	builtBin  string
	buildErr  error
)

func TestMain(m *testing.M) {
	code := m.Run()
	if buildDir != "" {
		os.RemoveAll(buildDir)
	}
	os.Exit(code)
}

func goTool() string {
	if root := runtime.GOROOT(); root != "" {
		if p := filepath.Join(root, "bin", "go"); fileExists(p) {
			return p
		}
	}
	return "go"
}

func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

func buildOrchestratorBinary(t *testing.T) string {
	t.Helper()
	buildOnce.Do(func() {
		buildDir, buildErr = os.MkdirTemp("", "orchestrator-bin-")
		if buildErr != nil {
			return
		}
		builtBin = filepath.Join(buildDir, "orchestrator-under-test")
		out, err := exec.Command(goTool(), "build", "-o", builtBin, ".").CombinedOutput()
		if err != nil {
			buildErr = fmt.Errorf("go build: %v\n%s", err, out)
		}
	})
	if buildErr != nil {
		t.Fatalf("failed to build orchestrator binary: %v", buildErr)
	}
	return builtBin
}

// startOrchestrator starts the real binary with ORCHESTRATOR_PORT=0 and ORCHESTRATOR_PORT_FILE (fix wave 25, scout
// C2-1): the kernel picks the port, the child writes the port it bound, and the harness trusts that port only while
// ITS child is alive — no pick-close-then-bind race, no fixed range, and a server some other run left on a port can
// never be the one under test. The child is killed and reaped in t.Cleanup.
func startOrchestrator(t *testing.T, bindAddr string, extraEnv ...string) (port string, waitForExit func()) {
	t.Helper()
	port, err := tryStartOrchestrator(t, bindAddr, extraEnv...)
	if err != nil {
		t.Fatal(err)
	}
	return port, func() {}
}

func tryStartOrchestrator(t *testing.T, bindAddr string, extraEnv ...string) (string, error) {
	t.Helper()
	binPath := buildOrchestratorBinary(t)
	portFile := filepath.Join(t.TempDir(), "orchestrator.port")
	stderr := &syncBuffer{}
	cmd := exec.Command(binPath)
	cmd.Env = append(os.Environ(),
		"DETECTION_SERVICE_TOKEN=test-detection-token",
		"ORCHESTRATOR_SERVICE_TOKEN=test-orchestrator-token",
		"LEDGER_SERVICE_TOKEN=test-ledger-token",
		"ORCHESTRATOR_PORT=0",
		"ORCHESTRATOR_PORT_FILE="+portFile,
	)
	if bindAddr != "" {
		cmd.Env = append(cmd.Env, "ORCHESTRATOR_BIND_ADDR="+bindAddr)
	}
	cmd.Env = append(cmd.Env, extraEnv...)
	cmd.Stdout = io.Discard
	cmd.Stderr = stderr
	if err := cmd.Start(); err != nil {
		return "", err
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		<-exited
	})

	// 1. The child announces the port it bound (or exits: then nothing it did not start is ever tested).
	deadline := time.Now().Add(30 * time.Second) // a hang guard, not a measurement
	var port string
	for port == "" {
		select {
		case err := <-exited:
			exited <- err
			return "", fmt.Errorf("orchestrator exited before announcing its port (%v): %s", err, stderr.String())
		case <-time.After(20 * time.Millisecond):
		}
		if b, err := os.ReadFile(portFile); err == nil && strings.HasSuffix(string(b), "\n") {
			port = strings.TrimSpace(string(b))
		} else if err != nil && !errors.Is(err, os.ErrNotExist) {
			return "", err
		}
		if port == "" && time.Now().After(deadline) {
			return "", fmt.Errorf("orchestrator announced no port: %s", stderr.String())
		}
	}
	// 2. /health on that port, while the child is still alive. Each probe has its own hang guard (fix wave 25, E-C
	// review: the default client has none, so a listener that accepted and never answered hung the harness).
	probe := &http.Client{Timeout: 10 * time.Second}
	for {
		select {
		case err := <-exited:
			exited <- err
			return "", fmt.Errorf("orchestrator exited after announcing port %s (%v): %s", port, err, stderr.String())
		default:
		}
		resp, err := probe.Get("http://127.0.0.1:" + port + "/health")
		if err == nil {
			resp.Body.Close()
			return port, nil
		}
		if time.Now().After(deadline) {
			return "", fmt.Errorf("orchestrator did not answer /health on its port %s: %v", port, err)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// syncBuffer collects the child's stderr; the exec copier goroutine writes while the harness may read.
type syncBuffer struct {
	mu sync.Mutex
	b  strings.Builder
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

// Fix wave 25 (scout C2-1/C2-12): the harness must never test a server its own child did not start. A stranger
// already listens on a port; the child is told to use that same port, so its bind fails and it exits. The harness
// must report that, not hand back the stranger's port.
func TestHarnessNeverTrustsAServerItsChildDidNotStart(t *testing.T) {
	stranger, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer stranger.Close()
	go func() {
		_ = http.Serve(stranger, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	}()
	strangerPort := strconv.Itoa(stranger.Addr().(*net.TCPAddr).Port)
	port, err := tryStartOrchestrator(t, "", "ORCHESTRATOR_PORT="+strangerPort)
	if err == nil {
		t.Fatalf("the harness accepted port %s, where a server it did not start answers /health", port)
	}
	t.Logf("refused as it must be: %v", err)
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

// ORCHESTRATOR_PORT=0 + ORCHESTRATOR_PORT_FILE (fix wave 25): the file names the port the child bound (it answers
// there), and a normal stop (SIGTERM) removes it; SIGKILL cannot (stated in the README, like ledger-rust's).
func TestPortFileNamesTheBoundPortAndIsRemovedOnSIGTERM(t *testing.T) {
	binPath := buildOrchestratorBinary(t)
	portFile := filepath.Join(t.TempDir(), "o.port")
	cmd := exec.Command(binPath)
	cmd.Env = append(os.Environ(), "DETECTION_SERVICE_TOKEN=d", "ORCHESTRATOR_SERVICE_TOKEN=o", "LEDGER_SERVICE_TOKEN=l",
		"ORCHESTRATOR_PORT=0", "ORCHESTRATOR_PORT_FILE="+portFile)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	defer func() {
		_ = cmd.Process.Kill()
		<-exited
		exited <- nil
	}()
	var port string
	for deadline := time.Now().Add(30 * time.Second); port == ""; time.Sleep(20 * time.Millisecond) {
		if b, err := os.ReadFile(portFile); err == nil && strings.HasSuffix(string(b), "\n") {
			port = strings.TrimSpace(string(b))
		}
		if time.Now().After(deadline) {
			t.Fatal("no port file")
		}
	}
	if n, err := strconv.Atoi(port); err != nil || n <= 0 || n > 65535 {
		t.Fatalf("port file holds %q", port)
	}
	resp, err := http.Get("http://127.0.0.1:" + port + "/health")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	entries, _ := os.ReadDir(filepath.Dir(portFile))
	if len(entries) != 1 {
		t.Fatalf("the publish left a temp file beside the port file: %v", entries)
	}
	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	err = <-exited
	exited <- err
	if _, statErr := os.Stat(portFile); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("port file still present after SIGTERM (exit %v)", err)
	}
}
