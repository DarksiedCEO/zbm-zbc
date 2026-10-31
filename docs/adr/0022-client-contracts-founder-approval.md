# ADR 0022 — Client contracts: founder approval (Andre, 2026-10-30)

Status: **proposed, not merged** (local branch `feat/legal-founder-contract-path` from `golive-next-2026-10-30`
8d20e83, Oct 30 2026). Amends ADR 0010 (Legal) for the client commercial contracts and one CQ row, adds two SOW
document ids and a placeholder guard, and adds a renewal reminder to ADR 0009 (Finance). Not AEGIS-audited yet.

## Context

Finance issues an invoice only when Legal confirms that the contract version is current and that the client's
acceptance matches it with sufficient evidence (ADR 0009, FIN-29; `finance-py` `HttpLegal.document_status`). Under
ADR 0010 as built, a client contract version could become current only through the LG-02 sign-off record (LG-02,
LG-17), and a clickwrap acceptance became evidence-sufficient only after a filed memo verified CQ-19 (LG-07, LG-18).
LG-02, LG-07 and LG-18 are founder-locked. Those gates were written by build sessions, not ruled by Andre.

On Oct 30 2026 Andre ruled that he approves the client contracts himself. Precedent: on Oct 19 2026 he replaced an
outside-sign-off requirement for the OOH principal/agent determination with a founder determination he alone sets,
with the rationale recorded on the ledger (project doc `claude/ooh-decisions-2026-10-16.md`, "Principal vs agent").
This ADR applies the same pattern to client contracts.

## Decision

1. **New founder rule LG-20** (`kind: founder`, in the rules seed). It states the founder path as a rule of its own
   and names the documents. LG-01, LG-02, LG-07 and LG-18 keep their text and their lock, and still govern every other
   document and every other CQ row. The path opens only while LG-20 is `verified` in the Andre-approved rule version
   in force (`FOUNDER_PATH_NOT_IN_FORCE` otherwise). A Legal whose rule version was approved before this ADR adds
   LG-20 first: an `add` proposal, not a weakening, which Andre approves. LG-20 is cited by the reason catalog, so it
   cannot be retired, and as a founder rule it cannot be reworded. Andre can close the path by amending LG-20 to
   `unverified` (a tightening). Reopening it after that needs a code change.
2. **Documents** (`seed/documents.json`, new field `founder_approval_allowed`): `true` only for `client_msa`, `sow`,
   `sow_seo_audit`, `sow_revenue_recovery` and `zbc_order_form`.

   **Two new document ids:** `sow_seo_audit` and `sow_revenue_recovery` (`doc_type: sow`, ZBM). A template fill uses
   the highest in-force template *of its document*, so one shared `sow` id allowed only one live SOW template. With
   one id per template, both are live. `sow` stays for ZBC and existing references. Finance looks a contract up by the
   `doc_id` in its `legal_ref`, so it needs no change for the new ids.
3. **Founder approval** of a version: `POST /legal/v1/documents/{doc_id}/versions/{v}/founder-approval`, with
   Andre's token only (`X-Andre-Approval-Token`, the existing `FounderGate`).
   - **Refusals:** a missing or wrong token gets 403 and is recorded as `founder_approval_refused`. A refused
     precondition gets 409 and is recorded as `document_approval_refused`; for the CQ-19 determination route it is
     `founder_determination_refused`.
   - **Body:** `version_sha256` (must equal the version's hash), `rationale` (20-2000 characters, required),
     `founder_confirms: true`, and optionally `effective_at` and `renew_superseded`.
   - **Reading the rationale back:** `GET .../versions/{v}/founder-approval/rationale` and
     `GET /legal/v1/register/CQ-19/founder-determination/rationale` return Andre's rationale text. They take Andre's
     token only; every other token gets 403 (AEGIS M4).
   - **Preconditions:** every other approval precondition stays (draft status, `approval_blocked_by`, required
     clauses, placeholder guard).
   - **Recorded on the version:** `approval_basis: "founder_approval"`, `approved_by: "andre"` and
     `founder_rationale_sha256`. The rationale text is a blob: only its SHA-256 reaches the ledger and the exports.
   - **Ledger event:** `document_version_founder_approved` (actor `andre`; payload `approval_basis`, `approved_by`,
     `rationale_sha256`, `rule_id: LG-20`).
   - **`review_by`:** set as for any approval (`LEGAL_MEMO_REVIEW_DAYS`, default 90 days).

   **Renewal.** The 90-day date stays as a renewal reminder. It does not cut off live clients. Sending the same
   founder-approval request again on a founder-approved version (same hash, before or after its `review_by`) moves the
   date (ledger event `document_version_founder_renewed`). The version and its hash do not change, so every client
   acceptance of it stays valid. A version approved through the memo sign-off path is not renewed this way
   (`VERSION_NOT_APPROVABLE`).
   - **A renewal re-runs every document check** (AEGIS L2): the founder flag, `approval_blocked_by`, required
     clauses and the placeholder guard.
   - **A superseded version is not renewed by default** (AEGIS L1): if a newer approved version of the same instance
     exists (same party, or both standard forms), renewal is refused with `VERSION_SUPERSEDED` unless the body says
     `renew_superseded: true`.

   **Filled SOWs** (per-client `{{placeholder}}` values) get a lightweight founder approval each.
   - Andre's token may now fill the current template with `variables` and `party_ref` and no `text`, exactly as the
     `scheduler` caller does. Only the scheduler could fill before, and none is deployed.
   - The fill is a new draft bound to that client (`review_label: founder_template:<doc>@<v>`).
   - Andre then founder-approves the fill's hash with a one-line rationale before the client can accept it. No fill is
     ever current without its own approval.
   - That approval is refused with `TEMPLATE_NOT_IN_FORCE` once the template the fill was made from is retired,
     withdrawn or expired (AEGIS M3).
   - **Retiring a template reaches its fills** (AEGIS r2 N7). While the template a fill was made from is not
     approved and in force, Legal refuses with `TEMPLATE_NOT_IN_FORCE`:
     - a founder approval of the fill;
     - a renewal of the fill;
     - a new acceptance of the fill.

     An acceptance already recorded stays valid, and the fill stays approved, until the fill's own `review_by`
     passes (it cannot be renewed), or until Andre withdraws the fill. Retiring the template does not withdraw its
     fills by itself.
   - `GET /legal/v1/documents/{doc_id}/current` never returns a version bound to one party (a client fill or a
     per-client upload). For a SOW it answers with the template, so client B never sees client A's fill (AEGIS M5).
     Each party accepts its own version by number and hash.
   - This is the simplest safe option: approvals stay hash-bound, and Andre sees each client's price and terms before
     they become a contract.

   **Unfilled placeholder guard** (AEGIS H1, M1; r2 N1-N4, N9; r3 C1-C3). On both approval paths Legal refuses a
   version whose text holds one of the placeholder forms listed below. The guard is a **backstop to Andre's own
   reading of the text, not a guarantee**: it detects exactly these forms, and an unfilled field written any other
   way can still pass. Andre reads every version before he approves its hash.

   Before matching, the text is prepared in this order:
   - HTML entities are decoded and NFKC is applied, repeated until stable (at most 3 passes, so `&amp;#91;` is
     decoded too);
   - every format character is removed (Unicode category Cf: zero-width space/joiners, word joiner, BOM, soft
     hyphen, direction marks);
   - line endings and curly quotes are folded;
   - every Unicode opening/closing punctuation pair (categories Ps/Pe, such as `【】`, `「」`, `⟦⟧`, `❲❳`, `⁅⁆`, `｟｠`)
     is read as a square bracket. Round brackets `()`, curly brackets `{}` and the low-9 quotes `‚` `„` are not.

   **Detected** (each counts once; overlapping detections of one field count once):
   - **any `[...]` span**: empty, any content (numbers too: `[30]`, `$[500]`, `[30,000]`), any length, wrapped over
     single line breaks. The only exceptions are listed below;
   - **an unclosed `[`**: one with no `]` before a blank line;
   - `<<...>>`, `«...»`, `‹...›`, `≪...≫`;
   - a single `{...}` that is blank, all capitals, names a field, or is an identifier or words (`{client_name}`,
     `{client name}`, and so `${client_name}` too); a single `<...>` that is blank, all capitals, names a field, or is a snake_case identifier
     (`<client_name>`). The field words are insert, TBD, TBC, TBA, TODO, placeholder, name, fee(s), price, amount,
     date, address, client, party, company, entity, email, phone, title, rate and term;
   - a `{{...}}` field that is not one of the version's declared template variables. A text holding a `{{...}}` with
     no template schema at all (`{{ fee }}`, `{{{FEE}}}`) is already refused at upload (422) and never becomes a
     version;
   - **blanks**: three or more underscores anywhere, a currency sign (`$`, `€`, `£`) before them, and `__/__/____`.
     The one exception is a line that is exactly `<label>: ____` (whitespace allowed, and more underscore runs only
     after whitespace, as in `By: ___ ___`; nothing else on the line) with the label one of By, Name, Title, Date,
     Signature, Its, Signed or Initials, in any case. So
     `Date: ____ Fee: ____`, `Name of Client: ____` and `Its monthly fee shall be ____` are refused;
   - an X-amount (`$X,XXX`, `$XX`) or a placeholder date (`XX/XX/XXXX`, `00/00/0000`, `MM/DD/YYYY`);
   - a free-standing `TBD`, `TBC`, `TBA` or `TODO` in any case, or `T.B.D.`;
   - drafting forms outside any bracket (money red team RTM-04):
     - X-runs `XXX` or longer (`XXXX`, `USD XXX`) and dates with an `XX` part (`XX/XX/2026`, `01/XX/2026`);
     - a parenthesis that opens with insert, enter, placeholder, TBD/TBC/TBA or "to be agreed / updated /
       confirmed / determined / completed / provided / inserted" (`(insert fee)`, `(INSERT CLIENT NAME)`,
       `(to be agreed)`);
     - template delimiters `%%...%%`, `%CLIENT_NAME%`, `__FEE__` (capitals only, so markdown `__bold__` passes) and
       `#FEE#` (capitals only, so headings and `#hashtags` pass);
     - `Lorem ipsum`, the word `placeholder` in any case, `??`, `TK` / `TKTK`, and two or more block or shade
       characters (`████`);
     - "name / fee / amount / address / price / email / details / text here" (`Client name here`);
     - a label followed by nothing but a dash (`Fee: —`), and a sentence that ends on "the date of", "as of" or
       "between" followed by `.` or `;` (`Effective Date: the date of.`). A line that merely wraps after those
       words is not counted.

   **A `[...]` span is not counted** only when it is one of these:
   - **exactly** one of these boilerplate phrases (case-insensitive, whitespace collapsed, a trailing `.` dropped;
     a fixed list, not a pattern): `SIGNATURE PAGE FOLLOWS`, `SIGNATURE PAGES FOLLOW`,
     `REMAINDER OF PAGE INTENTIONALLY LEFT BLANK`, `REMAINDER OF THIS PAGE INTENTIONALLY LEFT BLANK`,
     `THIS PAGE INTENTIONALLY LEFT BLANK`, `INTENTIONALLY LEFT BLANK`, `INTENTIONALLY OMITTED`, `RESERVED`, `N/A`,
     `NOT APPLICABLE`, and `EXHIBIT` / `SCHEDULE` / `ANNEX` / `APPENDIX` / `ATTACHMENT` followed by one letter A-Z,
     a number 1-99, or a letter and a number 1-99 joined by `-` (`EXHIBIT A-1`);
   - a numbered footnote `[^1]` to `[^999]`, except right after a currency sign (`$[^500]` counts). A footnote
     label with letters, such as `[^FEE]`, counts;
   - a list marker (`[a]` to `[z]` or `[A]` to `[Z]`, except `[x]` and `[X]`, or `[i]` to `[ix]`) in list-marker
     position only: at the start of a line, right after `(`, or right after a section reference (`Section 4 [a]`,
     `§4[c]`). Anywhere else (`Fee: [b]`, `Fee: [B]`) it counts;
   - a markdown link `[text](target)` whose target is an `http(s)://` URL or a `#anchor` that exists in the text (a
     heading's slug, `{#id}` or `id="..."`), and whose text is neither all capitals nor holds a drafting word
     (insert, TBD, TBC, TBA, TODO, placeholder).

   **Not detected** (Andre's reading is the only check for these):
   - bare capitalised names with no brackets, such as `CLIENT NAME` or `Acme CLIENT LLC`;
   - parenthesised fields that do not open with a drafting word, such as `(CLIENT NAME)` or `$(        )`;
   - a boilerplate phrase or exhibit label used where a value belongs (`Monthly fee: [Reserved]`, `[N/A]`);
   - mixed-case link text to a real URL or anchor (`[Amount to be agreed](#fees)`);
   - wording that defers a term without a token or parenthesis (`a fee to be agreed`), single or double X's
     (`X%`, `USD XX`), a zero amount with no note (`$0.00`), other currency signs before X-amounts, `t.b.d.` in
     lower case, a sentence left unfinished without a closing `.` or `;`, look-alike letters from other scripts (`ТВD` in Cyrillic),
     blanks drawn with other characters (`$..........`, `$——————`, `_ _ _ _ _`), and checkboxes (`☐ $500`);
   - template syntaxes other than those listed, such as lower-case `__fee__` (the brace, angle and bracket forms
     `{% client %}`, `<%= fee %>` and `[% fee %]` are caught by the rules above);
   - anything else that is not one of the detected forms above.

   **Cost.** Every step is one regex pass with no overlapping repetition, or a bounded look-around: line
   boundaries are computed once, each line's signature-line test runs once, and a list marker looks at most 60
   characters back. Checking is therefore linear in the text. Measured CPU time on the shared build host
   (AEGIS r3/r4 stress forms, 1 MB each): 0.1-0.8 s; a 5 MB signature-line worst case (`By: ` + 5 million `_` +
   `x`): about 1.1 s. The tests bound each 1 MB form at 2 s of CPU time.
   - The count runs on the version's content-addressed blob **outside** the service-wide lock, on both approval
     routes (founder approval and the memo sign-off `decision`), so a long text never stalls the other Legal
     routes.
   - The idempotency record is checked first: a replayed request is answered from it and never counted again.

   The refusal is `UNFILLED_PLACEHOLDER` (LG-02). It reports how many placeholders remain, never the tokens. There
   is no override: a text with a detected form is edited and uploaded again.

4. **The existing memo sign-off and `decision` routes are unchanged.** The plain `decision` route still refuses a
   client contract without its LG-02 record. The founder path is a separate, explicit route.
5. **CQ-19 founder determination**: `POST /legal/v1/register/CQ-19/founder-determination`, Andre's token only.
   - **Body:** `rationale` (20-2000 characters) and `founder_confirms: true`. Any other CQ row is refused with
     `FOUNDER_PATH_NOT_ALLOWED`.
   - **The row itself:** its own `status` and memo list are untouched. It gains `founder_determination`, holding
     `status`, `determined_at`, `renew_by` (today + `LEGAL_MEMO_REVIEW_DAYS`), `rationale_sha256` and
     `scope_doc_ids`.
   - **Ledger event:** `register_row_founder_determined`.
   - **Effect:** while the determination is `active` (LG-20 in force, `renew_by` not passed, not invalidated), a
     `clickwrap_unticked_box` acceptance of a `founder_approval_allowed` document that passes every LG-03 check is
     `evidence_sufficient: true` with `evidence_basis: "founder_determination"`.
   - **Precedence:** a filed memo verifying CQ-19 takes precedence (`evidence_basis: "memo"`).
   - **Invalidation:** a Compliance invalidation of CQ-19 also ends the determination (tighten only).
   - **Scope:** the determination covers only the documents it recorded in `scope_doc_ids` that are still flagged
     (AEGIS L3). A later seed that flags another document does not extend it.
   - **Closing stops what was already produced** (AEGIS M2). Legal reports `basis_in_force` on the version and on
     the acceptance views:
     - A founder-approved version is out of force (no longer `current`, no new acceptances) while LG-20 is not
       verified.
     - An acceptance resting on the determination stops counting (`evidence_sufficient: false`,
       `basis_in_force: false`) once CQ-19 is invalidated or LG-20 is not in force.
     - The determination's renewal date passing does not cut off acceptances it already covered.
     - **An invalidation is final for what it covered** (AEGIS r2 N5). Each determination carries a `lineage`, which
       is recorded on every acceptance it makes sufficient (`evidence_lineage`). Renewing a determination that is
       active or past its renewal date keeps the lineage, so its acceptances still count. A determination made after
       an invalidation starts a new lineage: acceptances recorded before the invalidation stay out of force, and the
       client accepts again.
     - The stored acceptance record itself never changes.
   - **Not passed on:** `evidence_basis` stays in Legal's acceptance record and is not passed to Finance.
6. **Unchanged acceptance checks**:
   - the exact `doc_sha256` of a version that is approved and in force;
   - `presented_sha256 == doc_sha256`;
   - `affirmative_act: true`;
   - a signer identity ref;
   - the party binding of a filled instance;
   - who may record an acceptance (`hub`, `clipper_network`, `onboarding`). **Amended by ADR 0028 (2026-10-31):** a
     client's acceptance is recorded only by the client acceptance page (caller `client_portal`, from a single-use
     link Andre creates); no caller records one on `POST /legal/v1/acceptances`.

   An acceptance record never changes after the fact. One recorded before the determination stays insufficient, and
   the client accepts again.
7. **Finance** carries no approval or evidence basis. It refuses (`LEGAL_NOT_CURRENT`) when Legal's version or
   acceptance view says `basis_in_force` is anything but `true`; a view without the field (a Legal from before this
   ADR) counts as `true`. The check runs when Andre issues the invoice, not only when it is drafted. A Legal it
   cannot read is still refused (`DEPENDENCY_UNAVAILABLE:legal_37`).
   - **`LegalAnswer`:** gains the renewal reminder, `renew_by` (the version's `review_by`) and `renewal_lapsed`, and
     `version_basis_in_force` / `acceptance_basis_in_force` (AEGIS r2 N6).
   - **A basis out of force** is refused with plain wording, never as a hash mismatch (AEGIS r2 N6):
     - for the version: "the MSA's approval basis is not in force at Legal: the founder approval path (LG-20) is
       closed; Andre reviews it at Legal";
     - for the acceptance: "the MSA acceptance's evidence basis is not in force at Legal: the CQ-19 founder
       determination it was recorded under has ended or LG-20 is closed; the client accepts again".
   - **`GET /fin/v1/clients/{id}/billing-readiness`:** returns `contract_renewal.MSA` with `renew_by`,
     `renewal_due_soon` (14 days or less) and `renewal_lapsed`.
   - **A lapsed date** is refused with plain wording: "the MSA version's renewal date passed on <date>: Andre renews it
     at Legal". It is not the generic "not current" message, so expiry is never silent.
   - **The issued invoice record** is unchanged.
8. **IP addresses** stay refused anywhere in an acceptance (or any write) body, by design (LG-03, G6). Legal records
   who (`party_ref`, `signer_identity_ref`) and when (Legal's clock), never an IP address.

## What is left unchanged

- **Other documents** keep their existing gates:
  - `clipper_agreement`;
  - `ic_agreement` (also blocked by CQ-17);
  - `nda_mutual` and `nda_oneway`;
  - `privacy_policy`, `terms` and `cookie_notice`;
  - `dmca_procedure`;
  - `tm_zbm` and `tm_zbc`;
  - `engagement_letter` (countersigned copy + ENG-AI-01);
  - `dpa`;
  - `lit_hold_notice`, `subpoena_response` and `dsar_response`;
  - `not_legal_advice_v1` (also blocked by CQ-15).

  None of them is needed to invoice an SEO audit or Revenue Recovery client.
- **CQ rows:** every row other than CQ-19, and CQ-19 for every other document. For example, clipper clickwrap stays
  insufficient, and the clipper ESIGN 7001(c) consent block is still required.
- **Legal's rules and machinery:**
  - the text and locks of LG-01, LG-02, LG-07, LG-17 and LG-18;
  - memo intake;
  - playbooks (LG-05) and obligations (LG-06);
  - e-sign (not built);
  - the advice-text guard;
  - the protocol answer for Compliance and Clipper Network.
- **Finance's own gates.** In particular, **FIN-CQ-11** (sales tax) still blocks every invoice until Andre amends it,
  a separate decision outside this ADR.

## Responsibility

Andre approves these contract versions and the CQ-19 determination himself, and holds the rationale for each. The
code checks that a rationale exists (at least 20 characters), not what it says.

## Seeds and pins

`devtools/gen_seeds.py` regenerates both changed files byte for byte: LG-20 is added, and documents gain
`founder_approval_allowed` and the two SOW ids. `--pin` rewrote `src/config.py`, and ADR 0010's table is updated. The
other five seed files are unchanged.

| File | Old SHA-256 | New SHA-256 |
|---|---|---|
| `legal_rules_seed.json` | `cebb5b1418eee245a472d040bfd8795ce3d93338902c4939b0454655f90afe9b` | `140a118216b8c0d1a3baee93daa261affe3058733cb9f97fe1f8c0bf99735ff4` |
| `documents.json` | `c4819ab8017e4d563a776b0e57964ff821b69f000ec5189581be858fb933f823` | `2073717e838b2ddec39027d8c4b68ed91ebdc1df683621faef72dc199939afbe` |

`documents.json` is read at every start, so the new fields reach a running Legal on redeploy. The rules seed reaches
only a Legal with no rule version approved yet. A Legal already running on an approved version adds LG-20 through
the rules process (the runbook in `docs/deploy/railway.md`, "Founder contract approval (ADR 0022)").

## Tests

- `services/legal-py/tests/test_adr0022_founder_contract_path.py`.
- The ADR 0022 block of `services/finance-py/tests/test_legal_client.py`.

Both fail on 8d20e83 and pass here.
