"""Allowlist invariants — phases never indexed; every kind mapped."""

from __future__ import annotations

import re

from bss_knowledge.paths import INDEXED_PATHS, KIND_FOR_PATH, KIND_RANK_WEIGHTS


def test_no_phase_docs_in_allowlist():
    """Doctrine: phases/V0_*.md is intentionally NOT indexed. They're
    historical build plans and mislead the LLM."""
    for p in INDEXED_PATHS:
        assert not re.match(r"^phases/V0_", p), (
            f"phases/V0_*.md leaked into INDEXED_PATHS: {p}"
        )


def test_every_path_has_a_kind():
    for p in INDEXED_PATHS:
        assert p in KIND_FOR_PATH


def test_every_kind_has_a_rank_weight():
    for kind in set(KIND_FOR_PATH.values()):
        assert kind in KIND_RANK_WEIGHTS, f"missing rank weight for kind={kind}"


def test_doctrine_outranks_runbook():
    """Doctrine (CLAUDE.md) should beat runbooks for prohibition queries."""
    assert KIND_RANK_WEIGHTS["doctrine"] > KIND_RANK_WEIGHTS["runbook"]


def test_handbook_outranks_decisions():
    """Handbook should beat DECISIONS.md for how-to queries."""
    assert KIND_RANK_WEIGHTS["handbook"] > KIND_RANK_WEIGHTS["decisions"]


def test_handbook_path_kind():
    assert KIND_FOR_PATH["docs/HANDBOOK.md"] == "handbook"


def test_claude_path_kind():
    assert KIND_FOR_PATH["CLAUDE.md"] == "doctrine"


def test_runbooks_all_runbook_kind():
    for p in INDEXED_PATHS:
        if p.startswith("docs/runbooks/"):
            assert KIND_FOR_PATH[p] == "runbook"
