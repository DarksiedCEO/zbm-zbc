# Counsel and CPA Packet — Z Best Media / Z Best Clips

Prepared 2026-10-04 from the `zbm-zbc` repository, branch `golive-plan` at `013caff`. This packet frames
questions; it answers none of them. Every "build default" below is what the software does today while the
question is open. It is not a position anyone has taken on the law.

Citations are `path:line` from the repository root. "Register" means the build's own list of open counsel
questions, `services/legal-py/seed/counsel_questions.json` (56 rows), plus one CPA row kept only in the finance
seed (`FIN-CQ-16`). An appendix maps every register ID to a question in this packet.

---

## 1. Background

### Status

- **Nothing is live.** No client store is connected (`README.md:329-333`). No real dollar moves
  (`docs/adr/0009-finance-department-architecture.md:3-7`). No document is in force
  (`services/legal-py/README.md:12`). No counsel is engaged, so no memo can be filed yet
  (`docs/adr/0010-legal-department-architecture.md:242-244`).
- **Open questions block the software.** Each register row blocks the feature it names until a counsel memo
  resolves it and the founder approves the change (`services/legal-py/seed/legal_rules_seed.json:212` LG-18;
  `services/clipper-network-py/seed/cn_rules_seed.json:422` CN-23;
  `docs/adr/0006-compliance-department-architecture.md:159-160`). A memo is accepted only from counsel named in
  an approved engagement letter (`legal_rules_seed.json:200` LG-17).
- **The specs behind the build are outside the repo.** The founder decision record and the Legal, Clipper
  Network and other specs live only in working sessions (`docs/findings/OPEN.md:27`). Where this packet says
  "spec", it relies on what the code and ADRs quote from those specs.

### Parties

| Party | Role as the build describes it |
|---|---|
| **Z Best Media (ZBM)** | Agency entity. Client lanes are Revenue Recovery, Digital Advertising and Out of Home (`services/onboarding-py/README.md:3-5`). It has its own chart of accounts and service revenue (`services/finance-py/src/chart.py:40-50`). |
| **Z Best Clips (ZBC)** | A separate entity that runs the clipping network. Its books are never mixed with ZBM's (`docs/adr/0009-finance-department-architecture.md:19-24`; `chart.py:18-38`). |
| **Silverback** | A third entity value in the Legal register (`services/legal-py/src/models.py:32`; `services/legal-py/seed/documents.json:249`). The repo does not describe how it relates to ZBM or ZBC. |
| **Andre (founder)** | The only human approver of rules, documents, payout batches and memos-as-rows. No second approver is named yet (`docs/adr/0009-finance-department-architecture.md:79-87`). |
| **Revenue Recovery client** | A merchant, Shopify first. It grants least-access platform roles. ZBM reads its order, customer and discount data to find "revenue leaks" (`README.md:176-205`; `services/onboarding-py/src/intelligences/i04_platform_access.py:26`). |
| **Brand (ZBC client)** | Funds a clipping campaign by prepaying a deposit (`docs/adr/0009-finance-department-architecture.md:19-21`). |
| **Clipper** | An independent short-form creator, 18 or older. Clippers post brand clips on their own accounts and are paid per certified view (`README.md:581-590`; compliance seed HR-13 at `services/compliance-py/seed/compliance_obligations_seed.json:446`). |
| **Platforms** | TikTok, YouTube, Instagram and X. View counts come from the platforms' official APIs through each clipper's own OAuth connection (`README.md:542-545`). |
| **Payout rails, bank, tax agent** | Stripe Connect, with Trolley for countries Stripe does not reach. The bank and the TIN-matching tax agent are not chosen yet. None of them is wired (`docs/adr/0009-finance-department-architecture.md:253-258`). |

### How money flows (as the code describes it)

**ZBM (Revenue Recovery):**

1. The client signs a client MSA. Onboarding cannot activate the client until (a) the contract gate passes:
   signed, in term, with the CCPA/CPRA clause, and (b) the compliance gate passes
   (`services/onboarding-py/src/intelligences/i14_contract_obligation.py:77-90`;
   `.../i15_compliance.py:27-39`).
2. ZBM invoices the client from ZBM's books. Payment is by ACH or wire only; cards are refused. Invoice line
   codes are `creative_services`, `strategy_services`, `production_services`, `retainer_fee` and
   `subscription_fee` (`services/finance-py/src/intelligences/i02_receivables.py:18-25`). The repo has no line
   code or fee model specific to Revenue Recovery, such as a percentage of recovered revenue.
3. Approving an invoice requires all of the following (`services/finance-py/src/svc_books.py:261-283`,
   `:355-373`):
   - the MSA is current at Legal;
   - the sales-tax row (FIN-CQ-11) is verified;
   - for recurring retainers or subscriptions, the auto-renewal rows are verified.
4. Fixes to the client's store are designed to graduate in three steps: shadow mode, then human approval, then
   autonomous (`README.md:192-205`). Today the one agent allowed to write to a store refuses every action, and
   no store connector exists (`services/detection-py/src/safety/action_execution.py:1-20`, `:74-85`).

**ZBC (clipping):**

1. **Deposit.** The brand signs an order form and pays a deposit invoice by ACH or wire. The deposit lands in a
   ZBC-owned restricted bank account titled `Cash - Restricted: ZBC Client Campaign Deposits` and is booked as
   a contract liability (account 2010). The words "escrow", "trust", "FBO" and "for the benefit of" are refused
   in account titles and invoices (`chart.py:20`, `:27`; `docs/adr/0009-finance-department-architecture.md:88-92`).
2. **Model A.** The founder-locked model is "Model A: ZBC sells verified views as principal". Custody is
   `own_deposit`; any other value refuses to start (`services/finance-py/src/config.py:169-173`).
3. **Certification.** A view becomes payable when Verification & Integrity certifies a platform-reported view
   count at settlement. The settlement lag is 14 days by default, within an allowed 7–14 days, and the clip must
   stay live for a minimum period (compliance seed HR-13, `:446`).
4. **Accrual.** One journal entry then moves revenue (2010 to 4010, gross) and records the creator cost (5010
   to 2020, creator payable) (`docs/adr/0009-finance-department-architecture.md:125-126`).
5. **Payout.** Payouts are weekly. The system proposes a batch, Andre approves it, the system releases it after
   12 hours, and it pays through the rail (`services/finance-py/README.md:53-61`). There are 12 release gates,
   including a tax form on file and 24% backup withholding (`docs/adr/0009-finance-department-architecture.md:76-78`;
   compliance seed US-IRS-BWH, `:1267`).
6. **Clawbacks.** A clawback is only ever netted against future earnings, never pulled back
   (`docs/adr/0009-finance-department-architecture.md:93-94`).
7. **Refunds.** Unearned deposit is refundable on Andre's approval (choice 31, `:182-183`).

**Controls that bear on legal questions:**

- Restricted cash must always cover creator and client liabilities (`:71-75`).
- Reconciliation must come to zero (`:95-97`).
- No bank, card, SSN or TIN data is held in Finance (`:108-112`).
- Clipper Network stores no DOB, ID, tax data, legal name, IP address or payment details (CN-24,
  `cn_rules_seed.json:436`).

---

## 2. Questions

Each question lists:

- **To:** the addressee, COUNSEL, CPA or BOTH.
- **Q:** the question.
- **Why it matters:** the consequence.
- **Build default:** what the code does now.
- **Raised at:** where the question appears.

IDs in brackets are the build's own register IDs. Questions marked **[NOT RAISED IN REPO]** do not appear in
the repository. They are included because the first-payment path relies on them, and the citations show the
relevant build behavior.

### (A) Blocks the first payment from a Shopify Revenue Recovery client

**A1. Counsel engagement terms on AI use** [CQ-24] — To: **COUNSEL**
- **Q:** Will counsel accept engagement-letter terms covering counsel's use of AI, review of agent-drafted
  documents, billing for AI-assisted work, and conflicts? The build requires an AI-use clause `ENG-AI-01`.
- **Why it matters:** This blocks everything else. No memo is accepted until an engagement letter containing
  `ENG-AI-01` is approved, so every other question in this packet stays blocked in the software.
- **Build default:** The counsel channel is not built; setting it refuses start
  (`services/legal-py/src/config.py:124`). Andre carries packages to counsel by hand
  (`docs/adr/0010-legal-department-architecture.md:242-244`). The engagement letter cannot be approved without
  the clause (`:104-106`).
- **Raised at:**
  - `services/legal-py/seed/counsel_questions.json:368`
  - `services/legal-py/seed/documents.json:176`
  - `services/legal-py/seed/legal_rules_seed.json:200`

**A2. CCPA/CPRA service-provider terms in the client MSA, and when ZBM becomes a "third party" or Delete Act data broker** [CQ-20, CQ-10; Onboarding P23; clause MSA-CCPA-01] — To: **COUNSEL**
- **Q:** What service-provider terms must the client MSA carry for the customer data ZBM reads from a client's
  store? Which client requests would make ZBM a "third party" rather than a service provider? Is ZBM a
  Delete Act data broker for any audience data it holds, and if so, what are the registration window and fee?
- **Why it matters:** The client cannot be activated, under both onboarding gates:
  - Gate 14 is unmet because the clause is missing.
  - Gate 15 is unmet because counsel has not approved the clause.
  - Legal reports `ccpa_cpra_clause_present: false` until clause MSA-CCPA-01 is in the executed MSA and CQ-20
    is verified.
  - The data is personal data: the commerce schema carries each customer's email
    (`services/detection-py/src/zbm_schema/__init__.py:115-118`).
- **Build default:**
  - `p23_clause_counsel_approved = False` (`services/onboarding-py/src/config.py:13`, `:74`).
  - `i14_contract_obligation.py:89-90` and `i15_compliance.py:31`.
  - `services/legal-py/src/service.py:1569-1575`.
  - Any "audience data sale" is blocked at activation and publish (compliance seed US-CA-DELETE-ACT at `:2066`
    and CQ-10 at `:3553`).
- **Raised at:**
  - `counsel_questions.json:311` (CQ-20) and `:150` (CQ-10)
  - `docs/adr/0004-onboarding-department-architecture.md:550`
  - `README.md:415`

**A3. AI-identity disclosure wording (Cal. Bus. & Prof. Code §17941)** [Onboarding P1; CN-CQ-08; rule CN-16] — To: **COUNSEL**
- **Q:** Does the build's first-message disclosure satisfy §17941 for client onboarding and clipper
  recruiting? If not, what wording would? It says the assistant is an AI and offers a human, Andre.
- **Why it matters:** The approval flag is required by the compliance gate in both the client lane and the
  creator lane, so no client and no clipper can be activated.
- **Build default:** Draft text exists at `services/onboarding-py/src/guardrails.py:139-146`. Its status is
  "pending counsel review" (`:137`), and `p1_wording_counsel_approved = False` (`config.py:12`, `:73`).
  Clipper Network requires the same disclosure (`cn_rules_seed.json:305-308`).
- **Raised at:**
  - `docs/adr/0004-onboarding-department-architecture.md:549`
  - `i15_compliance.py:29`, `:43`
  - `counsel_questions.json:598` (CN-CQ-08)
  - `README.md:415`

**A4. What clickwrap presentation and acceptance record make a contract binding** [CQ-19] — To: **COUNSEL**
- **Q:** What clickwrap presentation and versioned acceptance record satisfy Cal. Civ. Code §1633.9
  attribution? Which documents need E-SIGN §7001(c) consumer consent? Which document types are excluded from
  e-signature under UETA §1633.3?
- **Why it matters:** An e-sign provider is not built, so clickwrap is the only signing path. Until counsel
  answers, no acceptance is "evidence-sufficient". Consequences:
  - The client MSA never reads as signed, so gate 14 shows `contract_signed` unmet.
  - The clipper agreement has the same problem.
  - Obligations are never created.
- **Build default:**
  - `evidence_sufficient` stays false until CQ-19 is verified (`services/legal-py/src/intelligences/i03_acceptance.py:8-14`;
    `service.py:1166-1178`).
  - `signed` is derived from it (`service.py:1565`, `:1598-1602`).
  - The e-sign provider refuses start (`services/legal-py/README.md:43`).
  - Acceptance records hold no IP address, by design (LG-03, `legal_rules_seed.json:37`).
- **Raised at:**
  - `counsel_questions.json:296`
  - `docs/adr/0010-legal-department-architecture.md:248-249`

**A5. Sales and use tax on ZBM services, ZBC deliverables and portal SaaS; nexus from paying out-of-state creators** [FIN-CQ-11, CQ-12] — To: **CPA**
- **Q:** Do California sales or use tax rules reach ZBM's services, ZBC's campaign deliverables, or a client
  portal subscription? Does paying creators in other states create foreign-qualification or income-tax nexus?
- **Why it matters:** Billing readiness is unmet and no invoice can be approved, for ZBM or ZBC, so no client
  can be billed.
- **Build default:**
  - Invoices carry `tax_treatment: unverified` (`services/finance-py/src/svc_books.py:342-343`).
  - Approval is refused while FIN-CQ-11 is unverified (`:366-367`).
  - Billing readiness is unmet (`:278-279`).
- **Raised at:**
  - `counsel_questions.json:757` (FIN-CQ-11) and `:184` (CQ-12)
  - `services/finance-py/seed/fin_rules_seed.json:534`
  - `docs/adr/0009-finance-department-architecture.md:248-249`

**A6. Auto-renewal law for recurring retainers and subscriptions** [FIN-CQ-10] — To: **COUNSEL**
- **Q:** Does California's Automatic Renewal Law reach small-business subscribers? Which records must be kept?
- **Why it matters:** This blocks payment only if Revenue Recovery is billed as a retainer or subscription. A
  recurring invoice cannot be approved until FIN-CQ-10 is verified and the federal ROSCA row is in force. The
  repo mentions "$1,500/mo retainer clients" (`docs/adr/0002-fulfillment-department-architecture.md:16`).
- **Build default:** Recurring invoices check FIN-CQ-10 and US-ROSCA (`svc_books.py:370-373`). US-ROSCA is
  seeded unverified (compliance seed `:975`).
- **Raised at:** `counsel_questions.json:742`; `fin_rules_seed.json:520`

**A7. [NOT RAISED IN REPO] Authority and liability for changes ZBM makes to a client's store** — To: **COUNSEL**
- **Q:** When ZBM applies a fix to a client's live store, what contract terms should govern it? The terms would
  need to cover:
  - ZBM's authority to make the change;
  - the client's per-change approval;
  - warranty disclaimers;
  - limitation of liability;
  - indemnity;
  - who bears the loss if a fix reduces revenue.
- **Why it matters:** The product's purpose is to find and fix leaks, and the build plans to move from
  human-approved to autonomous changes. A search for liability, indemnity and warranty terms found none
  covering Revenue Recovery work; the only hits are music (CQ-21) and insurance (CQ-23).
- **Build default:**
  - Nothing executes (`action_execution.py:1-20`, `:74-85`).
  - Graduation needs 20 shadow decisions at 95% or better agreement, then 30 clean human-approved changes over
    at least 30 days (`README.md:194-196`).
  - Onboarding requires the client's "yes for that exact change" (`services/onboarding-py/README.md:23`).
- **Raised at:** nowhere

**A8. [NOT RAISED IN REPO] Handling Shopify merchants' customer personal data beyond the CCPA clause** — To: **COUNSEL**
- **Q:** What retention, deletion, security and breach-notice terms should apply to customer records pulled
  from a client's store? This includes ZBM's duties under Shopify's API and partner terms for protected
  customer data, and whether ZBM needs its own privacy notice or a DPA with the client.
- **Why it matters:** The first client's data will include end customers' emails. A2 covers only the CCPA
  service-provider clause.
- **Build default:**
  - `Customer.email` is in the schema (`zbm_schema/__init__.py:115-118`).
  - The Shopify translation layer is not built (`README.md:337-339`).
  - The Shopify access facts are unverified drafts (`docs/adr/0004-onboarding-department-architecture.md:504-507`).
  - A control requires a DPA on file for every vendor that touches ZBM or ZBC data
    (`services/compliance-py/src/controls.py:89-90`).
- **Raised at:** nowhere beyond A2

**A9. [NOT RAISED IN REPO] Fee basis for Revenue Recovery, and how it is invoiced and recognized** — To: **BOTH** (founder decides the model first)
- **Q:** If ZBM charges a share of recovered revenue, a fixed fee or a retainer, what contract language
  defines and measures "recovered revenue"? How is that revenue recognized (variable consideration)?
- **Why it matters:** The invoice model has no Revenue Recovery or performance-fee line code. The Billing
  department the onboarding gate needs is a stand-in.
- **Build default:**
  - `i02_receivables.py:18-20` has no such line code.
  - The billing gate is `billing_setup_p16` (`i15_compliance.py:38`).
  - Billing is a stand-in (`docs/adr/0004-onboarding-department-architecture.md:562`).
- **Raised at:** nowhere

### (B) Blocks ZBC clipping campaigns taking money

A1 (engagement), A3 (disclosure wording, creator lane), A4 (clickwrap, needed for the clipper agreement and
order form) and A5 (sales tax; ZBC deposit invoices go through the same approval check) also block this group.
They are not repeated here.

**B1. Model A: is the brand's prepayment ZBC's own customer deposit, and does that keep ZBC outside money transmission and trust law?** [FIN-CQ-01; related CQ-02, see D1] — To: **COUNSEL**
- **Q:** Under the client contract, ZBC sells verified views as principal, and the brand's prepayment is ZBC's
  own customer deposit held in a ZBC-owned restricted account. Does that arrangement avoid money-transmitter
  licensing and escrow law? Does the segregated-account policy, or a contractual restriction on the deposit,
  create a trust or fiduciary duty under California law?
- **Why it matters:** No campaign can be `fundable`, and no deposit invoice can be approved. If the answer is
  no, the founder-locked model and custody settings must change, and those settings refuse to start on any
  other value.
- **Build default:**
  - `FIN_REVENUE_MODEL=principal` and `FIN_CUSTODY_MODEL=own_deposit` (`services/finance-py/src/config.py:169-173`).
  - The account title rules and refused words (`docs/adr/0009-finance-department-architecture.md:88-92`).
  - The fundable check (`svc_books.py:251-259`).
- **Raised at:**
  - `counsel_questions.json:615`
  - `fin_rules_seed.json:392`
  - `services/finance-py/devtools/gen_rules_seed.py:148`
  - `docs/adr/0009-finance-department-architecture.md:248`, `:251`

**B2. Talent Agencies Act** [CQ-16 = CN-CQ-01] — To: **COUNSEL**
- **Q:** Does recruiting clippers and placing them on brand campaigns make ZBC a talent agency under the
  Talent Agencies Act (Cal. Lab. Code §1700.4)? Are short-form clippers "artists"? Does structuring ZBC as a
  buyer of deliverables, with no agency relationship, avoid §§1700.5 and 1700.25?
- **Why it matters:** Every clipper enrolment on a campaign is blocked.
- **Build default:** ZBC buys deliverables. No quotas, required hours, exclusivity or posting schedules
  (CN-25, `cn_rules_seed.json:450`).
- **Raised at:**
  - `counsel_questions.json:252`, `:508`
  - `cn_rules_seed.json:483`
  - `docs/adr/0008-clipper-network-architecture.md:426-427`

**B3. Worker classification of clippers and editors** [CQ-01 = CN-CQ-02 = FIN-CQ-07; CQ-17] — To: **COUNSEL**
- **Q:** Are clippers independent contractors under AB 5's ABC test? Does any exemption under Labor Code
  §2775 and following fit clippers and editors? Candidates are the AB 2257 creative exemptions, §2776
  business-to-business and §2778. Which ZBC practices would break the exemption?
- **Why it matters:**
  - Every payout is blocked.
  - The independent-contractor agreement cannot be approved.
  - The finding exposes ZBC to retroactive state liability.
- **Build default:**
  - Clippers are treated as contractors: W-9 or W-8, 1099-NEC, no payroll (compliance seed US-IRS-BWH at
    `:1267`).
  - The IC agreement approval is blocked by CQ-17 (`documents.json:62`).
- **Raised at:**
  - `counsel_questions.json:11`, `:267`, `:518`, `:702`
  - compliance seed `:3285`
  - `README.md:510`

**B4. Enforceability of the clipper agreement: venue, arbitration, class waiver, forfeiture, clawback** [CQ-11 = CN-CQ-04; CQ-18] — To: **COUNSEL**
- **Q:** What venue, arbitration and governing-law terms will hold across states? Do these survive state wage
  challenges, or reclassification of a clipper as an employee or consumer?
  - arbitration and the class-action waiver;
  - forfeiture of pay for a clip removed before the minimum live period;
  - the post-settlement clawback.
- **Why it matters:**
  - Every payout is blocked.
  - Verification & Integrity cannot certify any view: its rules VI-05 and VI-06 cite CQ-11, so nothing
    certifies until it is resolved.
- **Build default:**
  - The settlement lag is 14 days, and a clawback clause is required (HR-13, compliance seed `:446`).
  - The minimum live period is set per campaign rulebook.
  - Clawback is by netting only (`docs/adr/0009-finance-department-architecture.md:93-94`).
- **Raised at:**
  - `counsel_questions.json:167`, `:279`, `:536`
  - compliance seed `:3584`
  - `docs/adr/0007-verification-integrity-architecture.md:172-174`
  - `README.md:555-557`

**B5. FTC Part 465 and pay-per-view clips** [CQ-03] — To: **COUNSEL**
- **Q:** Can a disclosed, pay-per-view ad clip be a "testimonial" under 16 CFR Part 465? Does paying only for
  verified views of disclosed ads keep ZBC outside §465.4?
- **Why it matters:** Every payout is blocked. The question has no FTC guidance on point.
- **Build default:**
  - Payout is only on platform-verified views (US-FTC-465-08, `:634`).
  - No pay is conditioned on sentiment (US-FTC-465-04, `:664`).
  - The material connection is disclosed on every clip (US-FTC-255-01, `:484`).
- **Raised at:** `counsel_questions.json:45`; compliance seed `:3345`; `README.md:510`

**B6. Comparing platform thumbnails to prove a posted clip is the approved clip** [VI-CQ-05] — To: **COUNSEL**
- **Q:** Do the platforms' terms allow ZBC to hash and compare thumbnails or media returned by their APIs (for
  example TikTok covers), to prove that the posted clip is the approved clip?
- **Why it matters:** Under the default settings nothing certifies, so nothing is paid. The alternative is a
  founder decision to accept metadata-only matching (`VI_REQUIRE_PERCEPTUAL_MATCH=0`).
- **Build default:** Perceptual matching is required, and the TikTok cover hash is off until counsel answers
  (`docs/adr/0007-verification-integrity-architecture.md:294-295`).
- **Raised at:** `counsel_questions.json:478`

### (C) Needed before scale

**C1. ASC 606 principal-versus-agent, per service** [FIN-CQ-03] — To: **CPA**
- **Q:** For each ZBM and ZBC service, is the entity a principal (gross revenue) or an agent (net)?
- **Why it matters:** Month-end close cannot be finalized, and statements stay `draft`.
- **Build default:** ZBC books gross campaign revenue (4010) and creator cost (5010) (`chart.py:34-35`; ADR
  0009 choice 36, `:192-194`).
- **Raised at:** `counsel_questions.json:647`

**C2. Accounting for clawbacks and administrative holds** [FIN-CQ-04] — To: **CPA**
- **Q:** How are netted clawbacks, write-offs and admin holds accounted for?
- **Why it matters:** The close cannot be finalized, and no refund admin fee is allowed.
- **Build default:** `FIN_REFUND_ADMIN_FEE_PCT` must be 0 (`config.py:194-195`). Clawback write-off is allowed
  only after 180 days.
- **Raised at:** `counsel_questions.json:662`

**C3. Is the deposits account restricted cash?** [FIN-CQ-05] — To: **CPA**
- **Q:** Does a contractual restriction make the deposits account "restricted cash" for presentation
  purposes?
- **Why it matters:** Close finalization.
- **Build default:** Accounts 1020, 1040 and 1041 are typed `asset_restricted` (`chart.py:20-22`).
- **Raised at:** `counsel_questions.json:677`

**C4. Backup-withholding base and 1099 amount when clawbacks are netted** [FIN-CQ-16; ADR 0009 choice 6] — To: **CPA**
- **Q:** Is the 24% backup withholding computed on the gross payment, or on the gross less clawback netting?
  Is the 1099-NEC reportable amount gross, or net of netting?
- **Why it matters:** This determines the correct withholding and information returns.
- **Build default:**
  - Withholding is on gross (`config.py:258-260`; `services/finance-py/src/svc_payouts.py:320-322`).
  - The ADR's design text says reportable = gross − netted (`docs/adr/0009-finance-department-architecture.md:128-129`).
- **Raised at:**
  - `fin_rules_seed.json:605`
  - `docs/adr/0009-finance-department-architecture.md:358-363`

**C5. 1099-NEC thresholds and filing setup** [Onboarding P8; compliance row US-IRS-1099NEC] — To: **CPA**
- **Q:** Is $2,000 the 2026 1099-NEC threshold, and what is the indexed threshold from 2027? Are 2026 filings
  needed? If so, by when must the Transmitter Control Code (TCC), the IRS IRIS filing setup and California FTB
  filing be ready?
- **Why it matters:** This is time-sensitive. The ADR notes that the TCC takes up to 45 days and that FIRE
  closes on Nov 19, 2026 (`docs/adr/0009-finance-department-architecture.md:266-267`).
- **Build default:**
  - 2026 is set to 2000.00 (`services/onboarding-py/src/config.py:14`; `services/finance-py/src/config.py:211-215`).
  - In onboarding, an unset year is treated as reportable.
  - The compliance row is unverified (`:1336`).
- **Raised at:**
  - `README.md:416`
  - `docs/adr/0004-onboarding-department-architecture.md:551`

**C6. TIN matching and withholding when a W-9 does not match** [FIN-CQ-06] — To: **CPA**
- **Q:** What first-year TIN-matching and backup-withholding posture should ZBC take?
- **Why it matters:** This decides whether clippers whose TIN does not match can be paid net of 24%, or must
  be blocked.
- **Build default:** `FIN_UNMATCHED_TIN_POLICY=block`. Setting `withhold_24` needs this row verified
  (`config.py:198`; `services/finance-py/src/service.py:284-285`).
- **Raised at:** `counsel_questions.json:692`

**C7. Income sourcing for foreign clippers** [CQ-04 = FIN-CQ-08] — To: **BOTH**
- **Q:** How is income sourced when editing happens abroad but views come from US audiences? How are "days in
  the US" allocated?
- **Why it matters:** All foreign payouts are blocked.
- **Build default:** Foreign payees go through the W-8 path (US-IRS-W8-VALID, `:1423`, unverified). 1042-S is
  not built (`docs/adr/0009-finance-department-architecture.md:226`).
- **Raised at:** `counsel_questions.json:60`, `:712`

**C8. Card acceptance, surcharges and late fees** [FIN-CQ-09] — To: **COUNSEL**
- **Q:** Does Cal. Civ. Code §1748.1 reach business-to-business invoices? What late fee survives Civ. Code
  §1671?
- **Why it matters:** Clients can pay only by ACH or wire, and no late fee can be charged.
- **Build default:** Card payments and late fees refuse to start (`config.py:189-191`; `svc_books.py:320`).
- **Raised at:** `counsel_questions.json:727`

**C9. Who bears the loss on a fraudulent bank transfer (UCC 4A)** [FIN-CQ-12] — To: **COUNSEL**
- **Q:** How does UCC Article 4A allocate loss under the bank's security-procedure agreement?
- **Why it matters:** The bank-controls attestation (FC-12) stays red. It is on the "before a real dollar
  moves" list (`docs/adr/0009-finance-department-architecture.md:249`). The payout-run control check reads
  FC-04, FC-05 and FC-06 only, not FC-12 (`svc_payouts.py:205-221`).
- **Build default:** FC-12 is red.
- **Raised at:** `counsel_questions.json:774`; `fin_rules_seed.json:548-550`

**C10. Music in paid clips** [CQ-21] — To: **COUNSEL**
- **Q:** What music warranty, repost prohibition and indemnity should apply? Does a platform's commercial
  music library license a paid clip?
- **Why it matters:** Every clip with music is blocked.
- **Build default:** Only the platform commercial library is allowed, and only once its rule is verified
  (LG-12, `legal_rules_seed.json:146`).
- **Raised at:** `counsel_questions.json:328`

**C11. DMCA agent, repeat-infringer policy and takedown procedure** [CQ-22; counter-notice checklist; business-day definition] — To: **COUNSEL**
- **Q:**
  - What DMCA agent designation and §512(i) repeat-infringer policy should cover the portal that hosts
    clipper submissions?
  - What is ZBC's exposure as an uploader without safe harbor?
  - Is the build's §512(g)(3) counter-notice checklist correct?
  - Is its business-day definition (Monday to Friday, less US federal holidays) correct?
- **Why it matters:** Portal media hosting is blocked. The repeat-infringer policy is counsel's to write.
- **Build default:** Legal returns counts only, with no policy applied
  (`docs/adr/0010-legal-department-architecture.md:212`). The business-day definition is at `:80-83`.
- **Raised at:**
  - `counsel_questions.json:343`
  - `docs/adr/0010-legal-department-architecture.md:83`, `:264`

**C12. SAG-AFTRA members** [CN-CQ-03] — To: **COUNSEL**
- **Q:** May SAG-AFTRA members be admitted to campaigns? Does the union's 2025 Influencer Waiver apply to
  clipping?
- **Why it matters:** Enrolment of union members is blocked.
- **Build default:** Union members are blocked.
- **Raised at:** `counsel_questions.json:533`; `cn_rules_seed.json:519`

**C13. CASL for messages to Canadian clippers** [CN-CQ-06] — To: **COUNSEL**
- **Q:** What does Canada's anti-spam law (CASL) require for member messages, as opposed to recruiting, sent to
  Canadian clippers?
- **Why it matters:** Canada outside Quebec is a jurisdiction ZBC operates in (HR-05, compliance seed
  `:141`), but email to Canadian clippers is blocked.
- **Build default:** No cold outbound to Canada (HR-08). Member email to Canada is blocked.
- **Raised at:** `counsel_questions.json:573`; `cn_rules_seed.json:565`

**C14. GDPR / UK GDPR representative** [CQ-05] — To: **COUNSEL**
- **Q:** Does onboarding a handful of EU and UK creators qualify for the "occasional processing" exemption in
  GDPR Art. 27(2)? If not, must ZBM appoint representatives?
- **Why it matters:** The UK is an operating jurisdiction, and five EU states are conditional (HR-05 at `:141`,
  HR-06 at `:183`). The question is control-only and blocks no gate.
- **Build default:** No representative is appointed.
- **Raised at:** `counsel_questions.json:75`; compliance seed `:3405`

**C15. Platform developer terms for data V&I keeps or derives** [VI-CQ-01, VI-CQ-02] — To: **COUNSEL**
- **Q:**
  - What retention and use limits do the TikTok, Instagram and X APIs impose?
  - Is fraud screening on YouTube API data, or a cross-clipper library of winning clips, "derived data" or
    prohibited aggregation?
- **Why it matters:** YouTube anomaly signals stay off. Retention rules VI-15c, d and e are unverified.
- **Build default:** A 30-day refresh-or-delete rule. YouTube derived signals are off (VI-15,
  `services/verification-py/seed/vi_rules_seed.json:206`, `:243-267`).
- **Raised at:** `counsel_questions.json:423`, `:433`

**C16. Keeping a refused minor's record** [VI-CQ-03] — To: **COUNSEL**
- **Q:** May V&I keep a minor's refusal record (the result plus identity hashes) to stop re-application?
- **Why it matters:** FTC guidance favors prompt deletion.
- **Build default:** The attestation and identity hashes are kept. Everything else about a minor is purged
  within 24 hours (`services/verification-py/src/service.py:2035`; `.../config.py:52`).
- **Raised at:** `counsel_questions.json:448`

**C17. Record retention periods** [CQ-26] — To: **BOTH**
- **Q:** How long must payroll, contractor, tax and contract records be kept? What must survive a litigation
  hold override?
- **Why it matters:** Legal deletes nothing until this is answered. That conflicts with promises of a clean
  exit and deletion.
- **Build default:** Legal deletes nothing (LG-13, `legal_rules_seed.json:157`). Finance keeps evidence at
  least 2,557 days (`docs/adr/0009-finance-department-architecture.md:205`).
- **Raised at:** `counsel_questions.json:393`

**C18. Insurance** [CQ-23] — To: **COUNSEL** (with a broker)
- **Q:** Which coverages and limits fit, for example media liability, E&O and cyber? Which exclusions would
  gut them?
- **Why it matters:** Insurance is the backstop for A7, B3, B5 and C10.
- **Build default:** No policy. Policy dates are only calendared (LG-11).
- **Raised at:** `counsel_questions.json:353`

**C19. Unauthorized practice of law (Cal. B&P §6125)** [CQ-15] — To: **COUNSEL**
- **Q:** Which agent activities fall inside B&P §6125? What portal language keeps them outside?
- **Why it matters:** The portal FAQ is forced off. The routing notice (`not_legal_advice_v1`) cannot be
  approved. §6126 carries criminal penalties.
- **Build default:** Legal emits only counsel-approved text, and an advice-text guard is on (LG-01,
  `legal_rules_seed.json:15`; `documents.json:244`).
- **Raised at:** `counsel_questions.json:235`

**C20. Timelines for privacy requests (DSARs)** [LG-16] — To: **COUNSEL**
- **Q:** Confirm the deadlines: acknowledge within 10 business days, respond within 45 days, one 45-day
  extension, 90 days at most.
- **Why it matters:** The rule rests on a secondary source and is seeded unverified.
- **Build default:** The rule is unverified (`legal_rules_seed.json:189`).
- **Raised at:** `legal_rules_seed.json:189`

**C21. Placement of the ad disclosure in captions** [Onboarding P9] — To: **COUNSEL**
- **Q:** Is a disclosure marker within the first 100 characters of a caption "clear and conspicuous" for paid
  clips, as the FTC Endorsement Guides require?
- **Why it matters:** Every paid clip relies on this check.
- **Build default:** The draft rule is a marker within 100 characters (`docs/adr/0004-onboarding-department-architecture.md:535-536`).
- **Raised at:** same location

**C22. Is an X impression a payable view?** [VI-CQ-06] — To: **COUNSEL**
- **Q:** Is X's `impression_count` a payable "view" under ZBC's contracts?
- **Why it matters:** X certification is blocked.
- **Build default:** X is off.
- **Raised at:** `counsel_questions.json:493`

**C23. [NOT RAISED IN REPO] Accessibility of client-facing pages and the portal** — To: **COUNSEL**
- **Q:** What accessibility standard does the law require for ZBM and ZBC sites, portals and client
  deliverables (ADA Title III; the EU Accessibility Act for EU consumer e-commerce)? Is automated WCAG 2.1 AA
  testing enough?
- **Why it matters:** The build enforces this only as a house rule, and every publish waits for an
  accessibility checker.
- **Build default:** HR-09 requires automated WCAG 2.1 AA on all content and says overlays are not
  remediation (compliance seed `:317`). EU-EAA is unverified (`:2864`).
- **Raised at:** nowhere as a counsel question

### (D) Nice to know, or needed only if scope changes

**D1. Other ways to hold client money (only if Model A fails)** [CQ-02, FIN-CQ-02, FIN-CQ-14] — To: **COUNSEL**
- **Q:** If ZBC pools client funds (Model B), do the FinCEN payment-processor conditions and Cal. Fin. Code
  §2010(l) cover it? What contract language would be needed? What are the third-party custody options and
  their rules:
  - a bank FBO account;
  - licensed escrow;
  - processor segregation;
  - the FDIC custodial-account recordkeeping rule and pass-through insurance.
- **Build default:** Any custody other than `own_deposit` refuses to start (`config.py:171-172`).
- **Raised at:** `counsel_questions.json:28`, `:632`, `:804`

**D2. Nacha rules for direct ACH** [FIN-CQ-13] — To: **COUNSEL**
- **Q:** If ZBC originates ACH itself, which Nacha reversal rules, data-security duties and fraud-monitoring
  procedures apply?
- **Build default:** Direct ACH is not built.
- **Raised at:** `counsel_questions.json:789`

**D3. Platform-operator reporting for a self-serve marketplace** [CQ-07 = FIN-CQ-15] — To: **BOTH**
- **Q:** Would a self-serve marketplace make ZBC a DAC7, UK or Canada "platform operator" with annual reporting
  duties?
- **Build default:** Activation is blocked for any `marketplace` flag.
- **Raised at:** `counsel_questions.json:105`, `:814`

**D4. New York synthetic-performer disclosure** [CQ-08] — To: **COUNSEL**
- **Q:** Which clips visible in New York need a GBL §396-b synthetic-performer disclosure? What counts as
  "conspicuous"?
- **Build default:** Clips with the `synthetic_performer` flag are blocked.
- **Raised at:** `counsel_questions.json:120`

**D5. France's influencer law** [CQ-06] — To: **COUNSEL**
- **Q:** Does France's Loi 2023-451 Art. 9 (representative and insurance) bind a non-EU agency?
- **Build default:** France is refused (HR-07, `:247`).
- **Raised at:** `counsel_questions.json:90`

**D6. Quebec's Charter of the French Language** [CQ-14] — To: **COUNSEL**
- **Q:** Do clients selling into Quebec trigger Charter of the French Language duties for ZBM's creative work?
- **Build default:** Quebec is refused (HR-07).
- **Raised at:** `counsel_questions.json:216`

**D7. Texas, Indiana and Kentucky privacy law** [CQ-09] — To: **COUNSEL**
- **Q:** Does the Texas TDPSA consent rule for sensitive data reach an SBA small business? Do Indiana and
  Kentucky keep a cure period?
- **Build default:** Control only; no gate is blocked.
- **Raised at:** `counsel_questions.json:135`

**D8. Crawling X and Meta policy pages** [CQ-13] — To: **COUNSEL**
- **Q:** May ZBM crawl X's and Meta's policy pages to detect changes?
- **Build default:** Change detection on X and Meta is blocked.
- **Raised at:** `counsel_questions.json:201`

**D9. TCPA consent for texts** [CN-CQ-07] — To: **COUNSEL**
- **Q:** What TCPA consent standard applies to recruiting or member texts?
- **Build default:** SMS is off (CN-09, `cn_rules_seed.json:172`).
- **Raised at:** `counsel_questions.json:588`

**D10. Reddit and X rules on recruiting posts** [CN-CQ-05] — To: **COUNSEL**
- **Q:** What do Reddit's and X's rules allow for paid-opportunity and recruiting posts?
- **Build default:** Reddit and X recruiting is off.
- **Raised at:** `counsel_questions.json:558`

**D11. Device and IP signals for duplicate accounts** [VI-CQ-04] — To: **COUNSEL**
- **Q:** May device fingerprints and IP addresses be collected to detect duplicate identities?
- **Build default:** Setting this on refuses start (`services/verification-py/src/config.py:168-169`).
- **Raised at:** `counsel_questions.json:463`

**D12. What triggers a litigation hold** [CQ-25] — To: **COUNSEL**
- **Q:** Which events beyond demand letters, subpoenas and complaints trigger preservation? How far does a hold
  reach into clipper-owned accounts?
- **Build default:** The triage matrix (LG-08).
- **Raised at:** `counsel_questions.json:378`

**D13. Trademark classes and clearance** [CQ-27] — To: **COUNSEL**
- **Q:** Should "Z Best Media" and "Z Best Clips" be filed in one class or several? Do the marks clear a search?
- **Build default:** Filings are not `ready`.
- **Raised at:** `counsel_questions.json:408`

### (E) Full-service agency: blocks the first non-RR job (added Oct 5, 2026)

On Oct 4, 2026 the founder directed that ZBM sell every service from day one. The new services are TV and
streaming, radio and audio, social media, creative production, influencer partnerships and websites, alongside
digital, out-of-home, Revenue Recovery and ZBC. The public site now offers all of them (branch
`full-service-v1` of `zbest-sites`; `docs/FULL-SERVICE-DESIGN.md` there). None of the questions below appears
in the repo's register.

**E1. Media spend: principal or agent, and how it is billed** — To: **BOTH**
- **Q:** When ZBM places TV, radio, out-of-home or paid digital media for a client, should the media cost be
  - billed gross (ZBM as principal),
  - billed net (ZBM as agent), or
  - paid by the client directly to the vendor?

  What revenue-recognition (ASC 606 principal-vs-agent), sales-tax and prepayment terms follow from each? Who
  is liable to the vendor if the client does not pay ("sequential liability")?
- **Build default:** No finance line code exists for media spend. `campaign_deposit` is refused for entity
  `zbm` (`services/finance-py/src/svc_books.py:323`; codes at `services/finance-py/src/models.py:349-350`). The
  site says only that payment for media "is written into your agreement".

**E2. Client MSA and SOW addenda covering every service** — To: **COUNSEL**
- **Q:** Can one client MSA plus per-service SOW addenda cover all ten services? The addenda would cover:
  - usage rights and talent/music licenses (creative, influencer),
  - media insertion terms and cancellation windows (TV, radio, OOH),
  - account access and posting approval (social),
  - IP transfer and hosting (websites).
- **Build default:** No MSA or SOW draft exists (§3 table). This is the same document as A2, widened.

**E3. Influencer disclosure and agency liability** — To: **COUNSEL**
- **Q:** Does the influencer page's disclosure answer meet 16 CFR 255 and the FTC's guidance? It says any
  material connection is disclosed "clearly and up front: '#ad' or 'Sponsored' where people will see it,
  alongside the platform's paid-partnership label." What monitoring program does ZBM need as the agency under
  §255.1(f), and what goes in the creator contract?
- **Build default:** Copy only; there is no influencer code. This is related to A3 and C21 (the clipper lane).

**E4. Broadcast and podcast sponsorship identification** — To: **COUNSEL**
- **Q:** For host-read and sponsored spots, what is ZBM's duty versus the station's duty under 47 CFR 73.1212,
  and the podcast network's duty under the FTC guides? What should the insertion order require?
- **Build default:** The site says host reads "must be identified as a paid message".

**E5. Regulated verticals listed on the site** — To: **COUNSEL**
- **Q:** The `/services` page lists "Med spas, clinics, and wellness" and "Legal and professional services".
  Before ZBM targets those verticals, what applies to:
  - tracking pixels and ad targeting for healthcare clients (HIPAA, and the FTC Health Breach Notification
    Rule where relevant),
  - claims in med-spa advertising,
  - state bar attorney-advertising rules for legal clients?
- **Build default:** Industries are listed and no client names appear. The founder is asked whether to keep
  them listed.

**E6. Political advertising (not offered)** — To: **COUNSEL**
- **Q:** If ZBM later offers political advertising (roster department 14), what does it need? This includes:
  - FEC and state "paid for by" disclaimers,
  - the broadcast political-file and lowest-unit-charge rules,
  - platform political-ad authorization,
  - state laws on AI-generated content in political ads.
- **Build default:** No page, deliberately (`docs/BRIEF-AMENDMENT-2026-10-04-full-service.md` in `zbest-sites`).

### Founder decisions with legal or financial effect (not counted as questions)

- **Name a second human approver for payouts.** Until one is named, the build provides "compensating dual
  control, not dual control" (`docs/adr/0009-finance-department-architecture.md:79-87`, `:265`).
- **Accept metadata-only clip matching or not** (the alternative to B6; `docs/adr/0007-verification-integrity-architecture.md:294-295`).
- **Revenue Recovery fee model** (the founder half of A9).
- **Contract storage location.** It is undecided, and onboarding gate 14 is unmet until it is decided
  (`docs/adr/0004-onboarding-department-architecture.md:547`).

---

## 3. Documents the professionals will likely need to draft

Every document below is `counsel_required` in the Legal register. Approval needs counsel's sign-off on the
exact SHA-256 of the version, then Andre's approval (LG-02, `legal_rules_seed.json:28`). **No draft text of
any legal document exists in the repository.** The only document text is a one-line placeholder used in a
test run (`services/legal-py/devtools/live_run.py:230`). The one client-facing wording that does exist is the
disclosure message (A3).

| Document | Needed for | Register entry | Draft in repo? |
|---|---|---|---|
| Outside-counsel engagement letter with AI-use clause `ENG-AI-01` | Everything (A1) | `documents.json:176` | No |
| Client MSA, carrying clause MSA-CCPA-01 | ZBM first payment (A2, A4, A7, A9) | `documents.json:10` | No |
| SOW / campaign brief (each fill needs its own sign-off) | ZBM and ZBC work orders | `documents.json:23`; ADR 0010 `:299-300` | No |
| ZBC campaign order form with the Model A deposit clause | ZBC deposits (B1) | `documents.json:36`; ADR 0009 `:251` | No |
| Clipper / creator agreement (venue, arbitration, forfeiture, clawback, disclosure duty, E-SIGN consent) | ZBC enrolment and payout (B2–B5) | `documents.json:48` | No |
| Independent-contractor agreement (editors) | Blocked by CQ-17 (B3) | `documents.json:62` | No |
| Privacy policy | Every site, form and portal (HR-11; CalOPPA row) | `documents.json:101` | No |
| Website / portal terms of service | Same | `documents.json:114` | No |
| Cookie / consent notice | HR-10 | `documents.json:127` | No |
| Data processing addendum (EU/UK); a US client-data addendum is not in the register (see A8) | EU/UK creators and clients | `documents.json:190` | No |
| DMCA agent designation, takedown and repeat-infringer procedure | Portal hosting (C11) | `documents.json:140` | No |
| Routing notice "not legal advice" | Portal (C19) | `documents.json:244` | No |
| Mutual and one-way NDAs | Deals | `documents.json:75`, `:88` | No |
| Litigation hold, subpoena response and DSAR response templates | Matters, privacy requests (C20, D12) | `documents.json:203`, `:216`, `:229` | No |
| Trademark applications (ZBM, ZBC) | D13 | `documents.json:152`, `:164` | No |
| AI-disclosure first message (P1) | Client and clipper activation (A3) | `services/onboarding-py/src/guardrails.py:139-146` | **Yes, draft text, pending counsel** |
| Messaging templates (17) and the rate card shown to clippers | Clipper communications; rate notice | `cn_rules_seed.json` templates; CN-17 at `:321` | Template variables only; not in the Legal register |
| Music warranty and indemnity terms | C10 | not a separate register entry | No |
| Vendor DPAs (ZBM/ZBC as customer) | Control in `services/compliance-py/src/controls.py:89-90` | not in the register | No |

---

## 4. Count

| Group | Questions | Of which not raised in the repo |
|---|---|---|
| A — blocks first Shopify Revenue Recovery payment | 9 | 3 (A7, A8, A9) |
| B — blocks ZBC campaigns taking money | 6 | 0 |
| C — needed before scale | 23 | 1 (C23) |
| D — nice to know or scope-dependent | 13 | 0 |
| E — full-service agency (added Oct 5) | 6 | 6 |
| **Total distinct questions** | **57** | **10** |

How the total is reached:

- **44 questions from the register.** The 57 register IDs (56 in `counsel_questions.json`, plus FIN-CQ-16)
  collapse to 44 questions. Six rows are declared aliases of other rows (CN-CQ-01, -02, -04 and FIN-CQ-07,
  -08, -15). Related rows that ask the same thing are merged:
  - CQ-10 into CQ-20;
  - CQ-12 into FIN-CQ-11;
  - CQ-17 into CQ-01;
  - CQ-18 into CQ-11;
  - CQ-02 and FIN-CQ-14 into FIN-CQ-02;
  - VI-CQ-01 into VI-CQ-02.
- **3 more questions from the repo, outside the register:** C5 (P8, the 1099 threshold), C20 (LG-16, DSAR
  deadlines) and C21 (P9, caption disclosure). P1, P23 and the ADR 0010 confirmation items are merged into A3,
  A2 and C11, so they add no new questions.
- **4 questions not raised in the repo:** A7, A8, A9 and C23.
- **6 questions added Oct 5 for the full-service direction:** E1–E6.
- **Total:** 44 + 3 + 4 + 6 = **57**.

| Addressee | Count | Questions |
|---|---|---|
| COUNSEL | 45 | All not listed below |
| CPA | 7 | A5, C1–C6 |
| BOTH | 5 | A9, C7, C17, D3, E1 |

---

## Appendix: register ID → packet question

| ID | Q | ID | Q | ID | Q | ID | Q |
|---|---|---|---|---|---|---|---|
| CQ-01 | B3 | CQ-15 | C19 | VI-CQ-02 | C15 | FIN-CQ-04 | C2 |
| CQ-02 | D1 | CQ-16 | B2 | VI-CQ-03 | C16 | FIN-CQ-05 | C3 |
| CQ-03 | B5 | CQ-17 | B3 | VI-CQ-04 | D11 | FIN-CQ-06 | C6 |
| CQ-04 | C7 | CQ-18 | B4 | VI-CQ-05 | B6 | FIN-CQ-07 | B3 |
| CQ-05 | C14 | CQ-19 | A4 | VI-CQ-06 | C22 | FIN-CQ-08 | C7 |
| CQ-06 | D5 | CQ-20 | A2 | CN-CQ-01 | B2 | FIN-CQ-09 | C8 |
| CQ-07 | D3 | CQ-21 | C10 | CN-CQ-02 | B3 | FIN-CQ-10 | A6 |
| CQ-08 | D4 | CQ-22 | C11 | CN-CQ-03 | C12 | FIN-CQ-11 | A5 |
| CQ-09 | D7 | CQ-23 | C18 | CN-CQ-04 | B4 | FIN-CQ-12 | C9 |
| CQ-10 | A2 | CQ-24 | A1 | CN-CQ-05 | D10 | FIN-CQ-13 | D2 |
| CQ-11 | B4 | CQ-25 | D12 | CN-CQ-06 | C13 | FIN-CQ-14 | D1 |
| CQ-12 | A5 | CQ-26 | C17 | CN-CQ-07 | D9 | FIN-CQ-15 | D3 |
| CQ-13 | D8 | CQ-27 | D13 | CN-CQ-08 | A3 | FIN-CQ-16 | C4 |
| CQ-14 | D6 | VI-CQ-01 | C15 | FIN-CQ-01 | B1 | FIN-CQ-02 | D1 |
| | | | | FIN-CQ-03 | C1 | | |

Compliance seed copies of CQ-01…CQ-14 are at `services/compliance-py/seed/compliance_obligations_seed.json:3285-3666`.
Clipper Network copies of CN-CQ-01…08 are at `services/clipper-network-py/seed/cn_rules_seed.json:483-601`.
Finance copies of FIN-CQ-01…16 are at `services/finance-py/seed/fin_rules_seed.json:392-605`.
