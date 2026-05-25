"""Spec 5 — step-up auth gate.

A sensitive action (payment-method change is the simplest of
``SENSITIVE_ACTION_LABELS``) requires re-OTP. The portal challenges,
the customer enters a freshly-mailed OTP, the action goes through.

Placeholder during v1.4.0 phase 1.
"""

from __future__ import annotations

import pytest


@pytest.mark.self_serve
def test_step_up_required_for_sensitive_action(page, base_urls, mailbox_path, e2e_customer_email):
    """Sensitive action challenges with step-up, succeeds after re-OTP."""
    pytest.skip("scaffold — implementation arrives in v1.4.0 phase 2")
