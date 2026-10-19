| id | mutant | 3.12 | 3.13 |
|---|---|---|---|
| N146 | svc_campaigns.py:L68 observed: sha of raw text | killed by tests/test_aegis_r12_mutation_gaps.py::test_n146_vendor_text_sha256_is_of_the_kept_text | killed by tests/test_aegis_r12_mutation_gaps.py::test_n146_vendor_text_sha256_is_of_the_kept_text |
| N147 | svc_campaigns.py:L67 observed: truncation flag | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N148 | svc_campaigns.py:L66 observed: truncation bound | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N156 | svc_campaigns.py:L169 names_a_card: no NFKC | killed by tests/test_aegis_r12_mutation_gaps.py::test_n156_names_a_card_reads_full_width_digits | killed by tests/test_aegis_r12_mutation_gaps.py::test_n156_names_a_card_reads_full_width_digits |
| X024 | service.py:L373 file_problem only when log non-empty | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log |
| F001 | store.py:L288 file_problem compare last byte dropped | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[last] | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[last] |
| F002 | store.py:L288 file_problem compare first byte dropped | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[first] | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[first] |
| F015 | store.py:L292 file_problem: read error reported as None | killed by tests/test_aegis_r12_mutation_gaps.py::test_f015_an_unreadable_log_is_a_problem | killed by tests/test_aegis_r12_mutation_gaps.py::test_f015_an_unreadable_log_is_a_problem |
| F008 | store.py:L288 file_problem regular-file check removed | killed by tests/test_aegis_r12_mutation_gaps.py::test_f008_a_fifo_at_the_path_of_an_empty_log_is_a_problem | killed by tests/test_aegis_r12_mutation_gaps.py::test_f008_a_fifo_at_the_path_of_an_empty_log_is_a_problem |
| F007 | store.py:L288 file_problem size check > instead of != | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| F003 | store.py:L288 file_problem compare first byte only | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[last] | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[last] |
| F004 | store.py:L288 file_problem compare length only | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[first] | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[first] |
| F005 | store.py:L288 file_problem size check removed | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log |
| F006 | store.py:L288 file_problem size check < instead of != | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_reads_the_file_of_an_empty_log |
| F009 | store.py:L283 file_problem missing file -> differs always | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[first] | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[first] |
| F010 | store.py:L283 file_problem missing file -> None always | killed by tests/test_aegis_h1_file_content.py::test_h1_deleted_log_file_is_reported_and_refused | killed by tests/test_aegis_h1_file_content.py::test_h1_deleted_log_file_is_reported_and_refused |
| F011 | store.py:L283 file_problem mem empty check inverted | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[first] | killed by tests/test_aegis_r12_mutation_gaps.py::test_f001_f002_integrity_check_alone_sees_an_edge_byte_edit[first] |
| F012 | store.py:L288 file_problem compares to mem without trailing newline | killed by tests/test_aegis_h1_file_content.py::test_h1_integrity_check_is_ok_on_an_unchanged_file | killed by tests/test_aegis_h1_file_content.py::test_h1_integrity_check_is_ok_on_an_unchanged_file |
| F013 | store.py:L281 file_problem: no O_NOFOLLOW | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_does_not_follow_a_symlink_even_to_identical_bytes | killed by tests/test_aegis_h1_edges.py::test_edges_integrity_check_does_not_follow_a_symlink_even_to_identical_bytes |
| F014 | store.py:L281 file_problem: no O_NONBLOCK | TIMEOUT(hang) at tests/test_aegis_r12_mutation_gaps.py::test_f008_a_fifo_at_the_path_of_an_empty_log_is_a_problem | TIMEOUT(hang) at tests/test_aegis_r12_mutation_gaps.py::test_f008_a_fifo_at_the_path_of_an_empty_log_is_a_problem |
| F016 | store.py:L284 file_problem: open error reported as None | killed by tests/test_aegis_h1_file_content.py::test_h1_unreadable_log_file_is_reported | killed by tests/test_aegis_h1_file_content.py::test_h1_unreadable_log_file_is_reported |
| F017 | store.py:L276 file_problem: no 'with self.lock' -> mem read before lock is n/a; mem from stale | killed by tests/test_aegis_r12_mutation_gaps.py::test_f015_an_unreadable_log_is_a_problem | killed by tests/test_aegis_r12_mutation_gaps.py::test_f015_an_unreadable_log_is_a_problem |
| H035 | pii.py:L318 _MAX_DECODE 16 -> 3 (previous bound) | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| H036 | pii.py:L318 _MAX_DECODE 16 -> 4 | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| H037 | pii.py:L318 _MAX_DECODE 16 -> 8 | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| H038 | pii.py:L327 _fully_decoded: unsettled returns the text | killed by tests/test_ooh2_pii1_cluster_rule.py::test_r12_decoding_that_never_settles_redacts_the_whole_value | killed by tests/test_ooh2_pii1_cluster_rule.py::test_r12_decoding_that_never_settles_redacts_the_whole_value |
| H039 | pii.py:L325 _fully_decoded: unescape once only | killed by tests/test_ooh2_pii1_cluster_rule.py::test_decisions_screen_finds_it[4111&amp;nbsp;1111&amp;nbsp;1111&amp;nbsp;1111] | killed by tests/test_ooh2_pii1_cluster_rule.py::test_decisions_screen_finds_it[4111&amp;nbsp;1111&amp;nbsp;1111&amp;nbsp;1111] |
| N050 | pii.py:L302 _redact_all: range(1) | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N051 | pii.py:L302 _redact_all: range(2) | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N052 | pii.py:L302 _redact_all: range(3) | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N053 | pii.py:L302 _redact_all: range(5) | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N054 | pii.py:L303 _redact_all: unsorted spans | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N055 | pii.py:L303 _redact_all: sorted reverse | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N056 | pii.py:L304 _redact_all: no-span return -> break | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N057 | pii.py:L304 _redact_all: no-span return -> pass (loops) | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N058 | pii.py:L315 _redact_all: residual kept | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N059 | pii.py:L315 _redact_all: always [redacted] | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N060 | pii.py:L315 _redact_all: inverted residual | SURVIVED scoped; full: survived | SURVIVED scoped; full: survived |
| N061 | pii.py:L315 _redact_all: residual check on find without t | SURVIVED scoped; full: survived | UNRUN |
| N062 | pii.py:L308 _redact_all: overlap skip removed | UNRUN | UNRUN |
| N063 | pii.py:L308 _redact_all: overlap skip < not <= | UNRUN | UNRUN |
| N064 | pii.py:L310 _redact_all: label dropped | UNRUN | UNRUN |
| N065 | pii.py:L310 _redact_all: label of the wrong slice | UNRUN | UNRUN |
| N066 | pii.py:L311 _redact_all: gap lost | UNRUN | UNRUN |
| N067 | pii.py:L314 _redact_all: tail lost | UNRUN | UNRUN |
| N068 | pii.py:L312 _redact_all: pos = lo | UNRUN | UNRUN |
| N069 | pii.py:L311 _redact_all: max(lo,pos) -> lo | UNRUN | UNRUN |
| N070 | pii.py:L312 _redact_all: marker text changed | UNRUN | UNRUN |
| H056 | pii.py:L369 screen_free_text: unsettled decoding keeps the text | UNRUN | UNRUN |
| H057 | pii.py:L369 screen_free_text: unsettled decoding reports no kind | UNRUN | UNRUN |
| H058 | pii.py:L374 screen_free_text: decoded form never kept | UNRUN | UNRUN |
| H059 | pii.py:L374 screen_free_text: decoded form kinds not recorded | UNRUN | UNRUN |
| H060 | pii.py:L373 screen_free_text: decoded form kept even when nothing found | UNRUN | UNRUN |
| H061 | pii.py:L370 screen_free_text: decoded form screened only when it equals text | UNRUN | UNRUN |
| H062 | pii.py:L372 screen_free_text: decoded screened from the kept text | UNRUN | UNRUN |
| H063 | pii.py:L377 screen_free_text: backstop reports kinds without number | UNRUN | UNRUN |
| H064 | pii.py:L377 screen_free_text: backstop returns kept text | UNRUN | UNRUN |
| H065 | pii.py:L377 screen_free_text: backstop reads the raw text | UNRUN | UNRUN |
| H066 | pii.py:L376 screen_free_text: kept text not whitespace-collapsed | UNRUN | UNRUN |
| H067 | pii.py:L376 screen_free_text: nothing found keeps the raw text | UNRUN | UNRUN |
| H068 | pii.py:L376 screen_free_text: nothing found keeps the staged text | UNRUN | UNRUN |
| H069 | pii.py:L366 screen_free_text: first stage on screen_form(text) | UNRUN | UNRUN |
| H070 | pii.py:L367 screen_free_text: decoding reads the staged text | UNRUN | UNRUN |
| H040 | pii.py:L343 _forms: unsettled decoding gives only the text | UNRUN | UNRUN |
| H041 | pii.py:L346 _forms: drops the decoded text | UNRUN | UNRUN |
| H042 | pii.py:L346 _forms: drops the as-kept text | UNRUN | UNRUN |
| H043 | pii.py:L347 _forms: no screen_form form | UNRUN | UNRUN |
| H044 | pii.py:L347 _forms: no _norm form | UNRUN | UNRUN |
| H045 | pii.py:L347 _forms: no plain form | UNRUN | UNRUN |
| H046 | pii.py:L353 _still_holds: cluster test on first form only | UNRUN | UNRUN |
| H047 | pii.py:L353 _still_holds: free-text test without _norm | UNRUN | UNRUN |
| H048 | pii.py:L353 _still_holds: free-text test dropped | UNRUN | UNRUN |
| H049 | pii.py:L353 _still_holds: cluster test dropped | UNRUN | UNRUN |
| H050 | pii.py:L353 _still_holds: unsettled decoding not a hit | UNRUN | UNRUN |
| H051 | pii.py:L353 _still_holds: free-text test on first form only | UNRUN | UNRUN |
| H052 | pii.py:L335 _screen_stages: first stage skipped | UNRUN | UNRUN |
| H053 | pii.py:L336 _screen_stages: second stage reads t without _norm | UNRUN | UNRUN |
| H054 | pii.py:L335 _screen_stages: first stage reads t without screen_form | UNRUN | UNRUN |
| H055 | pii.py:L336 _screen_stages: second stage uses the cluster finder | UNRUN | UNRUN |
| H025 | pii.py:L113 strip_format: decimal() instead of digit() | UNRUN | UNRUN |
| H026 | pii.py:L113 strip_format: non-ASCII bound > \x7f -> > \xff | UNRUN | UNRUN |
| H027 | pii.py:L113 strip_format: digit applied to every character | UNRUN | UNRUN |
| H028 | pii.py:L113 strip_format: default 0 for non-digits | UNRUN | UNRUN |
| H029 | pii.py:L113 strip_format: ch >= \x7f | UNRUN | UNRUN |
| H001 | pii.py:L55 _W drops underscore | UNRUN | UNRUN |
| H002 | pii.py:L55 _W drops digits | UNRUN | UNRUN |
| H003 | pii.py:L55 _W drops the non-ASCII branch | UNRUN | UNRUN |
| H004 | pii.py:L55 _W non-ASCII branch no longer excludes spaces | UNRUN | UNRUN |
| H005 | pii.py:L55 _W ASCII letters lower-case only | UNRUN | UNRUN |
| H006 | pii.py:L55 _W non-ASCII branch -> only Latin-1 supplement | UNRUN | UNRUN |
| H007 | pii.py:L55 _W ASCII range bound \x7f -> \x7e (DEL joins) | UNRUN | UNRUN |
| H008 | pii.py:L56 _DOM drops hyphen | UNRUN | UNRUN |
| H009 | pii.py:L56 _DOM + -> * | UNRUN | UNRUN |
| H010 | pii.py:L57 _WORD_DOM drops hyphen | UNRUN | UNRUN |
| H011 | pii.py:L57 _WORD_DOM * -> + | UNRUN | UNRUN |
| H012 | pii.py:L67 _EMAIL_WORD local bound 64 -> 63 | UNRUN | UNRUN |
| H013 | pii.py:L67 _EMAIL_WORD local bound 64 -> 65 | UNRUN | UNRUN |
| H014 | pii.py:L67 _EMAIL_WORD local part drops _W | UNRUN | UNRUN |
| H015 | pii.py:L67 _EMAIL_WORD local part drops '.' | UNRUN | UNRUN |
| H016 | pii.py:L67 _EMAIL_WORD local part drops '+' | UNRUN | UNRUN |
| H017 | pii.py:L67 _EMAIL_WORD local part drops '-' | UNRUN | UNRUN |
| H018 | pii.py:L80 _INTL drops the ASCII lookbehind | UNRUN | UNRUN |
| H019 | pii.py:L80 _INTL drops the non-ASCII lookbehind | UNRUN | UNRUN |
| H020 | pii.py:L80 _INTL lookbehind drops '_' | UNRUN | UNRUN |
| H021 | pii.py:L80 _INTL lookbehind drops digits | UNRUN | UNRUN |
| H022 | pii.py:L80 _INTL lookbehind drops letters | UNRUN | UNRUN |
| H023 | pii.py:L80 _INTL lookbehind drops '+' | UNRUN | UNRUN |
| H024 | pii.py:L80 _INTL second lookbehind admits non-ASCII spaces as blockers | UNRUN | UNRUN |
| H030 | pii.py:L124 screen_form: one pass only | UNRUN | UNRUN |
| H031 | pii.py:L124 screen_form: two passes only | UNRUN | UNRUN |
| H032 | pii.py:L128 screen_form: returns once | UNRUN | UNRUN |
| H033 | pii.py:L127 screen_form: no fixed-point break | UNRUN | UNRUN |
| H034 | pii.py:L126 screen_form: reads strip_format of the original every pass | UNRUN | UNRUN |
| P001 | pii.py:L87 _INVISIBLE drops U+034F | UNRUN | UNRUN |
| P002 | pii.py:L88 _INVISIBLE drops tag variation selectors | UNRUN | UNRUN |
| P003 | pii.py:L87 _INVISIBLE drops variation selectors ufe00-ufe0f | UNRUN | UNRUN |
| P004 | pii.py:L87 _INVISIBLE drops braille blank u2800 | UNRUN | UNRUN |
| P005 | pii.py:L87 _INVISIBLE drops hangul filler u3164 and uffa0 | UNRUN | UNRUN |
| P006 | pii.py:L87 _INVISIBLE drops u180b-u180f | UNRUN | UNRUN |
| P010 | pii.py:L103 strip_format: no NFKC | UNRUN | UNRUN |
| P011 | pii.py:L107 strip_format: marks kept on digits | UNRUN | UNRUN |
| P012 | pii.py:L107 strip_format: Me not dropped | UNRUN | UNRUN |
| P013 | pii.py:L107 strip_format: Mc not dropped | UNRUN | UNRUN |
| P014 | pii.py:L107 strip_format: Mn not dropped | UNRUN | UNRUN |
| P016 | pii.py:L107 strip_format: mark dropped regardless of previous digit | UNRUN | UNRUN |
| P017 | pii.py:L110 strip_format: hidden ignored for _REMOVED | UNRUN | UNRUN |
| P021 | pii.py:L135 _norm: hidden not passed | UNRUN | UNRUN |
| P022 | pii.py:L136 _norm: no %40 | UNRUN | UNRUN |
| P023 | pii.py:L137 _norm: no at spellings | UNRUN | UNRUN |
| P024 | pii.py:L138 _norm: no dot spellings | UNRUN | UNRUN |
| P025 | pii.py:L236 _screened_form: alt uses same hidden | UNRUN | UNRUN |
| P026 | pii.py:L237 _screened_form: always t | UNRUN | UNRUN |
| P027 | pii.py:L234 _screened_form: always alt | UNRUN | UNRUN |
| P030 | pii.py:L155 _DIGIT_RUN: letters allowed between groups | UNRUN | UNRUN |
| P031 | pii.py:L155 _DIGIT_RUN: single separator only | UNRUN | UNRUN |
| P032 | pii.py:L155 _DIGIT_RUN: lowercase letters only block | UNRUN | UNRUN |
| P033 | pii.py:L155 _DIGIT_RUN: at most 3 separators | UNRUN | UNRUN |
| P034 | pii.py:L155 _DIGIT_RUN: separators not repeated groups | UNRUN | UNRUN |
| P035 | pii.py:L156 _GROUP single digits | UNRUN | UNRUN |
| P036 | pii.py:L152 _CARD_PREFIX: drops Visa 4 | UNRUN | UNRUN |
| P037 | pii.py:L152 _CARD_PREFIX: 5[0-8] -> 5[1-5] | UNRUN | UNRUN |
| P038 | pii.py:L152 _CARD_PREFIX: drops 3[0-9] (Amex/JCB/Diners) | UNRUN | UNRUN |
| P039 | pii.py:L152 _CARD_PREFIX: 3[0-9] -> 3[4-7] | UNRUN | UNRUN |
| P040 | pii.py:L152 _CARD_PREFIX: drops 6 (Discover/UnionPay) | UNRUN | UNRUN |
| P041 | pii.py:L152 _CARD_PREFIX: drops 8[12] | UNRUN | UNRUN |
| P042 | pii.py:L152 _CARD_PREFIX: drops 2-series Mastercard 222[1-9] | UNRUN | UNRUN |
| P043 | pii.py:L152 _CARD_PREFIX: drops 2720 | UNRUN | UNRUN |
| P044 | pii.py:L153 _CARD_PREFIX: 9792/9860 dropped | UNRUN | UNRUN |
| P045 | pii.py:L152 _CARD_PREFIX not anchored (search-like) | UNRUN | UNRUN |
| P046 | pii.py:L154 _CARD_PREFIX_15: drops 4 | UNRUN | UNRUN |
| P047 | pii.py:L154 _CARD_PREFIX_15: 3[4-8] -> 3[47] | UNRUN | UNRUN |
| P048 | pii.py:L154 _CARD_PREFIX_15: drops 6011 | UNRUN | UNRUN |
| P049 | pii.py:L154 _CARD_PREFIX_15: drops 5[0-8] | UNRUN | UNRUN |
| P050 | pii.py:L169 luhn: 15-digit rule removed | UNRUN | UNRUN |
| P051 | pii.py:L168 luhn: prefix check removed | UNRUN | UNRUN |
| P052 | pii.py:L168 luhn: 16-digit special (len != 16) | UNRUN | UNRUN |
| P053 | pii.py:L175 luhn: 'd * 2 - 9 if d > 4' -> d*2 | UNRUN | UNRUN |
| P054 | pii.py:L174 luhn: i % 2 -> (i+1) % 2 | UNRUN | UNRUN |
| P055 | pii.py:L172 luhn: no reversed | UNRUN | UNRUN |
| P056 | pii.py:L163 aba: weights (3,7,1)->(3,1,7) | UNRUN | UNRUN |
| P057 | pii.py:L161 aba: len check != 9 removed | UNRUN | UNRUN |
| P058 | pii.py:L163 aba: always False | UNRUN | UNRUN |
| P059 | pii.py:L199 _card_from: group >=3 -> >=2 | UNRUN | UNRUN |
| P060 | pii.py:L199 _card_from: group >=3 -> >=4 | UNRUN | UNRUN |
| P061 | pii.py:L199 _card_from: aba exclusion removed | UNRUN | UNRUN |
| P062 | pii.py:L199 _card_from: only groups[i] checked | UNRUN | UNRUN |
| P063 | pii.py:L199 _card_from: only groups[j] checked | UNRUN | UNRUN |
| P064 | pii.py:L202 _card_from: exceed 19 -> continue (not stop) | UNRUN | UNRUN |
| P065 | pii.py:L200 _card_from: grouping failure -> continue | UNRUN | UNRUN |
| P066 | pii.py:L187 card_spans: only first start index | UNRUN | UNRUN |
| P067 | pii.py:L188 card_spans: span of the first group only | UNRUN | UNRUN |
| P068 | pii.py:L202 _card_from: stop at 20 / 18 | UNRUN | UNRUN |
| P069 | pii.py:L194 _card_from: j range excludes last group | UNRUN | UNRUN |
| P070 | pii.py:L215 _SSEP: {1,3} -> {1,2} | UNRUN | UNRUN |
| P071 | pii.py:L215 _SSEP: {1,3} -> {1,4} | UNRUN | UNRUN |
| P072 | pii.py:L215 _SSEP: {1,3} -> {0,3} | UNRUN | UNRUN |
| P073 | pii.py:L215 _SSEP: newline allowed | UNRUN | UNRUN |
| P074 | pii.py:L215 _SSEP: lowercase letters only excluded | UNRUN | UNRUN |
| P075 | pii.py:L216 _SSN: lookbehind removed | UNRUN | UNRUN |
| P076 | pii.py:L216 _SSN: lookahead removed | UNRUN | UNRUN |
| P077 | pii.py:L216 _SSN: area 3->2 digits | UNRUN | UNRUN |
| P078 | pii.py:L216 _SSN: group 2->3 | UNRUN | UNRUN |
| P079 | pii.py:L216 _SSN: serial 4->3 | UNRUN | UNRUN |
| P080 | pii.py:L217 _SSN_WORDS: drops ssn | UNRUN | UNRUN |
| P081 | pii.py:L217 _SSN_WORDS: drops itin | UNRUN | UNRUN |
| P082 | pii.py:L217 _SSN_WORDS: drops tin | UNRUN | UNRUN |
| P083 | pii.py:L217 _SSN_WORDS: drops tax id | UNRUN | UNRUN |
| P084 | pii.py:L217 _SSN_WORDS: drops social security | UNRUN | UNRUN |
| P085 | pii.py:L218 _SSN_VALUE: {0,20} -> {0,19} | UNRUN | UNRUN |
| P086 | pii.py:L218 _SSN_VALUE: {0,20} -> {0,21} | UNRUN | UNRUN |
| P087 | pii.py:L218 _SSN_VALUE: {0,20} -> {0,10} | UNRUN | UNRUN |
| P088 | pii.py:L218 _SSN_VALUE: word lookbehind removed | UNRUN | UNRUN |
| P089 | pii.py:L218 _SSN_VALUE: word lookahead removed | UNRUN | UNRUN |
| P090 | pii.py:L218 _SSN_VALUE: {8} -> {7} | UNRUN | UNRUN |
| P091 | pii.py:L218 _SSN_VALUE: {8} -> {9} | UNRUN | UNRUN |
| P092 | pii.py:L218 _SSN_VALUE: inner sep {0,3} -> {0,2} | UNRUN | UNRUN |
| P093 | pii.py:L218 _SSN_VALUE: inner sep {0,3} -> {1,3} | UNRUN | UNRUN |
| P094 | pii.py:L219 _SSN_VALUE: not case-insensitive | UNRUN | UNRUN |
| P095 | pii.py:L218 _SSN_VALUE: trailing digit lookahead removed | UNRUN | UNRUN |
| P096 | pii.py:L218 _SSN_VALUE: gap allows newline | UNRUN | UNRUN |
| P097 | pii.py:L224 _ssn_spans: group span -> whole span | UNRUN | UNRUN |
| P098 | pii.py:L224 _ssn_spans: only _SSN | UNRUN | UNRUN |
| P099 | pii.py:L223 _ssn_spans: only _SSN_VALUE | UNRUN | UNRUN |
| P104 | pii.py:L273 cluster: sentinel removed | UNRUN | UNRUN |
| P105 | pii.py:L276 cluster: end excludes last digit | UNRUN | UNRUN |
| P106 | pii.py:L279 cluster: start one late | UNRUN | UNRUN |
| P107 | pii.py:L277 cluster: count reset not done on letter | UNRUN | UNRUN |
| P108 | pii.py:L280 cluster: only digits counted on same segment (count not incremented) | UNRUN | UNRUN |
| P110 | pii.py:L297 _free_text_spans: no cluster | UNRUN | UNRUN |
| P111 | pii.py:L297 _free_text_spans: no shapes | UNRUN | UNRUN |
| P112 | pii.py:L285 _label: card before email | UNRUN | UNRUN |
| P113 | pii.py:L287 _label: ssn before card | UNRUN | UNRUN |
| P114 | pii.py:L291 _label: no phone | UNRUN | UNRUN |
| P115 | pii.py:L293 _label: number default -> card | UNRUN | UNRUN |
| P126 | pii.py:L308 screen_free_text: no overlap skip | UNRUN | UNRUN |
| P127 | pii.py:L310 screen_free_text: kinds only first | UNRUN | UNRUN |
| P130 | pii.py:L383 free_text_has_personal_data: kinds[0]->screen only | UNRUN | UNRUN |
| P131 | pii.py:L383 free_text_has_personal_data: always False | UNRUN | UNRUN |
| P132 | pii.py:L241 has_personal_data: raw text | UNRUN | UNRUN |
| P133 | pii.py:L241 has_personal_data: _norm only | UNRUN | UNRUN |
| P134 | pii.py:L390 redact: loop 4 -> 1 | UNRUN | UNRUN |
| P135 | pii.py:L394 redact: residual returns t | UNRUN | UNRUN |
| P136 | pii.py:L248 _redact_once: overlap skip removed | UNRUN | UNRUN |
| P137 | pii.py:L228 _spans: no cards | UNRUN | UNRUN |
| P138 | pii.py:L228 _spans: no ssn | UNRUN | UNRUN |
| P139 | pii.py:L228 _spans: no _matches | UNRUN | UNRUN |
