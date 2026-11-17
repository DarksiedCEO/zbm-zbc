"""Audited click-to-reveal of the tenant's OWN text (core-spec/2 RV-1..RV-4, V-4, V-5; owner defaults Q2-5, Q2-6;
card C2-14).

  service API  ``reveal_span(actor, tid, surface, rid, span)`` -> {"value": <the span's text>}
        surface  closed enum ``SURFACES`` (``citation_run``: the owner's view of a recorded citation run, rid = cid)
        span     the braced B-CD-01 span code the owner's view listed (``{c:text:<16 hex>}``, CV-5 ``spans``)

Live wiring defaults OFF (``settings.reveal``, absent = OFF): OFF, every reveal is refused before anything is read or
audited. ON, each attempt is ONE audited commit: the typed audit row (actor, tenant, time, span code brace-less (T-2),
surface, outcome) is recorded on the ledger FIRST (``_commit`` evidence, event ``span_revealed``), then the log line
``span_reveal`` - and ONLY THEN is the value returned. ``_commit`` failing (ledger / store down, closed, integrity)
raises: no value. The value is in no audit row, log line, cache or ledger (RV-3); the route answers ``no-store``.
Another tenant's run, an unknown run, a span code not in the run's owner view, a legacy inline result: the SAME 404
``NOT_FOUND`` (no oracle; audited ``not_found``). Rate cap (Q2-6): ``RATE_CAP`` attempts per (actor, tenant) per
rolling hour, every attempt counted (refusals too; replayed from the log after a restart); over it -> 429
``REVEAL_RATE_LIMITED``, audited ``rate_limited``. K-1 strong keys inside a span of the tenant's own text are
revealable (Q2-5). A field the view masks WHOLE (any v1 finding, F8-2) lists no span code and is not revealable here.
Span codes are recomputed from the decrypted document in memory, in the same code context and allowlist scope as the
owner's view (svc_citations.citation_run_view): a code from another process or another tenant matches nothing."""
from __future__ import annotations

import re
from datetime import timedelta

from errors import Invalid, NotFound, Throttled, Unavailable
from reasons import R

LOG_KIND = "span_reveal"
AUDIT_EVENT = "span_revealed"
SURFACES = ("citation_run",)
OUTCOMES = ("revealed", "not_found", "rate_limited")
RATE_CAP = 100
WINDOW = timedelta(hours=1)
SPAN_RX = re.compile(r"\{c:text:([0-9a-f]{16})\}\Z")
INDEX_RUNS = 256                                     # run span indexes kept in memory (positions only)


class RevealMixin:
    """State: ``self._reveal_hits`` {(actor, tid): [attempt times]} (memory; rebuilt from the log on replay)."""

    def _reveal_state(self) -> dict:
        hits = getattr(self, "_reveal_hits", None)
        if hits is None:
            hits = self._reveal_hits = {}
        return hits

    def reveal_span(self, actor: str, tid: str, surface, rid, span) -> dict:
        if getattr(self.settings, "reveal", False) is not True:
            raise Unavailable(R("INVALID"))              # live wiring OFF (default): nothing is read or audited
        self._gate()
        m = SPAN_RX.match(span) if isinstance(span, str) else None
        if surface not in SURFACES or m is None or not isinstance(rid, str):
            raise Invalid(R("INVALID"))
        with self.lock:
            self._get(self.tenants, tid, "TENANT_NOT_FOUND")
        now = self.now()
        with self.lock:
            hits = self._reveal_state().setdefault((actor, tid), [])
            hits[:] = [t for t in hits if now - t < WINDOW]
            over = len(hits) >= RATE_CAP
            # every attempt counts (enumeration is capped too): the audited commit below appends it to ``hits`` via
            # ``_a_span_reveal`` (the same path replay takes), so it is not appended here as well (C2-14b: no double
            # count). An attempt whose audit fails returns no value and is not counted.
        value = None if over else self._reveal_find(tid, rid, span)
        outcome = "rate_limited" if over else ("revealed" if value is not None else "not_found")
        code = "c:text:" + m.group(1)                    # T-2: the brace-less reference form at a sink
        at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        row = {"tenant_id": tid, "actor": actor, "at": at, "span": code, "surface": surface, "outcome": outcome}
        data = {"tenant_id": tid, "span": code, "surface": surface, "outcome": outcome}
        # audit FIRST: raises (LEDGER_UNAVAILABLE / STORE_UNAVAILABLE / ...) -> the value is never returned
        self._commit(LOG_KIND, data, actor, evidence=(AUDIT_EVENT, tid, row, (tid, actor, at, code)))
        if over:
            raise Throttled(R("REVEAL_RATE_LIMITED"))
        if value is None:
            raise NotFound(R("NOT_FOUND"))
        return {"value": value}

    def _a_span_reveal(self, d, at):
        """Replay: the attempt counts against the cap again (a restart does not reset the hour)."""
        from datetime import datetime, timezone
        try:
            t = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if isinstance(at, str) \
                else at
        except ValueError:
            return
        self._reveal_state().setdefault((d.get("actor"), d.get("tenant_id")), []).append(t)

    def _reveal_find(self, tid: str, rid: str, span: str):
        """The span's text from the tenant's OWN recorded citation result, or None (never raises for a miss).

        C2-14c (RV-4 at N >= 1,000): the run's span index {span code -> (string ordinal, start, end)} is built ONCE per
        (tenant, run, blob ref, blob hmac, code context, codes key id, allowlist scope) by the same callum._display
        pass as the owner's view, so a reveal costs O(1) in prior reveals (was a full re-display per reveal). The index
        holds POSITIONS only - no revealed text is kept in memory between reveals and none goes to any sink or cache
        (RV-3): the value is sliced from the document decrypted for THIS reveal. Any change of blob, coder, key id or
        allowlist set rebuilds it; a restart starts empty (memory only)."""
        from agents import callum
        from svc_citations import RESULT_FIELD, _result_document
        with self.lock:
            r = self.citation_runs.get(rid)
            if not self._owns(r, tid):
                return None
            ref, mac, inline = r.get("result_ref"), r.get("result_hmac"), r.get("result")
        if inline is not None or ref is None:
            return None                                  # legacy inline result: not revealable (fail closed)
        doc = _result_document(self._vaulted_open(tid, RESULT_FIELD, ref, mac))
        if doc is None:
            return None
        strings = list(_strings(doc, ""))
        # V-3 (C2-14d): the same TENANT coder and allowlist scope as the owner's view (svc_citations._owner_coding), so
        # a code the view listed is found here, in this process or after a restart, and never under another tenant
        with self._owner_coding(tid), self._cv_scope(tid):
            fp = _scope_fp()
            idx = self._reveal_index_state()
            hit = idx.get((tid, rid))
            if hit is None or hit[0] != ref or hit[1] != mac or not _same_fp(hit[2], fp):
                pos: dict = {}
                for i, (name, value) in enumerate(strings):
                    d = callum._display(name, value)
                    for s in (d or {}).get("spans") or ():
                        pos.setdefault(s.get("code"), (i, s["start"], s["end"]))   # first match wins (as before)
                with self.lock:
                    idx.pop((tid, rid), None)
                    while len(idx) >= INDEX_RUNS:
                        idx.pop(next(iter(idx)))         # bounded: oldest run index dropped
                    idx[(tid, rid)] = hit = (ref, mac, fp, pos)
        got = hit[3].get(span)
        if got is None:
            return None
        i, start, end = got
        return strings[i][1][start:end]

    def _reveal_index_state(self) -> dict:
        idx = getattr(self, "_reveal_index", None)
        if idx is None:
            idx = self._reveal_index = {}
        return idx

def _strings(obj, name: str):
    """(field name, string) for every string in a decrypted document; list items carry their list's name."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, k if isinstance(k, str) else name)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v, name)
    elif isinstance(obj, str) and obj:
        yield name, obj


def _scope_fp() -> tuple:
    """What the span codes of a display depend on besides the document: the active code context (its coder object,
    own hosts, evidence map object; codes._fallback builds a new _Ctx per call around ONE process coder), its key id,
    and the active allowlist entry (coder, code set). Objects are held (not their id) so an id cannot be reused.
    C2-14d: ``vault.coder(tid)`` returns a NEW Coder per call, so a coder is compared by what its codes depend on
    (``_same_coder``: key id + tenant, the codes key being derived from exactly those) instead of by identity."""
    from agents import cv_allowlist
    from primitives import codes
    c = codes._ctx()
    return (c.coder, tuple(c.own or ()), c.evidence, codes.key_id(), cv_allowlist._ACTIVE.get())


def _same_fp(a: tuple, b: tuple) -> bool:
    ea, eb = a[4], b[4]
    same_allow = (ea is None and eb is None) or (ea is not None and eb is not None and _same_coder(ea[0], eb[0])
                                                 and ea[1] == eb[1])
    return _same_coder(a[0], b[0]) and a[1] == b[1] and a[2] is b[2] and a[3] == b[3] and same_allow


def _same_coder(x, y) -> bool:
    """The same codes: the same object, or two Coders of the same key id AND tenant (C2-14d; the per-tenant codes key
    is derived from the current key and the tenant id only). Anything else (incl. the process-random fallback vs a
    tenant coder) differs."""
    if x is y:
        return True
    kx, ky = getattr(x, "key_id", None), getattr(y, "key_id", None)
    tx, ty = getattr(x, "tenant_id", None), getattr(y, "tenant_id", None)
    return type(x) is type(y) and kx is not None and kx == ky and tx is not None and tx == ty
