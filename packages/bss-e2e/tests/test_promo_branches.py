"""Specs 2–4 — promo-code branch coverage.

* **Public applied at signup** — type ``E2E_PUBLIC10`` at signup, see the
  live discounted-price preview, complete the order, see the discount on
  the dashboard line card.
* **Targeted on dashboard** — assign ``PROMO_E2E_TARGETED`` to the test
  customer via ``bss promo assign`` before signup, see the targeted-offer
  card on the dashboard after activation.
* **Exhausted-code degrades** — type an already-exhausted public code at
  signup, order still completes at full price (the v1.1.3 fix).

All three placeholders during v1.4.0 phase 1.
"""

from __future__ import annotations

import pytest

from bss_e2e.helpers.seed import PROMO_PUBLIC_CODE, PROMO_TARGETED_ID  # noqa: F401


@pytest.mark.self_serve
def test_public_promo_applied_at_signup(page, base_urls, mailbox_path, e2e_customer_email):
    """Public code typed at signup discounts the first cycle."""
    pytest.skip("scaffold — implementation arrives in v1.4.0 phase 2")


@pytest.mark.self_serve
def test_targeted_promo_visible_on_dashboard(page, base_urls, mailbox_path, e2e_customer_email):
    """Targeted promo assigned upfront surfaces on the dashboard."""
    pytest.skip("scaffold — implementation arrives in v1.4.0 phase 2")


@pytest.mark.self_serve
def test_exhausted_promo_degrades_to_full_price(page, base_urls, mailbox_path, e2e_customer_email):
    """Exhausted code at signup — order completes at full price (v1.1.3)."""
    pytest.skip("scaffold — implementation arrives in v1.4.0 phase 2")
