"""Copies-only offline CLI. No Service/API/provider imports, credentials or environment lookup."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

SRC=Path(__file__).resolve().parents[1]/'src'
sys.path.insert(0,str(SRC))
import recovery_inventory as inventory


class BoundaryError(Exception):
    def __init__(self,code): self.code=code


def _identity(st):
    return (st.st_dev,st.st_ino)


def _version(st):
    return (_identity(st),st.st_size,st.st_mtime_ns,st.st_ctime_ns,st.st_mode,st.st_uid,st.st_nlink)


def _parts(path):
    if type(path) is not str or len(path)>4096 or not path.startswith('/') or '\x00' in path:
        raise BoundaryError('PATH_REFUSED')
    parts=path.split('/')[1:]
    if not parts or len(parts)>64 or any(not p or p in ('.','..') for p in parts):
        raise BoundaryError('PATH_REFUSED')
    return parts


def _directory(path,stack,private=True):
    parts=_parts(path);flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK
    fd=os.open('/',flags);stack.callback(os.close,fd);links=[];ancestry=[_identity(os.fstat(fd))]
    for part in parts:
        parent=fd;fd=os.open(part,flags,dir_fd=parent);stack.callback(os.close,fd)
        st=os.fstat(fd)
        # Root/system intermediate directories may be public. Writable intermediates require root's sticky bit.
        if st.st_uid not in (0,os.getuid()) or (stat.S_IMODE(st.st_mode)&0o022 and not(st.st_uid==0 and st.st_mode&stat.S_ISVTX)):
            raise BoundaryError('DIRECTORY_REFUSED')
        links.append((parent,part,_identity(st)));ancestry.append(_identity(st))
    st=os.fstat(fd)
    if private and (st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700):
        raise BoundaryError('DIRECTORY_REFUSED')
    return fd,links,ancestry


def _links_unchanged(links):
    for parent,name,identity in links:
        if _identity(os.stat(name,dir_fd=parent,follow_symlinks=False))!=identity:
            raise BoundaryError('INPUT_CHANGED')


def _read_bounded(fd,cap):
    chunks=[];remaining=cap+1
    while remaining:
        chunk=os.read(fd,min(65536,remaining))
        if not chunk: break
        chunks.append(chunk);remaining-=len(chunk)
    raw=b''.join(chunks)
    if len(raw)>cap: raise BoundaryError('INPUT_LIMIT')
    return raw


def _acquire(path,stack):
    directory,links,ancestry=_directory(path,stack)
    directory_version=_version(os.fstat(directory))
    names=[]
    with os.scandir(directory) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names)>2: raise BoundaryError('BUNDLE_LAYOUT')
    if set(names)!={'finance-log.copy.jsonl','provenance.json'}: raise BoundaryError('BUNDLE_LAYOUT')
    files=[]
    for name,cap in [('finance-log.copy.jsonl',inventory.MAX_LOG),('provenance.json',inventory.MAX_MANIFEST)]:
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory);stack.callback(os.close,fd)
        st=os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600
                or st.st_nlink!=1): raise BoundaryError('INPUT_REFUSED')
        if st.st_size>cap: raise BoundaryError('INPUT_LIMIT')
        files.append((name,fd,st,cap))
    raw=[_read_bounded(fd,cap) for _,fd,_,cap in files]
    # Retained descriptors are checked together after both reads and on both sides of evaluation.
    def unchanged():
        _links_unchanged(links)
        if _version(os.fstat(directory))!=directory_version:
            raise BoundaryError('INPUT_CHANGED')
        for name,fd,before,_ in files:
            if (_version(os.fstat(fd))!=_version(before)
                    or _version(os.stat(name,dir_fd=directory,follow_symlinks=False))!=_version(before)):
                raise BoundaryError('INPUT_CHANGED')
    unchanged()
    return raw,ancestry,unchanged


def _evaluator():
    # Observed fixed files, not an attestation of imported/executed bytes or a signed image.
    paths={'core_sha256':SRC/'recovery_inventory.py','cli_sha256':Path(__file__), 'store_sha256':SRC/'store.py'}
    result={'capability':'finance-record-envelope-v1','python':[sys.version_info.major,sys.version_info.minor],
            'executed_artifact_attested':False}
    for key,path in paths.items():
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            st=os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size>1024*1024: raise BoundaryError('EVALUATOR_UNAVAILABLE')
            result[key]=hashlib.sha256(_read_bounded(fd,1024*1024)).hexdigest()
        finally: os.close(fd)
    return result


def _write_all(fd,data):
    offset=0
    while offset<len(data):
        count=os.write(fd,data[offset:])
        if count<=0: raise OSError('short write')
        offset+=count


def _publish(path,data,input_ancestry):
    parts=_parts(path);parent_path='/'+'/'.join(parts[:-1]);name=parts[-1]
    with ExitStack() as stack:
        parent,links,ancestors=_directory(parent_path,stack)
        if input_ancestry[-1] in ancestors: raise BoundaryError('OUTPUT_OVERLAP')
        _links_unchanged(links)
        # The existing parent is supplied, not created/adopted/chmodded. O_EXCL returns a descriptor
        # for the FILE this syscall created, even if its name is subsequently exchanged.
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        file_identity=None
        try:
            file_identity=_identity(os.fstat(fd))
            os.fchmod(fd,0o600)
            # Exclusive construction, NOT atomic visibility: partial private bytes can precede success.
            _write_all(fd,data);os.fsync(fd)
            version=_version(os.fstat(fd))
            os.fsync(parent)
            _links_unchanged(links)
            st=os.fstat(parent)
            if (st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700
                    or _version(os.stat(name,dir_fd=parent,follow_symlinks=False))!=version):
                raise BoundaryError('OUTPUT_UNKNOWN')
        except Exception:
            # Only the still-bound created inode may be unlinked. Never remove a replacement or directory.
            try:
                if file_identity is not None and _identity(os.stat(name,dir_fd=parent,follow_symlinks=False))==file_identity:
                    os.unlink(name,dir_fd=parent)
                os.fsync(parent)
            except OSError: pass
            raise BoundaryError('OUTPUT_UNKNOWN') from None
        finally:
            os.close(fd)


def run(copies_dir,report_file):
    publication_started=False
    try:
        # Acquisition descriptors remain retained through evaluation. No source files are written or claimed.
        with ExitStack() as stack:
            raw,ancestry,unchanged=_acquire(copies_dir,stack)
            identity=_evaluator()
            unchanged()
            result=inventory.evaluate(raw[0],raw[1],identity)
            unchanged()
            data=inventory.canonical(result)+b'\n'
            if len(data)>inventory.MAX_REPORT: raise BoundaryError('REPORT_LIMIT')
            publication_started=True
            _publish(report_file,data,ancestry)
        return {'evaluation':result['evaluation'],'report_written':True,'report_sha256':hashlib.sha256(data).hexdigest(),
                'reason':None,'residual_possible':False},inventory.exit_code(result)
    except BoundaryError as exc:
        return {'evaluation':'OPERATIONAL_FAILURE','report_written':False,'report_sha256':None,
                'reason':exc.code,'residual_possible':exc.code=='OUTPUT_UNKNOWN'},4
    except (OSError,ValueError,OverflowError,RecursionError,AttributeError,NotImplementedError):
        return {'evaluation':'OPERATIONAL_FAILURE','report_written':False,'report_sha256':None,
                'reason':'OUTPUT_UNKNOWN' if publication_started else 'OPERATIONAL_FAILURE',
                'residual_possible':publication_started},4


class Parser(argparse.ArgumentParser):
    def error(self,message): raise BoundaryError('INVALID_ARGUMENTS')


def main(argv=None):
    try:
        parser=Parser(add_help=False,allow_abbrev=False)
        parser.add_argument('--copies-dir',required=True);parser.add_argument('--report-file',required=True)
        args=parser.parse_args(argv)
        result,code=run(args.copies_dir,args.report_file)
    except BoundaryError:
        result={'evaluation':'INVALID_INPUT','report_written':False,'report_sha256':None,
                'reason':'INVALID_ARGUMENTS','residual_possible':False};code=2
    try: sys.stdout.write(json.dumps(result,sort_keys=True,separators=(',',':'))+'\n')
    except OSError: return 4
    return code


if __name__=='__main__':
    raise SystemExit(main())
