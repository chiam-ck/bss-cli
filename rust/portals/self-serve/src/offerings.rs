//! Flatten the TMF productOffering payload into template dicts. Port of
//! `bss_self_serve.offerings`.
//!
//! The catalog returns TMF-shaped JSON; templates want simple keys (`price`,
//! `data`, `voice`, `sms`, `roaming`). Sorted cheapest-first; non-sellable /
//! retired / VAS offerings drop out.

use serde::Serialize;
use serde_json::Value;

/// A template-shaped plan row.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct PlanRow {
    pub id: String,
    pub name: String,
    pub price: String,
    pub data: String,
    pub voice: String,
    pub sms: String,
    /// `None` (→ template `—`) for no/zero roaming inclusion.
    pub roaming: Option<String>,
}

/// Python `format(x, 'g')` for the value ranges the catalog produces (prices +
/// GB divisions): 6 significant digits, trailing zeros stripped. No scientific
/// notation in this range.
fn fmt_g(x: f64) -> String {
    if x == 0.0 {
        return "0".to_string();
    }
    let s = format!("{x:.6}");
    let trimmed = s.trim_end_matches('0').trim_end_matches('.');
    trimmed.to_string()
}

/// Render a JSON number the way Python `str()` would: integers without a decimal.
fn number_pystr(v: &Value) -> String {
    if let Some(i) = v.as_i64() {
        i.to_string()
    } else if let Some(u) = v.as_u64() {
        u.to_string()
    } else if let Some(f) = v.as_f64() {
        // Non-integral quantity (rare for allowances) — Python str(float).
        if f.fract() == 0.0 {
            format!("{f:.1}") // e.g. 500.0 -> "500.0"
        } else {
            fmt_g(f)
        }
    } else {
        v.to_string()
    }
}

fn price_value(p: &Value) -> f64 {
    if let Some(first) = p
        .get("productOfferingPrice")
        .and_then(Value::as_array)
        .and_then(|a| a.first())
    {
        if let Some(v) = first
            .get("price")
            .and_then(|x| x.get("taxIncludedAmount"))
            .and_then(|x| x.get("value"))
            .and_then(Value::as_f64)
        {
            return v;
        }
    }
    f64::INFINITY
}

fn is_sellable_plan(o: &Value) -> bool {
    let sellable = o.get("isSellable").and_then(Value::as_bool).unwrap_or(true);
    let status = o
        .get("lifecycleStatus")
        .and_then(Value::as_str)
        .unwrap_or("active");
    let bundle = o.get("isBundle").and_then(Value::as_bool).unwrap_or(true);
    sellable && status == "active" && bundle
}

fn allowance_str(allowances: &[Value], kind: &str) -> String {
    for a in allowances {
        let atype = a
            .get("allowanceType")
            .or_else(|| a.get("type"))
            .and_then(Value::as_str);
        if atype != Some(kind) {
            continue;
        }
        // `quantity` key present (even if null) wins over `total`.
        let qty = if a.get("quantity").is_some() {
            a.get("quantity").unwrap_or(&Value::Null)
        } else {
            a.get("total").unwrap_or(&Value::Null)
        };
        let unit = a.get("unit").and_then(Value::as_str).unwrap_or("");

        let is_unlimited =
            qty.is_null() || qty.as_str() == Some("unlimited") || qty.as_f64() == Some(-1.0);
        if is_unlimited {
            return "unlimited".to_string();
        }
        if unit == "mb" {
            if let Some(n) = qty.as_f64() {
                if n >= 1024.0 {
                    return format!("{} GB", fmt_g(n / 1024.0));
                }
            }
        }
        return format!("{} {}", number_pystr(qty), unit).trim().to_string();
    }
    "—".to_string()
}

fn price_str(p: &Value) -> String {
    if let Some(first) = p
        .get("productOfferingPrice")
        .and_then(Value::as_array)
        .and_then(|a| a.first())
    {
        if let Some(v) = first
            .get("price")
            .and_then(|x| x.get("taxIncludedAmount"))
            .and_then(|x| x.get("value"))
        {
            if let Some(f) = v.as_f64() {
                return fmt_g(f);
            }
        }
    }
    let flat = p.get("price").or_else(|| p.get("monthlyPrice"));
    match flat {
        Some(v) if !v.is_null() => number_pystr(v),
        _ => "?".to_string(),
    }
}

/// Find a flattened plan row by id, or `None`. Port of `offerings.find_plan`.
pub fn find_plan(flattened: &[PlanRow], plan_id: &str) -> Option<PlanRow> {
    flattened.iter().find(|p| p.id == plan_id).cloned()
}

/// Return template-shaped rows for every active sellable plan, cheapest-first.
pub fn flatten_offerings(offerings: &[Value]) -> Vec<PlanRow> {
    let mut sellable: Vec<&Value> = offerings.iter().filter(|o| is_sellable_plan(o)).collect();
    // Stable sort by recurring price ascending (Python `sorted`).
    sellable.sort_by(|a, b| {
        price_value(a)
            .partial_cmp(&price_value(b))
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    sellable
        .into_iter()
        .map(|p| {
            let empty = Vec::new();
            let allowances = p
                .get("bundleAllowance")
                .or_else(|| p.get("allowances"))
                .and_then(Value::as_array)
                .unwrap_or(&empty);
            let mut voice = allowance_str(allowances, "voice");
            if voice == "—" {
                voice = allowance_str(allowances, "voice_minutes");
            }
            let roaming = allowance_str(allowances, "data_roaming");
            let roaming = if roaming == "—" || roaming == "0 mb" {
                None
            } else {
                Some(roaming)
            };
            let id = p
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let name = p
                .get("name")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| id.clone());
            PlanRow {
                id,
                name,
                price: price_str(p),
                data: allowance_str(allowances, "data"),
                voice,
                sms: allowance_str(allowances, "sms"),
                roaming,
            }
        })
        .collect()
}
