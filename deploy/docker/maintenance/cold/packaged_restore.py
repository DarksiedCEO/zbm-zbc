"""Explicit selected-catalog data restore only. Never starts or opens applications."""
import sys,json,re,datetime,stat
from pathlib import Path
import remote_restore,restore_transport
import b2_adapter as b
from b2_catalog import CATBASE
ROOT=Path('/recovery')
LIMIT=16384

def read(stream):
 raw=stream.readline(LIMIT+2)
 if len(raw)>LIMIT+1 or not raw.endswith(b'\n'):raise b.Refused('input framing')
 def pairs(items):
  v={}
  for k,x in items:
   if k in v:raise b.Refused('duplicate input')
   v[k]=x
  return v
 return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(b.Refused('nonfinite')))

def selection(value):
 if type(value)is not dict or set(value)!={'locator','cutoff_utc'}:raise b.Refused('exact selection required')
 loc=value['locator'];keys={'schema','set_id','repository','snapshot','catalog_sha256','members_sha256','seals_sha256'}
 if type(loc)is not dict or set(loc)!=keys or loc['schema']!='b2-catalog-locator-v1' or type(loc['set_id'])is not str or not re.fullmatch('cold-[0-9a-f]{32}',loc['set_id']):raise b.Refused('locator schema')
 if loc['repository']!=CATBASE+loc['set_id'] or not all(b.hexid(loc[k]) for k in ('snapshot','catalog_sha256','members_sha256','seals_sha256')):raise b.Refused('locator binding')
 cutoff=value['cutoff_utc']
 if type(cutoff)is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z',cutoff):raise b.Refused('cutoff format')
 datetime.datetime.strptime(cutoff,'%Y-%m-%dT%H:%M:%SZ')
 return loc

def destinations():
 paths=[ROOT,ROOT/'scratch',ROOT/'targets',*[ROOT/'targets'/m for m in b.MEMBERS]]
 for p in paths:
  for ancestor in (p,*p.parents):
   if ancestor.is_symlink():raise b.Refused('destination alias')
  if not stat.S_ISDIR(p.lstat().st_mode):raise b.Refused('destination type')
 leaves=[ROOT/'scratch',*[ROOT/'targets'/m for m in b.MEMBERS]]
 ids=[(p.stat().st_dev,p.stat().st_ino) for p in leaves]
 if len(set(ids))!=len(ids) or any(any(p.iterdir()) for p in leaves):raise b.Refused('fresh isolated destinations required')

def execute(value):
 if type(value)is not dict or set(value)!={'schema','password','credentials','selection'} or value['schema']!='fixed-b2-data-restore-v1':raise b.Refused('fixed command schema')
 loc=selection(value['selection']);restore_transport.credentials(value['password'],value['credentials']);destinations()
 result=restore_transport.restore(value['password'],value['credentials'],loc)
 if result.get('set_id')!=loc['set_id'] or result.get('catalog_snapshot')!=loc['snapshot'] or result.get('catalog_sha256')!=loc['catalog_sha256'] or result.get('application_start')!='NOT_PERFORMED' or set(result.get('members',{}))!=set(b.MEMBERS):raise b.Refused('restore result binding')
 return {'schema':'closed-data-restore-v1','state':'DATA_RESTORED_CLOSED','selected_catalog':loc,'operator_cutoff_utc':value['selection']['cutoff_utc'],'cutoff_timestamp_authenticated':False,'data':result,'application_integrity':'NOT_PERFORMED','post_cutoff_payments':'UNRECONCILED','business_admission':'NOT_OPENED'}

def main(stream=None,out=None,argv=None):
 stream=sys.stdin.buffer if stream is None else stream;out=sys.stdout if out is None else out;argv=sys.argv[1:] if argv is None else argv
 try:
  if argv:raise b.Refused('no argv configuration')
  value=read(stream);result=execute(value);out.write(json.dumps(result,sort_keys=True,separators=(',',':'))+'\n');return 0
 except Exception:out.write('RESTORE_REFUSED\n');return 2
if __name__=='__main__':raise SystemExit(main())
