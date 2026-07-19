"""Shared agent log renderer tests — same projection across portals."""

from __future__ import annotations

from bss_orchestrator.session import (
    AgentEventError,
    AgentEventFinalMessage,
    AgentEventPromptReceived,
    AgentEventToolCallCompleted,
    AgentEventToolCallStarted,
)
from bss_portal_ui.agent_log import project, render_html


def test_project_prompt_keeps_full_text() -> None:
    long = "x" * 500
    proj = project(AgentEventPromptReceived(prompt=long))
    assert proj.kind == "prompt"
    assert proj.icon == "→"
    assert proj.detail == long
    assert proj.detail_full == long


def test_project_tool_call_started_includes_args_in_title() -> None:
    proj = project(
        AgentEventToolCallStarted(
            name="customer.get",
            args={"customer_id": "CUST-042"},
            call_id="c1",
        )
    )
    assert proj.kind == "tool_started"
    assert "customer.get" in proj.title
    assert 'customer_id="CUST-042"' in proj.title


def test_project_tool_call_completed_marks_error() -> None:
    proj = project(
        AgentEventToolCallCompleted(
            name="x", call_id="1", result="boom", is_error=True
        )
    )
    assert proj.kind == "tool_completed"
    assert proj.icon == "⚠"
    assert proj.is_error is True


def test_project_final_message() -> None:
    proj = project(AgentEventFinalMessage(text="all done"))
    assert proj.kind == "final"
    assert proj.icon == "✓"


def test_project_error() -> None:
    proj = project(AgentEventError(message="kaboom"))
    assert proj.kind == "error"
    assert proj.is_error is True


def test_render_html_returns_single_line_with_event_class() -> None:
    frag = render_html(AgentEventPromptReceived(prompt="hi"))
    assert "\n" not in frag
    assert "agent-event--prompt" in frag
    assert "→" in frag


def test_render_html_for_each_kind() -> None:
    cases = [
        (AgentEventPromptReceived(prompt="x"), "agent-event--prompt"),
        (AgentEventToolCallStarted(name="t", args={}, call_id="1"), "agent-event--tool_started"),
        (AgentEventToolCallCompleted(name="t", call_id="1", result="ok"), "agent-event--tool_completed"),
        (AgentEventFinalMessage(text="done"), "agent-event--final"),
        (AgentEventError(message="boom"), "agent-event--error"),
    ]
    for event, css_marker in cases:
        frag = render_html(event)
        assert css_marker in frag, f"missing {css_marker} in {frag!r}"
