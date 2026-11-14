# Third-party notices (services/seo-py)

Every data file bundled in this service's source tree (`src/`) and in its test tree, its source and its licence
(spec wave1-spec/1 A-32). No wordfreq, NLTK-derived or schema.org data is bundled (removed with the dictionary design:
`path_words.txt`, `schema_types.py`).

## src/primitives/cldr_validity.txt

- What: the language, script and region subtags of the CLDR validity data (statuses regular, special,
  macrolanguage, macroregion, deprecated, private_use, unknown), one line per kind; used by Roman to validate a
  page's `lang` (spec B-RO-05, D-01 L, Q-05).
- Source: Unicode CLDR release-48, github.com/unicode-org/cldr commit acd6d88ae493633240e19a87a721076a8a75c310
  (2025-10-23), `common/validity/{language,script,region}.xml`, derived by `devtools/conformance/cldr_extract.py`.
- License: Unicode-3.0
- Notice: Copyright (c) 1991-2025 Unicode, Inc. Distributed under the Unicode License v3 (the full text is
  `tests/conformance/oracles/data/cldr/LICENSE-Unicode-3.0.txt`).

## tests/conformance/oracles/data/cldr/{language,script,region,variant}.xml

- What: the CLDR validity data files as published, read by the independent BCP 47 oracle of the conformance suite
  (never by the service).
- Source: as above (Unicode CLDR release-48).
- License: Unicode-3.0
- Notice: Copyright (c) 1991-2025 Unicode, Inc.; licence text beside the files.

## src/agents/public_suffix_list.dat

- What: the Public Suffix List (registrable-domain rules), bundled before Wave 1 (agents/domain_match.py).
- Source: publicsuffix.org (Mozilla).
- License: MPL-2.0
- Note: MPL-2.0 is not on the owner-approved list of spec A-32 (CC-BY-4.0, Unicode-3.0, MIT-CMU, CNRI-Python,
  PDFium) and is a file-level copyleft licence: OPEN QUESTION for the owner (Wave-1 v2 report). The file is unmodified.

## src/agents/common_words.txt

- What: a short list of common English words that are also plausible brand names (agents/brand_mention.py).
- Source: self-authored for this repository (its header says so).
- License: Public-Domain (self-authored; no third-party licence applies)
