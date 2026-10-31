# Runbook: a client accepts the MSA and a SOW (ADR 0028)

For Andre. How a client accepts Z Best Media's documents, how you check it, and how you take a link back. The client
acceptance page is the only way a client's acceptance is recorded; Legal refuses every other way, including your
onboarding token. Design: `docs/adr/0028-client-acceptance-page.md`. One-time setup on Railway:
`docs/deploy/railway.md`, "Client acceptance page (ADR 0028)".

What you need open: the Legal tunnel and header files from `docs/deploy/railway.md`, "Founder contract approval
(ADR 0022)", step 1 (`$L` is `http://127.0.0.1:18420`, `$A` sends Legal's service token and your approval token).
Every `request_id` is yours to pick once (`[A-Za-z0-9._:-]`, at most 128 characters).

## 1. Once: ZBM's own facts

The documents name ZBM's legal entity, its type and state, its address, the Search Console service account and the
secure credential channel. They live in Legal's configuration, never in the repository and never in a request. In the
document texts they are written as `{{zbm_...}}` variables:

| In the text you wrote | Write instead | Value in `LEGAL_ZBM_FACTS` |
|---|---|---|
| `[ZBM LEGAL ENTITY NAME]` | `{{zbm_legal_entity_name}}` | the entity's exact legal name |
| `[ZBM ENTITY TYPE AND STATE OF FORMATION]` | `{{zbm_entity_type_and_state}}` | e.g. `a California limited liability company` |
| `[ZBM STREET ADDRESS]` | `{{zbm_street_address}}` | the street address |
| `[ZIP]` | `{{zbm_zip}}` | the ZIP code |
| `[ZBM GSC SERVICE ACCOUNT EMAIL]` (SEO SOW) | `{{zbm_gsc_service_account_email}}` | the Search Console service account's email |
| `[SECURE CREDENTIAL CHANNEL]` (Revenue Recovery SOW) | `{{zbm_secure_credential_channel}}` | how clients send you app credentials |

Railway -> `legal` -> Variables -> `LEGAL_ZBM_FACTS`, one line of JSON with the names in the last column, for example
`{"zbm_legal_entity_name": "...", "zbm_entity_type_and_state": "...", "zbm_street_address": "...", "zbm_zip": "...",
"zbm_gsc_service_account_email": "...", "zbm_secure_credential_channel": "..."}`. Legal refuses to start on a
malformed value. Changing a fact later changes only fills made after the change; every fill is approved by you on its
own hash, so a client always accepts exactly the text you approved.

## 2. Once per template: upload and approve

As `docs/deploy/railway.md` "Founder contract approval (ADR 0022)", steps 2-7 (LG-20, the CQ-19 founder
determination, uploads, founder approval), with one difference: the MSA is a template too, because it names ZBM's
facts. Each template's `template_variables` lists every `{{name}}` in its text, the `zbm_*` ones included, each as
`{"type": "string", "max_length": 500}`. The SOW templates keep their client variables (`client_legal_name`, the fee
and term variables, `msa_version`, ...). No `[BRACKET]` is left anywhere.

## 3. Per client: fill, read, approve

For client `<id>` (Finance's client id; Legal's party is `client:<id>`):

1. Fill the MSA for the client (no client variables: Legal takes every `zbm_*` value from `LEGAL_ZBM_FACTS`):
   `curl -sS $A -H 'Content-Type: application/json' -d '{"request_id": "msa-fill-<id>-1", "entity": "zbm", "party_ref": "client:<id>", "variables": {}}' $L/legal/v1/documents/client_msa/versions`
   -> 201 with the fill's `version` and `sha256`. `ZBM_FACT_MISSING` names a fact `LEGAL_ZBM_FACTS` lacks.
2. Fill the SOW with the client's values, `msa_version` being the MSA fill's version from 1:
   `curl -sS $A -H 'Content-Type: application/json' -d @sow-fill-<id>.json $L/legal/v1/documents/sow_seo_audit/versions`
   with `{"request_id": "sow-fill-<id>-1", "entity": "zbm", "party_ref": "client:<id>", "variables": {...}}`.
3. Read both texts before you approve them:
   `curl -sS $A $L/legal/v1/documents/<doc_id>/versions/<version>/text`.
4. Approve each fill as founder (one line of rationale each):
   `curl -sS $A -H 'Content-Type: application/json' -d '{"request_id": "<doc>-fap-<id>-1", "version_sha256": "<sha256>", "rationale": "<20-2000 characters>", "founder_confirms": true}' $L/legal/v1/documents/<doc_id>/versions/<version>/founder-approval`.

## 4. Create the link

```sh
curl -sS $A -H 'Content-Type: application/json' -d @link-<id>.json $L/legal/v1/client-acceptance-invitations
```

`link-<id>.json`:

```json
{"request_id": "link-<id>-1", "party_ref": "client:<id>",
 "documents": [{"doc_id": "client_msa", "version": "<msa fill version>", "doc_sha256": "<msa fill sha256>"},
               {"doc_id": "sow_seo_audit", "version": "<sow fill version>", "doc_sha256": "<sow fill sha256>"}],
 "display": {"client_legal_name": "<the client's exact legal name>", "notice_email": "<the client's notice email>",
             "contact_first_name": "<first name>", "sow_number": "<SOW number>", "sow_title": "<SOW title>"},
 "expires_in_days": 14}
```

The answer holds `token` and `accept_path` **once**. The link is `https://<the client-portal domain><accept_path>`.
Keep the answer's `invitation_id`. Legal keeps only the token's hash: if you lose the answer, revoke this link
(step 7) and create a new one; the same request sent again answers `token: null`.

Refusals tell you what is wrong with a document: `VERSION_NOT_IN_FORCE` (not approved yet), `INSTANCE_NOT_BOUND`
(another client's fill), `TEMPLATE_NOT_ACCEPTABLE` (the template itself, not a fill), `VERSION_SUPERSEDED` (a newer
standard form exists), `TEMPLATE_NOT_IN_FORCE` (the fill's template was retired), `EVIDENCE_INSUFFICIENT` (no CQ-19
founder determination in force), `ACCEPTANCE_HASH_MISMATCH` (wrong sha256).

A new SOW for a client who already accepted the MSA: the same request with the SOW alone. The page shows the
clickwrap Part C text, naming the MSA version the client accepted and its date. An MSA alone does not open on the
page (`page: "msa_only"`, with a warning): always send the MSA with the client's first SOW.

## 5. Send the link (clickwrap text v1, Part A)

From your ZBM mailbox, to the client contact on record only. Subject: **Your agreement with Z Best Media is ready to
review**. Body:

> Hi {first name},
>
> Your documents for {client legal name} are ready. Please review and accept them here:
>
> {link}
>
> This link is unique to {client legal name}. It expires on {expiry date} and stops working once the documents are
> accepted. Please do not forward it to anyone who is not authorized to sign for {client legal name}.
>
> If you were not expecting this email, reply to it or write to legalnotice@zbestmedia.com.
>
> Z Best Media

(Part A's exact words; only the values in braces are filled. The expiry date is the answer's `expires_at`.) Never paste the link anywhere else.

## 6. Check the acceptance, then send the confirmation (Part D)

1. `curl -sS $A $L/legal/v1/client-acceptance-invitations/<invitation_id>` -> `"status": "accepted"` and two
   `acceptance_ids`. `open` with `"usable": false` lists why the link stopped working (a retired template, a newer
   MSA): fix it and send a new link. `expired`: send a new link.
2. `curl -sS $A $L/legal/v1/client-acceptance-invitations/<invitation_id>/evidence` -> the signatory's name, title and
   company exactly as typed, the time, each acceptance's id, version and fingerprint, the page text as shown
   (`page_text_b64`) and the HMACs of the client's IP address and browser.
3. **Send the confirmation email by hand** (no mail provider is wired to the page yet; the page told the client that
   Z Best Media will email the copy). The clickwrap text v1, Part D, word for word, with the values from 2, ZBM's
   entity name and address from `LEGAL_ZBM_FACTS`, and each document's exact text as an attachment: save it with
   `curl -sS $A $L/legal/v1/documents/<doc_id>/versions/<version>/text` and check
   `shasum -a 256 <file>` equals the fingerprint before you attach it. Send it to the `notice_email` of the link.
4. Finance: set the client's billing profile with the MSA's acceptance
   (`PUT /fin/v1/clients/<id>/billing-profile`, `msa: {doc_id, version, doc_sha256, acceptance_id}`), and draft the
   invoice with the SOW's (`legal_ref`). `GET /fin/v1/clients/<id>/billing-readiness` must say `allowed: true`
   before you approve the invoice. Finance refuses an invoice whose acceptance is not the page's, not this client's,
   or not this exact document and hash.

## 7. Take a link back

Before it is used: `curl -sS $A -H 'Content-Type: application/json' -d '{"request_id": "revoke-<id>-1"}'
$L/legal/v1/client-acceptance-invitations/<invitation_id>/revoke` -> `"status": "revoked"`. The client then sees
"This link cannot be used", the same page as for any unknown link. A used link cannot be revoked: the acceptance
stands. To end a contract, follow the contract's own terms; to stop a template being accepted again, retire it
(`decision: retire`), which also stops every open link that names one of its fills.

## 8. Once, after the first deploy: check the client address the page records

The page keys its rate limits and the recorded IP HMAC on the last `X-Forwarded-For` entry Railway's edge adds
(**UNVERIFIED** until you do this). Create a link for a test client (`client:andre-test`, with test fills), open it
from your phone on mobile data, accept it, and read its evidence (step 6.2). Then compute the HMAC of your phone's
public IP address (shown by any "what is my IP" page at that moment) with the evidence key:

```sh
python3 -c 'import hmac,hashlib,sys; k=sys.stdin.readline().strip().encode(); print(hmac.new(k, b"ip-v1\x1f" + sys.argv[1].encode(), hashlib.sha256).hexdigest())' <your phone's IP>
# paste PORTAL_EVIDENCE_KEY, Enter
```

Equal to the evidence's `ip_hmac_sha256`: the setting is right. Not equal: tell the next build session (the page
then records Railway's address, and its per-address limit is one shared limit). The same command, with a disputed
IP address, answers whether that address submitted an acceptance.

## What the page does and does not do

- The client reads each document's full text on the page, can open or download each one, ticks one unticked box per
  document, and types full name, title and the company signed for (it must match the client's legal name). Nothing
  is recorded until **Accept and sign** succeeds; "Not now" records nothing.
- Recorded at Legal: one acceptance per document, the typed signatory block, the page text as shown and its
  SHA-256, the time, and HMACs of the IP address and browser. The IP address and browser themselves are never stored.
- The link works once, for 14 days at most, for one client and the exact document versions you named. A document
  changed, retired or superseded after you created the link makes it stop working.
- No confirmation email is sent automatically yet (step 6.3).
