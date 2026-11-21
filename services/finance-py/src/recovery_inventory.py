"""Pure copied-byte inventory (ADR0041). No replay, I/O, ledger truth or restore authority."""
import hashlib
import json
from decimal import Decimal, InvalidOperation
import re
from datetime import datetime, timezone

from store import StoreCorrupt, verify_lines

MAX_LOG = 16 * 1024 * 1024
MAX_MANIFEST = 8192
MAX_LINE = 256 * 1024
MAX_RECORDS = 20000
MAX_NODES = 250000
MAX_DEPTH = 32
MAX_REPORT = 65536
TAGS = ('put','journal','integrity_finding','idem','proposal','decision','lease','reconcile',
        'founder_refused','injection','audit','evidence')
FIELDS = {'seq','kind','at','data','prev_line_sha256','record_sha256'}
MANIFEST_FIELDS = {'schema','declared_source_sha','declared_image_digest','backup_reference_sha256',
    'collected_at','cutoff_at','acquisition','expected_log_bytes','expected_log_sha256',
    'declared_last_seq','declared_tail_sha256'}


class InvalidCopy(ValueError):
    def __init__(self, code):
        self.code = code


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _digest(value, length=64):
    return type(value) is str and len(value)==length and bool(re.fullmatch('[0-9a-f]+',value))


def _time(value):
    if type(value) is not str or len(value)>40 or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)',value):
        raise InvalidCopy('INVALID_TIMESTAMP')
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError,OverflowError):
        raise InvalidCopy('INVALID_TIMESTAMP') from None


def _pairs(pairs):
    out={}
    for key,value in pairs:
        if key in out:
            raise InvalidCopy('DUPLICATE_KEY')
        out[key]=value
    return out


def _integer(value):
    if len(value)>64:
        raise InvalidCopy('NUMBER_LIMIT')
    return int(value)


def _float(value):
    if len(value)>64:
        raise InvalidCopy('NUMBER_LIMIT')
    try:
        result=Decimal(value)
    except InvalidOperation:
        raise InvalidCopy('NUMBER_LIMIT') from None
    if not result.is_finite():
        raise InvalidCopy('NONFINITE_NUMBER')
    # Conservative parser capability, not money arithmetic. Avoid binary conversion and JSON overflow.
    if result.copy_abs()>Decimal('1e308'):
        raise InvalidCopy('NUMBER_LIMIT')
    return result


def _constant(_):
    raise InvalidCopy('NONFINITE_NUMBER')


def _json(raw, budget):
    # Depth scan before the stdlib decoder. Strings/escapes do not count as structural brackets.
    depth=0;quoted=False;escaped=False
    for byte in raw:
        if quoted:
            if escaped: escaped=False
            elif byte==92: escaped=True
            elif byte==34: quoted=False
        elif byte==34: quoted=True
        elif byte in (91,123):
            depth+=1
            if depth>MAX_DEPTH: raise InvalidCopy('DEPTH_LIMIT')
        elif byte in (93,125): depth-=1
    try:
        value=json.loads(raw.decode('ascii'),object_pairs_hook=_pairs,parse_int=_integer,
                         parse_float=_float,parse_constant=_constant)
    except InvalidCopy:
        raise
    except (ValueError,UnicodeError,RecursionError,OverflowError):
        raise InvalidCopy('INVALID_JSON') from None
    stack=[value];nodes=0
    while stack:
        node=stack.pop();nodes+=1
        if nodes>budget: raise InvalidCopy('NODE_LIMIT')
        if type(node) is dict:
            nodes+=len(node)  # Keys also consume the aggregate budget.
            if nodes>budget: raise InvalidCopy('NODE_LIMIT')
            stack.extend(node.values())
        elif type(node) is list: stack.extend(node)
    return value,nodes


def _manifest(raw):
    if len(raw)>MAX_MANIFEST: raise InvalidCopy('MANIFEST_LIMIT')
    m,_=_json(raw,MAX_NODES)
    if type(m) is not dict or set(m)!=MANIFEST_FIELDS or m['schema']!='finance-copy-provenance-v1':
        raise InvalidCopy('MANIFEST_SCHEMA')
    if m['declared_source_sha'] is not None and not _digest(m['declared_source_sha'],40):
        raise InvalidCopy('MANIFEST_SCHEMA')
    image=m['declared_image_digest']
    if image is not None and (type(image) is not str or len(image)!=71 or not image.startswith('sha256:') or not _digest(image[7:])):
        raise InvalidCopy('MANIFEST_SCHEMA')
    if not _digest(m['backup_reference_sha256']) or not _digest(m['expected_log_sha256']):
        raise InvalidCopy('MANIFEST_SCHEMA')
    if type(m['expected_log_bytes']) is not int or not 0<=m['expected_log_bytes']<=MAX_LOG:
        raise InvalidCopy('MANIFEST_SCHEMA')
    if type(m['acquisition']) is not str or m['acquisition'] not in ('unknown','stopped_writer_declared','concurrent_writer_declared'):
        raise InvalidCopy('MANIFEST_SCHEMA')
    collected=_time(m['collected_at']);cutoff=None if m['cutoff_at'] is None else _time(m['cutoff_at'])
    if cutoff is not None and cutoff>collected: raise InvalidCopy('MANIFEST_SCHEMA')
    seq,tail=m['declared_last_seq'],m['declared_tail_sha256']
    if (seq is None)!=(tail is None): raise InvalidCopy('MANIFEST_SCHEMA')
    if seq is not None and (type(seq) is not int or not 1<=seq<=MAX_RECORDS or not _digest(tail)):
        raise InvalidCopy('MANIFEST_SCHEMA')
    m['collected_at']=collected.isoformat().replace('+00:00','Z')
    m['cutoff_at']=None if cutoff is None else cutoff.isoformat().replace('+00:00','Z')
    return m


def _base(identity):
    return {'schema':'finance-offline-inventory-v1','evaluator':identity,
        'inspected':{'log':None,'manifest':None},'declarations':None,
        'declared_binding':{'status':'NOT_EVALUATED','mismatches':[],'tail_declaration':'NOT_PROVIDED'},
        'chain':{'status':'NOT_EVALUATED','last_seq':None,'tail_sha256':None},
        'record_envelope_capability':'NOT_EVALUATED',
        'inventory':{'counts_complete':False,'records':None,'operations':None,'outer_kinds':None,'operation_tags':None},
        'diagnostics':{'items':[],'total':0,'complete':True},'evaluation':'INVALID_INPUT',
        'acquisition_consistency':'UNKNOWN','authenticity':'UNKNOWN','absent_tail_completeness':'UNKNOWN',
        'ledger_crosscheck':'NOT_PROVIDED','bank_truth':'NOT_PROVIDED','application_replay':'NOT_EVALUATED',
        'restore_authorized':False,'deployment_authorized':False}


def _diagnostic(report,code,index=None):
    d=report['diagnostics'];d['total']+=1
    if len(d['items'])<32: d['items'].append({'code':code,'record_index':index})
    else: d['complete']=False


def evaluate(raw,manifest_raw,identity):
    """All bytes are owned bounded copies. Helper reparsing is bounded, not streaming or replay."""
    out=_base(identity)
    if type(raw) is not bytes or type(manifest_raw) is not bytes:
        _diagnostic(out,'INPUT_UNAVAILABLE');return out
    if len(raw)>MAX_LOG or len(manifest_raw)>MAX_MANIFEST:
        _diagnostic(out,'BYTE_LIMIT');return out
    out['inspected']={'log':{'bytes':len(raw),'sha256':_sha(raw)},
                      'manifest':{'bytes':len(manifest_raw),'sha256':_sha(manifest_raw)}}
    try:
        m=_manifest(manifest_raw)
        out['declarations']={**m,'provenance':'DECLARED_UNVERIFIED'}
        mismatch=out['declared_binding']['mismatches']
        if m['expected_log_bytes']!=len(raw): mismatch.append('BYTE_COUNT')
        if m['expected_log_sha256']!=_sha(raw): mismatch.append('FILE_HASH')
        out['declared_binding']['status']='MISMATCH' if mismatch else 'MATCH'
        if not raw:
            out['chain']['status']='EMPTY';out['evaluation']='INCOMPLETE'
            _diagnostic(out,'EMPTY_LOG');return out
        if not raw.endswith(b'\n'): raise InvalidCopy('TORN_LINE')
        if raw.count(b'\n')>MAX_RECORDS: raise InvalidCopy('RECORD_LIMIT')
        lines=raw.split(b'\n')[:-1]
        records=[];nodes=0
        for index,line in enumerate(lines,1):
            if not line: raise InvalidCopy('BLANK_LINE')
            if len(line)>MAX_LINE: raise InvalidCopy('LINE_LIMIT')
            rec,count=_json(line,MAX_NODES-nodes);nodes+=count
            if type(rec) is not dict or set(rec)!=FIELDS: raise InvalidCopy('RECORD_SCHEMA')
            if type(rec['seq']) is not int or not 1<=rec['seq']<=MAX_RECORDS: raise InvalidCopy('RECORD_SCHEMA')
            if type(rec['kind']) is not str or not 1<=len(rec['kind'])<=64: raise InvalidCopy('RECORD_SCHEMA')
            _time(rec['at'])
            if not _digest(rec['prev_line_sha256']) or not _digest(rec['record_sha256']): raise InvalidCopy('RECORD_SCHEMA')
            data=rec['data']
            if type(data) is not dict or type(data.get('ops')) is not list: raise InvalidCopy('OPS_SCHEMA')
            for op in data['ops']:
                if (type(op) is not list or len(op)!=2 or type(op[0]) is not str or not 1<=len(op[0])<=64
                        or type(op[1]) is not dict): raise InvalidCopy('OPS_SCHEMA')
            records.append(rec)
        try:
            verify_lines(lines)  # Exact original lines WITHOUT LF; never reset genesis between records.
        except StoreCorrupt:
            out['chain']['status']='INVALID';raise InvalidCopy('CHAIN_INVALID') from None
        out['chain']={'status':'VALID','last_seq':len(records),'tail_sha256':_sha(lines[-1])}
        if m['declared_last_seq'] is not None:
            out['declared_binding']['tail_declaration']='EVALUATED'
            if m['declared_last_seq']!=len(records): mismatch.append('LAST_SEQUENCE')
            if m['declared_tail_sha256']!=_sha(lines[-1]): mismatch.append('TAIL_LINE_HASH')
        out['declared_binding']['status']='MISMATCH' if mismatch else 'MATCH'
        outer={k:0 for k in (*TAGS,'UNKNOWN')};ops=dict(outer);count=0
        for index,rec in enumerate(records,1):
            tag=rec['kind'] if rec['kind'] in TAGS else 'UNKNOWN';outer[tag]+=1
            if tag=='UNKNOWN': _diagnostic(out,'UNKNOWN_OUTER_KIND',index)
            for op in rec['data']['ops']:
                tag=op[0] if op[0] in TAGS else 'UNKNOWN';ops[tag]+=1;count+=1
                if tag=='UNKNOWN': _diagnostic(out,'UNKNOWN_OPERATION_TAG',index)
        out['inventory']={'counts_complete':True,'records':len(records),'operations':count,
                          'outer_kinds':outer,'operation_tags':ops}
        unsupported=outer['UNKNOWN'] or ops['UNKNOWN']
        out['record_envelope_capability']='UNSUPPORTED' if unsupported else 'SUPPORTED'
        out['evaluation']='INVALID_INPUT' if unsupported else 'INCOMPLETE' if mismatch else 'EVALUATED'
    except InvalidCopy as exc:
        out['record_envelope_capability']='UNSUPPORTED' if out['declarations'] is not None else 'NOT_EVALUATED'
        _diagnostic(out,exc.code)
    return out


def exit_code(report):
    return {'EVALUATED':0,'INVALID_INPUT':2,'INCOMPLETE':3,'OPERATIONAL_FAILURE':4}[report['evaluation']]
