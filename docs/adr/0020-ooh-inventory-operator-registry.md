# ADR 0020 — Out-of-Home & DOOH (15): the inventory and operator registry (OOH-2)

Status: accepted for build, Oct 16 2026, from the founder's round 2 decisions of Oct 16 2026 (as given in the build
brief) and ADR 0019. Built on branch `ooh-dept-15-wave2`. **Not in force.** No agent is built; nothing here buys
media or calls a vendor.

**Source caveat.** The master blueprint, the hardening directive and the founder's round 2 decision note
(`claude/ooh-decisions-2026-10-16.md`) were not available to this build as files. The scope below is the build
brief's: the inventory/operator system, the geographic registry, the supplier schema, and avails/holds. What goes
beyond the brief is marked and listed under open questions.

## Context

OOH-1 kept inventory as a campaign's free-text unit (`vendor`, `kind`, `market`, `vendor_unit_ref`). OOH-2 makes
inventory ZBM's own registry: one canonical object per physical face, for every format, in a named market, with every
fact tied to the source that states it.

## Decisions

1. **One registry, global** (`src/svc_inventory.py`). Markets, sources and faces are ZBM's knowledge of the market,
   not a client's data, so they are not per tenant. The client hub never reads them (403): a face carries vendor
   cost (`rate_card_amount`), and the hub never shows vendor cost (locked).
2. **Nothing is invented.** A market's four flags start `NOT_MET`; a face attribute or supplier attribute nobody has
   sourced is `{"status": "UNKNOWN", "value": null}`; there are no defaults. Impressions, audience and measurement are
   not attributes: no measurement provider is chosen (a later port).
3. **Geography** (`policy.MARKETS`): `los-angeles` (Los Angeles County), `orange-county`, `inland-empire` (Riverside
   and San Bernardino Counties), `san-diego`, `ventura`, `santa-barbara`, `imperial-az-border` (Imperial County, CA,
   and the Arizona border area, whose Arizona extent is recorded as UNKNOWN until researched). A campaign names only
   registry markets (`MARKET_UNKNOWN`), each once.
4. **Four independent readiness flags per market** (`policy.READINESS_FLAGS`): `RESEARCH_COVERAGE`,
   `PLANNING_COVERAGE`, `EXECUTION_AUTHORIZED`, `OPERATING_MATURITY`, each `NOT_MET | MET` with its evidence
   reference, who set it, when, and its history. No single "active" flag exists and none is derived from another.
   - `POST /ooh/v1/markets/{mid}/readiness`: raising to `MET` is Andre's (`ANDRE_APPROVAL_REQUIRED` otherwise, and the
     `capability:registry` kill switch stops it); lowering to `NOT_MET` is the safe direction, open to the dashboard,
     the agent runtime and Compliance (38), and never stopped by a kill switch. Ledger events
     `market_readiness_raised_by_andre` / `market_readiness_lowered`.
   - What they gate (`policy.READINESS_GATES`; open question 1): `PLANNING_COVERAGE` — a campaign reaches
     `PLAN_APPROVED` only when every market it names is `MET` (guard `MARKET_PLANNING_NOT_COVERED`);
     `EXECUTION_AUTHORIZED` — the purchase gate refuses, at authorization and again at submission, any unit whose
     market is not `MET` (`MARKET_EXECUTION_NOT_AUTHORIZED`). `RESEARCH_COVERAGE` and `OPERATING_MATURITY` gate
     nothing yet; they are recorded for later waves.
5. **Named sources** (`POST /ooh/v1/sources`, preparers): `VENDOR_QUOTE` or `RATE_CARD` (the founder's "vendor quotes
   and rate cards now"), of a registered vendor, with the document reference and the date the source states
   (`dated`, never in the future: `SOURCE_DATED_IN_FUTURE`). Class `VENDOR_ASSERTED`. Optional vendor text is
   untrusted data (ADR 0019 decision 19; only its hash reaches the ledger). `VENDOR_API` and `PLATFORM_FEED` are
   named and refused `SOURCE_NOT_CONNECTED`.
6. **The canonical face** (`POST /ooh/v1/faces`, preparers): one object for every format —
   `STATIC_BILLBOARD`, `DIGITAL_BILLBOARD`, `TRANSIT_STREET_FURNITURE`, `MOBILE_BILLBOARD` — with its identity
   (vendor, the vendor's face reference, format, market) taken from a named source and stamped with that source's
   id and date. One physical face is one record: `(vendor, normalised reference)` is unique (`FACE_EXISTS`; the
   ADR 0019 M1 normalisation, so `WEST-101`, `west_101` and `w-e-s-t-1-0-1` are one face). `GET /ooh/v1/faces`
   filters by market, format, vendor and (respelt) reference.
7. **Proof per format** (`policy.PROOF_REQUIREMENTS`, fixed by the format, never settable): static — `POSTING_PHOTO`;
   digital — `PLAY_LOGS`; transit / street furniture — `INSTALL_PROOF`; mobile — `ROUTE_GPS_PROOF`. Every face and
   every campaign unit carries it. Checking the proof itself is the proof-of-performance wave (C17).
8. **Sourced attributes** (`POST /ooh/v1/faces/{fid}/attributes`, preparers; `policy.FACE_ATTRIBUTES`): each
   attribute has a type and the formats it applies to (`ATTRIBUTE_NOT_FOR_FORMAT`: a mobile billboard has no fixed
   latitude; `ATTRIBUTE_UNKNOWN`; `ATTRIBUTE_VALUE_INVALID`). Every value names a source of the face's own vendor
   (`SOURCE_VENDOR_MISMATCH`) and takes that source's date as its `as_of`. The current value is the one from the most
   recent source (a tie goes to the later record); an older source recorded later is kept in the history,
   `current: false`, and never displaces it. Text attributes are vendor data (untrusted, hash-only on the ledger).
9. **Supplier schema** (`POST /ooh/v1/vendors/{vid}/attributes`, `GET /ooh/v1/vendors/{vid}`;
   `policy.SUPPLIER_ATTRIBUTES`): operator type, formats offered, markets served, payment methods, payment terms,
   cancellation policy reference, contact reference — sourced exactly like face attributes. Vendors are paid by ACH
   or check only (locked): `card` is refused `VENDOR_PAYMENT_METHOD_REFUSED`. Contacts are kept by reference: no
   names, phones or other personal data (the repo is public; the personal-data key refusal still applies). Each
   supplier shows `data_port: NOT_CONNECTED`.
10. **Campaign units are registry faces** (`POST …/inventory` now takes `face_id` and the flight): vendor, format,
    market, the vendor's reference, the unit key and the proof requirement come from the face. A face outside the
    campaign's markets is `MARKET_NOT_IN_CAMPAIGN`; the free-text body is gone (422). The OOH-1 uniqueness, flight
    and cross-tenant face rules are unchanged.
11. **Avails** (`POST /ooh/v1/faces/{fid}/avails`, preparers): the vendor's `AVAILABLE | UNAVAILABLE` answer for a
    flight, from a named source of that vendor, optionally valid until a time. A unit's move to `HOLD_REQUESTED` is
    refused `AVAILS_UNAVAILABLE` when the vendor's latest unexpired answer for an overlapping flight is
    `UNAVAILABLE`. Holds themselves are ADR 0019's: only Andre confirms one.
12. **The vendor-API / platform port** (`svc_inventory.NotConnectedInventoryPort`): an interface with no
    implementation. `GET /ooh/v1/inventory-port` reports `NOT_CONNECTED`; `POST /ooh/v1/inventory-port/sync` answers
    503 `INVENTORY_PORT_NOT_CONNECTED` and records nothing; `/status` lists `inventory_data`, `measurement` and
    `programmatic_dooh` as `NOT_CONNECTED`. `OOH_INVENTORY_PROVIDER` still refuses start.
13. **Record-first, replayable.** Every registry change commits through `_commit` and is applied by its `_a_*`
    handler; the face-key index is rebuilt by replay. Tested: restart gives an identical snapshot.
14. **Log format 3** (from the hold change, ADR 0019): the unit record shape changed again here; there is no
    migration (the service is unreleased).

## What OOH-2 does NOT do

- No face data. The registry starts empty; faces come only from sources Andre's team records.
- No vendor API, platform, measurement or programmatic DOOH connection (ports, NOT_CONNECTED).
- No pricing from rate cards: `rate_card_amount` is a sourced fact; planning and pricing are OOH-3.
- No proof checking (C17), no geometry beyond a point, no DOOH loop or share-of-voice model.
- No research of the markets themselves: every flag is `NOT_MET` until Andre raises it with evidence.

## Open questions for the founder

1. Which flag gates what. Built: `PLANNING_COVERAGE` gates `PLAN_APPROVED`; `EXECUTION_AUTHORIZED` gates
   authorization and submission; the other two gate nothing yet. Should `RESEARCH_COVERAGE` be required before
   faces are registered in a market, or `OPERATING_MATURITY` before any automation?
2. Whether raising `RESEARCH_COVERAGE` / `PLANNING_COVERAGE` should stay Andre's alone (built) or be open to the
   agent runtime with evidence.
3. The Arizona side of `imperial-az-border`: which Arizona counties or cities it covers.
4. Whether a digital face may carry more than one open order for different loop slots (built: no — the locked rule
   is one open order per face per overlapping flight).
