"""Shared actions of the Marcus pipeline conformance suite (marcus-pipeline-spec/1)."""

from __future__ import annotations

import json

from helpers import rid

SERVICES = ("cc_release_watcher", "cc_wat_fetcher", "cc_link_extractor", "cc_index_builder", "cc_graph_importer")
STARTABLE = ("cc_release_watcher", "cc_index_builder", "cc_graph_importer")
OUTCOMES = ("OK", "PARTIAL", "NOT_CONNECTED", "BLOCKED", "FAILED", "KILLED", "QUARANTINED", "INSUFFICIENT_EVIDENCE",
            "RESTRICTED")
KINDS = ("link_index_job_requested", "link_index_job_recorded", "link_index_job_interrupted", "cc_release_seen",
         "wat_file_done", "wat_file_quarantined", "index_snapshot_published", "index_snapshot_retired",
         "graph_ranks_imported", "cc_spike_recorded")


def post_job(h, service: str, caller: str = "dashboard", **body):
    b = {"request_id": body.pop("request_id", None) or rid(), "service": service, **body}
    return h.post("/link-index/jobs", b, caller=caller)


def job(h, service: str, code: int = 201, caller: str = "dashboard", **body) -> dict:
    r = post_job(h, service, caller=caller, **body)
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


def watch(h) -> dict:
    return job(h, "cc_release_watcher")


def build(h, crawls=("CC-MAIN-2026-40",), code: int = 201, **body) -> dict:
    if not h.svc.cc_releases:
        watch(h)
    return job(h, "cc_index_builder", code=code, crawl_ids=list(crawls), **body)


def graphs(h, kind: str = "host_graph", release: str = "cc-main-2026-jul-aug-sep", code: int = 201) -> dict:
    if not h.svc.cc_releases:
        watch(h)
    return job(h, "cc_graph_importer", code=code, release_id=release, kind=kind)


def snapshot(h, sid: str) -> dict:
    """The manifest and every shard's rows of a published snapshot, decrypted with the service's index key (the test
    reads them as Marcus would)."""
    st = h.svc.index_store
    man = st.read_manifest(sid)
    rows = {}
    for sh in man["shards"]:
        rows[sh["shard_id"]] = st.read_shard(sid, sh["shard_id"], sh["sha256"])
    return {"manifest": man, "rows": rows}


def all_rows(h, sid: str) -> list:
    s = snapshot(h, sid)
    return [r for sh in s["manifest"]["shards"] for r in s["rows"][sh["shard_id"]]]


def stored_bytes(h) -> bytes:
    """Everything the service wrote that a reader of the data directory or the process could see: the log lines, the
    ledger payloads, the index store's files (as stored, encrypted), and the vault files."""
    out = [json.dumps(r, sort_keys=True).encode() for r in h.svc.log.iter_records()]
    out += [json.dumps(e.get("_payload"), sort_keys=True).encode() for e in h.ledger.events]
    out += h.svc.index_store.raw_objects()
    return b"\n".join(out)


def outputs(h) -> bytes:
    """Every non-owner view the routes answer for the pipeline: the job views and /department."""
    out = [json.dumps(h.svc.link_index_job_view(j), sort_keys=True).encode() for j in h.svc.link_index_jobs]
    out.append(h.get("/department").content)
    return b"\n".join(out)


def kinds_logged(h) -> list:
    return [r["kind"] for r in h.svc.log.iter_records()]
