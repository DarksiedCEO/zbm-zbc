package main

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
)

// ADR 0023 / AEGIS A2 M-2: ORCHESTRATOR_SERVICE_TOKEN hygiene at start-up.

func TestInboundTokenHygiene(t *testing.T) {
	good := "3f9a1c0b7d2e4f6a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a" // openssl rand -hex 32 shape
	if err := checkInboundToken(good, "other-detection", "other-ledger"); err != nil {
		t.Fatal(err)
	}
	bad := map[string]string{
		"empty":       "",
		"short":       "3f9a1c0b7d2e4f6a8b9c0d1e2f3a4b5",                      // 31
		"unresolved":  "${{orchestrator.ORCHESTRATOR_SERVICE_TOKEN}}xxxxxxxx", // literal reference
		"braces":      "{{ORCHESTRATOR_SERVICE_TOKEN}}0123456789abcdef",
		"low entropy": strings.Repeat("ab", 20),
		"one char":    strings.Repeat("a", 64),
		"changeme":    "changeme-changeme-0123456789abcdefghij",
		"placeholder": "PLACEHOLDER_orchestrator_token_0123456789",
		"angle":       "<openssl rand -hex 32 output goes here 0123>",
		"space":       "3f9a1c0b7d2e4f6a 8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
		"non-ascii":   "3f9a1c0b7d2e4f6a8b9c0d1e2f3a4b5c6d7e8f9a0b1ç",
		"your-token":  "your-token-here-0123456789abcdefghijklm",
		// AEGIS edge review L-1: accepted before, all patterned
		"repeated block":  "abcdefghijabcdefghijabcdefghijab",
		"period ten":      "0123456789012345678901234567890123",
		"word and repeat": "Password1234567890Password1234567",
		"ascending run":   "0123456789abcdefghijklmnopqrstuv",
		"descending run":  "zyxwvutsrqpoA1b2C3d4E5f6G7h8I9j0K",
		"low bits hex":    "3f9a1c0b7d2e4f6a8b9c0d1e2f3a4b5",
		"same as ledger":  good,
	}
	for name, tok := range bad {
		outbound := []string{"det"}
		if name == "same as ledger" {
			outbound = append(outbound, good)
		}
		err := checkInboundToken(tok, outbound...)
		if err == nil {
			t.Errorf("%s: accepted", name)
			continue
		}
		if tok != "" && strings.Contains(err.Error(), tok) {
			t.Errorf("%s: the message carries the token", name)
		}
	}
}

func TestOutboundTokensRefuseUnresolvedReferences(t *testing.T) {
	if checkRef("LEDGER_SERVICE_TOKEN", "${{LEDGER_SERVICE_TOKEN}}") == nil {
		t.Fatal("accepted")
	}
	if checkRef("LEDGER_SERVICE_TOKEN", "plain-token") != nil {
		t.Fatal("refused")
	}
}

func TestRealBinaryRefusesAWeakToken(t *testing.T) {
	binPath := buildOrchestratorBinary(t)
	cmd := exec.Command(binPath)
	cmd.Env = append(os.Environ(), "DETECTION_SERVICE_TOKEN=d", "LEDGER_SERVICE_TOKEN=l", "ORCHESTRATOR_PORT=0",
		"ORCHESTRATOR_SERVICE_TOKEN=test-orchestrator-token")
	out, err := cmd.CombinedOutput()
	if err == nil {
		t.Fatal("started with a 23-character token")
	}
	if !strings.Contains(string(out), "REFUSING TO START") || !strings.Contains(string(out), "at least 32") ||
		strings.Contains(string(out), "test-orchestrator-token") {
		t.Fatalf("%s", out)
	}
}

// L-1: the entropy rules never refuse a generated token. TOKEN_HYGIENE_SAMPLES (default 20,000 each) raises the draw;
// it was run once with 200,000 (ADR 0023).
func TestGeneratedTokensAreNeverRefused(t *testing.T) {
	n := 20000
	if v, err := strconv.Atoi(os.Getenv("TOKEN_HYGIENE_SAMPLES")); err == nil && v > 0 {
		n = v
	}
	var b [32]byte
	refused := map[string]int{}
	for i := 0; i < n; i++ {
		if _, err := rand.Read(b[:]); err != nil {
			t.Fatal(err)
		}
		for kind, tok := range map[string]string{"hex": hex.EncodeToString(b[:]),
			"base64": base64.StdEncoding.EncodeToString(b[:]), "base64url": base64.RawURLEncoding.EncodeToString(b[:])} {
			if checkInboundToken(tok) != nil {
				refused[kind]++
			}
		}
	}
	t.Logf("%d tokens of each kind drawn; refused: %v", n, refused)
	if len(refused) != 0 {
		t.Fatalf("generated tokens refused: %v of %d each", refused, n)
	}
}
