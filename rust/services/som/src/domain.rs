//! Pure SOM domain — the ServiceOrder + Service (CFS/RFS) state machines and the
//! decomposition task list. Port of `app.policies.service_order` and the
//! `_TASK_TYPES` constant. The transition checks are the guardrails every
//! state-change flows through.

use bss_db::PolicyViolation;
use serde_json::json;

/// The four provisioning tasks a decomposition fans out (order matters for the
/// deterministic `pendingTasks` map + publish order).
pub const TASK_TYPES: [&str; 4] = [
    "HLR_PROVISION",
    "PCRF_POLICY_PUSH",
    "OCS_BALANCE_INIT",
    "ESIM_PROFILE_PREPARE",
];

/// Legal ServiceOrder transitions.
fn so_allowed(current: &str) -> &'static [&'static str] {
    match current {
        "acknowledged" => &["in_progress"],
        "in_progress" => &["completed", "failed"],
        _ => &[],
    }
}

/// Legal Service (CFS/RFS) transitions.
fn svc_allowed(current: &str) -> &'static [&'static str] {
    match current {
        "designed" => &["failed", "reserved"],
        "reserved" => &["activated", "failed"],
        "activated" => &["terminated"],
        _ => &[],
    }
}

/// `service_order.transition.invalid` — reject an illegal ServiceOrder transition.
pub fn check_service_order_transition(current: &str, target: &str) -> Result<(), PolicyViolation> {
    let allowed = so_allowed(current);
    if !allowed.contains(&target) {
        return Err(transition_violation(
            "service_order.transition.invalid",
            "ServiceOrder",
            current,
            target,
            allowed,
        ));
    }
    Ok(())
}

/// `service.transition.invalid` — reject an illegal Service transition.
pub fn check_service_transition(current: &str, target: &str) -> Result<(), PolicyViolation> {
    let allowed = svc_allowed(current);
    if !allowed.contains(&target) {
        return Err(transition_violation(
            "service.transition.invalid",
            "Service",
            current,
            target,
            allowed,
        ));
    }
    Ok(())
}

fn transition_violation(
    rule: &str,
    entity: &str,
    current: &str,
    target: &str,
    allowed: &[&str],
) -> PolicyViolation {
    // Python renders `sorted(allowed)`; our tables are authored already sorted.
    let mut sorted: Vec<&str> = allowed.to_vec();
    sorted.sort_unstable();
    let allowed_display = if sorted.is_empty() {
        "none".to_string()
    } else {
        // Python str(list): ['a', 'b']
        format!(
            "[{}]",
            sorted
                .iter()
                .map(|s| format!("'{s}'"))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };
    PolicyViolation::with_context(
        rule,
        format!(
            "{entity} cannot transition from '{current}' to '{target}'. Allowed: {allowed_display}."
        ),
        json!({
            "current_state": current,
            "target_state": target,
            "allowed": sorted,
        }),
    )
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn so_legal_transitions_pass() {
        check_service_order_transition("acknowledged", "in_progress").unwrap();
        check_service_order_transition("in_progress", "completed").unwrap();
        check_service_order_transition("in_progress", "failed").unwrap();
    }

    #[test]
    fn so_illegal_transitions_rejected() {
        let err = check_service_order_transition("completed", "in_progress").unwrap_err();
        assert_eq!(err.rule, "service_order.transition.invalid");
        assert_eq!(err.context["current_state"], "completed");
        assert_eq!(err.context["allowed"], json!([]));

        let err = check_service_order_transition("acknowledged", "completed").unwrap_err();
        assert_eq!(err.context["allowed"], json!(["in_progress"]));
    }

    #[test]
    fn svc_legal_transitions_pass() {
        check_service_transition("designed", "reserved").unwrap();
        check_service_transition("reserved", "activated").unwrap();
        check_service_transition("reserved", "failed").unwrap();
        check_service_transition("activated", "terminated").unwrap();
    }

    #[test]
    fn svc_illegal_transitions_rejected() {
        let err = check_service_transition("designed", "activated").unwrap_err();
        assert_eq!(err.rule, "service.transition.invalid");
        assert_eq!(err.context["allowed"], json!(["failed", "reserved"]));
    }

    #[test]
    fn transition_message_lists_sorted_allowed() {
        let err = check_service_transition("reserved", "designed").unwrap_err();
        assert!(err.message.contains("Allowed: ['activated', 'failed']."));
    }

    #[test]
    fn transition_message_none_when_terminal() {
        let err = check_service_order_transition("completed", "failed").unwrap_err();
        assert!(err.message.contains("Allowed: none."));
    }

    #[test]
    fn task_types_are_the_four() {
        assert_eq!(TASK_TYPES.len(), 4);
        assert_eq!(TASK_TYPES[0], "HLR_PROVISION");
        assert_eq!(TASK_TYPES[3], "ESIM_PROFILE_PREPARE");
    }
}
