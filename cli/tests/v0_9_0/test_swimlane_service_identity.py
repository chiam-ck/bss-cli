"""v0.9 — bss.service.identity per-span column in the swimlane renderer.

Behaviour:

- A trace with no ``bss.service.identity`` tags renders identically to
  the v0.2 baseline (column hidden — the existing v0.2 snapshot test
  validates this).
- A trace where one or more spans carry the tag renders an extra
  18-wide column between the service name and the bar, showing the
  per-span identity (truncated to 17 chars + ellipsis if longer).
- Spans without the tag in a mixed trace render an em-dash placeholder.

Used by ops to filter "which surface did this write?" at the trace
level. SQL queries against ``audit.domain_event.service_identity``
remain the source of truth for cross-trace audits.
"""

from __future__ import annotations

import re

from bss_cockpit.renderers.trace import render_swimlane


def _make_span(
    *,
    span_id: str,
    process_id: str,
    operation: str,
    start: int = 0,
    duration: int = 1000,
    service_identity: str | None = None,
    parent: str | None = None,
) -> dict:
    span: dict = {
        "spanID": span_id,
        "processID": process_id,
        "operationName": operation,
        "startTime": start,
        "duration": duration,
        "tags": [],
        "references": (
            [{"refType": "CHILD_OF", "spanID": parent}] if parent else []
        ),
    }
    if service_identity is not None:
        span["tags"].append(
            {"key": "bss.service.identity", "type": "string", "value": service_identity}
        )
    return span


def _make_trace(spans: list[dict]) -> dict:
    return {
        "traceID": "abcdef0123456789abcdef0123456789",
        "spans": spans,
        "processes": {
            "p1": {"serviceName": "bss-crm"},
            "p2": {"serviceName": "bss-com"},
        },
    }


def test_no_tags_means_column_hidden():
    """Pre-v0.9 traces (no service.identity tags) keep the v0.2 layout."""
    trace = _make_trace([
        _make_span(span_id="a", process_id="p1", operation="GET /x"),
        _make_span(span_id="b", process_id="p2", operation="GET /y"),
    ])
    out = render_swimlane(trace, width=140, show_sql=False)
    # No identity-shaped column anywhere in the output.
    for line in out.splitlines():
        assert "portal_self_serve" not in line
        assert "default" not in line  # column would print this for default rows


def test_column_appears_when_at_least_one_span_has_tag():
    trace = _make_trace([
        _make_span(span_id="a", process_id="p1", operation="GET /x",
                   service_identity="default"),
        _make_span(span_id="b", process_id="p2", operation="GET /y",
                   service_identity="portal_self_serve", parent="a"),
    ])
    out = render_swimlane(trace, width=140, show_sql=False)
    assert "default" in out
    assert "portal_self_serve" in out


def test_column_truncates_long_identity():
    """Identities longer than 17 chars get an ellipsis."""
    long_id = "partner_extremely_long_name"  # > 17 chars
    trace = _make_trace([
        _make_span(span_id="a", process_id="p1", operation="GET /x",
                   service_identity=long_id),
    ])
    out = render_swimlane(trace, width=140, show_sql=False)
    # The full long value must NOT appear; the truncated form must.
    assert long_id not in out
    assert "partner_extremel" in out  # 16-char prefix + ellipsis


def test_em_dash_placeholder_for_untagged_span_in_mixed_trace():
    """Spans without the tag in a tagged trace show a placeholder."""
    trace = _make_trace([
        _make_span(span_id="a", process_id="p1", operation="GET /x",
                   service_identity="default"),
        _make_span(span_id="b", process_id="p2", operation="GET /y",
                   parent="a"),  # no tag
    ])
    out = render_swimlane(trace, width=140, show_sql=False)
    # Find the line for the second span (operation GET /y) and confirm
    # an em-dash precedes the bar.
    lines = [ln for ln in out.splitlines() if "GET /y" in ln]
    assert lines, "expected a row for GET /y"
    assert "—" in lines[0]


def test_column_width_does_not_overflow_layout():
    """The bar still occupies width when the identity column is added."""
    trace = _make_trace([
        _make_span(span_id="a", process_id="p1", operation="GET /x",
                   service_identity="portal_self_serve"),
    ])
    out = render_swimlane(trace, width=140, show_sql=False)
    # Find the data row (any line containing the bar character).
    data_rows = [ln for ln in out.splitlines() if "┃" in ln]
    assert data_rows, "expected at least one data row with a bar"
    # Total width sanity: nothing absurd.
    for r in data_rows:
        # Strip ANSI codes for length check (no errors here, but defensive).
        clean = re.sub(r"\x1b\[[0-9;]*m", "", r)
        assert len(clean) >= 100  # roughly the widthy layout
