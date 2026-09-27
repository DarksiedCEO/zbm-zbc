"""
The engine-owned pytest plugin (round 19 R4). Shipped by the engine into the fresh verification container's engine
directory (hash pinned by ``runner.py``), loaded with ``-p zbm_engine_plugin`` and ``--disable-plugin-autoload`` so
nothing else is plugged in from a distribution entry point, and the engine directory first on ``pythonpath`` so
nothing in the tree can shadow it. It writes ``<junitxml>.zbm.json`` — a record the engine reads back and compares
with the junit file (``engine/parsers.py::plugin_record_problem``). Any disagreement is ``unknown``.

What it watches:

1. **Plugging.** Every ``pytest_plugin_registered`` after the session started must be a ``conftest.py`` module (the
   only registration collection legitimately performs); after collection finished, ANY registration is a violation.
   The plugin set (``pluginmanager.get_plugins()``) and every hook's implementation list are snapshotted at
   ``pytest_collection_finish`` and compared before every test protocol and at session end. A set of pytest and
   pluggy module attributes (the runner's functions, the report classes) is snapshotted at plugin load and compared
   at the same points — a source module that monkeypatches the runner at import time is caught at the next test.
2. **Outcomes, from four positions.** Per test phase: ``call.excinfo is None`` from a ``tryfirst`` non-wrapper
   ``pytest_runtest_makereport`` (returns None: it observes only), the raw report from the innermost wrapper
   (``trylast`` wrapper: nothing runs between it and pytest's own report builder), the final report from the
   outermost wrapper (``tryfirst`` wrapper) and the report ``pytest_runtest_logreport`` receives (``tryfirst``
   non-wrapper). The raw outcome must agree with ``excinfo``; the final outcome may differ from the raw one only
   through pytest's own xfail machinery (``wasxfail``); logreport must equal the final; every logged report must
   have been built by a makereport this plugin saw. The engine then checks every junit testcase against these.

This is a second net behind the plugin-set snapshot, stated as such: it catches a flip performed by code that was
already registered (a base-tree conftest, a builtin) and it makes a fabricated report visible; it does not see a
test that genuinely passes because a monkeypatched interpreter internal outside the snapshot made it pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

RECORD_SUFFIX = ".zbm.json"
_WATCHED_MODULES = ("_pytest.runner", "_pytest.python", "_pytest.skipping", "_pytest.junitxml", "_pytest.main",
                    "_pytest.nodes", "_pytest.reports", "_pytest.outcomes", "_pytest.config", "pluggy._hooks",
                    "pluggy._callers", "pluggy._manager", "pluggy._result")
_WATCHED_CLASSES = ("_pytest.runner:CallInfo", "_pytest.reports:TestReport", "_pytest.reports:BaseReport",
                    "_pytest.python:Function", "_pytest.nodes:Item", "_pytest.main:Session",
                    "_pytest.config:Config", "_pytest.config:PytestPluginManager", "pluggy._hooks:HookCaller",
                    "pluggy._hooks:HookImpl", "pluggy._manager:PluginManager")


def _self_sha256() -> str:
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def _attr_snapshot() -> dict[str, int]:
    """id() of every function/class attribute of the watched modules and classes (a replaced attribute changes id)."""
    out: dict[str, int] = {}
    for modname in _WATCHED_MODULES:
        mod = sys.modules.get(modname)
        if mod is None:
            continue
        out[modname] = id(mod)
        for k, v in vars(mod).items():
            if callable(v) and not k.startswith("__"):
                out[f"{modname}.{k}"] = id(v)
    for spec in _WATCHED_CLASSES:
        modname, cname = spec.split(":")
        mod = sys.modules.get(modname)
        cls = getattr(mod, cname, None) if mod is not None else None
        if cls is None:
            continue
        for k, v in vars(cls).items():
            if callable(v) or isinstance(v, (staticmethod, classmethod, property)):
                out[f"{spec}.{k}"] = id(v)
    return out


class ZbmEngineWatch:
    def __init__(self) -> None:
        self.record: dict = {"plugin_sha256": _self_sha256(), "plugin_file": os.path.abspath(__file__),
                             "session_started": False, "collection_finished": False, "session_finished": False,
                             "exitstatus": None, "collected": None, "violations": [], "tests": {}}
        self._attrs_at_load = _attr_snapshot()
        self._plugins: set[int] | None = None
        self._hookimpls: dict | None = None
        self._pm = None
        self._config = None
        self._record_path: str | None = None
        self._seen_make: set[tuple[str, str]] = set()

    # --- helpers ----------------------------------------------------------------------------------------------------

    def _violation(self, what: str) -> None:
        v = self.record["violations"]
        if len(v) < 200:
            v.append(what[:300])

    def _hook_snapshot(self) -> dict:
        out = {}
        for name in dir(self._pm.hook):
            if not name.startswith("pytest_"):
                continue
            caller = getattr(self._pm.hook, name)
            impls = getattr(caller, "get_hookimpls", None)
            if impls is None:
                continue
            out[name] = tuple((getattr(h, "plugin_name", None), id(getattr(h, "function", None))) for h in impls())
        return out

    def _check_snapshots(self, where: str) -> None:
        if self._pm is None:
            return
        now_plugins = {id(p) for p in self._pm.get_plugins()}
        if self._plugins is not None and now_plugins != self._plugins:
            self._violation(f"{where}: the plugin set changed after collection")
        if self._hookimpls is not None:
            now = self._hook_snapshot()
            changed = [n for n in set(now) | set(self._hookimpls) if now.get(n) != self._hookimpls.get(n)]
            if changed:
                self._violation(f"{where}: hook implementations changed after collection: {', '.join(sorted(changed)[:5])}")
        attrs = _attr_snapshot()
        moved = [k for k, v in self._attrs_at_load.items() if attrs.get(k) != v]
        if moved:
            self._violation(f"{where}: pytest/pluggy internals replaced: {', '.join(sorted(moved)[:5])}")

    def _phase(self, nodeid: str, when: str) -> dict:
        return self.record["tests"].setdefault(nodeid, {}).setdefault(when, {})

    def _write(self) -> None:
        if not self._record_path:
            return
        try:
            tmp = self._record_path + ".part"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.record, fh, sort_keys=True)
            os.replace(tmp, self._record_path)
        except OSError:
            pass

    # --- session --------------------------------------------------------------------------------------------------

    def pytest_configure(self, config) -> None:
        self._config = config
        self._pm = config.pluginmanager
        xml = getattr(config.option, "xmlpath", None)
        self._record_path = (str(xml) + RECORD_SUFFIX) if xml else None
        self.record["disable_plugin_autoload"] = bool(getattr(config.known_args_namespace, "disable_plugin_autoload", False)
                                                      or os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD"))
        self._write()

    def pytest_sessionstart(self, session) -> None:
        self.record["session_started"] = True
        self._write()

    def pytest_plugin_registered(self, plugin, plugin_name, manager) -> None:
        if not self.record["session_started"]:
            return
        if self.record["collection_finished"]:
            self._violation(f"plugin registered after collection: {plugin_name!r} ({type(plugin).__name__})")
            return
        file = getattr(plugin, "__file__", None)
        is_conftest = isinstance(file, str) and os.path.basename(file) == "conftest.py"
        builtin = str(getattr(type(plugin), "__module__", "")).startswith("_pytest.")   # FixtureManager at session start
        if not is_conftest and not builtin:
            self._violation(f"plugin registered during collection by a non-conftest: {plugin_name!r} ({type(plugin).__name__})")

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session) -> None:
        self.record["collection_finished"] = True
        self.record["collected"] = len(session.items)
        self._plugins = {id(p) for p in self._pm.get_plugins()}
        self._hookimpls = self._hook_snapshot()
        self._check_snapshots("collection_finish")
        self._write()

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self._check_snapshots(f"before {item.nodeid}")
        return None

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        self._check_snapshots("session_finish")
        self.record["session_finished"] = True
        self.record["exitstatus"] = int(getattr(exitstatus, "value", exitstatus))
        self._write()

    # --- per-test outcomes, four positions ------------------------------------------------------------------------

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        ph = self._phase(item.nodeid, call.when)
        ph["excinfo_none"] = call.excinfo is None
        ph["excinfo_type"] = type(call.excinfo.value).__name__ if call.excinfo is not None else None
        ph["xfail_marked"] = item.get_closest_marker("xfail") is not None
        self._seen_make.add((item.nodeid, call.when))
        return None

    @pytest.hookimpl(wrapper=True, trylast=True)
    def _inner_makereport(self, item, call):
        rep = yield
        ph = self._phase(item.nodeid, call.when)
        ph["raw_outcome"] = getattr(rep, "outcome", None)
        ph["raw_wasxfail"] = hasattr(rep, "wasxfail")
        return rep

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def _outer_makereport(self, item, call):
        rep = yield
        ph = self._phase(item.nodeid, call.when)
        ph["final_outcome"] = getattr(rep, "outcome", None)
        ph["final_wasxfail"] = hasattr(rep, "wasxfail")
        return rep

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logreport(self, report) -> None:
        nodeid, when = getattr(report, "nodeid", ""), getattr(report, "when", "")
        ph = self._phase(nodeid, when)
        ph["logged_outcome"] = getattr(report, "outcome", None)
        ph["logged_wasxfail"] = hasattr(report, "wasxfail")
        if (nodeid, when) not in self._seen_make:
            self._violation(f"report logged without a makereport this plugin saw: {nodeid} {when}")


def _register_wrappers(pm, watch: ZbmEngineWatch) -> None:
    """Two more implementations of ``pytest_runtest_makereport`` from separate plugin objects (pluggy takes one
    implementation per hook per plugin): the innermost and the outermost wrapper."""

    class _Inner:
        @pytest.hookimpl(wrapper=True, trylast=True)
        def pytest_runtest_makereport(self, item, call):
            return (yield from watch._inner_makereport(item, call))

    class _Outer:
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_makereport(self, item, call):
            return (yield from watch._outer_makereport(item, call))

    pm.register(_Inner(), "zbm_engine_watch_inner")
    pm.register(_Outer(), "zbm_engine_watch_outer")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config) -> None:
    """Registers the watch and its two wrapper plugins; ``pytest_configure`` is historic, so pytest calls the
    watch's own ``pytest_configure`` on registration."""
    pm = config.pluginmanager
    if pm.has_plugin("zbm_engine_watch"):
        return
    watch = ZbmEngineWatch()
    pm.register(watch, "zbm_engine_watch")
    _register_wrappers(pm, watch)
