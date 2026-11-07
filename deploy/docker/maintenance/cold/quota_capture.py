"""Owner-local Restic capture through the exact owned loopback quota endpoint."""
import os,secrets
from pathlib import Path
from gate3_guardrails import QuotaRepository,disk_bytes
from bounded_process import Budget,run_fixed
import gate3_restic as engine
from member_limits import limits
from capacity_policy import capture_bytes,output_bytes
LIMIT=8*1048576;ENTRIES=8192;RESERVE=2*1048576

def preflight(backup,manifest):
 profile=limits(manifest['member']);limit=capture_bytes(manifest['member'])
 backup=Path(backup);backup.mkdir(mode=0o700,exist_ok=True)
 entries=list(backup.rglob('*'))
 if any(p.is_symlink() or not(p.is_file() or p.is_dir()) for p in entries):raise ValueError('backup entry refused')
 stage=sum(v['bytes'] for v in manifest['files'].values())+len(__import__('json').dumps(manifest,sort_keys=True).encode())
 required=stage+profile['repository']+profile['control_reserve']
 fs=os.statvfs(backup)
 if stage>profile['stage'] or disk_bytes(backup)+required>limit or len(entries)+512>ENTRIES or fs.f_bavail*fs.f_frsize<required+RESERVE or fs.f_favail<1024:raise ValueError('single-volume capture capacity refused')
 return {'retained_bytes':disk_bytes(backup),'stage_bytes':stage,'repository_allowance':profile['repository'],'control_reserve':profile['control_reserve'],'limit':limit,'device':backup.stat().st_dev}
class Restic(engine.Restic):
 def __init__(self,root,password,endpoint,token,member):
  super().__init__(root,password,token);self.endpoint=endpoint;self.profile=limits(member);self.budget=Budget.start(seconds=90,commands=64,output_bytes=output_bytes(member))
 def validate(self,repo):
  if str(repo)!=self.endpoint:raise ValueError('only current owned quota endpoint')
  return self.endpoint
 def run(self,repo,*args,cwd=None,timeout=90):
  return run_fixed([str(Path('/opt/zbm-maintenance/bin/restic')),'--no-cache','-r',self.validate(repo),*map(str,args)],env=self.env,cwd=cwd,budget=self.budget,output_limit=self.profile['source'] if args and args[0]=='dump' else 1048576)

def seal(source,backup,run,password,manifest):
 backup=Path(backup);proof=preflight(backup,manifest);root=backup/run
 if root.exists():raise ValueError('existing set requires repair')
 root.mkdir(mode=0o700);stage=root/'stage';repo=root/'sealed'
 profile=limits(manifest['member']);limit=capture_bytes(manifest['member'])
 engine.bounded_stage(source,stage,manifest,profile['stage'],backup,limit-profile['control_reserve'])
 token=secrets.token_hex(32)
 # Exact per-member retained+stage allowance includes replacement writes.
 q=QuotaRepository(repo,backup,min(limit-profile['control_reserve'],disk_bytes(backup)+profile['repository']),token,minimum_free=RESERVE,entry_limit=ENTRIES,minimum_inodes=512)
 try:
  r=Restic(root,password,q.repository,token,manifest['member']);r.init(q.repository);snapshot=r.backup(q.repository,stage);r.verify(q.repository,snapshot,manifest)
 finally:q.close() # failure to drain prevents return/guard release
 import shutil;shutil.rmtree(stage)
 return {'member':manifest['member'],'set_id':run,'snapshot':snapshot,'manifest':manifest,'repository_files':engine.inventory(repo)},root,{'quota_peak':q.peak,'quota_refusals':q.refusals,'active_writes':q.active,'reserved_bytes':q.reserved,'same_device':Path(source).stat().st_dev==backup.stat().st_dev,'preflight':proof}
