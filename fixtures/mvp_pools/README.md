# MVP shared fixture pools (test-set spec v0)

Source spec: project doc `claude/spec-2026-11-21-mvp-32-test-sets-v0.md`. That spec says the shared pools come first,
"on day 1 of the lanes, so every lane can start against them". This folder holds the two new pools and the
front-desk team's rule sets. **Status: v0.2 draft.** It freezes together with each agent's spec v2. Nothing here is
real data: every subject record is labelled fixture data (answer files hold only ids and answers), phone numbers are
only in the fictional 555-0100..0199 range, and web addresses use only `.test` or `example.com`.

| Set | For agents | What it is | Answer key |
|---|---|---|---|
| RR pool B | RR-S1..S6 | 120 service businesses with planted leaks and 20 clean controls, across 8 vertical packs: site and form (7 builders), mobile pages at 3 viewports, Business-Profile-like listing, reviews, phone line, DM inbox, consent on file, 30-day lead log | Per business: findings per detector, refusals (missing consent), the lost leads each leak caused, "not a fault" traps |
| Front-desk call bank, text track | RR-F1, F3 (F4 routing) | 504 scripted calls, 63 per vertical, 35% Spanish, 48 emergencies (medical, fire, violence, gas leak where the trade handles gas), prompt injection, health details, prices not in the pack, angry callers, silence, interruptions, self-corrected callback numbers | Route, facts to capture, must-say, must-never, health terms that must never reach the record |
| F3 summarizer transcripts | RR-F3 | 300 two-sided transcripts built on the call bank, with per-turn speech-recognition confidence: health details, the caller's own corrections, a garbled low-confidence number then a clean repeat, emergencies | Key facts (name, number, need, urgency), must flag the low-confidence turn, corrected value wins, health terms that must never reach the summary |
| F2 text-back | RR-F2 | 300 missed calls: landline, blocked caller ID, opted out, repeat within 5 minutes, quiet hours, boundaries | send now / defer to 08:00 / do not send |
| F4 transfer router | RR-F4 | 200 cases: connect, second person connects, nobody on call, nobody answers, caller hangs up mid-transfer, outside hours | Connected or a fallback, never a dropped caller, within 30 s |
| F5 consent keeper | RR-F5 | About 10,000 events: written consent, STOP variants, Spanish ALTO/BAJA, STOP typos, HELP, START (does not restore), new consent, reassigned number, carrier opt-out, and a STOP and a send in the same instant (file order shuffled) | allow / block and why |
| CI watch corpus | CI-1 | 30 fictional competitor pages × 10 versions (20 pricing, 10 changelog). Every version carries noise (footer date, tracking parameters, A/B headline, cookie banner, nonce, testimonial order); about 3 in 10 also carry one real change: price, new feature, removed feature, new plan, terms | The exact meaningful changes per version, silence on noise-only versions |
| SEO local: citation network | SEO-L4 | 200 businesses, each with an owner-confirmed record and 15–40 listings across 40 fictional directories. Listings are correct, correct with formatting-only differences (St./Street, suite styles, phone punctuation, a toll-free number the owner also lists, case, http/https/www), or carry one fault: wrong name, wrong address, wrong phone, or marked closed. 1 business in 10 is all-correct | A verdict per listing |
| SEO local: listing sync | SEO-L1 | 60 businesses × 5 platforms (GBP, Apple, Bing, Yelp, Facebook) against the client-approved record: in sync, drifted (edited by a platform, the public or an aggregator), hand-edited by the OWNER after the last sync, or the platform API failing mid-sync. Traps: the phone in national format (formatting only) and an owner edit made before the last sync (already reconciled) | Per platform: no action / update with approval / surface the conflict (never overwrite the owner) / report the platform error (never mark synced), and the fields involved |
| SEO local: review requests | SEO-L6 | 300 scenarios: 120 clean sends and 180 that must be refused: no real transaction, SMS without SMS consent, opted out, review gating (an audience filter), an incentive, suggested review text, "contact us instead of posting", and 20 with two violations at once. Traps: "feel free to tell us" is not an incentive; no SMS consent does not block an email | send / refuse and every reason (FTC 16 CFR Part 465; Google's ban on selectively asking happy customers) |
| SEO local: review replies | SEO-L7 | 300 reviews across the 8 verticals: positive, negative, mixed, refund requests, suspected fakes (a new one-review account inside a same-day pile-up of new accounts), and health or case details in med spa, dental and injury-law reviews. Traps: a genuine brand-new reviewer is not a fake; "no need for a refund" is not a refund request | Flag as suspected fake, route to the owner, owner approval before posting, must-nevers (never confirm a patient or client, never repeat the detail, never argue, never promise a refund) and the exact terms the reply must not repeat |
| SEO local: listing pairs | SEO-L5 | 200 pairs: true duplicates, moved businesses, and three NOT-duplicate traps (a dentist's own listing inside a dental practice, a department listing, two businesses at one address) | Duplicate or not, the action, owner approval for any removal |

## How the answers are proven
Each answer key is planted by the generator **and** re-derived independently from the subject data by
`poolgen/oracle.py` (pool B, call bank), `poolgen/frontdesk_oracle.py` (F2, F3, F4, F5), `poolgen/seo_local_oracle.py`
(L4, L5, using its own address, city/state/ZIP, website and phone normalisation), `poolgen/local_ops_oracle.py` (L1 by
diffing each listing against the record and reading the edit log; L6 from the scenario, with its own message patterns,
English only), `poolgen/review_replies_oracle.py` (L7, its own fake, refund and detail-term rules, English only) and `poolgen/ci_watch_oracle.py` (parses the page content and diffs versions). The oracles rebuild **every
field** of every answer row except the set's label (the call bank's `scenario`, F2/F4 `case`, F5 `episode`; pool-B
classes are re-derived too; F3's `need` is the caller's purpose, a label the oracle only bounds to the allowed
vocabulary), and require the rows to match the subjects one to one.
- `tests/test_key_tamper.py` changes each field of the answers (once per field and type; the call bank's caller name
  and health terms on every row), and adds and drops rows, and requires the oracle to notice every time.
- Negative controls corrupt the subject data and require the same.
- These thresholds are generated exactly on their line and one past it, and `tests/test_exam_integrity.py` checks each
  is present: F2 quiet hours (20:59 texts, 21:00 waits; 08:00 / 07:59) and the 300 s repeat window (300 / 299); pool B
  form reply 240 / 241 min, callback 120 / 121 min, DM 1440 / 1441 min, tap target 24 / 23 px, review recency 90 / 91
  days, review reply 7 / 8 days, reply rate exactly 30%.
- **Generated only on the line, not one past:** the burst (exactly 10 reviews in 3 days; 9 is never generated) and the
  F4 30 s ring timeout (30 is generated; 31 would be an invalid ringing plan, which the oracle rejects).
- **Generated on the line only:** the call bank's opening and closing minute (calls fall on :00, :15, :30, :45, so
  calls land exactly at opening and closing but never a minute either side).
- **Not generated at the line at all:** the 730-day review window and the 2.5-star recent average.
- **Known limit:** pool B's oracle reads the vertical packs' hours from the same table as the generator, so a wrong
  hours table would be wrong in both places.
- Nov 25 2026 sweeps with zero disagreements: 500 pool-B, 200 call-bank, 100 F2/F4 and 40 F5 seeds; after the round-2
  fixes, 120 seeds of pool B, L4, L5 and F4, 150 of the call bank and 200 of L1 and L6. Earlier sweeps caught and fixed:
  - four planting gaps (review trend, review reply rate, the DM 24-hour boundary, the form-reply 4-hour boundary);
  - a call-bank flag that marked a corrected callback on calls where no number is spoken.

## AEGIS review (Nov 25 2026, subject e41e57b5)
Test-verification and Inquisitor found that:
- the oracles checked only part of each answer;
- the F2 boundaries were never generated;
- **the held-out exam was not secret**: record ids embedded the seed, and answers followed from a record's position or id.

All three are fixed in the next commit. Ids are now opaque hashes, record order is shuffled per seed, and F5 episode
order is shuffled per block. `tests/test_exam_integrity.py` checks that a fresh seed shares no ids, no file hashes and
no seed text with the public dev key, and that answers lined up by position disagree. The Counterfeit attacker's
review was still pending at handoff.

## AEGIS review round 2 (Nov 25 2026, subject 95ba0730)
Test-verification and Inquisitor found, and the next commits fix:
- **The call-bank oracle only checked that the caller's name and the health terms appeared somewhere in the call**
  (336/336 name swaps and 24/24 health-list swaps went unnoticed). It now parses the one self-introduced name and
  requires the health list to equal the health terms actually spoken; a test tampers every row.
- **Pool B still leaked the answer through visible fields:** vertical and phone followed the loop index, which gave
  away control status (420/420) and the planted class (360/360). Role, vertical and region now come from independent
  per-seed shuffles and the phone from the business's own stream.
- **Review ids named the class** (`b00`, `t0`, `r-old`), and the oracle told burst reviews apart by that prefix. Ids are
  now random hex, and the oracle finds the burst from the data (ten or more reviews inside 3 days), with the generator
  keeping other reviews out of that stretch.
- **"Every threshold on its line and one past it" was false.** The claim above now lists exactly what is and isn't
  generated; the F4 30 s line is now generated.
- The L4 oracle ignored city, state, ZIP and website (200/200 moved listings unnoticed); it now checks them. L4 listings
  were capped at 40, not 45. The L5 practitioner sat inside a non-dental business in 106 of 120 pairs; it is now always
  a dental practice. L4 controls were tied to the city; they now come from a shuffled slot.
All the reviewers' probes were replayed on the fix and no longer reproduce.

## AEGIS review round 3 (Nov 25 2026, subject c0d1e59c)
Inquisitor broke 6 claims (2 high); fixed in the next commit:
- **F3 `need` was copied from the key**, never checked (900/900 rewrites unnoticed). It is now bounded to the reason
  categories and call purposes; a swap to another valid purpose is still not caught, so it is listed as a label.
- **SEO-L1 answers could be read off the last-sync age** (30 whenever an edit was planted: 1191/1200 rows right without
  reading a value). The sync age is now independent of the state, some drift is never logged, and some logged edits
  were already reverted. The same shortcut now gets about half the rows. (Round 4: the edit log alone still read
  about three quarters of rows; see below.)
- The call-bank minute claim is corrected (on the line only); the boundary test now checks the 7 / 8-day review reply
  and the exact 30% reply rate; the visible call-bank `difficulty` field (which equalled "adversarial") is removed.
- **Open for the spec freeze:** a silent F3 turn (confidence 0.0) is not flagged as low-confidence; the rule now says so
  and Andre confirms it.
test-verification round 3 (same subject) also found, and the next commit fixes:
- **F3 took the caller's name and number from the agent's own confirmation line** (a consistent wrong name or number
  in transcript and key passed: 196/196). The oracle now takes them from the caller's trusted turns (the last number
  given wins) and requires the confirmation line to agree.
- **The F3 low-confidence trap could not be fallen into:** the garbled turn never held a full number. It now holds a
  plausible wrong 10-digit number, so trusting it gives a wrong callback (dropping the confidence filter now fails 27
  rows).
- L1 now generates an owner edit exactly at the last sync (reconciled, not a conflict).
- L6 now has 20 requests with two violations, and the key lists every reason.
- Still not generated (mutants survive): a call with two names, a non-health call
  that mentions a health term. Six generator mutants are caught only by the dev-seed hash pin.

## AEGIS review round 4 (Nov 25 2026, subject dc6b77cc)
Inquisitor broke 4 claims (1 high); fixed in the next commit and its probes replayed:
- **SEO-L7 fakes were readable from the text** (three fixed sentences only fakes used; 300/300 on a fresh seed). Fakes
  and genuine angry reviews now share the same lines, so the text predicts nothing beyond the base rate.
- **The L7 fake rule was never exercised at its edges:** pile-ups of 2-4 and established accounts inside a pile-up were
  never generated, so four loosened rules agreed with the key. Both now are, plus an account exactly 7 days old and a
  pile-up of exactly 5; each loosened rule now disagrees with the key.
- **SEO-L1 "only the values tell" was false.** With owner edits that were set back to the record and owner edits exactly
  at the last sync on drifted listings, a predictor that reads only the edit log, sync age and API status now gets
  about three quarters of rows (1849/2400), down from 1961; the rest needs the values. Platform errors are still read
  from the API status by design. An owner edit exactly at the sync is now on every seed (31-50 rows on the dev seeds),
  and reading "at the sync" as "after it" now disagrees with the key on 204 rows.
- The held-out id test now covers L1, L6 and L7.

test-verification round 4 (same subject) confirmed the round-3 fixes and found the same L7 threshold gaps; also fixed:
- Every oracle now compares answers type-strictly: a key holding `1` for a yes/no field used to pass, since
  `True == 1` in Python. A test stores each yes/no answer as a number and requires it to be caught.
- The L7 fake rule's edges include a two-review new account and a pile-up of exactly 4; a test requires every
  loosened rule to disagree with the key.
- Still surviving: the call bank's single-name and health-terms-only-in-health-calls checks (no call has two names,
  and no non-health call says a health term).

## AEGIS Counterfeit (Nov 25 2026, subject e41e57b5; fixed Nov 26)
Counterfeit ran late against the round-1 commit. Its two HIGH findings (the held-out seed readable from ids, and ids
that gave the answer away) were already fixed by round 2. Its oracle-blindness finding was fixed by the type-strict,
fully re-derived oracles of rounds 2-4. The two remaining gaps are fixed here:
- **Fixture safety** (reworked after four AEGIS test-verification passes: b76b94f2, 2cb71287, 7dc6a8a1, 4d5c7b0c).
  Every rendered file must be JSON lines; each line is parsed, duplicate keys are refused, and every string, JSON
  number and dict key is checked decoded. A value may contain only printable ASCII and the Spanish letters and
  marks the call bank uses, so other-script and dingbat digits, look-alike dots and invisible characters fail as one
  class. A value that changes under percent-decoding or HTML-entity decoding is scanned again as decoded. Numbers:
  any run of digit groups joined by up to five non-alphanumeric characters; every 7/10/11-digit span that is a
  dialable North American number must be in 555-0100..0199; any 13-19 digit Luhn-valid span (a card shape) and any
  `+` number with a country code other than 1 fail. Hosts: every domain-shaped token, and every host after
  `scheme://` (localhost, IPv6 and integer IPs included), must be `.test` or `example.com`. IPv4 literals, email
  addresses and SSN shapes fail outright. Generated id fields are exempt from the number scan only and must match
  their field's exact generated shape. Every bypass the reviews found is a test case, and 43 seeds scan clean.
  Not detected, by construction (disclosed): numbers in words; digits joined by letters (`213x867x5309`) or by six
  or more separators; hosts written with spaces or `[.]`; an SSN with no separators; any 12-digit number; a card
  number failing the Luhn check; a bare IPv6 address such as `::1`; an international number without its `+`; and
  digits inside a correctly shaped generated id (hash output).
- **`build verify`** now fails when no dev seed is pinned, or when a seed's pins do not name exactly the generated
  files. It still proves the output has not drifted from the pins, not that the pins are right; the oracles do that.

## Use

```bash
cd fixtures/mvp_pools
python3 -B -m poolgen.build verify                 # regenerate the pinned DEV seeds, compare hashes
python3 -B -m poolgen.build stats --seed dev-a     # counts per class
python3 -B -m poolgen.build build --seed dev-a --out /some/empty/dir
python3 -B -m unittest discover -s tests -v
```

Standard library only. Generation is a pure function of the seed, through a SHA-256 counter stream (no `random`
module), so it is byte-identical on every Python version and platform.

## Seeds and the held-out exam
- `seeds/dev.json` pins the DEV seeds and the SHA-256 of every file they produce. Builders use them freely.
- **Held-out seeds are never committed** (the repository is public). At certification the reviewer lane draws one
  privately (`secrets.token_hex(16)`), records only its SHA-256 in the certification record, and runs this same
  generator. The commitment proves which exam was used without revealing it in advance.

## Open items (need Andre or the spec freeze)
1. **The 8 vertical packs are a proposed list:** med spa, dental, personal-injury law, HVAC, plumbing, auto-dealer
   service, restaurant, insurance agency. Wineries and clubs could swap in.
2. **Fixture prices and hours** in `poolgen/verticals.py` are test values, not ZBM's prices.
3. **Per-agent counts above the pool size** (for example S2's 200 call models) come from running several test calls
   per business, set at each agent's spec freeze.
4. **The rule choices to confirm at the freeze:**
   - STOP typos are treated as opt-outs (fail toward suppression);
   - START does not restore consent (same as service-py);
   - text-backs wait for quiet hours (08:00–21:00 recipient-local);
   - transfers ring every on-call person at once, so 30 s holds.
5. **Not built yet:** the synthetic-audio track (needs speech synthesis on the 32-core lane), RR pool A (e-commerce,
   extends `fixtures/`), the SEO site corpus (≥ 400 generated sites; the local layer is here), the L2 post and L3 Q&A
   sets, and the app-store and docs page shapes for the CI watch corpus.
6. **Not wired into CI:** these tests run with the command above. Adding a CI step needs a `.github/workflows` change,
   which builders don't make.
