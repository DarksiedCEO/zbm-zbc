"""The Marcus index pipeline's jobs (marcus-pipeline-spec/1; CLARIFICATIONS v1.1 GP-1..GP-6): the orchestration of the
five v1 services in src/marcus/ (Release Watcher, WAT Fetcher, Link Extractor, Index Builder, Graph Importer).

  POST /seo/v1/link-index/jobs           {request_id, service, crawl_ids?, paths?, release_id?, kind?, resume?}
                                         callers: dashboard, scheduler (department-internal)
  GET  /seo/v1/link-index/jobs/{job_id}  callers: dashboard, scheduler, compliance_38 (the view is machine facts only)

  service = cc_release_watcher  read the release list (``cc_releases`` port), record each new release once
            cc_index_builder    one snapshot: the WAT Fetcher delivers each file of the crawl(s) (or the targeted list,
                                path B), verified, to the Link Extractor; the Index Builder keeps the rows of the
                                registered target domains, writes the encrypted shards, re-reads and hash-checks every
                                one, then the manifest (last); ``resume`` continues an interrupted build after its last
                                recorded file
            cc_graph_importer   one graph release's rank file, kept to the current snapshot's hosts / domains

Rules (shared by every service): record-first (``link_index_job_requested`` on the ledger and the log before any port is
read, CB-7); ``link_index_job_recorded`` with the result's machine facts (it is also the publish point of a snapshot:
the manifest file is written just before it, and a manifest without its record is deleted at the next start);
``link_index_job_interrupted`` for a kill switch (KILLED_*, the partial result discarded), a restricted service
(AGENT_RESTRICTED), a crash (RUN_FAILED with a safe location) or a dead process (PROCESS_DIED, the interrupted-audits
job); kill switches ``global``, ``write``, ``capability:link_index`` and ``provider:commoncrawl`` before every step and
every read (CB-6); one running job per service (409 RUN_IN_PROGRESS) and request-key replays (CB-8). The index (rows,
shards, graph ranks, crawled-source sets) is never in the log, the ledger or a view (D-MP-01): those hold ids, counts,
keyed hashes and crawl ids; the index itself goes through marcus/index_store.py only (W-MP-2). Nothing here spends
anything or opens a socket (CB-12); the live ports are config.NOT_BUILT."""

from __future__ import annotations

import logging
import time
from typing import Optional

from clock import iso
from errors import Conflict, Forbidden, Invalid, NotFound, Unavailable
from ledger import derived_id
from marcus import graph_importer, index_builder, index_store, link_extractor, release_watcher, wat_fetcher
from primitives import Killed
from reasons import R

log = logging.getLogger("seo.link_index")
CAPABILITY, PROVIDER = "link_index", "commoncrawl"
OWN = "zbm"                                       # the index is ZBM's own (not tenant data, Q-MP-2)
SERVICES = (release_watcher.AGENT, wat_fetcher.AGENT, link_extractor.AGENT, index_builder.AGENT,
            graph_importer.AGENT)
NEEDS = {release_watcher.AGENT: (release_watcher.AGENT,),
         index_builder.AGENT: (wat_fetcher.AGENT, link_extractor.AGENT, index_builder.AGENT),
         graph_importer.AGENT: (graph_importer.AGENT,)}
JOB_CONTRACT = "link_index.job/1"
BASE_COUNTS = ("files", "files_quarantined", "files_resumed", "records", "records_selected", "records_malformed",
               "records_too_large", "records_duplicate", "records_skipped", "links", "rows_kept", "links_lookalike")
REFRESH_TIER = "monthly"                          # osei-spec/1 B-OS-30: Marcus's tier (the pipeline feeds Marcus)
METHODOLOGY = {
    release_watcher.AGENT: "the release list read through the cc_releases port (v1: fixtures, GP-1), validated "
                           "strictly; each new release recorded once; no data is fetched",
    index_builder.AGENT: "each WAT file of the crawl (or the targeted list) delivered gzip-streamed and verified by "
                         "the WAT Fetcher, parsed by the Link Extractor (normalised, scrubbed before a row exists), "
                         "the rows of the registered target domains kept, sharded, encrypted, re-read and "
                         "hash-checked, the manifest written last",
    graph_importer.AGENT: "one release's rank file (GP-3) streamed twice (node count, then rows), strict per row, "
                          "kept to the current snapshot's hosts or domains, encrypted at rest"}
LIMITATIONS = {
    release_watcher.AGENT: ["v1 reads a fixture release list only; the live port is not built"],
    index_builder.AGENT: ["v1 reads fixture WAT files only (no Common Crawl download, Q-MP-4)",
                          "rel is UNKNOWN unless a Phase-0 spike result is recorded (B-MP-24)",
                          "throughput is reported, not certified: the real numbers are the Phase-0 spike's"],
    graph_importer.AGENT: ["Common Crawl's published ranks; never a vendor metric, never rescaled"]}


def _location(step: str) -> str:
    return step.split(":", 1)[0] if step else "start"


class LinkIndexMixin:
    _mp_hook = None                               # tests only: called with each step name (crash / kill injection)

    def _init_link_index(self) -> None:
        self.link_index_jobs: dict = {}
        self.cc_releases: dict = {}
        self.index_snapshots: dict = {}
        self.wat_done: dict = {}                  # snapshot id -> {path hash: progress}
        self.graph_ranks: dict = {}
        self.cc_spike: Optional[dict] = None
        self.backlink_reports: dict = {}          # Marcus's recorded reports (marcus-spec/1, not built): retention pins
        self.running_link_index: dict = {}       # service -> job id (memory only)

    def _open_index_store(self) -> None:
        try:
            ring = index_store.Keyring.from_vault(self.vault)
        except index_store.IndexKeyUnavailable:
            ring = None
        self.index_store = index_store.IndexStore(self.settings.data_dir, ring)

    # ------------------------------------------------------------------ replay

    def _a_link_index_job_requested(self, d, at):
        self.link_index_jobs[d["job_id"]] = {
            "job_id": d["job_id"], "service": d["service"], "status": "running", "outcome": None, "facts": {},
            "not_connected": [], "crawl_ids": list(d.get("crawl_ids") or []), "snapshot_id": d.get("snapshot_id"),
            "release_id": d.get("release_id"), "kind": d.get("kind"), "resume_of": d.get("resume_of"),
            "requested_at": at, "produced_at": None, "interrupted_code": None}

    def _a_link_index_job_recorded(self, d, at):
        j = self.link_index_jobs[d["job_id"]]
        j.update(status="recorded", outcome=d["outcome"], facts=dict(d["facts"]),
                 not_connected=list(d.get("not_connected") or []), produced_at=at)
        snap = d.get("snapshot")
        if snap:
            self.index_snapshots[snap["snapshot_id"]] = {**snap, "status": "published", "published_at": at,
                                                         "job_id": d["job_id"], "order": len(self.index_snapshots)}
        g = d.get("graph")
        if g:
            self.graph_ranks[(g["release_id"], g["kind"])] = {**g, "imported_at": at, "job_id": d["job_id"]}

    def _a_link_index_job_interrupted(self, d, at):
        j = self.link_index_jobs[d["job_id"]]
        j.update(status="interrupted", outcome=d["outcome"], interrupted_code=d["code"],
                 facts=dict(d.get("facts") or {}), produced_at=at)

    def _a_cc_release_seen(self, d, at):
        self.cc_releases[f"{d['kind']}:{d['release_id']}"] = {"id": d["release_id"], "kind": d["kind"],
                                                              "published": d["published"], "at": at}

    def _a_wat_file_done(self, d, at):
        self.wat_done.setdefault(d["snapshot_id"], {})[d["path_hash"]] = dict(d)

    def _a_wat_file_quarantined(self, d, at):
        pass

    def _a_index_snapshot_retired(self, d, at):
        s = self.index_snapshots.get(d["snapshot_id"])
        if s is not None:
            s["status"] = "retired"

    def _a_cc_spike_recorded(self, d, at):
        self.cc_spike = {"spike_ref": d["spike_ref"], "rel_capture": d["rel_capture"], "at": at}

    # ------------------------------------------------------------------ views

    def link_index_job_view(self, job_id: str) -> dict:
        with self.lock:
            j = self.link_index_jobs.get(job_id)
            if j is None:
                raise NotFound(R("JOB_NOT_FOUND"))
            s = j["service"]
            return {"job_id": j["job_id"], "service": s, "status": j["status"], "outcome": j["outcome"],
                    "findings": [], "facts": dict(j["facts"]), "methodology": METHODOLOGY[s],
                    "limitations": list(LIMITATIONS[s]), "not_connected": list(j["not_connected"]),
                    "crawl_ids": list(j["crawl_ids"]), "snapshot_id": j["snapshot_id"],
                    "release_id": j["release_id"], "kind": j["kind"], "resume_of": j["resume_of"],
                    "interrupted_code": j["interrupted_code"], "requested_at": j["requested_at"],
                    "produced_at": j["produced_at"], "refresh_tier": REFRESH_TIER,
                    "evidence_ref": {"run_id": j["job_id"], "contract": JOB_CONTRACT, "item_key": s}}

    def complete_snapshots(self) -> list:
        """The published, unretired snapshots whose manifest is on the store with its recorded hash (oldest first):
        the only snapshots a reader (Marcus) may read."""
        with self.lock:
            snaps = sorted((s for s in self.index_snapshots.values() if s["status"] == "published"),
                           key=lambda s: s["order"])                     # the log's order
        out = []
        for s in snaps:
            try:
                self.index_store.get("manifest", s["snapshot_id"], s["manifest_sha256"])
            except (index_store.StoreCorrupt, index_store.IndexKeyUnavailable):
                continue
            out.append(dict(s))
        return out

    def link_index_scorecard(self, agent: str) -> dict:
        runs, outcomes = 0, {}
        for j in self.link_index_jobs.values():
            if j["status"] == "running" or agent not in NEEDS.get(j["service"], ()):
                continue
            runs += 1
            outcomes[j["outcome"]] = outcomes.get(j["outcome"], 0) + 1
        return {"runs": runs, "outcomes": dict(sorted(outcomes.items()))}

    # ------------------------------------------------------------------ the guard

    def _li_check(self, agents=()) -> None:
        with self.lock:
            code = self.kill_code(capability=CAPABILITY, provider=PROVIDER, write=True)
            if code is None and self._closed:
                code = "SERVICE_CLOSED"
            if code is None and any(self.agent_blocked(a) for a in agents):
                code = "AGENT_RESTRICTED"
        if code is not None:
            raise Killed(code)

    def _mp_step(self, ctx: dict, name: str) -> None:
        """A step boundary: the step is remembered (a crash's safe location), the test hook runs, then the kill
        switches and the services' lifecycle are checked (CB-6, CB-2)."""
        ctx["step"] = name
        hook = self._mp_hook
        if hook is not None:
            hook(name)
        self._li_check(ctx["agents"])

    def _li_hash(self, field: str, value: str) -> str:
        return self.vault.field_hash(OWN, field, value)[1]

    # ------------------------------------------------------------------ start (record-first)

    def link_index_start(self, caller: str, body: dict) -> dict:
        service = body["service"]
        if service not in NEEDS:
            raise Invalid(R("INVALID"), field="service")
        with self.lock:
            self._gate()
            self.check_kill(capability=CAPABILITY, provider=PROVIDER, write=True)
            if any(self.agent_blocked(a) for a in NEEDS[service]):
                raise Forbidden(R("AGENT_RESTRICTED"))
            rk = self.rk("link_index", service, body)
            prev = self._idem(caller, rk, body)
            if prev:
                return self.link_index_job_view(prev[1])
            if service in self.running_link_index:
                raise Conflict(R("RUN_IN_PROGRESS"))
            if service != release_watcher.AGENT and self.index_store.keyring is None:
                raise Unavailable(R("INDEX_KEY_UNAVAILABLE"))
            job_id = derived_id("lij", rk)
            data, paths = self._li_request(service, body, job_id)
            self._commit("link_index_job_requested", self._req(data, caller, rk, body, job_id), caller,
                         evidence=("link_index_job_requested", f"link_index:{job_id}", data, (caller, rk)))
            self.running_link_index[service] = job_id
        ctx = {"step": "", "agents": NEEDS[service], "job_id": job_id, "service": service, "paths": paths}
        run = {release_watcher.AGENT: self._li_watch, index_builder.AGENT: self._li_build,
               graph_importer.AGENT: self._li_graphs}[service]
        try:
            self._li_guarded(ctx, data, run)
        finally:
            with self.lock:
                self.running_link_index.pop(service, None)
        return self.link_index_job_view(job_id)

    def _li_request(self, service: str, body: dict, job_id: str) -> tuple:
        """Under the lock: the request checked against the recorded state; (the requested record's data, the targeted
        paths in memory)."""
        data = {"job_id": job_id, "service": service}
        if service == index_builder.AGENT:
            resume = body.get("resume")
            if resume is not None:
                j = self.link_index_jobs.get(resume)
                if j is None or j["service"] != index_builder.AGENT:
                    raise NotFound(R("JOB_NOT_FOUND"))
                if (j["status"] != "interrupted" or j["interrupted_code"] not in ("PROCESS_DIED", "RUN_FAILED")
                        or j["snapshot_id"] in self.index_snapshots
                        or any(x["resume_of"] == resume for x in self.link_index_jobs.values())):
                    raise Conflict(R("JOB_NOT_RESUMABLE"))
                return {**data, "crawl_ids": list(j["crawl_ids"]), "snapshot_id": j["snapshot_id"],
                        "resume_of": resume}, None
            crawls = list(body.get("crawl_ids") or [])
            if not crawls or len(set(crawls)) != len(crawls):
                raise Invalid(R("INVALID"), field="crawl_ids")
            if any(f"crawl:{c}" not in self.cc_releases for c in crawls):
                raise Conflict(R("RELEASE_NOT_RECORDED"))
            paths = body.get("paths")
            if paths is not None:
                try:
                    for p in paths:
                        m = wat_fetcher.WAT_PATH.fullmatch(p) if isinstance(p, str) else None
                        if m is None or m.group(1) not in crawls:
                            raise wat_fetcher.PathNotAllowed()
                        wat_fetcher.check_path(m.group(1), p)
                except wat_fetcher.PathNotAllowed:
                    raise Invalid(R("PATH_NOT_ALLOWED"), field="paths") from None
                if not paths or len(set(paths)) != len(paths):
                    raise Invalid(R("PATH_NOT_ALLOWED"), field="paths")
            return {**data, "crawl_ids": crawls, "snapshot_id": derived_id("snp", job_id),
                    "paths_count": len(paths) if paths is not None else None, "resume_of": None}, paths
        if service == graph_importer.AGENT:
            rel, kind = body.get("release_id"), body.get("kind")
            if kind not in release_watcher.GRAPH_KINDS or not isinstance(rel, str):
                raise Invalid(R("INVALID"), field="release_id")
            if f"{kind}:{rel}" not in self.cc_releases:
                raise Conflict(R("RELEASE_NOT_RECORDED"))
            return {**data, "release_id": rel, "kind": kind}, None
        return data, None

    def _li_guarded(self, ctx: dict, data: dict, run) -> None:
        """Run one job; a kill switch or a restricted service -> ``link_index_job_interrupted`` (KILLED), any other
        exception -> RUN_FAILED at its step (the exception's message is never kept)."""
        try:
            run(ctx, data)
        except Killed as k:
            self._li_discard(ctx, data, keep_stage=False)
            self._li_interrupt(ctx, "KILLED", k.code, {"location": _location(ctx["step"])})
        except Exception as exc:  # noqa: BLE001 - recorded as RUN_FAILED with a safe location code
            log.error("link index job failed: service=%s step=%s error=%s", ctx["service"], _location(ctx["step"]),
                      type(exc).__name__)
            self._li_discard(ctx, data, keep_stage=True)
            self._li_interrupt(ctx, "FAILED", "RUN_FAILED", {"location": _location(ctx["step"])})

    def _li_interrupt(self, ctx, outcome: str, code: str, facts: dict) -> None:
        with self.lock:
            d = {"job_id": ctx["job_id"], "service": ctx["service"], "outcome": outcome, "code": code,
                 "facts": facts}
            self._commit("link_index_job_interrupted", d, "seo",
                         evidence=("link_index_job_interrupted", f"link_index:{ctx['job_id']}", d,
                                   ("interrupted", ctx["job_id"])))

    def _li_record(self, ctx, outcome: str, facts: dict, not_connected=(), snapshot=None, graph=None) -> None:
        with self.lock:
            d = {"job_id": ctx["job_id"], "service": ctx["service"], "outcome": outcome, "facts": facts,
                 "not_connected": list(not_connected)}
            if snapshot is not None:
                d["snapshot"] = snapshot
            if graph is not None:
                d["graph"] = graph
            self._commit("link_index_job_recorded", d, "seo",
                         evidence=("link_index_job_recorded", f"link_index:{ctx['job_id']}", d,
                                   ("recorded", ctx["job_id"])))

    def mark_interrupted_link_index_jobs(self) -> int:
        """Jobs left ``running`` by a process that is gone are recorded interrupted (PROCESS_DIED); a build's staged
        files stay, so it can be resumed after its last recorded file (B-MP-13)."""
        n = 0
        with self.lock:
            mine = set(self.running_link_index.values())
            stale = [j for j in self.link_index_jobs.values() if j["status"] == "running" and j["job_id"] not in mine]
        for j in stale:
            self._li_interrupt({"job_id": j["job_id"], "service": j["service"]}, "FAILED", "PROCESS_DIED", {})
            n += 1
        return n

    def record_cc_spike(self, body: dict, andre: bool) -> dict:
        """B-MP-24 / GP-6: the record that a Phase-0 spike verified rel capture, with the owner's approval reference
        (Q-MP-4). No route in v1 (no spike is run); Andre only."""
        if not andre:
            raise Forbidden(R("ANDRE_APPROVAL_REQUIRED"))
        ref = body.get("approval_ref")
        if body.get("rel_capture") != "VERIFIED" or not isinstance(ref, str) or not index_builder.SHA256.fullmatch(ref):
            raise Invalid(R("INVALID"), field="approval_ref")
        with self.lock:
            self._gate()
            rk = self.rk("cc_spike", "rel_capture", body)
            if self._idem("andre", rk, body):
                return dict(self.cc_spike or {})
            d = {"spike_ref": ref, "rel_capture": "VERIFIED"}
            self._commit("cc_spike_recorded", self._req(d, "andre", rk, body, "cc_spike"), "andre",
                         evidence=("cc_spike_recorded", "cc_spike:" + ref, d, ("andre", rk)))
            return dict(self.cc_spike)

    # ------------------------------------------------------------------ Release Watcher (B-MP-01..03)

    def _li_watch(self, ctx: dict, data: dict) -> None:
        self._mp_step(ctx, "release_read")
        port = self.ports.cc_releases
        if not getattr(port, "connected", False):
            return self._li_record(ctx, "NOT_CONNECTED", {"reason": "NOT_CONNECTED"}, not_connected=["cc_releases"])
        crawls_raw, graphs_raw = port.read()
        self._mp_step(ctx, "release_parsed")
        facts = {"doc_source": port.doc_source, "read_date": port.read_date}
        try:
            releases = release_watcher.parse(crawls_raw, graphs_raw)
        except release_watcher.SourceInvalid as exc:
            import hashlib
            facts.update(reason=exc.code, size=len(crawls_raw) + len(graphs_raw),
                         list_sha256=hashlib.sha256(crawls_raw + b"\x00" + graphs_raw).hexdigest())
            return self._li_record(ctx, "QUARANTINED", facts)
        new = 0
        for r in releases:
            with self.lock:
                if f"{r['kind']}:{r['id']}" in self.cc_releases:
                    continue
                d = {"job_id": ctx["job_id"], "release_id": r["id"], "kind": r["kind"], "published": r["published"]}
                self._commit("cc_release_seen", d, "seo", evidence=(
                    "cc_release_seen", f"cc_release:{r['kind']}", {k: v for k, v in d.items() if k != "job_id"},
                    ("release", r["kind"], r["id"])))
                new += 1
        facts.update(releases_seen=len(releases), releases_new=new)
        self._li_record(ctx, "OK", facts)

    # ------------------------------------------------------------------ WAT Fetcher -> Link Extractor -> Index Builder

    def _li_plan(self, ctx: dict, data: dict, port) -> dict:
        """The build's file list and targets, fixed at the build's start and kept encrypted beside its staged files
        (a resumed build reads the same plan: same files, same order, same targets, B-MP-13 / B-MP-35)."""
        st, sid = self.index_store, data["snapshot_id"]
        if data.get("resume_of"):
            return st.get_json("stage", f"{sid}.plan")
        files = []
        for crawl in data["crawl_ids"]:
            if ctx["paths"] is not None:
                files += [[crawl, p] for p in ctx["paths"] if p.split("/")[1] == crawl]
            else:
                files += [[crawl, p] for p in wat_fetcher.parse_path_list(port.paths(crawl), crawl)]
        with self.lock:
            tgt = index_builder.targets(self.tenants)
        plan = {"files": files, "targets": tgt}
        st.put("stage", f"{sid}.plan", st.dumps(plan))
        return plan

    def _li_build(self, ctx: dict, data: dict) -> None:
        st, sid = self.index_store, data["snapshot_id"]
        t0 = time.monotonic()
        self._mp_step(ctx, "release_read")
        port = self.ports.cc_wat
        if not getattr(port, "connected", False):
            return self._li_record(ctx, "NOT_CONNECTED", {"reason": "NOT_CONNECTED"}, not_connected=["cc_wat"])
        try:
            plan = self._li_plan(ctx, data, port)
        except wat_fetcher.PathNotAllowed:
            return self._li_record(ctx, "BLOCKED", {"reason": "PATH_NOT_ALLOWED"})
        except wat_fetcher.Quarantined as q:
            return self._li_record(ctx, "BLOCKED", {"reason": q.code})
        tgt = plan["targets"]
        live = self.complete_snapshots()
        prev = live[-1] if live else None
        prev_sources = self._li_prev_sources(prev)
        with self.lock:
            spike = dict(self.cc_spike) if self.cc_spike else None
            done = dict(self.wat_done.get(sid, {}))
        counts = link_extractor.Counts({k: 0 for k in BASE_COUNTS})
        qreasons: dict = {}
        seen: set = set()
        by_dom: dict = {}
        for i, (crawl, path) in enumerate(plan["files"]):
            counts["files"] += 1
            ph = self._li_hash("wat_path", path)
            if ph in done:
                doc = st.get_json("stage", f"{sid}.{i}", done[ph]["stage_sha256"])
                counts["files_resumed"] += 1
                for k, v in done[ph]["counts"].items():
                    counts[k] += v
            else:
                doc = self._li_file(ctx, data, i, path, ph, tgt, spike is not None, counts, qreasons)
                if doc is None:
                    continue
            for key, row in doc["rows"]:
                by_dom.setdefault(key, []).append(row)
            seen.update(doc["sources"])
        parts = index_builder.split(by_dom)
        shards = []
        for dom, part, rows in parts:
            shard_id = derived_id("shd", sid, dom, part)
            sha = st.put("shard", shard_id, st.dumps({"snapshot_id": sid, "shard_id": shard_id, "domain": dom,
                                                      "rows": rows}))
            shards.append({"shard_id": shard_id, "sha256": sha, "domain": dom, "rows": len(rows)})
        self._mp_step(ctx, "shards_written")
        for j, (sh, (_d, _p, rows)) in enumerate(zip(shards, parts)):
            try:
                ok = st.read_shard(sid, sh["shard_id"], sh["sha256"]) == rows      # read back, hash checked
            except index_store.StoreCorrupt:
                ok = False
            if not ok:
                self._li_discard(ctx, data, keep_stage=False)
                return self._li_record(ctx, "FAILED", {"reason": "SHARD_VERIFY_FAILED"})
            self._mp_step(ctx, f"shard_verified:{j}")
        prev_id = prev["snapshot_id"] if prev else None
        crawled, missed = sorted(prev_sources & seen), sorted(prev_sources - seen)
        src_sha = st.put("sources", sid, st.dumps({"contract": index_builder.SOURCES_CONTRACT, "snapshot_id": sid,
                                                   "previous_snapshot_id": prev_id, "crawled": crawled,
                                                   "not_crawled": missed}))
        total = sum(s["rows"] for s in shards)
        mcounts = {k: int(counts[k]) for k in ("files", "files_quarantined", "files_resumed", "records",
                                               "records_selected", "links", "links_lookalike")}
        mcounts.update(rows=total, shards=len(shards))
        manifest = {"contract": index_builder.CONTRACT, "snapshot_id": sid, "crawls": list(data["crawl_ids"]),
                    "built_at": iso(self.now()),
                    "builder_version": {"version": index_builder.BUILDER_VERSION,
                                        "rel_capture": "VERIFIED" if spike else "UNVERIFIED",
                                        "spike_ref": spike["spike_ref"] if spike else None},
                    "shards": shards, "counts": mcounts, "licence_ack": index_builder.LICENCE_ACK, "complete": True,
                    "previous_snapshot_id": prev_id,
                    "crawled_sources": {"previous_snapshot_id": prev_id, "sha256": src_sha, "crawled": len(crawled),
                                        "not_crawled": len(missed)},
                    "key_id": st.keyring.current[0]}
        index_builder.validate_manifest(manifest)
        blob = st.encrypt("manifest", sid, st.dumps(manifest))
        self._mp_step(ctx, "manifest_built")
        msha = st.put_blob("manifest", sid, blob)                     # the last index write of the build
        self._mp_step(ctx, "manifest_written")
        elapsed = max(time.monotonic() - t0, 1e-6)
        facts = {k: (dict(v) if isinstance(v, dict) else v) for k, v in counts.items()}
        facts.update(quarantine_reasons=dict(sorted(qreasons.items())), rows=total, shards=len(shards),
                     snapshot_id=sid, crawled_sources=len(crawled), not_crawled_sources=len(missed),
                     throughput={"records_per_s": round(counts["records"] / elapsed, 1),
                                 "links_per_page": round(counts["links"] / max(1, counts["records_selected"]), 2),
                                 "label": "measured"})
        snap = {"snapshot_id": sid, "manifest_sha256": msha, "crawls": list(data["crawl_ids"]), "shards": len(shards),
                "rows": total, "previous_snapshot_id": prev_id}
        self._li_record(ctx, "PARTIAL" if counts["files_quarantined"] else "OK", facts, snapshot=snap)
        hook = self._mp_hook
        if hook is not None:
            hook("recorded")
        self._li_drop_stage(sid)
        self._li_retention()

    def _li_file(self, ctx, data, i, path, ph, tgt, rel_verified, counts, qreasons):
        """One WAT file: delivered (verified, streamed), extracted, filtered to the targets, staged encrypted, and
        recorded ``wat_file_done``; or quarantined (recorded, the job continues). Returns the staged document."""
        st, sid, port = self.index_store, data["snapshot_id"], self.ports.cc_wat
        self._mp_step(ctx, f"file_open:{i}")
        fc = link_extractor.Counts()
        stats: dict = {}
        srcs: set = set()
        size = 0
        try:
            fh = port.open(path)
        except wat_fetcher.PathNotAllowed:
            self._li_quarantine(ctx, sid, i, ph, "PATH_NOT_ALLOWED", 0, counts, qreasons)
            return None
        try:
            with fh:
                import os
                size = os.fstat(fh.fileno()).st_size
                lines = wat_fetcher.gz_lines(fh, check=lambda: self._li_check(ctx["agents"]), stats=stats)
                rows = list(link_extractor.extract_lines(
                    lines, fc, rel_verified, on_page=lambda u: srcs.add(self._li_hash("crawled_source", u))))
        except wat_fetcher.Quarantined as q:
            self._li_quarantine(ctx, sid, i, ph, q.code, size, counts, qreasons)
            return None
        self._mp_step(ctx, f"file_records:{i}")
        kept = []
        for r in rows:
            key, lookalike = index_builder.match(index_builder.host_of(r["dst"]), tgt)
            if key is not None:
                kept.append([key, r])
            elif lookalike:
                fc["links_lookalike"] += 1
        fc["rows_kept"] = len(kept)
        doc = {"rows": kept, "sources": sorted(srcs)}
        sha = st.put("stage", f"{sid}.{i}", st.dumps(doc))
        self._mp_step(ctx, f"stage_written:{i}")
        with self.lock:
            d = {"job_id": ctx["job_id"], "snapshot_id": sid, "path_hash": ph, "file_index": i,
                 "records": fc["records"], "bytes": stats.get("decoded", 0), "stage_sha256": sha,
                 "counts": dict(sorted(fc.items()))}
            self._commit("wat_file_done", d, "seo", evidence=("wat_file_done", f"link_index:{ctx['job_id']}", d,
                                                             ("wat", sid, ph)))
        self._mp_step(ctx, f"file_done:{i}")
        for k, v in fc.items():
            counts[k] += v
        return doc

    def _li_quarantine(self, ctx, sid, i, ph, reason, size, counts, qreasons) -> None:
        counts["files_quarantined"] += 1
        qreasons[reason] = qreasons.get(reason, 0) + 1
        with self.lock:
            d = {"job_id": ctx["job_id"], "snapshot_id": sid, "path_hash": ph, "file_index": i, "reason": reason,
                 "size": size}
            self._commit("wat_file_quarantined", d, "seo", evidence=(
                "wat_file_quarantined", f"link_index:{ctx['job_id']}", d, ("quarantine", ctx["job_id"], ph)))

    def _li_prev_sources(self, prev) -> set:
        """The keyed hashes of every source page of the previous snapshot's slices (B-MP-31)."""
        if prev is None:
            return set()
        st, sid = self.index_store, prev["snapshot_id"]
        man = st.read_manifest(sid)
        out = set()
        for sh in man["shards"]:
            for r in st.read_shard(sid, sh["shard_id"], sh["sha256"]):
                out.add(self._li_hash("crawled_source", r["src"]))
        return out

    def _li_snapshot_objects(self, sid: str) -> list:
        st = self.index_store
        out = []
        if st.exists("manifest", sid):
            try:
                out += [("shard", s["shard_id"]) for s in st.read_manifest(sid)["shards"]]
            except (index_store.StoreCorrupt, index_store.IndexKeyUnavailable, KeyError, TypeError):
                pass
        return out

    def _li_drop_stage(self, sid: str) -> None:
        st = self.index_store
        for n in st.names("stage"):
            if n.startswith(sid + "."):
                st.delete("stage", n)

    def _li_discard(self, ctx, data, keep_stage: bool) -> None:
        """A build that did not finish leaves no complete manifest and no shard; its staged files are kept only when it
        may be resumed (a crash), never after a kill switch (the result is discarded, CB-6)."""
        sid = data.get("snapshot_id")
        if not sid or ctx["service"] != index_builder.AGENT:
            return
        st = self.index_store
        st.delete("manifest", sid)
        live = self._li_live_shards()
        for n in st.names("shard"):
            if n not in live:                             # this build's shards: no published snapshot names them
                st.delete("shard", n)
        st.delete("sources", sid)
        if not keep_stage:
            self._li_drop_stage(sid)

    def _li_live_shards(self) -> set:
        out = set()
        with self.lock:
            sids = [s["snapshot_id"] for s in self.index_snapshots.values() if s["status"] == "published"]
        for sid in sids:
            out.update(n for _k, n in self._li_snapshot_objects(sid))
        return out

    def _li_retention(self) -> None:
        """B-MP-34: the last ``SEO_MARCUS_SNAPSHOTS_KEPT`` complete snapshots stay; an older one is retired whole
        (recorded, then its manifest deleted first, then its shards and crawled-source set) unless a recorded report
        still references it (kept until that report's retention ends, Q-MP-3)."""
        keep = self.settings.marcus_snapshots_kept
        live = self.complete_snapshots()
        now = iso(self.now())
        with self.lock:
            pinned = {r.get("snapshot_id") for r in self.backlink_reports.values() if r.get("retain_until", "") > now}
        st = self.index_store
        for s in live[:max(0, len(live) - keep)]:
            sid = s["snapshot_id"]
            if sid in pinned:
                continue
            objects = self._li_snapshot_objects(sid)
            with self.lock:
                self._commit("index_snapshot_retired", {"snapshot_id": sid}, "seo", evidence=(
                    "index_snapshot_retired", f"link_index:{sid}", {"snapshot_id": sid}, ("retired", sid)))
            st.delete("manifest", sid)
            for kind, name in objects:
                st.delete(kind, name)
            st.delete("sources", sid)

    def link_index_sweep(self) -> None:
        """At start: a manifest without its record (a build that died between the two), shards no published snapshot
        names, and staged files of finished snapshots are deleted. Staged files of an interrupted build stay."""
        st = getattr(self, "index_store", None)
        if st is None or st.root is None:
            return
        with self.lock:
            live = {s["snapshot_id"] for s in self.index_snapshots.values() if s["status"] == "published"}
            finished = set(self.index_snapshots)
        for n in st.names("manifest"):
            if n not in live:
                st.delete("manifest", n)
        if st.keyring is not None:
            keep = self._li_live_shards()
            for n in st.names("shard"):
                if n not in keep:
                    st.delete("shard", n)
        for n in st.names("sources"):
            if n not in live:
                st.delete("sources", n)
        for n in st.names("stage"):
            if n.split(".", 1)[0] in finished:
                st.delete("stage", n)

    # ------------------------------------------------------------------ Graph Importer (B-MP-40..42)

    def _li_graphs(self, ctx: dict, data: dict) -> None:
        st = self.index_store
        rel, kind = data["release_id"], data["kind"]
        self._mp_step(ctx, "graph_read")
        port = self.ports.cc_wat
        if not getattr(port, "connected", False):
            return self._li_record(ctx, "NOT_CONNECTED", {"reason": "NOT_CONNECTED"}, not_connected=["cc_wat"])
        live = self.complete_snapshots()
        if not live:
            return self._li_record(ctx, "INSUFFICIENT_EVIDENCE", {"reason": "NO_COMPLETE_SNAPSHOT"})
        sid = live[-1]["snapshot_id"]
        man = st.read_manifest(sid)
        hosts = set()
        for sh in man["shards"]:
            for r in st.read_shard(sid, sh["shard_id"], sh["sha256"]):
                hosts.add(index_builder.host_of(r["src"]))
                hosts.add(index_builder.host_of(r["dst"]))
        hosts.discard("")
        keep = hosts if kind == "host_graph" else {d for d in map(index_builder.registrable, hosts) if d}
        path = graph_importer.rank_path(rel, kind)
        check = lambda: self._li_check(ctx["agents"])               # noqa: E731
        counts = graph_importer.Counts()
        try:
            nodes = -1
            with port.open(path) as fh:
                for x in wat_fetcher.gz_lines(fh, check=check):
                    if x:
                        nodes += 1
            self._mp_step(ctx, "graph_counted")
            with port.open(path) as fh:
                lines = (x.decode("ascii", "replace") if isinstance(x, bytes) else x
                         for x in wat_fetcher.gz_lines(fh, check=check))
                nodes, rows = graph_importer.parse_ranks(lines, keep, counts, max(nodes, 0), check=check)
        except wat_fetcher.PathNotAllowed:
            return self._li_record(ctx, "QUARANTINED", {"reason": "PATH_NOT_ALLOWED"})
        except (wat_fetcher.Quarantined, graph_importer.SourceInvalid) as q:
            return self._li_record(ctx, "QUARANTINED", {"reason": q.code})
        doc = graph_importer.validate_graph({"contract": graph_importer.CONTRACT, "release": rel, "kind": kind,
                                             "nodes": nodes, "snapshot_id": sid, "rows": dict(sorted(rows.items()))})
        name = derived_id("grk", rel, kind, sid)
        sha = st.put("graph", name, st.dumps(doc))
        self._mp_step(ctx, "graph_written")
        facts = {**{k: v for k, v in sorted(counts.items())}, "nodes": nodes, "rows_kept": len(rows),
                 "hosts_wanted": len(keep), "snapshot_id": sid}
        g = {"release_id": rel, "kind": kind, "name": name, "sha256": sha, "nodes": nodes, "rows": len(rows),
             "snapshot_id": sid}
        self._li_record(ctx, "PARTIAL" if counts["rows_invalid"] or counts["rows_duplicate"] else "OK", facts,
                        graph=g)
