# ADR 0031: Invoice issuance date for calendar terms

Status: accepted for implementation; independent review and remote CI required.

Invoice issuance calculates `due_at` from the Los Angeles date of the captured `issued_at` instant plus the
invoice's existing `due_days` integer. It uses the existing `service.LA` timezone constant, also used by Finance's
`_today_la` journal-date helper. No new timezone setting or holiday calendar is introduced. Using the captured
instant avoids a second clock read for the due-date calculation. `issued_at` stays a UTC timestamp.

This corrects the evening boundary where UTC is already the following day but Los Angeles is not. DST changes
use the existing IANA America/Los_Angeles zone. `due_days` remains a count of calendar days, including weekends
and holidays; it is not the business-day calendar used by refund, acknowledgement or other contract deadlines.
Those deadlines and their scheduling are outside this change.

Only new approvals of draft invoices calculate a due date. Existing persisted invoices and recorded decision
responses retain their dates without migration or recalculation. Idempotent replay within the existing request
window returns the recorded result. A request outside that window or a new approval of an already issued
invoice remains refused. This change does not reissue or edit invoices, alter amounts, grant approval, bypass
accepted-SOW/Compliance checks or change payment terms.

Operator note: display `due_at` as its recorded date, not as a UTC timestamp to convert again. Existing issued
invoices retain the dates communicated to clients. Any correction of an existing invoice needs its own
independently authorized process; this release performs none. The setting is fixed Los Angeles, not the client's
local timezone. Do not describe this change as implementing business-day contract deadlines.

Boundary tests cover LA midnight, the UTC year boundary, DST spring/fall transitions including both repeated
hours, due_days0/1/120, weekend inclusion, journal date agreement under a fixed clock, and durable replay after
restart/clock advancement. Local tests do not certify production configuration or legal deadline compliance.
