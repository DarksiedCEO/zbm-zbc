"""Process-local owned backend groups; uncertain groups are never forgotten.

New-boot recovery additionally requires a fresh isolated PID namespace. This is
not a cross-host process adoption or process-killing API.
"""
import os,threading
from owner_wire import Refused
lock=threading.RLock();groups=set()
def created(pid):
 with lock:groups.add(pid)
def reaped(pid):
 with lock:
  try:os.killpg(pid,0)
  except ProcessLookupError:groups.discard(pid)
  # Permission/other errors remain unresolved, never inferred absent.
def require_quiescent():
 with lock:
  for pid in tuple(groups):reaped(pid)
  if groups:raise Refused('owned backend group unresolved')
def require_fresh_namespace():
 if os.getpid()!=1:raise Refused('fresh isolated owner PID namespace required')
 from pathlib import Path
 if {int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit()}!={1}:raise Refused('unexpected process in recovery namespace')
