"""Reusable local-only Restic capture/copy/catalog/restore primitives.
Repository argument must be a path beneath an explicit owned root, or a
127.0.0.1 REST repository supplied by the quota server. Secrets stay in memory.
"""
import hashlib,json,os,shutil,subprocess,time
from pathlib import Path
from urllib.parse import urlsplit
from gate3_guardrails import durable,disk_bytes
RESTIC=Path('/opt/zbm-maintenance/bin/restic')


def inventory(root):
    result={}
    for path in sorted(Path(root).rglob('*')):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):raise ValueError('unsupported inventory entry')
        if path.is_file():result[str(path.relative_to(root))]={'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    return result


class Restic:
    def __init__(self,owned_root,password,transport_token='',scratch=None):
        self.root=Path(owned_root).resolve();self.password=password;self.transport_token=transport_token
        self.scratch=Path(scratch) if scratch else self.root/'scratch';self.scratch.mkdir(exist_ok=True)
        if not self.scratch.resolve().is_relative_to(self.root):raise ValueError('scratch outside owned root')
        self.env={'PATH':'/usr/bin:/bin','HOME':str(self.root),'TMPDIR':str(self.scratch),'RESTIC_PASSWORD':password,'RESTIC_FROM_PASSWORD':password,'RESTIC_REST_USERNAME':'synthetic','RESTIC_REST_PASSWORD':transport_token,'RESTIC_RETRY_LOCK':'0s'}
    def validate(self,repo):
        value=str(repo)
        if value.startswith('rest:'):
            u=urlsplit(value[5:])
            if u.scheme!='http' or u.hostname!='127.0.0.1' or u.username or u.password:raise ValueError('only local quota HTTP backend allowed')
        else:
            path=Path(value)
            if not path.is_absolute() or not path.resolve().is_relative_to(self.root):raise ValueError('repository outside owned local root')
        return value
    def command(self,repo,*args):return [str(RESTIC),'--no-cache','-r',self.validate(repo),*map(str,args)]
    def run(self,repo,*args,cwd=None,timeout=90):
        result=subprocess.run(self.command(repo,*args),env=self.env,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
        if result.returncode:
            detail=result.stderr.decode(errors='replace').replace(self.password,'[REDACTED]').replace(self.transport_token or '__NO_TOKEN__','[REDACTED]')
            raise RuntimeError('RESTIC_'+str(args[0]).upper()+'_FAILED: '+detail[:3000])
        return result.stdout
    def init(self,repo):self.run(repo,'init')
    def backup(self,repo,payload):
        lines=self.run(repo,'backup','--json','.',cwd=payload).splitlines()
        return next(json.loads(line)['snapshot_id'] for line in reversed(lines) if json.loads(line).get('message_type')=='summary')
    def verify(self,repo,snapshot,manifest=None):
        # --no-cache explicitly avoids all cache reuse. Full-data check remains bounded
        # by the tiny fixture repository, not claimed efficient for long retention.
        self.run(repo,'check','--read-data')
        entries=[json.loads(line) for line in self.run(repo,'ls','--json',snapshot).splitlines()]
        files={entry['path'].lstrip('/') for entry in entries if entry.get('type')=='file'}
        actual=json.loads(self.run(repo,'dump',snapshot,'/manifest.json'))
        if manifest is not None and actual!=manifest:raise ValueError('manifest differs')
        if files!=set(actual['files'])|{'manifest.json'}:raise ValueError('file coverage differs')
        for name,meta in actual['files'].items():
            if Path(name).is_absolute() or '..' in Path(name).parts:raise ValueError('unsafe manifest path')
            content=self.run(repo,'dump',snapshot,'/'+name)
            if len(content)!=meta['bytes'] or hashlib.sha256(content).hexdigest()!=meta['sha256']:raise ValueError('content differs')
        return actual
    def copy(self,source,destination,snapshot):
        self.run(destination,'copy','--from-repo',self.validate(source),snapshot)
        snapshots=json.loads(self.run(destination,'snapshots','--json'))
        matches=[s for s in snapshots if s.get('original',s['id'])==snapshot or s['id']==snapshot]
        if len(matches)!=1:raise ValueError('ambiguous copied snapshot')
        return matches[0]['id']
    def restore(self,repo,snapshot,destination,manifest):
        destination=Path(destination)
        if destination.exists() and any(destination.iterdir()):raise ValueError('restore requires empty destination')
        self.verify(repo,snapshot,manifest)
        self.run(repo,'restore',snapshot,'--target',destination)
        return destination


def bounded_stage(source,payload,manifest,maximum_bytes,spool,spool_limit):
    """Exact source size allowance, checked before and throughout staging.

    Caller owns the store lock. No symlinks/special files; every copy has a byte
    ceiling and post-copy digest. Staging completes before quota REST writes.
    """
    source=Path(source);payload=Path(payload)
    encoded=json.dumps(manifest,sort_keys=True).encode()
    maximum=sum(meta['bytes'] for meta in manifest['files'].values())+len(encoded)
    if maximum>maximum_bytes or disk_bytes(spool)+maximum>spool_limit:raise ValueError('capture payload budget refused')
    payload.mkdir(parents=True,exist_ok=False)
    remaining=maximum_bytes
    try:
        for name,meta in manifest['files'].items():
            if Path(name).is_absolute() or '..' in Path(name).parts:raise ValueError('unsafe source path')
            src=source/name;dst=payload/name;dst.parent.mkdir(parents=True,exist_ok=True)
            if src.is_symlink() or not src.is_file():raise ValueError('unsupported source')
            total=0;digest=hashlib.sha256()
            with src.open('rb') as inp,dst.open('xb') as out:
                while True:
                    chunk=inp.read(min(65536,meta['bytes']-total+1))
                    if not chunk:break
                    if total+len(chunk)>meta['bytes'] or len(chunk)>remaining:raise ValueError('source exceeded capture budget')
                    out.write(chunk);digest.update(chunk);total+=len(chunk);remaining-=len(chunk)
                out.flush();os.fsync(out.fileno())
            if total!=meta['bytes'] or digest.hexdigest()!=meta['sha256']:raise ValueError('source changed during staging')
        if len(encoded)>remaining:raise ValueError('manifest exceeds capture budget')
        with (payload/'manifest.json').open('xb') as out:out.write(encoded);out.flush();os.fsync(out.fileno())
    except Exception:
        shutil.rmtree(payload);raise
    return maximum


def write_catalog(restic,repo,staging,catalog):
    """Publish one encrypted complete catalog and prove exact readback."""
    staging=Path(staging);staging.mkdir()
    durable(staging/'catalog.json',catalog);manifest={'files':inventory(staging)};durable(staging/'manifest.json',manifest)
    snapshot=restic.backup(repo,staging);restic.verify(repo,snapshot,manifest)
    if json.loads(restic.run(repo,'dump',snapshot,'/catalog.json'))!=catalog:raise ValueError('catalog readback differs')
    shutil.rmtree(staging);return snapshot
