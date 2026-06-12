"""Shared fixtures for the v1.4 Playwright suite.

v1.4.1 visual-artefact contract: every spec produces a folder of named
screenshots + a Playwright trace zip + a video recording, under
``docs/e2e-reports/<UTC-ts>/<spec-slug>/``. ``pytest_sessionfinish``
generates a top-level ``index.html`` gallery linking all of them so an
operator (or reviewer) can open one file and watch the whole run.

The contract every spec sees:

* ``base_urls`` — dict with ``self_serve`` and ``cockpit`` URLs.
* ``mailbox_path`` — ``Path`` to the LoggingEmailAdapter file under
  ``<repo-root>/.dev-mailbox/portal-mailbox.log``.
* ``browser`` (session) — a Playwright chromium ``Browser`` launched with
  the system-chromium resolver.
* ``page`` (function) — a fresh context + page per test. The context has
  ``tracing`` started + ``record_video_dir`` set; the spec's tear-down
  finalises both into the spec dir.
* ``snap`` (function) — call ``snap("label")`` at meaningful checkpoints;
  writes ``NN-label.png`` into the spec dir. Step counter increments
  per call so screenshots sort chronologically.
* ``e2e_customer_email`` (function) — unique ``e2e-<uuid>@bss-cli.local``.
* ``available_msisdn`` (function) — a fresh ``available`` MSISDN from the
  pool, dynamically picked at fixture time to dodge cross-run collisions.

Fixtures use the synchronous Playwright API (the recommended pattern
with pytest); async setup work runs on thread-isolated event loops via
:func:`_run_async_in_thread`.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from bss_e2e.helpers.chromium import launch_kwargs

# Repo root resolved relative to this file: packages/bss-e2e/tests/conftest.py
# → ../../../  = repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]


def _slugify(s: str) -> str:
    """Filename-safe slug: lowercase, alnum + dash, collapse runs."""
    s = s.lower().replace("_", "-")
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "spec"


# ── base url / mailbox / browser fixtures ──────────────────────────────────


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
    """Session-scoped Playwright chromium browser."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs())
        try:
            yield browser
        finally:
            browser.close()


# ── visual-artefact fixtures ────────────────────────────────────────────────


@pytest.fixture(scope="session")
def run_report_dir() -> Path:
    """Root directory for this run's artefacts.

    Honors ``BSS_E2E_REPORT_DIR`` (set by the Makefile ``e2e`` target so all
    pytest invocations in one make run share a timestamp). Falls back to a
    fresh UTC timestamp when running pytest directly during dev.
    """
    env_dir = os.environ.get("BSS_E2E_REPORT_DIR", "").strip()
    if env_dir:
        out = Path(env_dir)
    else:
        # Report-dir timestamp — wall-clock for filesystem naming, not a
        # state-machine input. bss_clock is for domain logic.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: bss-clock
        out = REPO_ROOT / "docs" / "e2e-reports" / ts
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.fixture
def spec_dir(request, run_report_dir: Path) -> Path:
    """Per-spec subdir under ``run_report_dir``. Slugified test name."""
    slug = _slugify(request.node.name)
    out = run_report_dir / slug
    if out.exists():
        # If the same spec is re-run within a single session (rare), wipe
        # so we don't mix screenshots from two runs.
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.fixture
def page(browser, spec_dir: Path) -> Iterator:
    """Per-test browser context + page with tracing + video recording.

    The context records its own video. The trace is started before the
    test runs and stopped at teardown into ``spec_dir/trace.zip``. Video
    finalises into ``spec_dir/video.webm`` after the context closes.
    """
    context = browser.new_context(
        viewport={"width": 1280, "height": 1040},
        color_scheme="light",
        record_video_dir=str(spec_dir),
        record_video_size={"width": 1280, "height": 1040},
    )
    context.tracing.start(screenshots=True, snapshots=True, sources=True)

    page = context.new_page()
    # Stash the spec_dir + step counter on the page so the ``snap``
    # fixture can write to the right place without a separate dependency.
    page._e2e_dir = spec_dir
    page._e2e_step = 0

    try:
        yield page
    finally:
        # Finalise trace first — context.close() voids the API.
        trace_path = spec_dir / "trace.zip"
        try:
            context.tracing.stop(path=str(trace_path))
        except Exception:  # noqa: BLE001 — best-effort artefact
            pass

        # Capture the video target path BEFORE the context closes
        # (Playwright finalises the file on context.close).
        video = page.video
        video_target = spec_dir / "video.webm"
        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass
        if video is not None:
            try:
                video.save_as(str(video_target))
            except Exception:  # noqa: BLE001 — best-effort artefact
                pass
        # Playwright's default ``record_video_dir`` writes a random-name
        # .webm alongside trace.zip. ``save_as`` copies to our target
        # path; the random one stays around — sweep it.
        for orphan in spec_dir.glob("*.webm"):
            if orphan != video_target:
                try:
                    orphan.unlink()
                except OSError:
                    pass


@pytest.fixture
def snap(page) -> Callable[[str], None]:
    """Capture a named, numbered screenshot into the spec dir.

    Step number auto-increments per call so the filesystem ordering
    matches the test's narrative order. Call after each meaningful
    page-state change — login complete, form filled, redirect landed,
    assertion target visible.
    """

    def _snap(label: str) -> None:
        page._e2e_step += 1
        slug = _slugify(label)
        path = page._e2e_dir / f"{page._e2e_step:02d}-{slug}.png"
        try:
            page.screenshot(path=str(path), full_page=False)
        except Exception:  # noqa: BLE001 — best-effort artefact, never block the test
            pass

    return _snap


# ── identity / inventory fixtures ───────────────────────────────────────────


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


# ── session-end hook: generate the visual gallery ──────────────────────────


def pytest_sessionfinish(session, exitstatus):
    """After the last spec ran, walk the report dir and produce a
    single-page ``index.html`` linking each spec's screenshots + trace +
    video. Best-effort: a generator failure logs but doesn't taint the
    session exit code (the test outcomes are what matter)."""
    env_dir = os.environ.get("BSS_E2E_REPORT_DIR", "").strip()
    if not env_dir:
        # Running pytest directly — no make-managed run dir to roll up.
        # Each invocation gets its own dir from run_report_dir; the user
        # can browse it manually but we don't auto-generate the gallery
        # without an explicit BSS_E2E_REPORT_DIR.
        return
    try:
        from bss_e2e.report import generate_index

        generate_index(Path(env_dir))
    except Exception as exc:  # noqa: BLE001
        # Print to stderr so make output shows it; never raise from a
        # session-finish hook (would mask test outcomes).
        import sys

        print(f"[bss-e2e] gallery generation failed: {exc}", file=sys.stderr)
