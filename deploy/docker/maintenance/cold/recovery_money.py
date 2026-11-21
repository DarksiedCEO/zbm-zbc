"""Bounded first-client USD journal reconciliation against actual provider totals.
No expected fixture records; complete native Ledger/log evidence is required."""
import hashlib
import json
import re
from collections import defaultdict

class Refused(ValueError):
    pass

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def cents(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,9})\.[0-9]{2}', value):
        raise Refused('noncanonical_money')
    whole, fraction = value.split('.')
    return int(whole) * 100 + int(fraction)

def check(entries, ledger_events, *, finance_log, invoice_id, client_id, receipt_id, payment_intent,
          amount_cents, fee_cents, refund_cents):
    """One restored USD invoice/payment/fee and bounded monotonic refunds.

    All totals are mandatory and derived from restored invoice/provider reads.
    This verifier never chooses a product price or fee schedule.
    """
    if any(type(v) is not int or v < 0 for v in (amount_cents, fee_cents, refund_cents)):
        raise Refused('provider_amount')
    if not 0 <= refund_cents <= amount_cents or fee_cents >= amount_cents:
        raise Refused('provider_amount')
    if not isinstance(entries, list) or not 3 <= len(entries) <= 19:
        raise Refused('unexpected_entry_count')
    if not isinstance(ledger_events, list) or len(ledger_events) > 4096:
        raise Refused('ledger_inventory')
    events = {}
    for event in ledger_events:
        eid = event.get('event_id')
        if not isinstance(eid, str) or eid in events:
            raise Refused('duplicate_or_missing_event_id')
        events[eid] = event
    if not isinstance(finance_log, bytes) or not 0 < len(finance_log) <= 4*1024*1024 or not finance_log.endswith(b'\n'):
        raise Refused('finance_log_inventory')
    named = {}; prev = '0'*64; epoch = None
    for seq, raw in enumerate(finance_log.splitlines(), 1):
        rec = json.loads(raw)
        if json.dumps(rec,sort_keys=True,separators=(',',':')).encode() != raw or rec.get('seq') != seq or rec.get('prev_line_sha256') != prev:
            raise Refused('finance_log_chain')
        if rec.get('record_sha256') != digest({k:v for k,v in rec.items() if k != 'record_sha256'}):
            raise Refused('finance_log_hash')
        prev = hashlib.sha256(raw).hexdigest()
        if epoch is None: epoch = prev[:16]
        anchor = events.get(f'fin-log-{epoch}-{seq}-{prev[:40]}', {})
        proof = {'epoch':epoch,'seq':seq,'line_sha256':prev,'kind':rec['kind']}
        if (anchor.get('department'),anchor.get('event_type'),anchor.get('subject_id'),anchor.get('payload_sha256')) != ('finance','local_log_appended','fin-log',digest(proof)):
            raise Refused('finance_log_anchor')
        for item in rec['data'].get('evidence', []):
            if item['event_id'] in named: raise Refused('duplicate_named_evidence')
            named[item['event_id']] = dict(item,seq=seq)
    expected = {
        'F11': {('1100', 'client:'+client_id): amount_cents, ('4110', None): -amount_cents},
        'F13': {('1060', None): amount_cents, ('1100', 'client:'+client_id): -amount_cents},
        'F13f': {('5020', None): fee_cents, ('1060', None): -fee_cents},
        'F7r': {('1100', 'client:'+client_id): refund_cents, ('1060', None): -refund_cents},
    }
    if [e.get('memo_code') for e in entries[:3]] != ['F11','F13','F13f'] or any(e.get('memo_code')!='F7r' for e in entries[3:]):
        raise Refused('unexpected_flow_order')
    seen, ids, idem, head, balances = set(), set(), set(), None, defaultdict(int)
    cumulative_refund = 0
    for entry in entries:
        if entry.get('entity') != 'zbm':
            raise Refused('wrong_entity')
        flow = entry.get('memo_code')
        if flow not in expected or (flow in seen and flow != 'F7r'):
            raise Refused('unexpected_or_duplicate_flow')
        seen.add(flow)
        if flow == 'F7r':
            key = entry.get('idempotency_key', '')
            prefix = 'F7r|' + payment_intent + '|'
            if not key.startswith(prefix): raise Refused('refund_identity')
            cumulative = cents(key[len(prefix):])
            if not cumulative_refund < cumulative <= refund_cents: raise Refused('refund_progression')
            delta = cumulative-cumulative_refund; cumulative_refund=cumulative
            expected['F7r'] = {('1100','client:'+client_id):delta,('1060',None):-delta}
        for field, bag in [('entry_id', ids), ('idempotency_key', idem)]:
            value = entry.get(field)
            if not isinstance(value, str) or not value or value in bag:
                raise Refused('duplicate_or_missing_identity')
            bag.add(value)
        if entry.get('prev_entry_sha256') != head:
            raise Refused('journal_chain')
        head = digest({k:v for k,v in entry.items() if k != 'entry_sha256'})
        if entry.get('entry_sha256') != head:
            raise Refused('journal_hash')
        lines = entry.get('lines')
        if not isinstance(lines, list) or len(lines) != 2:
            raise Refused('posting_lines')
        vector, debits, credits = {}, 0, 0
        for line in lines:
            debit, credit = cents(line.get('debit')), cents(line.get('credit'))
            if (debit > 0) == (credit > 0):
                raise Refused('posting_side')
            key = (line.get('account'), line.get('subledger'))
            if key in vector:
                raise Refused('duplicate_posting_line')
            vector[key] = debit-credit
            debits += debit; credits += credit
            balances[key] += debit-credit
        if debits != credits or vector != expected[flow]:
            raise Refused('posting_amount_or_account')
        source = entry.get('source')
        if not isinstance(source, dict) or not source.get('id'):
            raise Refused('posting_source')
        if flow == 'F11' and source != {'kind':'invoice','id':invoice_id}:
            raise Refused('invoice_source')
        if flow in ('F13','F13f') and source != {'kind':'receipt','id':receipt_id}:
            raise Refused('payment_source')
        keys = {'F11':'F11|'+invoice_id, 'F13':'F13|'+payment_intent,
                'F13f':'F13f|'+payment_intent, 'F7r':f'F7r|{payment_intent}|{cumulative_refund//100}.{cumulative_refund%100:02d}'}
        if entry['idempotency_key'] != keys[flow]:
            raise Refused('wrong_operation_identity')
        if flow == 'F7r' and source != {'kind':'stripe_refund','id':receipt_id}:
            raise Refused('refund_source')
        fields = ('entity','lines','memo_code','source','idempotency_key','reverses_entry_id','approval_ref')
        try: content = digest({k:entry[k] for k in fields})
        except KeyError: raise Refused('missing_content') from None
        payload = {'entry_id':entry['entry_id'],'entity':'zbm','flow':flow,
                   'total':f'{debits//100}.{debits%100:02d}','content_sha256':content,
                   'lines':2,'source_kind':source.get('kind'),'source_id':source.get('id')}
        evidence = named.get(entry.get('ledger_event_id'), {})
        if evidence.get('event_type') != 'journal_entry_posted' or not isinstance(evidence.get('rk'), str):
            raise Refused('missing_committed_evidence')
        payload.update(rk=evidence['rk'],seq=evidence['seq'])
        if evidence.get('payload_sha256') != digest(payload): raise Refused('committed_payload')
        event = events.get(entry.get('ledger_event_id'), {})
        if (event.get('department'),event.get('event_type'),event.get('subject_id'),event.get('payload_sha256')) != ('finance','journal_entry_posted',entry['entry_id'],digest(payload)):
            raise Refused('ledger_binding')
    if cumulative_refund != refund_cents: raise Refused('refund_total')
    actual_postings = {e['event_id'] for e in ledger_events if e.get('department') == 'finance' and e.get('event_type') == 'journal_entry_posted'}
    if actual_postings != {e['ledger_event_id'] for e in entries}:
        raise Refused('unmatched_ledger_posting')
    if sum(balances.values()) != 0:
        raise Refused('imbalance')
    expected_balance = {('1100','client:'+client_id):refund_cents, ('4110',None):-amount_cents,
                        ('1060',None):amount_cents-fee_cents-refund_cents, ('5020',None):fee_cents}
    if dict(balances) != expected_balance:
        raise Refused('unexpected_balance')
    return {'journal_head':head,'entry_count':len(entries),'entries_digest':digest(entries),
            'debit_cents':sum(v for v in balances.values() if v > 0),
            'credit_cents':-sum(v for v in balances.values() if v < 0),
            'stripe_clearing_cents':balances[('1060',None)],'receivable_cents':refund_cents,
            'processing_fee_cents':fee_cents,'revenue_credit_cents':amount_cents}
