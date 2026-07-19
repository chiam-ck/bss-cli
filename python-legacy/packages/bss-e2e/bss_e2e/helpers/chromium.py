"""System-chromium resolver for the v1.4 Playwright suite.

Resolution order:

1. ``PLAYWRIGHT_CHROMIUM_EXECUTABLE`` env var (operator override).
2. ``/snap/bin/chromium`` (the canonical install path on the
   ck@samurai-bot dev box; CK installed via snap during v1.1 screenshot
   capture when Playwright's bundled download failed on Ubuntu 26.04).
3. The Playwright browsers cache under ``~/.cache/ms-playwright``.

Returns ``None`` if nothing usable is found; the caller can then choose
between letting Playwright try its default (which may download) or failing
fast with a setup-instructions message.

Lifted with minor tweaks from ``docs/screenshots/capture_promo.py``.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_chromium() -> str | None:
    """Return an absolute path to a chromium executable, or None."""
    explicit = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if explicit and Path(explicit).is_file():
        return explicit

    snap = Path("/snap/bin/chromium")
    if snap.is_file() and os.access(snap, os.X_OK):
        return str(snap)

    cache = Path.home() / ".cache" / "ms-playwright"
    if cache.is_dir():
        for c in sorted(cache.glob("chromium-*/chrome-linux64/chrome"), reverse=True):
            if c.is_file() and os.access(c, os.X_OK):
                return str(c)

    return None


def launch_kwargs() -> dict:
    """Return ready-to-splat kwargs for ``playwright.chromium.launch``.

    Includes the system-chromium ``executable_path`` if resolvable, plus
    the ``--no-sandbox`` style args that snap chromium needs in headless
    CI-like environments.
    """
    kwargs: dict = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
        ],
    }
    chrome = resolve_chromium()
    if chrome:
        kwargs["executable_path"] = chrome
    return kwargs
