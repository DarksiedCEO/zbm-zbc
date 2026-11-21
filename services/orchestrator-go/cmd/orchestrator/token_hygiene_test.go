package main

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	mrand "math/rand/v2"
	"net/http"
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
	if !strings.Contains(string(out), "REFUSING TO START") || !strings.Contains(string(out), "exactly 64 lowercase hex") ||
		strings.Contains(string(out), "test-orchestrator-token") {
		t.Fatalf("%s", out)
	}
}

// L-1 / r2 NEW-1: no rule refuses a generated token. TOKEN_HYGIENE_SAMPLES (default 20,000 of each kind) raises the
// draw; it was run with 200,000 (ADR 0023). Standard base64 (+ / =) is NOT accepted: it is refused by the shape rule.
func TestGeneratedTokensAreNeverRefused(t *testing.T) {
	n := 20000
	if v, err := strconv.Atoi(os.Getenv("TOKEN_HYGIENE_SAMPLES")); err == nil && v > 0 {
		n = v
	}
	var b [48]byte
	refused := map[string]int{}
	for i := 0; i < n; i++ {
		if _, err := rand.Read(b[:]); err != nil {
			t.Fatal(err)
		}
		for kind, tok := range map[string]string{"hex32": hex.EncodeToString(b[:32]),
			"base64url32": base64.RawURLEncoding.EncodeToString(b[:32]),
			"base64url48": base64.RawURLEncoding.EncodeToString(b[:48])} {
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

// r2 NEW-1: only generator-shaped tokens; each of these was accepted before (or would be by a looser shape).
func TestOnlyGeneratorShapedTokensAreAccepted(t *testing.T) {
	var b [32]byte
	_, _ = rand.Read(b[:])
	h := hex.EncodeToString(b[:])
	for name, tok := range map[string]string{
		"std base64 padded":  base64.StdEncoding.EncodeToString(b[:]),
		"base64url 42":       base64.RawURLEncoding.EncodeToString(b[:])[:42],
		"base64url padded":   base64.URLEncoding.EncodeToString(b[:]),
		"human passphrase 1": "zbmzbcprodtoken2026october30secret",
		"human passphrase 2": "MyCompanyOrch2026SecretKey!ZbmZbc",
		"keyboard walk":      "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik9ol0p",
		"hex 32 (16 bytes)":  h[:32],
	} {
		err := checkInboundToken(tok)
		if err == nil || !strings.Contains(err.Error(), "exactly 64 lowercase hex") {
			t.Errorf("%s: %v", name, err)
		}
	}
	// 64 upper-case or 63/65 hex characters are also >= 43 base64url characters: accepted by that shape (>= 252 bits)
	for _, ok := range []string{h, base64.RawURLEncoding.EncodeToString(b[:]), strings.ToUpper(h), h[:63], h + "a"} {
		if err := checkInboundToken(ok); err != nil {
			t.Errorf("generated %q refused: %v", ok[:8], err)
		}
	}
}

// one generator-shaped token per rule that ONLY that rule refuses (AEGIS r2 surviving mutants M7, M7b, N14, N17):
// draws until the other rules all pass, then requires that rule's own message.
func shapedWith(t *testing.T, build func(r *mrand.Rand) string, others func(string) bool) string {
	t.Helper()
	r := mrand.New(mrand.NewPCG(1, 2))
	for i := 0; i < 10000; i++ {
		if tok := build(r); generatorShaped(tok) && !others(tok) {
			return tok
		}
	}
	t.Fatal("no candidate")
	return ""
}

func randFrom(r *mrand.Rand, alphabet string, n int) string {
	var sb strings.Builder
	for i := 0; i < n; i++ {
		sb.WriteByte(alphabet[r.IntN(len(alphabet))])
	}
	return sb.String()
}

const hexAlphabet = "0123456789abcdef"

func TestEachTokenRuleRefusesOnItsOwn(t *testing.T) {
	cases := []struct {
		name, want string
		build      func(r *mrand.Rand) string
		others     func(string) bool
	}{
		{"few distinct", "different characters",
			// 9 non-adjacent hex digits: no run possible; repeats checked below
			func(r *mrand.Rand) string { return randFrom(r, "02468ace9", 64) },
			func(s string) bool { return hasRepeatedBlock(s) || hasSequentialRun(s) || hasPlaceholderWord(s) }},
		{"repeated 10-block (exactly 10)", "repeats a 10-character block",
			func(r *mrand.Rand) string {
				blk := randFrom(r, hexAlphabet, 10)
				return randFrom(r, hexAlphabet, 22) + blk + randFrom(r, hexAlphabet, 22) + blk
			},
			func(s string) bool {
				return distinctChars(s) < minDistinctChars || hasSequentialRun(s) || hasPlaceholderWord(s)
			}},
		{"run of exactly 10", "consecutive characters",
			func(r *mrand.Rand) string {
				return randFrom(r, hexAlphabet, 27) + "0123456789" + randFrom(r, hexAlphabet, 27)
			},
			func(s string) bool {
				return distinctChars(s) < minDistinctChars || hasRepeatedBlock(s) || hasPlaceholderWord(s)
			}},
		{"placeholder word", "placeholder",
			func(r *mrand.Rand) string {
				return "changeme" + randFrom(r, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_", 40)
			},
			func(s string) bool {
				return distinctChars(s) < minDistinctChars || hasRepeatedBlock(s) || hasSequentialRun(s)
			}},
	}
	for _, c := range cases {
		tok := shapedWith(t, c.build, c.others)
		err := checkInboundToken(tok)
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: %v", c.name, err)
		}
	}
}

func TestBearerIsWithAnEmptyTokenTrustsNothing(t *testing.T) {
	trusted := bearerIs("")
	for _, auth := range []string{"", "Bearer ", "Bearer", "Bearer x"} {
		r, _ := http.NewRequest("GET", "http://x/", nil)
		if auth != "" {
			r.Header.Set("Authorization", auth)
		}
		if trusted(r) {
			t.Errorf("bearerIs(\"\") trusted %q", auth)
		}
	}
	tok := strings.Repeat("ab", 32)
	ok := bearerIs(tok)
	r, _ := http.NewRequest("GET", "http://x/", nil)
	r.Header.Set("Authorization", "Bearer "+tok)
	if !ok(r) {
		t.Fatal("the valid token was not trusted")
	}
	r.Header.Set("Authorization", "bearer "+tok)
	if ok(r) {
		t.Fatal("a lower-case scheme was trusted (requireAuth refuses it)")
	}
}
