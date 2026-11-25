# MVP shared fixture pools (test-set spec v0)

Source spec: project doc `claude/spec-2026-11-21-mvp-32-test-sets-v0.md`. That spec says the shared pools come first,
"on day 1 of the lanes, so every lane can start against them". This folder holds the two new pools and the
front-desk team's rule sets. **Status: v0.1 draft.** It freezes together with each agent's spec v2. Nothing here is
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
| SEO local: citation network | SEO-L4 | 200 businesses, each with an owner-confirmed record and 15–45 listings across 40 fictional directories. Listings are correct, correct with formatting-only differences (St./Street, suite styles, phone punctuation, a toll-free number the owner also lists, case, http/https/www), or carry one fault: wrong name, wrong address, wrong phone, or marked closed. 1 business in 10 is all-correct | A verdict per listing |
| SEO local: listing pairs | SEO-L5 | 200 pairs: true duplicates, moved businesses, and three NOT-duplicate traps (a practitioner in a practice, a department listing, two businesses at one address) | Duplicate or not, the action, owner approval for any removal |

## How the answers are proven
Each answer key is planted by the generator **and** re-derived independently from the subject data by
`poolgen/oracle.py` (pool B, call bank), `poolgen/frontdesk_oracle.py` (F2, F3, F4, F5) and `poolgen/seo_local_oracle.py`
(L4, L5, using its own address and phone normalisation). The oracles rebuild **every
field** of every answer row except the set's label (the call bank's `scenario`, F2/F4 `case`, F5 `episode`; pool-B
classes are re-derived too), and require the rows to match the subjects one to one.
- `tests/test_key_tamper.py` changes each field of the answers, and adds and drops rows, and requires the oracle to
  notice every time.
- Negative controls corrupt the subject data and require the same.
- Every threshold is generated exactly on its line and one past it (for example a 240-minute form reply is not slow and
  241 is; 20:59 may text and 21:00 waits; 300 s since the last text may text and 299 s may not).
  `tests/test_exam_integrity.py` checks that each line is present.
- **Known limit:** pool B's oracle reads the vertical packs' hours from the same table as the generator, so a wrong
  hours table would be wrong in both places.
- Nov 25 2026 sweeps with zero disagreements: 500 pool-B, 200 call-bank, 100 F2/F4 and 40 F5 seeds. Earlier sweeps caught and fixed:
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
   extends `fixtures/`), the SEO site corpus (≥ 400 generated sites; the local layer is here), the remaining GBP-like
   profile set for L1–L3, and the CI watch corpus.
6. **Not wired into CI:** these tests run with the command above. Adding a CI step needs a `.github/workflows` change,
   which builders don't make.
