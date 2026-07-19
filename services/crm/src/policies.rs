//! Pure policy validators — port of the value-only checks in `app.policies.*`.
//!
//! DB-touching checks (email_unique, customer_active, agent_active,
//! all_tickets_resolved, document_hash_unique, no_active_subscriptions, KYC
//! corroboration) are done inline in the service layer where the connection is in
//! hand. Rule namespaces match the oracle byte-for-byte.

use bss_db::PolicyViolation;
use serde_json::json;

use crate::error::ApiError;
use crate::repo::PortRequestRow;

fn pv(rule: &str, msg: impl Into<String>, ctx: serde_json::Value) -> ApiError {
    PolicyViolation::with_context(rule, msg.into(), ctx).into()
}

// ── customer ────────────────────────────────────────────────────────────────

pub fn check_requires_contact_medium(n: usize) -> Result<(), ApiError> {
    if n == 0 {
        return Err(pv(
            "customer.create.requires_contact_medium",
            "At least one contact medium (email or phone) is required",
            json!({}),
        ));
    }
    Ok(())
}

// ── inventory ───────────────────────────────────────────────────────────────

pub fn check_msisdn_releasable(status: &str, msisdn: &str) -> Result<(), ApiError> {
    if status == "ported_out" {
        return Err(pv(
            "msisdn.release.terminal_status",
            format!("MSISDN {msisdn} is in terminal status '{status}' and cannot be released back to the pool"),
            json!({ "msisdn": msisdn, "status": status }),
        ));
    }
    if status != "reserved" && status != "assigned" {
        return Err(pv(
            "msisdn.release.only_if_reserved_or_assigned",
            format!("MSISDN {msisdn} cannot be released (status={status})"),
            json!({ "msisdn": msisdn, "status": status }),
        ));
    }
    Ok(())
}

pub fn check_msisdn_reserved_for_assign(status: &str, msisdn: &str) -> Result<(), ApiError> {
    if status != "reserved" && status != "assigned" {
        return Err(pv(
            "esim.assign_msisdn.msisdn_must_be_reserved",
            format!("MSISDN {msisdn} must be reserved or assigned before binding to eSIM"),
            json!({ "msisdn": msisdn, "status": status }),
        ));
    }
    Ok(())
}

pub fn check_sane_prefix(prefix: &str, count: i64) -> Result<(), ApiError> {
    let rule = "msisdn.add_range.sane_prefix";
    if !prefix.chars().all(|c| c.is_ascii_digit()) || prefix.is_empty() {
        return Err(pv(
            rule,
            format!("Prefix '{prefix}' must be all digits"),
            json!({ "prefix": prefix }),
        ));
    }
    if !(4..=7).contains(&prefix.len()) {
        return Err(pv(
            rule,
            format!("Prefix '{prefix}' must be 4–7 digits long"),
            json!({ "prefix": prefix, "length": prefix.len() }),
        ));
    }
    if !(1..=10000).contains(&count) {
        return Err(pv(
            rule,
            format!("Count {count} must be in [1, 10000]"),
            json!({ "count": count }),
        ));
    }
    Ok(())
}

// ── case ────────────────────────────────────────────────────────────────────

const VALID_PRIORITIES: &[&str] = &["low", "normal", "medium", "high", "critical"];

pub fn check_case_transition(state: &str, trigger: &str) -> Result<(), ApiError> {
    if !crate::domain::case::is_valid_transition(state, trigger) {
        return Err(pv(
            "case.transition.valid_from_state",
            format!("Cannot '{trigger}' case from state '{state}'"),
            json!({ "current_state": state, "trigger": trigger }),
        ));
    }
    Ok(())
}

pub fn check_resolution_code(code: Option<&str>) -> Result<(), ApiError> {
    if code.map(|c| c.is_empty()).unwrap_or(true) {
        return Err(pv(
            "case.close.requires_resolution_code",
            "Resolution code is required to close a case",
            json!({}),
        ));
    }
    Ok(())
}

pub fn check_case_not_closed(case_id: &str, state: &str) -> Result<(), ApiError> {
    if state == "closed" {
        return Err(pv(
            "case.update.case_is_closed",
            format!("Case {case_id} is closed; reopen is not supported"),
            json!({ "case_id": case_id, "state": state }),
        ));
    }
    Ok(())
}

pub fn check_priority_valid(priority: &str) -> Result<(), ApiError> {
    if !VALID_PRIORITIES.contains(&priority) {
        return Err(pv(
            "case.update.invalid_priority",
            format!("Priority '{priority}' is not valid; expected one of [\"critical\", \"high\", \"low\", \"medium\", \"normal\"]"),
            json!({ "priority": priority }),
        ));
    }
    Ok(())
}

// ── ticket ──────────────────────────────────────────────────────────────────

pub fn check_ticket_transition(state: &str, trigger: &str) -> Result<(), ApiError> {
    if !crate::domain::ticket::is_valid_transition(state, trigger) {
        return Err(pv(
            "ticket.transition.valid_from_state",
            format!("Cannot '{trigger}' ticket from state '{state}'"),
            json!({ "current_state": state, "trigger": trigger }),
        ));
    }
    Ok(())
}

pub fn check_resolution_notes(notes: Option<&str>) -> Result<(), ApiError> {
    if notes.map(|n| n.is_empty()).unwrap_or(true) {
        return Err(pv(
            "ticket.resolve.requires_resolution_notes",
            "Resolution notes are required to resolve a ticket",
            json!({}),
        ));
    }
    Ok(())
}

pub fn check_ticket_cancel_allowed(state: &str) -> Result<(), ApiError> {
    if !crate::domain::ticket::CANCELLABLE.contains(&state) {
        return Err(pv(
            "ticket.cancel.not_if_resolved_or_closed",
            format!("Cannot cancel ticket in state '{state}'"),
            json!({ "current_state": state }),
        ));
    }
    Ok(())
}

// ── port request ────────────────────────────────────────────────────────────

pub fn check_direction_valid(direction: &str) -> Result<(), ApiError> {
    if direction != "port_in" && direction != "port_out" {
        return Err(pv(
            "port_request.create.direction_valid",
            format!("Direction must be port_in or port_out (got '{direction}')"),
            json!({ "direction": direction }),
        ));
    }
    Ok(())
}

pub fn check_target_sub_required(direction: &str, target: Option<&str>) -> Result<(), ApiError> {
    if direction == "port_out" && target.map(|t| t.is_empty()).unwrap_or(true) {
        return Err(pv(
            "port_request.create.target_sub_required_for_port_out",
            "port_out requires target_subscription_id",
            json!({ "direction": direction }),
        ));
    }
    Ok(())
}

pub fn check_donor_msisdn_unique(
    donor: &str,
    existing: Option<&PortRequestRow>,
) -> Result<(), ApiError> {
    if let Some(e) = existing {
        return Err(pv(
            "port_request.create.donor_msisdn_unique_among_pending",
            format!(
                "Donor MSISDN {donor} already has an open port request {} (state={})",
                e.id, e.state
            ),
            json!({ "donor_msisdn": donor, "existing_id": e.id, "existing_state": e.state }),
        ));
    }
    Ok(())
}

pub fn check_pr_transition_valid(state: &str, trigger: &str) -> Result<(), ApiError> {
    if !crate::domain::port_request::is_valid_transition(state, trigger) {
        return Err(pv(
            "port_request.transition.valid",
            format!("Cannot '{trigger}' a port request in state '{state}'"),
            json!({ "from_state": state, "trigger": trigger }),
        ));
    }
    Ok(())
}

pub fn check_reject_reason(reason: &str) -> Result<(), ApiError> {
    if reason.trim().is_empty() {
        return Err(pv(
            "port_request.reject.requires_reason",
            "Rejection reason is required",
            json!({}),
        ));
    }
    Ok(())
}
