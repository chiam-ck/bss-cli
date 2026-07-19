"""Shared helpers for the v1.4 Playwright suite.

Three modules:

* :mod:`bss_e2e.helpers.chromium` — system-chromium resolver (snap, Playwright
  cache, env override). Lifted from ``docs/screenshots/capture_promo.py`` so
  the suite picks up an already-installed browser without a fresh download.
* :mod:`bss_e2e.helpers.otp` — tail the LoggingEmailAdapter mailbox to extract
  the most recent OTP for a given recipient address.
* :mod:`bss_e2e.helpers.seed` — e2e-prefix promo and customer factories
  layered on top of ``bss-clients``. Idempotent; surgical to
  ``PROMO_E2E_*`` / ``e2e-*@bss-cli.local``.
"""
