//! Subscription hero renderer — the flagship ASCII view. Port of
//! `bss_cockpit.renderers.subscription`.

use chrono::{DateTime, Utc};
use serde_json::Value;

use super::boxes::{format_iccid, format_msisdn, progress_bar, r#box, state_dot, BAR_WIDTH};
use super::fmt::{ljust, py_round, py_title, rjust, rjust_f, truncate};

const FRAME_WIDTH: usize = 78;

/// One balance row: bar + numeric + percent, all column-aligned.
fn fmt_balance(used: f64, total: Option<f64>, unit: &str) -> String {
    match total {
        None => format!("{}  unlimited", progress_bar(0.0, None, BAR_WIDTH)),
        Some(t) => {
            // Python: `0 if not total else int(round((used / total) * 100))` —
            // `not total` catches 0.0, so a zero total is 0%, not a div-by-zero.
            let pct = if t == 0.0 {
                0
            } else {
                py_round((used / t) * 100.0)
            };
            let nums = format!(
                "{} / {} {}",
                rjust_f(used, 6, 1),
                rjust_f(t, 6, 1),
                ljust(&unit.to_uppercase(), 3)
            );
            format!(
                "{}  {}  {}%",
                progress_bar(used, Some(t), BAR_WIDTH),
                nums,
                rjust(&pct.to_string(), 3)
            )
        }
    }
}

/// `N days (YYYY-MM-DD)` — or the raw string when it doesn't parse, or `—` when
/// absent. `now` is the render-time wall clock (not transactional — matches the
/// oracle's explicit `noqa: bss-clock`).
fn days_to(dt_str: Option<&str>, now: Option<DateTime<Utc>>) -> String {
    let Some(dt_str) = dt_str.filter(|s| !s.is_empty()) else {
        return "—".to_string();
    };
    let normalized = dt_str.replace('Z', "+00:00");
    let Ok(then) = DateTime::parse_from_rfc3339(&normalized) else {
        // Python: `except ValueError: return dt_str` — the raw value passes through.
        return dt_str.to_string();
    };
    let then_utc = then.with_timezone(&Utc);
    let now = now.unwrap_or_else(Utc::now);
    // Python's `timedelta.days` FLOORS toward negative infinity: a delta of
    // -0.5 days is `-1`, not `0`. `num_days()` truncates toward zero, so the
    // floor is computed explicitly.
    let seconds = (then_utc - now).num_seconds();
    let days = (seconds as f64 / 86_400.0).floor() as i64;
    format!("{days} days ({})", then_utc.date_naive())
}

/// Right-aligned amount column, left-aligned dates. Empty when there's no history.
fn vas_history_table(history: &[Value]) -> Vec<String> {
    if history.is_empty() {
        return Vec::new();
    }
    let mut rows = vec![
        format!("── VAS Top-up History {}", "─".repeat(36)),
        String::new(),
        format!(
            "  {}  {}  {}",
            ljust("Date", 10),
            ljust("Offering", 14),
            rjust("Amount", 9)
        ),
        format!(
            "  {}  {}  {}",
            "─".repeat(10),
            "─".repeat(14),
            "─".repeat(9)
        ),
    ];
    for entry in history.iter().take(5) {
        let date = truncate(&or_str(entry, &["purchasedAt", "date"], "—"), 10);
        let offering = truncate(&or_str(entry, &["vasOfferingId", "offering"], "—"), 14);
        let amount = entry
            .get("amount")
            .or_else(|| entry.get("price"))
            .filter(|v| !v.is_null())
            .map(scalar_str)
            .filter(|s| !s.is_empty());
        // Python: `f"SGD {amount:>4}" if amount else "—"` — the falsy check means
        // an amount of 0 renders as the dash.
        let amount_str = match amount.filter(|a| a != "0") {
            Some(a) => format!("SGD {}", rjust(&a, 4)),
            None => "—".to_string(),
        };
        rows.push(format!(
            "  {}  {}  {}",
            ljust(&date, 10),
            ljust(&offering, 14),
            rjust(&amount_str, 9)
        ));
    }
    rows
}

/// First non-null value among `keys`, as a plain string; `default` otherwise.
/// Mirrors Python's `a or b or default` (so `""` and `null` both fall through).
fn or_str(v: &Value, keys: &[&str], default: &str) -> String {
    for k in keys {
        match v.get(*k) {
            None | Some(Value::Null) => continue,
            Some(x) => {
                let s = scalar_str(x);
                if !s.is_empty() {
                    return s;
                }
            }
        }
    }
    default.to_string()
}

/// A JSON scalar as Python would interpolate it (strings unquoted).
fn scalar_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// Optional context the dispatcher never supplies but `bss subscription show` does.
#[derive(Default)]
pub struct SubscriptionCtx<'a> {
    pub customer: Option<&'a Value>,
    pub offering: Option<&'a Value>,
    pub esim: Option<&'a Value>,
    pub now: Option<DateTime<Utc>>,
}

/// Render the subscription hero view.
///
/// Active subscriptions get a single-rule frame (`box`); **blocked** subscriptions
/// get a double-rule frame so the visual weight tells the story before the state
/// label is read (v0.6 polish).
pub fn render_subscription(sub: &Value, ctx: &SubscriptionCtx<'_>) -> String {
    let sub_id = or_str(sub, &["id"], "SUB-???");
    let cust_id = or_str(sub, &["customerId"], "—");
    let cust_name = ctx
        .customer
        .map(|c| or_str(c, &["name"], "—"))
        .unwrap_or_else(|| "—".to_string());
    let msisdn = format_msisdn(&or_str(sub, &["msisdn"], ""));
    let plan_name = ctx
        .offering
        .map(|o| or_str(o, &["name"], "—"))
        .unwrap_or_else(|| "—".to_string());
    let plan_id = or_str(sub, &["offeringId"], "—");
    let price = ctx
        .offering
        .and_then(|o| o.get("price"))
        .filter(|v| !v.is_null())
        .map(scalar_str)
        .filter(|s| !s.is_empty() && s != "0");
    let price_str = match price {
        Some(p) => format!(" — SGD {p}/mo"),
        None => String::new(),
    };
    let state = or_str(sub, &["state"], "unknown");

    let activated = or_str(sub, &["activatedAt", "startDate"], "—");
    let next_renewal = {
        let s = or_str(sub, &["nextRenewalAt", "endDate"], "");
        if s.is_empty() {
            None
        } else {
            Some(s)
        }
    };

    let balances = sub
        .get("balances")
        .filter(|v| !v.is_null())
        .or_else(|| sub.get("bundleBalances"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let mut rows: Vec<String> = Vec::new();
    for b in &balances {
        // Accept both shapes: (type/used) from the renderer's original tests, and
        // (allowanceType/consumed/remaining/total) from the live subscription
        // payload. `-1` total is the unlimited sentinel.
        let label = py_title(&or_str(b, &["type", "allowanceType"], "?"));
        let total_raw = b.get("total");
        let unlimited = match total_raw {
            None | Some(Value::Null) => true,
            Some(v) => v.as_f64() == Some(-1.0) || v.as_str() == Some("unlimited"),
        };
        let mut used_val = b.get("used").and_then(Value::as_f64);
        if used_val.is_none() && b.get("consumed").is_some() {
            used_val = b.get("consumed").and_then(Value::as_f64);
        }
        if used_val.is_none() && b.get("remaining").is_some() && !unlimited {
            if let (Some(t), Some(r)) = (
                total_raw.and_then(Value::as_f64),
                b.get("remaining").and_then(Value::as_f64),
            ) {
                used_val = Some(t - r);
            }
        }
        let used = used_val.unwrap_or(0.0);
        let total_val = if unlimited {
            None
        } else {
            total_raw.and_then(Value::as_f64)
        };
        let unit = b.get("unit").and_then(Value::as_str).unwrap_or("");
        rows.push(format!(
            "  {} {}",
            ljust(&label, 7),
            fmt_balance(used, total_val, unit)
        ));
    }
    if rows.is_empty() {
        rows.push("  (no bundle balances)".to_string());
    }

    let mut lines = vec![
        format!("Customer:  {cust_name} ({cust_id})"),
        format!("MSISDN:    {msisdn}"),
        format!("Plan:      {plan_name} ({plan_id}){price_str}"),
        format!("State:     {}", state_dot(&state)),
        format!("Activated: {activated}"),
        format!("Renews in: {}", days_to(next_renewal.as_deref(), ctx.now)),
        String::new(),
        format!("── Bundle {}", "─".repeat(50)),
    ];
    lines.extend(rows);

    // VAS history (if any).
    let history = sub
        .get("vasHistory")
        .filter(|v| !v.is_null())
        .or_else(|| sub.get("topUps"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if !history.is_empty() {
        lines.push(String::new());
        lines.extend(vas_history_table(&history));
    }

    // eSIM block — integrated, not floating below the frame.
    if let Some(esim) = ctx.esim {
        lines.push(String::new());
        lines.push(format!("── eSIM {}", "─".repeat(52)));
        lines.push(format!(
            "  ICCID:    {}",
            format_iccid(&or_str(esim, &["iccid"], "—"))
        ));
        let imsi = or_str(esim, &["imsi"], "");
        if !imsi.is_empty() {
            lines.push(format!("  IMSI:     {imsi}"));
        }
        let code = or_str(esim, &["activationCode"], "");
        if !code.is_empty() {
            lines.push(format!("  LPA:      {}", truncate(&code, 54)));
        }
    }

    let title = format!("Subscription {sub_id}");
    if state.to_lowercase() == "blocked" {
        super::boxes::double_box(&lines, &title, FRAME_WIDTH)
    } else {
        r#box(&lines, &title, FRAME_WIDTH)
    }
}
