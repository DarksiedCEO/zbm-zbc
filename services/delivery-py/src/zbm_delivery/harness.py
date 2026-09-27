"""
The embedded deer-flow harness (spec D1, §0.4(a)): ``DeerFlowClient`` built from our pinned config with our
middlewares, an in-memory checkpointer per run (multi-turn state lives for the run and never on disk), no
subagents unless configured, and the prompt-level skill filter set to the manifest's names (empty). The DF gateway
(``backend/app/``), nginx, the frontend and the IM channels are never installed or exposed.

deer-flow reads a few things from the process environment (``DEER_FLOW_EXTENSIONS_CONFIG_PATH``, ``DEER_FLOW_HOME``
for thread data, and the ``$DLV_*`` values the config file references); ``prepare_environment`` sets exactly those,
after the gate accepted the configuration. The DF sandbox provider singleton is resolved by class path from the
config (``sandbox.use``), so ``provider`` returns OUR provider instance.
"""

from __future__ import annotations

import os
from typing import Sequence


def prepare_environment(settings, data_dir: str) -> None:
    os.environ["DEER_FLOW_EXTENSIONS_CONFIG_PATH"] = os.path.abspath(settings.extensions_config)
    os.environ["DEER_FLOW_HOME"] = os.path.join(os.path.abspath(data_dir), "deerflow-home")
    os.environ["DLV_SKILLS_ROOT"] = os.path.abspath(settings.skills_root)
    os.environ["DLV_SANDBOX_IMAGE"] = settings.sandbox_image or ""
    os.environ["DEER_FLOW_CONFIG_PATH"] = os.path.abspath(settings.deerflow_config)
    os.makedirs(os.environ["DEER_FLOW_HOME"], exist_ok=True)


def make_client(settings, thread_id: str, middlewares: Sequence, *, manifest_skill_names: list[str]):
    from deerflow.client import DeerFlowClient
    from langgraph.checkpoint.memory import InMemorySaver

    return DeerFlowClient(config_path=os.path.abspath(settings.deerflow_config), checkpointer=InMemorySaver(),
                          model_name="engine", thinking_enabled=False,
                          subagent_enabled=settings.max_subagents_per_run > 0, plan_mode=False,
                          available_skills=set(manifest_skill_names), middlewares=list(middlewares),
                          environment="engine")


def provider():
    from deerflow.config import get_app_config
    from deerflow.sandbox.sandbox_provider import get_sandbox_provider

    get_app_config()
    return get_sandbox_provider()
