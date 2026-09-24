package orchestrator

import (
	"errors"
	"fmt"

	"github.com/DarksiedCEO/zbm-zbc/services/orchestrator-go/internal/client"
)

// StepError is a failed step of a scan or a read (fix wave 3, AEGIS D3).
// Error() is the full chain for the server log — it can include upstream
// URLs, host:port and upstream response bodies. PublicMessage builds the
// caller-safe text.
type StepError struct {
	Step string
	Err  error
}

func (e *StepError) Error() string { return e.Step + ": " + e.Err.Error() }
func (e *StepError) Unwrap() error { return e.Err }

func stepErr(step string, err error) error { return &StepError{Step: step, Err: err} }

// IntegrityError is the ledger's own verdict that its hash chain does not
// verify. The verdict text comes from ledger-rust's verify response (e.g.
// `ChainBroken { at_seq: 3, reason: "..." }`) and carries no address, so it
// is shown to the caller in full — hiding it would hide the very failure the
// ledger exists to surface.
type IntegrityError struct {
	When    string
	Verdict string
}

func (e *IntegrityError) Error() string {
	return fmt.Sprintf("LEDGER INTEGRITY FAILURE %s: %s", e.When, e.Verdict)
}

// PublicMessage is the text an HTTP caller may see for err: the failed step,
// which upstream failed and how (client.UpstreamError.Public), or the
// ledger's integrity verdict — never an internal URL, host:port or an
// upstream response body. Anything unrecognized is "internal error".
func PublicMessage(err error) string {
	var integrity *IntegrityError
	if errors.As(err, &integrity) {
		return integrity.Error()
	}
	var reason string
	var upstream *client.UpstreamError
	switch {
	case errors.As(err, &upstream):
		reason = upstream.Public()
	case errors.Is(err, client.ErrInvalidMoney):
		reason = "a finding carried an invalid money amount"
	default:
		reason = "internal error"
	}
	var step *StepError
	if errors.As(err, &step) {
		return step.Step + " failed: " + reason
	}
	return reason
}
