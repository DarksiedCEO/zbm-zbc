"""Validate receipts read from the exact owned child's parent-opened output."""
import json,re

def python_receipt(text,pid,instance,member,epoch):
 rows=re.findall(r'PYTHON_CLEAN_SHUTDOWN (\{[^\n]+\})',text)
 if len(rows)!=1:raise ValueError('missing or repeated clean receipt')
 p=json.loads(rows[0])
 if (type(p.get('pid')) is not int or p['pid']!=pid or p.get('instance')!=instance or p.get('member')!=member or p.get('epoch')!=epoch or p.get('clean') is not True or p.get('service_closed') is not True):raise ValueError('clean receipt context mismatch')
 return p

def ledger_receipt(text,pid):
 instances=re.findall(r'INSTANCE (\d+) ([0-9a-f]{32})',text)
 rows=re.findall(r'CLEAN_SHUTDOWN (\{[^\n]+\})',text)
 if len(instances)!=1 or len(rows)!=1:raise ValueError('missing or repeated Ledger receipt')
 p=json.loads(rows[0])
 if type(p.get('pid')) is not int or p['pid']!=pid or (str(pid),p.get('instance'))!=instances[0]:raise ValueError('Ledger receipt context mismatch')
 return p
