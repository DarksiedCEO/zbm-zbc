# Receipt delivery preparation (mock-only)

No real mail is sent by this version. `NotBuiltClientMail` remains the production default. Do not treat a queued receipt as delivered.

1. Founder authentication is required to PUT a client's billing contact. Use one bounded ASCII mailbox; `email: null` disables it. The API returns enabled/revision only. Protect the private Finance journal and backups as recipient PII.
2. The existing scheduler-only receipt send route is unchanged. A missing contact or disabled adapter does not consume a claim. No credentials or configuration in this increment enable delivery.
3. Future adapters must consume the saved envelope unchanged and support its key throughout the entire retry interval. Before connecting one, independently validate acceptance versus retry semantics, ambiguous timeouts, provider deduplication, safe network behavior and deployment credentials. The 23-hour local bound is not provider certification.
4. After a first attempt, never edit an envelope, reset attempts/deadline, or generate another key. Changed contact, clock rollback, seven claims or expiry require founder review. Previously attempted receipts without an envelope cannot be safely retried by this increment.
5. For `delivery_uncertain` or `delivery_failed`, investigate externally and record an evidence ref/hash at the founder-only `delivery-review` route. This terminal acknowledgement records operator handling only. It neither marks delivery nor enables a resend. If a manual external action is necessary, manage duplicate risk outside this automated path; no automatic recovery from uncertainty is provided.
6. Check the private journal reader compatibility before any deployment rollback. This change adds collections; it does not implement restore or backward schema migration. Do not replace or edit durable state to bypass a refusal.

Receipt GET/send responses and ledger audit payloads omit recipient/body. Delivery status does not affect or book money. A withdrawn receipt cannot start delivery; an already accepted in-flight message may require a separate manual client correction.
