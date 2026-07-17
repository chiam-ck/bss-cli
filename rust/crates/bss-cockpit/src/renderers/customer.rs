//! Customer 360 renderer — KYC, contact, subscriptions, cases, interactions.
//! Port of `bss_cockpit.renderers.customer`.

use std::collections::HashMap;

use serde_json::Value;

use super::boxes::{format_msisdn, r#box, state_dot};
use super::fmt::{ljust, py_or, scalar_str, truncate};

const FRAME_WIDTH: usize = 70;
const DEFAULT_INTERACTIONS_LIMIT: usize = 5;

fn get_or(v: &Value, key: &str, default: &str) -> String {
    match v.get(key) {
        None | Some(Value::Null) => default.to_string(),
        Some(x) => scalar_str(x),
    }
}

/// `email · phone`, or `—` when neither is present.
fn contact_line(contact_mediums: &[Value]) -> String {
    let mut email: Option<String> = None;
    let mut phone: Option<String> = None;
    for cm in contact_mediums {
        let ch = cm.get("characteristic");
        let pick = |field: &str| -> Option<String> {
            let from_ch = ch
                .and_then(|c| c.get(field))
                .map(scalar_str)
                .filter(|s| !s.is_empty());
            from_ch.or_else(|| cm.get("value").map(scalar_str).filter(|s| !s.is_empty()))
        };
        match cm.get("mediumType").and_then(Value::as_str) {
            // Python assigns unconditionally, so a LATER medium of the same type
            // overwrites an earlier one (unlike the `if not email` guards in
            // bss_csr.views.flatten_customer — deliberately different).
            Some("email") => email = pick("emailAddress"),
            Some("mobile") => phone = pick("phoneNumber"),
            _ => {}
        }
    }
    let parts: Vec<String> = [email, phone].into_iter().flatten().collect();
    if parts.is_empty() {
        "—".to_string()
    } else {
        parts.join(" · ")
    }
}

/// Compact `✓ KYC` inline next to the customer name; empty for other statuses.
fn kyc_badge(customer: &Value) -> String {
    let status = py_or(customer, &["kycStatus", "kyc_status"], "").to_lowercase();
    match status.as_str() {
        "verified" => " · ✓ KYC verified".to_string(),
        "not_verified" | "pending" => " · ⚠ KYC not verified".to_string(),
        _ => String::new(),
    }
}

/// `── Title (n) ─────`. The rule is at least 2 runes wide (Python's `max(2, ...)`).
fn section(title: &str, count: Option<usize>) -> String {
    let label = match count {
        Some(n) => format!("{title} ({n})"),
        None => title.to_string(),
    };
    let fill = 60usize.saturating_sub(label.chars().count() + 4).max(2);
    format!("── {label} {}", "─".repeat(fill))
}

/// Compact `  bundle 42%` for a sub's data row; empty when there's no data bundle.
///
/// Note this **truncates** (`int(...)`), unlike the subscription renderer's
/// balance rows which use banker's `round()`. Faithfully different.
fn bundle_pct(s: &Value) -> String {
    let Some(balances) = s.get("balances").and_then(Value::as_array) else {
        return String::new();
    };
    for b in balances {
        if b.get("type").and_then(Value::as_str) != Some("data") {
            continue;
        }
        // Python: `if b.get("type") == "data" and b.get("total")` — a total of 0
        // or null is falsy, so the row is skipped entirely.
        let Some(total) = b.get("total").and_then(Value::as_f64).filter(|t| *t != 0.0) else {
            continue;
        };
        let used = b.get("used").and_then(Value::as_f64).unwrap_or(0.0);
        return format!("  bundle {}%", (used / total * 100.0) as i64);
    }
    String::new()
}

/// Everything the 360 stitches around the bare TMF629 customer.
#[derive(Default)]
pub struct Customer360Ctx<'a> {
    pub subscriptions: &'a [Value],
    pub cases: &'a [Value],
    pub tickets_by_case: HashMap<String, Vec<Value>>,
    pub interactions: &'a [Value],
    /// Python's `interactions_limit: int = 5`.
    pub interactions_limit: Option<usize>,
}

/// Render the customer 360 hero view (the CLI counterpart to the portal 360).
pub fn render_customer_360(customer: &Value, ctx: &Customer360Ctx<'_>) -> String {
    let cid = get_or(customer, "id", "CUST-???");
    let name = get_or(customer, "name", "—");
    let status = get_or(customer, "status", "unknown");
    let since = truncate(&py_or(customer, &["createdAt", "since"], "—"), 10);
    let mediums = customer
        .get("contactMedium")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let contact = contact_line(&mediums);
    let kyc = kyc_badge(customer);
    let limit = ctx.interactions_limit.unwrap_or(DEFAULT_INTERACTIONS_LIMIT);

    let mut lines = vec![
        format!("Status:  {}{kyc}", state_dot(&status)),
        format!("Contact: {contact}"),
        format!("Since:   {since}"),
        String::new(),
    ];

    // ── Subscriptions — one compact card per sub ────────────────────────────
    lines.push(section("Subscriptions", Some(ctx.subscriptions.len())));
    if ctx.subscriptions.is_empty() {
        lines.push("  (none)".to_string());
    }
    for s in ctx.subscriptions {
        let msisdn = format_msisdn(&get_or(s, "msisdn", ""));
        let bundle = bundle_pct(s);
        let sub_state = get_or(s, "state", "?");
        let marker = if sub_state == "blocked" || sub_state == "suspended" {
            "⚠ "
        } else {
            "  "
        };
        lines.push(format!(
            "{marker}{} {} {} {msisdn}{bundle}",
            ljust(&get_or(s, "id", "?"), 10),
            ljust(&get_or(s, "offeringId", "?"), 8),
            ljust(&sub_state, 10),
        ));
    }
    lines.push(String::new());

    // ── Cases — open listed; resolved/closed collapsed to a count ───────────
    let open_cases: Vec<&Value> = ctx
        .cases
        .iter()
        .filter(|c| {
            !matches!(
                c.get("state").and_then(Value::as_str),
                Some("closed") | Some("resolved")
            )
        })
        .collect();
    let closed_count = ctx.cases.len() - open_cases.len();
    lines.push(section("Open Cases", Some(open_cases.len())));
    if open_cases.is_empty() {
        lines.push("  (none)".to_string());
    }
    for c in &open_cases {
        let subject = truncate(&py_or(c, &["subject"], "(no subject)"), 34);
        lines.push(format!(
            "  {} {} {} {}",
            ljust(&get_or(c, "id", ""), 10),
            ljust(&subject, 34),
            ljust(&get_or(c, "priority", ""), 6),
            get_or(c, "state", ""),
        ));
        let case_id = c.get("id").map(scalar_str).unwrap_or_default();
        if let Some(tickets) = ctx.tickets_by_case.get(&case_id) {
            for t in tickets {
                lines.push(format!(
                    "    └─ {} {} {} {}",
                    ljust(&get_or(t, "id", ""), 8),
                    ljust(&get_or(t, "ticketType", ""), 14),
                    ljust(&get_or(t, "priority", ""), 6),
                    get_or(t, "state", ""),
                ));
            }
        }
    }
    if closed_count > 0 {
        lines.push(format!("  (+ {closed_count} resolved/closed)"));
    }
    lines.push(String::new());

    // ── Recent interactions ─────────────────────────────────────────────────
    lines.push(section("Recent Interactions", Some(ctx.interactions.len())));
    if ctx.interactions.is_empty() {
        lines.push("  (none)".to_string());
    }
    for it in ctx.interactions.iter().take(limit) {
        let when = truncate(&py_or(it, &["createdAt", "occurredAt"], ""), 16).replace('T', " ");
        let chan = truncate(&py_or(it, &["channel"], ""), 14);
        let action = truncate(&py_or(it, &["action", "summary"], ""), 34);
        lines.push(format!(
            "  {}  {}  {action}",
            ljust(&when, 16),
            ljust(&chan, 14)
        ));
    }
    if ctx.interactions.len() > limit {
        lines.push(format!(
            "  (+ {} more — --interactions N to widen)",
            ctx.interactions.len() - limit
        ));
    }

    r#box(&lines, &format!("{cid}  {name}"), FRAME_WIDTH)
}
