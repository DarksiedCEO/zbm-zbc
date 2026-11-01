# ZBM receipt provider workflow — disabled by default

Do not activate from a green local test. ADR 0038 lists mandatory sender/account, credential custody, recipient,
rollback, reconciliation and explicit authorization gates. No provider credential value belongs in this runbook.

Settings: FIN_RECEIPT_MAIL_PROVIDER=disabled (default) or resend; FIN_RECEIPT_MAIL_PROFILE_ID,
FIN_RECEIPT_MAIL_PROFILE_REVISION, FIN_RECEIPT_MAIL_FROM, FIN_RECEIPT_RESEND_KEY_FILE and bounded
FIN_RECEIPT_MAIL_DEADLINE_S (default 10;1–30). Enabled configuration must be complete or startup refuses.
A local file must be regular, not symlinked, owned by the service user and inaccessible to group/others.
The optional container entrypoint can materialize FIN_RECEIPT_RESEND_KEY when launched as root, then drop user.
An explicitly supplied raw secret may be materialized even when app mode is disabled. IaC preserve() is source
preservation only; this task does not authorize plan/apply, provisioning or changes to live variables.

Use the existing scheduler-authorized POST /fin/v 1/client-receipts/{id}/send. The founder manages the contact;
this does not verify provider account/sender ownership. Only ZBM is enabled by this adapter. A new complete request
is committed before I/O. Already attempted or legacy saved envelopes require review, not enrichment or a new key.
Existing sent means API acceptance, not delivery. Private provider acceptance ID is stored only in the envelope;
do not publish it or copy recipient/body/key/credential into ledger, diagnostics or public artifacts.

For uncertainty, leave the original claim/key/window intact. Do not resend automatically, change profile to bypass
a refusal, reset attempts, edit journal, delete record or extend cutoff. Founder delivery-review records evidence
and does not authorize another send. A failed local append can require existing integrity/reconciliation handling;
503 outcome_unknown does not mean nothing happened. Obtain approved private operator evidence before any recovery.

Do not roll back to an unguarded older image. The current application checks history under its volume claim, but
cannot prevent an external GUI selecting an old image. External enforcement remains an activation blocker.
