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
   `STRIPE_GATEWAY_ALLOWED_IPS` replaces the list (no network wider than /16 or IPv6 /32);
   `STRIPE_GATEWAY_IP_ALLOWLIST_DISABLED=1` is the kill switch (logged at start). Typos refuse the start.
3. **Per-client token buckets** (IPv4 address, IPv6 /64): gateway 60/min burst 60 (per Stripe address; a 429 is
   retried by Stripe, so a burst is delayed, not lost); orchestrator 30/min burst 30. 429 + `Retry-After`.
4. **Failed-auth lockout on the orchestrator**: 10 answers of 401 inside 600 s from one client → every request from it
   is 429 until the window ends, the right token included (a lucky guess is not rewarded).
5. **Orchestrator token hygiene at start**: ≥ 32 printable ASCII, ≥ 10 distinct characters, no placeholder text, no
   unresolved `${{` / `{{` / `}}` reference (mirrors finance-py `_check_ref`), different from the two outbound tokens.
   The constant-time compare is unchanged.
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

Every limiter is in memory (one replica per service, `numReplicas: 1`), bounded to 16 384 clients, and answers 429 when
full rather than growing. Refusal log lines are throttled to one a second with a count of the rest.

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
- If Railway ever stops setting `X-Real-IP`, both services refuse everything but `/health` (fail closed, loud).
- The dashboard's calls come from Vercel's addresses, so they share Vercel's bucket; fine at an operator's volume.
- `stripe listen --forward-to` the Railway domain is refused (it comes from the operator's address).
- A restart clears every bucket and lockout.

## Tests

`internal/edge/edge_test.go`, `cmd/stripe-gateway/edge_test.go`, `cmd/orchestrator/edge_test.go`,
`cmd/orchestrator/token_hygiene_test.go` (including real-binary runs), `services/finance-py/tests/
test_gateway_route_allowlist.py` (every app route enumerated per caller), `devtools/test_deploy_config.py` (IaC pins
the client-address source and preserves the operator settings).
