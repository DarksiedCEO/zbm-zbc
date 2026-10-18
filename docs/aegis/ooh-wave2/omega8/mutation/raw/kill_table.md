| id | mutation | killing tests 3.12 / 3.13 | first killing test (3.12) |
|---|---|---|---|
| M001 | store.py:L58 stmt->pass | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M003 | store.py:L59 cond->False | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M004 | store.py:L59 cmp-><= | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M006 | store.py:L59 const 0->1 | 1 / 1 | `test_h1_short_reads_are_completed` (tests/test_aegis_h1_file_content.py) |
| M008 | store.py:L60 stmt->pass | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M009 | store.py:L60 call pread->none/empty | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M010 | store.py:L61 cond->True | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M012 | store.py:L61 drop not | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M014 | store.py:L63 stmt->pass | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M015 | store.py:L64 stmt->pass | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M016 | store.py:L64 augassign flip | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M017 | store.py:L65 stmt->pass | 1 / 1 | `test_h1_short_reads_are_completed` (tests/test_aegis_h1_file_content.py) |
| M018 | store.py:L65 augassign flip | 1 / 1 | `test_h1_short_reads_are_completed` (tests/test_aegis_h1_file_content.py) |
| M019 | store.py:L66 return->None | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M020 | store.py:L66 return->True | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M021 | store.py:L66 return->False | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M022 | store.py:L66 return->"" | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M023 | store.py:L66 return->b"" | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M024 | store.py:L66 return->"x" | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M025 | store.py:L66 call join->none/empty | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M026 | store.py:L66 bytes const->x | 1 / 1 | `test_h1_short_reads_are_completed` (tests/test_aegis_h1_file_content.py) |
| M027 | store.py:L174 return->None | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M028 | store.py:L174 return->True | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M029 | store.py:L174 return->False | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M030 | store.py:L174 return->"" | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M031 | store.py:L174 return->b"" | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M032 | store.py:L174 return->"x" | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M033 | store.py:L174 call join->none/empty | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M034 | store.py:L174 bytes const->x | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M035 | store.py:L174 binop->- | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M036 | store.py:L174 bytes const->empty | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M037 | store.py:L174 bytes const->x | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M038 | store.py:L190 raise->pass | 1 / 1 | `test_t169_failed_cut_back_latches_the_fault_and_every_later_write_refuses` (tests/test_aegis_store_write_path.py) |
| M039 | store.py:L191 cond->True | 582 / 582 | `test_b1_wrong_andre_token_on_buy_limit_price` (tests/test_aegis_omega_red.py) |
| M040 | store.py:L191 cond->False | 57 / 57 | `test_c03_own_pending_line_is_rolled_forward_after_a_lost_answer` (tests/test_aegis_commit_path.py) |
| M041 | store.py:L192 stmt->pass | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M042 | store.py:L193 stmt->pass | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M043 | store.py:L195 stmt->pass | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M045 | store.py:L195 const 384->383 | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M046 | store.py:L197 raise->pass | 1 / 1 | `test_t144_log_open_failure_is_a_clean_refusal` (tests/test_aegis_store_write_path.py) |
| M047 | store.py:L199 stmt->pass | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M048 | store.py:L200 stmt->pass | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M049 | store.py:L200 cmp->!= | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M050 | store.py:L200 binop->- | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M051 | store.py:L200 binop->- | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M052 | store.py:L200 const 1->2 | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M053 | store.py:L200 const 1->0 | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M054 | store.py:L201 cond->True | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M055 | store.py:L201 cond->False | 13 / 13 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M056 | store.py:L201 operand->True | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M057 | store.py:L201 operand->False | 3 / 3 | `test_t149_t153_file_with_this_line_plus_an_extra_tail_is_refused` (tests/test_aegis_store_write_path.py) |
| M058 | store.py:L201 operand->True | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M059 | store.py:L201 operand->False | 5 / 5 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M060 | store.py:L202 operand->True | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M061 | store.py:L202 operand->False | 3 / 3 | `test_h1_adoption_refuses_another_line_of_the_same_size` (tests/test_aegis_h1_file_content.py) |
| M062 | store.py:L201 drop not | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M063 | store.py:L201 or->and | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M064 | store.py:L201 operand->True | 3 / 3 | `test_t149_t153_file_with_this_line_plus_an_extra_tail_is_refused` (tests/test_aegis_store_write_path.py) |
| M065 | store.py:L201 operand->False | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M066 | store.py:L201 operand->True | 3 / 3 | `test_t149_t153_file_with_this_line_plus_an_extra_tail_is_refused` (tests/test_aegis_store_write_path.py) |
| M067 | store.py:L201 operand->False | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M068 | store.py:L201 cmp->!= | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M069 | store.py:L201 cmp->== | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M070 | store.py:L201 _pread_exact->raw pread | 1 / 1 | `test_h1_short_reads_are_completed` (tests/test_aegis_h1_file_content.py) |
| M071 | store.py:L201 n-1 | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M072 | store.py:L201 const 0->1 | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M073 | store.py:L201 const 0->-1 | 105 / 105 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M074 | store.py:L202 and->or | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M075 | store.py:L202 operand->True | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M076 | store.py:L202 operand->False | 3 / 3 | `test_h1_adoption_refuses_another_line_of_the_same_size` (tests/test_aegis_h1_file_content.py) |
| M077 | store.py:L202 operand->True | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M078 | store.py:L202 operand->False | 3 / 3 | `test_h1_adoption_refuses_another_line_of_the_same_size` (tests/test_aegis_h1_file_content.py) |
| M079 | store.py:L202 cmp->== | 5 / 5 | `test_h1_adoption_refuses_another_line_of_the_same_size` (tests/test_aegis_h1_file_content.py) |
| M081 | store.py:L202 offset->0 | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M082 | store.py:L202 n-1 | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M083 | store.py:L202 binop->- | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M085 | store.py:L202 const 1->0 | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M086 | store.py:L202 binop->- | 5 / 5 | `test_h1_adoption_refuses_another_line_of_the_same_size` (tests/test_aegis_h1_file_content.py) |
| M087 | store.py:L202 bytes const->empty | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M088 | store.py:L202 bytes const->x | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| M089 | store.py:L203 raise->pass | 13 / 13 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M090 | store.py:L204 cond->True | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M094 | store.py:L207 stmt->pass | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M095 | store.py:L207 binop->- | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M096 | store.py:L207 bytes const->empty | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M097 | store.py:L207 bytes const->x | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M098 | store.py:L238 cond->True | 3 / 3 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |
| M099 | store.py:L238 cond->False | 600 / 600 | `test_b1_wrong_andre_token_on_buy_limit_price` (tests/test_aegis_omega_red.py) |
| M100 | store.py:L238 drop not | 603 / 603 | `test_b1_wrong_andre_token_on_buy_limit_price` (tests/test_aegis_omega_red.py) |
| M101 | store.py:L239 return->True | 587 / 587 | `test_b1_wrong_andre_token_on_buy_limit_price` (tests/test_aegis_omega_red.py) |
| M102 | store.py:L239 return->False | 587 / 587 | `test_b1_wrong_andre_token_on_buy_limit_price` (tests/test_aegis_omega_red.py) |
| M103 | store.py:L239 return->"" | 587 / 587 | `test_b1_wrong_andre_token_on_buy_limit_price` (tests/test_aegis_omega_red.py) |
| M104 | store.py:L239 return->b"" | 587 / 587 | `test_b1_wrong_andre_token_on_buy_limit_price` (tests/test_aegis_omega_red.py) |
| M105 | store.py:L239 return->"x" | 587 / 587 | `test_b1_wrong_andre_token_on_buy_limit_price` (tests/test_aegis_omega_red.py) |
| M106 | store.py:L242 stmt->pass | 38 / 38 | `test_c03_own_pending_line_is_rolled_forward_after_a_lost_answer` (tests/test_aegis_commit_path.py) |
| M107 | store.py:L242 call read->none/empty | 30 / 30 | `test_c03_own_pending_line_is_rolled_forward_after_a_lost_answer` (tests/test_aegis_commit_path.py) |
| M108 | store.py:L244 bytes const->x | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M109 | store.py:L246 return->None | 1 / 1 | `test_h1_unreadable_log_file_is_reported` (tests/test_aegis_h1_file_content.py) |
| M110 | store.py:L246 return->True | 1 / 1 | `test_h1_unreadable_log_file_is_reported` (tests/test_aegis_h1_file_content.py) |
| M111 | store.py:L246 return->False | 1 / 1 | `test_h1_unreadable_log_file_is_reported` (tests/test_aegis_h1_file_content.py) |
| M112 | store.py:L246 return->"" | 1 / 1 | `test_h1_unreadable_log_file_is_reported` (tests/test_aegis_h1_file_content.py) |
| M113 | store.py:L246 return->b"" | 1 / 1 | `test_h1_unreadable_log_file_is_reported` (tests/test_aegis_h1_file_content.py) |
| M114 | store.py:L246 return->"x" | 1 / 1 | `test_h1_unreadable_log_file_is_reported` (tests/test_aegis_h1_file_content.py) |
| M115 | store.py:L247 cond->True | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M116 | store.py:L247 cond->False | 2 / 2 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |
| M117 | store.py:L247 cmp->== | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M118 | store.py:L248 return->None | 2 / 2 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |
| M119 | store.py:L248 return->True | 2 / 2 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |
| M120 | store.py:L248 return->False | 2 / 2 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |
| M121 | store.py:L248 return->"" | 2 / 2 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |
| M122 | store.py:L248 return->b"" | 2 / 2 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |
| M123 | store.py:L248 return->"x" | 2 / 2 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |
| M124 | store.py:L249 return->True | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M125 | store.py:L249 return->False | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M126 | store.py:L249 return->"" | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M127 | store.py:L249 return->b"" | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M128 | store.py:L249 return->"x" | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| M129 | service.py:L336 stmt->pass | 3 / 3 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |
| T222 | T222 clear_pending raise->pass | 2 / 2 | `test_t222_store_clear_pending_raises_when_the_file_cannot_be_removed` (tests/test_aegis_omega5_mutation.py) |
| X001 | except FileNotFoundError->ZeroDivisionError (file_problem) | 107 / 107 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| X002 | except OSError->ZeroDivisionError (file_problem) | 1 / 1 | `test_h1_unreadable_log_file_is_reported` (tests/test_aegis_h1_file_content.py) |
| X004 | file_problem compares to lines joined without newlines | 30 / 30 | `test_c03_own_pending_line_is_rolled_forward_after_a_lost_answer` (tests/test_aegis_commit_path.py) |
| X005 | _memory_bytes newline-separated, no trailing newline | 106 / 106 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| X006 | _pread_exact offset not advanced | 1 / 1 | `test_h1_short_reads_are_completed` (tests/test_aegis_h1_file_content.py) |
| X007 | _pread_exact n -= 1 | 2 / 2 | `test_h1_adoption_still_works_when_the_file_matches` (tests/test_aegis_h1_file_content.py) |
| X009 | size==expected -> size>=expected | 3 / 3 | `test_t149_t153_file_with_this_line_plus_an_extra_tail_is_refused` (tests/test_aegis_store_write_path.py) |
| X012 | adopt = size >= expected+len+1 | 1 / 1 | `test_t149_t153_file_with_this_line_plus_an_extra_tail_is_refused` (tests/test_aegis_store_write_path.py) |
| X013 | adopt = size > expected | 1 / 1 | `test_t149_t153_file_with_this_line_plus_an_extra_tail_is_refused` (tests/test_aegis_store_write_path.py) |
| X014 | mem compare first byte only | 5 / 5 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| X015 | mem compare length only | 5 / 5 | `test_h1_adoption_needs_the_whole_file_to_match` (tests/test_aegis_h1_file_content.py) |
| X018 | adopt compare only trailing newline | 3 / 3 | `test_h1_adoption_refuses_another_line_of_the_same_size` (tests/test_aegis_h1_file_content.py) |
| X019 | adopt compare first byte only | 2 / 2 | `test_h1_adoption_refuses_another_line_of_the_same_size` (tests/test_aegis_h1_file_content.py) |
| X020 | adopt compare last content byte dropped | 1 / 1 | `test_t148_same_size_partial_match_is_refused` (tests/test_aegis_store_write_path.py) |
| X021 | file_problem call guard if problem is None -> if True | 16 / 16 | `test_ip_anchor_beyond_the_local_log_is_caught` (tests/test_aegis_integrity_path.py) |
| X022 | file_problem result discarded (None) | 3 / 3 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |
| X025 | file_problem guard if problem is None -> if False | 3 / 3 | `test_h1_deleted_log_file_is_reported_and_refused` (tests/test_aegis_h1_file_content.py) |