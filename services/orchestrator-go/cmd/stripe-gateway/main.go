// Command stripe-gateway is the public entry point for Stripe webhooks (ADR 0009 amendment "Stripe incoming",
// launch hardening Oct 5 2026). It does exactly one thing: take a Stripe webhook delivery and hand Finance (31)
// the RAW body and the Stripe-Signature header, untouched, as the `rail_gateway` caller.
//
// It does not parse, verify or trust the event: Finance verifies the signature with the endpoint secret and then
// reads the object back from Stripe itself. Keeping the gateway dumb means it holds no Stripe secret at all.
//
// Answer to Stripe (Stripe retries every non-2xx delivery for up to three days; Finance deduplicates by event id):
//   - Finance 2xx                              -> 200  (handled, or a duplicate)
//   - Finance 5xx, or Finance unreachable/slow -> 503  (try again later)
//   - Finance 4xx (bad signature, anomaly)     -> 400  (Stripe retries; the break/alert is on Finance's side)
//
// Nothing from Finance's answer is passed back to the internet, and nothing from the body or the signature is
// logged: a refused delivery is logged by status and a random correlation id only.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	// Stripe event bodies are small; Finance itself refuses a body over 256 KiB.
	maxBodyBytes     = 256 << 10
	maxSignatureLen  = 1024
	readHeaderTimout = 5 * time.Second
	readTimeout      = 15 * time.Second
	// upstreamTimeout: Finance may wait up to 30 s for its Stripe turn and then read Stripe back; past this the
	// delivery is answered 503 and Stripe sends it again (Finance's dedupe makes the retry safe).
	upstreamTimeout = 45 * time.Second
	writeTimeout    = upstreamTimeout + 10*time.Second
	idleTimeout     = 60 * time.Second
	maxHeaderBytes  = 16 << 10
	financePath     = "/fin/v1/stripe/events"
	minTokenLen     = 32
	defaultPort     = "8470"
	maxInFlight     = 32
	// Finance's route limit for /fin/v1/stripe/events is 300 KiB; a little is kept back
	maxForwardBytes = 300<<10 - 1024
)

type config struct {
	financeURL   string
	serviceToken string
	callerToken  string
	addr         string
}

func loadConfig(getenv func(string) string) (config, error) {
	c := config{
		financeURL:   strings.TrimRight(getenv("STRIPE_GATEWAY_FINANCE_URL"), "/"),
		serviceToken: getenv("STRIPE_GATEWAY_FINANCE_TOKEN"),
		callerToken:  getenv("STRIPE_GATEWAY_CALLER_TOKEN"),
	}
	u, err := url.Parse(c.financeURL)
	if c.financeURL == "" || err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return c, errors.New("STRIPE_GATEWAY_FINANCE_URL must be Finance's base URL (http:// or https://)")
	}
	// The Finance tokens and Stripe's body (it can carry client names and emails) never cross a network in clear
	// text: plain http is accepted only to a loopback Finance (AEGIS launch-hardening L8).
	if u.Scheme == "http" {
		ip := net.ParseIP(u.Hostname())
		if !(u.Hostname() == "localhost" || (ip != nil && ip.IsLoopback())) {
			return c, errors.New("STRIPE_GATEWAY_FINANCE_URL: plain http only to a loopback Finance; use https")
		}
	}
	for name, v := range map[string]string{"STRIPE_GATEWAY_FINANCE_TOKEN": c.serviceToken,
		"STRIPE_GATEWAY_CALLER_TOKEN": c.callerToken} {
		if len(v) < minTokenLen {
			return c, fmt.Errorf("%s must be set (>= %d characters): fail closed", name, minTokenLen)
		}
	}
	if c.serviceToken == c.callerToken {
		return c, errors.New("the Finance service token and the rail_gateway caller token must differ")
	}
	bind := getenv("STRIPE_GATEWAY_BIND_ADDR")
	if bind == "" {
		bind = "127.0.0.1" // loopback by default: put it behind the TLS-terminating proxy, never bare on the internet
	}
	port := getenv("STRIPE_GATEWAY_PORT")
	if port == "" {
		port = defaultPort
	}
	c.addr = net.JoinHostPort(bind, port)
	return c, nil
}

type gateway struct {
	cfg    config
	client *http.Client
	// slots bounds the deliveries in flight at once (AEGIS launch-hardening L9): past it a delivery is answered 503
	// at once (Stripe retries) instead of holding a connection while Finance is slow
	slots chan struct{}
}

func correlationID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "unavailable"
	}
	return hex.EncodeToString(b[:])
}

func answer(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"result": msg})
}

func (g *gateway) handle(w http.ResponseWriter, r *http.Request) {
	select {
	case g.slots <- struct{}{}:
		defer func() { <-g.slots }()
	default:
		answer(w, http.StatusServiceUnavailable, "try again")
		return
	}
	sig := r.Header.Get("Stripe-Signature")
	if sig == "" || len(sig) > maxSignatureLen {
		answer(w, http.StatusBadRequest, "missing or oversized Stripe-Signature")
		return
	}
	if r.ContentLength > maxBodyBytes {
		w.Header().Set("Connection", "close")
		answer(w, http.StatusRequestEntityTooLarge, "body too large")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			w.Header().Set("Connection", "close")
			answer(w, http.StatusRequestEntityTooLarge, "body too large")
			return
		}
		answer(w, http.StatusBadRequest, "could not read body")
		return
	}
	// The body travels to Finance as a JSON string; invalid UTF-8 would be rewritten on the way and could never
	// verify, so it is refused here.
	if len(body) == 0 || !utf8.Valid(body) {
		answer(w, http.StatusBadRequest, "body is not UTF-8 JSON")
		return
	}
	sum := sha256.Sum256(body)
	fwd, _ := json.Marshal(map[string]string{
		"request_id": "stripe-" + hex.EncodeToString(sum[:])[:48],
		"payload":    string(body),
		"signature":  sig,
	})
	// JSON escaping can grow the body; past Finance's route limit Finance would answer 413 for ever (AEGIS L7)
	if len(fwd) > maxForwardBytes {
		log.Printf("correlation_id=%s delivery too large once escaped (%d bytes)", correlationID(), len(fwd))
		answer(w, http.StatusRequestEntityTooLarge, "body too large")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), upstreamTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, g.cfg.financeURL+financePath, bytes.NewReader(fwd))
	if err != nil {
		answer(w, http.StatusServiceUnavailable, "try again")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+g.cfg.serviceToken)
	req.Header.Set("X-FIN-Caller-Token", g.cfg.callerToken)
	resp, err := g.client.Do(req)
	if err != nil {
		log.Printf("correlation_id=%s finance unreachable: %T", correlationID(), err)
		answer(w, http.StatusServiceUnavailable, "try again")
		return
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		answer(w, http.StatusOK, "received")
	case resp.StatusCode >= 500:
		log.Printf("correlation_id=%s finance answered %d", correlationID(), resp.StatusCode)
		answer(w, http.StatusServiceUnavailable, "try again")
	default:
		log.Printf("correlation_id=%s finance refused the delivery (%d)", correlationID(), resp.StatusCode)
		answer(w, http.StatusBadRequest, "not accepted")
	}
}

func newMux(g *gateway) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		answer(w, http.StatusOK, "ok")
	})
	mux.HandleFunc("POST /webhooks/stripe", g.handle)
	return mux
}

func newGateway(cfg config) *gateway {
	return &gateway{cfg: cfg, slots: make(chan struct{}, maxInFlight), client: &http.Client{
		Timeout: upstreamTimeout,
		// no HTTP(S)_PROXY: the Finance tokens go straight to Finance and nowhere else (AEGIS L8)
		Transport: &http.Transport{Proxy: nil, ForceAttemptHTTP2: true, MaxIdleConns: 16,
			DialContext:     (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
			IdleConnTimeout: 90 * time.Second, TLSHandshakeTimeout: 10 * time.Second,
			ResponseHeaderTimeout: upstreamTimeout},
		// never follow a redirect with the Finance tokens attached
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

func newServer(addr string, h http.Handler) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           h,
		ReadHeaderTimeout: readHeaderTimout,
		ReadTimeout:       readTimeout,
		WriteTimeout:      writeTimeout,
		IdleTimeout:       idleTimeout,
		MaxHeaderBytes:    maxHeaderBytes,
	}
}

func main() {
	cfg, err := loadConfig(os.Getenv)
	if err != nil {
		log.Fatalf("stripe-gateway: REFUSING TO START — %v", err)
	}
	srv := newServer(cfg.addr, newMux(newGateway(cfg)))
	log.Printf("stripe-gateway listening on %s, forwarding to Finance at %s%s", cfg.addr, cfg.financeURL, financePath)
	log.Fatal(srv.ListenAndServe())
}
