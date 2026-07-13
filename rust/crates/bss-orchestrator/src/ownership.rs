//! Output ownership trip-wire — defence-in-depth for the chat surface (v0.12).
//! Port of `orchestrator/bss_orchestrator/ownership.py`.
//!
//! Server-side policies are the primary boundary; the `*.mine` wrappers add a
//! pre-flight check; this is the third layer — a check on every tool *result*. If a
//! tool ever returns a row whose `customerId` doesn't match the chat's bound actor,
//! that is a P0 (a policy miss, a wrapper alias gap, or a canonical tool over-
//! returning), and we fail loudly rather than ship the leak.
//!
//! `OWNERSHIP_PATHS` enumerates the JSON paths whose values must equal the actor for
//! each customer-bound tool. An empty list means "no customer-bound output by
//! contract" — the entry must still be present so `validate_ownership_paths_cover_
//! profile` can confirm the seam was considered. The route-side `record_violation`
//! (CRM interaction log) lands with the P6 chat route.

use serde_json::Value;

/// Raised when a tool's response leaks a customer-bound id that doesn't match the
/// chat's bound actor. Carries the tool name, expected actor, offending path + value.
#[derive(Debug, Clone)]
pub struct AgentOwnershipViolation {
    pub tool_name: String,
    pub actor: String,
    pub path: String,
    pub found: Value,
}

impl std::fmt::Display for AgentOwnershipViolation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "agent.ownership_violation: tool {:?} returned {}={} for actor {:?}",
            self.tool_name, self.path, self.found, self.actor
        )
    }
}

impl std::error::Error for AgentOwnershipViolation {}

/// JSON paths in each tool's response that must equal the bound actor. Path syntax
/// (tiny — no jsonpath): `key`, `a.b`, `[*].key`, `[*].a.b`. An empty slice means
/// "no customer-bound fields by contract". Port of Python's `OWNERSHIP_PATHS`.
pub const OWNERSHIP_PATHS: &[(&str, &[&str])] = &[
    // Public catalog reads — no customer-bound output.
    ("catalog.list_vas", &[]),
    ("catalog.list_active_offerings", &[]),
    ("catalog.get_offering", &[]),
    // Subscription reads.
    ("subscription.list_mine", &["[*].customerId"]),
    ("subscription.get_mine", &["customerId"]),
    // Balance / LPA / usage are scoped by an already-owned subscription_id; their
    // responses carry no customerId, so nothing to trip on.
    ("subscription.get_balance_mine", &[]),
    ("subscription.get_lpa_mine", &[]),
    ("usage.history_mine", &[]),
    // Customer / payment reads.
    ("customer.get_mine", &["id"]),
    ("payment.method_list_mine", &["[*].customerId"]),
    ("payment.charge_history_mine", &["[*].customerId"]),
    // Writes return the affected aggregate — its customerId must match.
    ("vas.purchase_for_me", &["customerId"]),
    ("subscription.schedule_plan_change_mine", &["customerId"]),
    (
        "subscription.cancel_pending_plan_change_mine",
        &["customerId"],
    ),
    ("subscription.terminate_mine", &["customerId"]),
    ("case.open_for_me", &["customerId"]),
    ("case.list_for_me", &["[*].customerId"]),
];

fn paths_for(tool_name: &str) -> Option<&'static [&'static str]> {
    OWNERSHIP_PATHS
        .iter()
        .find(|(t, _)| *t == tool_name)
        .map(|(_, p)| *p)
}

/// Resolve `path` against `obj`, returning `(label, value)` for every leaf reached.
/// Empty when the path doesn't exist (a missing key is not itself a violation).
fn walk(obj: &Value, path: &str) -> Vec<(String, Value)> {
    let mut frontier: Vec<(String, Value)> = vec![(String::new(), obj.clone())];
    for part in path.split('.') {
        let mut next: Vec<(String, Value)> = Vec::new();
        for (label, value) in &frontier {
            if part == "[*]" {
                if let Some(arr) = value.as_array() {
                    for (i, elem) in arr.iter().enumerate() {
                        next.push((format!("{label}[{i}]"), elem.clone()));
                    }
                }
            } else if let Some(map) = value.as_object() {
                if let Some(v) = map.get(part) {
                    let new_label = if label.is_empty() {
                        part.to_string()
                    } else {
                        format!("{label}.{part}")
                    };
                    next.push((new_label, v.clone()));
                }
            }
        }
        frontier = next;
    }
    frontier
}

/// Trip-wire: err if a customer-bound field in `result_json` doesn't equal `actor`.
/// Unconfigured tools and non-JSON observations are tolerated (can't carry a
/// customer-bound field). Port of `assert_owned_output`.
pub fn assert_owned_output(
    tool_name: &str,
    result_json: &str,
    actor: &str,
) -> Result<(), Box<AgentOwnershipViolation>> {
    let Some(paths) = paths_for(tool_name) else {
        return Ok(());
    };
    if paths.is_empty() {
        return Ok(());
    }
    let Ok(parsed) = serde_json::from_str::<Value>(result_json) else {
        return Ok(());
    };
    let expected = Value::String(actor.to_string());
    for path in paths {
        for (label, value) in walk(&parsed, path) {
            if value != expected {
                return Err(Box::new(AgentOwnershipViolation {
                    tool_name: tool_name.to_string(),
                    actor: actor.to_string(),
                    path: if label.is_empty() {
                        (*path).to_string()
                    } else {
                        label
                    },
                    found: value,
                }));
            }
        }
    }
    Ok(())
}

/// Startup self-check: every tool in `profile_tools` has an `OWNERSHIP_PATHS` entry
/// (`[]` allowed — a missing entry is not). Port of
/// `validate_ownership_paths_cover_profile`.
pub fn validate_ownership_paths_cover_profile(profile_tools: &[&str]) -> Result<(), String> {
    let mut missing: Vec<&str> = profile_tools
        .iter()
        .copied()
        .filter(|t| paths_for(t).is_none())
        .collect();
    if missing.is_empty() {
        return Ok(());
    }
    missing.sort_unstable();
    Err(format!(
        "OWNERSHIP_PATHS is missing entries for {missing:?}. Every tool in the \
         customer_self_serve profile needs an explicit entry — use [] if the tool's \
         response carries no customer-bound fields, but be deliberate about it."
    ))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use serde_json::json;

    #[test]
    fn passes_when_owned() {
        // list of dicts, each owned by the actor.
        let out = json!([{"customerId": "CUST-1"}, {"customerId": "CUST-1"}]).to_string();
        assert!(assert_owned_output("subscription.list_mine", &out, "CUST-1").is_ok());
        // nested single dict.
        let out = json!({"customerId": "CUST-1"}).to_string();
        assert!(assert_owned_output("subscription.get_mine", &out, "CUST-1").is_ok());
        // `customer.get_mine` keys on `id`.
        let out = json!({"id": "CUST-1"}).to_string();
        assert!(assert_owned_output("customer.get_mine", &out, "CUST-1").is_ok());
    }

    #[test]
    fn trips_on_foreign_id() {
        let out = json!([{"customerId": "CUST-1"}, {"customerId": "CUST-999"}]).to_string();
        let err = assert_owned_output("subscription.list_mine", &out, "CUST-1").unwrap_err();
        assert_eq!(err.found, json!("CUST-999"));
        assert_eq!(err.path, "[1].customerId");
    }

    #[test]
    fn tolerates_unconfigured_and_empty_and_nonjson() {
        // Unconfigured tool → no trip.
        assert!(assert_owned_output("catalog.get_active_price", "{}", "CUST-1").is_ok());
        // Empty-paths tool → no trip even with a foreign id present.
        let out = json!({"customerId": "CUST-999"}).to_string();
        assert!(assert_owned_output("subscription.get_balance_mine", &out, "CUST-1").is_ok());
        // Non-JSON observation (a tool-error string) → no trip.
        assert!(assert_owned_output(
            "subscription.get_mine",
            "{\"error\":\"CLIENT_ERROR\"}",
            "CUST-1"
        )
        .is_ok());
    }

    #[test]
    fn missing_key_is_not_a_violation() {
        // `customerId` absent → walk yields nothing → ok.
        let out = json!({"id": "SUB-1", "state": "active"}).to_string();
        assert!(assert_owned_output("subscription.get_mine", &out, "CUST-1").is_ok());
    }
}
