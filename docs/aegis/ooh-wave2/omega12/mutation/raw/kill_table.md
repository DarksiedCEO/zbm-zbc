| id | mutant | 3.12 | 3.13 |
|---|---|---|---|
| M011 | store.py:L74 _pread_exact EOF guard not b -> False | TIMEOUT(hang) at tests/test_aegis_h1_edges.py::test_edges_pread_exact_stops_at_the_end_of_the_file | TIMEOUT(hang) at tests/test_aegis_h1_edges.py::test_edges_pread_exact_stops_at_the_end_of_the_file |
| M013 | store.py:L74 _pread_exact break -> pass | TIMEOUT(hang) at tests/test_aegis_h1_edges.py::test_edges_pread_exact_stops_at_the_end_of_the_file | TIMEOUT(hang) at tests/test_aegis_h1_edges.py::test_edges_pread_exact_stops_at_the_end_of_the_file |
| M044 | store.py:L228 create mode 0o600 -> 0o601 | killed by tests/test_aegis_h1_edges.py::test_edges_the_log_file_is_created_owner_only | killed by tests/test_aegis_h1_edges.py::test_edges_the_log_file_is_created_owner_only |
| M080 | store.py:L238 adopt tail uses raw os.pread | killed by tests/test_aegis_h1_edges.py::test_edges_adoption_completes_short_reads_of_the_tail | killed by tests/test_aegis_h1_edges.py::test_edges_adoption_completes_short_reads_of_the_tail |
| M091 | store.py:L240 if adopt: -> False | killed by tests/test_aegis_h1_edges.py::test_edges_adoption_fsyncs_the_file_and_writes_nothing_again | killed by tests/test_aegis_h1_edges.py::test_edges_adoption_fsyncs_the_file_and_writes_nothing_again |
| M092 | store.py:L241 adopt fsync deleted | killed by tests/test_aegis_h1_edges.py::test_edges_adoption_fsyncs_the_file_and_writes_nothing_again | killed by tests/test_aegis_h1_edges.py::test_edges_adoption_fsyncs_the_file_and_writes_nothing_again |
| M093 | store.py:L241 adopt fsync -> None | killed by tests/test_aegis_h1_edges.py::test_edges_adoption_fsyncs_the_file_and_writes_nothing_again | killed by tests/test_aegis_h1_edges.py::test_edges_adoption_fsyncs_the_file_and_writes_nothing_again |
| T222 | store.py:L326 clear_pending raise->pass | killed by tests/test_aegis_omega5_mutation.py::test_t222_unremovable_pending_after_certain_refusal_answers_maybe | killed by tests/test_aegis_omega5_mutation.py::test_t222_unremovable_pending_after_certain_refusal_answers_maybe |
| X008 | store.py:L74 _pread_exact EOF returns b'' | killed by tests/test_aegis_h1_edges.py::test_edges_pread_exact_stops_at_the_end_of_the_file | killed by tests/test_aegis_h1_edges.py::test_edges_pread_exact_stops_at_the_end_of_the_file |
| X011 | store.py:L238 adopt compare without newline | killed by tests/test_aegis_h1_edges.py::test_edges_adoption_refuses_this_line_without_its_newline | killed by tests/test_aegis_h1_edges.py::test_edges_adoption_refuses_this_line_without_its_newline |
| X016 | store.py:L237 append mem compare last byte dropped | killed by tests/test_aegis_h1_edges.py::test_edges_a_same_size_edit_of_the_files_first_or_last_byte_is_refused[last] | killed by tests/test_aegis_h1_edges.py::test_edges_a_same_size_edit_of_the_files_first_or_last_byte_is_refused[last] |
| X017 | store.py:L237 append mem compare first byte dropped | killed by tests/test_aegis_h1_edges.py::test_edges_a_same_size_edit_of_the_files_first_or_last_byte_is_refused[first] | killed by tests/test_aegis_h1_edges.py::test_edges_a_same_size_edit_of_the_files_first_or_last_byte_is_refused[first] |
| X024 | service.py:L373 file_problem only when log non-empty | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log |
| N020 | pii.py:L257 cluster: ASCII letters -> isalpha | killed by tests/test_ooh2_pii1_cluster_rule.py::test_decisions_screen_finds_it[1234\u02bc56789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_decisions_screen_finds_it[1234\u02bc56789] |
| N170 | service.py:L100 request_sha: hashes raw body | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] |
| N140 | svc_campaigns.py:L66 observed: kinds not exposed | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] |
| F001 | store.py:L288 file_problem compare last byte dropped | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N021 | pii.py:L257 cluster: ASCII letters -> isalnum | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[123 ** 45 ** 6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[123 ** 45 ** 6789] |
| N171 | service.py:L94 request_sha: cache constant key | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] |
| N141 | svc_campaigns.py:L65 observed: redacted flag False | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] |
| F002 | store.py:L288 file_problem compare first byte dropped | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N022 | pii.py:L257 cluster: space ends a cluster | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[123 ** 45 ** 6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[123 ** 45 ** 6789] |
| N172 | service.py:L77 _screened: vendor_text not screened | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] |
| N142 | svc_campaigns.py:L65 observed: redacted flag inverted | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] |
| F003 | store.py:L288 file_problem compare first byte only | killed by tests/test_aegis_h1_file_content.py::test_h1_integrity_check_reports_a_changed_file_before_any_write | killed by tests/test_aegis_h1_file_content.py::test_h1_integrity_check_reports_a_changed_file_before_any_write |
| N023 | pii.py:L257 cluster: any non-digit ends a cluster | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[123 ** 45 ** 6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[123 ** 45 ** 6789] |
| N173 | service.py:L77 _screened: kinds not text | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] |
| N143 | svc_campaigns.py:L64 observed: screen replaced by screen_form (no redaction) | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] |
| F004 | store.py:L288 file_problem compare length only | killed by tests/test_aegis_h1_file_content.py::test_h1_integrity_check_reports_a_changed_file_before_any_write | killed by tests/test_aegis_h1_file_content.py::test_h1_integrity_check_reports_a_changed_file_before_any_write |
| N024 | pii.py:L257 cluster: punctuation ends a cluster | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[12-34-56-789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[12-34-56-789] |
| N174 | service.py:L79 _screened: only top-level key vendor_text (lists skipped) | killed by tests/test_aegis_r9_pii_hardening.py::test_r9_f1_only_vendor_text_is_redacted_for_the_hash | killed by tests/test_aegis_r9_pii_hardening.py::test_r9_f1_only_vendor_text_is_redacted_for_the_hash |
| N144 | svc_campaigns.py:L64 observed: kept text is raw | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] |
| F005 | store.py:L288 file_problem size check removed | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log |
| N025 | pii.py:L261 cluster: digit test isdigit | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N175 | service.py:L77 _screened: isinstance(x,str) -> True | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] |
| N145 | svc_campaigns.py:L64 observed: kinds from [0] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_unit_vendor_text_card_or_ssn_is_redacted_before_the_write[4111 1111 1111 1111] |
| F006 | store.py:L288 file_problem size check < instead of != | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log |
| N026 | pii.py:L261 cluster: digit test isdecimal | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N176 | service.py:L77 _screened: other key screened too (description) | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123 ** 45 ** 6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123..45..6789] |
| N146 | svc_campaigns.py:L68 observed: sha of raw text | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| F007 | store.py:L288 file_problem size check > instead of != | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N027 | pii.py:L261 cluster: digit upper bound 9 -> 8 | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[123 ** 45 ** 6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[123 ** 45 ** 6789] |
| N177 | service.py:L100 request_sha: cache returns before screening wrong key (key from screened) | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] |
| N147 | svc_campaigns.py:L67 observed: truncation flag | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| F008 | store.py:L288 file_problem regular-file check removed | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N028 | pii.py:L261 cluster: digit lower bound 0 -> 1 | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[021000021] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_free_text_screen_finds_it[021000021] |
| N178 | service.py:L103 request_sha: 64 -> 63 | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N148 | svc_campaigns.py:L66 observed: truncation bound | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| F009 | store.py:L283 file_problem missing file -> differs always | killed by tests/test_aegis_h1_file_content.py::test_h1_same_size_edit_of_an_existing_line_refuses_the_next_write[first] | killed by tests/test_aegis_h1_file_content.py::test_h1_same_size_edit_of_an_existing_line_refuses_the_next_write[first] |
| N029 | pii.py:L248 _ASCII_LETTERS drops Z | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N179 | service.py:L103 request_sha: no eviction | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N149 | svc_campaigns.py:L66 observed: untrusted flag False | killed by tests/test_contracts.py::test_contract_vendor_text_is_untrusted | killed by tests/test_contracts.py::test_contract_vendor_text_is_untrusted |
| F010 | store.py:L283 file_problem missing file -> None always | killed by tests/test_aegis_h1_file_content.py::test_h1_deleted_log_file_is_reported_and_refused | killed by tests/test_aegis_h1_file_content.py::test_h1_deleted_log_file_is_reported_and_refused |
| N030 | pii.py:L248 _ASCII_LETTERS drops z | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N180 | service.py:L102 request_sha: cache store of raw hash | killed by tests/test_aegis_r9_pii_hardening.py::test_r9_f1_request_hash_is_over_the_redacted_body | killed by tests/test_aegis_r9_pii_hardening.py::test_r9_f1_request_hash_is_over_the_redacted_body |
| N150 | svc_campaigns.py:L863 approval screen: check removed | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_client_approval_doc_ref_card_or_ssn_is_refused_before_any_write[4111-1111-1111-1111] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_client_approval_doc_ref_card_or_ssn_is_refused_before_any_write[4111-1111-1111-1111] |
| F011 | store.py:L283 file_problem mem empty check inverted | killed by tests/test_aegis_h1_file_content.py::test_h1_same_size_edit_of_an_existing_line_refuses_the_next_write[first] | killed by tests/test_aegis_h1_file_content.py::test_h1_same_size_edit_of_an_existing_line_refuses_the_next_write[first] |
| N031 | pii.py:L248 _ASCII_LETTERS drops A | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N181 | service.py:L99 request_sha: cache hit returns other | killed by tests/test_aegis_r9_pii_hardening.py::test_r9_f1_request_hash_is_over_the_redacted_body | killed by tests/test_aegis_r9_pii_hardening.py::test_r9_f1_request_hash_is_over_the_redacted_body |
| N151 | svc_campaigns.py:L864 approval screen: reason code changed | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_client_approval_doc_ref_card_or_ssn_is_refused_before_any_write[4111-1111-1111-1111] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_client_approval_doc_ref_card_or_ssn_is_refused_before_any_write[4111-1111-1111-1111] |
| F012 | store.py:L288 file_problem compares to mem without trailing newline | killed by tests/test_aegis_h1_file_content.py::test_h1_integrity_check_is_ok_on_an_unchanged_file | killed by tests/test_aegis_h1_file_content.py::test_h1_integrity_check_is_ok_on_an_unchanged_file |
| N032 | pii.py:L248 _ASCII_LETTERS drops a | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_face_text_attribute_is_refused[123 ** 45 ** 6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_face_text_attribute_is_refused[123 ** 45 ** 6789] |
| R001 | service.py:L77 _screened: vendor_text key never matched | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] |
| N152 | svc_campaigns.py:L864 approval screen: field name changed | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| F013 | store.py:L281 file_problem: no O_NOFOLLOW | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_does_not_follow_a_symlink_even_to_identical_bytes | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_does_not_follow_a_symlink_even_to_identical_bytes |
| N033 | pii.py:L248 _ASCII_LETTERS lowercase only | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| R002 | service.py:L77 _screened: vendor_text not str-checked (unredacted path) | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] |
| N153 | svc_campaigns.py:L864 approval screen: raise -> pass | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_client_approval_doc_ref_card_or_ssn_is_refused_before_any_write[4111-1111-1111-1111] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_client_approval_doc_ref_card_or_ssn_is_refused_before_any_write[4111-1111-1111-1111] |
| F014 | store.py:L281 file_problem: no O_NONBLOCK | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_does_not_block_on_a_fifo | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_does_not_block_on_a_fifo |
| N034 | pii.py:L248 _ASCII_LETTERS uppercase only | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_ordinary_vendor_text_is_not_redacted[permit 123-45 at 6789 Sunset] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_ordinary_vendor_text_is_not_redacted[permit 123-45 at 6789 Sunset] |
| R003 | service.py:L77 _screened: kept text replaced by kinds list | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] |
| N154 | svc_campaigns.py:L863 approval screen: old has_personal_data | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_client_approval_doc_ref_is_refused[DOC-2026-10-18-001] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_client_approval_doc_ref_is_refused[DOC-2026-10-18-001] |
| F015 | store.py:L292 file_problem: read error reported as None | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N035 | pii.py:L256 cluster: sentinel removed | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_face_text_attribute_is_refused[123 ** 45 ** 6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_face_text_attribute_is_refused[123 ** 45 ** 6789] |
| R004 | service.py:L77 _screened: vendor text kept raw | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_vendor_text_is_redacted_and_hashed_redacted[123-45-6789] |
| N155 | svc_campaigns.py:L863 approval screen: negated | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_client_approval_doc_ref_card_or_ssn_is_refused_before_any_write[4111-1111-1111-1111] | killed by tests/test_ooh2_pii1_card_ssn.py::test_pii1_client_approval_doc_ref_card_or_ssn_is_refused_before_any_write[4111-1111-1111-1111] |
| F016 | store.py:L284 file_problem: open error reported as None | killed by tests/test_aegis_h1_file_content.py::test_h1_unreadable_log_file_is_reported | killed by tests/test_aegis_h1_file_content.py::test_h1_unreadable_log_file_is_reported |
| N036 | pii.py:L256 cluster: sentinel is a digit | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_face_text_attribute_is_refused[123 ** 45 ** 6789] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_face_text_attribute_is_refused[123 ** 45 ** 6789] |
| R005 | service.py:L80 _screened: lists not recursed | killed by tests/test_aegis_r9_pii_hardening.py::test_r9_f1_only_vendor_text_is_redacted_for_the_hash | killed by tests/test_aegis_r9_pii_hardening.py::test_r9_f1_only_vendor_text_is_redacted_for_the_hash |
| N156 | svc_campaigns.py:L169 names_a_card: no NFKC | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| F017 | store.py:L276 file_problem: no 'with self.lock' -> mem read before lock is n/a; mem from stale | killed by tests/test_aegis_h1_file_content.py::test_h1_integrity_check_is_ok_on_an_unchanged_file | killed by tests/test_aegis_h1_file_content.py::test_h1_integrity_check_is_ok_on_an_unchanged_file |
| N037 | pii.py:L259 cluster: end excludes last digit | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_labels_say_what_was_redacted | killed by tests/test_ooh2_pii1_cluster_rule.py::test_cluster_labels_say_what_was_redacted |
| R006 | service.py:L77 _screened: dict values not recursed | killed by tests/test_aegis_r9_pii_hardening.py::test_r9_f1_only_vendor_text_is_redacted_for_the_hash | killed by tests/test_aegis_r9_pii_hardening.py::test_r9_f1_only_vendor_text_is_redacted_for_the_hash |
| N157 | svc_campaigns.py:L169 names_a_card: no casefold | UNRUN | UNRUN |
| O001 | store.py:L228 append open: drop O_NOFOLLOW | UNRUN | UNRUN |
| N038 | pii.py:L262 cluster: start one late | UNRUN | UNRUN |
| R007 | service.py:L77 _screened: screen only (no kinds) uses has_personal_data redact | UNRUN | UNRUN |
| N158 | svc_campaigns.py:L169 names_a_card: card_spans -> False | UNRUN | UNRUN |
| O002 | store.py:L228 append open: drop O_NONBLOCK | UNRUN | UNRUN |
| N039 | pii.py:L258 cluster: min count > not >= | UNRUN | UNRUN |
| R008 | service.py:L76 _screened: dict returned unscreened | UNRUN | UNRUN |
| N159 | svc_campaigns.py:L169 names_a_card: not bool | UNRUN | UNRUN |
| O003 | store.py:L233 append: S_ISREG check removed | UNRUN | UNRUN |
| N040 | pii.py:L245 CLUSTER_MIN_DIGITS 9 -> 10 | UNRUN | UNRUN |
| R009 | service.py:L100 request_sha: hash of raw body | UNRUN | UNRUN |
| N160 | svc_campaigns.py:L146 _luhn_card alias always False | UNRUN | UNRUN |
| O004 | store.py:L233 append: S_ISREG check inverted | UNRUN | UNRUN |
| N041 | pii.py:L245 CLUSTER_MIN_DIGITS 9 -> 8 | UNRUN | UNRUN |
| R010 | service.py:L94 request_sha: cache key constant (all bodies collide) | UNRUN | UNRUN |
| N161 | svc_campaigns.py:L145 _aba_routing alias always True | UNRUN | UNRUN |
| O005 | store.py:L142 start: drop O_NOFOLLOW | UNRUN | UNRUN |
| N043 | pii.py:L260 cluster: count not reset at a letter (variant) | UNRUN | UNRUN |
| R011 | service.py:L94 request_sha: cache key = body length | UNRUN | UNRUN |
| N190 | svc_inventory.py:L73 parse_value: text screen removed | UNRUN | UNRUN |
| O006 | store.py:L142 start: drop O_NONBLOCK | UNRUN | UNRUN |
| N044 | pii.py:L260 cluster: first not reset at a letter | UNRUN | UNRUN |
| R012 | service.py:L94 request_sha: cache key = first 8 hex of digest | UNRUN | UNRUN |
| N191 | svc_inventory.py:L73 parse_value: text screen negated | UNRUN | UNRUN |
| O007 | store.py:L150 start: S_ISREG check removed | UNRUN | UNRUN |
| N045 | pii.py:L263 cluster: count not incremented | UNRUN | UNRUN |
| R013 | service.py:L102 request_sha: cache never stores | UNRUN | UNRUN |
| N192 | svc_inventory.py:L73 parse_value: text -> old screen | UNRUN | UNRUN |
| O008 | store.py:L146 start: ELOOP message condition flipped | UNRUN | UNRUN |
| N046 | pii.py:L263 cluster: count += 2 | UNRUN | UNRUN |
| R014 | service.py:L102 request_sha: cache stores raw body hash (leak) | UNRUN | UNRUN |
| N193 | svc_inventory.py:L75 parse_value: ref screen removed | UNRUN | UNRUN |
| O009 | store.py:L152 start: read size st_size-1 | UNRUN | UNRUN |
| N047 | pii.py:L258 cluster: letters never flush | UNRUN | UNRUN |
| R015 | service.py:L103 request_sha: cache bound 64 -> 0 (evict all) | UNRUN | UNRUN |
| N194 | svc_inventory.py:L75 parse_value: ref -> new screen | UNRUN | UNRUN |
| O010 | store.py:L153 start: read errors not StoreCorrupt | UNRUN | UNRUN |
| N050 | pii.py:L285 _redact_all: range(1) | UNRUN | UNRUN |
| R016 | service.py:L103 request_sha: cache bound 64 -> unbounded | UNRUN | UNRUN |
| N195 | svc_inventory.py:L73 parse_value: text kind and -> or | UNRUN | UNRUN |
| O011 | store.py:L65 _close_quietly swallows nothing (except ZeroDivisionError) | UNRUN | UNRUN |
| N051 | pii.py:L285 _redact_all: range(2) | UNRUN | UNRUN |
| R017 | service.py:L104 request_sha: evict newest | UNRUN | UNRUN |
| N196 | svc_inventory.py:L73 parse_value: text raise reason changed | UNRUN | UNRUN |
| O012 | store.py:L64 _close_quietly never closes | UNRUN | UNRUN |
| N052 | pii.py:L285 _redact_all: range(3) | UNRUN | UNRUN |
| R018 | service.py:L98 request_sha: no move_to_end | UNRUN | UNRUN |
| N197 | svc_inventory.py:L75 parse_value: ref raise -> pass | UNRUN | UNRUN |
| O013 | store.py:L65 _close_quietly swallows everything (Exception) | UNRUN | UNRUN |
| N053 | pii.py:L285 _redact_all: range(5) | UNRUN | UNRUN |
| R019 | service.py:L97 request_sha: hit ignored | UNRUN | UNRUN |
| N198 | svc_inventory.py:L73 parse_value: text raise -> pass | UNRUN | UNRUN |
| O014 | store.py:L266 append finally: raw os.close | UNRUN | UNRUN |
| N054 | pii.py:L286 _redact_all: unsorted spans | UNRUN | UNRUN |
| R020 | service.py:L94 request_sha: cache key from _screened body-less (canonical of screened) | UNRUN | UNRUN |
| S001 | svc_inventory.py:L73 parse_value text: old screen | UNRUN | UNRUN |
| O015 | store.py:L294 file_problem finally: raw os.close | UNRUN | UNRUN |
| N055 | pii.py:L286 _redact_all: sorted reverse | UNRUN | UNRUN |
| R021 | service.py:L99 request_sha: return hit -> hash of key | UNRUN | UNRUN |
| S002 | svc_inventory.py:L73 parse_value text: screen removed | UNRUN | UNRUN |
| O016 | store.py:L156 start finally: raw os.close | UNRUN | UNRUN |
| N056 | pii.py:L287 _redact_all: no-span return -> break | UNRUN | UNRUN |
| R022 | service.py:L95 request_sha: cache lock removed (read) | UNRUN | UNRUN |
| S003 | svc_inventory.py:L75 parse_value ref: new screen on ref too (changes) | UNRUN | UNRUN |
| O017 | store.py:L138 start: log missing but data dir present -> raw [] | UNRUN | UNRUN |
| N057 | pii.py:L287 _redact_all: no-span return -> pass (loops) | UNRUN | UNRUN |
| S004 | svc_inventory.py:L75 parse_value ref: screen removed | UNRUN | UNRUN |
| X009 | store.py:L237 size==expected -> size>=expected | UNRUN | UNRUN |
| N058 | pii.py:L298 _redact_all: residual kept | UNRUN | UNRUN |
| S005 | svc_inventory.py:L73 parse_value: text and ref both old screen | UNRUN | UNRUN |
| X012 | store.py:L236 adopt = size >= expected+len+1 | UNRUN | UNRUN |
| N059 | pii.py:L298 _redact_all: always [redacted] | UNRUN | UNRUN |
| S006 | svc_inventory.py:L73 parse_value: text screen only for ref kind | UNRUN | UNRUN |
| X013 | store.py:L236 adopt = size > expected | UNRUN | UNRUN |
| N060 | pii.py:L298 _redact_all: inverted residual | UNRUN | UNRUN |
| S007 | svc_campaigns.py:L863 client_approval_doc_ref: screen removed | UNRUN | UNRUN |
| X014 | store.py:L237 mem compare first byte only | UNRUN | UNRUN |
| N061 | pii.py:L298 _redact_all: residual check on find without t | UNRUN | UNRUN |
| S008 | svc_campaigns.py:L863 client_approval_doc_ref: old screen | UNRUN | UNRUN |
| X015 | store.py:L237 mem compare length only | UNRUN | UNRUN |
| N062 | pii.py:L291 _redact_all: overlap skip removed | UNRUN | UNRUN |
| S009 | svc_campaigns.py:L863 client_approval_doc_ref: screens a different key | UNRUN | UNRUN |
| X018 | store.py:L238 adopt compare only trailing newline | UNRUN | UNRUN |
| N063 | pii.py:L291 _redact_all: overlap skip < not <= | UNRUN | UNRUN |
| S010 | svc_campaigns.py:L64 observed: text not screened | UNRUN | UNRUN |
| X019 | store.py:L238 adopt compare first byte only | UNRUN | UNRUN |
| N064 | pii.py:L293 _redact_all: label dropped | UNRUN | UNRUN |
| S011 | svc_campaigns.py:L65 observed: kinds dropped (redacted flag False) | UNRUN | UNRUN |
| X020 | store.py:L238 adopt compare last content byte dropped | UNRUN | UNRUN |
| N065 | pii.py:L293 _redact_all: label of the wrong slice | UNRUN | UNRUN |
| S012 | svc_campaigns.py:L68 observed: sha of raw text | UNRUN | UNRUN |
| N066 | pii.py:L294 _redact_all: gap lost | UNRUN | UNRUN |
| S013 | svc_campaigns.py:L64 observed: old screen (has_personal_data/redact) | UNRUN | UNRUN |
| N067 | pii.py:L297 _redact_all: tail lost | UNRUN | UNRUN |
| S014 | svc_campaigns.py:L64 observed: truncation before redaction n/a: text[:max] | UNRUN | UNRUN |
| N068 | pii.py:L295 _redact_all: pos = lo | UNRUN | UNRUN |
| S015 | svc_campaigns.py:L103 names_a_card: no casefold | UNRUN | UNRUN |
| N069 | pii.py:L294 _redact_all: max(lo,pos) -> lo | UNRUN | UNRUN |
| S016 | svc_campaigns.py:L103 names_a_card: no NFKC | UNRUN | UNRUN |
| N070 | pii.py:L295 _redact_all: marker text changed | UNRUN | UNRUN |
| S017 | svc_campaigns.py:L169 names_a_card: returns False for card_spans | UNRUN | UNRUN |
| N080 | pii.py:L308 screen_free_text: first pass cluster rule skipped | UNRUN | UNRUN |
| S018 | svc_campaigns.py:L146 _luhn_card alias -> always False | UNRUN | UNRUN |
| N081 | pii.py:L308 screen_free_text: first pass uses raw text | UNRUN | UNRUN |
| S019 | svc_campaigns.py:L145 _aba_routing alias -> always False | UNRUN | UNRUN |
| N082 | pii.py:L308 screen_free_text: first pass finder = all spans | UNRUN | UNRUN |
| N083 | pii.py:L308 screen_free_text: first pass finder none | UNRUN | UNRUN |
| N084 | pii.py:L309 screen_free_text: second pass not _norm | UNRUN | UNRUN |
| N085 | pii.py:L309 screen_free_text: second pass skipped | UNRUN | UNRUN |
| N086 | pii.py:L309 screen_free_text: second pass cluster only | UNRUN | UNRUN |
| N087 | pii.py:L309 screen_free_text: second pass shapes only | UNRUN | UNRUN |
| N088 | pii.py:L309 screen_free_text: _norm(text) not _norm(t) | UNRUN | UNRUN |
| N090 | pii.py:L314 html: loop range(3)->range(1) | UNRUN | UNRUN |
| N091 | pii.py:L314 html: loop range(3)->range(2) | UNRUN | UNRUN |
| N092 | pii.py:L314 html: loop range(3)->range(0) | UNRUN | UNRUN |
| N093 | pii.py:L314 html: loop range(3)->range(4) | UNRUN | UNRUN |
| N094 | pii.py:L315 html: unescape removed | UNRUN | UNRUN |
| N095 | pii.py:L316 html: break -> pass | UNRUN | UNRUN |
| N096 | pii.py:L316 html: decoded not advanced | UNRUN | UNRUN |
| N097 | pii.py:L313 html: decoded starts from text | UNRUN | UNRUN |
| N098 | pii.py:L319 html: decoded != t guard dropped | UNRUN | UNRUN |
| N099 | pii.py:L319 html: decoded != t -> True | UNRUN | UNRUN |
| N100 | pii.py:L319 html: spans check dropped | UNRUN | UNRUN |
| N101 | pii.py:L319 html: and -> or | UNRUN | UNRUN |
| N102 | pii.py:L319 html: check on undecoded norm | UNRUN | UNRUN |
| N103 | pii.py:L319 html: check without _norm | UNRUN | UNRUN |
| N104 | pii.py:L319 html: whole branch removed | UNRUN | UNRUN |
| N105 | pii.py:L320 html: redact without _norm | UNRUN | UNRUN |
| N106 | pii.py:L320 html: redact skipped (decoded kept in clear) | UNRUN | UNRUN |
| N107 | pii.py:L320 html: redact with cluster only | UNRUN | UNRUN |
| N108 | pii.py:L320 html: kinds not collected | UNRUN | UNRUN |
| N109 | pii.py:L320 html: redact result discarded | UNRUN | UNRUN |
| N110 | pii.py:L321 screen_free_text: kinds check inverted | UNRUN | UNRUN |
| N111 | pii.py:L322 screen_free_text: clean returns raw text | UNRUN | UNRUN |
| N112 | pii.py:L322 screen_free_text: clean returns t | UNRUN | UNRUN |
| N113 | pii.py:L322 screen_free_text: clean returns _norm(text) | UNRUN | UNRUN |
| N114 | pii.py:L323 screen_free_text: kinds order/empty | UNRUN | UNRUN |
| N115 | pii.py:L323 screen_free_text: text not collapsed | UNRUN | UNRUN |
| N116 | pii.py:L323 screen_free_text: kinds unsorted list | UNRUN | UNRUN |
| N117 | pii.py:L323 screen_free_text: returns text in clear when redacted | UNRUN | UNRUN |
| N118 | pii.py:L327 free_text_has_personal_data: always False | UNRUN | UNRUN |
| N119 | pii.py:L327 free_text_has_personal_data: [0] not [1] | UNRUN | UNRUN |
| N120 | pii.py:L327 free_text_has_personal_data: text changed instead | UNRUN | UNRUN |
| N001 | pii.py:L84 _REMOVED drops Co | UNRUN | UNRUN |
| N002 | pii.py:L84 _REMOVED drops Cf | UNRUN | UNRUN |
| N003 | pii.py:L84 _REMOVED adds Cn (previous behaviour) | UNRUN | UNRUN |
| N004 | pii.py:L84 _REMOVED empty | UNRUN | UNRUN |
| N005 | pii.py:L84 _REMOVED adds Zs | UNRUN | UNRUN |
| N006 | pii.py:L112 screen_form: Cn exemption removed | UNRUN | UNRUN |
| N007 | pii.py:L112 screen_form: all printable | UNRUN | UNRUN |
| N008 | pii.py:L112 screen_form: Cn exemption -> Cs | UNRUN | UNRUN |
| N009 | pii.py:L112 screen_form: Cn exemption -> Cc (controls kept) | UNRUN | UNRUN |
| N010 | pii.py:L112 screen_form: Cn exemption -> and | UNRUN | UNRUN |
| N011 | pii.py:L112 screen_form: no space collapse | UNRUN | UNRUN |
| N012 | pii.py:L113 screen_form: collapse only (join without split) | UNRUN | UNRUN |
| N130 | pii.py:L99 strip_format: Cn path not kept (hidden for Cf only) | UNRUN | UNRUN |
| N131 | pii.py:L93 strip_format: no NFKC | UNRUN | UNRUN |
| N132 | pii.py:L93 strip_format: _INVISIBLE not applied | UNRUN | UNRUN |
| N133 | pii.py:L93 strip_format: _ZW not applied | UNRUN | UNRUN |
| N134 | pii.py:L280 _free_text_spans: cluster dropped | UNRUN | UNRUN |
| N135 | pii.py:L280 _free_text_spans: shapes dropped | UNRUN | UNRUN |
| P001 | pii.py:L78 _INVISIBLE drops U+034F | UNRUN | UNRUN |
| P002 | pii.py:L79 _INVISIBLE drops tag variation selectors | UNRUN | UNRUN |
| P003 | pii.py:L78 _INVISIBLE drops variation selectors ufe00-ufe0f | UNRUN | UNRUN |
| P004 | pii.py:L78 _INVISIBLE drops braille blank u2800 | UNRUN | UNRUN |
| P005 | pii.py:L78 _INVISIBLE drops hangul filler u3164 and uffa0 | UNRUN | UNRUN |
| P006 | pii.py:L78 _INVISIBLE drops u180b-u180f | UNRUN | UNRUN |
| P010 | pii.py:L93 strip_format: no NFKC | UNRUN | UNRUN |
| P011 | pii.py:L97 strip_format: marks kept on digits | UNRUN | UNRUN |
| P012 | pii.py:L97 strip_format: Me not dropped | UNRUN | UNRUN |
| P013 | pii.py:L97 strip_format: Mc not dropped | UNRUN | UNRUN |
| P014 | pii.py:L97 strip_format: Mn not dropped | UNRUN | UNRUN |
| P015 | pii.py:L103 strip_format: non-ASCII digits not converted | UNRUN | UNRUN |
| P016 | pii.py:L97 strip_format: mark dropped regardless of previous digit | UNRUN | UNRUN |
| P017 | pii.py:L100 strip_format: hidden ignored for _REMOVED | UNRUN | UNRUN |
| P020 | pii.py:L113 screen_form: hidden not passed | UNRUN | UNRUN |
| P021 | pii.py:L118 _norm: hidden not passed | UNRUN | UNRUN |
| P022 | pii.py:L119 _norm: no %40 | UNRUN | UNRUN |
| P023 | pii.py:L120 _norm: no at spellings | UNRUN | UNRUN |
| P024 | pii.py:L121 _norm: no dot spellings | UNRUN | UNRUN |
| P025 | pii.py:L219 _screened_form: alt uses same hidden | UNRUN | UNRUN |
| P026 | pii.py:L220 _screened_form: always t | UNRUN | UNRUN |
| P027 | pii.py:L217 _screened_form: always alt | UNRUN | UNRUN |
| P030 | pii.py:L138 _DIGIT_RUN: letters allowed between groups | UNRUN | UNRUN |
| P031 | pii.py:L138 _DIGIT_RUN: single separator only | UNRUN | UNRUN |
| P032 | pii.py:L138 _DIGIT_RUN: lowercase letters only block | UNRUN | UNRUN |
| P033 | pii.py:L138 _DIGIT_RUN: at most 3 separators | UNRUN | UNRUN |
| P034 | pii.py:L138 _DIGIT_RUN: separators not repeated groups | UNRUN | UNRUN |
| P035 | pii.py:L139 _GROUP single digits | UNRUN | UNRUN |
| P036 | pii.py:L135 _CARD_PREFIX: drops Visa 4 | UNRUN | UNRUN |
| P037 | pii.py:L135 _CARD_PREFIX: 5[0-8] -> 5[1-5] | UNRUN | UNRUN |
| P038 | pii.py:L135 _CARD_PREFIX: drops 3[0-9] (Amex/JCB/Diners) | UNRUN | UNRUN |
| P039 | pii.py:L135 _CARD_PREFIX: 3[0-9] -> 3[4-7] | UNRUN | UNRUN |
| P040 | pii.py:L135 _CARD_PREFIX: drops 6 (Discover/UnionPay) | UNRUN | UNRUN |
| P041 | pii.py:L135 _CARD_PREFIX: drops 8[12] | UNRUN | UNRUN |
| P042 | pii.py:L135 _CARD_PREFIX: drops 2-series Mastercard 222[1-9] | UNRUN | UNRUN |
| P043 | pii.py:L135 _CARD_PREFIX: drops 2720 | UNRUN | UNRUN |
| P044 | pii.py:L136 _CARD_PREFIX: 9792/9860 dropped | UNRUN | UNRUN |
| P045 | pii.py:L135 _CARD_PREFIX not anchored (search-like) | UNRUN | UNRUN |
| P046 | pii.py:L137 _CARD_PREFIX_15: drops 4 | UNRUN | UNRUN |
| P047 | pii.py:L137 _CARD_PREFIX_15: 3[4-8] -> 3[47] | UNRUN | UNRUN |
| P048 | pii.py:L137 _CARD_PREFIX_15: drops 6011 | UNRUN | UNRUN |
| P049 | pii.py:L137 _CARD_PREFIX_15: drops 5[0-8] | UNRUN | UNRUN |
| P050 | pii.py:L152 luhn: 15-digit rule removed | UNRUN | UNRUN |
| P051 | pii.py:L151 luhn: prefix check removed | UNRUN | UNRUN |
| P052 | pii.py:L151 luhn: 16-digit special (len != 16) | UNRUN | UNRUN |
| P053 | pii.py:L158 luhn: 'd * 2 - 9 if d > 4' -> d*2 | UNRUN | UNRUN |
| P054 | pii.py:L157 luhn: i % 2 -> (i+1) % 2 | UNRUN | UNRUN |
| P055 | pii.py:L155 luhn: no reversed | UNRUN | UNRUN |
| P056 | pii.py:L146 aba: weights (3,7,1)->(3,1,7) | UNRUN | UNRUN |
| P057 | pii.py:L144 aba: len check != 9 removed | UNRUN | UNRUN |
| P058 | pii.py:L146 aba: always False | UNRUN | UNRUN |
| P059 | pii.py:L182 _card_from: group >=3 -> >=2 | UNRUN | UNRUN |
| P060 | pii.py:L182 _card_from: group >=3 -> >=4 | UNRUN | UNRUN |
| P061 | pii.py:L182 _card_from: aba exclusion removed | UNRUN | UNRUN |
| P062 | pii.py:L182 _card_from: only groups[i] checked | UNRUN | UNRUN |
| P063 | pii.py:L182 _card_from: only groups[j] checked | UNRUN | UNRUN |
| P064 | pii.py:L185 _card_from: exceed 19 -> continue (not stop) | UNRUN | UNRUN |
| P065 | pii.py:L183 _card_from: grouping failure -> continue | UNRUN | UNRUN |
| P066 | pii.py:L170 card_spans: only first start index | UNRUN | UNRUN |
| P067 | pii.py:L171 card_spans: span of the first group only | UNRUN | UNRUN |
| P068 | pii.py:L185 _card_from: stop at 20 / 18 | UNRUN | UNRUN |
| P069 | pii.py:L177 _card_from: j range excludes last group | UNRUN | UNRUN |
| P070 | pii.py:L198 _SSEP: {1,3} -> {1,2} | UNRUN | UNRUN |
| P071 | pii.py:L198 _SSEP: {1,3} -> {1,4} | UNRUN | UNRUN |
| P072 | pii.py:L198 _SSEP: {1,3} -> {0,3} | UNRUN | UNRUN |
| P073 | pii.py:L198 _SSEP: newline allowed | UNRUN | UNRUN |
| P074 | pii.py:L198 _SSEP: lowercase letters only excluded | UNRUN | UNRUN |
| P075 | pii.py:L199 _SSN: lookbehind removed | UNRUN | UNRUN |
| P076 | pii.py:L199 _SSN: lookahead removed | UNRUN | UNRUN |
| P077 | pii.py:L199 _SSN: area 3->2 digits | UNRUN | UNRUN |
| P078 | pii.py:L199 _SSN: group 2->3 | UNRUN | UNRUN |
| P079 | pii.py:L199 _SSN: serial 4->3 | UNRUN | UNRUN |
| P080 | pii.py:L200 _SSN_WORDS: drops ssn | UNRUN | UNRUN |
| P081 | pii.py:L200 _SSN_WORDS: drops itin | UNRUN | UNRUN |
| P082 | pii.py:L200 _SSN_WORDS: drops tin | UNRUN | UNRUN |
| P083 | pii.py:L200 _SSN_WORDS: drops tax id | UNRUN | UNRUN |
| P084 | pii.py:L200 _SSN_WORDS: drops social security | UNRUN | UNRUN |
| P085 | pii.py:L201 _SSN_VALUE: {0,20} -> {0,19} | UNRUN | UNRUN |
| P086 | pii.py:L201 _SSN_VALUE: {0,20} -> {0,21} | UNRUN | UNRUN |
| P087 | pii.py:L201 _SSN_VALUE: {0,20} -> {0,10} | UNRUN | UNRUN |
| P088 | pii.py:L201 _SSN_VALUE: word lookbehind removed | UNRUN | UNRUN |
| P089 | pii.py:L201 _SSN_VALUE: word lookahead removed | UNRUN | UNRUN |
| P090 | pii.py:L201 _SSN_VALUE: {8} -> {7} | UNRUN | UNRUN |
| P091 | pii.py:L201 _SSN_VALUE: {8} -> {9} | UNRUN | UNRUN |
| P092 | pii.py:L201 _SSN_VALUE: inner sep {0,3} -> {0,2} | UNRUN | UNRUN |
| P093 | pii.py:L201 _SSN_VALUE: inner sep {0,3} -> {1,3} | UNRUN | UNRUN |
| P094 | pii.py:L202 _SSN_VALUE: not case-insensitive | UNRUN | UNRUN |
| P095 | pii.py:L201 _SSN_VALUE: trailing digit lookahead removed | UNRUN | UNRUN |
| P096 | pii.py:L201 _SSN_VALUE: gap allows newline | UNRUN | UNRUN |
| P097 | pii.py:L207 _ssn_spans: group span -> whole span | UNRUN | UNRUN |
| P098 | pii.py:L207 _ssn_spans: only _SSN | UNRUN | UNRUN |
| P099 | pii.py:L206 _ssn_spans: only _SSN_VALUE | UNRUN | UNRUN |
| P104 | pii.py:L256 cluster: sentinel removed | UNRUN | UNRUN |
| P105 | pii.py:L259 cluster: end excludes last digit | UNRUN | UNRUN |
| P106 | pii.py:L262 cluster: start one late | UNRUN | UNRUN |
| P107 | pii.py:L260 cluster: count reset not done on letter | UNRUN | UNRUN |
| P108 | pii.py:L263 cluster: only digits counted on same segment (count not incremented) | UNRUN | UNRUN |
| P110 | pii.py:L280 _free_text_spans: no cluster | UNRUN | UNRUN |
| P111 | pii.py:L280 _free_text_spans: no shapes | UNRUN | UNRUN |
| P112 | pii.py:L268 _label: card before email | UNRUN | UNRUN |
| P113 | pii.py:L270 _label: ssn before card | UNRUN | UNRUN |
| P114 | pii.py:L274 _label: no phone | UNRUN | UNRUN |
| P115 | pii.py:L276 _label: number default -> card | UNRUN | UNRUN |
| P120 | pii.py:L322 screen_free_text: returns raw text when clean | UNRUN | UNRUN |
| P121 | pii.py:L322 screen_free_text: returns norm text when clean | UNRUN | UNRUN |
| P122 | pii.py:L323 screen_free_text: redacted text not space-collapsed | UNRUN | UNRUN |
| P123 | pii.py:L323 screen_free_text: kinds empty list when redacted | UNRUN | UNRUN |
| P126 | pii.py:L291 screen_free_text: no overlap skip | UNRUN | UNRUN |
| P127 | pii.py:L293 screen_free_text: kinds only first | UNRUN | UNRUN |
| P130 | pii.py:L327 free_text_has_personal_data: kinds[0]->screen only | UNRUN | UNRUN |
| P131 | pii.py:L327 free_text_has_personal_data: always False | UNRUN | UNRUN |
| P132 | pii.py:L224 has_personal_data: raw text | UNRUN | UNRUN |
| P133 | pii.py:L224 has_personal_data: _norm only | UNRUN | UNRUN |
| P134 | pii.py:L334 redact: loop 4 -> 1 | UNRUN | UNRUN |
| P135 | pii.py:L338 redact: residual returns t | UNRUN | UNRUN |
| P136 | pii.py:L231 _redact_once: overlap skip removed | UNRUN | UNRUN |
| P137 | pii.py:L211 _spans: no cards | UNRUN | UNRUN |
| P138 | pii.py:L211 _spans: no ssn | UNRUN | UNRUN |
| P139 | pii.py:L211 _spans: no _matches | UNRUN | UNRUN |
| G001 | pii.py:L78 stmt->pass | UNRUN | UNRUN |
| G002 | pii.py:L84 stmt->pass | UNRUN | UNRUN |
| G003 | pii.py:L93 stmt->pass | UNRUN | UNRUN |
| G004 | pii.py:L94 stmt->pass | UNRUN | UNRUN |
| G005 | pii.py:L96 stmt->pass | UNRUN | UNRUN |
| G006 | pii.py:L97 cond->True | UNRUN | UNRUN |
| G007 | pii.py:L97 cond->False | UNRUN | UNRUN |
| G008 | pii.py:L97 operand->True | UNRUN | UNRUN |
| G009 | pii.py:L97 operand->False | UNRUN | UNRUN |
| G010 | pii.py:L97 operand->True | UNRUN | UNRUN |
| G011 | pii.py:L97 operand->False | UNRUN | UNRUN |
| G012 | pii.py:L97 operand->True | UNRUN | UNRUN |
| G013 | pii.py:L97 operand->False | UNRUN | UNRUN |
| G014 | pii.py:L97 cmp->not in | UNRUN | UNRUN |
| G015 | pii.py:L97 const 1->2 | UNRUN | UNRUN |
| G016 | pii.py:L97 const 1->0 | UNRUN | UNRUN |
| G017 | pii.py:L99 cond->True | UNRUN | UNRUN |
| G018 | pii.py:L99 cond->False | UNRUN | UNRUN |
| G019 | pii.py:L99 cmp->not in | UNRUN | UNRUN |
| G020 | pii.py:L100 cond->True | UNRUN | UNRUN |
| G021 | pii.py:L100 cond->False | UNRUN | UNRUN |
| G022 | pii.py:L101 stmt->pass | UNRUN | UNRUN |
| G023 | pii.py:L103 stmt->pass | UNRUN | UNRUN |
| G024 | pii.py:L103 ifexp->True | UNRUN | UNRUN |
| G025 | pii.py:L103 ifexp->False | UNRUN | UNRUN |
| G026 | pii.py:L103 and->or | UNRUN | UNRUN |
| G027 | pii.py:L103 operand->True | UNRUN | UNRUN |
| G028 | pii.py:L103 operand->False | UNRUN | UNRUN |
| G029 | pii.py:L103 operand->True | UNRUN | UNRUN |
| G030 | pii.py:L103 operand->False | UNRUN | UNRUN |
| G031 | pii.py:L103 cmp-><= | UNRUN | UNRUN |
| G032 | pii.py:L103 cmp boundary | UNRUN | UNRUN |
| G033 | pii.py:L103 cmp->!= | UNRUN | UNRUN |
| G034 | pii.py:L104 return->None | UNRUN | UNRUN |
| G035 | pii.py:L104 return->True | UNRUN | UNRUN |
| G036 | pii.py:L104 return->False | UNRUN | UNRUN |
| G037 | pii.py:L104 return->"" | UNRUN | UNRUN |
| G038 | pii.py:L104 return->b"" | UNRUN | UNRUN |
| G039 | pii.py:L104 return->"x" | UNRUN | UNRUN |
| G040 | pii.py:L104 call join->none/empty | UNRUN | UNRUN |
| G041 | pii.py:L112 return->None | UNRUN | UNRUN |
| G042 | pii.py:L112 return->True | UNRUN | UNRUN |
| G043 | pii.py:L112 return->False | UNRUN | UNRUN |
| G044 | pii.py:L112 return->"" | UNRUN | UNRUN |
| G045 | pii.py:L112 return->b"" | UNRUN | UNRUN |
| G046 | pii.py:L112 return->"x" | UNRUN | UNRUN |
| G047 | pii.py:L112 call join->none/empty | UNRUN | UNRUN |
| G048 | pii.py:L112 call join->none/empty | UNRUN | UNRUN |
| G049 | pii.py:L112 ifexp->True | UNRUN | UNRUN |
| G050 | pii.py:L112 ifexp->False | UNRUN | UNRUN |
| G051 | pii.py:L112 or->and | UNRUN | UNRUN |
| G052 | pii.py:L112 operand->True | UNRUN | UNRUN |
| G053 | pii.py:L112 operand->False | UNRUN | UNRUN |
| G054 | pii.py:L112 operand->True | UNRUN | UNRUN |
| G055 | pii.py:L112 operand->False | UNRUN | UNRUN |
| G056 | pii.py:L112 cmp->!= | UNRUN | UNRUN |
| G057 | pii.py:L118 stmt->pass | UNRUN | UNRUN |
| G058 | pii.py:L211 return->None | UNRUN | UNRUN |
| G059 | pii.py:L211 return->True | UNRUN | UNRUN |
| G060 | pii.py:L211 return->False | UNRUN | UNRUN |
| G061 | pii.py:L211 return->"" | UNRUN | UNRUN |
| G062 | pii.py:L211 return->b"" | UNRUN | UNRUN |
| G063 | pii.py:L211 return->"x" | UNRUN | UNRUN |
| G064 | pii.py:L211 binop->- | UNRUN | UNRUN |
| G065 | pii.py:L211 binop->- | UNRUN | UNRUN |
| G066 | pii.py:L216 stmt->pass | UNRUN | UNRUN |
| G067 | pii.py:L217 cond->True | UNRUN | UNRUN |
| G068 | pii.py:L217 cond->False | UNRUN | UNRUN |
| G069 | pii.py:L218 return->None | UNRUN | UNRUN |
| G070 | pii.py:L218 return->True | UNRUN | UNRUN |
| G071 | pii.py:L218 return->False | UNRUN | UNRUN |
| G072 | pii.py:L218 return->"" | UNRUN | UNRUN |
| G073 | pii.py:L218 return->b"" | UNRUN | UNRUN |
| G074 | pii.py:L218 return->"x" | UNRUN | UNRUN |
| G075 | pii.py:L219 stmt->pass | UNRUN | UNRUN |
| G076 | pii.py:L220 return->None | UNRUN | UNRUN |
| G077 | pii.py:L220 return->True | UNRUN | UNRUN |
| G078 | pii.py:L220 return->False | UNRUN | UNRUN |
| G079 | pii.py:L220 return->"" | UNRUN | UNRUN |
| G080 | pii.py:L220 return->b"" | UNRUN | UNRUN |
| G081 | pii.py:L220 return->"x" | UNRUN | UNRUN |
| G082 | pii.py:L220 ifexp->True | UNRUN | UNRUN |
| G083 | pii.py:L220 ifexp->False | UNRUN | UNRUN |
| G084 | pii.py:L245 const 9->10 | UNRUN | UNRUN |
| G085 | pii.py:L245 const 9->8 | UNRUN | UNRUN |
| G086 | pii.py:L248 stmt->pass | UNRUN | UNRUN |
| G087 | pii.py:L255 stmt->pass | UNRUN | UNRUN |
| G088 | pii.py:L255 const 0->1 | UNRUN | UNRUN |
| G089 | pii.py:L255 const 0->-1 | UNRUN | UNRUN |
| G090 | pii.py:L256 binop->- | UNRUN | UNRUN |
| G091 | pii.py:L257 cond->True | UNRUN | UNRUN |
| G092 | pii.py:L257 cond->False | UNRUN | UNRUN |
| G093 | pii.py:L257 cmp->not in | UNRUN | UNRUN |
| G094 | pii.py:L258 cond->True | UNRUN | UNRUN |
| G095 | pii.py:L258 cond->False | UNRUN | UNRUN |
| G096 | pii.py:L258 cmp->< | UNRUN | UNRUN |
| G097 | pii.py:L259 stmt->pass | UNRUN | UNRUN |
| G098 | pii.py:L259 binop->- | UNRUN | UNRUN |
| G099 | pii.py:L259 const 1->2 | UNRUN | UNRUN |
| G100 | pii.py:L259 const 1->0 | UNRUN | UNRUN |
| G101 | pii.py:L260 stmt->pass | UNRUN | UNRUN |
| G102 | pii.py:L260 const 0->1 | UNRUN | UNRUN |
| G103 | pii.py:L260 const 0->-1 | UNRUN | UNRUN |
| G104 | pii.py:L261 cond->True | UNRUN | UNRUN |
| G105 | pii.py:L261 cond->False | UNRUN | UNRUN |
| G106 | pii.py:L262 stmt->pass | UNRUN | UNRUN |
| G107 | pii.py:L262 ifexp->True | UNRUN | UNRUN |
| G108 | pii.py:L262 ifexp->False | UNRUN | UNRUN |
| G109 | pii.py:L262 cmp->is not | UNRUN | UNRUN |
| G110 | pii.py:L263 stmt->pass | UNRUN | UNRUN |
| G111 | pii.py:L263 binop->- | UNRUN | UNRUN |
| G112 | pii.py:L263 const 1->2 | UNRUN | UNRUN |
| G113 | pii.py:L263 const 1->0 | UNRUN | UNRUN |
| G114 | pii.py:L264 return->None | UNRUN | UNRUN |
| G115 | pii.py:L264 return->True | UNRUN | UNRUN |
| G116 | pii.py:L264 return->False | UNRUN | UNRUN |
| G117 | pii.py:L264 return->"" | UNRUN | UNRUN |
| G118 | pii.py:L264 return->b"" | UNRUN | UNRUN |
| G119 | pii.py:L264 return->"x" | UNRUN | UNRUN |
| G120 | pii.py:L268 cond->True | UNRUN | UNRUN |
| G121 | pii.py:L268 cond->False | UNRUN | UNRUN |
| G122 | pii.py:L268 or->and | UNRUN | UNRUN |
| G123 | pii.py:L268 operand->True | UNRUN | UNRUN |
| G124 | pii.py:L268 operand->False | UNRUN | UNRUN |
| G125 | pii.py:L268 operand->True | UNRUN | UNRUN |
| G126 | pii.py:L268 operand->False | UNRUN | UNRUN |
| G127 | pii.py:L268 cmp->not in | UNRUN | UNRUN |
| G128 | pii.py:L269 return->None | UNRUN | UNRUN |
| G129 | pii.py:L269 return->True | UNRUN | UNRUN |
| G130 | pii.py:L269 return->False | UNRUN | UNRUN |
| G131 | pii.py:L269 return->"" | UNRUN | UNRUN |
| G132 | pii.py:L269 return->b"" | UNRUN | UNRUN |
| G133 | pii.py:L269 return->"x" | UNRUN | UNRUN |
| G134 | pii.py:L270 cond->True | UNRUN | UNRUN |
| G135 | pii.py:L270 cond->False | UNRUN | UNRUN |
| G136 | pii.py:L271 return->None | UNRUN | UNRUN |
| G137 | pii.py:L271 return->True | UNRUN | UNRUN |
| G138 | pii.py:L271 return->False | UNRUN | UNRUN |
| G139 | pii.py:L271 return->"" | UNRUN | UNRUN |
| G140 | pii.py:L271 return->b"" | UNRUN | UNRUN |
| G141 | pii.py:L271 return->"x" | UNRUN | UNRUN |
| G142 | pii.py:L272 cond->True | UNRUN | UNRUN |
| G143 | pii.py:L272 cond->False | UNRUN | UNRUN |
| G144 | pii.py:L273 return->None | UNRUN | UNRUN |
| G145 | pii.py:L273 return->True | UNRUN | UNRUN |
| G146 | pii.py:L273 return->False | UNRUN | UNRUN |
| G147 | pii.py:L273 return->"" | UNRUN | UNRUN |
| G148 | pii.py:L273 return->b"" | UNRUN | UNRUN |
| G149 | pii.py:L273 return->"x" | UNRUN | UNRUN |
| G150 | pii.py:L274 cond->True | UNRUN | UNRUN |
| G151 | pii.py:L274 cond->False | UNRUN | UNRUN |
| G152 | pii.py:L274 or->and | UNRUN | UNRUN |
| G153 | pii.py:L274 operand->True | UNRUN | UNRUN |
| G154 | pii.py:L274 operand->False | UNRUN | UNRUN |
| G155 | pii.py:L274 operand->True | UNRUN | UNRUN |
| G156 | pii.py:L274 operand->False | UNRUN | UNRUN |
| G157 | pii.py:L275 return->None | UNRUN | UNRUN |
| G158 | pii.py:L275 return->True | UNRUN | UNRUN |
| G159 | pii.py:L275 return->False | UNRUN | UNRUN |
| G160 | pii.py:L275 return->"" | UNRUN | UNRUN |
| G161 | pii.py:L275 return->b"" | UNRUN | UNRUN |
| G162 | pii.py:L275 return->"x" | UNRUN | UNRUN |
| G163 | pii.py:L276 return->None | UNRUN | UNRUN |
| G164 | pii.py:L276 return->True | UNRUN | UNRUN |
| G165 | pii.py:L276 return->False | UNRUN | UNRUN |
| G166 | pii.py:L276 return->"" | UNRUN | UNRUN |
| G167 | pii.py:L276 return->b"" | UNRUN | UNRUN |
| G168 | pii.py:L276 return->"x" | UNRUN | UNRUN |
| G169 | pii.py:L280 return->None | UNRUN | UNRUN |
| G170 | pii.py:L280 return->True | UNRUN | UNRUN |
| G171 | pii.py:L280 return->False | UNRUN | UNRUN |
| G172 | pii.py:L280 return->"" | UNRUN | UNRUN |
| G173 | pii.py:L280 return->b"" | UNRUN | UNRUN |
| G174 | pii.py:L280 return->"x" | UNRUN | UNRUN |
| G175 | pii.py:L280 binop->- | UNRUN | UNRUN |
| G176 | pii.py:L285 const 4->5 | UNRUN | UNRUN |
| G177 | pii.py:L285 const 4->3 | UNRUN | UNRUN |
| G178 | pii.py:L286 stmt->pass | UNRUN | UNRUN |
| G179 | pii.py:L287 cond->True | UNRUN | UNRUN |
| G180 | pii.py:L287 cond->False | UNRUN | UNRUN |
| G181 | pii.py:L287 drop not | UNRUN | UNRUN |
| G182 | pii.py:L288 return->None | UNRUN | UNRUN |
| G183 | pii.py:L288 return->True | UNRUN | UNRUN |
| G184 | pii.py:L288 return->False | UNRUN | UNRUN |
| G185 | pii.py:L288 return->"" | UNRUN | UNRUN |
| G186 | pii.py:L288 return->b"" | UNRUN | UNRUN |
| G187 | pii.py:L288 return->"x" | UNRUN | UNRUN |
| G188 | pii.py:L289 stmt->pass | UNRUN | UNRUN |
| G189 | pii.py:L289 const 0->1 | UNRUN | UNRUN |
| G190 | pii.py:L289 const 0->-1 | UNRUN | UNRUN |
| G191 | pii.py:L291 cond->True | UNRUN | UNRUN |
| G192 | pii.py:L291 cond->False | UNRUN | UNRUN |
| G193 | pii.py:L291 cmp->> | UNRUN | UNRUN |
| G194 | pii.py:L293 stmt->pass | UNRUN | UNRUN |
| G195 | pii.py:L294 stmt->pass | UNRUN | UNRUN |
| G196 | pii.py:L295 stmt->pass | UNRUN | UNRUN |
| G197 | pii.py:L296 stmt->pass | UNRUN | UNRUN |
| G198 | pii.py:L297 stmt->pass | UNRUN | UNRUN |
| G199 | pii.py:L297 binop->- | UNRUN | UNRUN |
| G200 | pii.py:L297 call join->none/empty | UNRUN | UNRUN |
| G201 | pii.py:L298 return->None | UNRUN | UNRUN |
| G202 | pii.py:L298 return->True | UNRUN | UNRUN |
| G203 | pii.py:L298 return->False | UNRUN | UNRUN |
| G204 | pii.py:L298 return->"" | UNRUN | UNRUN |
| G205 | pii.py:L298 return->b"" | UNRUN | UNRUN |
| G206 | pii.py:L298 return->"x" | UNRUN | UNRUN |
| G207 | pii.py:L298 ifexp->True | UNRUN | UNRUN |
| G208 | pii.py:L298 ifexp->False | UNRUN | UNRUN |
| G209 | pii.py:L307 stmt->pass | UNRUN | UNRUN |
| G210 | pii.py:L308 stmt->pass | UNRUN | UNRUN |
| G211 | pii.py:L309 stmt->pass | UNRUN | UNRUN |
| G212 | pii.py:L313 stmt->pass | UNRUN | UNRUN |
| G213 | pii.py:L314 const 3->4 | UNRUN | UNRUN |
| G214 | pii.py:L314 const 3->2 | UNRUN | UNRUN |
| G215 | pii.py:L315 stmt->pass | UNRUN | UNRUN |
| G216 | pii.py:L316 cond->True | UNRUN | UNRUN |
| G217 | pii.py:L316 cond->False | UNRUN | UNRUN |
| G218 | pii.py:L316 cmp->!= | UNRUN | UNRUN |
| G219 | pii.py:L317 break->pass | UNRUN | UNRUN |
| G220 | pii.py:L318 stmt->pass | UNRUN | UNRUN |
| G221 | pii.py:L319 cond->True | UNRUN | UNRUN |
| G222 | pii.py:L319 cond->False | UNRUN | UNRUN |
| G223 | pii.py:L319 and->or | UNRUN | UNRUN |
| G224 | pii.py:L319 operand->True | UNRUN | UNRUN |
| G225 | pii.py:L319 operand->False | UNRUN | UNRUN |
| G226 | pii.py:L319 operand->True | UNRUN | UNRUN |
| G227 | pii.py:L319 operand->False | UNRUN | UNRUN |
| G228 | pii.py:L319 cmp->== | UNRUN | UNRUN |
| G229 | pii.py:L320 stmt->pass | UNRUN | UNRUN |
| G230 | pii.py:L321 cond->True | UNRUN | UNRUN |
| G231 | pii.py:L321 cond->False | UNRUN | UNRUN |
| G232 | pii.py:L321 drop not | UNRUN | UNRUN |
| G233 | pii.py:L322 return->None | UNRUN | UNRUN |
| G234 | pii.py:L322 return->True | UNRUN | UNRUN |
| G235 | pii.py:L322 return->False | UNRUN | UNRUN |
| G236 | pii.py:L322 return->"" | UNRUN | UNRUN |
| G237 | pii.py:L322 return->b"" | UNRUN | UNRUN |
| G238 | pii.py:L322 return->"x" | UNRUN | UNRUN |
| G239 | pii.py:L323 return->None | UNRUN | UNRUN |
| G240 | pii.py:L323 return->True | UNRUN | UNRUN |
| G241 | pii.py:L323 return->False | UNRUN | UNRUN |
| G242 | pii.py:L323 return->"" | UNRUN | UNRUN |
| G243 | pii.py:L323 return->b"" | UNRUN | UNRUN |
| G244 | pii.py:L323 return->"x" | UNRUN | UNRUN |
| G245 | pii.py:L323 call join->none/empty | UNRUN | UNRUN |
| G246 | pii.py:L327 return->None | UNRUN | UNRUN |
| G247 | pii.py:L327 return->True | UNRUN | UNRUN |
| G248 | pii.py:L327 return->False | UNRUN | UNRUN |
| G249 | pii.py:L327 return->"" | UNRUN | UNRUN |
| G250 | pii.py:L327 return->b"" | UNRUN | UNRUN |
| G251 | pii.py:L327 return->"x" | UNRUN | UNRUN |
| G252 | pii.py:L327 const 1->2 | UNRUN | UNRUN |
| G253 | pii.py:L327 const 1->0 | UNRUN | UNRUN |
| G254 | pii.py:L333 stmt->pass | UNRUN | UNRUN |
| G255 | store.py:L64 stmt->pass | UNRUN | UNRUN |
| G256 | store.py:L64 call close->none/empty | UNRUN | UNRUN |
| G257 | store.py:L125 stmt->pass | UNRUN | UNRUN |
| G258 | store.py:L125 const 448->449 | UNRUN | UNRUN |
| G259 | store.py:L125 const 448->447 | UNRUN | UNRUN |
| G260 | store.py:L125 bool flip | UNRUN | UNRUN |
| G261 | store.py:L126 stmt->pass | UNRUN | UNRUN |
| G262 | store.py:L126 call join->none/empty | UNRUN | UNRUN |
| G263 | store.py:L127 stmt->pass | UNRUN | UNRUN |
| G264 | store.py:L128 cond->True | UNRUN | UNRUN |
| G265 | store.py:L128 cond->False | UNRUN | UNRUN |
| G266 | store.py:L128 cmp->is | UNRUN | UNRUN |
| G267 | store.py:L129 cond->True | UNRUN | UNRUN |
| G268 | store.py:L129 cond->False | UNRUN | UNRUN |
| G269 | store.py:L129 and->or | UNRUN | UNRUN |
| G270 | store.py:L129 operand->True | UNRUN | UNRUN |
| G271 | store.py:L129 operand->False | UNRUN | UNRUN |
| G272 | store.py:L129 operand->True | UNRUN | UNRUN |
| G273 | store.py:L129 operand->False | UNRUN | UNRUN |
| G274 | store.py:L129 drop not | UNRUN | UNRUN |
| G275 | store.py:L129 bytes const->empty | UNRUN | UNRUN |
| G276 | store.py:L129 bytes const->x | UNRUN | UNRUN |
| G277 | store.py:L130 raise->pass | UNRUN | UNRUN |
| G278 | store.py:L136 stmt->pass | UNRUN | UNRUN |
| G279 | store.py:L142 stmt->pass | UNRUN | UNRUN |
| G280 | store.py:L144 return->True | UNRUN | UNRUN |
| G281 | store.py:L144 return->False | UNRUN | UNRUN |
| G282 | store.py:L144 return->"" | UNRUN | UNRUN |
| G283 | store.py:L144 return->b"" | UNRUN | UNRUN |
| G284 | store.py:L144 return->"x" | UNRUN | UNRUN |
| G285 | store.py:L146 stmt->pass | UNRUN | UNRUN |
| G286 | store.py:L146 ifexp->True | UNRUN | UNRUN |
| G287 | store.py:L146 ifexp->False | UNRUN | UNRUN |
| G288 | store.py:L146 cmp->!= | UNRUN | UNRUN |
| G289 | store.py:L147 raise->pass | UNRUN | UNRUN |
| G290 | store.py:L149 stmt->pass | UNRUN | UNRUN |
| G291 | store.py:L150 cond->True | UNRUN | UNRUN |
| G292 | store.py:L150 cond->False | UNRUN | UNRUN |
| G293 | store.py:L150 drop not | UNRUN | UNRUN |
| G294 | store.py:L151 raise->pass | UNRUN | UNRUN |
| G295 | store.py:L152 return->None | UNRUN | UNRUN |
| G296 | store.py:L152 return->True | UNRUN | UNRUN |
| G297 | store.py:L152 return->False | UNRUN | UNRUN |
| G298 | store.py:L152 return->"" | UNRUN | UNRUN |
| G299 | store.py:L152 return->b"" | UNRUN | UNRUN |
| G300 | store.py:L152 return->"x" | UNRUN | UNRUN |
| G301 | store.py:L152 _pread_exact->raw pread | UNRUN | UNRUN |
| G302 | store.py:L152 n-1 | UNRUN | UNRUN |
| G303 | store.py:L152 const 0->1 | UNRUN | UNRUN |
| G304 | store.py:L152 const 0->-1 | UNRUN | UNRUN |
| G305 | store.py:L154 raise->pass | UNRUN | UNRUN |
| G306 | store.py:L156 stmt->pass | UNRUN | UNRUN |
| G307 | store.py:L226 stmt->pass | UNRUN | UNRUN |
| G308 | store.py:L228 stmt->pass | UNRUN | UNRUN |
| G309 | store.py:L228 const 384->385 | UNRUN | UNRUN |
| G310 | store.py:L228 const 384->383 | UNRUN | UNRUN |
| G311 | store.py:L230 raise->pass | UNRUN | UNRUN |
| G312 | store.py:L232 stmt->pass | UNRUN | UNRUN |
| G313 | store.py:L233 cond->True | UNRUN | UNRUN |
| G314 | store.py:L233 cond->False | UNRUN | UNRUN |
| G315 | store.py:L233 drop not | UNRUN | UNRUN |
| G316 | store.py:L234 raise->pass | UNRUN | UNRUN |
| G317 | store.py:L235 stmt->pass | UNRUN | UNRUN |
| G318 | store.py:L236 stmt->pass | UNRUN | UNRUN |
| G319 | store.py:L236 cmp->!= | UNRUN | UNRUN |
| G320 | store.py:L236 binop->- | UNRUN | UNRUN |
| G321 | store.py:L236 binop->- | UNRUN | UNRUN |
| G322 | store.py:L236 const 1->2 | UNRUN | UNRUN |
| G323 | store.py:L236 const 1->0 | UNRUN | UNRUN |
| G324 | store.py:L264 raise->pass | UNRUN | UNRUN |
| G325 | store.py:L266 stmt->pass | UNRUN | UNRUN |
| G326 | store.py:L267 stmt->pass | UNRUN | UNRUN |
| G327 | store.py:L268 return->None | UNRUN | UNRUN |
| G328 | store.py:L268 return->True | UNRUN | UNRUN |
| G329 | store.py:L268 return->False | UNRUN | UNRUN |
| G330 | store.py:L268 return->"" | UNRUN | UNRUN |
| G331 | store.py:L268 return->b"" | UNRUN | UNRUN |
| G332 | store.py:L268 return->"x" | UNRUN | UNRUN |
| G333 | store.py:L274 cond->True | UNRUN | UNRUN |
| G334 | store.py:L274 cond->False | UNRUN | UNRUN |
| G335 | store.py:L274 drop not | UNRUN | UNRUN |
| G336 | store.py:L275 return->True | UNRUN | UNRUN |
| G337 | store.py:L275 return->False | UNRUN | UNRUN |
| G338 | store.py:L275 return->"" | UNRUN | UNRUN |
| G339 | store.py:L275 return->b"" | UNRUN | UNRUN |
| G340 | store.py:L275 return->"x" | UNRUN | UNRUN |
| G341 | store.py:L276 stmt->pass | UNRUN | UNRUN |
| G342 | store.py:L281 stmt->pass | UNRUN | UNRUN |
| G343 | store.py:L283 return->None | UNRUN | UNRUN |
| G344 | store.py:L283 return->True | UNRUN | UNRUN |
| G345 | store.py:L283 return->False | UNRUN | UNRUN |
| G346 | store.py:L283 return->"" | UNRUN | UNRUN |
| G347 | store.py:L283 return->b"" | UNRUN | UNRUN |
| G348 | store.py:L283 return->"x" | UNRUN | UNRUN |
| G349 | store.py:L283 ifexp->True | UNRUN | UNRUN |
| G350 | store.py:L283 ifexp->False | UNRUN | UNRUN |
| G351 | store.py:L285 return->None | UNRUN | UNRUN |
| G352 | store.py:L285 return->True | UNRUN | UNRUN |
| G353 | store.py:L285 return->False | UNRUN | UNRUN |
| G354 | store.py:L285 return->"" | UNRUN | UNRUN |
| G355 | store.py:L285 return->b"" | UNRUN | UNRUN |
| G356 | store.py:L285 return->"x" | UNRUN | UNRUN |
| G357 | store.py:L287 stmt->pass | UNRUN | UNRUN |
| G358 | store.py:L288 cond->True | UNRUN | UNRUN |
| G359 | store.py:L288 cond->False | UNRUN | UNRUN |
| G360 | store.py:L288 operand->True | UNRUN | UNRUN |
| G361 | store.py:L288 operand->False | UNRUN | UNRUN |
| G362 | store.py:L288 operand->True | UNRUN | UNRUN |
| G363 | store.py:L288 operand->False | UNRUN | UNRUN |
| G364 | store.py:L288 operand->True | UNRUN | UNRUN |
| G365 | store.py:L288 operand->False | UNRUN | UNRUN |
| G366 | store.py:L288 drop not | UNRUN | UNRUN |
| G367 | store.py:L288 cmp->== | UNRUN | UNRUN |
| G368 | store.py:L288 cmp->== | UNRUN | UNRUN |
| G369 | store.py:L288 _pread_exact->raw pread | UNRUN | UNRUN |
| G370 | store.py:L288 n-1 | UNRUN | UNRUN |
| G371 | store.py:L288 const 0->1 | UNRUN | UNRUN |
| G372 | store.py:L288 const 0->-1 | UNRUN | UNRUN |
| G373 | store.py:L289 return->None | UNRUN | UNRUN |
| G374 | store.py:L289 return->True | UNRUN | UNRUN |
| G375 | store.py:L289 return->False | UNRUN | UNRUN |
| G376 | store.py:L289 return->"" | UNRUN | UNRUN |
| G377 | store.py:L289 return->b"" | UNRUN | UNRUN |
| G378 | store.py:L289 return->"x" | UNRUN | UNRUN |
| G379 | store.py:L290 return->True | UNRUN | UNRUN |
| G380 | store.py:L290 return->False | UNRUN | UNRUN |
| G381 | store.py:L290 return->"" | UNRUN | UNRUN |
| G382 | store.py:L290 return->b"" | UNRUN | UNRUN |
| G383 | store.py:L290 return->"x" | UNRUN | UNRUN |
| G384 | store.py:L292 return->None | UNRUN | UNRUN |
| G385 | store.py:L292 return->True | UNRUN | UNRUN |
| G386 | store.py:L292 return->False | UNRUN | UNRUN |
| G387 | store.py:L292 return->"" | UNRUN | UNRUN |
| G388 | store.py:L292 return->b"" | UNRUN | UNRUN |
| G389 | store.py:L292 return->"x" | UNRUN | UNRUN |
| G390 | store.py:L294 stmt->pass | UNRUN | UNRUN |
| G391 | service.py:L76 cond->True | UNRUN | UNRUN |
| G392 | service.py:L76 cond->False | UNRUN | UNRUN |
| G393 | service.py:L77 return->None | UNRUN | UNRUN |
| G394 | service.py:L77 return->True | UNRUN | UNRUN |
| G395 | service.py:L77 return->False | UNRUN | UNRUN |
| G396 | service.py:L77 return->"" | UNRUN | UNRUN |
| G397 | service.py:L77 return->b"" | UNRUN | UNRUN |
| G398 | service.py:L77 return->"x" | UNRUN | UNRUN |
| G399 | service.py:L77 ifexp->True | UNRUN | UNRUN |
| G400 | service.py:L77 ifexp->False | UNRUN | UNRUN |
| G401 | service.py:L77 and->or | UNRUN | UNRUN |
| G402 | service.py:L77 operand->True | UNRUN | UNRUN |
| G403 | service.py:L77 operand->False | UNRUN | UNRUN |
| G404 | service.py:L77 operand->True | UNRUN | UNRUN |
| G405 | service.py:L77 operand->False | UNRUN | UNRUN |
| G406 | service.py:L77 cmp->!= | UNRUN | UNRUN |
| G407 | service.py:L77 const 0->1 | UNRUN | UNRUN |
| G408 | service.py:L77 const 0->-1 | UNRUN | UNRUN |
| G409 | service.py:L79 cond->True | UNRUN | UNRUN |
| G410 | service.py:L79 cond->False | UNRUN | UNRUN |
| G411 | service.py:L80 return->None | UNRUN | UNRUN |
| G412 | service.py:L80 return->True | UNRUN | UNRUN |
| G413 | service.py:L80 return->False | UNRUN | UNRUN |
| G414 | service.py:L80 return->"" | UNRUN | UNRUN |
| G415 | service.py:L80 return->b"" | UNRUN | UNRUN |
| G416 | service.py:L80 return->"x" | UNRUN | UNRUN |
| G417 | service.py:L81 return->None | UNRUN | UNRUN |
| G418 | service.py:L81 return->True | UNRUN | UNRUN |
| G419 | service.py:L81 return->False | UNRUN | UNRUN |
| G420 | service.py:L81 return->"" | UNRUN | UNRUN |
| G421 | service.py:L81 return->b"" | UNRUN | UNRUN |
| G422 | service.py:L81 return->"x" | UNRUN | UNRUN |
| G423 | service.py:L84 stmt->pass | UNRUN | UNRUN |
| G424 | service.py:L94 stmt->pass | UNRUN | UNRUN |
| G425 | service.py:L96 stmt->pass | UNRUN | UNRUN |
| G426 | service.py:L97 cond->True | UNRUN | UNRUN |
| G427 | service.py:L97 cond->False | UNRUN | UNRUN |
| G428 | service.py:L97 cmp->is | UNRUN | UNRUN |
| G429 | service.py:L98 stmt->pass | UNRUN | UNRUN |
| G430 | service.py:L99 return->None | UNRUN | UNRUN |
| G431 | service.py:L99 return->True | UNRUN | UNRUN |
| G432 | service.py:L99 return->False | UNRUN | UNRUN |
| G433 | service.py:L99 return->"" | UNRUN | UNRUN |
| G434 | service.py:L99 return->b"" | UNRUN | UNRUN |
| G435 | service.py:L99 return->"x" | UNRUN | UNRUN |
| G436 | service.py:L100 stmt->pass | UNRUN | UNRUN |
| G437 | service.py:L102 stmt->pass | UNRUN | UNRUN |
| G438 | service.py:L103 cond->True | UNRUN | UNRUN |
| G439 | service.py:L103 cond->False | UNRUN | UNRUN |
| G440 | service.py:L103 cmp-><= | UNRUN | UNRUN |
| G441 | service.py:L103 cmp boundary | UNRUN | UNRUN |
| G442 | service.py:L103 const 64->65 | UNRUN | UNRUN |
| G443 | service.py:L103 const 64->63 | UNRUN | UNRUN |
| G444 | service.py:L104 stmt->pass | UNRUN | UNRUN |
| G445 | service.py:L104 bool flip | UNRUN | UNRUN |
| G446 | service.py:L105 return->None | UNRUN | UNRUN |
| G447 | service.py:L105 return->True | UNRUN | UNRUN |
| G448 | service.py:L105 return->False | UNRUN | UNRUN |
| G449 | service.py:L105 return->"" | UNRUN | UNRUN |
| G450 | service.py:L105 return->b"" | UNRUN | UNRUN |
| G451 | service.py:L105 return->"x" | UNRUN | UNRUN |
| G452 | svc_campaigns.py:L62 cond->True | UNRUN | UNRUN |
| G453 | svc_campaigns.py:L62 cond->False | UNRUN | UNRUN |
| G454 | svc_campaigns.py:L62 cmp->is not | UNRUN | UNRUN |
| G455 | svc_campaigns.py:L63 return->True | UNRUN | UNRUN |
| G456 | svc_campaigns.py:L63 return->False | UNRUN | UNRUN |
| G457 | svc_campaigns.py:L63 return->"" | UNRUN | UNRUN |
| G458 | svc_campaigns.py:L63 return->b"" | UNRUN | UNRUN |
| G459 | svc_campaigns.py:L63 return->"x" | UNRUN | UNRUN |
| G460 | svc_campaigns.py:L64 stmt->pass | UNRUN | UNRUN |
| G461 | svc_campaigns.py:L65 stmt->pass | UNRUN | UNRUN |
| G462 | svc_campaigns.py:L66 return->None | UNRUN | UNRUN |
| G463 | svc_campaigns.py:L66 return->True | UNRUN | UNRUN |
| G464 | svc_campaigns.py:L66 return->False | UNRUN | UNRUN |
| G465 | svc_campaigns.py:L66 return->"" | UNRUN | UNRUN |
| G466 | svc_campaigns.py:L66 return->b"" | UNRUN | UNRUN |
| G467 | svc_campaigns.py:L66 return->"x" | UNRUN | UNRUN |
| G468 | svc_campaigns.py:L66 bool flip | UNRUN | UNRUN |
| G469 | svc_campaigns.py:L67 cmp-><= | UNRUN | UNRUN |
| G470 | svc_campaigns.py:L67 cmp boundary | UNRUN | UNRUN |
| G471 | svc_campaigns.py:L145 stmt->pass | UNRUN | UNRUN |
| G472 | svc_campaigns.py:L146 stmt->pass | UNRUN | UNRUN |
| G473 | svc_campaigns.py:L165 cond->True | UNRUN | UNRUN |
| G474 | svc_campaigns.py:L165 cond->False | UNRUN | UNRUN |
| G475 | svc_campaigns.py:L165 or->and | UNRUN | UNRUN |
| G476 | svc_campaigns.py:L165 operand->True | UNRUN | UNRUN |
| G477 | svc_campaigns.py:L165 operand->False | UNRUN | UNRUN |
| G478 | svc_campaigns.py:L165 operand->True | UNRUN | UNRUN |
| G479 | svc_campaigns.py:L165 operand->False | UNRUN | UNRUN |
| G480 | svc_campaigns.py:L165 cmp->!= | UNRUN | UNRUN |
| G481 | svc_campaigns.py:L165 and->or | UNRUN | UNRUN |
| G482 | svc_campaigns.py:L165 operand->True | UNRUN | UNRUN |
| G483 | svc_campaigns.py:L165 operand->False | UNRUN | UNRUN |
| G484 | svc_campaigns.py:L165 operand->True | UNRUN | UNRUN |
| G485 | svc_campaigns.py:L165 operand->False | UNRUN | UNRUN |
| G486 | svc_campaigns.py:L166 return->None | UNRUN | UNRUN |
| G487 | svc_campaigns.py:L166 return->False | UNRUN | UNRUN |
| G488 | svc_campaigns.py:L166 return->"" | UNRUN | UNRUN |
| G489 | svc_campaigns.py:L166 return->b"" | UNRUN | UNRUN |
| G490 | svc_campaigns.py:L166 return->"x" | UNRUN | UNRUN |
| G491 | svc_campaigns.py:L166 bool flip | UNRUN | UNRUN |
| G492 | svc_campaigns.py:L169 return->None | UNRUN | UNRUN |
| G493 | svc_campaigns.py:L169 return->True | UNRUN | UNRUN |
| G494 | svc_campaigns.py:L169 return->False | UNRUN | UNRUN |
| G495 | svc_campaigns.py:L169 return->"" | UNRUN | UNRUN |
| G496 | svc_campaigns.py:L169 return->b"" | UNRUN | UNRUN |
| G497 | svc_campaigns.py:L169 return->"x" | UNRUN | UNRUN |
| G498 | svc_campaigns.py:L861 stmt->pass | UNRUN | UNRUN |
| G499 | svc_campaigns.py:L862 stmt->pass | UNRUN | UNRUN |
| G500 | svc_campaigns.py:L863 cond->True | UNRUN | UNRUN |
| G501 | svc_campaigns.py:L863 cond->False | UNRUN | UNRUN |
| G502 | svc_campaigns.py:L864 raise->pass | UNRUN | UNRUN |
| G503 | svc_campaigns.py:L865 stmt->pass | UNRUN | UNRUN |
| G504 | svc_inventory.py:L72 return->None | UNRUN | UNRUN |
| G505 | svc_inventory.py:L72 return->True | UNRUN | UNRUN |
| G506 | svc_inventory.py:L72 return->False | UNRUN | UNRUN |
| G507 | svc_inventory.py:L72 return->"" | UNRUN | UNRUN |
| G508 | svc_inventory.py:L72 return->b"" | UNRUN | UNRUN |
| G509 | svc_inventory.py:L72 return->"x" | UNRUN | UNRUN |
| G510 | svc_inventory.py:L73 cond->True | UNRUN | UNRUN |
| G511 | svc_inventory.py:L73 cond->False | UNRUN | UNRUN |
| G512 | svc_inventory.py:L73 and->or | UNRUN | UNRUN |
| G513 | svc_inventory.py:L73 operand->True | UNRUN | UNRUN |
| G514 | svc_inventory.py:L73 operand->False | UNRUN | UNRUN |
| G515 | svc_inventory.py:L73 operand->True | UNRUN | UNRUN |
| G516 | svc_inventory.py:L73 operand->False | UNRUN | UNRUN |
| G517 | svc_inventory.py:L73 cmp->!= | UNRUN | UNRUN |
| G518 | svc_inventory.py:L74 raise->pass | UNRUN | UNRUN |
| G519 | svc_inventory.py:L75 cond->True | UNRUN | UNRUN |
| G520 | svc_inventory.py:L75 cond->False | UNRUN | UNRUN |
| G521 | svc_inventory.py:L75 and->or | UNRUN | UNRUN |
| G522 | svc_inventory.py:L75 operand->True | UNRUN | UNRUN |
| G523 | svc_inventory.py:L75 operand->False | UNRUN | UNRUN |
| G524 | svc_inventory.py:L75 operand->True | UNRUN | UNRUN |
| G525 | svc_inventory.py:L75 operand->False | UNRUN | UNRUN |
| G526 | svc_inventory.py:L75 cmp->!= | UNRUN | UNRUN |
| G527 | svc_inventory.py:L76 raise->pass | UNRUN | UNRUN |
