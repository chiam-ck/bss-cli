//! System prompts. Port of `orchestrator/bss_orchestrator/prompts.py`
//! (operator `SYSTEM_PROMPT`) + `customer_chat_prompt.py` (the customer-chat
//! builder). All prompt TEXT is embedded byte-for-byte (R2 — the prompt is a
//! behavioural contract with the model).
//!
//! **Doctrine guard:** the operator-side `ITERATIVE FLOW` block lives only in
//! `bss_cockpit`'s `COCKPIT_INVARIANTS` (ported in P5b) — it must NOT appear in the
//! customer-chat prompt. `test_iterative_flow_scope` pins that boundary.

use serde_json::Value;

/// The operator/CSR ops-copilot system prompt (`bss ask` / REPL). Verbatim.
pub const SYSTEM_PROMPT: &str = include_str!("prompt_txt/system_prompt.txt");

/// Customer-chat template for a linked (authenticated) customer. Verbatim; the
/// `{placeholder}` slots are filled by [`build_customer_chat_prompt`].
pub const CUSTOMER_CHAT_LINKED: &str = include_str!("prompt_txt/customer_linked.txt");

/// Customer-chat template for a pre-signup (browse-only) visitor. Verbatim.
pub const CUSTOMER_CHAT_ANONYMOUS: &str = include_str!("prompt_txt/customer_anonymous.txt");

/// Render the customer-chat system prompt with the customer's snapshot (the chat
/// route calls this once per turn). Port of `build_customer_chat_prompt`; empty
/// values fall back to the same `(loading)`/`there`/`active` placeholders as Python's
/// `x or default`. `operator_name` / `operator_support_email` default in Python to
/// `"BSS-CLI Mobile"` / `"support@bss-cli.local"` — the caller passes the branded
/// values.
#[allow(clippy::too_many_arguments)]
pub fn build_customer_chat_prompt(
    customer_name: &str,
    customer_email: &str,
    account_state: &str,
    current_plan: &str,
    balance_summary: &str,
    operator_name: &str,
    operator_support_email: &str,
    prior_messages: &[(String, String)],
    is_linked: bool,
) -> String {
    let email = or(customer_email, "your address on file");
    let base = if !is_linked {
        CUSTOMER_CHAT_ANONYMOUS
            .replace("{customer_email}", email)
            .replace("{operator_name}", operator_name)
            .replace("{operator_support_email}", operator_support_email)
    } else {
        CUSTOMER_CHAT_LINKED
            .replace("{customer_name}", or(customer_name, "there"))
            .replace("{customer_email}", email)
            .replace("{account_state}", or(account_state, "active"))
            .replace("{current_plan}", or(current_plan, "(loading)"))
            .replace("{balance_summary}", or(balance_summary, "(loading)"))
            .replace("{operator_name}", operator_name)
            .replace("{operator_support_email}", operator_support_email)
    };
    if prior_messages.is_empty() {
        return base;
    }
    let mut lines: Vec<String> = vec![
        String::new(),
        "Prior conversation in this session (oldest first):".to_string(),
    ];
    for (role, body) in prior_messages {
        let label = if role == "user" { "User" } else { "Assistant" };
        lines.push(format!("- {label}: {body}"));
    }
    lines.push(String::new());
    lines.push(
        "Continue the conversation naturally. The customer's next message is what you \
         must answer; do not re-introduce yourself, do not repeat earlier explanations \
         the customer already saw above."
            .to_string(),
    );
    format!("{base}\n{}\n", lines.join("\n"))
}

/// Compress a subscription's `balances` list into a one-line `balance_summary`. Port
/// of `build_balance_summary` — `(loading)` for a missing/empty subscription,
/// `no allowances` when it carries none, else `type used/total unit` per balance.
pub fn build_balance_summary(subscription: Option<&Value>) -> String {
    let empty = match subscription {
        None | Some(Value::Null) => true,
        Some(Value::Object(m)) => m.is_empty(),
        Some(_) => false,
    };
    if empty {
        return "(loading)".to_string();
    }
    let balances = subscription
        .and_then(|s| s.get("balances"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut parts: Vec<String> = Vec::new();
    for b in &balances {
        let b_type = b.get("type").and_then(Value::as_str).unwrap_or("");
        let used = b
            .get("used")
            .map(render_num)
            .unwrap_or_else(|| "0".to_string());
        let unit = b.get("unit").and_then(Value::as_str).unwrap_or("");
        match b.get("total") {
            None | Some(Value::Null) => parts.push(format!("{b_type} unlimited")),
            Some(total) => parts.push(format!("{b_type} {used}/{} {unit}", render_num(total))),
        }
    }
    if parts.is_empty() {
        "no allowances".to_string()
    } else {
        parts.join(", ")
    }
}

/// `x or default` for a `&str` (empty → default).
fn or<'a>(value: &'a str, default: &'a str) -> &'a str {
    if value.is_empty() {
        default
    } else {
        value
    }
}

/// A JSON number → its digits (matching Python `f"{n}"`), else `"0"` (the `used or 0`
/// / defensive path).
fn render_num(v: &Value) -> String {
    match v {
        Value::Number(n) => n.to_string(),
        _ => "0".to_string(),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use serde_json::json;

    #[test]
    fn iterative_flow_stays_out_of_customer_chat() {
        // Doctrine guard: ITERATIVE FLOW is an operator capability — it lives in
        // bss_cockpit's COCKPIT_INVARIANTS, never in the customer-chat prompt.
        assert!(!SYSTEM_PROMPT.contains("ITERATIVE FLOW"));
        assert!(!CUSTOMER_CHAT_LINKED.contains("ITERATIVE FLOW"));
        assert!(!CUSTOMER_CHAT_ANONYMOUS.contains("ITERATIVE FLOW"));
        // ...and it IS present in the ported cockpit invariants (the operator side).
        assert!(bss_cockpit::prompts::COCKPIT_INVARIANTS.contains("ITERATIVE FLOW"));
    }

    #[test]
    fn linked_prompt_fills_placeholders() {
        let out = build_customer_chat_prompt(
            "Ck",
            "ck@example.com",
            "active",
            "PLAN_M",
            "data 2/5 GB",
            "BSS-CLI Mobile",
            "support@bss-cli.local",
            &[],
            true,
        );
        assert!(out.contains("Ck"));
        assert!(out.contains("PLAN_M"));
        assert!(!out.contains("{customer_name}"), "no unfilled placeholders");
    }

    #[test]
    fn empty_values_use_loading_defaults() {
        let out = build_customer_chat_prompt(
            "",
            "",
            "",
            "",
            "",
            "BSS-CLI Mobile",
            "support@bss-cli.local",
            &[],
            true,
        );
        assert!(out.contains("there"));
        assert!(out.contains("(loading)"));
    }

    #[test]
    fn prior_messages_appended() {
        let prior = vec![
            ("user".to_string(), "hi".to_string()),
            ("assistant".to_string(), "hello".to_string()),
        ];
        let out = build_customer_chat_prompt(
            "Ck", "ck@x.com", "active", "PLAN_M", "-", "Op", "s@x.com", &prior, true,
        );
        assert!(out.contains("Prior conversation in this session (oldest first):"));
        assert!(out.contains("- User: hi"));
        assert!(out.contains("- Assistant: hello"));
    }

    #[test]
    fn balance_summary_shapes() {
        assert_eq!(build_balance_summary(None), "(loading)");
        assert_eq!(build_balance_summary(Some(&json!({}))), "(loading)");
        assert_eq!(
            build_balance_summary(Some(&json!({"id": "SUB-1"}))),
            "no allowances"
        );
        assert_eq!(
            build_balance_summary(Some(&json!({"balances": [
                {"type": "data", "used": 2, "total": 5, "unit": "GB"},
                {"type": "voice", "used": 0, "total": null, "unit": "min"}
            ]}))),
            "data 2/5 GB, voice unlimited"
        );
    }
}
