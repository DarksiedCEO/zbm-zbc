package edge

// Cloudflare's published edge ranges (AEGIS edge review M-3). If Cloudflare is put in front of a Railway domain and
// Railway's edge does NOT translate CF-Connecting-IP into X-Real-IP (Railway confirms that only for Cloudflare's IPv4
// ranges), the derived client address is Cloudflare's own: every client then shares one identity and the gateway
// refuses every Stripe delivery. Guard.WarnRanges logs that case as a distinct warning
// (`reason=client_in_cloudflare_range`) so it is seen in the first minutes, not after a lockout.
//
// Source: https://www.cloudflare.com/ips/ (page "Last updated: Sep 28, 2023"), retrieved 2026-10-30.
const cloudflareRangesVersion = "cloudflare.com/ips retrieved 2026-10-30 (page last updated 2023-09-28)"

const cloudflareRanges = "103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,104.16.0.0/13,104.24.0.0/14," +
	"108.162.192.0/18,131.0.72.0/22,141.101.64.0/18,162.158.0.0/15,172.64.0.0/13,173.245.48.0/20,188.114.96.0/20," +
	"190.93.240.0/20,197.234.240.0/22,198.41.128.0/17," +
	"2400:cb00::/32,2606:4700::/32,2803:f800::/32,2405:b500::/32,2405:8100::/32,2a06:98c0::/29,2c0f:f248::/32"

// CloudflareRanges returns Cloudflare's published ranges (no width limit: these are a provider's whole networks)
// and the version string naming their source and date.
func CloudflareRanges() (*Allowlist, string) {
	a, err := parseList(cloudflareRanges, 0, 0)
	if err != nil {
		panic(err)
	}
	return &a, cloudflareRangesVersion
}
