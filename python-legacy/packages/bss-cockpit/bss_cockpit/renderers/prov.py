"""Provisioning task list renderer — simple one-line-per-task summary."""

from __future__ import annotations

from typing import Any


def render_prov_tasks(tasks: list[dict[str, Any]]) -> str:
    """Render a provisioning task list as an aligned text table."""
    if not tasks:
        return "(no tasks)"
    header = f"{'ID':<8} {'SERVICE':<10} {'TASK_TYPE':<22} {'STATE':<12} ATTEMPTS"
    lines = [header, "-" * len(header)]
    for t in tasks:
        lines.append(
            f"{t.get('id', ''):<8} "
            f"{t.get('serviceId', ''):<10} "
            f"{t.get('taskType', ''):<22} "
            f"{t.get('state', ''):<12} "
            f"{t.get('attempts', 0)}/{t.get('maxAttempts', 0)}"
        )
    return "\n".join(lines)
