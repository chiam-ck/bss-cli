//! Login-gated `/` — the customer dashboard. Port of `bss_self_serve.routes.landing`.
//!
//! Reads only: `subscription.list_for_customer` + per-line `get_balance` +
//! `catalog.list_offerings` (plan names) + `list_customer_offers` (assigned
//! offers). `customer_id` is bound from the verified session, so cross-customer
//! reads are impossible. Empty-state for unlinked / no-subscription identities.

use axum::extract::State;
use axum::response::Response;
use axum::Extension;
use minijinja::context;
use serde_json::{json, Value};

use crate::deps::require_session;
use crate::middleware::PortalSession;
use crate::routes::render;
use crate::templating::request_ctx;
use crate::AppState;

/// `GET /` — the dashboard.
pub async fn dashboard(
    State(state): State<AppState>,
    Extension(portal): Extension<PortalSession>,
) -> Response {
    if let Err(r) = require_session(&portal, "/") {
        return r;
    }
    let identity = portal.identity.clone();
    let email = identity.as_ref().map(|i| i.email.clone());
    let customer_id = identity.as_ref().and_then(|i| i.customer_id.clone());

    let Some(customer_id) = customer_id else {
        return render(
            &state,
            "dashboard_empty.html",
            context! {
                email => email,
                request => request_ctx("/", portal.identity_email()),
            },
        );
    };

    let Some(clients) = &state.clients else {
        return render(
            &state,
            "dashboard_empty.html",
            context! {
                email => email,
                request => request_ctx("/", portal.identity_email()),
            },
        );
    };

    let subs: Vec<Value> = match clients.subscription.list_for_customer(&customer_id).await {
        Ok(v) => v.as_array().cloned().unwrap_or_default(),
        Err(e) => {
            tracing::warn!(error = %e, "dashboard.subs_read_failed");
            Vec::new()
        }
    };
    if subs.is_empty() {
        return render(
            &state,
            "dashboard_empty.html",
            context! {
                email => email,
                request => request_ctx("/", portal.identity_email()),
            },
        );
    }

    let offerings: Vec<Value> = clients
        .catalog
        .list_offerings()
        .await
        .ok()
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default();
    let name_for = |oid: &str| -> String {
        offerings
            .iter()
            .find(|o| o.get("id").and_then(Value::as_str) == Some(oid))
            .and_then(|o| o.get("name").and_then(Value::as_str))
            .unwrap_or(oid)
            .to_string()
    };

    let now = bss_clock::now();
    let mut lines: Vec<Value> = Vec::with_capacity(subs.len());
    for sub in &subs {
        // Best-effort balance read — a 404 (pending_activation) → empty bars.
        let sub_id = sub.get("id").and_then(Value::as_str).unwrap_or_default();
        let balances: Vec<Value> = clients
            .subscription
            .get_balance(sub_id)
            .await
            .ok()
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default();
        lines.push(line_view(sub, &name_for, now, &balances));
    }

    // Assigned (issued) offers — customer-level, best-effort.
    let assigned_offers: Vec<Value> = clients
        .catalog
        .list_customer_offers(&customer_id, Some("issued"))
        .await
        .ok()
        .and_then(|v| v.get("offers").and_then(Value::as_array).cloned())
        .unwrap_or_default()
        .into_iter()
        .filter(|o| o.get("promotion").is_some())
        .collect();

    // v-reservation: a pending (incomplete) open order surfaces a low-prominence
    // "resume" link. Absent → the dashboard shows nothing about it.
    let open_order = clients
        .inventory
        .open_order_by_identity(email.as_deref().unwrap_or(""))
        .await
        .ok()
        .filter(|v| !v.is_null());

    render(
        &state,
        "dashboard.html",
        context! {
            email => email,
            customer_id => customer_id,
            lines => minijinja::Value::from_serialize(&lines),
            assigned_offers => minijinja::Value::from_serialize(&assigned_offers),
            open_order => minijinja::Value::from_serialize(&open_order),
            request => request_ctx("/", portal.identity_email()),
        },
    )
}

// ── per-line composition (port of _line_view / _bar_for / _cta_for) ──────────

const ALLOWANCE_LABEL: &[(&str, &str)] = &[
    ("data", "Data"),
    ("voice", "Voice"),
    ("sms", "SMS"),
    ("data_roaming", "Roaming"),
];

fn allowance_label(kind: &str) -> &'static str {
    ALLOWANCE_LABEL
        .iter()
        .find(|(k, _)| *k == kind)
        .map(|(_, v)| *v)
        .unwrap_or("?")
}

fn as_i64(v: &Value, key: &str) -> Option<i64> {
    v.get(key)
        .and_then(|x| x.as_i64().or_else(|| x.as_f64().map(|f| f as i64)))
}

fn bar_for(balance: &Value) -> Value {
    let atype = balance
        .get("allowanceType")
        .and_then(Value::as_str)
        .unwrap_or("");
    let label = allowance_label(atype);
    let unit = balance.get("unit").and_then(Value::as_str).unwrap_or("");
    let total = as_i64(balance, "total").unwrap_or(0);
    let consumed = as_i64(balance, "consumed").unwrap_or(0);
    let remaining = as_i64(balance, "remaining").unwrap_or((total - consumed).max(0));

    if total < 0 {
        return json!({
            "label": label, "unit": unit, "remaining": remaining, "total": total,
            "percent": 100, "unlimited": true, "low": false, "exhausted": false,
        });
    }
    let percent = if total > 0 {
        ((remaining as f64 / total as f64) * 100.0)
            .round()
            .clamp(0.0, 100.0) as i64
    } else {
        0
    };
    json!({
        "label": label, "unit": unit, "remaining": remaining, "total": total,
        "percent": percent, "unlimited": false,
        "low": percent > 0 && percent <= 10,
        "exhausted": remaining <= 0 && total > 0,
    })
}

fn cta_for(state: &str, has_pending: bool) -> &'static str {
    match state {
        "active" if has_pending => "pending_plan_change",
        "blocked" => "blocked",
        "pending_activation" => "pending_activation",
        "terminated" => "terminated",
        _ => "active",
    }
}

fn days_remaining(period_end: Option<&str>, now: chrono::DateTime<chrono::Utc>) -> Option<i64> {
    let end = period_end?;
    let parsed = chrono::DateTime::parse_from_rfc3339(&end.replace('Z', "+00:00")).ok()?;
    Some((parsed.with_timezone(&chrono::Utc) - now).num_days().max(0))
}

fn line_view(
    sub: &Value,
    name_for: &impl Fn(&str) -> String,
    now: chrono::DateTime<chrono::Utc>,
    balances: &[Value],
) -> Value {
    let state = sub.get("state").and_then(Value::as_str).unwrap_or("");
    let has_pending = sub
        .get("pendingOfferingId")
        .map(|v| !v.is_null())
        .unwrap_or(false);

    // Bars, with the stranded "Roaming 0/0" row filtered out.
    let bars: Vec<Value> = balances
        .iter()
        .map(bar_for)
        .filter(|b| {
            b.get("label").and_then(Value::as_str) != Some("Roaming")
                || b.get("total").and_then(Value::as_i64).unwrap_or(0) > 0
                || b.get("remaining").and_then(Value::as_i64).unwrap_or(0) > 0
        })
        .collect();

    // Applied promo discount badge, read off the subscription row.
    let discount = if sub
        .get("discountType")
        .map(|v| !v.is_null())
        .unwrap_or(false)
    {
        let remaining = as_i64(sub, "discountPeriodsRemaining").unwrap_or(0);
        let dtype = sub
            .get("discountType")
            .and_then(Value::as_str)
            .unwrap_or("");
        let dval = sub.get("discountValue").cloned().unwrap_or(json!(0));
        let currency = sub
            .get("priceCurrency")
            .and_then(Value::as_str)
            .unwrap_or("SGD");
        json!({
            "label": discount_label(dtype, &dval, currency),
            "effective_amount": sub.get("effectiveAmount").cloned().unwrap_or(Value::Null),
            "base_amount": sub.get("priceAmount").cloned().unwrap_or(Value::Null),
            "periods_remaining": remaining,
            "perpetual": remaining == -1,
            "active": remaining != 0,
        })
    } else {
        Value::Null
    };

    let offering_id = sub.get("offeringId").and_then(Value::as_str).unwrap_or("");
    let pending_id = sub.get("pendingOfferingId").and_then(Value::as_str);
    let mut line = json!({
        "id": sub.get("id").and_then(Value::as_str).unwrap_or(""),
        "msisdn": sub.get("msisdn").and_then(Value::as_str).unwrap_or(""),
        "state": state,
        "state_label": state.replace('_', " ").to_uppercase(),
        "offering_id": offering_id,
        "offering_name": name_for(offering_id),
        "current_period_end": sub.get("currentPeriodEnd").cloned().unwrap_or(Value::Null),
        "next_renewal_at": sub.get("nextRenewalAt").cloned().unwrap_or(Value::Null),
        "terminated_at": sub.get("terminatedAt").cloned().unwrap_or(Value::Null),
        "days_remaining": days_remaining(
            sub.get("currentPeriodEnd").and_then(Value::as_str), now),
        "pending_offering_id": pending_id,
        "pending_effective_at": sub.get("pendingEffectiveAt").cloned().unwrap_or(Value::Null),
        "cta_branch": cta_for(state, has_pending),
        "bars": bars,
        "discount": discount,
    });
    if let Some(pid) = pending_id {
        line["pending_offering_name"] = json!(name_for(pid));
    }
    line
}

/// Human-readable discount label. Port of `bss_models.discount.discount_label`:
/// `20% off` (percent, trailing zeros dropped) / `SGD 5.00 off` (absolute, 2dp).
pub fn discount_label(discount_type: &str, discount_value: &Value, currency: &str) -> String {
    let value = discount_value
        .as_f64()
        .or_else(|| discount_value.as_str().and_then(|s| s.parse::<f64>().ok()))
        .unwrap_or(0.0);
    match discount_type {
        "percent" => format!("{}% off", trim_num(value)),
        "absolute" => format!("{currency} {value:.2} off"),
        other => {
            tracing::warn!(discount_type = other, "portal.discount.unknown_type");
            String::new()
        }
    }
}

/// `20.0 -> "20"`, `12.5 -> "12.5"` (drop trailing zeros; no scientific notation).
fn trim_num(x: f64) -> String {
    let s = format!("{x:.6}");
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn discount_labels() {
        assert_eq!(discount_label("percent", &json!(20), "SGD"), "20% off");
        assert_eq!(discount_label("percent", &json!(12.5), "SGD"), "12.5% off");
        assert_eq!(discount_label("percent", &json!("20.00"), "SGD"), "20% off");
        assert_eq!(discount_label("absolute", &json!(5), "SGD"), "SGD 5.00 off");
        assert_eq!(
            discount_label("absolute", &json!("2.5"), "USD"),
            "USD 2.50 off"
        );
    }

    #[test]
    fn bar_math() {
        let b = bar_for(&json!({"allowanceType":"data","total":1000,"consumed":900,"unit":"mb"}));
        assert_eq!(b["percent"], 10);
        assert_eq!(b["low"], true);
        assert_eq!(b["label"], "Data");
        let unl = bar_for(&json!({"allowanceType":"voice","total":-1,"unit":"min"}));
        assert_eq!(unl["unlimited"], true);
        assert_eq!(unl["percent"], 100);
    }

    #[test]
    fn cta_branches() {
        assert_eq!(cta_for("active", true), "pending_plan_change");
        assert_eq!(cta_for("active", false), "active");
        assert_eq!(cta_for("blocked", false), "blocked");
        assert_eq!(cta_for("terminated", false), "terminated");
    }
}
