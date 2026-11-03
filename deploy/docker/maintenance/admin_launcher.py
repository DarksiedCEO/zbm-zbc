"""Fixed installed admin entrypoint; isolated Python ignores ambient imports."""
import sys
sys.path.insert(0,'/opt/zbm-maintenance/cold')
from admin_entrypoint import main
if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception:
  print('BACKUP_ADMIN_REFUSED',file=sys.stderr)
  raise SystemExit(2)
