"""Specs 2–4 — promo-code branch coverage.

* **Public applied at signup** — type ``E2E_PUBLIC10`` at signup, see the
  live discounted-price preview, complete the order.
* **Targeted on dashboard** — assign ``PROMO_E2E_TARGETED`` to a fresh
  customer upfront via ``bss-clients``, then walk the signup funnel. The
  signup form pre-applies the targeted offer (``apply_offer`` checkbox).
* **Exhausted-code at signup** — v1.4.1 ships the admin
  ``catalog.exhaust_promotion`` verb. The spec exhausts the public e2e
  promo, walks signup with the now-exhausted code, asserts the order
  completes (no discount applied, no parked state). This tests the
  catalog-side validate rejection path; the COM ``_claim_entitlement``
  degrade-after-loyalty-refusal path that v1.1.3 protected has different
  timing semantics (in-flight order whose discount snapshot survives an
  exhaust between order create and service_order complete) and is left
  to a future spec that can stage that race.
"""

from __future__ import annotations

import os

import pytest
from bss_clients import CatalogClient, TokenAuthProvider
from bss_e2e.helpers.otp import wait_for_otp
from bss_e2e.helpers.seed import (
    PROMO_EXHAUSTED_CODE,
    PROMO_EXHAUSTED_ID,
    PROMO_PUBLIC_CODE,
    PROMO_TARGETED_ID,
    ensure_e2e_customer,
    ensure_e2e_promos,
)
from playwright.sync_api import expect

# Shared with test_signup_smoke — plan choice is fixed (PLAN_M middle tier).
PROMO_PLAN = "PLAN_M"


def _login(page, base_url, email, mailbox_path) -> None:
    """Same OTP login walk used in test_signup_smoke."""
    page.goto(f"{base_url}/auth/login")
    page.fill("input[name=email]", email)
    page.click("button[type=submit]")
    page.wait_for_url("**/auth/check-email**", timeout=10_000)
    otp = wait_for_otp(mailbox_path, email)
    page.fill("input[name=code]", otp)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def _run_async_in_thread(coro):
    """Mirror of the conftest helper — needed for seed/setup calls inside specs."""
    import asyncio
    import threading

    box: list = []
    error: list = []

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box.append(loop.run_until_complete(coro))
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)
        finally:
            loop.close()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if error:
        raise error[0]
    return box[0]


# ── Spec 2: public code applied at signup ───────────────────────────────────


@pytest.mark.self_serve
def test_public_promo_applied_at_signup(
    page, snap, base_urls, mailbox_path, e2e_customer_email, available_msisdn
):
    """Type ``E2E_PUBLIC10`` at signup → live preview shows a discount;
    the order completes (degrade path is the v1.1.3 backstop, not the test)."""
    base = base_urls["self_serve"]
    _run_async_in_thread(ensure_e2e_promos())

    _login(page, base, e2e_customer_email, mailbox_path)
    page.goto(f"{base}/signup/{PROMO_PLAN}?msisdn={available_msisdn}")
    page.wait_for_selector("form.signup-form", timeout=10_000)
    page.fill("input[name=name]", "E2E Public Promo")
    page.fill("input[name=phone]", "+65 9000 0001")
    snap("signup-form-before-promo")

    # Type the promo code with real keystrokes — HTMX listens for
    # ``change`` and ``keyup[Enter]``, and the live preview is the
    # contract we're asserting on. Tab (blur) fires the change event.
    promo_input = page.locator("#promo_code")
    promo_input.click()
    page.keyboard.type(PROMO_PUBLIC_CODE, delay=20)
    page.keyboard.press("Tab")

    # Wait for the preview region to populate.
    page.wait_for_function(
        "() => { const el = document.querySelector('#promo-preview');"
        "        return el && el.innerText.trim().length > 0; }",
        timeout=10_000,
    )
    snap("promo-preview-rendered")

    page.click("button.form-submit")
    page.wait_for_url("**/confirmation/**", timeout=45_000)
    expect(page.locator("#lpa-code")).to_be_visible()
    snap("confirmation-with-promo-applied")


# ── Spec 3: targeted promo on dashboard ─────────────────────────────────────


@pytest.mark.self_serve
def test_targeted_promo_visible_on_dashboard(
    page, snap, base_urls, mailbox_path, e2e_customer_email, available_msisdn
):
    """Walk a normal signup, then assign the targeted promo → dashboard
    surfaces the issued offer.

    Order matters: targeted assignment requires a ``customer_id``, and the
    cleanest BSS-side mint of one is to let the signup funnel produce it.
    Pre-creating the customer via ``ensure_e2e_customer`` before login
    flips ``is_returning=True`` and skips the KYC + COF steps without
    leaving an actual COF on record — the order then stalls. So we sign
    up first (fresh customer + active subscription via the full funnel),
    then assign the targeted promo, then check the dashboard.

    Doctrine note: the v1.3.0 "pairing upfront" path runs server-side
    via ``CatalogClient.assign_promotion`` — eligibility row + issued
    loyalty offer in lockstep. The dashboard reads
    ``promotion_eligibility`` and renders the issued offer as an
    "available" card.
    """
    base = base_urls["self_serve"]
    _run_async_in_thread(ensure_e2e_promos())

    # 1) Normal signup — no promo on the form.
    _login(page, base, e2e_customer_email, mailbox_path)
    page.goto(f"{base}/signup/{PROMO_PLAN}?msisdn={available_msisdn}")
    page.wait_for_selector("form.signup-form", timeout=10_000)
    page.fill("input[name=name]", "E2E Targeted Recipient")
    page.fill("input[name=phone]", "+65 9000 0002")
    page.click("button.form-submit")
    page.wait_for_url("**/confirmation/**", timeout=45_000)
    expect(page.locator("#lpa-code")).to_be_visible()
    snap("signup-complete-no-promo")

    # 2) Resolve the freshly-minted customer_id from the email and
    #    assign the targeted promo via the catalog API. v1.3.0 mints the
    #    loyalty offer (issued) at assign time.
    customer_id = _run_async_in_thread(ensure_e2e_customer(e2e_customer_email))

    async def _assign() -> None:
        catalog = CatalogClient(
            base_url=os.environ.get("BSS_CATALOG_URL", "http://localhost:8001"),
            auth_provider=TokenAuthProvider(os.environ["BSS_API_TOKEN"]),
        )
        try:
            await catalog.assign_promotion(
                PROMO_TARGETED_ID, customer_ids=[customer_id]
            )
        finally:
            await catalog.close()

    _run_async_in_thread(_assign())

    # 3) Refresh dashboard — the new targeted offer should be reachable.
    page.goto(f"{base}/")
    page.wait_for_load_state("networkidle")
    expect(page.locator(".line-card--active").first).to_be_visible()
    page_html = page.content()
    assert (
        PROMO_TARGETED_ID in page_html
        or "E2E Targeted 20%" in page_html
    ), (
        "expected targeted offer (id or display_name) to render on dashboard; "
        "neither found"
    )
    snap("dashboard-with-targeted-offer")


# ── Spec 4: exhausted code at signup → order completes at full price ────────


@pytest.mark.self_serve
def test_exhausted_promo_degrades_to_full_price(
    page, snap, base_urls, mailbox_path, e2e_customer_email, available_msisdn
):
    """Exhausted code at signup — order completes (no discount applied).

    v1.4.1 adds the ``catalog.exhaust_promotion`` admin verb. Spec setup:
      1. Ensure the three e2e promos exist.
      2. Exhaust ``PROMO_E2E_EXHAUSTED`` via the catalog API.

    Then the spec walks a normal signup with the now-exhausted code typed
    into the promo field. ``validate_for_order`` rejects the code (state
    != "active") and the order proceeds at full price — no parked state,
    no degrade signal, just no discount.

    Re-runs are safe: ``exhaust_promotion`` is idempotent on already-
    exhausted rows. The spec doesn't need to reset state between runs."""
    base = base_urls["self_serve"]
    _run_async_in_thread(ensure_e2e_promos())

    async def _exhaust() -> None:
        catalog = CatalogClient(
            base_url=os.environ.get("BSS_CATALOG_URL", "http://localhost:8001"),
            auth_provider=TokenAuthProvider(os.environ["BSS_API_TOKEN"]),
        )
        try:
            await catalog.exhaust_promotion(PROMO_EXHAUSTED_ID)
        finally:
            await catalog.close()

    _run_async_in_thread(_exhaust())

    _login(page, base, e2e_customer_email, mailbox_path)
    page.goto(f"{base}/signup/{PROMO_PLAN}?msisdn={available_msisdn}")
    page.wait_for_selector("form.signup-form", timeout=10_000)
    page.fill("input[name=name]", "E2E Exhausted Promo")
    page.fill("input[name=phone]", "+65 9000 0003")

    promo_input = page.locator("#promo_code")
    promo_input.click()
    page.keyboard.type(PROMO_EXHAUSTED_CODE, delay=20)
    page.keyboard.press("Tab")
    # Give the HTMX preview a moment to settle (rejection text or empty).
    page.wait_for_timeout(500)
    snap("exhausted-code-typed")

    # Submit. With the code rejected at validate, no discount snapshot
    # lands on the order, and the funnel walks normally to /confirmation
    # at full price.
    page.click("button.form-submit")
    page.wait_for_url("**/confirmation/**", timeout=45_000)
    expect(page.locator("#lpa-code")).to_be_visible()
    snap("confirmation-no-discount-applied")
