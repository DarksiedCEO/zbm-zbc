"""Explicit opt-in configuration, generated secrets only."""
import os
import secrets

import pytest

import api
import config
from ports import NotBuiltClientMail
from receipt_resend_adapter import ResendReceiptMail
from helpers import base_env


def configured(tmp_path):
    path=tmp_path/'key'
    key='re_'+secrets.token_hex(32)
    path.write_text(key); path.chmod(0o600)
    return {**base_env(), 'FIN_RECEIPT_MAIL_PROVIDER':'resend', 'FIN_RECEIPT_MAIL_PROFILE_ID':'owned',
        'FIN_RECEIPT_MAIL_PROFILE_REVISION':'v1', 'FIN_RECEIPT_MAIL_FROM':'Owned <from@example.test>',
        'FIN_RECEIPT_RESEND_KEY_FILE':str(path)},key


def test_disabled_never_reads_key(tmp_path,monkeypatch):
    env,key=configured(tmp_path)
    env['FIN_RECEIPT_MAIL_PROVIDER']='disabled'
    monkeypatch.setattr(config,'_secret_file',lambda *args:pytest.fail('disabled key read'))
    settings=config.load(env)
    assert type(api.build_ports(settings).client_mail) is NotBuiltClientMail
    assert key not in repr(settings)


def test_enabled_factory_is_explicit_no_io(tmp_path):
    env,key=configured(tmp_path)
    settings=config.load(env)
    port=api.build_ports(settings).client_mail
    assert type(port) is ResendReceiptMail
    assert key not in repr(settings)+repr(port)


@pytest.mark.parametrize('field,value', [('FIN_RECEIPT_MAIL_PROVIDER','unknown'),
 ('FIN_RECEIPT_MAIL_PROFILE_ID',''),('FIN_RECEIPT_MAIL_PROFILE_REVISION',''),
 ('FIN_RECEIPT_MAIL_FROM','bad\r\nfrom'),('FIN_RECEIPT_MAIL_DEADLINE_S','0'),
 ('FIN_RECEIPT_MAIL_DEADLINE_S','31'),('FIN_RECEIPT_MAIL_DEADLINE_S','1.5'),
 ('FIN_RECEIPT_RESEND_KEY_FILE','relative')])
def test_bad_config_refuses_without_claim_or_secret(tmp_path,field,value):
    env,key=configured(tmp_path); env[field]=value
    env['FIN_DATA_DIR']=str(tmp_path/'data')
    with pytest.raises(RuntimeError) as exc: config.load(env)
    assert key not in str(exc.value)
    assert not (tmp_path/'data').exists()


@pytest.mark.parametrize('mode',['symlink','world_readable','empty','wrong_prefix'])
def test_bad_secret_file_refuses(tmp_path,mode):
    env,key=configured(tmp_path)
    from pathlib import Path
    path=Path(env['FIN_RECEIPT_RESEND_KEY_FILE'])
    if mode=='symlink':
        link=tmp_path/'link'; link.symlink_to(path); env['FIN_RECEIPT_RESEND_KEY_FILE']=str(link)
    if mode=='world_readable': path.chmod(0o644)
    if mode=='empty': path.write_text('')
    if mode=='wrong_prefix': path.write_text(secrets.token_hex(32))
    with pytest.raises(RuntimeError) as exc: config.load(env)
    assert key not in str(exc.value)


def test_adapter_construction_failure_releases_service_claim(tmp_path,monkeypatch):
    import receipt_resend_adapter
    from fakes import FakeLedgerClient
    env,key=configured(tmp_path);env['FIN_DATA_DIR']=str(tmp_path/'data')
    settings=config.load(env)
    def refuse(*args): raise ValueError('owned constructor failure')
    monkeypatch.setattr(receipt_resend_adapter,'ResendReceiptMail',refuse)
    with pytest.raises(ValueError): api.build_service(settings,ledger=FakeLedgerClient())
    token=settings.data_dir_lock.claim()
    settings.data_dir_lock.release_claim(token)
    assert not (tmp_path/'data'/'fin_log.jsonl').exists()
