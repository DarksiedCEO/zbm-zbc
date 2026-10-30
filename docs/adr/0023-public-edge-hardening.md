# ADR 0023 — Public edge hardening (orchestrator and stripe-gateway)

Status: **Proposed** (branch `sec/public-edge-hardening` from `golive-next-2026-10-30` `8d20e83`; local, not pushed).
Amends ADR 0001 "Request limits" (adds per-client limits in front of them) and ADR 0009 decision 2 (the caller
`rail_gateway` reaches one route; new caller `payout_rail_gateway`). ADR numbers 0021 and 0022 are taken on other
branches.

## Context

Two services have public Railway domains: `orchestrator` (the dashboard's API, one bearer token) and `stripe-gateway`
(`POST /webhooks/stripe`, forwards the raw body to Finance as caller `rail_gateway`). Railway's edge is a reverse proxy
with TLS, not a WAF: nothing filters by source, rate or reputation. Findings this answers:

- AEGIS A2 M-2: the orchestrator accepted any non-empty token, with no rate limit or lockout.
- AEGIS A1 M1 (Q5): the internet-facing gateway's Finance credentials read every route open to any caller (invoices,
  treasury, breaks, audit export, payables) and could post payout-rail events and open disputes.
- The founder's standing rule: no fake payment confirmation is ever processed. Finance already verifies every Stripe
  signature (constant-time, ±300 s) and reads every object back from Stripe; this ADR adds the layers in front of it.

## Facts this rests on (retrieved 2026-10-30)

| Id | Fact | Source |
|---|---|---|
| F1 | Stripe's webhook source addresses: 15 IPv4 addresses (listed in `stripe_ips.go`). "If your integration also receives webhooks from us, make sure these events originate from a Stripe webhook IP address." Changes are announced on the api-announce list with "seven days' notice". Machine-readable copies at `stripe.com/files/ips/ips_webhooks.json` / `.txt` **could not be fetched from the build environment (proxy refusal): the list is the docs page's, UNVERIFIED against the JSON.** | https://docs.stripe.com/ips |
| F2 | Railway employee: "Our HTTP edge overwrites caller-supplied X-Real-IP and X-Forwarded-For on every request it handles"; "this information can change at any time, so it is not a versioned contractual guarantee"; "The hop count is not fixed and we do not publish proxy CIDRs"; "Requests over *.railway.internal do not pass through the public edge and do not get its header sanitization." **A forum answer by an employee, not a documentation page.** | https://station.railway.com/questions/need-authoritative-client-ip-proxy-tru-090eac86 |
| F3 | Railway-authored (bot) replies in the same thread: `X-Real-IP` is taken from `CF-Connecting-IP` only when the peer is in Cloudflare's published IPv4 ranges. **Bot answer, UNVERIFIED.** | same thread |

## Decision

1. **Client address = `X-Real-IP`, and nothing else.** One header value, parsed as an IP (no list, no zone, no port).
   `X-Forwarded-For`, `Forwarded`, `X-Envoy-External-Address` are never read. Missing, repeated or malformed → 403 with
   the reason logged (fail closed). `GET /health` passes without it (Railway's health check reaches the container
   directly). `*_CLIENT_IP_SOURCE=remote-addr` is for runs with no proxy; the IaC pins `x-real-ip` and a deploy test
   checks it. Shared code: `services/orchestrator-go/internal/edge`.
2. **Stripe source-IP allowlist at the gateway, default ON**, compiled in with source and retrieval date
   (`cmd/stripe-gateway/stripe_ips.go`), checked before the body is read or Finance is called; 403 otherwise.
   `STRIPE_GATEWAY_ALLOWED_IPS` replaces the list (no network wider than IPv4 /16 or IPv6 /48);
   `STRIPE_GATEWAY_IP_ALLOWLIST_DISABLED=1` is the kill switch (logged at start). Typos refuse the start.
3. **Per-client token buckets** (IPv4 address, IPv6 /64): gateway 60/min burst 60 (per Stripe address; a 429 is
   retried by Stripe, so a burst is delayed, not lost); orchestrator 30/min burst 30. 429 + `Retry-After`.
4. **Failed-auth lockout on the orchestrator, which never blocks the valid token** (revised after AEGIS edge review
   H-1/M-1): 10 answers of 401 inside 600 s from one client → its requests *without* the valid token are 429 until
   the window ends. A request carrying the valid bearer (constant-time compare in the guard, `Guard.Trusted`) skips
   the lockout and the anonymous bucket and is counted in its own per-client table (same rate), which unauthenticated
   traffic can neither fill nor drain. The first version also refused the right token ("a lucky guess is not
   rewarded"); with a ≥ 128-bit token that bought nothing and let anyone on a shared egress address (Vercel's) lock
   the dashboard out. The lockout stays because it costs a valid caller nothing and cuts unauthenticated noise.
5. **Orchestrator token hygiene at start**: ≥ 32 printable ASCII, ≥ 10 distinct characters, estimated ≥ 128 bits
   (4 bits a character for hex, 6 for base64/base64url, 6.5 otherwise), no 10-character block repeated, no
   10-character run of consecutive characters, no placeholder text (words of ≥ 7 letters only: a shorter word turns
   up in random base64), no unresolved `${{` / `{{` / `}}` reference (mirrors finance-py `_check_ref`), different from
   the two outbound tokens. 0 refusals in 200,000 each of `openssl rand -hex 32`-shaped, base64 and base64url tokens
   (`TestGeneratedTokensAreNeverRefused`, `TOKEN_HYGIENE_SAMPLES=200000`). The constant-time compare is unchanged.
6. **Least privilege at Finance**: `api.NAMED_ROUTES` gives each narrow caller an exact route list enforced in a
   middleware before routing: `seo_02` → `GET /fin/v1/invoices/{id}/status`; `rail_gateway` → `POST
   /fin/v1/stripe/events`; new `payout_rail_gateway` → `POST /fin/v1/rails/{stripe|trolley}/events` (payout-rail
   webhooks: paid / failed / returned / destination_changed, each signature-checked by the rail adapter). All get the
   token-free `GET /health` and 403 elsewhere. The gateway binary only ever calls `/fin/v1/stripe/events`, so the
   payout-rail route moved to its own caller, which no deployed service holds (the route is unreachable on Railway
   until one is wired). `POST /fin/v1/disputes` no longer names `rail_gateway` (the service refused it anyway).
7. **Limits confirmed, unchanged**: both servers set `ReadHeaderTimeout` 5 s, `ReadTimeout` 15 s, `MaxHeaderBytes`
   16 KiB, idle and write timeouts; bodies 64 KiB (orchestrator, read before routing) and 256 KiB (gateway); the gateway
   also caps deliveries in flight at 32.

Every limiter is in memory (one replica per service, `numReplicas: 1`) and bounded to 16 384 clients. A full table
evicts its least recently used entry (revised after AEGIS H-1: the first version refused new clients when full, so a
sweep of 16 384 IPv6 /64s from one /48 locked every new client out for 10 minutes). Eviction only ever gives a client
a fresh bucket or forgets failures; the token is the access control.

8. **Logs** (AEGIS M-2, M-3, L-4): each service logs one `event=edge_config` line at start naming the client-address
   source and limits. Every refusal is a structured line (`event=… level=… reason=… client=…`), at most one a second per
   reason; a refused Stripe delivery is `event=stripe_delivery_refused level=ALERT` for a log alert. A derived client
   address inside Cloudflare's published ranges (`internal/edge/cloudflare.go`, cloudflare.com/ips retrieved
   2026-10-30) is logged once a minute as `reason=client_in_cloudflare_range`: it means Cloudflare is in front and its
   client address was not passed through.
9. **Finance allowlist matches the routed path** (AEGIS L-2): `scope["path"]`, never `request.url.path`, and any
   percent-encoding or `?`/`#` in a narrow caller's path is refused. The route-level caller check is worded apart
   from the middleware's, and tests exercise each layer alone (AEGIS L-3).
10. **Post-deploy spoof probe** (AEGIS M-2): after every deploy, a request with a forged `X-Real-IP: 3.18.12.63` (and
    one with the header twice) must be refused 403 with the caller's real address in the log
    (docs/deploy/railway.md, Smoke-test checklist).

## Cloudflare (the next layer; Andre's steps)

Code limits cannot absorb a volumetric flood or score reputation. The plan is Cloudflare's proxy in front of custom
subdomains of the two Railway services, with WAF managed rules and rate-limiting rules, and Railway's `X-Real-IP` then
taken from `CF-Connecting-IP` (F3, to verify after cut-over: the gateway's refusal log must show real client addresses,
not Cloudflare's). Exact steps, risks of moving `zbestmedia.com`'s nameservers, the Stripe endpoint change and secret
roll, and Vercel Firewall rules for `/api/lead`: the operator note written alongside this branch (`waf-steps.md`).

## Consequences

- A Stripe IP change not yet reflected here refuses deliveries (403) until the override or a code update; Stripe
  retries for up to three days in live mode. Mitigation: the api-announce subscription, the override variable, the
  kill switch.
- If Railway ever stops setting `X-Real-IP`, both services refuse everything but `/health` (fail closed, loud). If it
  ever passes a client's own `X-Real-IP` through, the allowlist and rate limits can be bypassed (the signature check
  still holds); only the post-deploy spoof probe detects that.
- An attacker evicting table entries resets their own buckets and failure counts; per-client limits are a speed bump,
  not the access control.
- The dashboard's calls come from Vercel's addresses, so they share Vercel's bucket; fine at an operator's volume.
- `stripe listen --forward-to` the Railway domain is refused (it comes from the operator's address).
- A restart clears every bucket and lockout.

## Tests

`internal/edge/edge_test.go`, `cmd/stripe-gateway/edge_test.go`, `cmd/orchestrator/edge_test.go`,
`cmd/orchestrator/token_hygiene_test.go` (including real-binary runs), `cmd/orchestrator/edge_probe_test.go`
(the AEGIS H-1/M-1 probes: an IPv6 /48 sweep of 16 384 /64s and 10 bad tokens from the dashboard's address must not
block the valid token), `services/finance-py/tests/
test_gateway_route_allowlist.py` (every app route enumerated per caller), `devtools/test_deploy_config.py` (IaC pins
the client-address source and preserves the operator settings).
