//! Tool-result renderer dispatch — **the single source of truth**. Port of
//! `bss_cockpit.renderers.dispatch`.
//!
//! Both cockpit surfaces (the CLI REPL and the browser) consume the conversation
//! store's `tool`-role rows by passing the tool name + raw JSON result through
//! [`render_tool_result`]. It returns a deterministic ASCII string when a renderer
//! is registered for the tool, `None` otherwise.
//!
//! **Doctrine (v0.19+):** when a tool has no registered renderer the LLM is
//! instructed to surface the raw JSON verbatim and stop — never to fall back to a
//! markdown table. The browser wraps any non-`None` return in `<pre>` so the
//! visible output is byte-identical to the REPL's. There is exactly ONE rendering
//! rule for tool results; this module is it.

use serde_json::Value;

use super::catalog::{render_catalog, render_catalog_show, render_vas_list};
use super::customer::{render_customer_360, Customer360Ctx};
use super::esim::render_esim_activation;
use super::fmt::{ljust, scalar_str, truncate};
use super::order::{render_order, OrderCtx};
use super::subscription::{render_subscription, SubscriptionCtx};
use super::tables::{
    render_msisdn_count, render_msisdn_list, render_port_request_get, render_port_request_list,
};

/// Every tool name with a registered renderer. Kept as a list (rather than a map)
/// so the order is stable and greppable against the Python dict.
pub const RENDERED_TOOLS: &[&str] = &[
    // Single-entity get
    "subscription.get",
    "customer.get",
    "customer.find_by_msisdn",
    "order.get",
    "catalog.get_offering",
    "inventory.esim.get_activation",
    "subscription.get_esim_activation",
    // Lists
    "subscription.list_for_customer",
    "customer.list",
    "order.list",
    "catalog.list_offerings",
    "catalog.list_active_offerings",
    "catalog.list_vas",
    "inventory.msisdn.list_available",
    "inventory.msisdn.count",
    "port_request.list",
    "port_request.get",
    // Balance
    "subscription.get_balance",
];

/// Render a tool's stringified JSON result to deterministic ASCII.
///
/// Returns `None` when no renderer is registered for `tool_name`, when
/// `raw_result` isn't valid JSON, when the payload is **empty** (Python's
/// `if not payload` — `{}`, `[]`, `""`, `0`, `false`, `null` all count), or when a
/// renderer panics. That "best-effort, never break the surface" contract lets the
/// caller fall back to surfacing the raw JSON verbatim — never a markdown table.
pub fn render_tool_result(tool_name: &str, raw_result: &str) -> Option<String> {
    if !RENDERED_TOOLS.contains(&tool_name) {
        return None;
    }
    let payload: Value = serde_json::from_str(raw_result).ok()?;
    if is_falsy(&payload) {
        return None;
    }
    // Python wraps the renderer call in `except Exception: return None`. The Rust
    // renderers are total over `Value` (every accessor is optional-chained), so
    // there is no exception to catch — but a panic would still be a bug, not a
    // fallback, and `catch_unwind` would need UnwindSafe bounds across the closure.
    // Left deliberately un-caught: a panic here should surface in tests, not be
    // silently swallowed into "no renderer".
    Some(dispatch(tool_name, &payload))
}

/// Python's truthiness for the `if not payload` guard.
fn is_falsy(v: &Value) -> bool {
    match v {
        Value::Null => true,
        Value::Bool(b) => !b,
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
    }
}

fn as_array(v: &Value) -> Vec<Value> {
    v.as_array().cloned().unwrap_or_default()
}

fn dispatch(tool_name: &str, payload: &Value) -> String {
    match tool_name {
        "subscription.get" => render_subscription(payload, &SubscriptionCtx::default()),
        "customer.get" | "customer.find_by_msisdn" => render_customer(payload),
        "order.get" => render_order(payload, &OrderCtx::default()),
        "catalog.get_offering" => render_catalog_show(payload),
        "inventory.esim.get_activation" | "subscription.get_esim_activation" => {
            render_esim_activation(payload, false)
        }
        "subscription.list_for_customer" => render_subscription_list(&as_array(payload)),
        "customer.list" => render_customer_list(&as_array(payload)),
        "order.list" => render_order_list(&as_array(payload)),
        "catalog.list_offerings" | "catalog.list_active_offerings" => {
            render_catalog(&as_array(payload))
        }
        "catalog.list_vas" => render_vas_list(&as_array(payload)),
        "inventory.msisdn.list_available" => render_msisdn_list(&as_array(payload)),
        "inventory.msisdn.count" => render_msisdn_count(payload),
        "port_request.list" => render_port_request_list(&as_array(payload)),
        "port_request.get" => render_port_request_get(payload),
        "subscription.get_balance" => render_balance(payload),
        // Unreachable: RENDERED_TOOLS gates entry.
        _ => String::new(),
    }
}

fn render_subscription_list(payload: &[Value]) -> String {
    if payload.is_empty() {
        return "(no subscriptions)".to_string();
    }
    payload
        .iter()
        .map(|s| render_subscription(s, &SubscriptionCtx::default()))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Render the customer 360, unpacking the optional `_extras` block produced by
/// `customer.get` (subscriptions / cases / interactions). Callers that hand us a
/// bare TMF629 customer still render — extras just default to empty.
fn render_customer(payload: &Value) -> String {
    let extras = payload.get("_extras");
    let pull = |k: &str| -> Vec<Value> {
        extras
            .and_then(|e| e.get(k))
            .map(as_array)
            .unwrap_or_default()
    };
    let subscriptions = pull("subscriptions");
    let cases = pull("cases");
    let interactions = pull("interactions");
    render_customer_360(
        payload,
        &Customer360Ctx {
            subscriptions: &subscriptions,
            cases: &cases,
            interactions: &interactions,
            ..Default::default()
        },
    )
}

fn render_customer_list(payload: &[Value]) -> String {
    if payload.is_empty() {
        return "(no customers)".to_string();
    }
    let mut rows = vec![format!("── Customers {}", "─".repeat(50)), String::new()];
    rows.push(format!(
        "  {}  {}  {}  Email",
        ljust("ID", 15),
        ljust("Name", 24),
        ljust("Status", 10)
    ));
    rows.push(format!(
        "  {}  {}  {}  {}",
        "─".repeat(15),
        "─".repeat(24),
        "─".repeat(10),
        "─".repeat(30)
    ));
    for c in payload.iter().take(25) {
        let ind = c.get("individual");
        let parts: Vec<String> = ["givenName", "familyName"]
            .iter()
            .filter_map(|k| ind.and_then(|i| i.get(*k)).and_then(Value::as_str))
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect();
        let joined = parts.join(" ").trim().to_string();
        let name = if joined.is_empty() {
            // Python: `... or c.get("name", "—")` — absence gives the dash.
            match c.get("name") {
                None | Some(Value::Null) => "—".to_string(),
                Some(n) => scalar_str(n),
            }
        } else {
            joined
        };
        let mut email = String::new();
        if let Some(mediums) = c.get("contactMedium").and_then(Value::as_array) {
            for cm in mediums {
                if cm.get("mediumType").and_then(Value::as_str) == Some("email") {
                    email = cm.get("value").map(scalar_str).unwrap_or_default();
                    break;
                }
            }
        }
        rows.push(format!(
            "  {}  {}  {}  {}",
            ljust(&get_or(c, "id", "?"), 15),
            ljust(&truncate(&name, 24), 24),
            ljust(&get_or(c, "status", "?"), 10),
            truncate(&email, 30),
        ));
    }
    if payload.len() > 25 {
        rows.push(format!("  (+ {} more)", payload.len() - 25));
    }
    rows.join("\n")
}

fn render_order_list(payload: &[Value]) -> String {
    if payload.is_empty() {
        return "(no orders)".to_string();
    }
    let mut rows = vec![format!("── Orders {}", "─".repeat(50)), String::new()];
    rows.push(format!(
        "  {}  {}  {}  Placed",
        ljust("ID", 14),
        ljust("State", 14),
        ljust("Customer", 16)
    ));
    rows.push(format!(
        "  {}  {}  {}  {}",
        "─".repeat(14),
        "─".repeat(14),
        "─".repeat(16),
        "─".repeat(19)
    ));
    for o in payload.iter().take(25) {
        rows.push(format!(
            "  {}  {}  {}  {}",
            ljust(&get_or(o, "id", "?"), 14),
            ljust(&get_or(o, "state", "?"), 14),
            ljust(&get_or(o, "customerId", "—"), 16),
            truncate(&super::fmt::py_or(o, &["orderDate"], ""), 19),
        ));
    }
    if payload.len() > 25 {
        rows.push(format!("  (+ {} more)", payload.len() - 25));
    }
    rows.join("\n")
}

/// The balance payload is reshaped into a subscription-like dict so the hero
/// renderer can draw it.
fn render_balance(payload: &Value) -> String {
    let fake = serde_json::json!({
        "id": get_or(payload, "subscriptionId", "—"),
        "state": get_or(payload, "state", "?"),
        "balances": payload.get("balances").filter(|v| !v.is_null()).cloned()
            .unwrap_or_else(|| Value::Array(Vec::new())),
    });
    render_subscription(&fake, &SubscriptionCtx::default())
}

fn get_or(v: &Value, key: &str, default: &str) -> String {
    match v.get(key) {
        None | Some(Value::Null) => default.to_string(),
        Some(x) => scalar_str(x),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    /// Inventory lock — the registered set, verified against the Python
    /// `RENDERER_DISPATCH` keys (18 tools, diffed at port time). A tool that
    /// silently falls out of this list downgrades to raw JSON on both cockpit
    /// surfaces without any test failing, so the set is pinned explicitly.
    #[test]
    fn rendered_tool_set_is_locked() {
        let mut got = RENDERED_TOOLS.to_vec();
        got.sort_unstable();
        let mut want = vec![
            "catalog.get_offering",
            "catalog.list_active_offerings",
            "catalog.list_offerings",
            "catalog.list_vas",
            "customer.find_by_msisdn",
            "customer.get",
            "customer.list",
            "inventory.esim.get_activation",
            "inventory.msisdn.count",
            "inventory.msisdn.list_available",
            "order.get",
            "order.list",
            "port_request.get",
            "port_request.list",
            "subscription.get",
            "subscription.get_balance",
            "subscription.get_esim_activation",
            "subscription.list_for_customer",
        ];
        want.sort_unstable();
        assert_eq!(got, want);
        // Every registered tool must actually dispatch (no unreachable arm).
        for t in RENDERED_TOOLS {
            assert!(
                !dispatch(t, &serde_json::json!({"id": "X"})).is_empty(),
                "{t} fell through to the unreachable arm"
            );
        }
    }

    #[test]
    fn unregistered_tool_returns_none() {
        // Doctrine: no renderer → None → the caller surfaces raw JSON verbatim,
        // never a fabricated markdown table.
        assert!(render_tool_result("customer.create", r#"{"id":"CUST-1"}"#).is_none());
        assert!(render_tool_result("nope.at.all", "{}").is_none());
    }

    #[test]
    fn invalid_json_returns_none() {
        assert!(render_tool_result("customer.list", "not json").is_none());
        assert!(render_tool_result("customer.list", "").is_none());
    }

    #[test]
    fn empty_payload_returns_none() {
        // Python's `if not payload` — every falsy shape.
        assert!(render_tool_result("customer.list", "[]").is_none());
        assert!(render_tool_result("subscription.get", "{}").is_none());
        assert!(render_tool_result("subscription.get", "null").is_none());
        assert!(render_tool_result("inventory.msisdn.count", "0").is_none());
        assert!(render_tool_result("customer.list", "false").is_none());
        assert!(render_tool_result("customer.list", r#""""#).is_none());
    }

    #[test]
    fn registered_tool_renders() {
        let out = render_tool_result(
            "inventory.msisdn.count",
            r#"{"available":940,"reserved":5,"assigned":50,"ported_out":5,"total":1000}"#,
        )
        .expect("a registered tool renders");
        assert!(out.contains("MSISDN pool"));
        assert!(out.contains("940"));
    }

    /// `customer.get` unpacks `_extras`; a bare TMF629 customer still renders.
    #[test]
    fn customer_get_unpacks_extras() {
        let with_extras = render_tool_result(
            "customer.get",
            r#"{"id":"CUST-1","name":"Ada","_extras":{"subscriptions":[
                 {"id":"SUB-1","offeringId":"PLAN_M","state":"active","msisdn":"91234567"}],
                 "cases":[],"interactions":[]}}"#,
        )
        .unwrap();
        assert!(with_extras.contains("SUB-1"));
        assert!(with_extras.contains("Subscriptions (1)"));

        let bare = render_tool_result("customer.get", r#"{"id":"CUST-1","name":"Ada"}"#).unwrap();
        assert!(bare.contains("Subscriptions (0)"));
    }

    /// The balance payload is reshaped into a subscription-like dict.
    #[test]
    fn balance_renders_through_the_subscription_hero() {
        let out = render_tool_result(
            "subscription.get_balance",
            r#"{"subscriptionId":"SUB-9","state":"active",
                "balances":[{"type":"data","used":512,"total":1024,"unit":"mb"}]}"#,
        )
        .unwrap();
        assert!(out.contains("Subscription SUB-9"));
        assert!(out.contains("Data"));
    }

    #[test]
    fn empty_lists_render_their_placeholder_not_none() {
        // A NON-empty payload whose renderer yields a placeholder still returns
        // Some — only an empty payload short-circuits to None.
        let out = render_tool_result("subscription.list_for_customer", r#"[{"id":"SUB-1"}]"#);
        assert!(out.is_some());
    }
}
