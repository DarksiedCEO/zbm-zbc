import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# api.py fails closed (raises at import time) if ZBM_SERVICE_TOKEN isn't
# set, so tests need a value in place before `from api import app` runs
# anywhere. This is a fixed test-only value, never used outside tests —
# real deployments must set their own via the environment.
TEST_SERVICE_TOKEN = "test-shared-secret-do-not-use-in-production"
os.environ.setdefault("ZBM_SERVICE_TOKEN", TEST_SERVICE_TOKEN)
