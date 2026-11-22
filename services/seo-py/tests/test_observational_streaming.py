"""Bounded file comparison correction; actual durable service/log, no network."""
import builtins
from pathlib import Path
import pytest
import sink_guard
from helpers import Harness

@pytest.fixture
def large(tmp_path,monkeypatch):
    monkeypatch.setattr(sink_guard,'_REGISTERED',set(sink_guard._REGISTERED))   # C2-6h: fixture-local registry
    h=Harness(tmp_path,data_dir=str(tmp_path/'data'))
    try:
        # C2-6h (ruling 02:15 PT F6f3-1, T-7): this fixture writes tenant_created itself (not via create_tenant), so it
        # registers the tenant first, in create_tenant's order; the unregistered-id refusal is unchanged
        sink_guard.register_tenant('large')
        # C2-2e re-pin (CL-5, T-3): the large line uses typed fields (tenant_created has no /padding[] field)
        h.svc._commit('tenant_created',{'tenant_id':'large','kind':'own'},'seo')
        h.svc._commit('tenant_domains_set',{'tenant_id':'large','domains':['site.test']*12000},'seo')
        assert max(map(len,h.svc.log._lines))>2*65536
        yield h
    finally:h.client.close();h.svc.close()


def bounded_reads(h,monkeypatch):
    original=builtins.open;reads=[]
    class Reader:
        def __init__(self,fh):self.fh=fh
        def __enter__(self):return self
        def __exit__(self,*args):return self.fh.__exit__(*args)
        def read(self,size=-1):
            assert 0<size<=65536,'unbounded/oversized observational log read'
            reads.append(size)
            return self.fh.read(size)
    def opening(path,*args,**kwargs):
        fh=original(path,*args,**kwargs)
        if str(path)==str(h.svc.log.path) and args==('rb',):return Reader(fh)
        return fh
    monkeypatch.setattr(builtins,'open',opening)
    return reads


def test_multi_chunk_exact_comparison(large,monkeypatch):
    before=Path(large.svc.log.path).read_bytes();events=len(large.ledger.events)
    reads=bounded_reads(large,monkeypatch)
    assert large.svc.observe_integrity()['integrity']['ok']
    assert reads.count(65536)>=4  # two chunks in both local snapshot passes
    assert Path(large.svc.log.path).read_bytes()==before
    assert len(large.ledger.events)==events


@pytest.mark.parametrize('mutation',['middle','newline','trailing'])
def test_exact_byte_and_boundary_mismatches_refuse(large,monkeypatch,mutation):
    path=Path(large.svc.log.path);raw=path.read_bytes()
    if mutation=='middle':raw=raw[:65536]+bytes([raw[65536]^1])+raw[65537:]
    elif mutation=='newline':raw=raw[:-1]
    else:raw+=b'extra'
    path.write_bytes(raw)
    reads=bounded_reads(large,monkeypatch)
    result=large.svc.observe_integrity()
    assert not result['integrity']['ok']
    assert result['integrity']['problem']=='local log differs from replayed state'
    assert reads and max(reads)<=65536
    assert path.read_bytes()==raw
