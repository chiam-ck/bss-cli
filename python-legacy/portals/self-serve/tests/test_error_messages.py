"""v0.10 — error_messages render() pipeline.

Confirms:

* Known rules render to their registered customer-facing string.
* Unknown rules fall back to the generic apology, never leak the
  rule code to the customer.
* ``is_known`` distinguishes registered rules from fallthroughs so
  ops can pivot on "unknown rule rate" in the portal_action table.
"""

from __future__ import annotations

from bss_self_serve.error_messages import (
    GENERIC_FALLBACK,
    RULE_MESSAGES,
    is_known,
    render,
)


def test_known_rule_renders_registered_string():
    out = render("policy.payment.method.declined")
    assert out == RULE_MESSAGES["policy.payment.method.declined"]
    # Customer-facing copy never embeds the rule code itself.
    assert "policy." not in out


def test_unknown_rule_falls_back_to_generic():
    out = render("policy.gibberish.never_registered")
    assert out == GENERIC_FALLBACK


def test_unknown_rule_does_not_leak_internal_string():
    """The fallback must not interpolate the unknown rule into the response."""
    out = render("policy.internal.engineer_only_diagnostic")
    assert "internal" not in out
    assert "engineer" not in out


def test_is_known_for_registered_rule():
    assert is_known("policy.payment.method.declined") is True


def test_is_known_for_unregistered_rule():
    assert is_known("policy.gibberish.never_registered") is False


def test_no_rule_message_contains_engineer_template_braces():
    """Customer copy is static; interpolation would be a footgun."""
    for msg in RULE_MESSAGES.values():
        assert "{" not in msg
        assert "}" not in msg
