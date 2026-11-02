"""Opt-in bootstrap candidate. No backup, recovery, adoption or admission protocol."""
import os,sys,signal,subprocess,re,stat
# -I excludes ambient import paths; this fixed root-owned package is trusted.
sys.path.insert(0,'/opt/zbm-maintenance')
from backup_config import child_environment,Config,MEMBERS

STAGE="validation"
def main():
    global STAGE
    if os.getuid()!=0 or os.environ.get('BACKUP_LAUNCH_ENABLED')!='1':
        raise ValueError('privileged opt-in required')
    member=os.environ.get('BACKUP_MEMBER')
    token=os.environ.get('BACKUP_COORD_TOKEN','')
    if member not in MEMBERS or not re.fullmatch('[0-9a-f]{64}',token):
        raise ValueError('member and dedicated coordination credential required')
    if not sys.argv[1:] or not sys.argv[1].startswith('/'):
        raise ValueError('trusted absolute application executable required')
    STAGE="environment-filter"
    incoming=dict(os.environ)
    # Known nonsecret bootstrap controls are not credentials or app settings.
    incoming.pop('BACKUP_LAUNCH_ENABLED',None)
    incoming.pop('BACKUP_MEMBER',None)
    env=child_environment(member,incoming,Config(enabled=True,coordination_token=token))
    env.update(PATH='/opt/venv/bin:/usr/local/bin:/usr/bin:/bin',HOME='/nonexistent',TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1')
    # Never share application secret directory. Require fresh private tmpfs directory.
    STAGE='secret-directory'
    path='/run/zbm-backup-secrets'
    st=os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or stat.S_IMODE(st.st_mode)!=0o700:
        raise ValueError('private backup secret directory required')
    STAGE="secret-create"
    fd=os.open(path+'/coordination',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        os.write(fd,token.encode('ascii'));os.fsync(fd)
    finally:os.close(fd)
    child=None;pending=False
    def stop(signum,frame):
        nonlocal pending
        pending=True
        if child is not None and child.poll() is None:child.terminate()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    # setpriv executes after clean env/descriptor boundary; clear every capability.
    STAGE="child-launch"
    child=subprocess.Popen(['/usr/bin/setpriv','--reuid=10001','--regid=10001','--clear-groups','--no-new-privs','--bounding-set=-all','--inh-caps=-all','--ambient-caps=-all','--',*sys.argv[1:]],env=env,close_fds=True)
    if pending and child.poll() is None:child.terminate()
    result=child.wait()
    return result if result>=0 else 128-result

if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        print('maintenance launcher: failed at '+STAGE,file=sys.stderr)
        sys.exit(1)
