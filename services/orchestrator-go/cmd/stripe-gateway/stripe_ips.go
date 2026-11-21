package main

// Stripe's published webhook source addresses (ADR 0023 "Public edge hardening").
//
// Source: https://docs.stripe.com/ips, section "Webhook notifications" ("The full list of IP addresses that webhook
// notifications might come from"), retrieved 2026-10-30. The same page links the machine-readable copies
// https://stripe.com/files/ips/ips_webhooks.json and .txt; those could not be fetched from the build environment on
// that date, so this list is the docs page's (UNVERIFIED against the JSON). Stripe announces changes on the
// api-announce mailing list "seven days' notice ... before making changes" (same page).
//
// When Stripe announces a change: update the list and the version below in one commit, or, to react at once without
// a deploy of new code, set STRIPE_GATEWAY_ALLOWED_IPS on Railway to the new full list (it REPLACES this one) and
// redeploy. docs/deploy/railway.md "Stripe source-IP allowlist" has the steps.
const stripeWebhookIPsVersion = "docs.stripe.com/ips#webhook-notifications retrieved 2026-10-30"

const stripeWebhookIPs = "3.18.12.63," +
	"3.130.192.231," +
	"13.235.14.237," +
	"13.235.122.149," +
	"18.211.135.69," +
	"35.154.171.200," +
	"52.15.183.38," +
	"54.88.130.119," +
	"54.88.130.237," +
	"54.187.174.169," +
	"54.187.205.235," +
	"54.187.216.72," +
	"35.157.207.129," +
	"3.69.109.8," +
	"3.120.168.93"
