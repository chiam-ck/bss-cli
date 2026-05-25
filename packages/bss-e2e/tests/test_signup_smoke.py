"""Spec 1 — self-serve signup golden path.

Walks a fresh e2e customer through:

1. ``/auth/login`` — email entry, OTP from mailbox, verify.
2. ``/signup/PLAN_M?msisdn=...`` — gated by verified email; the signup form
   pre-fills the signed-in email.
3. Form submit (single click) — the server walks step 2 (KYC prebaked) →
   step 3 (mock COF) → step 4 (order) → step 5 (poll), with the rendered
   progress page auto-advancing via HTMX. Final response is an
   ``HX-Redirect`` to ``/confirmation/{subscription_id}?session=...``.
4. Confirmation page — eSIM QR (``<img>``) + LPA activation code
   (``id=lpa-code``) visible.
5. Dashboard ``/`` — ``.line-card--active`` for the new subscription.

If this spec is green, the same Playwright pipeline + auth/login helper
can carry the other four self-serve specs without further re-engineering.

The smoke-test customer email is unique-per-run (``e2e-<uuid>@bss-cli.local``)
so the spec is re-runnable without explicit teardown — ``make e2e`` does
the surgical ``reset_e2e_data()`` sweep at exit anyway.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from bss_e2e.helpers.otp import wait_for_otp

# Plan choice is fixed (middle tier — has all four allowance types so the
# dashboard rendering exercises the full line-card layout). MSISDN is
# dynamic via the ``available_msisdn`` fixture so re-runs don't collide
# with stale subscriptions from previous spec runs.
SMOKE_PLAN = "PLAN_M"


def _login(page: Page, base_url: str, email: str, mailbox_path) -> None:
    """Walk the magic-link OTP flow. Leaves the page signed in at ``/``."""
    page.goto(f"{base_url}/auth/login")
    page.fill("input[name=email]", email)
    page.click("button[type=submit]")
    page.wait_for_url("**/auth/check-email**", timeout=10_000)

    otp = wait_for_otp(mailbox_path, email)
    page.fill("input[name=code]", otp)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


@pytest.mark.self_serve
def test_signup_golden_path_smoke(
    page, base_urls, mailbox_path, e2e_customer_email, available_msisdn
):
    """Fresh customer signs up; confirmation + dashboard render the new line."""
    base = base_urls["self_serve"]

    # ── Step 1: auth ────────────────────────────────────────────────────
    _login(page, base, e2e_customer_email, mailbox_path)

    # ── Step 2: signup form ─────────────────────────────────────────────
    page.goto(f"{base}/signup/{SMOKE_PLAN}?msisdn={available_msisdn}")
    # Wait for the form (not just navigation) — signup is gated on
    # session verification and may redirect transparently if the cookie
    # rotated.
    page.wait_for_selector("form.signup-form", timeout=10_000)
    page.fill("input[name=name]", "E2E Smoke")
    page.fill("input[name=phone]", "+65 9000 0000")
    # ``card_pan`` defaults to "4242424242424242" under the mock-tokenizer
    # override; leave as-is.

    # ── Step 3: submit + wait for funnel auto-advance ───────────────────
    page.click("button.form-submit")
    # The progress page polls /signup/step/poll which emits HX-Redirect
    # to /confirmation/{sub_id}?session=... when COM finishes the order.
    # 45 s gives plenty of headroom for the mock chain.
    page.wait_for_url("**/confirmation/**", timeout=45_000)

    # ── Step 4: confirmation renders eSIM QR + LPA code ────────────────
    expect(page.locator("#lpa-code")).to_be_visible()
    expect(page.locator(".confirmation-qr img, img.confirmation-qr-image").first).to_be_visible()

    # ── Step 5: dashboard shows the active line ─────────────────────────
    page.goto(f"{base}/")
    page.wait_for_load_state("networkidle")
    # ``.line-card--active`` is the live class — any active subscription
    # matches, and there's exactly one for this brand-new customer.
    expect(page.locator(".line-card--active").first).to_be_visible()
    # MSISDN we requested should render somewhere on the card (last 4 digits
    # is enough; portal may format with spaces / dashes).
    expect(page.locator(".line-card-msisdn").first).to_contain_text(available_msisdn[-4:])
