#!/usr/bin/env python3
"""Dormant operator-only provisioning. Default: no CLI, generation or prompts.

Apply is non-atomic and requires a separately authorized exclusive sitting.
A names-only run file blocks every repeat, including after uncertain interruption.
Never remove it to retry: reconcile remote state and prepare a newly approved run.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import uuid
import warnings
import credential_plan as c

AUTHORIZED_TARGET = {
    'project_id': '36763540-f7ab-45fb-a4e0-0947ce5a4baa',
    'environment_id': '54e4a004-ab96-441d-9264-ac63d92a5d79',
    'project_name': 'zbm-zbc',
    'environment_name': 'production',
}

class Refused(ValueError):
    pass

def need(ok):
    if not ok:
        raise Refused('provisioning refused; keep rollout HOLD')

def decode(raw):
    def unique(pairs):
        result={}
        for k,v in pairs:
            need(k not in result);result[k]=v
        return result
    need(type(raw) is str and len(raw)<=2_000_000)
    return json.loads(raw,object_pairs_hook=unique)

def validate(binding):
    need(type(binding) is dict and set(binding)=={'project_id','environment_id','project_name','environment_name','services'})
    # Refuse every other project/environment before CLI/version/read access.
    need(all(binding.get(k)==v for k,v in AUTHORIZED_TARGET.items()))
    for k in ('project_id','environment_id'):
        need(type(binding[k]) is str and str(uuid.UUID(binding[k]))==binding[k])
    for k in ('project_name','environment_name'):
        need(type(binding[k]) is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',binding[k]))
    roles={s for s,n in c.REQUIRED|set(c.URLS)}
    need(type(binding['services']) is dict and set(binding['services'])==roles)
    ids=[]
    for s,row in binding['services'].items():
        need(type(row) is dict and set(row)=={'id','complete_names','names'} and row['complete_names'] is True)
        need(type(row['id']) is str and str(uuid.UUID(row['id']))==row['id']);ids.append(row['id'])
        names=row['names'];need(type(names) is list and all(type(n) is str and re.fullmatch('[A-Z][A-Z0-9_]{0,127}',n) for n in names))
        need(len(names)==len(set(names)))
    need(len(ids)==len(set(ids)))

def checked_read(binding,service,read):
    data=read(service);need(type(data) is dict and len(data)<=2048)
    expected={'RAILWAY_PROJECT_ID':binding['project_id'],'RAILWAY_ENVIRONMENT_ID':binding['environment_id'],
              'RAILWAY_PROJECT_NAME':binding['project_name'],'RAILWAY_ENVIRONMENT_NAME':binding['environment_name'],
              'RAILWAY_SERVICE_ID':binding['services'][service]['id'],'RAILWAY_SERVICE_NAME':service}
    need(all(data.get(k)==v for k,v in expected.items()))
    need(all(type(k) is str and re.fullmatch('[A-Z][A-Z0-9_]{0,127}',k) and (v is None or type(v) is str and len(v)<=65536) for k,v in data.items()))
    return data

def materialize(value):
    if isinstance(value,c.Secret):return value._value
    if type(value) is dict:return {k:materialize(v) for k,v in value.items()}
    if type(value) is list:return [materialize(v) for v in value]
    return value

def checkpoint(fd,state):
    data=(json.dumps(state,sort_keys=True)+'\n').encode()
    os.lseek(fd,0,os.SEEK_SET)
    view=memoryview(data)
    while view:
        count=os.write(fd,view);need(count>0);view=view[count:]
    os.ftruncate(fd,len(data));os.fsync(fd)

def execute(binding,state_path,read,write,prompt,generate):
    """Only main wires real transports. Injected functions are offline test seams."""
    validate(binding)
    path=Path(state_path)
    # Exclusive, never-overwritten reservation precedes all remote writes.
    try:fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except OSError:raise Refused('existing or unavailable run record; no retry') from None
    state={'status':'PREFLIGHT','completed_names':[],'attempted_name':None}
    try:
        checkpoint(fd,state)
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(parent)
        finally:os.close(parent)
        snapshots={s:checked_read(binding,s,read) for s in sorted(binding['services'])}
        inventory={}
        for s,data in snapshots.items():
            names=set(binding['services'][s]['names'])
            # Sealed omission is present according to the complete external manifest.
            need(all(n in names or n.startswith('RAILWAY_') for n in data))
            # Validate trigger isolation against the COMPLETE manifest before
            # filtering planner inputs; sealed extras must not disappear.
            if s=='backup-trigger':
                need(all(n.startswith('RAILWAY_') or n in ('BACKUP_COORD_TOKEN','BACKUP_LAUNCH_ENABLED') for n in names))
                if 'BACKUP_LAUNCH_ENABLED' in names:
                    need(data.get('BACKUP_LAUNCH_ENABLED')=='0')
            for n in names:
                if (s,n) not in c.REQUIRED:continue
                value=data.get(n)
                if value is None:value=prompt(s+'.'+n)
                need(type(value) is str and bool(value))
                inventory[(s,n)]=c.Cell.supplied(value)
            for role in c.REQUIRED:
                if role[0]==s and role not in inventory:inventory[role]=c.Cell.absent()
        planned=c.plan(inventory)
        generated={}
        for role in planned._generation:
            need(role in c.GENERATED and role[1] not in binding['services'][role[0]]['names'])
            value=generate();c.token(value,role);generated[role]=value;inventory[role]=c.Cell.supplied(value)
        planned=c.plan(inventory);need(not planned._generation)
        changes={k:materialize(v) for k,v in planned._deltas.items()};changes.update(generated)
        for k,v in planned._urls.items():
            if snapshots[k[0]].get(k[1])!=v:changes[k]=v
        changes={k:json.dumps(v,separators=(',',':')) if type(v) is dict else v for k,v in changes.items()}
        need(all(type(v) is str and v and ('${{' not in v or c.URLS.get(k)==v) for k,v in changes.items()))
        # Full preflight completes before the first mutation; no bulk replacement.
        for s in snapshots:need(checked_read(binding,s,read)==snapshots[s])
        for (s,n),value in sorted(changes.items()):
            for other in snapshots:need(checked_read(binding,other,read)==snapshots[other])
            state.update(status='UNKNOWN',attempted_name=s+'.'+n);checkpoint(fd,state)
            write(s,n,value)
            expected={**snapshots[s],n:value}
            for other in snapshots:
                actual=checked_read(binding,other,read)
                need(actual==(expected if other==s else snapshots[other]))
            snapshots[s]=expected
            state['completed_names'].append(s+'.'+n);state.update(status='IN_PROGRESS',attempted_name=None);checkpoint(fd,state)
        state['status']='COMPLETE_HOLD';checkpoint(fd,state)
        return {'status':'COMPLETE_HOLD','written_names':tuple(state['completed_names'])}
    except BaseException:
        state['status']='UNKNOWN' if state['attempted_name'] or state['completed_names'] else 'REFUSED_NO_WRITES'
        try:checkpoint(fd,state)
        except BaseException:pass # surviving record still forbids automatic retry
        raise Refused('provisioning stopped; inspect names-only record; no automatic retry') from None
    finally:os.close(fd)

class CLI:
    def __init__(self,binding):
        validate(binding);self.binding=binding
        self.env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','USER','LANG','LC_ALL')}
    def call(self,args,value=None):
        try:r=subprocess.run(args,input=value,text=True,capture_output=True,env=self.env,timeout=90)
        except (OSError,subprocess.TimeoutExpired):raise Refused('CLI outcome unavailable') from None
        need(r.returncode==0);return r.stdout
    def command(self,s,action):
        return ['railway','variable',action,'--project',self.binding['project_id'],'--environment',self.binding['environment_id'],'--service',self.binding['services'][s]['id']]
    def read(self,s):return decode(self.call(self.command(s,'list')+['--json']))
    def write(self,s,n,v):self.call(self.command(s,'set')+[n,'--stdin','--skip-deploys'],v)
    def version(self):need(re.search(r'\b5\.71\.0\b',self.call(['railway','--version'])) is not None)

def hidden(label):
    need(sys.stdin.isatty())
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        return getpass.getpass(label+' existing value (hidden): ')

def openssl():
    try:r=subprocess.run(['openssl','rand','-hex','32'],capture_output=True,text=True,timeout=15,env={'PATH':os.environ.get('PATH','/usr/bin:/bin')})
    except (OSError,subprocess.TimeoutExpired):raise Refused('generation refused') from None
    need(r.returncode==0 and re.fullmatch('[0-9a-f]{64}\n?',r.stdout));return r.stdout.strip()

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--binding');parser.add_argument('--state')
    for flag in ('exclusive-sitting','auto-deploy-off','manual-sealing','url-cutover-approved','presence-manifest-current'):
        parser.add_argument('--confirm-'+flag,action='store_true')
    args=parser.parse_args(argv)
    if not args.apply:
        print('HOLD: no CLI access, generation, prompt or writes. Apply requires separately authorized operator sitting.');return 0
    need(all(getattr(args,'confirm_'+f) for f in ('exclusive_sitting','auto_deploy_off','manual_sealing','url_cutover_approved','presence_manifest_current')))
    need(args.binding and args.state)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0));os.umask(0o077)
    binding=decode(Path(args.binding).read_text());cli=CLI(binding);cli.version()
    result=execute(binding,args.state,cli.read,cli.write,hidden,openssl)
    print(json.dumps(result));return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except BaseException as exc:
        if isinstance(exc,SystemExit):raise
        print('STOP: provisioning refused or uncertain; rollout HOLD; no automatic retry.',file=sys.stderr)
        raise SystemExit(2) from None
