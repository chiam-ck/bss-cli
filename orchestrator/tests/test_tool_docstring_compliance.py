"""Semantic-layer test — every registered tool has a full docstring.

The LLM sees each tool's description via its docstring. The doctrine is:
every tool has ``Args:``, ``Returns:``, ``Raises:`` sections. Missing
sections mean the LLM runs blind, which breaks small models like MiMo v2
Flash in predictable ways (fabricated IDs, wrong enums, skipped recoveries).

This test is grep-strict. If a new tool is added without the three sections,
the test fails with a message pointing at the tool name.
"""

from __future__ import annotations

import pytest
from bss_orchestrator.tools import TOOL_REGISTRY

REQUIRED_SECTIONS = ("Args:", "Returns:", "Raises:")


@pytest.mark.parametrize("name", sorted(TOOL_REGISTRY))
def test_tool_has_docstring_sections(name: str) -> None:
    fn = TOOL_REGISTRY[name]
    doc = fn.__doc__ or ""
    assert doc.strip(), f"Tool {name!r} has no docstring"
    # Docstring must summarise behaviour — at least 40 chars of non-whitespace.
    body = " ".join(doc.split())
    assert len(body) >= 40, (
        f"Tool {name!r} docstring is too short to guide an LLM: {body!r}"
    )
    missing = [s for s in REQUIRED_SECTIONS if s not in doc]
    assert not missing, (
        f"Tool {name!r} missing docstring sections: {missing}. "
        "Every LLM-exposed tool needs Args/Returns/Raises."
    )
