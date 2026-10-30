package edge

import (
	"errors"
	"fmt"
	"net/netip"
	"strings"
)

// Allowlist is a set of source addresses and networks.
type Allowlist struct {
	prefixes []netip.Prefix
}

// Narrowest a network entry may be widened to: an override is a list of a provider's addresses, never a way to
// allow "everyone" (that is what each service's kill switch is for, and it says so in the log).
const (
	minPrefixV4 = 16
	minPrefixV6 = 32
	maxEntries  = 1024
)

// ParseAllowlist reads a comma-separated list of IP addresses and CIDR networks. Every item must parse; empty items,
// zones, and networks wider than /16 (IPv4) or /32 (IPv6) are refused.
func ParseAllowlist(raw string) (Allowlist, error) {
	var a Allowlist
	items := strings.Split(raw, ",")
	if strings.TrimSpace(raw) == "" {
		return a, errors.New("the allowlist is empty")
	}
	if len(items) > maxEntries {
		return a, fmt.Errorf("the allowlist has more than %d entries", maxEntries)
	}
	for i, it := range items {
		it = strings.TrimSpace(it)
		if it == "" {
			return a, fmt.Errorf("allowlist entry %d is empty", i+1)
		}
		var p netip.Prefix
		if strings.Contains(it, "/") {
			q, err := netip.ParsePrefix(it)
			if err != nil {
				return a, fmt.Errorf("allowlist entry %d is not an address or CIDR network", i+1)
			}
			p = q
		} else {
			ip, err := netip.ParseAddr(it)
			if err != nil || ip.Zone() != "" {
				return a, fmt.Errorf("allowlist entry %d is not an address or CIDR network", i+1)
			}
			p = netip.PrefixFrom(ip, ip.BitLen())
		}
		if p.Addr().Zone() != "" {
			return a, fmt.Errorf("allowlist entry %d carries a zone", i+1)
		}
		if p.Addr().Is4In6() {
			p = netip.PrefixFrom(p.Addr().Unmap(), max(p.Bits()-96, 0))
		}
		if (p.Addr().Is4() && p.Bits() < minPrefixV4) || (p.Addr().Is6() && p.Bits() < minPrefixV6) {
			return a, fmt.Errorf("allowlist entry %d is wider than /%d (IPv4) or /%d (IPv6); use the kill switch to "+
				"turn the check off instead", i+1, minPrefixV4, minPrefixV6)
		}
		a.prefixes = append(a.prefixes, p.Masked())
	}
	return a, nil
}

// MustParseAllowlist is ParseAllowlist for compiled-in constants: a bad constant is a build defect, caught by tests.
func MustParseAllowlist(raw string) Allowlist {
	a, err := ParseAllowlist(raw)
	if err != nil {
		panic(err)
	}
	return a
}

// Contains reports whether ip is in the list.
func (a Allowlist) Contains(ip netip.Addr) bool {
	ip = ip.Unmap()
	for _, p := range a.prefixes {
		if p.Contains(ip) {
			return true
		}
	}
	return false
}

// Len is the number of entries.
func (a Allowlist) Len() int { return len(a.prefixes) }
