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
clickwrap Part C text, naming the MSA version the client accepted and its date. The SOW's `msa_version` must be that
version (and, in a first engagement, the version of the MSA in the same link); otherwise `MSA_VERSION_MISMATCH`. An MSA alone does not open on the
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

**A link the page's log names.** When one link is hit too often, the client-portal log says `link rate-limited: link
<8 hex>`; a link Legal no longer serves logs `link not usable at Legal ... link <8 hex>`. The 8 hex characters are the
link's log fingerprint (the first 8 hex characters of the SHA-256 of its token; the token itself is never logged).
Find it: `curl -sS $A $L/legal/v1/client-acceptance-invitations/by-fingerprint/<8 hex>` -> `links` (each with its
`invitation_id`, `party_ref` and `status`). If it is open and is not the client's own normal use, revoke it as above
and send the client a new one. A used, expired or revoked link costs Legal at most one call every 15 minutes however
often it is replayed, so it needs no action.

## 8. After the first deploy, before the first real link: check the client address the page records (mandatory)

**Do not send any client a link until check B below is done and its result is written down.** Until then nobody
knows which address an acceptance records: possibly Railway's edge, for every client, which would make the IP
evidence in every acceptance worthless and could put every client in one rate-limit bucket.

The page records the client's address (as an HMAC) and keys its per-address limit on it. It reads `X-Real-IP` first
(`PORTAL_CLIENT_IP_HEADER`), else the `X-Forwarded-For` entry `PORTAL_TRUSTED_PROXY_HOPS` from the end. What Railway's
edge puts in those headers is **not documented**: public reports disagree (a moderator says both are overwritten;
a user measured `X-Forwarded-For` as `[client, infrastructure hop]`; on CDN-routed traffic `X-Real-IP` was reported
to be the CDN's address). Junk links cannot use up the page's limits any more (they are refused by their tag before
anything is counted), so this check is about the **evidence**: which address an acceptance records.

You need: a test client `client:zz-test-andre` (Finance refuses to bill any `zz-test-` client: no billing profile, no
invoice), a link for it (fills of the standard templates), your phone on **mobile data** (Wi-Fi off), a computer on
another network, and the evidence key (`PORTAL_EVIDENCE_KEY`). This command prints the HMAC the page would record
for an address:

```sh
python3 -c 'import hmac,hashlib,sys; k=sys.stdin.readline().strip().encode(); print(hmac.new(k, b"ip-v1\x1f" + sys.argv[1].encode(), hashlib.sha256).hexdigest())' <address>
# paste PORTAL_EVIDENCE_KEY, Enter
```

**B (decisive). Which address is recorded.** On the phone, note its public address (any "what is my IP" page), open
the test link, accept it, and read the link's evidence (step 6.2). Compute the HMAC of the phone's address:

| `ip_hmac_sha256` equals | Meaning | Set on `client-portal` |
|---|---|---|
| the phone's address | the edge sets `X-Real-IP` to the client | nothing (the defaults) |
| neither | `X-Real-IP` is not the client (for example a CDN or edge address). Try the forwarded list: set `PORTAL_CLIENT_IP_HEADER=none` with `PORTAL_TRUSTED_PROXY_HOPS=1`, redeploy, repeat B with a new link; if still neither, `2`, then `3` | the first setting that records the phone's address |
| neither, with every setting | the edge gives the page no client address | `PORTAL_CLIENT_IP_HEADER=none` and `PORTAL_TRUSTED_PROXY_HOPS=1`, and record in ADR 0028 that acceptances then record the edge's address, not the client's: the HMAC proves nothing about who submitted, and the electronic-records statement ("we will record ... your IP address") is then not met. Tell the next build session |

Never set `PORTAL_TRUSTED_PROXY_HOPS=0` on the public deploy: every client would share the edge's address.

**A (spoofing).** From the computer, open the test link (a new one: the first is used) with a spoofed header, e.g.
`curl -s -o /dev/null -H 'X-Real-IP: 192.0.2.1' -H 'X-Forwarded-For: 192.0.2.1' https://$PORTAL/accept/<token>`,
then accept it in a browser with the same headers if you can (a header-editing extension), or skip the accept and
rely on B. If an acceptance's `ip_hmac_sha256` equals the HMAC of `192.0.2.1`, the edge passes client values
through: switch to the first setting from B's table that does not, and tell the next build session.

**A2 (independent buckets).** Create **two** test links (one per device: a single link has its own limit, 40 opens
an hour, shared by everyone who holds it). From the computer and from the phone at the same time, each open its own
link 20 times within one minute: neither should see 429 (each address has its own 30-a-minute bucket). If either sees
429, read the client-portal log for that minute: a line `link rate-limited: link <8 hex>` means that link hit its
own hourly limit (opened more than 40 times this hour: wait an hour or make new links and repeat); no such line means
the per-address limit, so the two devices share an address and B's table applies.

Keep the results (the three HMACs and the settings) with your records.

## 9. Change the link key (or the link format)

`LEGAL_LINK_TAG_KEY` (on `legal`) tags every link; `client-portal` reads the same value through its reference
`PORTAL_LINK_TAG_KEY`. Both `/health` answers show `link_tags` with the format `version` and the key's `key_id` (8
hex characters of the key's SHA-256, never the key): they must match. Change the key only if it may have leaked, and
when no client is in the middle of accepting. In this order:

1. On `legal`, set `LEGAL_LINK_TAG_KEY` to a new value (`openssl rand -hex 32`; it must differ from every other
   secret, or Legal refuses to start).
2. Redeploy `legal`. Check its `/health`: `link_tags.status` is `ok` with the new `key_id`. (Between this step and the
   next, every link answers "This link cannot be used".)
3. Redeploy `client-portal` (it reads the new value through the reference only when it restarts). Check its
   `/health`: `link_tags.key_id` equals Legal's. The restart also clears the page's memory of dead links.
4. Every link created before step 1 is dead. For each one you sent and the client has not used (your step 4 records:
   `curl -sS $A $L/legal/v1/client-acceptance-invitations/<invitation_id>` shows its `status`), revoke it (step 7,
   so it stays dead even if the old key ever came back) and create and send a new one (steps 4 and 5).

If Legal's `/health` shows `link_tags.status` starting `off:`, the key is unset or malformed: Legal and its finance
gates keep running, but no link can be created or used (503) until the key is fixed and Legal redeployed.

A change to `src/link_tag.py` (the link format) ships in **both** services from the same commit, `legal` first, then
`client-portal`. If the page's `/health` shows `link_tags.foreign_versions_seen`, links in a format the page does not
read have arrived: the two services run different builds; redeploy both from one commit.

## What the page does and does not do

- The client reads each document's full text on the page, can open or download each one, ticks one unticked box per
  document, and types full name, title and the company signed for (it must match the client's legal name). Nothing
  is recorded until **Accept and sign** succeeds; "Not now" records nothing.
- Recorded at Legal: one acceptance per document, the typed signatory block, the page text as shown and its
  SHA-256, the time, and HMACs of the IP address and browser. The IP address and browser themselves are never stored.
- The link works once, for 14 days at most, for one client and the exact document versions you named. A document
  retired or superseded after you created the link makes it stop working, and approving a newer fill of the MSA or a
  SOW for the same client revokes that client's open links to the older fill.
- A client who refreshes the confirmation, or whose connection dropped after **Accept and sign**, sees the same
  confirmation again (the page asks Legal for the receipt of that exact submission and that exact form); a different
  submission on a used link sees "This link cannot be used".
- A link always names the client's latest approved fill: once you approve a newer fill, a link to the older one can
  neither be created nor used.
- The client's legal name must be written the way a client can type it back (no ™ sign, full-width letters,
  superscripts or non-breaking spaces: write `TM`, plain letters and digits); Legal refuses the link otherwise.
- Legal renders the page's words itself and refuses any other page text, and refuses a company that is not the
  client's legal name on the link.
- No confirmation email is sent automatically yet (step 6.3).
