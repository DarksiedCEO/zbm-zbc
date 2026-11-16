# Third-party notices (services/seo-py)

Every data file bundled in this service's source tree (`src/`) and in its test tree, its source and its licence
(spec A-32). This branch carries only Priya's entries (priya-spec/2); the core branch has the same file with the
Wave-1 entries, and the two are combined at merge (lead ruling N-1, 2026-11-14).

## src/agents/priya_vocab.txt.gz

- What: Priya's vocabulary V (priya-spec/2 section 2.3): every word of the wordfreq 3.1.1 `small` word lists of
  42 languages plus the words of its `large` lists (21 languages) with zipf >= 2.5 (owner 2026-11-14 14:45),
  in the canonical form of `agents/priya_gate.canonical`, sorted and unique, one word per line, gzip.
  Built by `devtools/priya/build_vocab.py` from the wordfreq 3.1.1 wheel (sha256
  4b1c6ecffc6198be3396d5cf871c4423ca71c907c231348d352dd54d62b97473); only the words are kept, no frequencies.
- Source: wordfreq 3.1.1 by Robyn Speer (https://github.com/rspeer/wordfreq), built from Exquisite Corpus
  (https://github.com/LuminosoInsight/exquisite-corpus).
- License: CC-BY-SA-4.0 (https://creativecommons.org/licenses/by-sa/4.0/). Owner decision 2026-11-14 10:30 PT:
  approved for exactly this one data file (path-scoped exception); the code that reads it stays under this
  repository's licence. V is a derived (adapted) work of the wordfreq data and is shared under the same licence.
- Changes made: words re-normalized (NFKC, casefold, accent fold, control / format / mark characters dropped,
  digits to ASCII, confusable letters in mixed-script words mapped per UTS #39), de-duplicated across languages,
  frequencies dropped.
- Upstream attribution (as listed in the wordfreq licence notice, README "License" section in the wheel METADATA):
  - Google Books Ngrams (http://books.google.com/ngrams) and Google Books Syntactic Ngrams
    (http://commondatastorage.googleapis.com/books/syntactic-ngrams/index.html); source acknowledged:
    Google Books Ngram Viewer.
  - The Leeds Internet Corpus, University of Leeds Centre for Translation Studies
    (http://corpus.leeds.ac.uk/list.html).
  - Wikipedia, the free encyclopedia (http://www.wikipedia.org).
  - ParaCrawl, a multilingual Web crawl (https://paracrawl.eu).
  - OPUS OpenSubtitles 2018 (http://opus.nlpl.eu/OpenSubtitles.php), data originating from the OpenSubtitles
    project (http://www.opensubtitles.org/).
  - SUBTLEX word lists SUBTLEX-US, SUBTLEX-UK, SUBTLEX-CH, SUBTLEX-DE and SUBTLEX-NL, created by Marc Brysbaert
    et al. (http://crr.ugent.be/programs-data/subtitle-frequencies). SUBTLEX is freely available data.
  - Word statistics collected by wordfreq from the streaming Twitter API (no Twitter content is republished).
- Licence note beside the file: `src/agents/priya_vocab.txt.gz.LICENSE`.

## src/agents/confusables-15.1.0.txt

- What: UTS #39 confusables (Unicode 15.1.0, 2023-08-11), read by `agents/priya_gate.py` for CAN-4.
- Source: unicode.org Public/security/15.1.0/confusables.txt, vendored byte-identical from Wave-1 b1611751
  `devtools/lookalikes/` (sha256 8289f833e4cf78fde56b2080dc0e42934ef5182c9c3f4dd1fbdf2bced69fd5ed; not
  re-verified against unicode.org here, the proxy blocks it).
- License: Unicode-3.0
- Notice: Copyright (c) 1991-2023 Unicode, Inc.
