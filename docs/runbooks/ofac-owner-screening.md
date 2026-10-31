# OFAC screening: clients and their owners

Use this before activating any client, every 90 days after that, before re-activating a client after a rule change,
and whenever a search shows a hit.

**Why owners too.** The MSA promises ZBM will not deal with persons on OFAC's lists or entities **owned 50 percent or
more** by them. Under OFAC's 50 Percent Rule, an entity owned in the aggregate, directly or indirectly, 50 percent or
more by one or more blocked persons is itself blocked, **whether or not the entity is on any list**. Searching only
the client's own name cannot catch that. Today the Compliance service records only the client-name search
(`lane: client`); owner searches are recorded in your evidence folder by this procedure.

**Time: 15 minutes for a client with one or two owners.**

---

## A. Collect the facts (from the client, in writing)

Ask the client for these before activation (template F.1). Keep the reply email.

1. The client's exact legal name, any trade names (DBA), and its state (or territory) of formation and entity number
   (from the Secretary of State registration).
2. Its registered address and principal place of business.
3. **Every person or entity that owns 25% or more of it**, directly or indirectly: full legal name, country, and for
   an entity, its own state/country of formation. Ask also: "Does any combination of owners who are not US persons
   together own 50% or more?" and "Is any owner, director or officer on an OFAC list, or owned by someone who is?"
4. For an owner that is itself an entity: **its** 25%+ owners, one level up, until you reach people.

Why 25%: it is the usual beneficial-ownership threshold, and it catches the combinations that can add up to 50%.
OFAC's rule aggregates: two blocked owners at 25% each make the client blocked.

If the client will not tell you who owns it, do not activate it.

## B. Search (each name: the client, every DBA, every owner)

1. Open **https://sanctionssearch.ofac.treasury.gov/**.
2. Search type: name. Select **both** the SDN list and the Consolidated (non-SDN) lists. Leave the minimum name score
   at the page's default (do not raise it to hide fuzzy hits).
3. Enter the name exactly as given. For a person, also try the surname alone if the full name returns nothing and the
   name is common. For an entity, also try without "Inc", "LLC" or punctuation.
4. Save the result page as a PDF (browser → Print → Save as PDF), showing the search terms, the lists searched, the
   result (including "no results"), the date and time, and the list date the page shows.
5. File name: `<client id>_<YYYY-MM-DD>_<role>_<short name>.pdf`, for example
   `acme_2026-11-02_owner_jdoe.pdf`. Folder: your evidence folder, `~/zbm/ofac/<client id>/` (`chmod 700`), backed up.
6. Record it in the screening log (section E).

## C. Results

### C.1 No results for every name

1. Record each search in the log (E).
2. Record the client-name search in Compliance exactly as `docs/deploy/railway.md` → Compliance step G.1-G.2 says
   (`result: no_match`, `evidence_sha256` of the client-name PDF). For owners, Compliance has no field yet: put the
   owner PDFs and the log row in the same folder, and compute one combined fingerprint so the client record can point
   at them:

   ```sh
   cd ~/zbm/ofac/<client id> && shasum -a 256 *_<YYYY-MM-DD>_*.pdf > owners-<YYYY-MM-DD>.sha256 && shasum -a 256 owners-<YYYY-MM-DD>.sha256
   ```

   Write that last fingerprint in the log row's "owner bundle sha256" column.
3. Continue the activation (step G.3 onward).

### C.2 A possible match (any hit, any score)

Do **not** activate, invoice or take payment while you check. Follow OFAC's own steps (OFAC FAQ 5):

1. **Which list?** Is the hit on an OFAC list (SDN or a non-SDN list), or on another agency's list included in the
   search? For another agency's list, that agency decides what it means.
2. **Compare the details.** Open the full entry (click the name). Compare with what the client gave you:
   - Name: how much of it matches? A shared common surname alone is weak.
   - For a person: date of birth, nationality, ID numbers, place of birth.
   - For an entity: address, country, registration number, the program listed.
   - Is the listed address different from your party's address?
3. **Decide and record:**
   - **Clearly not the same party** (several details differ, none of the identifying ones match): this is a false
     positive. Write a short clearing note in the folder: which entry, which details you compared, which differ, your
     conclusion, date, your name. Save the entry's page as PDF too. Then record the client search in Compliance as
     `no_match` **with `evidence_sha256` of a single PDF that contains the search result, the entry and your clearing
     note** (combine them into one PDF), so the record itself points at how the hit was cleared. Note "cleared false
     positive" in the log.
   - **Several details match, or you cannot tell:** treat it as a likely match. Do not serve the client. Contact the
     OFAC Compliance Hotline (https://ofac.treasury.gov/ofac-compliance-hotline; contact details at
     https://ofac.treasury.gov/contact-ofac) and describe the case. Record nothing as `no_match` until it is resolved.
     If you record it in Compliance meanwhile, record `potential_match`: that opens a hold that cannot be released by
     the normal paths (by design).
   - **A true match, and no money has moved:** refuse the engagement. Do not tell the client why in a way that helps
     evade sanctions; "we are unable to take on this engagement" is enough. If a transaction was attempted, read
     section D: a rejected transaction may itself need a report.
4. Time limit: 2 business days to clear or escalate. Do not let a possible match sit.

### C.3 A match after money has arrived

Follow `docs/deploy/railway.md` → Compliance step G.4, "A payment Compliance holds", `sanctions: true`. In short:

1. Do not refund, return or move the money. Finance refuses refunds and releases for it.
2. Contact Stripe support and tell them what you found, so neither side moves the funds.
3. Decide with the program's rules whether the property is **blocked** (a blocked person has an interest in it) or
   the transaction is **rejected**.
4. File the report through the **OFAC Reporting System (ORS)** within **10 business days**: a report of blocked
   property (31 CFR 501.603(b)(1)) or of a rejected transaction (31 CFR 501.604), with the supporting documents.
5. Put **September 30** in the calendar: if ZBM still holds blocked property on **June 30**, the Annual Report of
   Blocked Property is due by September 30 (31 CFR 501.603(b)(2); form TD F 90-22.50 through ORS; OFAC FAQ 50).
6. Leaving the hold needs an OFAC document (license, unblocking report for a false positive under FAQ 1196, or a
   compliance release under 31 CFR 501.806), as the deploy runbook describes.

## D. Re-screening

- **Every 90 days** for every active client (Andre's rule; the Compliance record's `valid_until` is the search date +
  90 days, and the activation needs a search no more than 30 days old). Re-screen at day 75 so nothing lapses.
- **Whenever ownership changes** (ask clients to tell you; the MSA requires their statements to stay true).
- **Whenever OFAC adds a major designation** you hear about that could touch a client's country or industry.
- Re-screen the client name **and every owner**, the same way as B, and add new log rows.

## E. Screening log (the record you keep)

Keep a spreadsheet or text file `~/zbm/ofac/screening-log.csv` (backed up). One row per search:

```text
date_utc,client_id,role(client|dba|owner),name_searched,lists,list_date,result(no_results|cleared_fp|likely_match|match),evidence_file,evidence_sha256,owner_bundle_sha256,compliance_attestation_id,notes
```

**Retention: 10 years.** OFAC extended its recordkeeping requirement (31 CFR 501.601) from five to ten years
(interim final rule Sept. 2024, adopted as final effective March 21, 2025). Keep the log and every evidence PDF for
10 years after the search, even after the client's other data is deleted ([client-offboarding.md](client-offboarding.md)).

## F. Templates

### F.1 Ownership request (before activation)

> **Subject: Two quick details we need before we start**
>
> [Name], before we start work we screen every client against the US Treasury's sanctions lists (OFAC), as US law
> requires. Please reply with:
>
> 1. [Client]'s exact legal name, any trade names, its state of formation and its entity number.
> 2. The name and country of each person or company that owns 25% or more of [Client], directly or through another
>    company. If a company owns 25% or more, please also list who owns 25% or more of that company.
>
> We keep this confidential and use it only for this check. Thank you. Andre

### F.2 Unable to proceed (likely or true match)

> **Subject: Your engagement with Z Best Media**
>
> [Name], thank you for your interest. We are unable to take on this engagement. We have not charged you. Andre

## Sources

Checked on 2026-10-30.

- OFAC, "Revised Guidance on Entities Owned by Persons Whose Property and Interests in Property Are Blocked" (Aug 13,
  2014): https://ofac.treasury.gov/media/6186/download ("owned in the aggregate, directly or indirectly, 50 percent
  or more by one or more blocked persons"; blocked "regardless of whether the entity itself is listed"; caution about
  significant minority ownership and control).
- OFAC FAQ 401 (indirect ownership through chains; aggregation; due diligence): https://ofac.treasury.gov/faqs/401
- OFAC FAQ 5 (what to do with a potential match): https://ofac.treasury.gov/faqs/5
- OFAC reporting FAQs 49 (10 business days, ORS), 50 (annual report as of June 30, due Sept 30), 1196 (unblocking
  after an error): https://ofac.treasury.gov/faqs/topic/1606
- OFAC final rule extending recordkeeping in 31 CFR 501.601 from 5 to 10 years, effective March 21, 2025:
  https://www.govinfo.gov/content/pkg/FR-2025-03-21/pdf/2025-04864.pdf
- Sanctions List Search: https://sanctionssearch.ofac.treasury.gov/
- The Compliance gate procedure: `docs/deploy/railway.md` → "Compliance (38): the hard gate" (on branch
  `feat/compliance-gate-deploy` until merged).
