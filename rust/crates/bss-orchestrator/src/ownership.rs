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
//! profile` can confirm the seam was considered.

use bss_clients::CrmClient;
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

/// Best-effort: log to `tracing` + emit a CRM interaction on the actor's record so
/// the violation is auditable. The CRM interaction triggers the v0.1 auto-logging
/// path which writes an `audit.domain_event` row server-side, satisfying the
/// "audit row written" requirement (phases/V0_12_0.md §2.1).
///
/// Failures here must not mask the original violation; they are logged and
/// swallowed. Port of Python's `record_violation`.
pub async fn record_violation(
    crm: &CrmClient,
    actor: &str,
    tool_name: &str,
    path: &str,
    found: &Value,
    transcript_so_far: &str,
) {
    let (summary, body) = violation_audit_text(actor, tool_name, path, found, transcript_so_far);
    tracing::error!(
        tool_name = %tool_name,
        actor = %actor,
        path = %path,
        found = %truncate_chars(&found.to_string(), 200),
        "agent.ownership_violation"
    );
    if let Err(e) = crm
        .log_interaction_full(actor, &summary, None, Some("outbound"), Some(&body))
        .await
    {
        tracing::error!(
            tool_name = %tool_name,
            actor = %actor,
            error = %e,
            "agent.ownership_violation.audit_log_failed"
        );
    }
}

/// The `(summary, body)` written to the CRM interaction. Pure so the audit text is
/// golden-testable against the oracle — it lands in the permanent audit trail, so
/// the exact wording is the contract.
fn violation_audit_text(
    actor: &str,
    tool_name: &str,
    path: &str,
    found: &Value,
    transcript_so_far: &str,
) -> (String, String) {
    let found_repr = py_repr(found);
    let body = format!(
        "Tool: {tool_name}\n\
         Path: {path}\n\
         Found value: {found_repr}\n\
         Expected actor: {actor}\n\
         Transcript (first 1000 chars):\n{}",
        truncate_chars(transcript_so_far, 1000)
    );
    // Python interpolates `{tool_name!r}` — repr(), i.e. SINGLE quotes. Rust's
    // `{:?}` would emit double quotes and silently drift the audit text.
    let summary = format!(
        "P0 agent ownership violation on {} — output leaked {path}={found_repr}",
        py_repr_str(tool_name)
    );
    (summary, body)
}

/// Python slicing (`s[:n]`) counts *characters*, not bytes.
fn truncate_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Python `repr()` for a JSON value — the leaked value is interpolated with `!r`
/// into both the interaction summary and body, so the audit text depends on it.
/// Strings take Python's quote selection: single quotes unless the string contains
/// a `'` and no `"`.
fn py_repr(v: &Value) -> String {
    match v {
        Value::String(s) => py_repr_str(s),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Null => "None".to_string(),
        Value::Number(n) => n.to_string(),
        Value::Array(items) => {
            let inner: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", inner.join(", "))
        }
        Value::Object(map) => {
            let inner: Vec<String> = map
                .iter()
                .map(|(k, val)| format!("{}: {}", py_repr_str(k), py_repr(val)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
    }
}

fn py_repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
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

    /// Golden — the violation audit text lands in the permanent CRM interaction
    /// record, so the wording is a contract. Captured from the Python oracle.
    #[test]
    fn violation_audit_text_matches_oracle() {
        let (summary, body) = violation_audit_text(
            "CUST-001",
            "subscription.get",
            "[*].customerId",
            &json!("CUST-002"),
            "what is my balance?",
        );
        assert_eq!(
            summary,
            "P0 agent ownership violation on 'subscription.get' — \
             output leaked [*].customerId='CUST-002'"
        );
        assert_eq!(
            body,
            "Tool: subscription.get\n\
             Path: [*].customerId\n\
             Found value: 'CUST-002'\n\
             Expected actor: CUST-001\n\
             Transcript (first 1000 chars):\n\
             what is my balance?"
        );
    }

    /// `{found!r}` / `{tool_name!r}` are Python reprs — single-quoted strings, and
    /// `True`/`None` rather than `true`/`null`. Captured from the oracle.
    #[test]
    fn py_repr_matches_oracle() {
        assert_eq!(py_repr(&json!("CUST-002")), "'CUST-002'");
        assert_eq!(py_repr(&json!("it's")), "\"it's\"");
        assert_eq!(py_repr(&Value::Null), "None");
        assert_eq!(py_repr(&json!(true)), "True");
        assert_eq!(py_repr(&json!(42)), "42");
        assert_eq!(py_repr(&json!(["a", "b"])), "['a', 'b']");
        assert_eq!(py_repr(&json!({"k": "v"})), "{'k': 'v'}");
    }

    /// Python's `s[:1000]` counts characters, not bytes — a multi-byte transcript
    /// must not be sliced mid-codepoint (Rust would panic on a byte slice).
    #[test]
    fn transcript_truncation_is_char_wise() {
        let long: String = "é".repeat(1500);
        let (_, body) = violation_audit_text("CUST-001", "t", "p", &json!("x"), &long);
        let transcript = body.rsplit("chars):\n").next().unwrap();
        assert_eq!(transcript.chars().count(), 1000);
    }
}
