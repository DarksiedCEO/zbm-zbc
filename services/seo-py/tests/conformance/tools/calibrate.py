"""SUITE_PLAN C-1, run ONCE at spec approval (never by a builder afterwards):

    python tests/conformance/tools/calibrate.py <release tree> <release name>

<release tree> is a ``git archive`` of the release's services/seo-py (never the working tree): the corpora of
``conformance.corpora`` are read in that release's lanes and written to ``data/baseline/<corpus>.<release>.json.gz``.
Counts are printed; no text."""
import logging
import os
import sys
import warnings

warnings.simplefilter("ignore")
logging.disable(logging.CRITICAL)
tree, release = sys.argv[1:3]
TESTS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.join(tree, "src"))
from agents import callum  # noqa: E402
from agents import secret_guard as sg  # noqa: E402

import sink_guard  # noqa: E402
import svc_audits  # noqa: E402
import svc_tenants  # noqa: E402
from conformance import baseline as B  # noqa: E402
from conformance import corpora as C  # noqa: E402

assert sg.__file__.startswith(os.path.abspath(tree)), sg.__file__
mods = {"sg": sg, "sink": sink_guard, "tenants": svc_tenants, "audits": svc_audits, "callum": callum}
which = sys.argv[3].split(",") if len(sys.argv) > 3 else ["neutral", "fuzz", "hosts", "real_hosts", "urls", "machine",
                                                          "wsample"]
for name in which:
    if name == "neutral":
        items = C.neutral()
        v = [B.code(C.lanes_text(sg, t)) for _l, _c, t in items]
    elif name == "fuzz":
        items = C.fuzz()
        v = [B.code(C.lanes_text(sg, t)) for _h, t in items]
    elif name == "hosts":
        items = C.hosts()
        v = [B.code(C.lanes_host(mods, h)) for _f, h in items]
    elif name == "real_hosts":
        items = C.real_hosts()
        v = [B.code(C.lanes_host(mods, h)) for h in items]
    elif name == "urls":
        items = C.urls()
        v = [B.code(C.lanes_url(sg, u)) for _l, _p, u in items]
    elif name == "wsample":
        items = C.wsample()
        v = [B.code(C.lanes_w(sg, t)) for _l, _j, t in items]
    else:
        items = C.machine()
        v = [B.code(C.lanes_machine(mods, x)) for _c, x in items]
    B.write(name, release, C.stream_hash(items), v)
    print(release, name, len(items), sum(1 for x in v if "1" in x.split("|")))
