# ADR0041 — Copies-only offline structural inventory

2026-11-01. Standalone preparation tool, subject to independent review/hostedCI. No recovery or deployment authority.

## Boundary

`services/finance-py/devtools/inventory_copies.py` reads explicitly supplied owned copies and writes one new private
report file under an explicitly supplied existing private parent. The pure `recovery_inventory.py` imports only stdlib and store.verify_lines/StoreCorrupt. No Service, API,
RecordLog/DataDirLock construction, replay, ledger client, provider, configuration, environment or live-data lookup.
No --apply, repair, balance calculation, movement association/deduplication, receipt compatibility or launch permission.
Optional ledger exports are deferred until a complete exact representation contract exists. All current rollback,
provider activation, acknowledgement intake, credit policy and actual recovery prerequisites remain separate.

## Inputs and bounded parsing

Required --copies-dir is an absolute directory containing exactly finance-log.copy.jsonl and provenance.json.
Endpoints must be current-user-owned, directory0700/files0600, regular single-link files. All path components are
opened through directory descriptors without following symlinks; dot/dotdot/empty ambiguity is refused. Intermediate
ancestors may be root/current-user-owned; writable intermediate directories require root ownership and sticky bit.
Strict private owner/mode applies to final input directory and report parent, not systemroot or /private/tmp.
This layout reduces accidental live-directory use; it cannot attest copy origin or absence of concurrent writers.

Retain directory/log/manifest descriptors through evaluation. Bound file reads and check all endpoint versions,
directory metadata and directory-entry identity after both reads, immediately before and after evaluation. Detected
change refuses evaluation/publication. Matching stat observations do not prove atomic acquisition, authenticity,
completeness or protection against privileged modification. No writer claim is acquired. Finite regular files do not
imply a forced wall-clock deadline for stalled filesystems. Inputs are never chmod'd, rewritten, normalized or repaired.

Limits: log16MiB, manifest8KiB, line256KiB,20,000records, JSONdepth32, cumulative250,000nodes, numbertoken64characters.
Record count is checked before splitting the bounded file into lines. Nested duplicate keys, nonfinite values and
invalid encodings are refused before the reused decoder. Numeric prevalidation uses Decimal, not binary conversion or
money arithmetic; fractional/exponent token magnitude above1e308 is explicitly unsupported, a conservative finite-JSON
capability bound. This may refuse otherwise finite larger numbers; do not truncate/alter evidence to fit. A bounded
per-line parse plus whole-chain helper reparse is deliberately bounded in-memory duplication, not streaming.

Nonempty files require final LF; split only b'\n', reject blank/torn lines, never strip/CRLF-normalize/reencode source.
Record hash excludes record_sha256; previous/tail line hash covers exact canonical line WITHOUT LF; whole-file hash
includes every LF. Strict outer record fields: seq,kind,at,data,prev_line_sha256,record_sha256. Sequence is literalint,
not bool; timestamps aware; data.ops must exist and be list-of-tag/object pairs. Known outer/inner tags are inventoried
separately; unknown labels go only to fixed UNKNOWN buckets. No application payload schema or reader is certified.
On parse/cap failure inventory counts are NOT_EVALUATED/null and counts_complete:false, never a misleading prefix.
Unknown tags still count the complete bounded parsed input and mark capability unsupported. Diagnostics cap32 does
not stop validation/counting; total and complete distinguish diagnostic truncation from inventory completeness.

## Manifest/report semantics

Strict manifest schema finance-copy-provenance-v1: declared_source_sha (40hex/null), declared_image_digest
(sha256:64hex/null), backup_reference_sha256, collected_at, cutoff_at (aware/null), acquisition (unknown,
stopped_writer_declared,concurrent_writer_declared), expected_log_bytes,expected_log_sha256,declared_last_seq and
 declared_tail_sha256. Seq/tail must be supplied together or bothnull. Tail is the last canonical line's SHA256 WITHOUT
LF, not record hash or file hash. No JSON-selected paths. No arbitrary manifest strings in report. These are
DECLARED_UNVERIFIED provenance, never external attestation; matching declared hashes does not establish completeness.

Deterministic report schema finance-offline-inventory-v1 binds observed file hashes/sizes, observed fixed core/CLI/store
file hashes and Pythonmajor/minor, bounded structural/tag counts and fixed diagnostics. Observed evaluator file hashes
are not an attestation of executed/imported bytes: replacement between import/hash is not excluded. No caller-selected
identity path or source/image lookup. No names, contacts, bodies, amounts, raw reasons, provider identifiers or paths.
No currenttime/random fields in report; same bytes/declarations/evaluator identity produce same report bytes.

Chain VALID can coexist with declaration MISMATCH. Any bytehash/size or supplied valid-chain seq/tail mismatch yields
INCOMPLETE/exit3, preserving valid chain status. Missing optional tail declaration is NOT_PROVIDED, not fabricated match.
Precedence: operational/publication failure4; invalid format/unsupported vocabulary2; mismatch/empty evidence3;
only supported nonempty structural inventory with matching evaluated declarations0. Exit0 means artifact preparation,
never readiness. Acquisition consistency/authenticity/absent-tail completeness always UNKNOWN; ledger/bank NOT_PROVIDED;
application replay NOT_EVALUATED; restore_authorized/deployment_authorized always false. Rehashed rewrites and absent
tails cannot be authenticated by local hashes alone. No green readiness or safe-to-lift label exists.

## Output durability and failure

Required --report-file is an absolute NEW file under an explicitly supplied existing current-user-owned0700 parent,
descriptor-verified and outside input directory ancestry. No destination directory is created, adopted, chmodded or
removed. The previous unpublished --report-dir interface is refused. The tool does not provision or establish durability
of the preexisting parent name; that setup remains external. Existing file/symlink/directory destinations are refused.

O_CREAT|O_EXCL|O_NOFOLLOW returns the descriptor for the file this syscall created. Retain that descriptor and the
verified parent/ancestor identities; fchmod only the known-created file to0600, write-all, fsync file then containing
parent, recheck parent identity/privacy and named-file identity/metadata before success. This eliminates the initial
mkdir-to-open directory ownership ambiguity; no post-mkdir inode guess is made. It is protected exclusive final-file
construction, NOT atomic visibility: private partial bytes may exist before completion. Same-owner interference is
not assumed impossible; replacement of the parent/name prevents success, and only the opened created inode is written.

Only success reports report_written:true. Report cap64KiB. No fallback path. On failure unlink only when the entry
under the retained parent still binds the created inode, then fsync parent. Never unlink a replacement or remove any
directory; no recursive cleanup. Failed durability/cleanup stays OUTPUT_UNKNOWN/residual_possible:true/exit4 even if
best-effort cleanup appears complete. No raw exception/argument/path output. Stdout contains only fixed status,
report_written, report_sha256/null, fixed reason/null and residual_possible. Unavailable inputs never acquire fabricated
zero-byte hashes. Input acquisition failure may prevent a report; the fixed small stdout failure remains honest.

## Validation

Owned fixture controls cover genuine journal byte inventory without replay, hash framing, matching/mismatched metadata,
strict nested JSON and caps, unknown tags/full counts, post-second-read and evaluation-boundary changes, path/descriptor
refusals, original directory-replacement reproduction, created-file/parent replacement, output collision/shortwrites/fsync/cleanup failures, exact modes, determinism/privacy and import/network/constructor
prohibition. This is not a recovery drill or closure of historical restore findings. Independent exact-head AEGIS,
separate local acceptance and existing affected/shared gates remain required before publication.
