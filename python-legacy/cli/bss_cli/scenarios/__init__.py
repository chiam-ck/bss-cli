"""YAML scenario runner — deterministic + (Phase-10 task #6) LLM mode.

Top-level API:

    from bss_cli.scenarios import load_scenario, run_scenario

A scenario is a YAML file validated against ``schema.Scenario``. The runner
walks ``setup → steps → teardown`` executing each step in its declared mode
(``action:`` → deterministic tool call, ``ask:`` → LLM). Captures flow through
the ``ScenarioContext`` so later steps can reference ``{{ variables }}``.

``action:`` dispatch goes through ``actions.ACTION_REGISTRY`` which composes
orchestrator ``TOOL_REGISTRY`` with a small set of scenario-only verbs
(``admin.reset_operational_data``, ``clock.freeze``, ``clock.unfreeze``,
``clock.advance``) that fan out to every service's admin surface.
"""

from __future__ import annotations

from .runner import load_scenario, run_scenario
from .schema import Scenario

__all__ = ["Scenario", "load_scenario", "run_scenario"]
