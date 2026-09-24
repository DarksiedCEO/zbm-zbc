package client

// F15 (fix wave 1, Sep 24 2026): the shared money wire-format vectors in
// fixtures/money_vectors.json. The same file is loaded by detection-py
// (tests/test_money_vectors.py) and the dashboard (tests/money.test.ts), so
// the three implementations cannot drift apart.

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

type moneyVectors struct {
	Contract struct {
		Pattern          string `json:"pattern"`
		Max              string `json:"max"`
		MaxIntegerDigits int    `json:"max_integer_digits"`
		MaxLength        int    `json:"max_length"`
	} `json:"contract"`
	StringVectors []struct {
		Input         string `json:"input"`
		Money         string `json:"money"`
		PositiveMoney string `json:"positive_money"`
		Note          string `json:"note"`
	} `json:"string_vectors"`
	JSONVectors []struct {
		JSON    string `json:"json"`
		Verdict string `json:"verdict"`
		Note    string `json:"note"`
	} `json:"json_vectors"`
}

func loadMoneyVectors(t *testing.T) moneyVectors {
	t.Helper()
	// internal/client -> orchestrator-go -> services -> repo root
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "fixtures", "money_vectors.json"))
	if err != nil {
		t.Fatalf("read shared money vectors: %v", err)
	}
	var v moneyVectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatalf("parse shared money vectors: %v", err)
	}
	if len(v.StringVectors) < 50 || len(v.JSONVectors) < 5 {
		t.Fatalf("suspiciously few vectors: %d string, %d json", len(v.StringVectors), len(v.JSONVectors))
	}
	return v
}

func short(s string) string {
	if len(s) > 32 {
		return s[:32] + "..."
	}
	return s
}

func TestMoneyVectors_ContractBlockMatchesGo(t *testing.T) {
	v := loadMoneyVectors(t)
	if v.Contract.Pattern != moneyPattern.String() {
		t.Errorf("pattern drift: vectors %q, Go %q", v.Contract.Pattern, moneyPattern.String())
	}
	if v.Contract.Max != MaxMoney {
		t.Errorf("max drift: vectors %q, Go %q", v.Contract.Max, MaxMoney)
	}
}

func TestMoneyVectors_ParseMoney(t *testing.T) {
	for i, vec := range loadMoneyVectors(t).StringVectors {
		m, err := ParseMoney(vec.Input)
		switch vec.Money {
		case "accept":
			if err != nil {
				t.Errorf("#%d %q: want accept, got %v", i, short(vec.Input), err)
			} else if m.String() != vec.Input {
				t.Errorf("#%d %q: round trip gave %q", i, short(vec.Input), m.String())
			}
		case "reject":
			if err == nil || !errors.Is(err, ErrInvalidMoney) {
				t.Errorf("#%d %q (%s): want ErrInvalidMoney, got %v", i, short(vec.Input), vec.Note, err)
			}
		default:
			t.Fatalf("#%d: bad verdict %q", i, vec.Money)
		}
	}
}

// Positive-only field: LabeledValue.amount_usd, decoded from JSON exactly as
// a detection-py response is.
func TestMoneyVectors_LabeledValueAmountFromJSONString(t *testing.T) {
	for i, vec := range loadMoneyVectors(t).StringVectors {
		amt, _ := json.Marshal(vec.Input)
		raw := `{"amount_usd":` + string(amt) + `,"classification":"observed","confidence":"high"}`
		var lv LabeledValue
		err := json.Unmarshal([]byte(raw), &lv)
		if vec.PositiveMoney == "accept" {
			if err != nil || lv.AmountUSD.String() != vec.Input {
				t.Errorf("#%d %q: want accept, got %v (%q)", i, short(vec.Input), err, lv.AmountUSD.String())
			}
		} else if err == nil {
			t.Errorf("#%d %q (%s): want reject, got accepted", i, short(vec.Input), vec.Note)
		}
	}
}

func TestMoneyVectors_JSONValues(t *testing.T) {
	for i, vec := range loadMoneyVectors(t).JSONVectors {
		raw := `{"amount_usd":` + vec.JSON + `,"classification":"observed","confidence":"high"}`
		var lv LabeledValue
		err := json.Unmarshal([]byte(raw), &lv)
		if (vec.Verdict == "accept") != (err == nil) {
			t.Errorf("#%d %s (%s): verdict %s, got err=%v", i, vec.JSON, vec.Note, vec.Verdict, err)
		}
	}
}
