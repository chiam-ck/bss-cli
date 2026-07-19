"""Snapshot test for the v0.2 swimlane renderer.

Locks the visual shape of ``bss trace get`` output. The fixture
trace was captured once from a real ``customer_signup_and_exhaust``
hero scenario run, then committed — so trace IDs, durations, span
counts are all frozen. No masking needed.

To intentionally update the golden file after a renderer change:

    UPDATE_SWIMLANE_SNAPSHOTS=1 uv run pytest \\
        cli/tests/v0_2_0/test_swimlane_snapshot.py

Then review the diff and commit the new ``signup_swimlane.txt``
in the same PR as the renderer change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from bss_cockpit.renderers.trace import render_swimlane

_HERE = Path(__file__).resolve().parent
_FIXTURE = _HERE / "fixtures" / "signup_trace.json"
_GOLDEN = _HERE / "snapshots" / "signup_swimlane.txt"


def _render() -> str:
    trace = json.loads(_FIXTURE.read_text())
    return render_swimlane(trace, width=140, show_sql=False)


def test_swimlane_matches_committed_golden() -> None:
    actual = _render()

    if os.environ.get("UPDATE_SWIMLANE_SNAPSHOTS") == "1":
        _GOLDEN.write_text(actual)
        return

    expected = _GOLDEN.read_text()
    if actual == expected:
        return

    # Build a side-by-side diff for the failure message
    import difflib

    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile="snapshot (committed)",
            tofile="actual (current renderer)",
            lineterm="",
        )
    )
    raise AssertionError(
        "swimlane output differs from committed snapshot.\n\n"
        "If this change is intentional, regenerate with:\n"
        "    UPDATE_SWIMLANE_SNAPSHOTS=1 uv run pytest "
        f"{_HERE.relative_to(Path.cwd())}/test_swimlane_snapshot.py\n\n"
        f"--- diff ---\n{diff}"
    )


def test_renderer_handles_show_sql_toggle() -> None:
    """--show-sql should expand SQL spans (more lines than collapsed)."""
    trace = json.loads(_FIXTURE.read_text())
    collapsed = render_swimlane(trace, width=140, show_sql=False)
    expanded = render_swimlane(trace, width=140, show_sql=True)
    assert expanded.count("\n") > collapsed.count("\n")
    assert "SQL spans hidden" in collapsed
    assert "SQL spans hidden" not in expanded


def test_renderer_marks_manual_spans_with_asterisk() -> None:
    """som.decompose, com.order.complete_to_subscription, subscription.purchase_vas → ' *'."""
    trace = json.loads(_FIXTURE.read_text())
    out = render_swimlane(trace, width=140)
    # The signup trace covers the order-completion path which fires
    # som.decompose and com.order.complete_to_subscription. Both
    # must show the asterisk marker.
    assert "som.decompose" in out
    assert "com.order.complete_to_subscription" in out
    # Find the lines with manual spans and confirm asterisk
    for line in out.splitlines():
        if line.strip().endswith("som.decompose *"):
            return
    raise AssertionError("som.decompose row missing the manual-span asterisk")


def test_renderer_filter_by_service() -> None:
    """--service crm should only show bss-crm rows."""
    trace = json.loads(_FIXTURE.read_text())
    out = render_swimlane(trace, width=140, only_service="bss-crm")
    # Header is always present; body rows should all be bss-crm
    body_rows = [ln for ln in out.splitlines() if ln.startswith("bss-") or "bss-" in ln[:30]]
    for row in body_rows:
        assert "bss-crm" in row[:30], f"non-crm row leaked: {row[:50]}"
