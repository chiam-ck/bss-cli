"""Customer-facing strings for ``PolicyViolation.rule`` codes (v0.10).

The BSS services raise ``PolicyViolationFromServer(rule=..., message=...,
context=...)`` — the ``message`` is engineer-shaped (operator-readable
diagnostic), the ``rule`` is a stable code suitable for i18n. This
module is the i18n stub: it maps ``rule`` to a customer-facing string.

Doctrine (V0_10_0.md Track 10.4):

* Known rule → render the mapped string verbatim. Context values are
  not interpolated by default; if a rule needs context, add a callable
  to ``RULE_FORMATTERS`` so the formatter is a code review point.
* Unknown rule → fall back to a generic apology. The portal also
  writes a ``portal_action`` audit row with ``error_rule=<rule>`` so
  ops can review and add a customer-facing string later.

The catalogue is intentionally shallow. New rules show up in the
audit table first; mappings are added by humans, not generated. This
prevents the "every internal error string leaks to customers"
anti-pattern that comes with auto-deriving customer copy from rule
codes.
"""

from __future__ import annotations

from typing import Callable, Final, Mapping

# Generic fallback when no specific copy is registered for a rule.
GENERIC_FALLBACK: Final[str] = (
    "Sorry, something went wrong — please try again. "
    "If the problem persists, contact support."
)


# Static rule → customer copy. Keys are stable BSS policy rule codes.
RULE_MESSAGES: Final[Mapping[str, str]] = {
    # Subscription
    "policy.subscription.terminate.subscription_already_terminated": (
        "This line is already cancelled."
    ),
    "policy.subscription.purchase_vas.subscription_not_active": (
        "Your line isn't active right now. Top-ups are only available "
        "while a line is in active or blocked state."
    ),
    "policy.subscription.purchase_vas.vas_offering_unknown": (
        "That add-on is no longer available. Please refresh the page."
    ),
    "policy.subscription.plan_change.target_not_sellable_now": (
        "That plan isn't available right now. Please pick another."
    ),
    "policy.subscription.plan_change.same_offering": (
        "That's already your current plan."
    ),
    "policy.subscription.plan_change.no_pending_change": (
        "No pending plan change to cancel."
    ),
    # Payment
    "policy.payment.method.declined": (
        "Your card was declined. Please check the details or use a different card."
    ),
    "policy.payment.method.duplicate": (
        "That card is already on file."
    ),
    "policy.payment.method.cannot_remove_last_with_active_lines": (
        "You can't remove your only payment method while you have an "
        "active line. Add another card first, or cancel your line."
    ),
    "policy.payment.method.unknown": (
        "That payment method isn't on file. Please refresh the page."
    ),
    # CRM / customer / contact
    "policy.customer.contact_medium.email_in_use": (
        "That email is already in use by another account."
    ),
    "policy.customer.contact_medium.unknown": (
        "We couldn't find that contact entry. Please refresh the page."
    ),
    "policy.customer.contact_medium.email_must_use_change_flow": (
        "Email changes need a verified new address. Use the email-change "
        "flow on the contact-details page."
    ),
    "policy.customer.contact_medium.no_active_pending": (
        "There's no email-change pending right now. Start a new one from "
        "the contact-details page."
    ),
    "policy.customer.contact_medium.wrong_code": (
        "That code doesn't match. Try again, or restart the change."
    ),
    "policy.customer.contact_medium.expired": (
        "Your verification code has expired. Restart the email change "
        "from the contact-details page."
    ),
    # Cross-resource ownership (server-side checks)
    "policy.ownership.subscription_not_owned": (
        "That line doesn't belong to your account."
    ),
    "policy.ownership.service_not_owned": (
        "That service doesn't belong to your account."
    ),
    "policy.ownership.payment_method_not_owned": (
        "That payment method doesn't belong to your account."
    ),
}


# Rules that need context interpolation get an explicit formatter so
# the customer-facing string stays under code review. No automatic
# {field} substitution — every interpolated value is a deliberate
# choice, not a leaked engineer-shaped diagnostic.
RULE_FORMATTERS: Final[Mapping[str, Callable[[Mapping[str, object]], str]]] = {}


def render(rule: str, context: Mapping[str, object] | None = None) -> str:
    """Return a customer-facing string for ``rule``.

    Unknown rules return ``GENERIC_FALLBACK``. Callers should also
    record the unknown rule in the ``portal_action`` audit row so ops
    can register copy for it.
    """
    formatter = RULE_FORMATTERS.get(rule)
    if formatter is not None:
        return formatter(context or {})
    return RULE_MESSAGES.get(rule, GENERIC_FALLBACK)


def is_known(rule: str) -> bool:
    """True iff ``rule`` has a registered customer-facing message."""
    return rule in RULE_MESSAGES or rule in RULE_FORMATTERS
