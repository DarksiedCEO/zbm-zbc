# Frontend alignment: `zbest-sites` vs the `zbm-zbc` backend

Added Oct 4, 2026 after the founder confirmed the UI being built is **`DarksiedCEO/zbest-sites`** (private, created
2026-09-16, last push 2026-09-25, `main` @ 49a46d1). Read-only check; nothing in either repo was changed.

## What `zbest-sites` is
- Two public **marketing sites** in one pnpm/Turborepo: `apps/zbm` (zbestmedia.com) and `apps/zbc`, shared design system
  `packages/ui`, Next.js 16 + Tailwind 4, deployed as two Vercel projects (`PRODUCT.md`).
- Interactive tools: a free **Revenue Recovery assessment** (8 questions, deterministic score) and an **AI Visibility
  Readiness Check**, both scored in the site's own route handler against **fixture rule sets**
  (`apps/zbm/app/api/lead/route.ts`, header comment).
- Its backend contract is `docs/backend-interface-spec-v0.4.md` (DRAFT): Users/Orgs/Memberships (tenancy), ZBC campaigns,
  reward policies, fraud/disputes, ledger-derived money, measurement, §11 RR assessment, §12 readiness check.

## Three mismatches that matter for go-live

### 1. The lead form drops every lead (fastest fix in the whole plan)
`apps/zbm/app/api/lead/route.ts` is a marked **STUB**: "Nothing is stored, sent or logged." No Postgres row, no Resend
email, no rate limit (all deferred to Phase 5 §3.5 of the site's brief). If the site is live, every contact and
assessment submission is lost. Wiring storage + an email notification is roughly 1–2 days and independent of the
`zbm-zbc` backend.

### 2. The site sells to service businesses; the backend's detection is built for e-commerce order data
- Site audience for ZBM (`PRODUCT.md`): home services (HVAC, roofing, restoration), med spas, legal/professional services,
  automotive, multi-location brands. Its industry leak patterns are follow-up, offer, trust, conversion, messaging leaks;
  e-commerce is 1 of 6 (`apps/zbm/content/revenue-recovery.ts`, `INDUSTRY_PATTERNS`).
- `detection-py`'s 8 agents work on orders, carts, discounts and products (e-commerce data; `_SURVEY.md`).
- `fulfillment-py` (missed-call → callback) matches the site's #1 vertical ("Home Services: Follow-Up Leak") better than
  detection does. The go-live plan marked it NOT NEEDED because customer #1 was assumed to be a Shopify store.
- **Decision for the founder:** customer #1 is a Shopify store (chat, Oct 2) *or* the service businesses the site is
  built to sell to. These lead to different builds.

### 3. What the site promises to deliver is a written audit report
The site's "Audit Deliverable" (`apps/zbm/content/revenue-recovery.ts`, `AUDIT_DELIVERABLE`): executive summary, leak
severity matrix, priority roadmap, confidence & uncertainty, written report. That is essentially the **Track B pilot**
in `GOLIVE_PLAN.md`, and it can be delivered before any store connector or store writer exists, because a marketing-
funnel audit of a service business needs its website, ads and call handling, not order data.

## Contract gap
`backend-interface-spec-v0.4` (site side) and `zbm-zbc`'s 364 endpoints (`API_SURFACE.md`) were written separately.
`zbm-zbc` has no Users/Organizations/Memberships, no client login, and none of the spec's §1 tenancy model; its
`client_id` exists only in onboarding/finance/legal. A reconciliation pass is needed before the sites call the backend.
Until then the sites should keep their own route handlers and not call `zbm-zbc` services directly (they send no CORS
headers and use service tokens; `API_SURFACE.md` summary).

## Effect on the plan
- **New item 0 (do first): wire the lead form** so inbound leads are kept and Andre is notified. ~1–2 days, site repo.
- **New decision D0:** who is customer #1: Shopify e-commerce, or the service businesses the site markets to? If service
  businesses: the pilot is a written funnel audit (largely deliverable now, with the conversion-audit tooling) and the
  first backend work is fulfillment's follow-up path plus a website/ads intake, not a Shopify connector.
