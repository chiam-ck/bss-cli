//! Order domain — the pure ProductOrder FSM. Port of `app.policies.order`'s
//! `check_order_transition` (the async client-driven policies live in `policies`).

use bss_db::PolicyViolation;
use serde_json::json;

/// Legal transitions, mirroring `_ORDER_TRANSITIONS`. Returned `allowed` sets are
/// sorted for the policy context (matching Python's `sorted(allowed)`).
fn allowed_targets(current: &str) -> Vec<&'static str> {
    match current {
        "acknowledged" => vec!["cancelled", "in_progress"],
        "in_progress" => vec!["cancelled", "completed", "failed"],
        _ => vec![],
    }
}

/// Only legal state transitions are allowed. Port of `check_order_transition`.
pub fn check_order_transition(
    current_state: &str,
    target_state: &str,
) -> Result<(), PolicyViolation> {
    let allowed = allowed_targets(current_state);
    if !allowed.contains(&target_state) {
        return Err(PolicyViolation::with_context(
            "order.transition.invalid",
            format!("Order cannot transition from '{current_state}' to '{target_state}'"),
            json!({
                "current_state": current_state,
                "target_state": target_state,
                "allowed": allowed,
            }),
        ));
    }
    Ok(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn legal_transitions_pass() {
        assert!(check_order_transition("acknowledged", "in_progress").is_ok());
        assert!(check_order_transition("acknowledged", "cancelled").is_ok());
        assert!(check_order_transition("in_progress", "completed").is_ok());
        assert!(check_order_transition("in_progress", "failed").is_ok());
        assert!(check_order_transition("in_progress", "cancelled").is_ok());
    }

    #[test]
    fn illegal_transitions_are_rejected_with_sorted_allowed() {
        let e = check_order_transition("acknowledged", "completed").unwrap_err();
        assert_eq!(e.rule, "order.transition.invalid");
        assert_eq!(e.context["allowed"], json!(["cancelled", "in_progress"]));
        assert_eq!(e.context["current_state"], "acknowledged");
        assert_eq!(e.context["target_state"], "completed");
    }

    #[test]
    fn terminal_states_allow_nothing() {
        let e = check_order_transition("completed", "in_progress").unwrap_err();
        assert_eq!(e.context["allowed"], json!([]));
        assert!(check_order_transition("cancelled", "in_progress").is_err());
        assert!(check_order_transition("failed", "in_progress").is_err());
    }
}
