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


@pytest.fixture
def available_msisdn() -> str:
    """Pull a currently-``available`` MSISDN from ``inventory.msisdn_pool``.

    Per-spec, fresh — avoids cross-run collisions where a prior spec's
    subscription still holds the number. The signup funnel takes ``msisdn``
    as a query param and SOM hard-fails the order if it's unavailable, so
    binding to a known-free number at test time keeps the funnel green
    without relying on ``demo-restore`` between runs.

    Runs the async DB query on a dedicated thread with its own event loop —
    pytest-asyncio (strict mode) sometimes leaves the main thread's loop in
    an ambiguous state that ``asyncio.run()`` refuses to re-enter, but a
    fresh-thread fresh-loop is always safe.
    """
    from bss_e2e.helpers.seed import pick_available_msisdn

    return _run_async_in_thread(pick_available_msisdn())


def _run_async_in_thread(coro):
    """Run an async coroutine on a fresh event loop in a fresh thread."""
    import asyncio
    import threading

    box: list = []
    error: list = []

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box.append(loop.run_until_complete(coro))
        except BaseException as exc:  # noqa: BLE001 — re-raise on main
            error.append(exc)
        finally:
            loop.close()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if error:
        raise error[0]
    return box[0]
