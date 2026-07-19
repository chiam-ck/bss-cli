"""Semantic-layer test — registry matches TOOL_SURFACE.md 1:1.

TOOL_SURFACE.md is the human contract for what the LLM can do. The registry
is the machine contract. Drift between them is exactly the class of bug that
makes the LLM promise a tool that doesn't exist, or hide a tool that does.

We extract the dotted tool names from the markdown tables (cells of the form
`` `domain.action` ``) and assert:

    set(markdown) == set(registry) for the LLM-exposed tools

Admin and Knowledge (post-v0.1) tools are explicitly excluded — they live
in namespaces not registered in v0.1.
"""

from __future__ import annotations

import re
from pathlib import Path

from bss_orchestrator.tools import TOOL_REGISTRY

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_SURFACE = _REPO_ROOT / "TOOL_SURFACE.md"

# Namespaces documented but not in the v0.1 LLM-exposed set we sync-check.
# - admin.*: CLI-only, not exposed to the LLM
# - billing.*: deferred to v0.2 (see DECISIONS.md 2026-04-13)
#
# v0.20 update: knowledge.* WAS excluded as "post-v0.1 / Phase 11 not
# implemented" but shipped activated in v0.20. Removed from the
# exclusion list so sync drift is caught both ways.
_EXCLUDED_NAMESPACES = ("admin.", "billing.")


def _markdown_tools() -> set[str]:
    """Parse all dotted ``domain.action`` identifiers appearing in tables."""
    text = _TOOL_SURFACE.read_text(encoding="utf-8")
    # Match `` `foo.bar` `` or `` `foo.bar.baz` `` inside a table cell.
    pattern = re.compile(r"\|\s*`([a-z_]+(?:\.[a-z_]+)+)`")
    found = {m.group(1) for m in pattern.finditer(text)}
    return {t for t in found if not t.startswith(_EXCLUDED_NAMESPACES)}


def test_registry_matches_tool_surface_md() -> None:
    doc_tools = _markdown_tools()
    reg_tools = {t for t in TOOL_REGISTRY.keys() if not t.startswith(_EXCLUDED_NAMESPACES)}

    missing_in_registry = doc_tools - reg_tools
    extra_in_registry = reg_tools - doc_tools

    assert not missing_in_registry, (
        "TOOL_SURFACE.md lists tools that are not implemented:\n  "
        + "\n  ".join(sorted(missing_in_registry))
    )
    assert not extra_in_registry, (
        "Registry has tools not documented in TOOL_SURFACE.md "
        "(either doc or code is out of sync):\n  "
        + "\n  ".join(sorted(extra_in_registry))
    )


def test_tool_surface_md_is_nonempty() -> None:
    # Guardrail: if the regex ever breaks we'd silently pass the sync check
    # above with an empty set on both sides.
    assert len(_markdown_tools()) >= 60, (
        "TOOL_SURFACE.md parse produced suspiciously few tool names — "
        "the regex probably needs updating."
    )
