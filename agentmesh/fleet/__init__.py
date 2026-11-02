"""Example fleet ("indian-avengers"): Planner, Coder and Reviewer agents that
hand work to each other, so AgentMesh has a real multi-agent workload to watch.

Public interface (build plan P4)::

    run_once(store, llm=None, seed=None) -> run_id
    run_forever(store, stop, interval_s=3.0, llm=None)
    add_cli(subparsers)  # registers `agentmesh fleet`

The default model is :class:`ScriptedLLM` (offline, deterministic with a
seed). :class:`ClaudeLLM` calls Claude when ``ANTHROPIC_API_KEY`` is set.
"""

from agentmesh.fleet.backlog import BACKLOG, GOALS
from agentmesh.fleet.cli import add_cli
from agentmesh.fleet.llm import (
    DEFAULT_CLAUDE_MODEL,
    ClaudeLLM,
    LLMError,
    LLMResult,
    ScriptedLLM,
    make_llm,
)
from agentmesh.fleet.runner import Fleet, RunSummary, run_fleet, run_forever, run_once

__all__ = [
    "BACKLOG",
    "DEFAULT_CLAUDE_MODEL",
    "GOALS",
    "ClaudeLLM",
    "Fleet",
    "LLMError",
    "LLMResult",
    "RunSummary",
    "ScriptedLLM",
    "add_cli",
    "make_llm",
    "run_fleet",
    "run_forever",
    "run_once",
]
