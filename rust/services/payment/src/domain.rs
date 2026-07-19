//! Pure payment domain — port of the testable core of `app.domain` +
//! `app.services.payment_service`'s branching.
//!
//! - [`ChargeResult`] mirrors the Python dataclass (status/gateway_ref/reason/
//!   provider_call_id/decline_code).
//! - [`decide_mock_charge`] is the mock adapter's pure decision (FAIL/DECLINE in
//!   the token → declined), factored out so it's unit-testable without the uuid.
//! - [`event_type_for_status`] is the charge → event routing key map.
//!
//! `tokenize` (server-side PAN tokenization) is deliberately absent: no HTTP route
//! calls it — it's a mock-only dev affordance used by `bss payment add-card`
//! (CLI, Phase 7). The production Stripe path forbids it outright.

/// Result of a charge attempt — port of `app.domain.tokenizer.ChargeResult`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChargeResult {
    /// `"approved"` | `"declined"` | `"errored"`.
    pub status: String,
    pub gateway_ref: String,
    pub reason: Option<String>,
    pub provider_call_id: String,
    pub decline_code: Option<String>,
}

/// The mock charge decision — pure over the token string. Mirrors
/// `app.domain.mock_tokenizer.charge`: a token carrying `FAIL`/`DECLINE`
/// declines (`card_declined_by_issuer` / `card_declined`); everything else
/// approves. The `gateway_ref`/`provider_call_id` (the `mock_<uuid4>`) are added
/// by the async wrapper — kept out here so the decision is deterministic.
pub fn decide_mock_charge(token: &str) -> (String, Option<String>, Option<String>) {
    if token.contains("FAIL") || token.contains("DECLINE") {
        (
            "declined".to_string(),
            Some("card_declined_by_issuer".to_string()),
            Some("card_declined".to_string()),
        )
    } else {
        ("approved".to_string(), None, None)
    }
}

/// Charge status → domain-event routing key. Port of the `PaymentService.charge`
/// dict: `approved → payment.charged`, `declined → payment.declined`, anything
/// else (`errored`) → `payment.errored`.
pub fn event_type_for_status(status: &str) -> &'static str {
    match status {
        "approved" => "payment.charged",
        "declined" => "payment.declined",
        _ => "payment.errored",
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn mock_charge_approves_plain_token() {
        let (status, reason, code) = decide_mock_charge("tok_abc123");
        assert_eq!(status, "approved");
        assert!(reason.is_none());
        assert!(code.is_none());
    }

    #[test]
    fn mock_charge_declines_fail_token() {
        let (status, reason, code) = decide_mock_charge("tok_FAIL_xyz");
        assert_eq!(status, "declined");
        assert_eq!(reason.as_deref(), Some("card_declined_by_issuer"));
        assert_eq!(code.as_deref(), Some("card_declined"));
    }

    #[test]
    fn mock_charge_declines_decline_token() {
        let (status, _, code) = decide_mock_charge("tok_DECLINE_xyz");
        assert_eq!(status, "declined");
        assert_eq!(code.as_deref(), Some("card_declined"));
    }

    #[test]
    fn event_type_map_matches_oracle() {
        assert_eq!(event_type_for_status("approved"), "payment.charged");
        assert_eq!(event_type_for_status("declined"), "payment.declined");
        assert_eq!(event_type_for_status("errored"), "payment.errored");
        assert_eq!(event_type_for_status("weird"), "payment.errored");
    }
}
