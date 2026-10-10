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

## WR-F002 — clipper-network-py: a money word in leetspeak AND spelled out letter by letter is not caught

- Department: Clipper Network (32), `services/clipper-network-py/src/textguard.py` `money_or_earnings`.
- What happens: `g u 4 r 4 n 7 3 e d` (and `c 4 s h`) in a display name is accepted (201). Each disguise alone is
  caught (`g u a r a n t e e d`, `gu4r4nteed`, pinned by bug sweep C L4); together they are not, because the run of
  single letters that is joined back into a word is broken by the digits.
- Why it is a MUST failure: CN-26 / spec 0.1.8 (no earnings promise in client text) and the L4 fix promise that
  leetspeak and letter spacing are folded; a combination of two documented disguises is the same promise.
- Replay: `clipper-network-py/R0001` (seed 1)
  and `clipper-network-py/R0002`, `clipper-network-py/R0003` (seed 2: `g ú a r a n t 3 e d`, `c 4 5 h`)

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

## WR-F004 — service-py: leetspeak digits typed full-width are not read as letters

- Department: Customer Service & Success (14), `services/service-py/src/channels.py` `opt_out_level` /
  `email_opt_out_decision`.
- What happens: `pleａsｅ ５７０ｐ tex７ｉｎｇ anｄ ｅm４1l1ng m３` (a full-width `５７０ｐ`) by email is neither honoured nor
  surfaced; the same text with ASCII digits (`570p`) revokes. The digit table (`_LEET`) is applied BEFORE the text
  is NFKC-normalised, so full-width digits never meet it.
- Why it is a MUST failure: ADR 0014 V2-H3 / Info promise "digits read as letters", and V1-H1 promises full-width
  forms are read; a combination of two documented disguises is the same promise.
- Replay: `service-py/R0007` (seed 2).

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
- Status: FIXED (war room fixes, commit `WR-F005` on branch `warroom-redteam-gate`; ADR 0007 "War room fixes"):
  `mailbox_base` folds with the shared lookalike fold (`src/lookalikes.py`), invisible characters included (the
  SHOULD observation below, treated as MUST); a minor recorded before the fix keeps every match it had through the
  frozen pre-fix fold (`email_base_v0`). Tests: `services/verification-py/tests/test_warroom_fixes.py`. The replay
  cases pass and stay as regression cases (`fixed`); the scenario no longer downgrades the wide set or invisible
  characters.

## SHOULD-level observations for Andre (scored, not gate-blocking)

These are outside what the departments' documents promise today, so the war room scores them and does not block on
them. Each may deserve a decision.

- **verification-py minor lock and invisible characters.** A minor's email with a zero-width space or a soft hyphen
  in it (`k­i­d.name@gmail.com`) is a different identity for the under-18 lock, so the new clipper id is attested
  adult. `mailbox_base` folds plus / dash tags, Gmail dots and lookalikes (bug sweep C) but not invisible characters.
  Example: `verification-py/minor-lookalike-email#000.1@1`.
  FIXED with WR-F005 (treated as MUST; the scenario's downgrade is removed).
- **service-py opt-out in the middle of a very long message.** A text over the inbound cap (20,000 characters for
  email) is cut to its head and its tail (`models.py`, `INBOUND_TAIL`); an opt-out in the middle is dropped. The
  documented promise covers an opt-out at the end only.
- **service-py opt-out wording below an unmarked quote.** Below an Outlook "Original Message" / "From: Date:" block,
  "Dont call me or email me anymore" or "I want to be removed from your email list" raises nothing (ADR 0014 R2 says
  "any other opt-out wording raises OPT_OUT_IN_QUOTED_TEXT"; the code reads only multi-word listed phrases there).
- **service-py dotted spelling.** `N.e.v.e.r call or email me again` is not read (a dot is a clause end for the scope
  rules); spelling with spaces is.
