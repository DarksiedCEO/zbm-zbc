package client

import (
	"fmt"
	"net/http"
)

// UpstreamError is a failed call to another service (fix wave 3, AEGIS D3).
//
// Error() is the full detail for the server log: the upstream's name, the
// request, and the transport error or response body — which can contain
// internal URLs and host:port (Go's transport errors quote the full URL).
// Public() is the text that may be returned to an HTTP caller and shown on
// the dashboard: which service failed and how, with no address, no URL and
// no upstream body.
type UpstreamError struct {
	Service    string // "detection-py" or "ledger-rust" — the service actually called
	Method     string
	Path       string // request path only, never the base URL
	StatusCode int    // 0 when no HTTP response was received
	Kind       UpstreamFailure
	Detail     string // log-only: transport error text, response body, decode error
}

// UpstreamFailure classifies an UpstreamError.
type UpstreamFailure string

const (
	UpstreamUnreachable UpstreamFailure = "unreachable"   // no HTTP response (refused, timeout, ...)
	UpstreamStatus      UpstreamFailure = "status"        // HTTP response with an unexpected status
	UpstreamBadResponse UpstreamFailure = "bad_response"  // response body could not be read/decoded
	UpstreamBadRequest  UpstreamFailure = "request_build" // the request itself could not be built
)

func (e *UpstreamError) Error() string {
	switch e.Kind {
	case UpstreamStatus:
		return fmt.Sprintf("%s returned %d for %s %s: %s", e.Service, e.StatusCode, e.Method, e.Path, e.Detail)
	default:
		return fmt.Sprintf("%s %s for %s %s: %s", e.Service, e.Kind, e.Method, e.Path, e.Detail)
	}
}

// Public is safe to return to a caller: no URL, host, port or upstream body.
func (e *UpstreamError) Public() string {
	switch e.Kind {
	case UpstreamUnreachable:
		return fmt.Sprintf("%s is unreachable (%s %s)", e.Service, e.Method, e.Path)
	case UpstreamStatus:
		return fmt.Sprintf("%s answered %d %s for %s %s", e.Service, e.StatusCode, http.StatusText(e.StatusCode), e.Method, e.Path)
	case UpstreamBadResponse:
		return fmt.Sprintf("%s sent a response that could not be read (%s %s)", e.Service, e.Method, e.Path)
	default:
		return fmt.Sprintf("could not build a request to %s (%s %s)", e.Service, e.Method, e.Path)
	}
}
