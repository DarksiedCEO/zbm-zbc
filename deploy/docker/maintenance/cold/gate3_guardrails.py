"""Extracted owner-local quota REST and durable filesystem primitives."""
import base64, contextlib, fcntl, hashlib, hmac, http.client, json, os, re, shutil, socket, threading, time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

from urllib.parse import urlsplit, unquote

def durable(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w') as out:
        json.dump(value,out,sort_keys=True);out.flush();os.fsync(out.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def disk_bytes(root):
    return sum(p.stat().st_size for p in Path(root).rglob('*') if p.is_file())

class LocalServer:
    def start(self,handler):
        self.http=ThreadingHTTPServer(('127.0.0.1',0),handler)
        self.http.daemon_threads=True
        self.url='http://127.0.0.1:'+str(self.http.server_port)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        return self
    def close(self):
        self.http.shutdown();self.http.server_close();self.thread.join(timeout=3)
    def __enter__(self):return self
    def __exit__(self,*_):self.close()

class QuietHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*_):pass
    def respond(self,status,obj=None,headers=None,raw=None):
        data=raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b'')
        self.send_response(status)
        for name,value in (headers or {}).items():self.send_header(name,value)
        self.send_header('Content-Length',str(len(data)));self.end_headers()
        if self.command!='HEAD':
            try:self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):pass
    def body(self,maximum=16384):
        if self.headers.get('Transfer-Encoding'):raise ValueError('chunked body refused')
        length=int(self.headers.get('Content-Length','0'))
        if length<0 or length>maximum:raise ValueError('body size refused')
        data=self.rfile.read(length)
        if len(data)!=length:raise ValueError('short body')
        return json.loads(data) if data else {}
    def authorized(self,token):
        return hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token)

class QuotaRepository(LocalServer):
    """Restic REST v2 stand-in with an atomic reservation before any body write.

    Scope is an OWNED spool tree: encrypted repo and bounded plaintext staging.
    No direct writer may mutate that tree concurrently except this server and
    staging, which must finish before Restic begins. All writes reserve their
    full Content-Length plus currently allocated spool bytes. Replacement
    temporarily counts both old and new files. No optimistic compression budget.
    """
    def __init__(self,root,spool,limit,token,minimum_free=0,read_block=None,entry_limit=8192,minimum_inodes=512):
        self.root=Path(root);self.spool=Path(spool);self.limit=limit;self.token=token
        self.minimum_free=minimum_free;self.lock=threading.RLock();self.reserved=0
        self.peak=0;self.refusals=0;self.read_block=read_block
        self.entry_limit=entry_limit;self.minimum_inodes=minimum_inodes;self.reserved_entries=0;self.active=0;self.accepting=True;self.closed=False;self.condition=threading.Condition(self.lock)
        self.root.mkdir(parents=True,exist_ok=True)
        owner=self
        class Handler(QuietHandler):
            def setup(self):
                super().setup();self.connection.settimeout(10)
            def path_info(self):
                expected='Basic '+base64.b64encode(('synthetic:'+owner.token).encode()).decode()
                if not hmac.compare_digest(self.headers.get('Authorization',''),expected):self.respond(401);return None
                path=unquote(urlsplit(self.path).path).strip('/')
                if path=='':return ('root',owner.root)
                if path=='config':return ('file',owner.root/'config')
                parts=path.split('/')
                if parts[0] not in ('data','index','snapshots','keys','locks'):self.respond(400);return None
                if len(parts)==1:return ('list',owner.root/parts[0])
                if len(parts)!=2 or not re.fullmatch('[0-9a-f]{64}',parts[1]):self.respond(400);return None
                file=owner.root/parts[0]
                if parts[0]=='data':file=file/parts[1][:2]
                return ('file',file/parts[1])
            def do_POST(self):
                with owner.condition:
                    if not owner.accepting:self.respond(503);self.close_connection=True;return
                    owner.active+=1
                try:self.post_body()
                finally:
                    with owner.condition:owner.active-=1;owner.condition.notify_all()
            def post_body(self):
                info=self.path_info()
                if info is None:return
                kind,path=info
                if kind=='root' and urlsplit(self.path).query=='create=true':
                    with owner.lock:
                        missing=sum(not (owner.root/f).exists() for f in ['data','index','snapshots','keys','locks'])
                        if not owner.entries_fit(missing):owner.refusals+=1;self.respond(507);return
                        for folder in ['data','index','snapshots','keys','locks']:(owner.root/folder).mkdir(exist_ok=True)
                    self.respond(200);return
                if kind!='file':self.respond(400);return
                try:
                    if self.headers.get('Transfer-Encoding'):raise ValueError('unknown size')
                    length=int(self.headers.get('Content-Length','-1'))
                    if length<0:raise ValueError('unknown size')
                except ValueError:self.respond(411);self.close_connection=True;return
                with owner.lock:
                    current=disk_bytes(owner.spool)
                    missing=1;parent=path.parent
                    while not parent.exists():missing+=1;parent=parent.parent
                    if not owner.entries_fit(missing):owner.refusals+=1;self.respond(507);self.close_connection=True;return
                    if current+owner.reserved+length>owner.limit or shutil.disk_usage(owner.spool).free<length+owner.minimum_free:
                        owner.refusals+=1;self.respond(507);self.close_connection=True;return
                    owner.reserved+=length;owner.reserved_entries+=missing
                    owner.peak=max(owner.peak,current+owner.reserved)
                tmp=path.with_name('.'+path.name+'.'+str(threading.get_ident())+'.tmp')
                try:
                    path.parent.mkdir(parents=True,exist_ok=True)
                    remaining=length
                    with tmp.open('xb') as out:
                        while remaining:
                            chunk=self.rfile.read(min(65536,remaining))
                            if not chunk:raise ValueError('short repository body')
                            out.write(chunk);remaining-=len(chunk)
                        out.flush();os.fsync(out.fileno())
                    with owner.lock:
                        os.replace(tmp,path)
                        fd=os.open(path.parent,os.O_RDONLY)
                        try:os.fsync(fd)
                        finally:os.close(fd)
                    self.respond(200)
                except (ValueError,OSError):self.respond(500)
                finally:
                    with owner.lock:
                        tmp.unlink(missing_ok=True);owner.reserved-=length;owner.reserved_entries-=missing
            def do_GET(self):
                info=self.path_info()
                if info is None:return
                kind,path=info
                if kind=='list':
                    items=[{'name':p.name,'size':p.stat().st_size} for p in path.rglob('*') if p.is_file() and re.fullmatch('[0-9a-f]{64}',p.name)] if path.exists() else []
                    self.respond(200,items,{'Content-Type':'application/vnd.x.restic.rest.v2'});return
                if kind!='file' or not path.is_file():self.respond(404);return
                if owner.read_block and path.parent.parent.name=='data':
                    owner.read_block[0].set()
                    if not owner.read_block[1].wait(15):self.respond(503);return
                content=path.read_bytes();headers={'Content-Type':'application/octet-stream'}
                requested=self.headers.get('Range')
                if requested:
                    match=re.fullmatch(r'bytes=(\d+)-(\d*)',requested)
                    if not match:self.respond(416);return
                    start=int(match[1]);end=int(match[2]) if match[2] else len(content)-1
                    if start> end or start>=len(content):self.respond(416);return
                    end=min(end,len(content)-1);headers['Content-Range']=f'bytes {start}-{end}/{len(content)}'
                    self.respond(206,raw=content[start:end+1],headers=headers);return
                self.respond(200,raw=content,headers=headers)
            def do_HEAD(self):
                info=self.path_info()
                if info is None:return
                kind,path=info
                if kind!='file' or not path.is_file():self.respond(404);return
                self.respond(200,raw=path.read_bytes())
            def do_DELETE(self):
                with owner.condition:
                    if not owner.accepting:self.respond(503);self.close_connection=True;return
                    owner.active+=1
                try:self.delete_body()
                finally:
                    with owner.condition:owner.active-=1;owner.condition.notify_all()
            def delete_body(self):
                info=self.path_info()
                if info is None:return
                kind,path=info
                if kind!='file':self.respond(400);return
                with owner.lock:
                    if not path.exists():self.respond(404);return
                    path.unlink()
                self.respond(200)
        self.start(Handler)
    def entries_fit(self,additional):
        entries=list(self.spool.rglob('*'));fs=os.statvfs(self.spool)
        if any(p.is_symlink() for p in entries):return False
        return len(entries)+self.reserved_entries+additional<=self.entry_limit and fs.f_favail>=self.minimum_inodes+self.reserved_entries+additional
    def close(self):
        with self.condition:
            if self.closed:return
            self.accepting=False;deadline=time.monotonic()+12
            while self.active or self.reserved:
                left=deadline-time.monotonic()
                if left<=0:raise RuntimeError('quota writer drain unresolved')
                self.condition.wait(left)
            self.closed=True
        super().close()
    @property
    def repository(self):return 'rest:'+self.url+'/'
