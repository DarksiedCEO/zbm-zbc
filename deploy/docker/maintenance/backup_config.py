"""Dormant trusted config parser; performs no IO, process launch, or activation."""
from dataclasses import dataclass,field
from pathlib import Path,PurePosixPath
from collections.abc import Mapping
import json,re,stat

MEMBERS=frozenset(('ledger','finance','legal','compliance','seo'))
APP_NAMES={k:frozenset(v) for k,v in json.loads(Path(__file__).with_name('app_env_names.json').read_text()).items()}
BASE=frozenset(('PATH','HOME','LANG','LC_ALL','TZ','TMPDIR','PORT','PYTHONDONTWRITEBYTECODE','PYTHONUNBUFFERED','SSL_CERT_FILE','SSL_CERT_DIR'))
PREFIX={'ledger':'LEDGER_','finance':'FIN_','legal':'LEGAL_','compliance':'COMPLIANCE_','seo':'SEO_'}
BACKUP_PREFIX=('BACKUP_','B2_','RESTIC_','AWS_','RCLONE_')
RAW_FILES={'LEDGER_CALLERS','FIN_STRIPE_SECRET_KEY','FIN_STRIPE_WEBHOOK_SECRET','FIN_RECEIPT_RESEND_KEY','SEO_LOG_HASH_KEY','SEO_GSC_SERVICE_ACCOUNT'}
class Refused(ValueError): pass

def need(ok,msg):
    if not ok: raise Refused(msg)

@dataclass(frozen=True)
class Layout:
    source:str
    spool:str
    control:str
@dataclass(frozen=True)
class Config:
    enabled:bool=False
    layouts:tuple=()
    coordination_token:str=field(default='',repr=False,compare=False)

def path(value):
    need(isinstance(value,str) and value.startswith('/') and value!='/' and '\x00' not in value,'invalid absolute path')
    p=PurePosixPath(value)
    need(str(p)==value and all(x not in ('.','..') for x in value.split('/')[1:]),'noncanonical path')
    # lstat each existing component: resolve(strict=False) may leave a loop unchanged.
    for part in reversed((Path(value), *Path(value).parents)):
        try: mode=part.lstat().st_mode
        except FileNotFoundError: break  # safely absent future suffix, no creation
        except OSError: raise Refused('unreadable path component') from None
        need(not stat.S_ISLNK(mode),'symlink path component')
        need(stat.S_ISDIR(mode),'non-directory path component')
    need(str(Path(value).resolve(strict=False))==value,'aliased path')
    return p

def load(data):
    """Trusted decoded object; enabled requires exactly five directory triples."""
    need(isinstance(data,dict),'config must be object')
    need(set(data)<= {'enabled','members','coordination_token'},'unknown config field')
    enabled=data.get('enabled',False)
    need(type(enabled) is bool,'enabled must be boolean')
    if not enabled:
        need(not data.get('members') and not data.get('coordination_token'),'disabled config must not carry activation inputs')
        return Config()
    token=data.get('coordination_token')
    need(isinstance(token,str) and re.fullmatch('[0-9a-f]{64}',token) is not None,'dedicated coordination token required')
    members=data.get('members')
    need(isinstance(members,dict) and set(members)==MEMBERS,'exact five members required')
    seen=[]; layouts=[]
    for member in sorted(MEMBERS):
        row=members[member]
        need(isinstance(row,dict) and set(row)=={'source','spool','control'},'exact directory roles required')
        for role,value in row.items():
            p=path(value)
            need(all(p!=q and p not in q.parents and q not in p.parents for q in seen),'overlapping directories')
            seen.append(p)
        layouts.append((member,Layout(**row)))
    return Config(True,tuple(layouts),token)

def child_environment(member,environment,config):
    """Called AFTER existing secret-file materialization; no ambient inheritance.
    Unknown app-prefixed settings refuse instead of silently disabling app options.
    Values/paths are not logged, read or materialized here.
    """
    need(member in MEMBERS,'unknown member')
    need(isinstance(environment,Mapping),'environment must be mapping')
    forbidden_values={v for k,v in environment.items() if isinstance(k,str) and k.startswith(BACKUP_PREFIX) and isinstance(v,str) and v}
    if config.enabled: forbidden_values.add(config.coordination_token)
    result={}
    for key,value in environment.items():
        need(isinstance(key,str) and isinstance(value,str) and '\x00' not in key+value,'invalid environment entry')
        if key.startswith(BACKUP_PREFIX): continue
        need(key not in RAW_FILES,'raw application secret requires existing entrypoint materialization')
        if key in BASE or key in APP_NAMES[member]:
            need(not value or value not in forbidden_values,'application setting aliases backup credential')
            result[key]=value
        elif key.startswith(PREFIX[member]):
            raise Refused('unknown application setting; update trusted allowlist')
    return result
