"""Unit tests for bss_cockpit.prompts.build_cockpit_prompt (v0.13 PR3).

The builder is pure (no I/O). Each test checks one composition rule.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bss_cockpit import build_cockpit_prompt
from bss_cockpit.conversation import PendingDestructive


def test_operator_md_is_prepended() -> None:
    md = "# Operator\n\nI am Ck."
    out = build_cockpit_prompt(operator_md=md)
    assert out.startswith("# Operator")


def test_invariants_block_always_present() -> None:
    out = build_cockpit_prompt(operator_md="")
    assert "Cockpit safety contract" in out
    assert "/confirm" in out


def test_customer_focus_block_when_set() -> None:
    out = build_cockpit_prompt(operator_md="", customer_focus="CUST-007")
    assert "Customer focus" in out
    assert "CUST-007" in out


def test_no_customer_focus_block_when_none() -> None:
    out = build_cockpit_prompt(operator_md="", customer_focus=None)
    assert "Customer focus" not in out


def test_pending_destructive_block_when_present() -> None:
    pd = PendingDestructive(
        tool_name="subscription.terminate",
        tool_args={"id": "SUB-7", "reason": "operator request"},
        proposal_message_id=42,
        proposed_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    out = build_cockpit_prompt(operator_md="", pending_destructive=pd)
    assert "Confirmed destructive action" in out
    assert "subscription.terminate" in out
    assert "SUB-7" in out


def test_pending_destructive_with_no_args_renders_clean() -> None:
    pd = PendingDestructive(
        tool_name="case.close",
        tool_args={},
        proposal_message_id=1,
        proposed_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    out = build_cockpit_prompt(operator_md="", pending_destructive=pd)
    assert "(no args)" in out


def test_extra_context_renders_as_kv_block() -> None:
    out = build_cockpit_prompt(
        operator_md="",
        extra_context={"model": "gemma", "session_id": "SES-1"},
    )
    assert "## Context" in out
    assert "- model: gemma" in out
    assert "- session_id: SES-1" in out


def test_blocks_separated_by_blank_lines() -> None:
    out = build_cockpit_prompt(
        operator_md="# A",
        customer_focus="CUST-1",
    )
    # Each pair of consecutive blocks should be separated by exactly
    # one blank line.
    assert "\n\n" in out
    assert "\n\n\n" not in out


def test_empty_operator_md_does_not_emit_leading_blank() -> None:
    out = build_cockpit_prompt(operator_md="")
    # Should start with the invariants block, not a blank line
    assert out.lstrip().startswith("## Cockpit safety contract")


def test_trailing_newline() -> None:
    out = build_cockpit_prompt(operator_md="# X")
    assert out.endswith("\n")
