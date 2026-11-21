"""Load only this checkout's packaged cold runtime for focused offline tests."""
from pathlib import Path
import sys
REPOSITORY=Path(__file__).resolve().parents[2]
RUNTIME=REPOSITORY/'deploy'/'docker'/'maintenance'/'cold'
if not (RUNTIME/'owner.py').is_file():raise RuntimeError('packaged cold runtime absent')
sys.path.insert(0,str(RUNTIME))
