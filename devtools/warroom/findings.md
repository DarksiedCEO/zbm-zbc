# War room findings

MUST failures the war room found and that are triaged (ADR 0018). Each one is a permanent case in
`replay/<service>.json` marked with its finding id (`known_failure`), so it shows in every report without blocking
the gate. The war room does not change service code: each finding is for the department to fix; when the fix lands,
the replay cases pass, their mark is cleared, and they stay as regression cases. Replay any case with
`python3 devtools/warroom/run.py --replay <id>`; each entry also records the seeded case it came from (`origin`) and
its transform chain.

Found on branch `warroom-redteam-gate` (based on 2cedde8), seeds 1 and 2.

## WR-F001 — service-py: Greek / Latin lookalikes that the repo's shared table folds are not folded (opt-outs missed)

- Department: Customer Service & Success (14), `services/service-py/src/triage.py` `CONFUSABLES` (used by `clean()`,
  so by every opt-out check).
- What happens: an opt-out typed with a Greek eta (`η` for n), beta (`β` for b), zeta (`ζ` for z) or Latin script g
  (`ɡ`) is neither honoured nor surfaced: no consent changes and no alert reaches Andre. Example: `uηs­υ­bsc­riβe`
  by email, `ԁoηt τеxt or εмаil мe іf уоu cαn hεlp іт` in chat.
- Why it is a MUST failure: `triage.clean()` promises "confusables mapped to Latin" (ADR 0014 V1-H1), and the four
  letters are in the repo's shared lookalike table (creative-py `shared/text.py` `CONFUSABLES`, which onboarding-py
  and clipper-network-py copy) AND in sales-py's deliberately small reply table (`i10_replies._CONFUSABLE`). service-py
  folds 19 of the 23 letters both tables list; the war room's core homoglyph set is exactly those 23.
- Replay: `service-py/R0001`, `service-py/R0002`, `service-py/R0003`, `service-py/R0004`, `service-py/R0005` (seed 1)
  and `service-py/R0006`, `service-py/R0008`, `service-py/R0009`, `service-py/R0010`, `service-py/R0011`, `service-py/R0012`, `service-py/R0013`, `service-py/R0014`, `service-py/R0015` (seed 2)
- Suggested fix (for the department): fold `β`, `ɡ`, `η`, `ζ` (or adopt the shared table) in `triage.CONFUSABLES`.
- Status: FIXED (war room fixes, commit `0969219` on branch `warroom-redteam-gate`; ADR 0014 "War room
  fixes"): every opt-out rule reads through the shared lookalike fold (`triage.normalise_opt_out`); a capital that
  only its casefold makes a lookalike (R0010) is surfaced, never revoked on. Tests:
  `services/service-py/tests/test_warroom_fixes.py`.

## WR-F002 — clipper-network-py: a money word in leetspeak AND spelled out letter by letter is not caught

- Department: Clipper Network (32), `services/clipper-network-py/src/textguard.py` `money_or_earnings`.
- What happens: `g u 4 r 4 n 7 3 e d` (and `c 4 s h`) in a display name is accepted (201). Each disguise alone is
  caught (`g u a r a n t e e d`, `gu4r4nteed`, pinned by bug sweep C L4); together they are not, because the run of
  single letters that is joined back into a word is broken by the digits.
- Why it is a MUST failure: CN-26 / spec 0.1.8 (no earnings promise in client text) and the L4 fix promise that
  leetspeak and letter spacing are folded; a combination of two documented disguises is the same promise.
- Replay: `clipper-network-py/R0001` (seed 1)
  and `clipper-network-py/R0002`, `clipper-network-py/R0003` (seed 2: `g ú a r a n t 3 e d`, `c 4 5 h`)
- Status: FIXED (war room fixes, commit `774420a` on branch `warroom-redteam-gate`; ADR 0008 "War room fixes"):
  `textguard._collapse` keeps a lone leet character (`0 1 3 4 5 7 @ $`) in a run of single letters, joins a run of 3+
  only when it holds a real letter, and reads the joined run's leet as letters (spacing then leet); `fold_for_matching`
  uses the shared lookalike fold (`src/lookalikes.py`). Numbers alone (`2 0 2 4`) stay numbers. Tests:
  `services/clipper-network-py/tests/test_warroom_fixes.py`. The replay cases pass and stay as regression cases.

## WR-F003 — onboarding-py: a final sigma in a legal name splits the 1099 total

- Department: Onboarding (1), ZBC creator lane, `services/onboarding-py/src/name_key.py` `name_key_text`.
- What happens: `ᴊօsé ɡαrςía` for `José García` keys as `jose garoia`, not `jose garcia`, so a second creator id
  under that spelling starts a separate 1099 running total (600.00 instead of 2100.00; the 1099 is not triggered).
  Cause: `casefold()` runs before the lookalike fold and turns the final sigma `ς` (which the table maps to `c`) into
  `σ`, which the table maps to `o`.
- Why it is a MUST failure: name_key.py promises that two spellings of one person's legal name give one key, folding
  lookalikes "with the SAME table creative-py's shared/text.py uses"; that table maps `ς` to `c`.
- Replay: `onboarding-py/R0001` (seed 1)
  and `onboarding-py/R0002` (seed 2: `JoᏚé ʛaгϲíα` — the lunate sigma `ϲ` is NFKC'd to `ς`, then casefolded to `σ`)
- Note: creative-py's `canonical()` applies its table after casefold too; the war room has no creative-py library
  yet, so whether it shows the same split is not checked here.
- Status: FIXED (war room fixes, commit `ebb9918` on branch `warroom-redteam-gate`; ADR 0004 "War room fixes"):
  `name_key_text` uses the shared lookalike fold (the final sigma, and a word-final capital sigma, read as `c` before
  casefolding; the confusables skeleton under the unchanged table). Tests:
  `services/onboarding-py/tests/test_warroom_fixes.py` (pinned pk2- keys: no churn for already-normalised Latin / Cyrillic names). Correction (AEGIS round 2, M1): 266 code points key differently than at 2cedde8 (Arabic, Hebrew, Armenian, Coptic and others the skeleton now folds); an existing record keeps its stored key, a later signup by the same person can split the 1099 total, and the W-9 reconciliation is the backstop (ADR 0004 "War room fixes").

## WR-F004 — service-py: leetspeak digits typed full-width are not read as letters

- Department: Customer Service & Success (14), `services/service-py/src/channels.py` `opt_out_level` /
  `email_opt_out_decision`.
- What happens: `pleａsｅ ５７０ｐ tex７ｉｎｇ anｄ ｅm４1l1ng m３` (a full-width `５７０ｐ`) by email is neither honoured nor
  surfaced; the same text with ASCII digits (`570p`) revokes. The digit table (`_LEET`) is applied BEFORE the text
  is NFKC-normalised, so full-width digits never meet it.
- Why it is a MUST failure: ADR 0014 V2-H3 / Info promise "digits read as letters", and V1-H1 promises full-width
  forms are read; a combination of two documented disguises is the same promise.
- Replay: `service-py/R0007` (seed 2), and `service-py/R0016` (seed 3, same class: a full-width full stop did not end
  a clause, so "... stop texting． Call or email if needed" revoked email; it failed on the war room's base too).
- Status: FIXED (war room fixes, commit `0969219`; ADR 0014 "War room fixes"): the digit table runs after
  NFKC (`channels._leet`), and the opt-out rules cut clauses after NFKC (`channels._read`). Tests:
  `services/service-py/tests/test_warroom_fixes.py`.

## WR-F005 — verification-py: the minor lock does not fold a Greek eta (or zeta) in an email

- Department: Verification & Integrity (33), `services/verification-py/src/intelligences/i07_duplicate_identity.py`
  `_LOOKALIKE` (used by `mailbox_base`, the under-18 lock's identity).
- What happens: a minor locked as `kid.name@gmail.com` re-applies as `kiԁ.ηame@gmail.com` (Greek eta for n) under a
  new clipper id and is attested adult: the lock is stepped around.
- Why it is a MUST failure: `_LOOKALIKE` says it is creative-py's shared table, "the near-identical Cyrillic / Greek
  entries"; `η` (and `ζ`) are in that table and in sales-py's small one (the war room's core homoglyph set), and
  bug sweep C L3 promises the minor lock folds homoglyphs.
- Replay: `verification-py/R0001`, `verification-py/R0002` (seed 2).
- Severity note: this one is a child-safety control; it is the most urgent of the five.
- Status: FIXED (war room fixes, commit `a0f14ef` on branch `warroom-redteam-gate`; ADR 0007 "War room fixes"):
  `mailbox_base` folds with the shared lookalike fold (`src/lookalikes.py`), invisible characters included (the
  SHOULD observation below, treated as MUST); a minor recorded before the fix keeps every match it had through the
  frozen pre-fix fold (`email_base_v0`). Tests: `services/verification-py/tests/test_warroom_fixes.py`. The replay
  cases pass and stay as regression cases (`fixed`); the scenario no longer downgrades the wide set or invisible
  characters.

## WR-F006 — service-py: a word written wholly in Cyrillic was folded into a Latin opt-out word (consent revoked)

- Found by: AEGIS review of the war room fixes (2cedde8..ffd6a82, H1), not by a seeded case; a regression the WR-F001
  fix introduced (the base did not have it).
- Department: Customer Service & Success (14), `services/service-py/src/triage.py` `clean(..., opt_out=True)` with
  the shared fold `src/lookalikes.py` (Cyrillic `п` -> `n`), read by every opt-out rule in `channels.py`.
- What happens: `Пожалуйста, отправьте код по SMS` ("please send the code by SMS") revokes SMS consent: `по` ("by")
  reads as `no` beside `sms` (`suspected`, which pauses SMS). A bare `По` is `exact` and revokes email. The same for
  `Напишите мне по SMS…`, `Можно по телефону или по SMS?`, and Ukrainian, Bulgarian and Serbian equivalents.
- Why it is a MUST failure: an ordinary message changes no consent (ADR 0014; `consent_unchanged`).
- Status: FIXED (commit `47c10fc` on branch `warroom-redteam-gate`; ADR 0014 "War room fixes"): a word of two or more
  letters written wholly in ONE non-Latin script is read with service-py's own `CONFUSABLES` alone (as at 2cedde8)
  unless the shared layers make it an English opt-out word of four letters or more; mixed-script words and single
  letters fold in full. Everything the base caught is still caught; Russian `стоп` was not an opt-out at the base and
  is not one now. Tests: `services/service-py/tests/test_warroom_fixes.py` (`BENIGN_FOREIGN`). War room: MUST
  `consent_unchanged` scenarios `service-py/foreign-script-ordinary-sms` and `-email`, and Cyrillic, Greek, Hebrew
  and Arabic lines in `chaos.FOREIGN_LINES` (the `mixed_language` transform).

## WR-F007 — verification-py: a ban recorded before WR-F005 did not block a mailbox variant (banned clipper re-enters)

- Found by: AEGIS review of the war room fixes (2cedde8..ffd6a82, H2).
- Department: Verification & Integrity (33), `services/verification-py/src/service.py` `identity_check` (ban lookup).
- What happens: a ban recorded before the WR-F005 fix stored the old `email_base` HMAC. A new identity check carries
  that value as `email_base_v0`, and the ban lookup compared `(kind, hmac)` literally, so the banned clipper re-applies
  as `kidη+2@example.com` and the check is `clear`.
- Why it is a MUST failure: a ban propagates to every identity HMAC of the banned clipper (spec C.5; ADR 0007), and the
  WR-F005 fix promises a record made before it keeps every match it had.
- Status: FIXED (commit `2c688ee`; ADR 0007 "War room fixes"): banned HMACs are kept as their `_lock_key`, and every
  ban lookup compares lock keys. Tests: `services/verification-py/tests/test_warroom_fixes.py` (a pre-fix ban, a
  post-fix ban, a restart from the ledger, an unrelated address).

## AEGIS round 2 (review of the WR-F006 / WR-F007 fixes)

- **N-H1 (High) — service-py: the WR-F006 fold made long non-Latin messages slow under the service lock.** A
  20,000-character email of U+FDFA took 37.9 s end to end (base 7.6 s); ordinary 20k Arabic or Russian text about 2 s.
  FIXED (`b8b289b`; ADR 0014 "War room fixes"): each distinct text is cleaned and folded once per message (memoised
  readings), each distinct word once (regex split, per-word cache), and the per-character loops are regex deletions.
  Tests count the work instead of timing it.
- **N-M1 (Medium) — service-py: a multi-word opt-out phrase written wholly in another script was missed**
  (`ԁоп'т техт ме`, `по моге`, Lisu `ꓠꓳ ꓟꓳꓣꓰ`). FIXED (`b8b289b`): surfaced to Andre (`OPT_OUT_POSSIBLE`), never a
  consent change; benign `по SMS` texts raise nothing.
- **N-L1 (Low) — clipper-network-py: full-width `Ｍａｒｙ－Ｊａｎｅ Ｏ＇Ｎｅｉｌ` refused.** FIXED (`89c92bc`; ADR 0008): see the
  full-width display-name observation below.
- **M1 (Medium) — ADR 0004 claimed no key churn for names without a sigma.** Corrected (this commit; ADR 0004 and the
  WR-F003 entry above): 266 code points key differently; W-9 reconciliation is the backstop. Key code unchanged.

## SHOULD-level observations for Andre (scored, not gate-blocking)

These are outside what the departments' documents promise today, so the war room scores them and does not block on
them. Each may deserve a decision.

- **verification-py minor lock and invisible characters.** A minor's email with a zero-width space or a soft hyphen
  in it (`k­i­d.name@gmail.com`) is a different identity for the under-18 lock, so the new clipper id is attested
  adult. `mailbox_base` folds plus / dash tags, Gmail dots and lookalikes (bug sweep C) but not invisible characters.
  Example: `verification-py/minor-lookalike-email#000.1@1`.
  FIXED with WR-F005 (`a0f14ef`) (treated as MUST; the scenario's downgrade is removed).
- **service-py opt-out in the middle of a very long message.** A text over the inbound cap (20,000 characters for
  email) is cut to its head and its tail (`models.py`, `INBOUND_TAIL`); an opt-out in the middle is dropped. The
  documented promise covers an opt-out at the end only. Not changed (not local: the cut is the gateway model's); ADR 0014 "War room fixes" records it and a
  way to fix it.
- **service-py opt-out wording below an unmarked quote.** Below an Outlook "Original Message" / "From: Date:" block,
  "Dont call me or email me anymore" or "I want to be removed from your email list" raises nothing (ADR 0014 R2 says
  "any other opt-out wording raises OPT_OUT_IN_QUOTED_TEXT"; the code reads only multi-word listed phrases there).
  FIXED with WR-F001/WR-F004 (`0969219`): the sentence-shaped opt-outs alert there (never revoke).
- **service-py dotted spelling.** `N.e.v.e.r call or email me again` is not read (a dot is a clause end for the scope
  rules); spelling with spaces is. FIXED with WR-F001/WR-F004 (`0969219`): a word spelled out with dots, hyphens or underscores is joined
  before the clause split.
- **service-py imperative stop, bare STOP after a sign-off, leet wording below an unmarked quote** (seeds 1-3:
  `service-py/email-clear-opt-out#016.1@1`, `#022.2@2`, `#004.2@3`). Nothing was raised. FIXED (alert, never revoke;
  ADR 0014 "War room fixes"). Below an unmarked quote the war room still scores `email_revoked` as missed: R2 alerts
  there by design (the quoted words may be a third party's).
- **clipper-network-py full-width punctuation in a display name** (`clipper-network-py/ordinary-display-name#008.1@3`:
  `Ｍａｒｙ－Ｊａｎｅ Ｏ＇Ｎｅｉｌ`). Refused 422: the N16-11 character allowlist (`textguard.display_name_problem`)
  admits only `space . ' -` as punctuation and checks the raw text, so the full-width hyphen U+FF0D is refused. Fails
  closed; not changed. A decision for Andre: NFKC the name before the allowlist check (and store the NFKC form).
  The scenario now has MUST checks (`645e381`): every case is answered (201 or 422, never a server error) and the
  pinned names are accepted as written. FIXED (N-L1, `89c92bc`): a full-width `. ' -` counts as that mark and the
  web-address checks also read the NFKC form; full-width acceptance is MUST, case-flipped or re-accented stays SHOULD.
- **sales-py, onboarding-py** (not in this fix wave): sales-py misses some `homoglyph_wide` opt-outs (no suppression)
  and onboarding-py's legal-name key does not join a letter-spaced name (`1099-name-variant`, `letter_spacing`); both scored SHOULD by their
  libraries.
