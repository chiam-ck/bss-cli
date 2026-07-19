"""Order renderer — order header + SOM decomposition tree."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ._box import box

# State-column width — every node line right-aligns its state in this
# many characters so the column lines up regardless of tree depth.
_STATE_COL = 14
# Width budget for the title text inside the tree (label + id) before
# the state column.
_TREE_LABEL_WIDTH = 40

_STUCK_OR_FAILED = {"failed", "stuck", "errored", "canceled", "cancelled"}


def _state_marker(state: str) -> str:
    """⚠ flag prefix for failed/stuck states."""
    return "⚠ " if state.lower() in _STUCK_OR_FAILED else "  "


def _state_col(state: str) -> str:
    """Right-aligned state column with optional warning marker."""
    return f"{_state_marker(state)}{state.lower():>{_STATE_COL - 2}}"


def _line(prefix: str, label: str, state: str = "") -> str:
    """Render one tree row. ``prefix`` is the ASCII tree indent +
    branch glyph; ``label`` the node text; ``state`` the right-column."""
    full = f"{prefix}{label}"
    pad = max(1, _TREE_LABEL_WIDTH + 4 - len(full))
    return f"{full}{' ' * pad}{_state_col(state)}"


def render_order(
    order: dict[str, Any],
    *,
    service_orders: list[dict[str, Any]] | None = None,
    services_by_so: dict[str, list[dict[str, Any]]] | None = None,
    tasks_by_service: dict[str, list[dict[str, Any]]] | None = None,
    subscription_id: str | None = None,
) -> str:
    """Render a product order with full SOM decomposition.

    Tree shape:

        Order ORD-014                                   completed
        └─ Service Order SO-022                         completed
           └─ CFS SVC-101  MobileBroadband              completed
              ├─ RFS SVC-102  Data                      completed
              │  ├─ PTK-001  hlr.activate               completed   (1.3s)
              │  ├─ PTK-002  pcrf.create_session        completed   (0.8s)
              │  └─ PTK-003  ocs.allocate_quota       ⚠ failed      (2 attempts)
              └─ RFS SVC-103  Voice                     completed
                 └─ PTK-004  hlr.subscribe              completed   (0.4s)
        → Subscription SUB-007 activated
    """
    oid = order.get("id", "ORD-???")
    state = order.get("state", "?")
    cust_id = order.get("customerId", "—")
    items = order.get("items") or []
    offering = items[0].get("offeringId", "—") if items else "—"
    placed = order.get("orderDate") or "—"
    completed = order.get("completedDate") or "—"

    lines: list[str] = [
        f"Customer:  {cust_id}",
        f"Placed:    {placed}",
        f"Completed: {completed}",
        "",
    ]

    service_orders = service_orders or []
    services_by_so = services_by_so or {}
    tasks_by_service = tasks_by_service or {}

    # Root row: the order itself
    lines.append(_line("", f"Order {oid}", state))

    total_so = len(service_orders)
    for so_idx, so in enumerate(service_orders):
        so_id = so.get("id", "SO-???")
        so_state = so.get("state", "?")
        is_last_so = so_idx == total_so - 1
        so_branch = "└─ " if is_last_so else "├─ "
        so_indent = "   " if is_last_so else "│  "
        lines.append(_line(so_branch, f"Service Order {so_id}", so_state))

        services = services_by_so.get(so_id, [])
        cfs_list = [s for s in services if s.get("serviceType") == "CFS"]
        rfs_list = [s for s in services if s.get("serviceType") == "RFS"]

        for ci, cfs in enumerate(cfs_list):
            is_last_cfs = ci == len(cfs_list) - 1 and not rfs_list
            cfs_branch = "└─ " if is_last_cfs else "├─ "
            cfs_indent = "   " if is_last_cfs else "│  "
            label = f"CFS {cfs.get('id')}  {cfs.get('name', '')}".rstrip()
            lines.append(_line(so_indent + cfs_branch, label, cfs.get("state", "")))

            for ri, rfs in enumerate(rfs_list):
                is_last_rfs = ri == len(rfs_list) - 1
                rfs_branch = "└─ " if is_last_rfs else "├─ "
                rfs_indent = "   " if is_last_rfs else "│  "
                rlabel = f"RFS {rfs.get('id')}  {rfs.get('name', '')}".rstrip()
                lines.append(
                    _line(so_indent + cfs_indent + rfs_branch, rlabel, rfs.get("state", ""))
                )

                tasks = tasks_by_service.get(rfs.get("id"), [])
                for ti, task in enumerate(tasks):
                    is_last_task = ti == len(tasks) - 1
                    task_branch = "└─ " if is_last_task else "├─ "
                    tstate = task.get("state", "")
                    duration = _fmt_duration(task)
                    attempts = task.get("attemptCount") or task.get("attempts") or 1
                    suffix_bits = []
                    if duration:
                        suffix_bits.append(duration)
                    if attempts and int(attempts) > 1:
                        suffix_bits.append(f"{attempts} attempts")
                    suffix = "  " + "  ".join(suffix_bits) if suffix_bits else ""
                    tlabel = f"{task.get('id')}  {task.get('taskType', '')}".rstrip()
                    line = _line(
                        so_indent + cfs_indent + rfs_indent + task_branch,
                        tlabel,
                        tstate,
                    )
                    lines.append(line + suffix)

        if not cfs_list and not rfs_list:
            lines.append(so_indent + "(no services attached)")

    if subscription_id:
        lines.append("")
        lines.append(f"→ Subscription {subscription_id} activated")

    # Summary footer with totals
    lines.append("")
    lines.append(_summary_line(order, service_orders, tasks_by_service))

    title = f"{oid}  {offering}"
    return box(lines, title=title, width=86)


def _summary_line(
    order: dict[str, Any],
    service_orders: list[dict[str, Any]],
    tasks_by_service: dict[str, list[dict[str, Any]]],
) -> str:
    """Bottom row with elapsed time + per-stage breakdown."""
    placed = order.get("orderDate")
    completed = order.get("completedDate")
    elapsed = ""
    if placed and completed:
        try:
            p = datetime.fromisoformat(placed.replace("Z", "+00:00"))
            c = datetime.fromisoformat(completed.replace("Z", "+00:00"))
            elapsed = f"  total {(c - p).total_seconds():.1f}s"
        except ValueError:
            pass
    n_so = len(service_orders)
    n_tasks = sum(len(v) for v in tasks_by_service.values())
    n_failed = sum(
        1
        for tasks in tasks_by_service.values()
        for t in tasks
        if str(t.get("state", "")).lower() in _STUCK_OR_FAILED
    )
    parts = [f"{n_so} SO"]
    if n_tasks:
        parts.append(f"{n_tasks} tasks")
    if n_failed:
        parts.append(f"{n_failed} failed")
    return f"Summary: {' · '.join(parts)}{elapsed}"


def _fmt_duration(task: dict[str, Any]) -> str:
    started = task.get("startedAt")
    completed = task.get("completedAt")
    if not started or not completed:
        return ""
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        c = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        sec = (c - s).total_seconds()
        return f"({sec:.1f}s)"
    except ValueError:
        return ""
