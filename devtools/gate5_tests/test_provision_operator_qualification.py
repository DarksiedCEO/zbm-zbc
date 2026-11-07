"""Offline qualification of the secure variable writer. Synthetic transports only.

No Railway CLI, network, real credential or real binding is used. Passing these
does not certify live CLI behaviour; see the Q3 note in provision_operator_README.md.
"""
import contextlib, io, json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'deploy/gate5'))
import provision_operator as p
import credential_plan as c
from test_provision_operator import Fake

def run(f):
 with tempfile.TemporaryDirectory() as d:
  path=Path(d)/'state'
  try:result=p.execute(f.binding,path,f.read,f.write,f.prompt,f.generate)['status']
  except p.Refused:result='REFUSED'
  return result,json.loads(path.read_text())

def make_absent(f,slots):
 for s,n in slots:
  f.data[s].pop(n,None)
  if n in f.binding['services'][s]['names']:f.binding['services'][s]['names'].remove(n)

class Qualification(unittest.TestCase):
 def test_q1_forbidden_manifest_name_refuses_before_prompt(self):
  for s,n in sorted(c.FORBIDDEN):
   # Record rather than raise: the writer's catch-all would swallow an exception.
   f=Fake();f.binding['services'][s]['names'].append(n);asked=[]
   f.prompt=lambda label:(asked.append(label),'x'*64)[1]
   result,state=run(f)
   self.assertEqual((result,state['status']),('REFUSED','REFUSED_NO_WRITES'),s+'.'+n)
   self.assertEqual(asked,[],s+'.'+n);self.assertFalse(f.writes)

 def test_q2_hidden_input_requires_two_matching_entries(self):
  entries=iter(['synthetic-a','synthetic-b'])
  with patch.object(p.sys.stdin,'isatty',return_value=True),patch.object(p.getpass,'getpass',side_effect=lambda _:next(entries)):
   with self.assertRaises(p.Refused):p.hidden('legal.LEGAL_SERVICE_TOKEN')
  with patch.object(p.sys.stdin,'isatty',return_value=True),patch.object(p.getpass,'getpass',return_value='synthetic-a') as g:
   self.assertEqual(p.hidden('legal.LEGAL_SERVICE_TOKEN'),'synthetic-a');self.assertEqual(g.call_count,2)
   self.assertTrue(all('legal.LEGAL_SERVICE_TOKEN' in a.args[0] and 'synthetic' not in a.args[0] for a in g.call_args_list))

 def test_q2_residual_consistently_wrong_sealed_source_is_copied(self):
  # Documented residual: no remote oracle exists for a sealed non-Ledger value.
  f=Fake();role=('legal','LEGAL_SERVICE_TOKEN');f.data['legal'].pop(role[1]);make_absent(f,c.DIRECT[role])
  f.prompt=lambda label:'f'*64
  self.assertEqual(run(f)[0],'COMPLETE_HOLD')
  self.assertEqual(sum(v=='f'*64 for *_,v in f.writes),len(c.DIRECT[role]))

 def test_q2_wrong_sealed_ledger_role_refuses(self):
  # Ledger digests are an oracle: a wrong hidden value cannot pass.
  f=Fake();f.data['finance'].pop('LEDGER_SERVICE_TOKEN');f.prompt=lambda label:'e'*64
  self.assertEqual(run(f),('REFUSED',{'attempted_name':None,'completed_names':[],'status':'REFUSED_NO_WRITES'}))

 def test_q3_writer_writes_no_references(self):
  # D02: the writer writes no references, so a CLI that resolves them cannot stop it mid-run.
  f=Fake();original=f.write
  def rendered(s,n,v):
   original(s,n,v)
   if '${{' in v:f.data[s][n]=v.replace('${{','resolved-').replace('}}','')
  f.write=rendered
  result,state=run(f)
  self.assertEqual((result,state['status']),('COMPLETE_HOLD','COMPLETE_HOLD'))
  self.assertEqual(sum('${{' in v for *_,v in f.writes),0)
  self.assertFalse(any((s,n) in c.URLS for s,n,_ in f.writes))

 def test_q4_no_values_in_output_or_state(self):
  f=Fake();buf=io.StringIO()
  with contextlib.redirect_stdout(buf),contextlib.redirect_stderr(buf):result,state=run(f)
  self.assertEqual(result,'COMPLETE_HOLD');self.assertEqual(buf.getvalue(),'')
  self.assertEqual(set(state),{'status','completed_names','attempted_name'})
  self.assertTrue(all(type(n) is str and '.' in n and n.split('.',1)[0] in f.binding['services'] for n in state['completed_names']))
  dump=json.dumps(state)
  self.assertFalse(any(len(v)>=16 and v in dump for *_,v in f.writes))

 def test_q5_generated_once_copied_to_every_direct_destination(self):
  f=Fake();role=('compliance','COMPLIANCE_SERVICE_TOKEN');make_absent(f,(role,*c.DIRECT[role]))
  values=iter('%064x'%i for i in range(1,99));calls=[]
  f.generate=lambda:(calls.append(1),next(values))[1]
  self.assertEqual(run(f)[0],'COMPLETE_HOLD');self.assertEqual(len(calls),1)
  copies={(s,n):v for s,n,v in f.writes if (s,n) in (role,*c.DIRECT[role])}
  self.assertEqual(set(copies),{role,*c.DIRECT[role]});self.assertEqual(len(set(copies.values())),1)

 def test_q6_all_reads_and_prompts_precede_first_write(self):
  f=Fake();log=[];r,w,pr=f.read,f.write,f.prompt
  f.read=lambda s:(log.append('R'),r(s))[1];f.write=lambda s,n,v:(log.append('W'),w(s,n,v))[1]
  name='PORTAL_EVIDENCE_KEY';f.hidden['client-portal.'+name]=f.data['client-portal'].pop(name)
  f.prompt=lambda label:(log.append('P'),pr(label))[1]
  self.assertEqual(run(f)[0],'COMPLETE_HOLD');seq=''.join(log)
  self.assertLess(seq.rindex('P'),seq.index('W'))
  services=len(f.binding['services'])
  self.assertEqual(seq[:seq.index('W')].count('R'),3*services) # snapshot, preflight re-read, pre-write

 def test_q7_cli_failure_after_unknown_checkpoint_blocks_rerun(self):
  # Real CLI.write wrapper over a synthetic subprocess that fails; reads stay fake.
  f=Fake();cli=p.CLI(f.binding);seen=[]
  class Bad:returncode=1;stdout='';stderr='synthetic-error-detail'
  def fake_run(args,**kw):seen.append((args,kw.get('input')));return Bad()
  cli.read=f.read
  with patch.object(p.subprocess,'run',side_effect=fake_run):
   with tempfile.TemporaryDirectory() as d:
    path=Path(d)/'state';buf=io.StringIO()
    with contextlib.redirect_stdout(buf),contextlib.redirect_stderr(buf):
     with self.assertRaises(p.Refused):p.execute(f.binding,path,cli.read,cli.write,f.prompt,f.generate)
     self.assertEqual(json.loads(path.read_text())['status'],'UNKNOWN')
     with self.assertRaises(p.Refused):p.execute(f.binding,path,cli.read,cli.write,f.prompt,f.generate)
    self.assertNotIn('synthetic-error-detail',buf.getvalue())
  self.assertEqual(len(seen),1);args,value=seen[0]
  self.assertEqual(args[:3],['railway','variable','set']);self.assertIn('--stdin',args);self.assertIn('--skip-deploys',args)
  self.assertTrue(value);self.assertNotIn(value,args)

if __name__=='__main__':unittest.main()
