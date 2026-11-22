"""G-7 / SUITE_PLAN "Registry": every item of core-spec/1 has a test, every registered test still exists, and the
count of conformance tests never drops below the committed floor (the suite only grows)."""
import json
from pathlib import Path

import pytest

from conformance.spec_items import ITEMS
from conformance.tools import registry as RG

pytestmark = pytest.mark.conformance
REG = json.loads((Path(__file__).resolve().parent / "registry.json").read_text(encoding="ascii"))


def test_core_G_7_every_spec_item_has_a_test():
    assert REG["spec"] == "core-spec/1"
    missing = [i for i in ITEMS if not REG["items"].get(i)]
    assert not missing, missing


def test_core_G_7_registered_tests_exist_and_the_suite_only_grows():
    nodes = set(RG.collect())
    gone = sorted({n for v in REG["items"].values() for n in v if not n.startswith("gate:")} - nodes)
    assert not gone, gone[:10]
    assert len(nodes) >= REG["floor"]
