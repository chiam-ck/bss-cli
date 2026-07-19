//! bss-telemetry — log-field redaction + span attribute keys.
//!
//! Rust port of `packages/bss-telemetry`. Phase 0 lands the two pieces the rest
//! of the tree depends on and that are pure/testable: the [`redaction`] rules and
//! the [`semconv`] attribute keys. The tracing-subscriber JSON setup, the OTLP/
//! OTel exporter, and the redaction `Layer` that applies [`redaction::redact_event`]
//! to live events are added with the hello-world conformance service (where Jaeger
//! exists to validate them) — matching the "instrument at the chokepoint,
//! never-fail-startup" posture of the Python crate.
#![forbid(unsafe_code)]

pub mod bootstrap;
pub mod healthcheck;
pub mod propagation;
pub mod redaction;
pub mod semconv;

pub use bootstrap::{emit_probe_span, init_telemetry, TelemetryGuard};
pub use healthcheck::{healthcheck_requested, maybe_run_healthcheck};
pub use propagation::{
    continue_trace, current_trace_id, current_traceparent, traceparent_for_trace_id, TRACEPARENT,
};
pub use redaction::{redact_event, should_redact, REDACTED, REDACTED_KEYS, SAFE_SUFFIXES};

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::redaction::{redact_event, should_redact, REDACTED};
    use serde_json::{json, Map, Value};

    #[test]
    fn redacts_exact_sensitive_keys() {
        for key in [
            "document_number",
            "cvv",
            "card_number",
            "ki",
            "pan",
            "password",
            "token",
        ] {
            assert!(should_redact(key), "{key} should redact");
        }
    }

    #[test]
    fn keeps_safe_and_unrelated_keys() {
        // Reference/id variants are safe (never in REDACTED_KEYS anyway).
        for key in [
            "ki_ref",
            "token_id",
            "card_number_ref",
            "customer_id",
            "msisdn",
            "status",
        ] {
            assert!(!should_redact(key), "{key} should NOT redact");
        }
    }

    #[test]
    fn redact_event_replaces_only_sensitive_top_level_values() {
        let mut event: Map<String, Value> = json!({
            "event": "payment.charged",
            "card_number": "4242424242424242",
            "ki_ref": "KI-REF-01",
            "amount": 1000,
            "token": "secret-token",
        })
        .as_object()
        .unwrap()
        .clone();

        redact_event(&mut event);

        assert_eq!(event["card_number"], REDACTED);
        assert_eq!(event["token"], REDACTED);
        // Safe / unrelated keys untouched.
        assert_eq!(event["ki_ref"], "KI-REF-01");
        assert_eq!(event["amount"], 1000);
        assert_eq!(event["event"], "payment.charged");
    }

    #[test]
    fn redact_event_does_not_recurse_into_nested_objects() {
        // Matches the Python processor: only top-level event_dict keys are checked.
        let mut event: Map<String, Value> = json!({
            "outer": {"card_number": "4242424242424242"},
        })
        .as_object()
        .unwrap()
        .clone();
        redact_event(&mut event);
        assert_eq!(event["outer"]["card_number"], "4242424242424242");
    }
}
