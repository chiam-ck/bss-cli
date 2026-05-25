"""Spec 1 — self-serve signup golden path.

Flow: magic-link login → KYC (prebaked) → payment-method (mock) → plan
select → MSISDN reserve → activation → dashboard shows active subscription
+ eSIM QR.

Currently a placeholder during v1.4.0 phase 1 (scaffolding). The first
real implementation lands as phase 2 once the package import + fixture
wiring is verified green via ``pytest --collect-only``.
"""

from __future__ import annotations

import pytest


@pytest.mark.self_serve
def test_signup_golden_path_smoke(page, base_urls, mailbox_path, e2e_customer_email):
    """Walk a fresh customer from /welcome to an active subscription."""
    pytest.skip("scaffold — implementation arrives in v1.4.0 phase 2")
