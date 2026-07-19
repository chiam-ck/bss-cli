"""Sanity-check the package's resource paths exist after install."""

from __future__ import annotations

from bss_portal_ui import STATIC_DIR, TEMPLATE_DIR


def test_template_dir_contains_shared_partials() -> None:
    assert (TEMPLATE_DIR / "partials" / "agent_log.html").is_file()
    assert (TEMPLATE_DIR / "partials" / "agent_event.html").is_file()


def test_static_dir_contains_vendored_htmx_and_base_css() -> None:
    assert (STATIC_DIR / "css" / "portal_base.css").is_file()
    assert (STATIC_DIR / "js" / "htmx.min.js").is_file()
    assert (STATIC_DIR / "js" / "htmx-sse.js").is_file()
