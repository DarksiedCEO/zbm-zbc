"""The Marcus index pipeline (marcus-pipeline-spec/1, CLARIFICATIONS v1.1 GP-1..GP-6): single-task services that build
ZBM's own link index from Common Crawl data. v1 reads FIXTURES only (the live ports are config.NOT_BUILT).

  release_watcher.py  Release Watcher (``cc_release_watcher``): notice and record CC crawl / web-graph releases
  wat_fetcher.py      WAT Fetcher (``cc_wat_fetcher``): deliver one crawl's WAT files, verified, to the extractor
  link_extractor.py   Link Extractor (``cc_link_extractor``): WAT records -> normalised, scrubbed link rows
  index_builder.py    Index Builder (``cc_index_builder``): the target slices, sharded, encrypted, manifested
  graph_importer.py   Graph Importer (``cc_graph_importer``): the host / domain rank files of one release
  index_store.py      the encrypted store every index object is written through (W-MP-2)

The orchestration (record-first jobs, kill switches, routes) is svc_link_index.py. Nothing here opens a socket."""
