package main

import (
	"os"
	"os/exec"
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
		"empty":          "",
		"short":          "3f9a1c0b7d2e4f6a8b9c0d1e2f3a4b5",                      // 31
		"unresolved":     "${{orchestrator.ORCHESTRATOR_SERVICE_TOKEN}}xxxxxxxx", // literal reference
		"braces":         "{{ORCHESTRATOR_SERVICE_TOKEN}}0123456789abcdef",
		"low entropy":    strings.Repeat("ab", 20),
		"one char":       strings.Repeat("a", 64),
		"changeme":       "changeme-changeme-0123456789abcdefghij",
		"placeholder":    "PLACEHOLDER_orchestrator_token_0123456789",
		"angle":          "<openssl rand -hex 32 output goes here 0123>",
		"space":          "3f9a1c0b7d2e4f6a 8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
		"non-ascii":      "3f9a1c0b7d2e4f6a8b9c0d1e2f3a4b5c6d7e8f9a0b1ç",
		"your-token":     "your-token-here-0123456789abcdefghijklm",
		"same as ledger": good,
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
