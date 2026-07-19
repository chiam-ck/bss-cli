"""Unit tests for the ASCII-box primitives.

The renderers are the LLM's visualisation language; if the primitives
produce misaligned frames or miscounted bars, the LLM-driven summaries
end up ragged in the terminal.
"""

from __future__ import annotations

from bss_cockpit.renderers._box import (
    box,
    format_iccid,
    format_msisdn,
    progress_bar,
    state_dot,
)


def test_state_dot_uppercases_and_prefixes_bullet() -> None:
    assert state_dot("active") == "● ACTIVE"
    assert state_dot("blocked") == "● BLOCKED"


def test_progress_bar_empty() -> None:
    bar = progress_bar(0, 100, width=10)
    assert bar == "[" + "░" * 10 + "]"


def test_progress_bar_full_clamps() -> None:
    # Usage over total clamps to 100%, no overflow.
    bar = progress_bar(150, 100, width=10)
    assert bar == "[" + "█" * 10 + "]"


def test_progress_bar_unlimited_uses_dash_fill() -> None:
    bar = progress_bar(1, None, width=6)
    assert bar == "[" + "─" * 6 + "]"


def test_box_frame_is_rectangular() -> None:
    rendered = box(["hello", "world"], title="T", width=20)
    lines = rendered.splitlines()
    # Top + 2 body + bottom
    assert len(lines) == 4
    # All lines same visible length.
    assert len({len(line) for line in lines}) == 1
    assert lines[0].startswith("┌") and lines[0].endswith("┐")
    assert lines[-1].startswith("└") and lines[-1].endswith("┘")


def test_box_truncates_oversized_content() -> None:
    rendered = box(["x" * 100], title="T", width=20)
    lines = rendered.splitlines()
    # Body line must not be wider than the frame.
    assert all(len(line) == len(lines[0]) for line in lines)


def test_format_msisdn_splits_8_digits() -> None:
    assert format_msisdn("90000005") == "9000 0005"


def test_format_msisdn_passes_through_non_8_digit() -> None:
    assert format_msisdn("+6590000005") == "+6590000005"
    assert format_msisdn("") == ""


def test_format_iccid_groups_in_fours() -> None:
    iccid = "8910101000000000005"
    got = format_iccid(iccid)
    assert got == "8910 1010 0000 0000 005"
