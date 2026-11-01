"""Owned copied evidence only; no recovery, provider or production operations."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT/'devtools/inventory_copies.py'
IDENTITY = {'capability':'finance-record-envelope-v1','core_sha256':'1'*64,'cli_sha256':'2'*64,
            'store_sha256':'3'*64,'python':[sys.version_info.major,sys.version_info.minor],
            'executed_artifact_attested':False}


def log_bytes(records=2, **change):
    from store import encode
    lines=[]
    previous='0'*64
    for n in range(records):
        row={'seq':n+1,'kind':'audit','at':'2026-11-01T00:00:00Z','data':{'ops':[['audit',{}]]},
             'prev_line_sha256':previous,**change}
        row['record_sha256']=hashlib.sha256(encode(row)).hexdigest()
        line=encode(row); lines.append(line);previous=hashlib.sha256(line).hexdigest()
    return b'\n'.join(lines)+(b'\n' if lines else b'')


def manifest(raw, **change):
    v={'schema':'finance-copy-provenance-v1','declared_source_sha':None,'declared_image_digest':None,
       'backup_reference_sha256':'4'*64,'collected_at':'2026-11-01T01:00:00Z','cutoff_at':None,
       'acquisition':'unknown','expected_log_bytes':len(raw),'expected_log_sha256':hashlib.sha256(raw).hexdigest(),
       'declared_last_seq':None,'declared_tail_sha256':None,**change}
    return json.dumps(v).encode()


def evaluate(raw=None, meta=None):
    from recovery_inventory import evaluate
    raw=log_bytes() if raw is None else raw
    return evaluate(raw,manifest(raw) if meta is None else meta,IDENTITY)


def bundle(tmp_path, raw=None):
    root=tmp_path/'copies';root.mkdir(mode=0o700)
    raw=log_bytes() if raw is None else raw
    for name,data in [('finance-log.copy.jsonl',raw),('provenance.json',manifest(raw))]:
        p=root/name;p.write_bytes(data);p.chmod(0o600)
    parent=tmp_path/'reports';parent.mkdir(mode=0o700)
    return root,parent


def cli_module():
    spec=importlib.util.spec_from_file_location('owned_inventory_cli',CLI)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m


def test_whole_file_line_and_record_hashes_are_distinct_and_bound():
    raw=log_bytes(); tail=hashlib.sha256(raw.split(b'\n')[-2]).hexdigest()
    result=evaluate(raw,manifest(raw,declared_last_seq=2,declared_tail_sha256=tail))
    assert result['evaluation']=='EVALUATED' and result['chain']['status']=='VALID'
    assert result['chain']['tail_sha256']==tail
    assert result['inspected']['log']['sha256']==hashlib.sha256(raw).hexdigest()
    assert tail!=json.loads(raw.split(b'\n')[-2])['record_sha256']
    assert result['inventory']['counts_complete'] and result['inventory']['records']==2
    assert result['restore_authorized'] is False and result['ledger_crosscheck']=='NOT_PROVIDED'


@pytest.mark.parametrize('field,value',[('expected_log_bytes',1),('expected_log_sha256','0'*64),
    ('declared_last_seq',1),('declared_tail_sha256','0'*64)])
def test_any_declaration_mismatch_is_incomplete_not_chain_invalid(field,value):
    from recovery_inventory import exit_code
    raw=log_bytes(); kw={'declared_last_seq':2,'declared_tail_sha256':hashlib.sha256(raw.split(b'\n')[-2]).hexdigest(),field:value}
    out=evaluate(raw,manifest(raw,**kw))
    assert out['evaluation']=='INCOMPLETE' and exit_code(out)==3
    assert out['chain']['status']=='VALID' and out['declared_binding']['status']=='MISMATCH'


@pytest.mark.parametrize('raw',[b'',b'\n',b'{}',b'{}\r\n',b'{private}\n',b'\xff\n'])
def test_empty_torn_bad_encoding_never_complete_inventory(raw):
    out=evaluate(raw)
    assert out['evaluation'] in ('INCOMPLETE','INVALID_INPUT')
    assert out['inventory']['counts_complete'] is False
    assert out['inventory']['records'] is None
    assert 'private' not in json.dumps(out)


@pytest.mark.parametrize('fragment',[b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":1e999}',b'{"x":'+b'1'*65+b'}',b'['*33+b'0'+b']'*33])
def test_nested_strict_json_before_verifier(fragment,monkeypatch):
    import recovery_inventory as m
    called=[];monkeypatch.setattr(m,'verify_lines',lambda _:called.append(True))
    raw=b'{"data":'+fragment+b'}\n'
    out=evaluate(raw)
    assert out['evaluation']=='INVALID_INPUT' and not called
    assert out['inventory']['counts_complete'] is False


@pytest.mark.parametrize('change',[{'seq':True},{'kind':'private-unknown'}, {'data':{}},
    {'data':{'ops':None}},{'data':{'ops':[['private-unknown',{}]]}},{'unexpected':42}])
def test_unsupported_envelope_or_tags_are_not_supported(change):
    out=evaluate(log_bytes(1,**change))
    assert out['evaluation']=='INVALID_INPUT'
    assert out['record_envelope_capability']=='UNSUPPORTED'
    assert 'private-unknown' not in json.dumps(out)


def test_unknown_tags_count_all_records_beyond_diagnostic_cap():
    out=evaluate(log_bytes(40,kind='private-unknown',data={'ops':[['private-op',{}]]}))
    assert out['inventory']['records']==40 and out['inventory']['counts_complete']
    assert out['inventory']['outer_kinds']['UNKNOWN']==40
    assert out['inventory']['operation_tags']['UNKNOWN']==40
    assert len(out['diagnostics']['items'])==32 and not out['diagnostics']['complete']
    assert out['diagnostics']['total']==80


def test_hash_corruption_and_truncated_valid_tail_remain_honest():
    raw=log_bytes(); broken=raw.replace(b'2026-11-01',b'2026-11-02',1)
    assert evaluate(broken)['chain']['status']=='INVALID'
    truncated=raw.split(b'\n')[0]+b'\n'
    out=evaluate(truncated)
    assert out['chain']['status']=='VALID'
    assert out['absent_tail_completeness']=='UNKNOWN' and not out['restore_authorized']
    assert out['application_replay']=='NOT_EVALUATED'


@pytest.mark.parametrize('kw',[{'extra':'private'}, {'expected_log_bytes':True}, {'declared_source_sha':'private'},
    {'declared_tail_sha256':'0'*64}, {'collected_at':'2026-11-01'}, {'cutoff_at':'2026-11-02T00:00:00Z'}])
def test_hostile_manifest_refused_without_echo(kw):
    out=evaluate(meta=manifest(log_bytes(),**kw))
    assert out['evaluation']=='INVALID_INPUT' and out['declarations'] is None
    assert out['record_envelope_capability']=='NOT_EVALUATED'
    assert 'private' not in json.dumps(out)


def test_cli_determinism_modes_and_inputs_unchanged(tmp_path):
    root,parent=bundle(tmp_path);before={p.name:p.read_bytes() for p in root.iterdir()}
    answers=[]
    for n in range(2):
        run=subprocess.run([sys.executable,'-B',str(CLI),'--copies-dir',str(root),'--report-file',str(parent/str(n))],capture_output=True,check=False)
        assert run.returncode==0,(run.stdout,run.stderr)
        assert not run.stderr and json.loads(run.stdout)['report_written'] is True
        report=parent/str(n);answers.append(report.read_bytes())
        assert report.stat().st_mode&0o777==0o600
        assert report.parent.stat().st_mode&0o777==0o700
    assert answers[0]==answers[1]
    assert before=={p.name:p.read_bytes() for p in root.iterdir()}


@pytest.mark.parametrize('mode',['symlink','fifo','hardlink','mode','extra','live_layout'])
def test_unsafe_bundle_endpoint_refused(tmp_path,mode):
    root,parent=bundle(tmp_path);file=root/'finance-log.copy.jsonl'
    if mode=='symlink':
        copy=tmp_path/'owned';file.rename(copy);file.symlink_to(copy)
    elif mode=='fifo': file.unlink();os.mkfifo(file,0o600)
    elif mode=='hardlink': os.link(file,tmp_path/'other')
    elif mode=='mode': file.chmod(0o644)
    elif mode=='extra': (root/'extra').write_text('private')
    else: file.rename(root/'fin_log.jsonl')
    out,code=cli_module().run(str(root),str(parent/'result'))
    assert code==4 and out['report_written'] is False
    assert 'private' not in json.dumps(out)


def test_change_during_second_read_is_detected(tmp_path,monkeypatch):
    root,parent=bundle(tmp_path);m=cli_module();read=m._read_bounded;calls=[]
    def changing(fd,cap):
        answer=read(fd,cap);calls.append(fd)
        if len(calls)==2:
            with (root/'finance-log.copy.jsonl').open('ab') as f:f.write(b'\n')
        return answer
    monkeypatch.setattr(m,'_read_bounded',changing)
    out,code=m.run(str(root),str(parent/'result'))
    assert code==4 and out['reason']=='INPUT_CHANGED' and not out['report_written']


@pytest.mark.parametrize('stage',[1,2])
def test_each_durability_fsync_failure_stays_unknown(tmp_path,monkeypatch,stage):
    root,parent=bundle(tmp_path);m=cli_module();original=os.fsync;calls=[]
    def fail(fd):
        calls.append(fd)
        if len(calls)==stage: raise OSError('private disk failure')
        return original(fd)
    monkeypatch.setattr(m.os,'fsync',fail)
    out,code=m.run(str(root),str(parent/'result'))
    assert code==4 and not out['report_written'] and out['reason']=='OUTPUT_UNKNOWN'
    assert 'private disk failure' not in json.dumps(out)


def test_no_replace_and_overlap_refuse(tmp_path):
    root,parent=bundle(tmp_path);m=cli_module();existing=parent/'existing';existing.mkdir(mode=0o700)
    p=existing/'owned';p.write_text('keep')
    assert m.run(str(root),str(existing))[1]==4 and p.read_text()=='keep'
    assert m.run(str(root),str(root/'result'))[1]==4


def test_evaluator_import_boundary(tmp_path):
    script="""
import builtins,sys
sys.path.insert(0,sys.argv[1])
original=builtins.__import__
def guarded(name,*args,**kwargs):
 if name.split('.')[0] in {'api','service','ports','ledger','httpx','receipt_compatibility','config'}: raise AssertionError(name)
 return original(name,*args,**kwargs)
builtins.__import__=guarded
import recovery_inventory
assert recovery_inventory.evaluate(b'',b'{}',{})['restore_authorized'] is False
"""
    run=subprocess.run([sys.executable,'-B','-c',script,str(ROOT/'src')],capture_output=True,check=False)
    assert run.returncode==0,(run.stdout,run.stderr)


def test_genuine_owned_finance_journal_is_structurally_supported(tmp_path):
    from helpers import Harness
    h=Harness(data_dir=str(tmp_path/'finance')).ready();h.fund_campaign()
    raw=(tmp_path/'finance'/'fin_log.jsonl').read_bytes()
    out=evaluate(raw)
    assert out['evaluation']=='EVALUATED' and out['inventory']['records']>2
    assert out['application_replay']=='NOT_EVALUATED' and not out['restore_authorized']


@pytest.mark.parametrize('stage',['identity_read','after_evaluation','directory_entry'])
def test_acquisition_rechecked_at_evaluation_boundary(tmp_path,monkeypatch,stage):
    root,parent=bundle(tmp_path);m=cli_module()
    def mutate():
        if stage=='directory_entry': (root/'extra').write_text('owned')
        else:
            with (root/'finance-log.copy.jsonl').open('ab') as f:f.write(b'\n')
    if stage=='after_evaluation':
        original=m.inventory.evaluate
        def changed(*args):
            out=original(*args);mutate();return out
        monkeypatch.setattr(m.inventory,'evaluate',changed)
    else:
        original=m._evaluator
        def changed():
            out=original();mutate();return out
        monkeypatch.setattr(m,'_evaluator',changed)
    out,code=m.run(str(root),str(parent/'result'))
    assert code==4 and out['reason']=='INPUT_CHANGED' and not out['report_written']


def test_output_exact_permissions_with_restrictive_file_umask(tmp_path):
    root,parent=bundle(tmp_path);m=cli_module();old=os.umask(0o200)
    try:out,code=m.run(str(root),str(parent/'result'))
    finally:os.umask(old)
    assert code==0 and out['report_written']
    assert (parent/'result').stat().st_mode&0o777==0o600
    assert parent.stat().st_mode&0o777==0o700


def test_final_report_collision_is_not_replaced_or_deleted(tmp_path):
    root,parent=bundle(tmp_path);m=cli_module();target=parent/'result'
    target.write_text('owned-collision');target.chmod(0o640)
    before=target.stat()
    out,code=m.run(str(root),str(target))
    assert code==4 and not out['report_written']
    assert target.read_text()=='owned-collision' and target.stat().st_mode==before.st_mode
    assert target.stat().st_ino==before.st_ino


def test_cleanup_failure_is_truthfully_unknown_and_never_recursive(tmp_path,monkeypatch):
    root,parent=bundle(tmp_path);m=cli_module()
    monkeypatch.setattr(m,'_write_all',lambda *args: (_ for _ in ()).throw(OSError('private')))
    monkeypatch.setattr(m.os,'unlink',lambda *args,**kwargs: (_ for _ in ()).throw(OSError('private')))
    out,code=m.run(str(root),str(parent/'result'))
    assert code==4 and out['residual_possible'] and out['reason']=='OUTPUT_UNKNOWN'
    assert (parent/'result').exists()


def test_short_writes_complete_and_zero_write_fails(tmp_path,monkeypatch):
    root,parent=bundle(tmp_path);m=cli_module();original=m.os.write
    monkeypatch.setattr(m.os,'write',lambda fd,data:original(fd,data[:7]))
    answer,code=m.run(str(root),str(parent/'short'))
    assert code==0
    data=(parent/'short').read_bytes()
    assert json.loads(data)['evaluation']=='EVALUATED'
    assert hashlib.sha256(data).hexdigest()==answer['report_sha256']
    monkeypatch.setattr(m.os,'write',lambda *args:0)
    assert m.run(str(root),str(parent/'zero'))[1]==4


@pytest.mark.parametrize('boundary',['byte','line','records','nodes'])
def test_resource_cap_failure_never_reports_partial_counts(boundary,monkeypatch):
    import recovery_inventory as m
    raw=log_bytes(3)
    if boundary=='byte':monkeypatch.setattr(m,'MAX_LOG',len(raw)-1)
    elif boundary=='line':monkeypatch.setattr(m,'MAX_LINE',len(raw.split(b'\n')[0])-1)
    elif boundary=='records':monkeypatch.setattr(m,'MAX_RECORDS',2)
    else:monkeypatch.setattr(m,'MAX_NODES',40)
    out=evaluate(raw)
    assert out['evaluation']=='INVALID_INPUT' and not out['inventory']['counts_complete']
    assert out['inventory']['records'] is None and out['inventory']['operation_tags'] is None


def test_actual_byte_manifest_and_record_limits():
    import recovery_inventory as m
    assert evaluate(b'x'*(m.MAX_LOG+1))['inspected']['log'] is None
    assert evaluate(meta=b' '*(m.MAX_MANIFEST+1))['inspected']['manifest'] is None
    assert evaluate(log_bytes(m.MAX_RECORDS+1))['inventory']['counts_complete'] is False


def test_cli_invalid_arguments_and_input_canaries_never_echo(tmp_path):
    root,parent=bundle(tmp_path)
    (root/'provenance.json').write_text('{"private-canary":"never-output"}')
    run=subprocess.run([sys.executable,'-B',str(CLI),'--copies-dir',str(root),'--report-file',str(parent/'result')],capture_output=True,check=False)
    assert run.returncode==2 and not run.stderr
    assert b'private-canary' not in run.stdout+(parent/'result').read_bytes()
    bad=subprocess.run([sys.executable,'-B',str(CLI),'--private-secret-argument'],capture_output=True,check=False)
    assert bad.returncode==2 and b'private-secret' not in bad.stdout+bad.stderr


def test_ancestor_symlink_and_dot_path_are_refused(tmp_path):
    root,parent=bundle(tmp_path);alias=tmp_path/'alias';alias.symlink_to(root,target_is_directory=True);m=cli_module()
    assert m.run(str(alias),str(parent/'a'))[1]==4
    assert m.run(str(root)+'/../copies',str(parent/'b'))[1]==4


def test_cli_has_no_network_or_constructor_side_effects(tmp_path,monkeypatch):
    import socket
    import store
    root,parent=bundle(tmp_path)
    def refuse(*args,**kwargs):raise AssertionError('forbidden side effect')
    monkeypatch.setattr(socket,'socket',refuse)
    monkeypatch.setattr(socket,'getaddrinfo',refuse)
    monkeypatch.setattr(store.RecordLog,'__init__',refuse)
    monkeypatch.setattr(store.DataDirLock,'__init__',refuse)
    assert cli_module().run(str(root),str(parent/'result'))[1]==0


def test_finite_number_capability_bound_uses_decimal_not_money_float():
    import recovery_inventory as m
    from decimal import Decimal
    value,_=m._json(b'{"n":1e308}',20)
    assert value['n']==Decimal('1e308')
    with pytest.raises(m.InvalidCopy,match='NUMBER_LIMIT'):
        m._json(b'{"n":1.1e308}',20)
    with pytest.raises(m.InvalidCopy):
        m._json(b'{"n":1e999}',20)


def test_original_mkdir_replacement_is_not_adopted_or_chmodded(tmp_path,monkeypatch):
    m=cli_module();tmp_path.chmod(0o700);target=tmp_path/'report';replacement=tmp_path/'replacement'
    replacement.mkdir(mode=0o750);before=replacement.stat();original=m.os.mkdir
    def raced(name,mode=0o777,*,dir_fd=None):
        original(name,mode,dir_fd=dir_fd)
        if name=='report':
            os.rmdir(name,dir_fd=dir_fd);os.rename(replacement,target)
    monkeypatch.setattr(m.os,'mkdir',raced)
    m._publish(str(target),b'{}\n',[(-1,-1)])
    assert replacement.exists() and replacement.stat().st_ino==before.st_ino
    assert replacement.stat().st_mode==before.st_mode
    assert target.is_file() and target.read_bytes()==b'{}\n'


def test_publication_never_creates_removes_or_chmods_directories(tmp_path,monkeypatch):
    root,parent=bundle(tmp_path);m=cli_module();fchmod=m.os.fchmod
    def no_directory(*args,**kwargs):raise AssertionError('directory mutation')
    def file_only(fd,mode):
        import stat
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        return fchmod(fd,mode)
    monkeypatch.setattr(m.os,'mkdir',no_directory);monkeypatch.setattr(m.os,'rmdir',no_directory)
    monkeypatch.setattr(m.os,'fchmod',file_only)
    assert m.run(str(root),str(parent/'result'))[1]==0


def test_parent_replacement_refuses_success_preserves_replacement(tmp_path,monkeypatch):
    root,parent=bundle(tmp_path);m=cli_module();replacement=tmp_path/'replacement'
    replacement.mkdir(mode=0o700);marker=replacement/'keep';marker.write_text('owned')
    retained=tmp_path/'retained';original=m.os.open;fired=[]
    def swapping(path,flags,*args,**kwargs):
        if path=='result.json' and flags&os.O_CREAT and not fired:
            fired.append(True);parent.rename(retained);replacement.rename(parent)
        return original(path,flags,*args,**kwargs)
    monkeypatch.setattr(m.os,'open',swapping)
    out,code=m.run(str(root),str(parent/'result.json'))
    assert fired and code==4 and out['reason']=='OUTPUT_UNKNOWN'
    assert (parent/'keep').read_text()=='owned' and not (parent/'result.json').exists()
    assert parent.stat().st_mode&0o777==0o700 and not (retained/'result.json').exists()


def test_only_created_file_descriptor_is_written_or_chmodded(tmp_path,monkeypatch):
    root,parent=bundle(tmp_path);m=cli_module();target=parent/'result.json';original=m.os.open;fired=[]
    moved=parent/'created-moved'
    def swapping(path,flags,*args,**kwargs):
        fd=original(path,flags,*args,**kwargs)
        if path=='result.json' and flags&os.O_CREAT and not fired:
            fired.append(True);target.rename(moved);target.write_text('replacement-owned');target.chmod(0o640)
        return fd
    monkeypatch.setattr(m.os,'open',swapping)
    out,code=m.run(str(root),str(target))
    assert fired and code==4 and out['reason']=='OUTPUT_UNKNOWN' and out['residual_possible']
    assert target.read_text()=='replacement-owned' and target.stat().st_mode&0o777==0o640
    assert json.loads(moved.read_bytes())['evaluation']=='EVALUATED' and moved.stat().st_mode&0o777==0o600


def test_legacy_report_directory_cli_option_refused_privately(tmp_path):
    run=subprocess.run([sys.executable,'-B',str(CLI),'--copies-dir',str(tmp_path),'--report-dir','private-old-path'],capture_output=True,check=False)
    assert run.returncode==2 and not run.stderr and b'private-old-path' not in run.stdout


def test_close_failure_after_publication_does_not_claim_no_residual(tmp_path,monkeypatch):
    root,parent=bundle(tmp_path);m=cli_module();open_fd=m.os.open;close_fd=m.os.close;owned=[];failed=[]
    def capture(path,flags,*args,**kwargs):
        fd=open_fd(path,flags,*args,**kwargs)
        if path=='result' and flags&os.O_CREAT:owned.append(fd)
        return fd
    def fail_close(fd):
        close_fd(fd)
        if fd in owned and not failed:
            failed.append(True);raise OSError('private close fault')
    monkeypatch.setattr(m.os,'open',capture);monkeypatch.setattr(m.os,'close',fail_close)
    out,code=m.run(str(root),str(parent/'result'))
    assert failed and code==4 and out['reason']=='OUTPUT_UNKNOWN' and out['residual_possible']
    assert not out['report_written'] and 'private close' not in json.dumps(out)
