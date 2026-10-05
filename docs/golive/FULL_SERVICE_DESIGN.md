# Full-service design: one front door for every client

Oct 4, 2026. Draft for the founder. **Scope: the advertising agency (ZBM) and the clipping agency (ZBC). Nothing else.**

## Verdict first
- **What the site can do now.** It can show and take requests for every service from one form, on branch `full-service-v1`.
- **What stands between a request and a paid job.** Five things, and none of them is a design question:
  1. The lead route is a stub that stores and sends nothing.
  2. There are no client MSA/SOW templates.
  3. ZBM has **no finance line code for media spend**, so it cannot invoice a TV, radio or billboard buy today.
  4. No CRM or record of the request exists past the browser.
  5. Most of the new services have no backend department code. At launch they are delivered by people and vendors, managed through the same intake, quote, contract and invoice path.

That is normal for a full-service agency at this stage. Writing it down is what stops the site from promising something the back office can't do.

## 1. The front door
Every route in leads to **`/start`**:
- the nav CTA,
- every service page,
- every campaign link.

| Entry | Example link | What the visitor sees |
|---|---|---|
| Billboard QR / URL | `zbestmedia.com/start?service=out-of-home&src=bb-i405-oct` | Intake with Out-of-Home pre-ticked |
| Radio spot vanity URL | `/start?service=radio-audio&src=kxyz-fall` | Radio & Audio pre-ticked |
| TV / streaming | `/start?service=tv-streaming&utm_campaign=ctv-q4` | TV & Streaming pre-ticked |
| Social ad | `/social-media?utm_campaign=...` → page CTA | Service page first, then the intake pre-ticked |
| Several services at once | `/start?service=tv-streaming,social-media` | Both pre-ticked |
| "We do everything" ads | `/services` | Hub listing every service |

How the link parameters are handled:
- `service` accepts any catalog slug, comma-separated or repeated. Unknown values are ignored, so a typo in a printed QR still lands on a working form.
- `src` (or `utm_campaign`) must match `^[a-z0-9][a-z0-9._-]{0,63}$`. Anything else is dropped. The value rides along as a hidden `source` field, so **every lead says which campaign produced it**.

**Intake fields:**
- **Required:** What do you need? (multi-select of all 10 services + "Not sure yet") · Name · Business · Best way to reach you.
- **Optional:** Website · Budget band · Timing · Details.

The schema is `apps/zbm/lib/lead.ts` (`start`). The same zod schema validates on the server.

## 2. Routing: who owns each request
Department numbers are the 45-department roster (Sep 24). Intake and qualification always sit with **Onboarding (1)** and **New Business / Sales (12, 27)**. The owning department below is who delivers.

| Service (slug) | Owning dept | Supporting | Backend code today (`zbm-zbc`) |
|---|---|---|---|
| Digital Advertising (`digital-advertising`) | 8 Paid Media | 6 Creative, 24 Measurement | none |
| TV & Streaming (`tv-streaming`) | 8 Paid Media | 6 Creative, 33 Vendor Mgmt | none |
| Radio & Audio (`radio-audio`) | 8 Paid Media | 6 Creative, 33 Vendor Mgmt | none |
| Out-of-Home (`out-of-home`) | 15 OOH & DOOH | 6 Creative, 33 Vendor Mgmt | none |
| Social Media (`social-media`) | 9 Social Media Mgmt | 36 Community & Reputation, 6 Creative | none |
| Creative & Production (`creative-production`) | 6 Creative Production | 7 Creative Intelligence | `creative-py` (built) |
| Influencer & Creator (`influencer-marketing`) | 11 Influencer & Partnership | 38 Compliance (FTC disclosure), 37 Legal | none |
| Z Best Clips (`z-best-clips`) | ZBC: Clipper Network | Verification & Integrity, Finance (ZBC entity) | `clipper-network-py`, `verification-py` (built; revisit pending) |
| Revenue Recovery (`revenue-recovery`) | 3 Revenue Recovery | 28 Client Delivery, fulfillment | `detection-py`, `fulfillment-py`, `delivery-py` (built; client-store fixes not built) |
| Websites & Landing Pages (`websites`) | Web/Digital Experience (Aug 12 design) | 20 Engineering, 6 Creative | none |
| Not sure (`not-sure`) | 12 New Business | — | — |

**Multi-service requests** get one owner. Use the first of these the request includes: **RR → Paid Media → OOH → Social → Creative → Web**. The other departments are attached as supporting. ZBC requests always go to ZBC, because ZBC is a separate entity with its own bank account and EIN.

## 3. One lifecycle for every service
Every service goes through **capture → qualify → quote → contract → deliver → report → bill**. Only the inputs to each step differ.

| Step | Same for all | Varies by service | Exists today? |
|---|---|---|---|
| Capture | `/start` record + source tag | pre-ticked service | **Form yes; storage NO** (stub) |
| Qualify | reply within an agreed window; ask before quoting | discovery questions per service | no |
| Quote | written plan: work, fee, measurement | basis: monthly mgmt fee (Digital, Social), media + production (TV, Radio, OOH), project (Creative, Web), mgmt + creator fees (Influencer), audit then phases (RR), campaign budget (ZBC) | no quote tool |
| Contract | MSA once per client + SOW per job; e-sign | SOW addenda: usage rights (Creative, Influencer), FTC disclosure (Influencer, ZBC), political rules (not offered) | `legal-py` stores and versions counsel-approved docs; **no MSA/SOW template approved**; no e-sign integration |
| Deliver | owner dept + supporting; client approvals logged | vendors for media buys, production crews, creators | only RR/Creative/ZBC have code |
| Report | real numbers, confidence stated | TV/audio directional; digital platform data; OOH impressions from operator | no |
| Bill | invoice from `finance-py`, ledger-anchored | line codes below | ZBM codes exist **except media** |

### Billing line codes (`services/finance-py/src/models.py:349`)
- **Existing:** `campaign_deposit` (ZBC only), `creative_services`, `strategy_services`, `production_services`, `retainer_fee`, `subscription_fee`.
- **Missing for full service:**
  - **Media spend** (TV, radio, OOH, paid digital where the client is invoiced for media). The code also refuses `campaign_deposit` for entity `zbm` (`svc_books.py:323`).
  - **Creator fees** (influencer pass-through).
- **Open questions:**
  - Should media be billed gross (ZBM as principal) or net (ZBM as agent)?
  - How should it be prepaid, and is sales tax due?

  These are CPA and counsel questions (ASC 606 principal-vs-agent). They are added to the counsel packet and are not decided here. Until they are answered, **ZBM can sell media planning and management, but should have the client pay media vendors directly or prepay against a written SOW.**

## 4. What to build next, in order
1. **Wire the lead route.** This is the hard blocker for cutover.
   - Use Postgres + Resend per Brief §6.2, plus a per-IP rate limit.
   - Store `services[]` and `source` as columns, not only in JSON, so leads can be counted per campaign and per service.
   - Estimate: 1–2 days.
2. **Counsel: MSA + SOW template** with service-specific addenda (usage rights, FTC disclosure). This gates the first signed job in every service. Add it to `COUNSEL_PACKET.md` group A.
3. **CPA/counsel: media pass-through treatment.** After that, add a `media_spend` (and `creator_fees`) line code to `finance-py` with failing-first tests and AEGIS review.
4. **Onboarding intake reconciliation.**
   - Map the `/start` record to `onboarding-py`'s client and intake facts.
   - Reconcile with the site's `backend-interface-spec-v0.4` first; do **not** build against guessed DTOs (CLAUDE.md).
5. **Founder decisions:**
   - Link Search Intelligence (currently hidden)?
   - Offer Political Advertising (no page until Compliance/Legal cover it)?
   - Confirm the commitment copy listed in `BRIEF-AMENDMENT-2026-10-04-full-service.md`.

## 5. What this design deliberately does not do
- It does not route leads automatically to agents. Until departments 8, 9, 11, 15 and Web exist in code, a person reads every request. The page says so.
- It does not quote prices on new services. Each has a "how it's scoped" line instead of a number.
- It does not add industry pages. `/services` lists industries; per-industry landing pages can follow once campaigns show which ones convert.
