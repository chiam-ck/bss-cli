"""ASCII swimlane renderer for a Jaeger trace.

The visual artifact for v0.2. Takes a Jaeger v1 trace dict (per
``JaegerClient.get_trace``) and produces a wide ASCII rendering with:

- One row per span, indented by parent-child depth
- Service name in a left column, span name on the right
- Bar showing relative duration within the trace
- Optional SQL collapsing (default on)
- Manual-span asterisks for the 3 named business spans

Uses Rich for output formatting; renders to plain text with
``--width`` to override the terminal width.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any

# Manual-span operation names from V0_2_0.md §2a.
MANUAL_SPAN_NAMES = frozenset({
    "com.order.complete_to_subscription",
    "som.decompose",
    "subscription.purchase_vas",
    "bss.ask",
})


@dataclass
class _RenderSpan:
    span_id: str
    parent_id: str | None
    service: str
    operation: str
    start_micros: int
    duration_micros: int
    is_error: bool
    is_sql: bool
    is_manual: bool
    # v0.9 — perimeter-resolved identity from the bss.service.identity
    # span tag. Empty string when the span pre-dates v0.9 or the
    # request never reached RequestIdMiddleware (rare; auth-401 paths).
    service_identity: str = ""
    depth: int = 0


def _normalize(trace: dict[str, Any]) -> tuple[list[_RenderSpan], int, int]:
    """Flatten a Jaeger trace into a list of _RenderSpan rows + trace bounds."""
    processes = trace.get("processes", {})
    spans = trace.get("spans", [])
    if not spans:
        return [], 0, 0

    rows: list[_RenderSpan] = []
    for s in spans:
        process_id = s.get("processID", "")
        service = processes.get(process_id, {}).get("serviceName", "?")
        operation = s.get("operationName", "?")
        is_sql = operation.upper() in {"BEGIN;", "COMMIT;", "ROLLBACK;"} or operation.startswith(
            ("SELECT", "INSERT", "UPDATE", "DELETE")
        )
        is_error = any(
            tag.get("key") == "error" and tag.get("value") is True
            for tag in s.get("tags", [])
        )
        is_manual = operation in MANUAL_SPAN_NAMES
        # v0.9 — surface bss.service.identity per span. Tag is set by
        # the per-request span hook in each service's middleware.
        service_identity = ""
        for tag in s.get("tags", []):
            if tag.get("key") == "bss.service.identity":
                service_identity = str(tag.get("value", "") or "")
                break
        # Find parent span_id from references
        parent_id = None
        for ref in s.get("references", []):
            if ref.get("refType") == "CHILD_OF":
                parent_id = ref.get("spanID")
                break
        rows.append(
            _RenderSpan(
                span_id=s.get("spanID", ""),
                parent_id=parent_id,
                service=service,
                operation=operation,
                start_micros=int(s.get("startTime", 0)),
                duration_micros=int(s.get("duration", 0)),
                is_error=is_error,
                is_sql=is_sql,
                is_manual=is_manual,
                service_identity=service_identity,
            )
        )

    trace_start = min(r.start_micros for r in rows)
    trace_end = max(r.start_micros + r.duration_micros for r in rows)
    return rows, trace_start, trace_end


def _assign_depths(rows: list[_RenderSpan]) -> None:
    """Walk the parent chain to assign each row a depth."""
    by_id = {r.span_id: r for r in rows}
    for r in rows:
        depth = 0
        cur = r
        seen: set[str] = set()
        while cur.parent_id and cur.parent_id in by_id and cur.span_id not in seen:
            seen.add(cur.span_id)
            cur = by_id[cur.parent_id]
            depth += 1
        r.depth = depth


def _sort_by_tree_order(rows: list[_RenderSpan]) -> list[_RenderSpan]:
    """Sort spans into parent-then-children order (DFS by start_micros)."""
    by_parent: dict[str | None, list[_RenderSpan]] = {}
    for r in rows:
        by_parent.setdefault(r.parent_id, []).append(r)
    for siblings in by_parent.values():
        siblings.sort(key=lambda x: x.start_micros)

    output: list[_RenderSpan] = []

    def walk(span_id: str | None) -> None:
        for child in by_parent.get(span_id, []):
            output.append(child)
            walk(child.span_id)

    # Roots have parent_id None
    walk(None)
    # Orphans (parent missing in batch — shouldn't normally happen)
    for r in rows:
        if r not in output:
            output.append(r)
    return output


def render_swimlane(
    trace: dict[str, Any],
    *,
    width: int | None = None,
    show_sql: bool = False,
    only_service: str | None = None,
) -> str:
    """Render the Jaeger trace as an ASCII swimlane."""
    rows, trace_start, trace_end = _normalize(trace)
    if not rows:
        return "(empty trace)\n"

    _assign_depths(rows)
    rows = _sort_by_tree_order(rows)

    if only_service:
        rows = [r for r in rows if r.service == only_service]

    visible = [r for r in rows if show_sql or not r.is_sql]
    hidden_sql = len(rows) - len(visible)

    services = sorted({r.service for r in rows})
    error_count = sum(1 for r in rows if r.is_error)
    total_micros = trace_end - trace_start
    total_ms = total_micros / 1000.0

    # Layout
    term_width = width or shutil.get_terminal_size((140, 24)).columns
    indent_per_level = 2
    max_depth = max((r.depth for r in visible), default=0)
    label_col_w = 14 + indent_per_level * max(max_depth, 1)
    duration_col_w = 8
    op_col_w = 40  # wide enough for the longest manual-span name (`com.order.complete_to_subscription` = 34) + asterisk
    # v0.9 — perimeter identity column. Wide enough for "portal_self_serve"
    # (17 chars) plus a leading space; truncate longer identities. Hidden
    # entirely if no span has a tag (pre-v0.9 traces stay clean).
    has_identity = any(r.service_identity for r in visible)
    identity_col_w = 18 if has_identity else 0
    bar_w = max(20, term_width - label_col_w - identity_col_w - duration_col_w - op_col_w - 4)

    trace_id = trace.get("traceID") or (
        rows[0].span_id if rows else "<unknown>"
    )
    trace_id_short = (trace_id[:16] + "…") if len(trace_id) > 16 else trace_id

    lines: list[str] = []
    lines.append(
        f"Trace {trace_id_short}  total {total_ms:.0f}ms  ·  "
        f"{len(rows)} spans  ·  {len(services)} services  ·  "
        f"{error_count} errors"
    )
    lines.append("")

    for r in visible:
        # Bar position
        if total_micros > 0:
            offset_frac = (r.start_micros - trace_start) / total_micros
            width_frac = max(r.duration_micros / total_micros, 0.001)
        else:
            offset_frac = 0.0
            width_frac = 1.0
        offset = int(offset_frac * bar_w)
        bar_chars = max(1, int(width_frac * bar_w))
        bar = " " * offset + "┃" + "━" * max(0, bar_chars - 2) + ("┃" if bar_chars >= 2 else "")
        bar = bar.ljust(bar_w)

        indent = " " * (indent_per_level * r.depth)
        svc_label = (indent + r.service).ljust(label_col_w)
        ms = r.duration_micros / 1000.0
        dur_label = f"{ms:>6.0f}ms"
        marker = " *" if r.is_manual else "  "
        op_label = r.operation
        if len(op_label) > op_col_w:
            op_label = op_label[: op_col_w - 1] + "…"

        # v0.9 — render the identity column when at least one span carries a tag.
        if identity_col_w > 0:
            ident = r.service_identity or "—"
            if len(ident) > identity_col_w - 1:
                ident = ident[: identity_col_w - 2] + "…"
            identity_label = ident.ljust(identity_col_w)
            line = f"{svc_label}{identity_label}{bar}  {dur_label}  {op_label}{marker}"
        else:
            line = f"{svc_label}{bar}  {dur_label}  {op_label}{marker}"
        if r.is_error:
            # Wrap full line in red ANSI
            line = f"\033[31m{line} ERR\033[0m"
        lines.append(line)

    if hidden_sql > 0:
        lines.append("")
        lines.append(f"⋯ {hidden_sql} SQL spans hidden — rerun with --show-sql to expand")
    lines.append("")
    lines.append("* business span (manually instrumented)")
    return "\n".join(lines) + "\n"
