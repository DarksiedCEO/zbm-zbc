| # | id | series | mutant (file:line, description) |
|---|---|---|---|
| 1 | N146 | 1-observed/names_a_card | svc_campaigns.py:L68 observed: sha of raw text |
| 2 | N147 | 1-observed/names_a_card | svc_campaigns.py:L67 observed: truncation flag |
| 3 | N148 | 1-observed/names_a_card | svc_campaigns.py:L66 observed: truncation bound |
| 4 | N156 | 1-observed/names_a_card | svc_campaigns.py:L169 names_a_card: no NFKC |
| 5 | X024 | 1-file_problem | service.py:L373 file_problem only when log non-empty |
| 6 | F001 | 1-file_problem | store.py:L288 file_problem compare last byte dropped |
| 7 | F002 | 1-file_problem | store.py:L288 file_problem compare first byte dropped |
| 8 | F015 | 1-file_problem | store.py:L292 file_problem: read error reported as None |
| 9 | F008 | 1-file_problem | store.py:L288 file_problem regular-file check removed |
| 10 | F007 | 1-file_problem | store.py:L288 file_problem size check > instead of != |
| 11 | F003 | 1-file_problem | store.py:L288 file_problem compare first byte only |
| 12 | F004 | 1-file_problem | store.py:L288 file_problem compare length only |
| 13 | F005 | 1-file_problem | store.py:L288 file_problem size check removed |
| 14 | F006 | 1-file_problem | store.py:L288 file_problem size check < instead of != |
| 15 | F009 | 1-file_problem | store.py:L283 file_problem missing file -> differs always |
| 16 | F010 | 1-file_problem | store.py:L283 file_problem missing file -> None always |
| 17 | F011 | 1-file_problem | store.py:L283 file_problem mem empty check inverted |
| 18 | F012 | 1-file_problem | store.py:L288 file_problem compares to mem without trailing newline |
| 19 | F013 | 1-file_problem | store.py:L281 file_problem: no O_NOFOLLOW |
| 20 | F014 | 1-file_problem | store.py:L281 file_problem: no O_NONBLOCK |
| 21 | F016 | 1-file_problem | store.py:L284 file_problem: open error reported as None |
| 22 | F017 | 1-file_problem | store.py:L276 file_problem: no 'with self.lock' -> mem read before lock is n/a; mem from stale |
| 23 | H035 | 1-decode loop | pii.py:L318 _MAX_DECODE 16 -> 3 (previous bound) |
| 24 | H036 | 1-decode loop | pii.py:L318 _MAX_DECODE 16 -> 4 |
| 25 | H037 | 1-decode loop | pii.py:L318 _MAX_DECODE 16 -> 8 |
| 26 | H038 | 1-decode loop | pii.py:L327 _fully_decoded: unsettled returns the text |
| 27 | H039 | 1-decode loop | pii.py:L325 _fully_decoded: unescape once only |
| 28 | N050 | 1-_redact_all | pii.py:L302 _redact_all: range(1) |
| 29 | N051 | 1-_redact_all | pii.py:L302 _redact_all: range(2) |
| 30 | N052 | 1-_redact_all | pii.py:L302 _redact_all: range(3) |
| 31 | N053 | 1-_redact_all | pii.py:L302 _redact_all: range(5) |
| 32 | N054 | 1-_redact_all | pii.py:L303 _redact_all: unsorted spans |
| 33 | N055 | 1-_redact_all | pii.py:L303 _redact_all: sorted reverse |
| 34 | N056 | 1-_redact_all | pii.py:L304 _redact_all: no-span return -> break |
| 35 | N057 | 1-_redact_all | pii.py:L304 _redact_all: no-span return -> pass (loops) |
| 36 | N058 | 1-_redact_all | pii.py:L315 _redact_all: residual kept |
| 37 | N059 | 1-_redact_all | pii.py:L315 _redact_all: always [redacted] |
| 38 | N060 | 1-_redact_all | pii.py:L315 _redact_all: inverted residual |
| 39 | N061 | 1-_redact_all | pii.py:L315 _redact_all: residual check on find without t |
| 40 | N062 | 1-_redact_all | pii.py:L308 _redact_all: overlap skip removed |
| 41 | N063 | 1-_redact_all | pii.py:L308 _redact_all: overlap skip < not <= |
| 42 | N064 | 1-_redact_all | pii.py:L310 _redact_all: label dropped |
| 43 | N065 | 1-_redact_all | pii.py:L310 _redact_all: label of the wrong slice |
| 44 | N066 | 1-_redact_all | pii.py:L311 _redact_all: gap lost |
| 45 | N067 | 1-_redact_all | pii.py:L314 _redact_all: tail lost |
| 46 | N068 | 1-_redact_all | pii.py:L312 _redact_all: pos = lo |
| 47 | N069 | 1-_redact_all | pii.py:L311 _redact_all: max(lo,pos) -> lo |
| 48 | N070 | 1-_redact_all | pii.py:L312 _redact_all: marker text changed |
| 49 | H056 | 1-screen_free_text+backstop | pii.py:L369 screen_free_text: unsettled decoding keeps the text |
| 50 | H057 | 1-screen_free_text+backstop | pii.py:L369 screen_free_text: unsettled decoding reports no kind |
| 51 | H058 | 1-screen_free_text+backstop | pii.py:L374 screen_free_text: decoded form never kept |
| 52 | H059 | 1-screen_free_text+backstop | pii.py:L374 screen_free_text: decoded form kinds not recorded |
| 53 | H060 | 1-screen_free_text+backstop | pii.py:L373 screen_free_text: decoded form kept even when nothing found |
| 54 | H061 | 1-screen_free_text+backstop | pii.py:L370 screen_free_text: decoded form screened only when it equals text |
| 55 | H062 | 1-screen_free_text+backstop | pii.py:L372 screen_free_text: decoded screened from the kept text |
| 56 | H063 | 1-screen_free_text+backstop | pii.py:L377 screen_free_text: backstop reports kinds without number |
| 57 | H064 | 1-screen_free_text+backstop | pii.py:L377 screen_free_text: backstop returns kept text |
| 58 | H065 | 1-screen_free_text+backstop | pii.py:L377 screen_free_text: backstop reads the raw text |
| 59 | H066 | 1-screen_free_text+backstop | pii.py:L376 screen_free_text: kept text not whitespace-collapsed |
| 60 | H067 | 1-screen_free_text+backstop | pii.py:L376 screen_free_text: nothing found keeps the raw text |
| 61 | H068 | 1-screen_free_text+backstop | pii.py:L376 screen_free_text: nothing found keeps the staged text |
| 62 | H069 | 1-screen_free_text+backstop | pii.py:L366 screen_free_text: first stage on screen_form(text) |
| 63 | H070 | 1-screen_free_text+backstop | pii.py:L367 screen_free_text: decoding reads the staged text |
| 64 | H040 | 1-screen_free_text+backstop | pii.py:L343 _forms: unsettled decoding gives only the text |
| 65 | H041 | 1-screen_free_text+backstop | pii.py:L346 _forms: drops the decoded text |
| 66 | H042 | 1-screen_free_text+backstop | pii.py:L346 _forms: drops the as-kept text |
| 67 | H043 | 1-screen_free_text+backstop | pii.py:L347 _forms: no screen_form form |
| 68 | H044 | 1-screen_free_text+backstop | pii.py:L347 _forms: no _norm form |
| 69 | H045 | 1-screen_free_text+backstop | pii.py:L347 _forms: no plain form |
| 70 | H046 | 1-screen_free_text+backstop | pii.py:L353 _still_holds: cluster test on first form only |
| 71 | H047 | 1-screen_free_text+backstop | pii.py:L353 _still_holds: free-text test without _norm |
| 72 | H048 | 1-screen_free_text+backstop | pii.py:L353 _still_holds: free-text test dropped |
| 73 | H049 | 1-screen_free_text+backstop | pii.py:L353 _still_holds: cluster test dropped |
| 74 | H050 | 1-screen_free_text+backstop | pii.py:L353 _still_holds: unsettled decoding not a hit |
| 75 | H051 | 1-screen_free_text+backstop | pii.py:L353 _still_holds: free-text test on first form only |
| 76 | H052 | 1-screen_free_text+backstop | pii.py:L335 _screen_stages: first stage skipped |
| 77 | H053 | 1-screen_free_text+backstop | pii.py:L336 _screen_stages: second stage reads t without _norm |
| 78 | H054 | 1-screen_free_text+backstop | pii.py:L335 _screen_stages: first stage reads t without screen_form |
| 79 | H055 | 1-screen_free_text+backstop | pii.py:L336 _screen_stages: second stage uses the cluster finder |
| 80 | H025 | 1-strip_format | pii.py:L113 strip_format: decimal() instead of digit() |
| 81 | H026 | 1-strip_format | pii.py:L113 strip_format: non-ASCII bound > \x7f -> > \xff |
| 82 | H027 | 1-strip_format | pii.py:L113 strip_format: digit applied to every character |
| 83 | H028 | 1-strip_format | pii.py:L113 strip_format: default 0 for non-digits |
| 84 | H029 | 1-strip_format | pii.py:L113 strip_format: ch >= \x7f |
| 85 | H001 | 2-_W/patterns/screen_form (H) | pii.py:L55 _W drops underscore |
| 86 | H002 | 2-_W/patterns/screen_form (H) | pii.py:L55 _W drops digits |
| 87 | H003 | 2-_W/patterns/screen_form (H) | pii.py:L55 _W drops the non-ASCII branch |
| 88 | H004 | 2-_W/patterns/screen_form (H) | pii.py:L55 _W non-ASCII branch no longer excludes spaces |
| 89 | H005 | 2-_W/patterns/screen_form (H) | pii.py:L55 _W ASCII letters lower-case only |
| 90 | H006 | 2-_W/patterns/screen_form (H) | pii.py:L55 _W non-ASCII branch -> only Latin-1 supplement |
| 91 | H007 | 2-_W/patterns/screen_form (H) | pii.py:L55 _W ASCII range bound \x7f -> \x7e (DEL joins) |
| 92 | H008 | 2-_W/patterns/screen_form (H) | pii.py:L56 _DOM drops hyphen |
| 93 | H009 | 2-_W/patterns/screen_form (H) | pii.py:L56 _DOM + -> * |
| 94 | H010 | 2-_W/patterns/screen_form (H) | pii.py:L57 _WORD_DOM drops hyphen |
| 95 | H011 | 2-_W/patterns/screen_form (H) | pii.py:L57 _WORD_DOM * -> + |
| 96 | H012 | 2-_W/patterns/screen_form (H) | pii.py:L67 _EMAIL_WORD local bound 64 -> 63 |
| 97 | H013 | 2-_W/patterns/screen_form (H) | pii.py:L67 _EMAIL_WORD local bound 64 -> 65 |
| 98 | H014 | 2-_W/patterns/screen_form (H) | pii.py:L67 _EMAIL_WORD local part drops _W |
| 99 | H015 | 2-_W/patterns/screen_form (H) | pii.py:L67 _EMAIL_WORD local part drops '.' |
| 100 | H016 | 2-_W/patterns/screen_form (H) | pii.py:L67 _EMAIL_WORD local part drops '+' |
| 101 | H017 | 2-_W/patterns/screen_form (H) | pii.py:L67 _EMAIL_WORD local part drops '-' |
| 102 | H018 | 2-_W/patterns/screen_form (H) | pii.py:L80 _INTL drops the ASCII lookbehind |
| 103 | H019 | 2-_W/patterns/screen_form (H) | pii.py:L80 _INTL drops the non-ASCII lookbehind |
| 104 | H020 | 2-_W/patterns/screen_form (H) | pii.py:L80 _INTL lookbehind drops '_' |
| 105 | H021 | 2-_W/patterns/screen_form (H) | pii.py:L80 _INTL lookbehind drops digits |
| 106 | H022 | 2-_W/patterns/screen_form (H) | pii.py:L80 _INTL lookbehind drops letters |
| 107 | H023 | 2-_W/patterns/screen_form (H) | pii.py:L80 _INTL lookbehind drops '+' |
| 108 | H024 | 2-_W/patterns/screen_form (H) | pii.py:L80 _INTL second lookbehind admits non-ASCII spaces as blockers |
| 109 | H030 | 2-_W/patterns/screen_form (H) | pii.py:L124 screen_form: one pass only |
| 110 | H031 | 2-_W/patterns/screen_form (H) | pii.py:L124 screen_form: two passes only |
| 111 | H032 | 2-_W/patterns/screen_form (H) | pii.py:L128 screen_form: returns once |
| 112 | H033 | 2-_W/patterns/screen_form (H) | pii.py:L127 screen_form: no fixed-point break |
| 113 | H034 | 2-_W/patterns/screen_form (H) | pii.py:L126 screen_form: reads strip_format of the original every pass |
| 114 | P001 | 2-P-series | pii.py:L87 _INVISIBLE drops U+034F |
| 115 | P002 | 2-P-series | pii.py:L88 _INVISIBLE drops tag variation selectors |
| 116 | P003 | 2-P-series | pii.py:L87 _INVISIBLE drops variation selectors ufe00-ufe0f |
| 117 | P004 | 2-P-series | pii.py:L87 _INVISIBLE drops braille blank u2800 |
| 118 | P005 | 2-P-series | pii.py:L87 _INVISIBLE drops hangul filler u3164 and uffa0 |
| 119 | P006 | 2-P-series | pii.py:L87 _INVISIBLE drops u180b-u180f |
| 120 | P010 | 2-P-series | pii.py:L103 strip_format: no NFKC |
| 121 | P011 | 2-P-series | pii.py:L107 strip_format: marks kept on digits |
| 122 | P012 | 2-P-series | pii.py:L107 strip_format: Me not dropped |
| 123 | P013 | 2-P-series | pii.py:L107 strip_format: Mc not dropped |
| 124 | P014 | 2-P-series | pii.py:L107 strip_format: Mn not dropped |
| 125 | P016 | 2-P-series | pii.py:L107 strip_format: mark dropped regardless of previous digit |
| 126 | P017 | 2-P-series | pii.py:L110 strip_format: hidden ignored for _REMOVED |
| 127 | P021 | 2-P-series | pii.py:L135 _norm: hidden not passed |
| 128 | P022 | 2-P-series | pii.py:L136 _norm: no %40 |
| 129 | P023 | 2-P-series | pii.py:L137 _norm: no at spellings |
| 130 | P024 | 2-P-series | pii.py:L138 _norm: no dot spellings |
| 131 | P025 | 2-P-series | pii.py:L236 _screened_form: alt uses same hidden |
| 132 | P026 | 2-P-series | pii.py:L237 _screened_form: always t |
| 133 | P027 | 2-P-series | pii.py:L234 _screened_form: always alt |
| 134 | P030 | 2-P-series | pii.py:L155 _DIGIT_RUN: letters allowed between groups |
| 135 | P031 | 2-P-series | pii.py:L155 _DIGIT_RUN: single separator only |
| 136 | P032 | 2-P-series | pii.py:L155 _DIGIT_RUN: lowercase letters only block |
| 137 | P033 | 2-P-series | pii.py:L155 _DIGIT_RUN: at most 3 separators |
| 138 | P034 | 2-P-series | pii.py:L155 _DIGIT_RUN: separators not repeated groups |
| 139 | P035 | 2-P-series | pii.py:L156 _GROUP single digits |
| 140 | P036 | 2-P-series | pii.py:L152 _CARD_PREFIX: drops Visa 4 |
| 141 | P037 | 2-P-series | pii.py:L152 _CARD_PREFIX: 5[0-8] -> 5[1-5] |
| 142 | P038 | 2-P-series | pii.py:L152 _CARD_PREFIX: drops 3[0-9] (Amex/JCB/Diners) |
| 143 | P039 | 2-P-series | pii.py:L152 _CARD_PREFIX: 3[0-9] -> 3[4-7] |
| 144 | P040 | 2-P-series | pii.py:L152 _CARD_PREFIX: drops 6 (Discover/UnionPay) |
| 145 | P041 | 2-P-series | pii.py:L152 _CARD_PREFIX: drops 8[12] |
| 146 | P042 | 2-P-series | pii.py:L152 _CARD_PREFIX: drops 2-series Mastercard 222[1-9] |
| 147 | P043 | 2-P-series | pii.py:L152 _CARD_PREFIX: drops 2720 |
| 148 | P044 | 2-P-series | pii.py:L153 _CARD_PREFIX: 9792/9860 dropped |
| 149 | P045 | 2-P-series | pii.py:L152 _CARD_PREFIX not anchored (search-like) |
| 150 | P046 | 2-P-series | pii.py:L154 _CARD_PREFIX_15: drops 4 |
| 151 | P047 | 2-P-series | pii.py:L154 _CARD_PREFIX_15: 3[4-8] -> 3[47] |
| 152 | P048 | 2-P-series | pii.py:L154 _CARD_PREFIX_15: drops 6011 |
| 153 | P049 | 2-P-series | pii.py:L154 _CARD_PREFIX_15: drops 5[0-8] |
| 154 | P050 | 2-P-series | pii.py:L169 luhn: 15-digit rule removed |
| 155 | P051 | 2-P-series | pii.py:L168 luhn: prefix check removed |
| 156 | P052 | 2-P-series | pii.py:L168 luhn: 16-digit special (len != 16) |
| 157 | P053 | 2-P-series | pii.py:L175 luhn: 'd * 2 - 9 if d > 4' -> d*2 |
| 158 | P054 | 2-P-series | pii.py:L174 luhn: i % 2 -> (i+1) % 2 |
| 159 | P055 | 2-P-series | pii.py:L172 luhn: no reversed |
| 160 | P056 | 2-P-series | pii.py:L163 aba: weights (3,7,1)->(3,1,7) |
| 161 | P057 | 2-P-series | pii.py:L161 aba: len check != 9 removed |
| 162 | P058 | 2-P-series | pii.py:L163 aba: always False |
| 163 | P059 | 2-P-series | pii.py:L199 _card_from: group >=3 -> >=2 |
| 164 | P060 | 2-P-series | pii.py:L199 _card_from: group >=3 -> >=4 |
| 165 | P061 | 2-P-series | pii.py:L199 _card_from: aba exclusion removed |
| 166 | P062 | 2-P-series | pii.py:L199 _card_from: only groups[i] checked |
| 167 | P063 | 2-P-series | pii.py:L199 _card_from: only groups[j] checked |
| 168 | P064 | 2-P-series | pii.py:L202 _card_from: exceed 19 -> continue (not stop) |
| 169 | P065 | 2-P-series | pii.py:L200 _card_from: grouping failure -> continue |
| 170 | P066 | 2-P-series | pii.py:L187 card_spans: only first start index |
| 171 | P067 | 2-P-series | pii.py:L188 card_spans: span of the first group only |
| 172 | P068 | 2-P-series | pii.py:L202 _card_from: stop at 20 / 18 |
| 173 | P069 | 2-P-series | pii.py:L194 _card_from: j range excludes last group |
| 174 | P070 | 2-P-series | pii.py:L215 _SSEP: {1,3} -> {1,2} |
| 175 | P071 | 2-P-series | pii.py:L215 _SSEP: {1,3} -> {1,4} |
| 176 | P072 | 2-P-series | pii.py:L215 _SSEP: {1,3} -> {0,3} |
| 177 | P073 | 2-P-series | pii.py:L215 _SSEP: newline allowed |
| 178 | P074 | 2-P-series | pii.py:L215 _SSEP: lowercase letters only excluded |
| 179 | P075 | 2-P-series | pii.py:L216 _SSN: lookbehind removed |
| 180 | P076 | 2-P-series | pii.py:L216 _SSN: lookahead removed |
| 181 | P077 | 2-P-series | pii.py:L216 _SSN: area 3->2 digits |
| 182 | P078 | 2-P-series | pii.py:L216 _SSN: group 2->3 |
| 183 | P079 | 2-P-series | pii.py:L216 _SSN: serial 4->3 |
| 184 | P080 | 2-P-series | pii.py:L217 _SSN_WORDS: drops ssn |
| 185 | P081 | 2-P-series | pii.py:L217 _SSN_WORDS: drops itin |
| 186 | P082 | 2-P-series | pii.py:L217 _SSN_WORDS: drops tin |
| 187 | P083 | 2-P-series | pii.py:L217 _SSN_WORDS: drops tax id |
| 188 | P084 | 2-P-series | pii.py:L217 _SSN_WORDS: drops social security |
| 189 | P085 | 2-P-series | pii.py:L218 _SSN_VALUE: {0,20} -> {0,19} |
| 190 | P086 | 2-P-series | pii.py:L218 _SSN_VALUE: {0,20} -> {0,21} |
| 191 | P087 | 2-P-series | pii.py:L218 _SSN_VALUE: {0,20} -> {0,10} |
| 192 | P088 | 2-P-series | pii.py:L218 _SSN_VALUE: word lookbehind removed |
| 193 | P089 | 2-P-series | pii.py:L218 _SSN_VALUE: word lookahead removed |
| 194 | P090 | 2-P-series | pii.py:L218 _SSN_VALUE: {8} -> {7} |
| 195 | P091 | 2-P-series | pii.py:L218 _SSN_VALUE: {8} -> {9} |
| 196 | P092 | 2-P-series | pii.py:L218 _SSN_VALUE: inner sep {0,3} -> {0,2} |
| 197 | P093 | 2-P-series | pii.py:L218 _SSN_VALUE: inner sep {0,3} -> {1,3} |
| 198 | P094 | 2-P-series | pii.py:L219 _SSN_VALUE: not case-insensitive |
| 199 | P095 | 2-P-series | pii.py:L218 _SSN_VALUE: trailing digit lookahead removed |
| 200 | P096 | 2-P-series | pii.py:L218 _SSN_VALUE: gap allows newline |
| 201 | P097 | 2-P-series | pii.py:L224 _ssn_spans: group span -> whole span |
| 202 | P098 | 2-P-series | pii.py:L224 _ssn_spans: only _SSN |
| 203 | P099 | 2-P-series | pii.py:L223 _ssn_spans: only _SSN_VALUE |
| 204 | P104 | 2-P-series | pii.py:L273 cluster: sentinel removed |
| 205 | P105 | 2-P-series | pii.py:L276 cluster: end excludes last digit |
| 206 | P106 | 2-P-series | pii.py:L279 cluster: start one late |
| 207 | P107 | 2-P-series | pii.py:L277 cluster: count reset not done on letter |
| 208 | P108 | 2-P-series | pii.py:L280 cluster: only digits counted on same segment (count not incremented) |
| 209 | P110 | 2-P-series | pii.py:L297 _free_text_spans: no cluster |
| 210 | P111 | 2-P-series | pii.py:L297 _free_text_spans: no shapes |
| 211 | P112 | 2-P-series | pii.py:L285 _label: card before email |
| 212 | P113 | 2-P-series | pii.py:L287 _label: ssn before card |
| 213 | P114 | 2-P-series | pii.py:L291 _label: no phone |
| 214 | P115 | 2-P-series | pii.py:L293 _label: number default -> card |
| 215 | P126 | 2-P-series | pii.py:L308 screen_free_text: no overlap skip |
| 216 | P127 | 2-P-series | pii.py:L310 screen_free_text: kinds only first |
| 217 | P130 | 2-P-series | pii.py:L383 free_text_has_personal_data: kinds[0]->screen only |
| 218 | P131 | 2-P-series | pii.py:L383 free_text_has_personal_data: always False |
| 219 | P132 | 2-P-series | pii.py:L241 has_personal_data: raw text |
| 220 | P133 | 2-P-series | pii.py:L241 has_personal_data: _norm only |
| 221 | P134 | 2-P-series | pii.py:L390 redact: loop 4 -> 1 |
| 222 | P135 | 2-P-series | pii.py:L394 redact: residual returns t |
| 223 | P136 | 2-P-series | pii.py:L248 _redact_once: overlap skip removed |
| 224 | P137 | 2-P-series | pii.py:L228 _spans: no cards |
| 225 | P138 | 2-P-series | pii.py:L228 _spans: no ssn |
| 226 | P139 | 2-P-series | pii.py:L228 _spans: no _matches |
