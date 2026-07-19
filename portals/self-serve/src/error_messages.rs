//! Customer-facing strings for `PolicyViolation.rule` codes. Port of
//! `bss_self_serve.error_messages`.
//!
//! Known rule → the mapped string verbatim. Unknown rule → [`GENERIC_FALLBACK`]
//! (the portal also writes a `portal_action` audit row with `error_rule=<rule>`
//! so ops can register copy later). The catalogue is intentionally shallow —
//! mappings are added by humans, not auto-derived from rule codes.

/// Generic fallback when no specific copy is registered for a rule.
pub const GENERIC_FALLBACK: &str = "Sorry, something went wrong — please try again. \
     If the problem persists, contact support.";

/// Static rule → customer copy. Mirrors `RULE_MESSAGES` verbatim.
const RULE_MESSAGES: &[(&str, &str)] = &[
    // Subscription
    (
        "policy.subscription.terminate.subscription_already_terminated",
        "This line is already cancelled.",
    ),
    (
        "policy.subscription.purchase_vas.subscription_not_active",
        "Your line isn't active right now. Top-ups are only available \
         while a line is in active or blocked state.",
    ),
    (
        "policy.subscription.purchase_vas.vas_offering_unknown",
        "That add-on is no longer available. Please refresh the page.",
    ),
    (
        "policy.subscription.plan_change.target_not_sellable_now",
        "That plan isn't available right now. Please pick another.",
    ),
    (
        "policy.subscription.plan_change.same_offering",
        "That's already your current plan.",
    ),
    (
        "policy.subscription.plan_change.no_pending_change",
        "No pending plan change to cancel.",
    ),
    // Order
    (
        "order.create.no_payment_method",
        "There's no payment card on your account yet. Add a card \
         first, then place your order again.",
    ),
    // KYC — signup step errors set portal-side from the Didit decision
    (
        "kyc.declined",
        "We couldn't verify your identity, so signup can't continue. \
         Please check your document details and try again, or contact \
         support.",
    ),
    (
        "kyc.expired",
        "Your identity verification session expired before it was \
         completed. Please start the signup again.",
    ),
    // Payment
    (
        "policy.payment.method.invalid_card",
        "That card number doesn't look right. Please check it and \
         try again.",
    ),
    (
        "policy.payment.method.declined",
        "Your card was declined. Please check the details or use a different card.",
    ),
    (
        "policy.payment.method.duplicate",
        "That card is already on file.",
    ),
    (
        "policy.payment.method.cannot_remove_last_with_active_lines",
        "You can't remove your only payment method while you have an \
         active line. Add another card first, or cancel your line.",
    ),
    (
        "policy.payment.method.unknown",
        "That payment method isn't on file. Please refresh the page.",
    ),
    // CRM / customer / contact
    (
        "policy.customer.contact_medium.email_in_use",
        "That email is already in use by another account.",
    ),
    (
        "policy.customer.contact_medium.unknown",
        "We couldn't find that contact entry. Please refresh the page.",
    ),
    (
        "policy.customer.contact_medium.email_must_use_change_flow",
        "Email changes need a verified new address. Use the email-change \
         flow on the contact-details page.",
    ),
    (
        "policy.customer.contact_medium.no_active_pending",
        "There's no email-change pending right now. Start a new one from \
         the contact-details page.",
    ),
    (
        "policy.customer.contact_medium.wrong_code",
        "That code doesn't match. Try again, or restart the change.",
    ),
    (
        "policy.customer.contact_medium.expired",
        "Your verification code has expired. Restart the email change \
         from the contact-details page.",
    ),
    // Cross-resource ownership (server-side checks)
    (
        "policy.ownership.subscription_not_owned",
        "That line doesn't belong to your account.",
    ),
    (
        "policy.ownership.service_not_owned",
        "That service doesn't belong to your account.",
    ),
    (
        "policy.ownership.payment_method_not_owned",
        "That payment method doesn't belong to your account.",
    ),
];

/// Return a customer-facing string for `rule`. Unknown rules → [`GENERIC_FALLBACK`].
pub fn render(rule: &str) -> &'static str {
    RULE_MESSAGES
        .iter()
        .find(|(k, _)| *k == rule)
        .map(|(_, v)| *v)
        .unwrap_or(GENERIC_FALLBACK)
}

/// True iff `rule` has a registered customer-facing message.
pub fn is_known(rule: &str) -> bool {
    RULE_MESSAGES.iter().any(|(k, _)| *k == rule)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_and_unknown() {
        assert!(is_known("policy.payment.method.invalid_card"));
        assert_eq!(
            render("policy.payment.method.duplicate"),
            "That card is already on file."
        );
        assert!(!is_known("some.unregistered.rule"));
        assert_eq!(render("some.unregistered.rule"), GENERIC_FALLBACK);
    }
}
