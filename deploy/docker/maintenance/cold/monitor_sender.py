"""Explicit opt-in adapter to the existing bounded Resend transport.
No default sender or credential; no values in repr, argv, files or logs.
"""
from resend_receipts import ResendReceiptTransport,ResendTransportProfile,_sender,_string
from owner_wire import Refused
class Sender:
 def __init__(self,profile,credential):
  self.profile=profile;self._transport=ResendReceiptTransport(profile,credential,10)
 @classmethod
 def from_environment(cls,env):
  if env.get('BACKUP_ALERTS_ENABLED')!='1':raise Refused('explicit alert enable required')
  credential=env.pop('BACKUP_RESEND_API_KEY',None);sender=env.get('BACKUP_ALERT_FROM')
  if not _string(credential,4096) or not 32<=len(credential)<=4096 or not credential.isascii() or any(c.isspace() for c in credential) or '{{'in credential:raise Refused('alert credential shape')
  if not _sender(sender):raise Refused('alert sender required')
  address=sender.rsplit('<',1)[-1].rstrip('>')
  if not address.endswith('@zbestmedia.com'):raise Refused('owned sender domain required')
  return cls(ResendTransportProfile('zbm-backup-alert','1',sender),credential)
 def __call__(self,request,now):
  result=self._transport.send_frozen(request,now)
  return {'outcome':result.outcome,'provider_message_id':result.provider_message_id}
