import secrets,json
members=('ledger','finance','legal','compliance','seo')
prefix=dict(ledger='LEDGER',finance='FIN',legal='LEGAL',compliance='COMPLIANCE',seo='SEO')
def build(secrets_input):
 credentials={'key_id':secrets_input['key_id'],'key':secrets_input['application_key']}
 coord=secrets.token_hex(32);backup=secrets_input['password'];andre=secrets.token_hex(32);ledger=secrets.token_hex(32);envs={}
 for m in members:
  envs[m]={prefix[m]+'_SERVICE_TOKEN':ledger if m=='ledger' else secrets.token_hex(32)}
  if m!='ledger':envs[m].update(LEDGER_SERVICE_URL='http://ledger.railway.internal:27000',LEDGER_SERVICE_TOKEN=ledger)
 envs['legal'].update(LEGAL_ANDRE_APPROVAL_TOKEN=andre,LEGAL_LINK_TAG_KEY=secrets.token_hex(32),LEGAL_CALLER_TOKENS=json.dumps({x:secrets.token_hex(32) for x in ('scheduler','client_portal','compliance_38','finance_31')}))
 envs['compliance'].update(COMPLIANCE_SCHEDULER_ENABLED='1',COMPLIANCE_WATCHER_ENABLED='1',COMPLIANCE_ANDRE_APPROVAL_TOKEN=andre,COMPLIANCE_CALLER_TOKENS=json.dumps({x:secrets.token_hex(32) for x in ('scheduler','onboarding','finance_31','seo_02')}),COMPLIANCE_LEGAL_URL='http://legal.railway.internal:27000',COMPLIANCE_LEGAL_TOKEN=envs['legal']['LEGAL_SERVICE_TOKEN'],COMPLIANCE_LEGAL_CALLER_TOKEN=json.loads(envs['legal']['LEGAL_CALLER_TOKENS'])['compliance_38'])
 envs['finance'].update(FIN_ANDRE_APPROVAL_TOKEN=andre,FIN_CARD_PREPAYMENTS='1',FIN_CALLER_TOKENS=json.dumps({x:secrets.token_hex(32) for x in ('onboarding','seo_02','rail_gateway')}),FIN_LEGAL_URL='http://legal.railway.internal:27000',FIN_LEGAL_TOKEN=envs['legal']['LEGAL_SERVICE_TOKEN'],FIN_LEGAL_CALLER_TOKEN=json.loads(envs['legal']['LEGAL_CALLER_TOKENS'])['finance_31'],FIN_COMPLIANCE_URL='http://compliance.railway.internal:27000',FIN_COMPLIANCE_TOKEN=envs['compliance']['COMPLIANCE_SERVICE_TOKEN'],FIN_COMPLIANCE_CALLER_TOKEN=json.loads(envs['compliance']['COMPLIANCE_CALLER_TOKENS'])['finance_31'],FIN_STRIPE_INCOMING='1',FIN_STRIPE_SUCCESS_URL='https://example.test/success',FIN_STRIPE_CANCEL_URL='https://example.test/cancel',COLD_LEGAL_PORT='27000',COLD_LEGAL_HOST='legal.railway.internal',COLD_STRIPE_HOST='coordinator.railway.internal')
 envs['seo'].update(SEO_ANDRE_APPROVAL_TOKEN=andre,SEO_CALLER_TOKENS=json.dumps({'dashboard':secrets.token_hex(32)}),SEO_INVOICE_VERIFICATION='finance',SEO_BOT_VERIFY_DNS='0',SEO_FINANCE_URL='http://finance.railway.internal:27000',SEO_FINANCE_TOKEN=envs['finance']['FIN_SERVICE_TOKEN'],SEO_FINANCE_CALLER_TOKEN=json.loads(envs['finance']['FIN_CALLER_TOKENS'])['seo_02'],SEO_COMPLIANCE_URL='http://compliance.railway.internal:27000',SEO_COMPLIANCE_TOKEN=envs['compliance']['COMPLIANCE_SERVICE_TOKEN'],SEO_COMPLIANCE_CALLER_TOKEN=json.loads(envs['compliance']['COMPLIANCE_CALLER_TOKENS'])['seo_02'])
 configs={m:{'member':m,'app_env':envs[m],'coord_token':coord,'backup_password':backup,'runtime_inputs':{},'transport_credentials':credentials} for m in members}
 configs['seo']['runtime_inputs']={'seo-key':secrets.token_hex(32)}
 return configs,envs,coord,andre,backup
