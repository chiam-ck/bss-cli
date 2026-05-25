"""Shared fixtures for the v1.4 Playwright suite.

The contract every spec sees:

* ``base_urls`` — dict with ``self_serve`` and ``cockpit`` URLs.
* ``mailbox_path`` — ``Path`` to the LoggingEmailAdapter file under
  ``<repo-root>/.dev-mailbox/portal-mailbox.log``.
* ``browser`` (session) — a Playwright chromium ``Browser`` launched with
  the system-chromium resolver.
* ``page`` (function) — a fresh context + page per test, so cookies don't
  leak across specs.
* ``e2e_customer_email`` (function) — a unique ``e2e-<uuid>@bss-cli.local``
  per test, so the suite can run repeatedly without collisions.

Fixtures use the synchronous Playwright API (``playwright.sync_api``)
because Playwright's recommended pytest integration is sync-first and
the suite's setup-via-clients work happens in fixture finalize using
``asyncio.run`` islands rather than a global event loop.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from bss_e2e.helpers.chromium import launch_kwargs

# Repo root resolved relative to this file: packages/bss-e2e/tests/conftest.py
# → ../../../  = repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def base_urls() -> dict[str, str]:
    """Surface URLs. Overridable via env for non-default compose ports."""
    return {
        "self_serve": os.environ.get("BSS_E2E_SELF_SERVE_URL", "http://localhost:9001"),
        "cockpit": os.environ.get("BSS_E2E_COCKPIT_URL", "http://localhost:9002"),
    }


@pytest.fixture(scope="session")
def mailbox_path() -> Path:
    """Path to the LoggingEmailAdapter mailbox file on the host."""
    return REPO_ROOT / ".dev-mailbox" / "portal-mailbox.log"


@pytest.fixture(scope="session")
def browser() -> Iterator:
    """Session-scoped Playwright chromium browser.

    Imported lazily so ``pytest --collect-only`` works even when
    playwright isn't installed yet (e.g. fresh checkout pre ``uv sync``).
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs())
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(browser) -> Iterator:
    """Per-test browser context + page. Cleaned up at teardown."""
    context = browser.new_context(
        viewport={"width": 1280, "height": 1040},
        color_scheme="light",
    )
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def e2e_customer_email() -> str:
    """Unique ``e2e-<short-uuid>@bss-cli.local`` per test."""
    short = uuid.uuid4().hex[:10]
    return f"e2e-{short}@bss-cli.local"
