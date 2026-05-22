#!/usr/bin/env python3
"""Capture v1.1 promo-code self-serve screenshots via Playwright headless.

REQUIRES the stack temporarily in mock/dev provider mode (logging email +
prebaked KYC + mock tokenizer) so the signup funnel auto-advances without
Resend/Didit/Stripe. See docker-compose.screenshots.yml. Restore with
`make up` afterwards.

Captures two surfaces, written next to this file:
  * portal_self_serve_signup_promo_v1_1.png — signup form, promo field +
    live discounted-price preview (WELCOME10 → 10% off).
  * portal_self_serve_dashboard_promo_v1_1.png — dashboard after the order,
    line card showing the active promo discount.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

OUT = Path(__file__).resolve().parent
ROOT = OUT.parent.parent
MAILBOX = ROOT / ".dev-mailbox" / "portal-mailbox.log"
BASE = "http://localhost:9001"
EMAIL = f"portal-demo-promo-{int(time.time())}@bss-cli.local"
MSISDN = os.environ.get("PROMO_MSISDN", "81000100")
PROMO_CODE = os.environ.get("PROMO_CODE", "WELCOME10")
PLAN = "PLAN_M"
VIEWPORT = {"width": 1280, "height": 1040}


def _resolve_chromium() -> str | None:
    explicit = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if explicit and Path(explicit).is_file():
        return explicit
    cache = Path.home() / ".cache" / "ms-playwright"
    if not cache.is_dir():
        return None
    for c in sorted(cache.glob("chromium-*/chrome-linux64/chrome"), reverse=True):
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _optimize(path: Path) -> None:
    if shutil.which("oxipng"):
        subprocess.run(["oxipng", "-o", "4", "--quiet", str(path)], check=False)
        print(f"  optimized: {path.name}")


def _latest_otp(email: str) -> str | None:
    txt = MAILBOX.read_text(encoding="utf-8")
    otp = None
    for block in txt.split("=== "):
        if f"To: {email}" in block:
            m = re.search(r"OTP:\s*(\d{6})", block)
            if m:
                otp = m.group(1)
    return otp


def _login(page: Page) -> None:
    page.goto(f"{BASE}/auth/login")
    page.fill("input[name=email]", EMAIL)
    page.click("button[type=submit]")
    page.wait_for_url("**/check-email**", timeout=10_000)
    # OTP was just written to the dev mailbox by the logging adapter.
    otp = None
    for _ in range(10):
        otp = _latest_otp(EMAIL)
        if otp:
            break
        time.sleep(0.3)
    if not otp:
        raise RuntimeError(f"no OTP in mailbox for {EMAIL}")
    page.fill("input[name=code]", otp)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    print(f"  logged in as {EMAIL}")


def _signup_promo(page: Page) -> None:
    page.goto(f"{BASE}/signup/{PLAN}?msisdn={MSISDN}")
    page.wait_for_selector("#promo_code", timeout=10_000)
    page.fill("input[name=name]", "Ada Promo")
    page.fill("input[name=phone]", "+65 9123 4567")
    # Promo field — type with real keystrokes so HTMX's
    # ``keyup[key=='Enter']`` / ``change`` triggers actually fire, and
    # wait on the live-preview network response itself.
    promo = page.locator("#promo_code")
    promo.click()
    page.keyboard.type(PROMO_CODE, delay=40)
    # Tab (not Enter) — blur fires HTMX's ``change`` trigger for the live
    # preview WITHOUT submitting the surrounding form (Enter would submit).
    page.keyboard.press("Tab")
    page.wait_for_function(
        "() => document.querySelector('#promo-preview') && "
        "document.querySelector('#promo-preview').innerText.trim().length > 0",
        timeout=10_000,
    )
    page.wait_for_timeout(500)
    # Scroll the discount section into view so the shot centres on it.
    page.locator(".discount-section").scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    out = OUT / "portal_self_serve_signup_promo_v1_1.png"
    page.screenshot(path=str(out), full_page=False)
    print(f"captured: {out.name}")
    _optimize(out)


def _complete_and_dashboard(page: Page) -> None:
    # Submit the signup form; in mock mode the progress page auto-advances
    # KYC → COF → order → activation and HX-Redirects to /confirmation.
    page.locator(".signup-form").scroll_into_view_if_needed()
    page.click("button.form-submit")
    try:
        page.wait_for_url("**/confirmation/**", timeout=45_000)
        print("  order completed → confirmation")
    except Exception:
        print(f"  WARN: no confirmation redirect (url={page.url}); continuing")
    page.wait_for_timeout(800)
    # Dashboard.
    page.goto(f"{BASE}/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(800)
    out = OUT / "portal_self_serve_dashboard_promo_v1_1.png"
    page.screenshot(path=str(out), full_page=False)
    print(f"captured: {out.name}")
    _optimize(out)


def main() -> int:
    with sync_playwright() as p:
        kwargs: dict = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ],
        }
        chrome = _resolve_chromium()
        if chrome:
            kwargs["executable_path"] = chrome
            print(f"using chromium: {chrome}")
        browser = p.chromium.launch(**kwargs)
        ctx = browser.new_context(viewport=VIEWPORT, color_scheme="dark")
        page = ctx.new_page()
        _login(page)
        _signup_promo(page)
        _complete_and_dashboard(page)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
