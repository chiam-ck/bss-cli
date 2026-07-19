"""SSE encoding helpers."""

from __future__ import annotations

from bss_portal_ui.sse import format_frame, status_html


def test_format_frame_encodes_event_and_data_lines() -> None:
    out = format_frame("message", "<li>hi</li>")
    assert out == b"event: message\ndata: <li>hi</li>\n\n"


def test_format_frame_handles_unicode() -> None:
    out = format_frame("status", '<span class="dot done"></span> done ✓')
    assert b"\xe2\x9c\x93" in out  # ✓ as utf-8


def test_status_html_for_each_known_status() -> None:
    assert status_html("live") == '<span class="dot live"></span> live'
    assert status_html("done") == '<span class="dot done"></span> done'
    assert status_html("error") == '<span class="dot error"></span> error'
    assert status_html("idle") == '<span class="dot idle"></span> idle'


def test_status_html_unknown_falls_back_to_idle_class() -> None:
    out = status_html("unknown")
    assert 'class="dot idle"' in out
