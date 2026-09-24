package client

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
)

// Money is an exact USD amount carried as its canonical two-decimal string
// (build contract section 1, docs/adr/0003-money-decimal-and-ledger-events.md).
//
// README gap #6 (fixed Sep 24 2026): the orchestrator used to decode
// amount_usd into a float64, which could not represent most cent values
// exactly (0.1, 49.99, ...). The orchestrator does no money arithmetic at
// all — it routes findings from detection-py to the ledger — so Money is
// deliberately just a validated, opaque string: whatever detection-py
// computed is passed through byte-for-byte, and no float64 ever touches it.
//
// The zero value is NOT a valid amount; build one with ParseMoney or by
// unmarshaling JSON. Marshaling a zero Money fails loudly rather than
// emitting "" or "0.00" by accident.
type Money struct {
	s string
}

// MaxMoney is the largest amount the contract admits (ADR 0003 section 1a,
// fix wave 1 F14): every money amount is < 10^15 dollars. detection-py
// rejects anything larger with 422, the dashboard refuses to display it, and
// this package rejects it on decode, so an out-of-contract amount can never
// be passed through to the ledger.
const MaxMoney = "999999999999999.99"

// maxMoneyLen is len(MaxMoney): checked before the regexp so an arbitrarily
// long string is rejected without scanning it.
const maxMoneyLen = len(MaxMoney)

// moneyPattern is the wire format: no sign, no leading zeros (except a lone
// "0"), at most 15 integer digits, exactly two fraction digits. Go's regexp
// "$" (without the m flag) matches only at the end of the text, and [0-9]
// is ASCII-only, so "1.00\n" and fullwidth digits are rejected — the same
// verdicts as fixtures/money_vectors.json.
var moneyPattern = regexp.MustCompile(`^(0|[1-9][0-9]{0,14})\.[0-9]{2}$`)

// ErrInvalidMoney is wrapped by every Money validation error.
var ErrInvalidMoney = errors.New("invalid money")

// ParseMoney validates s against the wire format and returns it as Money.
func ParseMoney(s string) (Money, error) {
	if len(s) > maxMoneyLen || !moneyPattern.MatchString(s) {
		shown := s
		if len(shown) > 32 {
			shown = shown[:32] + "..."
		}
		return Money{}, fmt.Errorf("%w: %q does not match %s (expected e.g. \"12.30\", max %s)", ErrInvalidMoney, shown, moneyPattern.String(), MaxMoney)
	}
	return Money{s: s}, nil
}

// MustParseMoney is ParseMoney for constants in tests; it panics on bad input.
func MustParseMoney(s string) Money {
	m, err := ParseMoney(s)
	if err != nil {
		panic(err)
	}
	return m
}

// String returns the canonical two-decimal text ("" for the invalid zero value).
func (m Money) String() string { return m.s }

// IsValid reports whether m was built through validation (i.e. is not the zero value).
func (m Money) IsValid() bool { return m.s != "" }

// IsPositive reports whether m is a valid amount greater than 0.00. Checked
// on the string, not by converting to a number.
func (m Money) IsPositive() bool {
	return m.IsValid() && m.s != "0.00"
}

// MarshalJSON emits the amount as a JSON string. A zero Money is an error.
func (m Money) MarshalJSON() ([]byte, error) {
	if !m.IsValid() {
		return nil, fmt.Errorf("%w: cannot marshal an uninitialized Money value", ErrInvalidMoney)
	}
	return json.Marshal(m.s)
}

// UnmarshalJSON accepts ONLY a JSON string in the wire format. A JSON number
// (e.g. 120.0), null, or any other type is rejected with a clear error:
// accepting a number here would reintroduce exactly the float ambiguity
// this type exists to remove.
func (m *Money) UnmarshalJSON(data []byte) error {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || trimmed[0] != '"' {
		kind := "non-string value"
		switch {
		case bytes.Equal(trimmed, []byte("null")):
			kind = "null"
		case len(trimmed) > 0 && (trimmed[0] == '-' || (trimmed[0] >= '0' && trimmed[0] <= '9')):
			kind = "JSON number"
		}
		return fmt.Errorf("%w: money must be a JSON string like \"12.30\", got %s %s", ErrInvalidMoney, kind, string(trimmed))
	}
	var s string
	if err := json.Unmarshal(trimmed, &s); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidMoney, err)
	}
	parsed, err := ParseMoney(s)
	if err != nil {
		return err
	}
	*m = parsed
	return nil
}
