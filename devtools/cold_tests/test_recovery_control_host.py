import _runtime_path
import tempfile,unittest,os
from unittest.mock import patch,Mock
from coordinator import Coordinator
class ControlHost(unittest.TestCase):
 def test_persistent_recovery_can_own_only_control_listener(self):
  with tempfile.TemporaryDirectory() as d,patch('coordinator.Server') as server,patch('coordinator.Front') as front:
   c=Coordinator(d,'a'*64,'disposable',serve_control=False)
   try:
    server.assert_not_called();self.assertIsNone(c.catalog_server);self.assertIsNone(c.catalog_thread)
    self.assertEqual(c.doc['phase'],'INITIALIZING');self.assertFalse(c.normal_active)
   finally:os.close(c.owner_fd)
if __name__=='__main__':unittest.main()
