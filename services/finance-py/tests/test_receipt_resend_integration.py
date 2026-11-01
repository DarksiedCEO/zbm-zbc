"""Owned complete receipt workflow; never contacts a provider."""
import copy
import secrets
from datetime import datetime, timezone

import pytest

import receipt_resend_adapter as bridge
from ports import ReceiptDeliveryOutcome
from helpers import Harness, ANDRE_TOKEN, rid
from test_service_sow import legal, draft, decide


@pytest.fixture
def world(tmp_path):
    h = Harness(data_dir=str(tmp_path/'finance')).ready()
    h.svc.ports.legal = legal()
    inv = h.ok(decide(h, draft(h)))['invoice']
    h.receive('zbm', '1010', inv['total'], inv['invoice_id'])
    rec, = h.svc.db['client_receipts'].values()
    h.clock.at = datetime.now(timezone.utc)
    h.ok(h.client.put('/fin/v1/clients/'+rec['client_id']+'/billing-contact',
               json={'request_id':rid(), 'email':'owned@example.test'}, headers=h.headers(andre=ANDRE_TOKEN)))
    adapter = bridge.ResendReceiptMail(bridge.profile('account', 'revision', 'Owned <from@example.test>'), secrets.token_hex(32), 1)
    h.svc.ports.client_mail = adapter
    return h, rec, adapter


def send(world):
    h, rec, _ = world
    return h.post('/fin/v1/client-receipts/'+rec['client_receipt_id']+'/send', {'request_id':rid()}, caller='scheduler')


def test_claim_precedes_io_and_private_acceptance(world, monkeypatch):
    h, rec, _ = world
    before=len(h.svc.entries)
    def answer(self, request, now):
        saved=h.svc.db['receipt_envelopes'][rec['client_receipt_id']]
        assert saved['schema']==bridge.SCHEMA
        assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status']=='sending'
        return ReceiptDeliveryOutcome('accepted', 'owned-private-id')
    monkeypatch.setattr(bridge.ResendReceiptTransport,'send_frozen',answer)
    out=send(world)
    assert out.status_code==200 and out.json()['sent']
    saved=h.svc.db['receipt_envelopes'][rec['client_receipt_id']]
    assert saved['acceptance']['provider_message_id']=='owned-private-id'
    assert 'owned-private-id' not in out.text+str(h.ledger.events)
    assert len(h.svc.entries)==before
    assert not send(world).json()['sent']


@pytest.mark.parametrize('outcome,identifier',[('retry',None),('accepted',None),('accepted','bad id'),('uncertain','unexpected')])
def test_real_typed_malformed_answers_are_uncertain(world,monkeypatch,outcome,identifier):
    h,rec,adapter=world
    monkeypatch.setattr(type(adapter),'deliver_receipt',lambda self,envelope:ReceiptDeliveryOutcome(outcome,identifier))
    out=send(world)
    assert out.status_code==200 and not out.json()['sent']
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status']=='delivery_uncertain'


def test_current_history_rejects_request_replacement(world,monkeypatch):
    import receipt_compatibility as compatibility
    h,rec,adapter=world
    monkeypatch.setattr(type(adapter),'deliver_receipt',lambda self,envelope:ReceiptDeliveryOutcome('uncertain'))
    send(world)
    records=list(h.svc.log.iter_records())
    assert not compatibility.assess_current(records)
    replacement=copy.deepcopy(h.svc.db['receipt_envelopes'][rec['client_receipt_id']])
    replacement['request']['idempotency_key']='replacement'
    records.append({'data':{'ops':[['put',{'coll':'receipt_envelopes','id':rec['client_receipt_id'],'rec':replacement}]]}})
    assert compatibility.assess_current(records)


@pytest.mark.parametrize('change',['schema','extra','body','digest','entity','profile','window','acceptance','unknown_nested'])
def test_complete_history_semantics(world,monkeypatch,change):
    import receipt_compatibility as compatibility
    h,rec,adapter=world
    monkeypatch.setattr(type(adapter),'deliver_receipt',lambda self,envelope:ReceiptDeliveryOutcome('uncertain'))
    send(world)
    records=copy.deepcopy(list(h.svc.log.iter_records()))
    saved=next(value['rec'] for record in records for kind,value in record['data'].get('ops',[])
               if kind=='put' and value.get('coll')=='receipt_envelopes')
    if change=='schema': saved['schema']='unknown'
    if change=='extra': saved['extra']='not supported'
    if change=='body': saved['request']['body']+=' '
    if change=='digest': saved['request']['body_sha256']='0'*64
    if change=='entity': saved['request']['entity']='zbc'
    if change=='profile': saved['request']['profile_id']=True
    if change=='window': saved['retry_until']='2099-01-01T00:00:00Z'
    if change=='acceptance': saved['acceptance']={'provider_message_id':'owned','accepted_at':'bad'}
    if change=='unknown_nested': saved['request']['extra']='unsupported'
    assert compatibility.assess_current(records)


def test_immutable_prior_profiles_and_old_reader_refusal(world,monkeypatch):
    import receipt_compatibility as compatibility
    h,rec,adapter=world
    monkeypatch.setattr(type(adapter),'deliver_receipt',lambda self,envelope:ReceiptDeliveryOutcome('accepted','private'))
    send(world)
    records=list(h.svc.log.iter_records())
    assert not compatibility.assess_current(records)
    for target in compatibility.TARGETS:
        assert compatibility.assess(records,target)
    saved=copy.deepcopy(h.svc.db['receipt_envelopes'][rec['client_receipt_id']])
    saved['acceptance']['provider_message_id']='different'
    records.append({'data':{'ops':[['put',{'coll':'receipt_envelopes','id':rec['client_receipt_id'],'rec':saved}]]}})
    assert compatibility.assess_current(records)


@pytest.mark.parametrize('state',['attempted','legacy_envelope','stale','fresh'])
def test_legacy_and_claim_states_never_send(world,monkeypatch,state):
    h,rec,adapter=world
    from clock import iso
    from datetime import timedelta
    current=h.svc.db['client_receipts'][rec['client_receipt_id']]
    if state in ('attempted','stale','fresh'): current['attempts']=1
    if state in ('stale','fresh'):
        current.update(status='sending', claimed_at=iso(h.clock.now()-timedelta(hours=1 if state=='stale' else 0)),claimed_by='owned-first')
    if state=='legacy_envelope':
        h.svc.db['receipt_envelopes'][rec['client_receipt_id']]={'envelope':{'to':'old@example.test'},'first_attempt_at':iso(h.clock.now())}
    monkeypatch.setattr(type(adapter),'deliver_receipt',lambda *args:pytest.fail('unapproved legacy send'))
    out=send(world)
    assert out.status_code==200 and not out.json()['sent']
    assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status']==('sending' if state=='fresh' else 'delivery_uncertain')


def test_zbc_default_is_not_promoted(world,monkeypatch):
    h,rec,adapter=world
    h.svc.db['client_receipts'][rec['client_receipt_id']]['entity']='zbc'
    monkeypatch.setattr(type(adapter),'deliver_receipt',lambda *args:pytest.fail('zbc not enabled'))
    assert send(world).json()['detail']=='entity_mail_disabled'
    assert not h.svc.db['receipt_envelopes']


@pytest.mark.parametrize('late',['withdrawn','delivery_reviewed'])
def test_late_acceptance_does_not_erase_review_or_withdrawal(world,monkeypatch,late):
    h,rec,adapter=world
    def answer(self,envelope):
        h.svc.db['client_receipts'][rec['client_receipt_id']].update(status=late,claimed_by=None)
        return ReceiptDeliveryOutcome('accepted','owned-late')
    monkeypatch.setattr(type(adapter),'deliver_receipt',answer)
    assert send(world).status_code==200
    current=h.svc.db['client_receipts'][rec['client_receipt_id']]
    assert current['status']==('withdrawn_after_send' if late=='withdrawn' else late)
    assert h.svc.db['receipt_envelopes'][rec['client_receipt_id']]['acceptance']['provider_message_id']=='owned-late'


def test_two_process_http_clients_share_one_writer_and_one_provider_call(world,monkeypatch):
    import json
    import socket
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import uvicorn
    import resend_receipts as transport
    from test_resend_receipts import worker_command
    h,rec,adapter=world
    h.svc.ports.client_mail=bridge.ResendReceiptMail(adapter.profile,adapter.credential,5)
    entered,release,ready=threading.Event(),threading.Event(),threading.Event()
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers['Content-Length'])))
            entered.set(); release.wait(5)
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"id":"owned-private-http-id"}')
    class Provider(ThreadingHTTPServer): daemon_threads=False
    provider=Provider(('127.0.0.1',0),Handler)
    provider_thread=threading.Thread(target=provider.serve_forever);provider_thread.start()
    monkeypatch.setattr(transport,'_worker_command',lambda:worker_command('m.ENDPOINT='+repr(f'http://127.0.0.1:{provider.server_port}/emails')+';m._worker_main()'))
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    port=sock.getsockname()[1]
    class Server(uvicorn.Server):
        async def startup(self,sockets=None):
            await super().startup(sockets=sockets);ready.set()
    server=Server(uvicorn.Config(h.app,log_level='critical',access_log=False,lifespan='off'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]));thread.start()
    children=[]
    code="import sys,json,httpx; d=json.load(sys.stdin); r=httpx.post(d['url'],headers=d['headers'],json=d['body'],trust_env=False,timeout=8); print(json.dumps({'status':r.status_code,'body':r.json()}))"
    def client():
        child=subprocess.Popen([sys.executable,'-I','-B','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        children.append(child)
        data={'url':f'http://127.0.0.1:{port}/fin/v1/client-receipts/{rec["client_receipt_id"]}/send',
              'headers':h.headers(caller='scheduler'),'body':{'request_id':rid()}}
        child.stdin.write(json.dumps(data).encode());child.stdin.close();child.stdin=None
        return child
    try:
        assert ready.wait(5)
        first=client();assert entered.wait(5)
        second=client();raw,_=second.communicate(timeout=5)
        result=json.loads(raw)
        assert result['status']==200 and not result['body']['sent']
        assert h.svc.db['client_receipts'][rec['client_receipt_id']]['status']=='sending'
        assert first.pid != second.pid
        release.set();raw,_=first.communicate(timeout=5)
        assert json.loads(raw)['body']['sent']
        assert len(calls)==1
        assert 'owned-private-http-id' not in raw.decode()+str(h.ledger.events)
        assert h.svc.db['receipt_envelopes'][rec['client_receipt_id']]['acceptance']['provider_message_id']=='owned-private-http-id'
    finally:
        release.set()
        for child in children:
            if child.poll() is None: child.kill()
            child.wait()
            for pipe in (child.stdin,child.stdout):
                if pipe is not None: pipe.close()
        server.should_exit=True;thread.join(8);sock.close()
        provider.shutdown();provider.server_close();provider_thread.join(5)
        assert not thread.is_alive() and not provider_thread.is_alive()


@pytest.mark.parametrize('mutation',['first_body','first_acceptance','sent_without_id','pending_with_id'])
def test_history_binds_receipt_and_acceptance(world,monkeypatch,mutation):
    import receipt_compatibility as compatibility
    import hashlib,json
    h,rec,adapter=world
    monkeypatch.setattr(type(adapter),'deliver_receipt',lambda self,envelope:ReceiptDeliveryOutcome('accepted','owned'))
    send(world)
    records=copy.deepcopy(list(h.svc.log.iter_records()))
    envelope_rows=[value['rec'] for record in records for kind,value in record['data'].get('ops',[])
                   if kind=='put' and value.get('coll')=='receipt_envelopes']
    if mutation=='first_body':
        body=json.loads(envelope_rows[0]['request']['body']);body['text']='different business facts'
        raw=json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        envelope_rows[0]['request'].update(body=raw,body_sha256=hashlib.sha256(raw.encode()).hexdigest())
    if mutation=='first_acceptance': envelope_rows[0]['acceptance']=copy.deepcopy(envelope_rows[-1]['acceptance'])
    if mutation=='sent_without_id': envelope_rows[-1]['acceptance']=None
    if mutation=='pending_with_id':
        rows=[value['rec'] for record in records for kind,value in record['data'].get('ops',[])
              if kind=='put' and value.get('coll')=='client_receipts']
        rows[-1]['status']='pending_send'
    assert compatibility.assess_current(records)


@pytest.mark.parametrize('mutation',['window_forward','claim_time','last_attempt','claim_identity','attempt_count'])
def test_initial_frozen_window_matches_same_committed_claim(world,monkeypatch,mutation):
    import receipt_compatibility as compatibility
    from clock import iso,parse_iso
    from datetime import timedelta
    h,rec,adapter=world
    monkeypatch.setattr(type(adapter),'deliver_receipt',lambda self,envelope:ReceiptDeliveryOutcome('uncertain'))
    send(world)
    records=copy.deepcopy(list(h.svc.log.iter_records()))
    for record in records:
        ops=record['data'].get('ops',[])
        envelopes=[v['rec'] for k,v in ops if k=='put' and v.get('coll')=='receipt_envelopes']
        if not envelopes:continue
        claims=[v['rec'] for k,v in ops if k=='put' and v.get('coll')=='client_receipts']
        assert len(envelopes)==len(claims)==1
        if mutation=='window_forward':
            for key in ('first_attempt_at','retry_until'):
                envelopes[0][key]=iso(parse_iso(envelopes[0][key])+timedelta(days=1))
        if mutation=='claim_time':claims[0]['claimed_at']=iso(parse_iso(claims[0]['claimed_at'])+timedelta(seconds=1))
        if mutation=='last_attempt':claims[0]['last_attempt_at']=iso(parse_iso(claims[0]['last_attempt_at'])+timedelta(seconds=1))
        if mutation=='claim_identity':claims[0]['claimed_by']=None
        if mutation=='attempt_count':claims[0]['attempts']=0
    assert compatibility.assess_current(records)
