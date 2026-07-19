"""Redaction tests — every provider's leak surface is covered."""

from __future__ import annotations

from bss_webhooks.redaction import redact_provider_payload

# ── resend ──────────────────────────────────────────────────────────


def test_resend_masks_to_field() -> None:
    body = {"to": ["a@example.com"], "subject": "test", "id": "msg_1"}
    out = redact_provider_payload(provider="resend", body=body)
    assert out["to"] == "[redacted]"
    assert out["subject"] == "test"
    assert out["id"] == "msg_1"


def test_resend_masks_from_and_cc_recursively() -> None:
    body = {
        "data": {
            "from": "noreply@example.com",
            "cc": ["b@example.com"],
            "reply_to": "support@example.com",
            "type": "email.delivered",
        }
    }
    out = redact_provider_payload(provider="resend", body=body)
    assert out["data"]["from"] == "[redacted]"
    assert out["data"]["cc"] == "[redacted]"
    assert out["data"]["reply_to"] == "[redacted]"
    assert out["data"]["type"] == "email.delivered"


def test_resend_does_not_mutate_input() -> None:
    body = {"to": ["a@example.com"], "subject": "test"}
    redact_provider_payload(provider="resend", body=body)
    assert body == {"to": ["a@example.com"], "subject": "test"}


# ── stripe ──────────────────────────────────────────────────────────


def test_stripe_masks_email_and_billing_details() -> None:
    body = {
        "id": "ch_1",
        "amount": 1000,
        "billing_details": {"email": "x@y", "name": "John"},
        "outcome": {"network_status": "approved"},
    }
    out = redact_provider_payload(provider="stripe", body=body)
    assert out["billing_details"] == "[redacted]"
    assert out["id"] == "ch_1"
    assert out["amount"] == 1000
    assert out["outcome"] == {"network_status": "approved"}


def test_stripe_keeps_last4_and_decline_code() -> None:
    """Ops needs last4 + decline_code visible for triage."""
    body = {
        "card": {"last4": "4242", "brand": "visa"},
        "decline_code": "insufficient_funds",
    }
    out = redact_provider_payload(provider="stripe", body=body)
    assert out["card"]["last4"] == "4242"
    assert out["card"]["brand"] == "visa"
    assert out["decline_code"] == "insufficient_funds"


def test_stripe_masks_full_pan_if_leaked() -> None:
    """Defense in depth — Stripe shouldn't return raw PAN, but if it does, mask it."""
    body = {"number": "4242424242424242", "cvc": "123"}
    out = redact_provider_payload(provider="stripe", body=body)
    assert out["number"] == "[redacted]"
    assert out["cvc"] == "[redacted]"


# ── didit ──────────────────────────────────────────────────────────


def test_didit_hashes_document_number() -> None:
    body = {"document_number": "S1234567A", "country": "SG"}
    out = redact_provider_payload(provider="didit", body=body)
    assert out["document_number"].startswith("sha256:")
    assert out["document_number"] != "S1234567A"
    assert out["country"] == "SG"


def test_didit_hashes_dob() -> None:
    body = {"date_of_birth": "1990-01-15", "verification_status": "approved"}
    out = redact_provider_payload(provider="didit", body=body)
    assert out["date_of_birth"].startswith("sha256:")
    assert out["verification_status"] == "approved"


def test_didit_masks_names() -> None:
    body = {"first_name": "Jane", "last_name": "Doe", "session_id": "ses_1"}
    out = redact_provider_payload(provider="didit", body=body)
    assert out["first_name"] == "[redacted]"
    assert out["last_name"] == "[redacted]"
    assert out["session_id"] == "ses_1"


def test_didit_recursive_hashing() -> None:
    body = {"data": {"document": {"document_number": "ABC123"}}}
    out = redact_provider_payload(provider="didit", body=body)
    assert out["data"]["document"]["document_number"].startswith("sha256:")


def test_didit_hash_is_stable() -> None:
    """Same input → same hash, so forensic queries can group by it."""
    a = redact_provider_payload(provider="didit", body={"document_number": "X"})
    b = redact_provider_payload(provider="didit", body={"document_number": "X"})
    assert a == b


# ── unknown provider ──────────────────────────────────────────────


def test_unknown_provider_passes_through() -> None:
    """Unknown providers fall through to identity (deep copy). Doctrine
    is to add an explicit redactor before shipping a new integration."""
    body = {"foo": {"bar": "baz"}}
    out = redact_provider_payload(provider="unknown_provider_v9", body=body)
    assert out == body
    # Verify deep copy — mutating the output doesn't affect the input.
    out["foo"]["bar"] = "mutated"
    assert body["foo"]["bar"] == "baz"
