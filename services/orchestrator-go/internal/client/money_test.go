package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestParseMoney_AcceptsCanonicalTwoDecimalStrings(t *testing.T) {
	for _, s := range []string{"0.00", "0.01", "2.01", "12.30", "49.99", "120.00", "1000000.05", "99999999999999999999.99"} {
		m, err := ParseMoney(s)
		if err != nil {
			t.Errorf("ParseMoney(%q) unexpected error: %v", s, err)
			continue
		}
		if m.String() != s {
			t.Errorf("ParseMoney(%q).String() = %q, want identical", s, m.String())
		}
	}
}

func TestParseMoney_RejectsEverythingElse(t *testing.T) {
	bad := []string{
		"", "12", "12.3", "12.300", ".50", "012.30", "00.00", "-1.00", "+1.00", "1e3", "1.2e1",
		"NaN", "Infinity", " 12.30", "12.30 ", "12,30", "1,200.00", "$12.30", "abc", "12.3a",
	}
	for _, s := range bad {
		if _, err := ParseMoney(s); err == nil || !errors.Is(err, ErrInvalidMoney) {
			t.Errorf("ParseMoney(%q) = %v, want ErrInvalidMoney", s, err)
		}
	}
}

func TestMoney_UnmarshalJSON_RejectsNumbersNullAndOtherTypes(t *testing.T) {
	cases := map[string]string{
		`120.0`:     "JSON number",
		`49.99`:     "JSON number",
		`-1`:        "JSON number",
		`null`:      "null",
		`true`:      "non-string",
		`{"a":1}`:   "non-string",
		`["12.30"]`: "non-string",
	}
	for raw, wantInErr := range cases {
		var m Money
		err := json.Unmarshal([]byte(raw), &m)
		if err == nil {
			t.Errorf("Unmarshal(%s) succeeded, want error", raw)
			continue
		}
		if !errors.Is(err, ErrInvalidMoney) || !strings.Contains(err.Error(), wantInErr) {
			t.Errorf("Unmarshal(%s) error = %q, want ErrInvalidMoney mentioning %q", raw, err, wantInErr)
		}
	}
}

func TestMoney_UnmarshalJSON_RejectsMalformedStrings(t *testing.T) {
	for _, raw := range []string{`"12.3"`, `"NaN"`, `"1e2"`, `""`, `"12.30 "`} {
		var m Money
		if err := json.Unmarshal([]byte(raw), &m); err == nil {
			t.Errorf("Unmarshal(%s) succeeded, want error", raw)
		}
	}
}

func TestMoney_JSONRoundTripIsByteIdentical(t *testing.T) {
	for _, s := range []string{"0.10", "0.20", "0.30", "2.01", "49.99", "54.38", "89.99"} {
		var m Money
		if err := json.Unmarshal([]byte(`"`+s+`"`), &m); err != nil {
			t.Fatalf("unmarshal %q: %v", s, err)
		}
		out, err := json.Marshal(m)
		if err != nil {
			t.Fatalf("marshal %q: %v", s, err)
		}
		if string(out) != `"`+s+`"` {
			t.Errorf("round trip of %q produced %s", s, out)
		}
	}
}

func TestMoney_ZeroValueRefusesToMarshal(t *testing.T) {
	if _, err := json.Marshal(Money{}); err == nil {
		t.Fatal("marshaling the zero Money should fail, not emit a default amount")
	}
}

func TestLabeledValue_RejectsZeroMissingAndNumericAmount(t *testing.T) {
	for _, raw := range []string{
		`{"amount_usd":"0.00","classification":"observed","confidence":"high"}`,
		`{"classification":"observed","confidence":"high"}`,
		`{"amount_usd":120.0,"classification":"observed","confidence":"high"}`,
		`{"amount_usd":null,"classification":"observed","confidence":"high"}`,
	} {
		var lv LabeledValue
		if err := json.Unmarshal([]byte(raw), &lv); err == nil {
			t.Errorf("Unmarshal(%s) succeeded, want error", raw)
		}
	}
}

func TestDetectionClient_RejectsFindingWithFloatAmountWithClearError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"findings":[{"finding_id":"f","recoverable_value":{"amount_usd":120.0,"classification":"observed","confidence":"high"}}]}`))
	}))
	defer srv.Close()

	_, err := NewDetectionClient(srv.URL, "t").DetectDiscountMisuse(context.Background(), nil)
	if err == nil || !strings.Contains(err.Error(), "money must be a JSON string") {
		t.Fatalf("expected a clear money-format error, got %v", err)
	}
}

// Exact pass-through: the amount string detection-py emitted is the exact
// string the ledger receives — no float64 in between.
func TestLedgerClient_AppendFindingPassesAmountThroughExactly(t *testing.T) {
	for _, amt := range []string{"0.30", "2.01", "49.99", "54.38", "12345678901234567.89"} {
		var body []byte
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, _ = io.ReadAll(r.Body)
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"seq":0,"hash":"h","prev_hash":"p"}`))
		}))

		var f Finding
		raw := `{"finding_id":"f-1","agent_id":"a","leak_category":"c","entity_id":"e","recoverable_value":{"amount_usd":"` + amt + `","classification":"observed","confidence":"high"}}`
		if err := json.Unmarshal([]byte(raw), &f); err != nil {
			t.Fatalf("unmarshal finding: %v", err)
		}
		if _, err := NewLedgerClient(srv.URL, "t").AppendFinding(context.Background(), f); err != nil {
			t.Fatalf("append: %v", err)
		}
		srv.Close()

		if !bytes.Contains(body, []byte(`"amount_usd":"`+amt+`"`)) {
			t.Errorf("ledger request body did not carry amount %q exactly: %s", amt, body)
		}
	}
}

func TestLedgerClient_AppendFindingWithoutValueSendsNullAmount(t *testing.T) {
	var body []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ = io.ReadAll(r.Body)
		_, _ = w.Write([]byte(`{"seq":0,"hash":"h","prev_hash":"p"}`))
	}))
	defer srv.Close()

	if _, err := NewLedgerClient(srv.URL, "t").AppendFinding(context.Background(), Finding{FindingID: "f"}); err != nil {
		t.Fatalf("append: %v", err)
	}
	if !bytes.Contains(body, []byte(`"amount_usd":null`)) {
		t.Errorf("expected null amount_usd, got %s", body)
	}
}

// Loosely-typed pass-through payloads (fixture orders) must not lose
// precision on any JSON number that is still a number: the client decodes
// with UseNumber, so the literal text survives the round trip back out.
func TestFixtureOrders_NumbersSurviveRoundTripWithoutFloat64(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[{"order_id":"o","line_items":[{"unit_price_usd":"49.99","quantity":3,"legacy_number":0.1000000000000000055511151231257827}]}]`))
	}))
	defer srv.Close()

	orders, err := NewDetectionClient(srv.URL, "t").FixtureOrders(context.Background())
	if err != nil {
		t.Fatalf("fixture orders: %v", err)
	}
	out, _ := json.Marshal(orders)
	if !bytes.Contains(out, []byte(`"legacy_number":0.1000000000000000055511151231257827`)) {
		t.Errorf("number literal was not preserved: %s", out)
	}
	if !bytes.Contains(out, []byte(`"unit_price_usd":"49.99"`)) {
		t.Errorf("string money was not preserved: %s", out)
	}
}
