import _runtime_path
import http.client
import socket
import threading
import unittest
from cold_front import Front
from owner_wire import Server

class DualStackListeners(unittest.TestCase):
 def check_both(self, server, status):
  self.assertEqual(server.socket.getsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY),0)
  for host in ('127.0.0.1','::1'):
   c=http.client.HTTPConnection(host,server.server_port,timeout=2)
   try:
    c.request('GET','/');r=c.getresponse();self.assertEqual(r.status,status);r.read()
   finally:c.close()
 def test_front_accepts_both_and_stays_closed(self):
  front=Front({'ledger':1},bind='::')
  try:self.check_both(front.servers[0],503)
  finally:front.stop()
 def test_control_accepts_both_without_bypassing_auth(self):
  server=Server(('::',0),'a'*64,lambda *_: self.fail('unauthorized dispatch'))
  t=threading.Thread(target=server.serve_forever);t.start()
  try:self.check_both(server,501)
  finally:server.shutdown();server.server_close();t.join()
