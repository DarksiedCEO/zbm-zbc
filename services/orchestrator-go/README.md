
## stripe-gateway (Stripe webhook entry point, Oct 5 2026)

`cmd/stripe-gateway` is a separate binary in this module. It is the only piece that Stripe calls:
`POST /webhooks/stripe`. It forwards the raw body and the `Stripe-Signature` header to Finance (31)
`POST /fin/v1/stripe/events` as the `rail_gateway` caller.

**What it does:**
- **Holds no Stripe secret.** Finance verifies the signature and reads the object back from Stripe.
- **Refuses some deliveries before Finance sees them:**
  - a missing or oversized signature, an empty body, or a body that isn't UTF-8 gets 400;
  - a body over 256 KiB gets 413;
  - any method other than POST gets 405.
- **Maps Finance's answer for Stripe:**
  - Finance 2xx → 200;
  - Finance 5xx, unreachable, or slower than 45 s → 503;
  - any other Finance answer (4xx, or a redirect, which is never followed) → 400.
- **Leaks nothing back.** Finance's answer body never reaches the caller. Logs carry a status and a random
  correlation id only.

**Run it:**

```bash
go build -o stripe-gateway ./cmd/stripe-gateway
STRIPE_GATEWAY_FINANCE_URL=http://127.0.0.1:8410 \
STRIPE_GATEWAY_FINANCE_TOKEN=<Finance's FIN_SERVICE_TOKEN> \
STRIPE_GATEWAY_CALLER_TOKEN=<Finance's rail_gateway caller token> \
STRIPE_GATEWAY_BIND_ADDR=127.0.0.1 STRIPE_GATEWAY_PORT=8470 ./stripe-gateway
```

- It refuses to start without both tokens (at least 32 characters each, and different from each other).
- It binds loopback by default. Put it behind the HTTPS reverse proxy that is the public hostname.
- Register `https://<host>/webhooks/stripe` as the Stripe endpoint, with the events listed in
  `services/finance-py/README.md`.
