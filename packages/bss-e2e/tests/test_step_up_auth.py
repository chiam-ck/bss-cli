"""Spec 5 — step-up auth gate.

Sensitive actions in ``SENSITIVE_ACTION_LABELS`` require a fresh OTP
challenge before the underlying mutation runs. Of the 11 labels,
``name_update`` is the cheapest to drive: no payment side effects, no
subscription state machine touched, just a CRM individual rename.

End-to-end shape:

1. Fresh customer signs up (full funnel — gives us a linked identity +
   active subscription).
2. ``POST /profile/contact/name/update`` with new given/family names.
3. Server gates via ``requires_step_up("name_update")`` → 303 to
   ``/auth/step-up?action=name_update&next=/profile/contact``.
4. ``POST /auth/step-up/start`` → step-up OTP emailed (different subject
   from the login OTP so the mailbox helper can filter).
5. ``POST /auth/step-up`` with the OTP → step-up cookie set; the server
   stashes the pending form payload and renders
   ``auth_step_up_replay.html`` which auto-submits the original
   ``/profile/contact/name/update`` POST. The replay request carries
   the freshly-set step-up cookie, the gate passes, the rename runs.
6. Browser ends up at ``/profile/contact?flash=name_update``.

Note the auto-replay is the production contract — manually re-submitting
the form would trigger the gate a second time (cookie is one-shot per
the v0.10 design).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from bss_e2e.helpers.otp import wait_for_otp

PROMO_PLAN = "PLAN_M"
NEW_GIVEN_NAME = "E2e"
NEW_FAMILY_NAME = "Renamed"


def _login(page: Page, base_url: str, email: str, mailbox_path) -> None:
    page.goto(f"{base_url}/auth/login")
    page.fill("input[name=email]", email)
    page.click("button[type=submit]")
    page.wait_for_url("**/auth/check-email**", timeout=10_000)
    otp = wait_for_otp(
        mailbox_path, email, subject_contains="portal login code"
    )
    page.fill("input[name=code]", otp)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def _walk_signup(
    page: Page, base_url: str, plan_id: str, msisdn: str, name: str
) -> None:
    page.goto(f"{base_url}/signup/{plan_id}?msisdn={msisdn}")
    page.wait_for_selector("form.signup-form", timeout=10_000)
    page.fill("input[name=name]", name)
    page.fill("input[name=phone]", "+65 9000 0003")
    page.click("button.form-submit")
    page.wait_for_url("**/confirmation/**", timeout=45_000)


@pytest.mark.self_serve
def test_step_up_required_for_sensitive_action(
    page, base_urls, mailbox_path, e2e_customer_email, available_msisdn
):
    """name_update gates on step-up OTP; updates succeed once cleared."""
    base = base_urls["self_serve"]

    # ── Setup: linked-identity customer with one active subscription ───
    _login(page, base, e2e_customer_email, mailbox_path)
    _walk_signup(
        page, base, PROMO_PLAN, available_msisdn, name="E2E Original"
    )

    # ── Step-up gate fires on the rename submit ────────────────────────
    page.goto(f"{base}/profile/contact")
    page.wait_for_selector("form[action='/profile/contact/name/update']", timeout=10_000)
    page.fill(
        "form[action='/profile/contact/name/update'] input[name=given_name]",
        NEW_GIVEN_NAME,
    )
    page.fill(
        "form[action='/profile/contact/name/update'] input[name=family_name]",
        NEW_FAMILY_NAME,
    )
    page.locator(
        "form[action='/profile/contact/name/update'] button[type=submit]"
    ).click()
    # The server gates via `requires_step_up("name_update")` — 303 →
    # /auth/step-up. Playwright follows the redirect automatically.
    page.wait_for_url("**/auth/step-up*", timeout=10_000)
    # The action label leaks into the page so the user sees what they're
    # confirming; it's a stable assertion target.
    expect(page.locator(".auth-action")).to_contain_text("name_update")

    # ── Trigger step-up OTP issuance ───────────────────────────────────
    page.click("button.auth-cta")  # "Email me a code"
    page.wait_for_selector("input[name=code]", timeout=10_000)

    # ── Read the step-up OTP from the mailbox (different subject) ──────
    step_up_otp = wait_for_otp(
        mailbox_path,
        e2e_customer_email,
        subject_contains="Confirm action: name_update",
        timeout_seconds=5.0,
    )
    page.fill("input[name=code]", step_up_otp)
    page.click("button.auth-cta")  # "Confirm"

    # ── Auto-replay: the server-rendered replay page auto-submits the
    #     original rename POST. The fresh step-up cookie carries the
    #     grant; the gate passes; we land on /profile/contact with the
    #     success flash. The replay is the production contract — manual
    #     re-submit would burn the gate a second time.
    page.wait_for_url("**/profile/contact*flash=name_update**", timeout=15_000)
    expect(page.locator(".form-flash")).to_contain_text("Name updated")
