"""Recoverable per-ingest key continuity; persistent SEO + fake ledger contract."""
import base64
import copy
import hashlib
import secrets
import threading

import pytest
from agents import logs as logs_mod
from helpers import Harness, rid
from ports import Ports

LINE = b'192.0.2.44 - - [10/Oct/2026:13:55:36 -0700] "GET /synthetic HTTP/1.1" 200 100 "-" "Googlebot/2.1"\n'

@pytest.fixture
def context(tmp_path):
    keys = []
    for name in ('original', 'replacement'):
        path = tmp_path / (name + '.key')
        path.write_text(secrets.token_hex(32)); path.chmod(0o600); keys.append(path)
    ports = Ports.default(); ports.fetcher = None
    h = Harness(tmp_path, data_dir=str(tmp_path/'data'), ports=ports, SEO_LOG_HASH_KEY_FILE=str(keys[0]))
    slot = [h]
    try:
        h.ok(h.post('/tenants/zbm/domains', {'request_id':rid(), 'domains':['site.test']}, andre=True))
        g = h.ok(h.post('/tenants/zbm/log-ingests', {'request_id':rid(), 'domain':'site.test', 'scheme':'https', 'format':'combined'}), 201)
        path = '/tenants/zbm/log-ingests/' + g['ingest_id']
        body = {'request_id':rid(), 'seq':1, 'last':False, 'data_b64':base64.b64encode(LINE).decode()}
        h.ok(h.post(path+'/chunks', body))
        yield slot, keys, g['ingest_id'], path, body
    finally:
        slot[0].client.close(); slot[0].svc.close()

def restart(slot, key):
    slot[0].client.close(); slot[0] = slot[0].restart(SEO_LOG_HASH_KEY_FILE=str(key)); return slot[0]

def state(h, iid):
    return (copy.deepcopy(h.svc.log.records), copy.deepcopy(h.ledger.events), copy.deepcopy(h.svc.log_ingests[iid]),
            {str(p.relative_to(h.settings.data_dir)):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in __import__('pathlib').Path(h.settings.data_dir).rglob('*') if p.is_file()})

def next_chunk(body):
    return {**body, 'request_id':rid(), 'seq':2}

def test_replacement_refused_before_processing_original_resumes(context, monkeypatch):
    slot,keys,iid,path,body = context
    h = restart(slot,keys[1]); before = state(h,iid)
    original = logs_mod.process_chunk
    calls = []
    def observe(*args, **kwargs):
        calls.append(True); return original(*args, **kwargs)
    monkeypatch.setattr(logs_mod, 'process_chunk', observe)
    second = next_chunk(body)
    h.refused(h.post(path+'/chunks', second),409,'LOG_HASH_KEY_MISMATCH')
    assert calls == [] and state(h,iid) == before
    h.ok(h.get(path)); assert state(h,iid) == before
    # Exact idempotent replay performs no new hashing or commits even under B.
    h.ok(h.post(path+'/chunks',body)); assert calls == [] and state(h,iid) == before
    h = restart(slot,keys[0]); h.ok(h.post(path+'/chunks',second))
    assert logs_mod.hll_estimate(h.svc.log_ingests[iid]['totals']['families']['Googlebot']['hll']) == 1
    assert len(h.ledger.of_type('log_chunk_ingested')) == 2
    h.ok(h.post(path+'/finish',{'request_id':rid()}))
    h = restart(slot,keys[1]); h.ok(h.get(path))
    assert h.svc.log_ingests[iid]['status'] == 'finished'

@pytest.mark.parametrize('fingerprint',[None,'','invalid','0'*16])
def test_missing_or_malformed_fingerprint_fails_closed(context, monkeypatch, fingerprint):
    slot,keys,iid,path,body = context; h = slot[0]
    # Explicit malformed historical-state simulation: do not infer a missing binding.
    if fingerprint is None: h.svc.log_ingests[iid].pop('key_fp')
    else: h.svc.log_ingests[iid]['key_fp'] = fingerprint
    before = state(h,iid)
    h.refused(h.post(path+'/chunks', next_chunk(body)),409,'LOG_HASH_KEY_MISMATCH')
    assert state(h,iid) == before

def test_key_swap_during_processing_refused_under_commit_lock(context, monkeypatch):
    slot,keys,iid,path,body = context; h = slot[0]
    before = state(h,iid); original = logs_mod.process_chunk; entered = threading.Event(); release = threading.Event(); result = {}
    def blocked(*args, **kwargs):
        delta = original(*args, **kwargs); entered.set(); assert release.wait(3); return delta
    monkeypatch.setattr(logs_mod,'process_chunk',blocked)
    second = next_chunk(body)
    def send():
        try: result['response'] = h.post(path+'/chunks',second)
        except BaseException as exc: result['exception'] = repr(exc)
    worker = threading.Thread(target=send); worker.start()
    try:
        assert entered.wait(3)
        with h.svc.lock: h.settings.log_hash_key = bytes.fromhex(keys[1].read_text())
    finally:
        release.set(); worker.join(4)
    assert not worker.is_alive() and 'exception' not in result
    h.refused(result['response'],409,'LOG_HASH_KEY_MISMATCH')
    assert state(h,iid) == before
    h.settings.log_hash_key = bytes.fromhex(keys[0].read_text())
    h.ok(h.post(path+'/chunks',second))
    assert logs_mod.hll_estimate(h.svc.log_ingests[iid]['totals']['families']['Googlebot']['hll']) == 1

def test_finish_existing_aggregate_under_new_key_does_not_rehash(context):
    slot,keys,iid,path,body = context; h = restart(slot,keys[1])
    old = copy.deepcopy(h.svc.log_ingests[iid]['totals']); fp = h.svc.log_ingests[iid]['key_fp']
    h.ok(h.post(path+'/finish',{'request_id':rid()}))
    assert h.svc.log_ingests[iid]['totals'] == old and h.svc.log_ingests[iid]['key_fp'] == fp
    assert len(h.ledger.of_type('log_chunk_ingested')) == 1
    h.ok(h.get(path))

def test_processing_uses_locked_key_snapshot_across_transient_swap(context, monkeypatch):
    slot,keys,iid,path,body = context; h = slot[0]
    original_guard = h.svc.guard; original_process = logs_mod.process_chunk
    key_a = h.settings.log_hash_key; used = []
    def swapped_guard(*args, **kwargs):
        guard = original_guard(*args, **kwargs)
        def wrapped(**kw):
            result = guard(**kw)
            h.settings.log_hash_key = bytes.fromhex(keys[1].read_text())
            return result
        return wrapped
    def processing(fmt, data, key, *args, **kwargs):
        used.append(key == key_a)
        try: return original_process(fmt,data,key,*args,**kwargs)
        finally: h.settings.log_hash_key = key_a
    monkeypatch.setattr(h.svc,'guard',swapped_guard)
    monkeypatch.setattr(logs_mod,'process_chunk',processing)
    h.ok(h.post(path+'/chunks',next_chunk(body)))
    assert used == [True]
    assert logs_mod.hll_estimate(h.svc.log_ingests[iid]['totals']['families']['Googlebot']['hll']) == 1
