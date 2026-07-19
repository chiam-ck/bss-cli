//! Shared view helpers for the cockpit CRM screens (v1.6). Port of `bss_csr.views`.
//!
//! The BSS surfaces mix TMF **camelCase** payloads (customer, ticket,
//! subscription, order, payment) with internal **snake_case** DTOs (case, port
//! request). [`field`] reads both spellings so route handlers and templates never
//! care which family a payload came from — the same leniency the ASCII renderers
//! in `bss-cockpit` apply.
//!
//! **Doctrine (CLAUDE.md):** read payload keys through `field` — hardcoding one
//! family blanks fields silently (the v0.13 case page did exactly that).
//!
//! Everything here is read-side presentation logic. No client calls, no writes —
//! keep it that way so the route modules stay the only place that talks to
//! services.

use minijinja::Value as JValue;
use serde_json::Value;

/// `foo_bar` → `fooBar`. Python: `re.sub(r"_([a-z])", upper)`.
fn camel(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    let mut chars = name.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '_' {
            match chars.peek() {
                // Only a lowercase letter is lifted, matching the `[a-z]` class.
                Some(n) if n.is_ascii_lowercase() => {
                    out.push(n.to_ascii_uppercase());
                    chars.next();
                }
                _ => out.push('_'),
            }
        } else {
            out.push(c);
        }
    }
    out
}

/// First non-empty value among `names` — each tried as given AND camelCased, so
/// callers write snake_case once. `None`/`""` count as empty (Python's
/// `v not in (None, "")`); `false` and `0` do **not**.
pub fn field<'a>(d: Option<&'a Value>, names: &[&str]) -> Option<&'a Value> {
    let map = d?.as_object()?;
    for n in names {
        for key in [n.to_string(), camel(n)] {
            match map.get(&key) {
                None | Some(Value::Null) => continue,
                Some(Value::String(s)) if s.is_empty() => continue,
                Some(v) => return Some(v),
            }
        }
    }
    None
}

/// [`field`] as a string, or `default` when absent.
pub fn field_str(d: Option<&Value>, names: &[&str], default: &str) -> String {
    match field(d, names) {
        Some(Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
        None => default.to_string(),
    }
}

/// Compact `YYYY-MM-DD HH:MM` for ISO strings; `—` when empty.
///
/// Python slices the raw string (`s[:16].replace("T", " ")`) rather than parsing,
/// so a non-ISO value degrades to its first 16 characters rather than erroring —
/// reproduced exactly.
pub fn fmt_dt(value: &str) -> String {
    if value.is_empty() {
        return "—".to_string();
    }
    let truncated: String = value.chars().take(16).collect();
    if value.contains('T') {
        truncated.replace('T', " ")
    } else {
        truncated
    }
}

/// The `fmt_dt` Jinja filter — accepts any JSON scalar. Empty/none → `—`.
pub fn fmt_dt_value(v: &JValue) -> String {
    if v.is_none() || v.is_undefined() {
        return "—".to_string();
    }
    let s = match v.as_str() {
        Some(s) => s.to_string(),
        None => v.to_string(),
    };
    // Python's `if not value` catches "" and False alike.
    if s.is_empty() || s == "false" || s == "none" {
        return "—".to_string();
    }
    fmt_dt(&s)
}

/// Map an entity state to a badge tone (`ok` / `warn` / `err` / `muted`).
pub fn state_tone(state: &str) -> &'static str {
    match state.to_lowercase().as_str() {
        "active" | "completed" | "resolved" | "verified" | "approved" | "succeeded"
        | "available" | "delivered" | "sellable" => "ok",
        "blocked" | "failed" | "stuck" | "cancelled" | "canceled" | "declined" | "terminated"
        | "rejected" | "exhausted" | "errored" | "ported_out" | "suspended" => "err",
        "open" | "in_progress" | "pending" | "pending_customer" | "pending_activation"
        | "acknowledged" | "submitted" | "processing" | "reserved" | "awaiting_payment"
        | "draft" | "requested" => "warn",
        _ => "muted",
    }
}

/// `givenName familyName` off the TMF629 `individual`, falling back to `name`.
pub fn customer_name(c: Option<&Value>) -> String {
    let Some(c) = c else {
        return "—".to_string();
    };
    let individual = c.get("individual");
    let parts: Vec<String> = ["givenName", "familyName"]
        .iter()
        .filter_map(|k| individual.and_then(|i| i.get(*k)).and_then(Value::as_str))
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();
    let joined = parts.join(" ").trim().to_string();
    if !joined.is_empty() {
        return joined;
    }
    match c.get("name").and_then(Value::as_str) {
        Some(n) if !n.is_empty() => n.to_string(),
        _ => "—".to_string(),
    }
}

/// Card/table row view of a TMF629 customer payload.
pub fn flatten_customer(c: &Value) -> Value {
    let mut email = String::new();
    let mut msisdn = String::new();
    if let Some(mediums) = c.get("contactMedium").and_then(Value::as_array) {
        for cm in mediums {
            let ch = cm.get("characteristic");
            let medium_type = cm.get("mediumType").and_then(Value::as_str).unwrap_or("");
            let value = cm.get("value").and_then(Value::as_str).unwrap_or("");
            if medium_type == "email" && email.is_empty() {
                email = if value.is_empty() {
                    ch.and_then(|c| c.get("emailAddress"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string()
                } else {
                    value.to_string()
                };
            }
            if medium_type == "mobile" && msisdn.is_empty() {
                msisdn = if value.is_empty() {
                    ch.and_then(|c| c.get("phoneNumber"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string()
                } else {
                    value.to_string()
                };
            }
        }
    }
    serde_json::json!({
        "id": c.get("id").and_then(Value::as_str).unwrap_or("?"),
        "name": customer_name(Some(c)),
        "status": field_str(Some(c), &["status"], "?"),
        "kyc_status": field_str(Some(c), &["kyc_status"], "?"),
        "email": email,
        "msisdn": msisdn,
        "created_at": fmt_dt(&field_str(Some(c), &["created_at", "since"], "")),
    })
}

/// Normalize bundle balances for the progress-bar partial.
///
/// Accepts both the live subscription payload shape
/// (`allowanceType`/`total`/`consumed`/`remaining`, `-1` total = unlimited) and
/// the older renderer-test shape (`type`/`used`).
pub fn balance_rows(balances: Option<&Value>) -> Vec<Value> {
    let Some(items) = balances.and_then(Value::as_array) else {
        return Vec::new();
    };
    items
        .iter()
        .map(|b| {
            let label = field_str(Some(b), &["allowance_type", "type"], "?");
            let total = b.get("total");
            let mut used = b.get("consumed").or_else(|| b.get("used")).and_then(num_of);
            let unlimited = matches!(total, None | Some(Value::Null))
                || total.and_then(num_of) == Some(-1.0)
                || total.and_then(Value::as_str) == Some("unlimited");
            // remaining → used, only when total is a real number.
            if used.is_none() && !unlimited {
                if let (Some(remaining), Some(t)) =
                    (b.get("remaining").and_then(num_of), total.and_then(num_of))
                {
                    used = Some(t - remaining);
                }
            }
            let used_f = used.unwrap_or(0.0);
            let total_f = if unlimited {
                None
            } else {
                total.and_then(num_of)
            };
            let pct = match total_f {
                Some(t) if t != 0.0 => ((used_f / t * 100.0).round() as i64).min(100),
                _ => 0,
            };
            serde_json::json!({
                "label": label.replace('_', " "),
                "used": used_f,
                "total": total_f,
                "unit": b.get("unit").and_then(Value::as_str).unwrap_or("").to_uppercase(),
                "pct": pct,
                "unlimited": unlimited,
                "exhausted": !unlimited && total_f.is_some_and(|t| used_f >= t),
            })
        })
        .collect()
}

fn num_of(v: &Value) -> Option<f64> {
    v.as_f64()
}

/// `SGD 22` style price string from a TMF620 offering payload.
pub fn offering_price(o: Option<&Value>) -> String {
    let Some(o) = o else {
        return "—".to_string();
    };
    if let Some(pops) = o.get("productOfferingPrice").and_then(Value::as_array) {
        if let Some(first) = pops.first() {
            let price = first.get("price").and_then(|p| p.get("taxIncludedAmount"));
            if let Some(value) = price.and_then(|p| p.get("value")) {
                let unit = price
                    .and_then(|p| p.get("unit"))
                    .and_then(Value::as_str)
                    .unwrap_or("SGD");
                if !value.is_null() {
                    return format!("{unit} {}", fmt_g(value));
                }
            }
        }
    }
    match o.get("price").or_else(|| o.get("monthlyPrice")) {
        Some(v) if !v.is_null() => format!("SGD {}", fmt_g(v)),
        _ => "—".to_string(),
    }
}

/// Python's `%g` — drops a trailing `.0` on whole floats. Non-numbers pass through.
fn fmt_g(v: &Value) -> String {
    match v {
        Value::Number(_) => match v.as_f64() {
            Some(f) if f.fract() == 0.0 && f.abs() < 1e15 => format!("{}", f as i64),
            Some(f) => format!("{f}"),
            None => v.to_string(),
        },
        Value::String(s) => s.clone(),
        _ => v.to_string(),
    }
}

/// Human string for one allowance bucket (`data` / `voice` / `sms` /
/// `data_roaming`).
pub fn offering_allowance(o: &Value, kind: &str) -> String {
    let buckets = o
        .get("bundleAllowance")
        .or_else(|| o.get("allowances"))
        .and_then(Value::as_array);
    let Some(buckets) = buckets else {
        return "—".to_string();
    };
    for a in buckets {
        let atype = a
            .get("allowanceType")
            .or_else(|| a.get("type"))
            .and_then(Value::as_str)
            .unwrap_or("");
        // `voice` also matches the `voice_minutes` spelling.
        if atype != kind && !(kind == "voice" && atype == "voice_minutes") {
            continue;
        }
        let qty = if a.get("quantity").is_some() {
            a.get("quantity")
        } else {
            a.get("total")
        };
        let unit = a.get("unit").and_then(Value::as_str).unwrap_or("");
        let qty_num = qty.and_then(num_of);
        if qty.is_none()
            || matches!(qty, Some(Value::Null))
            || qty.and_then(Value::as_str) == Some("unlimited")
            || qty_num == Some(-1.0)
        {
            return "unlimited".to_string();
        }
        if unit == "mb" {
            if let Some(q) = qty_num {
                if q >= 1024.0 {
                    return format!("{} GB", fmt_g(&serde_json::json!(q / 1024.0)));
                }
            }
        }
        if unit == "min" || unit == "minutes" {
            return format!("{} min", fmt_g(qty.unwrap_or(&Value::Null)));
        }
        if unit == "sms" || unit == "count" {
            return format!("{} sms", fmt_g(qty.unwrap_or(&Value::Null)));
        }
        return format!("{} {unit}", fmt_g(qty.unwrap_or(&Value::Null)))
            .trim()
            .to_string();
    }
    "—".to_string()
}

pub fn flatten_order(o: &Value) -> Value {
    let items = o.get("items").and_then(Value::as_array);
    let offering_id = match items.and_then(|i| i.first()) {
        Some(first) => field_str(Some(first), &["offering_id"], "—"),
        None => "—".to_string(),
    };
    serde_json::json!({
        "id": o.get("id").and_then(Value::as_str).unwrap_or("?"),
        "customer_id": field_str(Some(o), &["customer_id"], "—"),
        "offering_id": offering_id,
        "state": field_str(Some(o), &["state"], "?"),
        "order_date": fmt_dt(&field_str(Some(o), &["order_date", "created_at"], "")),
        "completed_date": fmt_dt(&field_str(Some(o), &["completed_date"], "")),
    })
}

pub fn flatten_case(c: &Value) -> Value {
    let ticket_ids = c
        .get("ticket_ids")
        .or_else(|| c.get("ticketIds"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let tickets = c
        .get("tickets")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    serde_json::json!({
        "id": c.get("id").and_then(Value::as_str).unwrap_or("?"),
        "customer_id": field_str(Some(c), &["customer_id"], "—"),
        "subject": non_empty_str(c.get("subject")).unwrap_or_else(|| "(no subject)".to_string()),
        "state": field_str(Some(c), &["state"], "?"),
        "priority": field_str(Some(c), &["priority"], "—"),
        "category": field_str(Some(c), &["category"], "—"),
        "opened_at": fmt_dt(&field_str(Some(c), &["opened_at", "created_at"], "")),
        // Python: `len(ticket_ids) or len(tickets)` — the id list wins unless empty.
        "ticket_count": if ticket_ids > 0 { ticket_ids } else { tickets },
    })
}

pub fn flatten_ticket(t: &Value) -> Value {
    serde_json::json!({
        "id": t.get("id").and_then(Value::as_str).unwrap_or("?"),
        "type": field_str(Some(t), &["ticket_type", "type"], "—"),
        "subject": non_empty_str(t.get("subject")).unwrap_or_else(|| "(no subject)".to_string()),
        "state": field_str(Some(t), &["state"], "?"),
        "priority": field_str(Some(t), &["priority"], ""),
        "agent_id": field_str(Some(t), &["assigned_to_agent_id", "agent_id"], ""),
        "customer_id": field_str(Some(t), &["customer_id"], ""),
        "case_id": field_str(Some(t), &["case_id"], ""),
        "opened_at": fmt_dt(&field_str(Some(t), &["opened_at"], "")),
    })
}

/// Python's `c.get("subject") or "(no subject)"` — null AND empty both fall back.
fn non_empty_str(v: Option<&Value>) -> Option<String> {
    match v.and_then(Value::as_str) {
        Some(s) if !s.is_empty() => Some(s.to_string()),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;
    use serde_json::json;

    #[test]
    fn camel_lifts_only_lowercase_after_underscore() {
        assert_eq!(camel("customer_id"), "customerId");
        assert_eq!(camel("assigned_to_agent_id"), "assignedToAgentId");
        assert_eq!(camel("state"), "state");
        // Python's `_([a-z])` does not match `_1` or `_A`; the underscore stays.
        assert_eq!(camel("a_1"), "a_1");
        assert_eq!(camel("a_B"), "a_B");
        assert_eq!(camel("trailing_"), "trailing_");
    }

    /// The doctrine rule: both spellings resolve, snake_case written once.
    #[test]
    fn field_reads_both_key_families() {
        let snake = json!({"customer_id": "CUST-1"});
        let camel_p = json!({"customerId": "CUST-1"});
        assert_eq!(field_str(Some(&snake), &["customer_id"], "—"), "CUST-1");
        assert_eq!(field_str(Some(&camel_p), &["customer_id"], "—"), "CUST-1");
    }

    #[test]
    fn field_skips_empty_and_null_but_not_false_or_zero() {
        let v = json!({"a": null, "b": "", "c": false, "d": 0, "e": "x"});
        // null + "" are skipped → falls through to the next name.
        assert_eq!(field_str(Some(&v), &["a", "e"], "—"), "x");
        assert_eq!(field_str(Some(&v), &["b", "e"], "—"), "x");
        // false / 0 are real values (Python checks `not in (None, "")`).
        assert_eq!(field_str(Some(&v), &["c"], "—"), "false");
        assert_eq!(field_str(Some(&v), &["d"], "—"), "0");
    }

    #[test]
    fn field_default_on_missing_or_none() {
        assert_eq!(field_str(Some(&json!({})), &["nope"], "—"), "—");
        assert_eq!(field_str(None, &["nope"], "—"), "—");
    }

    #[test]
    fn fmt_dt_shapes() {
        assert_eq!(fmt_dt("2026-07-15T09:30:00+00:00"), "2026-07-15 09:30");
        assert_eq!(fmt_dt(""), "—");
        // Non-ISO degrades to the first 16 chars rather than erroring.
        assert_eq!(fmt_dt("not a date at all"), "not a date at al");
        assert_eq!(fmt_dt("short"), "short");
    }

    #[test]
    fn state_tone_buckets() {
        assert_eq!(state_tone("active"), "ok");
        assert_eq!(state_tone("ACTIVE"), "ok");
        assert_eq!(state_tone("blocked"), "err");
        assert_eq!(state_tone("ported_out"), "err");
        assert_eq!(state_tone("pending_activation"), "warn");
        assert_eq!(state_tone("whatever"), "muted");
        assert_eq!(state_tone(""), "muted");
    }

    #[test]
    fn customer_name_prefers_individual() {
        let c = json!({"individual": {"givenName": "Ada", "familyName": "Lovelace"}, "name": "x"});
        assert_eq!(customer_name(Some(&c)), "Ada Lovelace");
        // Partial individual still works.
        let c = json!({"individual": {"givenName": "Ada"}});
        assert_eq!(customer_name(Some(&c)), "Ada");
        // Falls back to `name`, then to the dash.
        assert_eq!(customer_name(Some(&json!({"name": "Acme"}))), "Acme");
        assert_eq!(customer_name(Some(&json!({}))), "—");
        assert_eq!(customer_name(None), "—");
    }

    #[test]
    fn flatten_customer_reads_contact_mediums() {
        let c = json!({
            "id": "CUST-1",
            "individual": {"givenName": "Ada", "familyName": "L"},
            "status": "active",
            "contactMedium": [
                {"mediumType": "email", "value": "a@b.c"},
                {"mediumType": "mobile", "characteristic": {"phoneNumber": "+6591234567"}},
            ],
        });
        let f = flatten_customer(&c);
        assert_eq!(f["email"], "a@b.c");
        // Falls back to the characteristic when `value` is absent.
        assert_eq!(f["msisdn"], "+6591234567");
        assert_eq!(f["name"], "Ada L");
        assert_eq!(f["kyc_status"], "?");
    }

    #[test]
    fn balance_rows_live_payload_shape() {
        let b = json!([{"allowanceType": "data", "total": 10240, "consumed": 5120, "unit": "mb"}]);
        let rows = balance_rows(Some(&b));
        assert_eq!(rows[0]["label"], "data");
        assert_eq!(rows[0]["pct"], 50);
        assert_eq!(rows[0]["unit"], "MB");
        assert_eq!(rows[0]["unlimited"], false);
        assert_eq!(rows[0]["exhausted"], false);
    }

    #[test]
    fn balance_rows_derives_used_from_remaining() {
        let b = json!([{"allowanceType": "data", "total": 100, "remaining": 25, "unit": "mb"}]);
        let rows = balance_rows(Some(&b));
        assert_eq!(rows[0]["used"], 75.0);
        assert_eq!(rows[0]["pct"], 75);
    }

    #[test]
    fn balance_rows_unlimited_and_exhausted() {
        let unl = json!([{"allowanceType": "voice", "total": -1, "used": 5}]);
        let rows = balance_rows(Some(&unl));
        assert_eq!(rows[0]["unlimited"], true);
        assert_eq!(rows[0]["pct"], 0);
        assert_eq!(rows[0]["exhausted"], false);

        let ex = json!([{"type": "data", "total": 100, "used": 100}]);
        let rows = balance_rows(Some(&ex));
        assert_eq!(rows[0]["exhausted"], true);
        assert_eq!(rows[0]["pct"], 100);

        assert!(balance_rows(None).is_empty());
    }

    #[test]
    fn offering_price_shapes() {
        let tmf = json!({"productOfferingPrice": [
            {"price": {"taxIncludedAmount": {"value": 22.0, "unit": "SGD"}}}
        ]});
        // %g drops the trailing .0.
        assert_eq!(offering_price(Some(&tmf)), "SGD 22");
        assert_eq!(offering_price(Some(&json!({"price": 15}))), "SGD 15");
        assert_eq!(offering_price(Some(&json!({}))), "—");
        assert_eq!(offering_price(None), "—");
    }

    #[test]
    fn offering_allowance_shapes() {
        let o = json!({"bundleAllowance": [
            {"allowanceType": "data", "quantity": 10240, "unit": "mb"},
            {"allowanceType": "voice_minutes", "quantity": 200, "unit": "min"},
            {"allowanceType": "sms", "quantity": -1, "unit": "sms"},
        ]});
        assert_eq!(offering_allowance(&o, "data"), "10 GB");
        // `voice` matches the `voice_minutes` spelling.
        assert_eq!(offering_allowance(&o, "voice"), "200 min");
        assert_eq!(offering_allowance(&o, "sms"), "unlimited");
        assert_eq!(offering_allowance(&o, "data_roaming"), "—");
        assert_eq!(offering_allowance(&json!({}), "data"), "—");
        // Sub-1024 mb stays in MB.
        let small = json!({"allowances": [{"type": "data", "total": 512, "unit": "mb"}]});
        assert_eq!(offering_allowance(&small, "data"), "512 mb");
    }

    #[test]
    fn flatten_case_ticket_count_prefers_id_list() {
        let c = json!({"id": "CASE-1", "ticket_ids": ["T1", "T2"], "tickets": [{}]});
        assert_eq!(flatten_case(&c)["ticket_count"], 2);
        // Empty id list → falls back to the embedded tickets.
        let c = json!({"id": "CASE-1", "ticket_ids": [], "tickets": [{}, {}, {}]});
        assert_eq!(flatten_case(&c)["ticket_count"], 3);
    }

    #[test]
    fn flatten_case_and_ticket_defaults() {
        let c = flatten_case(&json!({"id": "CASE-1", "subject": ""}));
        assert_eq!(c["subject"], "(no subject)");
        assert_eq!(c["priority"], "—");
        let t = flatten_ticket(&json!({"id": "TKT-1", "ticketType": "fault"}));
        assert_eq!(t["type"], "fault");
        assert_eq!(t["subject"], "(no subject)");
    }

    #[test]
    fn flatten_order_reads_first_item() {
        let o = json!({
            "id": "ORD-1", "customerId": "CUST-1", "state": "completed",
            "items": [{"offeringId": "PLAN_M"}],
            "orderDate": "2026-07-15T09:30:00Z",
        });
        let f = flatten_order(&o);
        assert_eq!(f["offering_id"], "PLAN_M");
        assert_eq!(f["customer_id"], "CUST-1");
        assert_eq!(f["order_date"], "2026-07-15 09:30");
        assert_eq!(f["completed_date"], "—");
        // No items → the dash, not a panic.
        assert_eq!(flatten_order(&json!({"id": "ORD-2"}))["offering_id"], "—");
    }
}
