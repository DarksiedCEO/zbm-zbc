"""Fixed owner-local packaging profile. No process, filesystem writes or network."""
import hashlib,json,os,re,stat
from pathlib import Path
from backup_config import child_environment,Config,MEMBERS,APP_NAMES
PREFIX={'ledger':'LEDGER','finance':'FIN','legal':'LEGAL','compliance':'COMPLIANCE','seo':'SEO'}
COMMANDS={m:('/usr/local/bin/ledger-server',) if m=='ledger' else ('/opt/venv/bin/python','-m','api') for m in MEMBERS}
BACKUP_KEYS={'BACKUP_LAUNCH_ENABLED','BACKUP_MEMBER','BACKUP_COORD_TOKEN','BACKUP_RESTIC_PASSWORD','BACKUP_B2_KEY_ID','BACKUP_B2_APPLICATION_KEY','BACKUP_VOLUME','BACKUP_RECOVERY_MODE','BACKUP_CAPACITY_POLICY'}
class Refused(ValueError):pass

def config(env,argv):
 m=env.get('BACKUP_MEMBER');token=env.get('BACKUP_COORD_TOKEN','')
 recovery=env.get('BACKUP_RECOVERY_MODE','0')
 if recovery not in ('0','1'):raise Refused('explicit recovery selector required')
 if env.get('BACKUP_LAUNCH_ENABLED')!='1' or m not in MEMBERS or tuple(argv)!=COMMANDS[m]:raise Refused('fixed opt-in member/command required')
 if any(k.startswith('BACKUP_') and k not in BACKUP_KEYS for k in env):raise Refused('unknown backup setting')
 if not re.fullmatch('[0-9a-f]{64}',token):raise Refused('dedicated token required')
 password=env.get('BACKUP_RESTIC_PASSWORD','');keyid=env.get('BACKUP_B2_KEY_ID','');key=env.get('BACKUP_B2_APPLICATION_KEY','')
 if not 32<=len(password)<=128 or any(not 1<=len(v)<=4096 or '\x00' in v for v in (keyid,key)):raise Refused('supervisor credentials required')
 if len({token,password,keyid,key})!=4:raise Refused('credential separation required')
 if env.get('BACKUP_VOLUME') not in ('/data','/volume'):raise Refused('fixed one-volume layout required')
 from cold.capacity_policy import parse
 parse(env.get('BACKUP_CAPACITY_POLICY'))
 prefix=PREFIX[m];setting=prefix+('_LOG_PATH' if m=='ledger' else '_DATA_DIR');source=env['BACKUP_VOLUME']+'/'+m
 if env.get(setting)!=(source+'/ledger.jsonl' if m=='ledger' else source):raise Refused('exact member source required')
 if env.get('PORT','27001')!='27001' or env.get(prefix+'_PORT','27001')!='27001':raise Refused('external ingress must use27001')
 # Public selectors are not credentials; keep every credential alias check.
 public={'BACKUP_LAUNCH_ENABLED','BACKUP_MEMBER','BACKUP_VOLUME','BACKUP_RECOVERY_MODE'}
 filtered=child_environment(m,{k:v for k,v in env.items() if k not in public},Config(enabled=True,coordination_token=token))
 app={k:v for k,v in filtered.items() if k in APP_NAMES[m]}
 app.pop(prefix+'_PORT',None);app.pop(prefix+'_BIND_ADDR',None)
 return {'member':m,'recovery':recovery=='1','app_env':app,'coord_token':token,'backup_password':password,'runtime_inputs':{},'transport_credentials':{'key_id':keyid,'key':key}}

def prerequisites(cfg):
 from cold.volume_layout import member_root
 root=member_root(cfg['member'],cfg['app_env']);source=root/cfg['member']
 for path in (root,source):
  if path.is_symlink() or not path.is_dir() or path.resolve()!=path:raise Refused('canonical existing volume/source required')
 if not any(source.iterdir()):raise Refused('existing application store required')
 # Only an exact independently qualified application image may enable this owner.
 expected=json.loads(Path(__file__).with_name('application-prerequisites.json').read_text())
 if cfg['member']!='ledger':
  root=Path('/app/services')/(cfg['member']+'-py')/'src'
  for row in expected[cfg['member']]:
   name,digest=row['path'],row['sha256']
   p=root/name
   if p.is_symlink() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise Refused('application startup/shutdown source prerequisite missing')
