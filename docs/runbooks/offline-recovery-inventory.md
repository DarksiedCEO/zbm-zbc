# Offline copied Finance evidence inventory

This tool prepares a private structural report only. It cannot restore, replay, reconcile, repair, lift lockdown,
authorize rollback/deployment, contact a provider or establish bank truth. No production exports are acquired by it.
Use only separately authorized owned copies, never the live Finance directory. Do not alter evidence to fit its bounds.

Create an owned0700 copies directory with exactly two owned0600 regular single-link files: finance-log.copy.jsonl and
provenance.json. Preserve log bytes exactly, including corruption. Do not strip lines or add missing LF. No symlinks,
FIFOs/devices/hardlinks or ambiguous path components. Supply an absolute NEW report-file path under an existing
owned0700 parent outside the copies directory. No output directory is created or chmodded. Existing file/symlink/
directory destinations are refused, never overwritten.

Run `python -B services/finance-py/devtools/inventory_copies.py --copies-dir ABS_COPIES --report-file ABS_NEW_REPORT_FILE`.
No credentials, environment settings or live service URL are accepted. ADR0041 specifies exact manifest fields/bounds.
Use actual separately recorded provenance; source/image IDs and stopped-writer acquisition claims remain declarations.
`declared_tail_sha256` means SHA256 of last canonical line WITHOUT its LF; file hash includes all LF bytes. Seq/tail may
both be null when not declared; do not substitute a fabricated trusted checkpoint.

Inspect the explicitly named report file and exit together.0=structural report evaluated,2=invalid/unsupported input,3=incomplete/empty or
mismatched declared evidence,4=operational/publication failure. Exit0 is not recovery approval. Chain VALID does not
prove authenticity or missing-tail completeness. Declaration MISMATCH can coexist with chain VALID. Counts_complete
is distinct from diagnostics.complete; capped diagnostics do not imply only that many problems or complete source schema.
Unknown outer/op tags stay visible in aggregate, never interpreted as financial actions. Payload/receipt replay is
NOT_EVALUATED. Ledger/bank truth remains NOT_PROVIDED even if a manifest claims a stopped writer.

File and containing-parent fsync are required before report_written:true. The already-existing parent name was not
created or durably provisioned by this tool; its setup is external. Construction is exclusive but not atomic visibility;
private incomplete bytes can exist during a failed run. OUTPUT_UNKNOWN/residual_possible requires operator inspection
of the specifically owned destination; do not recursively delete unrelated files or assume publication succeeded.
The previous unpublished --report-dir option is refused. No output/input path or raw error detail is echoed. Copy/path guards cannot prove origin or exclude all concurrent writes.
The finite regular-file limits do not enforce a hard deadline for a stalled filesystem.

Before any later recovery, separately resolve authorized acquisition/source/cutoff/account scope and retention;
independent completeness/checkpoint/bank evidence; actual target image/reader compatibility and old-image rollback gate;
traffic freeze/single-writer ownership; RPO/RTO and isolated destination; explicit founder restore/resume authority.
Do not use this report as --apply instructions or substitute unavailable balances with zero. Provider activation,
receipt/acknowledgement policy and credit decisions are outside this inventory and remain open.

### SEO privacy-key continuity

Preserve the original `SEO_LOG_HASH_KEY_FILE` alongside the encrypted recovery
configuration. A syntactically valid replacement is not equivalent to the original
key: unfinished log ingests refuse new chunks with `LOG_HASH_KEY_MISMATCH`.
Restore the original key and retry the same uncommitted sequence; do not alter
the stored fingerprint or bypass the check. Existing reads and already-computed
aggregates remain available. Starting a new ingest with an intentionally rotated
key is separate from continuing a restored ingest. This key pseudonymizes client
IP addresses; it does not sign the ledger or SEO journal.
