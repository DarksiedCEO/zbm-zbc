// Package edge holds the perimeter controls shared by the two internet-facing binaries (cmd/orchestrator and
// cmd/stripe-gateway; ADR 0023 "Public edge hardening"): how the client address is derived behind Railway's edge,
// a per-client token-bucket rate limiter, a per-client failed-authentication limiter, a source-address allowlist,
// and the middleware (Guard) that applies them before any route, body read or upstream call.
//
// Railway's edge is a reverse proxy, not a WAF: everything here assumes nothing else filters the traffic.
package edge

import (
	"fmt"
	"net/http"
	"net/netip"
	"strings"
)

// IPSource says where the client address comes from.
type IPSource int

const (
	// SourceXRealIP: the single X-Real-IP header Railway's HTTP edge sets. A Railway employee states the edge
	// "overwrites caller-supplied X-Real-IP and X-Forwarded-For on every request it handles"
	// (station.railway.com/questions/need-authoritative-client-ip-proxy-tru-090eac86, retrieved 2026-10-30), so the
	// value is the address the edge itself saw. X-Forwarded-For is never read: it is a list a client can seed, and
	// the edge's hop count is not published.
	SourceXRealIP IPSource = iota
	// SourceRemoteAddr: the TCP peer. Only correct when nothing proxies the service (local runs, CI); on Railway it
	// is the edge's own address, so every client would share one identity.
	SourceRemoteAddr
)

const (
	sourceXRealIP    = "x-real-ip"
	sourceRemoteAddr = "remote-addr"
)

func (s IPSource) String() string {
	if s == SourceRemoteAddr {
		return sourceRemoteAddr
	}
	return sourceXRealIP
}

// ParseIPSource reads a *_CLIENT_IP_SOURCE setting: unset/empty or "x-real-ip" (the Railway default), or
// "remote-addr". Anything else refuses the start, so a typo never silently picks the wrong identity.
func ParseIPSource(env, raw string) (IPSource, error) {
	switch raw {
	case "", sourceXRealIP:
		return SourceXRealIP, nil
	case sourceRemoteAddr:
		return SourceRemoteAddr, nil
	}
	return 0, fmt.Errorf("%s must be %q (behind Railway's edge, the default) or %q (no proxy: local runs only)",
		env, sourceXRealIP, sourceRemoteAddr)
}

// ClientIP returns the request's client address, or the zero Addr and a short reason (never the raw header value)
// when it cannot be determined. Callers fail closed on the zero Addr.
func ClientIP(r *http.Request, src IPSource) (netip.Addr, string) {
	if src == SourceRemoteAddr {
		ap, err := netip.ParseAddrPort(r.RemoteAddr)
		if err != nil {
			return netip.Addr{}, "remote address unparseable"
		}
		return ap.Addr().Unmap().WithZone(""), ""
	}
	vals := r.Header.Values("X-Real-Ip")
	switch {
	case len(vals) == 0:
		return netip.Addr{}, "X-Real-IP missing (request did not come through Railway's edge?)"
	case len(vals) > 1:
		return netip.Addr{}, "X-Real-IP repeated"
	}
	v := strings.TrimSpace(vals[0])
	if v == "" || len(v) > 64 || strings.ContainsAny(v, ", ") {
		return netip.Addr{}, "X-Real-IP is not a single address"
	}
	ip, err := netip.ParseAddr(v)
	if err != nil || ip.Zone() != "" || ip.IsUnspecified() {
		return netip.Addr{}, "X-Real-IP is not a valid address"
	}
	return ip.Unmap(), ""
}

// key is the identity a limiter counts against: the address itself for IPv4, its /64 for IPv6 (one subscriber
// usually holds a whole /64, so counting single IPv6 addresses would let one client rotate past any limit).
func key(ip netip.Addr) netip.Prefix {
	if ip.Is4() {
		return netip.PrefixFrom(ip, 32)
	}
	p, _ := ip.Prefix(64)
	return p
}
