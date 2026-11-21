"""Narrow retired-owner reboot eligibility, before any shutdown or boot rewrite."""
import copy,hashlib
from owner_wire import Refused,canonical,MEMBERS
from metadata_retirement import read

def eligible(journal,member,old_boot,last_success):
 if type(journal)is not dict or journal.get('member')!=member or journal.get('boot')!=old_boot or journal.get('run')is not None or journal.get('operations')!={} or journal.get('last_success')!=last_success:raise Refused('idle retired journal required')
 if not last_success or set(last_success)!={'run','catalog_sha256','catalog_snapshot'}:raise Refused('last good catalog required')
 last=journal.get('last_retirement')
 if not last or last['record']['phase']!='DONE' or last['record']['action']!='retire' or last['record']['receipt']['result']!=last_success:raise Refused('exact completed retirement required')
 if journal.get('retired_resolution_sha256'):raise Refused('unarchived repair overlay')
 return hashlib.sha256(canonical(journal)).hexdigest()

def intent(old,boots):
 if old.get('phase')!='OPEN' or old.get('receipts')!={} or old.get('seals')!={} or old.get('barrier')is not None or not old.get('last_success') or old.get('schedule') is not None:raise Refused('only idle advanced coordinator may rearm')
 if set(boots)!=set(MEMBERS) or set(old.get('boots',{}))!=set(MEMBERS):raise Refused('full roster required')
 for b in list(boots.values())+list(old['boots'].values()):
  if type(b)is not str or len(b)!=32 or any(c not in '0123456789abcdef' for c in b):raise Refused('boot identity')
 if boots==old['boots']:raise Refused('no owner restart')
 v={'schema':'idle-rearm-v1','run':old['run'],'old_boots':old['boots'],'boots':boots,'last_success':old['last_success'],'original_sha256':hashlib.sha256(canonical(old)).hexdigest()}
 v['identity']=hashlib.sha256(canonical(v)).hexdigest();return v

def current_journal(o,permission):
 j=o.journal
 if j.temp.exists() or __import__('owner_wire').decode(read(j.path,524288))!=j.doc:raise Refused('journal disk mismatch')
 return eligible(j.doc,o.member,permission['old_boots'][o.member],permission['last_success'])
