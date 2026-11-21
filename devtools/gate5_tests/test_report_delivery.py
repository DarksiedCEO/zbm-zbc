import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy/gate5'))
import report_cli as cli
import report_delivery as delivery
import report_transport as transport


def fixture():
    raw = b'<html><body>Existing approved report</body></html>'
    return dict(report_id='rpt_1', tenant_id='tenant_1', audit_id='audit_1', format='html',
                media_type='text/html; charset=utf-8', audit_report_sha256='a'*64,
                document_sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), content=raw.decode())


def prepared():
    return cli.prepare(fixture(), client='client_1', invoice='invoice_1',
                       sender='reports@zbestmedia.com', recipient='client@example.com')


class ReportDelivery(unittest.TestCase):
    def test_dry_run_never_prompts_or_constructs_transport(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'report.json'; p.write_text(json.dumps(fixture()))
            with patch.object(cli, 'Transport') as sender, patch.object(cli.getpass, 'getpass') as key, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(['--report', str(p), '--client', 'client_1', '--invoice', 'invoice_1', '--sender', 'reports@zbestmedia.com', '--recipient', 'client@example.com']), 0)
            sender.assert_not_called(); key.assert_not_called()
            self.assertEqual(json.loads(out.getvalue())['state'], 'DRY_RUN')
            self.assertEqual(list(Path(td).iterdir()), [p])

    def test_hash_size_sender_and_format_refuse(self):
        for field, value in [('document_sha256','0'*64), ('bytes',True), ('format','markdown'), ('media_type','text/plain')]:
            v=fixture();v[field]=value
            with self.subTest(field=field), self.assertRaises(delivery.Refused):
                cli.prepare(v,client='c',invoice='i',sender='reports@zbestmedia.com',recipient='client@example.com')
        with self.assertRaises(delivery.Refused):
            cli.prepare(fixture(),client='c',invoice='i',sender='someone@example.com',recipient='client@example.com')

    def test_changed_recipient_sender_and_mapping_change_approval(self):
        _,_,original=prepared()
        for field,value in [('client','different'),('invoice','different'),('sender','hello@zbestmedia.com'),('recipient','other@example.com')]:
            args=dict(client='client_1',invoice='invoice_1',sender='reports@zbestmedia.com',recipient='client@example.com');args[field]=value
            self.assertNotEqual(cli.prepare(fixture(),**args)[2]['approval_sha256'],original['approval_sha256'])

    def test_approval_refused_before_key_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'report.json';p.write_text(json.dumps(fixture()))
            with patch.object(cli.getpass,'getpass') as key,self.assertRaises(delivery.Refused):
                cli.main(['--report',str(p),'--client','c','--invoice','i','--sender','reports@zbestmedia.com','--recipient','client@example.com','--execute','--approve-sha256','0'*64,'--outbox',td])
            key.assert_not_called()

    def test_accepted_duplicate_and_restart_no_second_send(self):
        binding,raw,_=prepared();send=Mock(return_value={'outcome':'accepted','message_id':'provider_1'})
        with tempfile.TemporaryDirectory() as td:
            for n in range(2):
                b=cli.private_outbox(str(Path(td).resolve()))
                try:
                    key=b.enqueue(binding,raw,sender='reports@zbestmedia.com',recipient='client@example.com')
                    result=b.dispatch(key,send)
                    self.assertEqual(result['status'],'ACCEPTED');self.assertFalse(result['inbox_delivered'])
                finally:b.close()
            self.assertEqual(send.call_count,1)

    def test_ambiguous_send_restart_refuses(self):
        binding,raw,_=prepared();send=Mock(side_effect=TimeoutError('synthetic-secret-must-not-print'))
        with tempfile.TemporaryDirectory() as td:
            b=cli.private_outbox(str(Path(td).resolve()));key=b.enqueue(binding,raw,sender='reports@zbestmedia.com',recipient='client@example.com')
            self.assertEqual(b.dispatch(key,send)['status'],'UNKNOWN');b.close()
            b=cli.private_outbox(str(Path(td).resolve()))
            try:
                with self.assertRaises(delivery.Refused):b.dispatch(key,send)
            finally:b.close()
            self.assertEqual(send.call_count,1)

    def test_interrupted_dispatch_preserves_refusal(self):
        binding,raw,_=prepared()
        with tempfile.TemporaryDirectory() as td:
            b=cli.private_outbox(str(Path(td).resolve()));key=b.enqueue(binding,raw,sender='reports@zbestmedia.com',recipient='client@example.com')
            with self.assertRaises(KeyboardInterrupt):b.dispatch(key,Mock(side_effect=KeyboardInterrupt()))
            b.close();b=cli.private_outbox(str(Path(td).resolve()));send=Mock()
            try:
                with self.assertRaises(delivery.Refused):b.dispatch(key,send)
                send.assert_not_called()
            finally:b.close()

    def test_envelope_conflict_and_capacity(self):
        binding,raw,_=prepared()
        with tempfile.TemporaryDirectory() as td:
            b=cli.private_outbox(str(Path(td).resolve()))
            try:
                b.enqueue(binding,raw,sender='reports@zbestmedia.com',recipient='client@example.com',capacity=1)
                with self.assertRaises(delivery.Refused):b.enqueue(binding,raw,sender='hello@zbestmedia.com',recipient='client@example.com')
                with self.assertRaises(delivery.Refused):b.enqueue(binding,raw,sender='reports@zbestmedia.com',recipient='other@example.com',capacity=1)
            finally:b.close()

    def test_wire_attachment_hash_and_explicit_recipient(self):
        binding,raw,_=prepared();key,body=delivery.freeze(binding,raw,'reports@zbestmedia.com','client@example.com')
        wire=json.loads(transport.wire_body(key,body,'reports@zbestmedia.com'))
        self.assertEqual(wire['to'],['client@example.com'])
        self.assertEqual(base64.b64decode(wire['attachments'][0]['content']),raw)
        bad=json.loads(body);bad['attachment_base64']=base64.b64encode(b'changed').decode()
        with self.assertRaises(delivery.Refused):transport.wire_body(key,delivery.encode(bad),'reports@zbestmedia.com')

    def test_transport_ambiguous_response_no_retry(self):
        binding,raw,_=prepared();key,body=delivery.freeze(binding,raw,'reports@zbestmedia.com','client@example.com')
        t=transport.Transport('re_'+'a'*32,'reports@zbestmedia.com')
        for result in (b'{}',b'{"outcome":"accepted","message_id":true}'):
            with patch.object(transport,'_exchange',return_value=result) as exchange:
                self.assertEqual(t(key,body),{'outcome':'unknown'});exchange.assert_called_once()
        with patch.object(transport,'_exchange',side_effect=TimeoutError) as exchange:
            self.assertEqual(t(key,body),{'outcome':'unknown'});exchange.assert_called_once()
        self.assertNotIn('a'*32,repr(t))

    def test_explicit_cli_executes_fixed_adapter_once(self):
        _,_,preview=prepared()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();p=root/'export.json';p.write_text(json.dumps(fixture()))
            args=['--report',str(p),'--client','client_1','--invoice','invoice_1','--sender','reports@zbestmedia.com','--recipient','client@example.com','--execute','--approve-sha256',preview['approval_sha256'],'--outbox',str(root/'outbox')]
            with patch.object(sys.stdin,'isatty',return_value=True),patch.object(cli.getpass,'getpass',return_value='re_'+'a'*32),patch.object(transport,'_exchange',return_value=b'{"outcome":"accepted","message_id":"provider_1"}') as exchange,contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(args),0);self.assertEqual(cli.main(args),0)
                exchange.assert_called_once()
            events=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertTrue(all(e['status']=='ACCEPTED' and e['inbox_delivered'] is False for e in events))
            payload=json.loads(exchange.call_args.args[0]);self.assertEqual(payload['mode'],'tls')
            self.assertEqual(json.loads(base64.b64decode(payload['body']))['to'],['client@example.com'])

    def test_corrupted_frozen_body_refuses_without_sender(self):
        binding,raw,_=prepared()
        with tempfile.TemporaryDirectory() as td:
            b=cli.private_outbox(str(Path(td).resolve()))
            try:
                key=b.enqueue(binding,raw,sender='reports@zbestmedia.com',recipient='client@example.com')
                b.db.execute("UPDATE deliveries SET body=? WHERE identity=?",(b'changed',key))
                send=Mock()
                with self.assertRaises(delivery.Refused):b.dispatch(key,send)
                send.assert_not_called()
            finally:b.close()

    def test_symlink_report_and_public_outbox_refuse(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'report';p.write_text(json.dumps(fixture()));link=Path(td)/'link';link.symlink_to(p)
            with self.assertRaises(OSError):cli.read_report(link)
            Path(td).chmod(0o755)
            with self.assertRaises(delivery.Refused):cli.private_outbox(str(Path(td).resolve()))

if __name__=='__main__':unittest.main()
