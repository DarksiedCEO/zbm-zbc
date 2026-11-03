"""Fixed B2/S3 read-only restore; credentials never appear in subprocess argv."""
from owner_wire import Refused
import remote_restore

def credentials(password,value):
 if type(value)is not dict or set(value)!={'key_id','application_key'}:raise Refused('credential schema')
 for secret in (password,value['key_id'],value['application_key']):
  if type(secret)is not str or not 1<=len(secret)<=1024 or any(ord(c)<33 or ord(c)>126 for c in secret):raise Refused('credential encoding')

def restore(password,value,locator):
 credentials(password,value)
 return remote_restore.restore(password,None,locator,None,_b2_credentials=value)
