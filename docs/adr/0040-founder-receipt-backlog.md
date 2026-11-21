# ADR 0040 — Founder receipt backlog as a bounded read projection

2026-11-01. Implementation proposal, subject to independent review and hosted CI; no deployment or provider activation.

## Decision

Add founder-only GET `/fin/v1/client-receipts/backlog` before the existing dynamic receipt-ID route. Existing service
bearer and Andre approval are required. Successful reads perform no journal, ledger, provider or contact operation.
Rejected founder authorization retains the existing fixed-route refusal audit; not every rejected read is write-free.
The read checks the service is open and captures bounded detached scalar projections under the existing money lock.
Sorting, fingerprinting and paging occur after releasing it. No sidecar, schema migration, index, configuration,
new credential or delivery capability is introduced. Existing GET-by-ID and send/review behavior are unchanged.

Scope is existing `client_receipts`, not orphan envelopes, complete external intake or a financial/history audit.
All statuses, including unknown and malformed records, remain visible. `paid_on` is a payment value date;
`created_at` is Finance receipt creation. Creation age is elapsed seconds, not business days or a contractual deadline.
The supplied MSA written-request acknowledgement provision is not interpreted as a receipt-send SLA here.

## Projection and privacy

Rows expose only canonical generated receipt/invoice IDs (26 Crockford characters), entity, closed status vocabulary,
bounded attempts, recorded dates/timestamps, operational age, acceptance-slot presence and review-field presence.
No client reference, address, amount, subject/body, contact, sender/profile, delivery key, provider ID or free-text
review evidence is inspected/copied/hashed for this view. Matching ID syntax is not action authority.

`acceptance_record` is `absent`, `present` or `unknown`; `acceptance_recorded_at` is Finance's recorded `accepted_at`,
not a provider-reported delivery time. Only the schema label, receipt binding and timestamp shape are inspected.
`acceptance_record_validated:false` and `delivery_verified:false` are unconditional: this is explicitly not full
payload/history validation, provider verification, account attestation, inbox delivery or acknowledgement. Unsupported
schemas/bindings or malformed slots/times are unknown. Legacy `sent_at` remains separately named `legacy_sent_at`
and is never promoted to a modern acceptance slot. Legitimate late acceptance may coexist with reviewed, uncertain
or withdrawn states. Manual review is evidence-field presence/type only; no review timestamp is invented.

Malformed rows return bounded placeholders and fixed issue codes. Malformed/oversized/nonstring collection keys are
never converted, repr'd, copied, hashed or sorted. Their `placeholder:<ordinal>` reference uses the bounded dictionary
iteration ordinal in a separate domain from valid-key digest references. This preserves one row per entry and unique
ordering within the captured snapshot; it is snapshot-local identity, not durable identity or an action ID. Reordering
malformed entries after replay may invalidate continuation. Valid key/record identity mismatch nulls both action IDs.
Only safe normalized projection fields contribute to the snapshot hash; it is not a whole-record integrity commitment.
An unrelated private body change deliberately does not invalidate this read cursor.

## Pagination and bounds

Limit defaults to50, strict decimal1..100. Omitted limit with a cursor adopts the cursor limit. Cursor is canonical
unpadded base64url JSON, at most256bytes, exact `v`, `snapshot`, `offset`, `limit` fields. Duplicate keys, nonfinite
numbers, bool/numeric coercions, noncanonical encodings and out-of-range values are refused without echo. Cursor
is not signed because it confers no authority: every page is founder-authenticated and offsets only select rows from
the same bounded inventory. Never use it as action authorization.

Check the5,000-record cap before collection traversal; over-cap inventories receive a fixed503, not a truncated
complete-looking subset. Strings are type/length checked before parsing (timestamp40, ID128), attempts at most2^31-1,
page at most100rows and serialized response at most256KiB. Malformed collection containers refuse503. These are
engineering limits requiring a separately reviewed index strategy for larger inventories, not limits on financial data.
No stored record is changed or discarded.

Unknown creation times sort first; known timestamps sort chronologically (including fractional seconds and UTC-offset
normalization), then opaque row reference. Snapshot hash excludes ALL clock-derived values and issues. Each response
uses one `as_of`; elapsed ages/future issues are added after hashing. A timestamp crossing from future to past does
not invalidate unchanged source pagination. Invalid/naive observation clocks yield unknown age and a fixed clock issue.
Future records remain visible, with unknown age; payment date is never a substitute. Source changes affecting the safe
projection invalidate continuation with409 and require restarting the first page. A change after capture does not
invalidate the already-consistent first page. Identical normalized projection after restart can resume. Empty inventory
has one empty page with no continuation. No stored generation or full-history authenticity is claimed.

## Validation and limits

Owned tests cover genuine paid-receipt creation, typed mocked acceptance and durable restart, privacy and authorized
zero writes, rejected-auth audit, lifecycle/unknown shapes, malformed keys, clock transitions/DST/fractions, pagination,
query/cursor bounds, capacity and a normal send racing after detached snapshot capture. Provider operations are mocked;
there is no actual outbound mail or delivery claim. Full Finance/live/shared gates and independent review remain
required for a concrete candidate. No restore, deadline policy, retry, automatic closure, cron or notification scope.
