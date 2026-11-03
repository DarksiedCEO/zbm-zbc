"""Fixed single ingress to coordinator-owned closed/open gates; no raw owner route."""
from http.server import BaseHTTPRequestHandler
from cold_front import Server
PREFIXES={'/ledger/':'ledger','/fin/':'finance','/legal/':'legal','/compliance/':'compliance','/seo/':'seo'}
def handler(front):
 # Reuse bounded framing/proxy logic. These loopback targets remain behind the
 # coordinator's all-five barrier; private owner ports are never public targets.
 handlers={m:front.handler(('127.0.0.1',p)) for m,p in front.ports.items()}
 base=front.handler(('127.0.0.1',1))
 class Intake(base):
  def route(self):
   if self.command=='GET' and self.path=='/health':
    return self.respond(200,b'{"supervisor":"alive","business_readiness":"separate"}')
   if type(self.path)is not str or '%' in self.path or '//' in self.path:
    return self.respond(400,b'{"error":"unsupported path"}')
   member=next((m for p,m in PREFIXES.items() if self.path.startswith(p)),None)
   if member not in handlers:return self.respond(404,b'{"error":"unknown route"}')
   return handlers[member].route(self)
 return Intake

def server(front):return Server(('0.0.0.0',27001),handler(front))
