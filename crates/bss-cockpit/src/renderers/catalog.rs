//! Catalog renderer — N-column plan comparison + single-plan card + VAS table.
//! Port of `bss_cockpit.renderers.catalog`.

use serde_json::Value;

use super::fmt::{ljust, scalar_str};

/// `PLAN_M` is the recommended default when present — gets a ★ marker on the
/// comparison table so the operator's eye lands on it first. When the catalog
/// grows past the v0.1 three-plan shape the marker stays on `PLAN_M` if still
/// active; if retired, the renderer falls back to the median plan by price (see
/// [`pick_popular`]).
const POPULAR_PLAN_DEFAULT: &str = "PLAN_M";

/// Python's `%g` — the shortest repr that round-trips, dropping a trailing `.0`.
fn fmt_g(x: f64) -> String {
    if x.fract() == 0.0 && x.abs() < 1e15 {
        format!("{}", x as i64)
    } else {
        format!("{x}")
    }
}

/// `%g` for a JSON value (numbers via [`fmt_g`], everything else as-is).
fn fmt_g_value(v: &Value) -> String {
    match v.as_f64() {
        Some(f) => fmt_g(f),
        None => scalar_str(v),
    }
}

/// Sort key — recurring price ascending. Offerings without a numeric price sink
/// to the end (`float("inf")`) so the catalog can grow new shapes without
/// breaking ordering.
fn price_value(p: &Value) -> f64 {
    p.get("productOfferingPrice")
        .and_then(Value::as_array)
        .and_then(|pops| pops.first())
        .and_then(|first| first.get("price"))
        .and_then(|price| price.get("taxIncludedAmount"))
        .and_then(|amt| amt.get("value"))
        .and_then(Value::as_f64)
        .unwrap_or(f64::INFINITY)
}

/// Active, sellable, bundle (i.e. a plan offering, not VAS). Each flag defaults
/// to permissive — Python's `o.get("isSellable", True)`.
fn is_sellable_plan(o: &Value) -> bool {
    let flag = |k: &str| o.get(k).and_then(Value::as_bool).unwrap_or(true);
    let lifecycle = o
        .get("lifecycleStatus")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("active");
    flag("isSellable") && lifecycle == "active" && flag("isBundle")
}

/// The ★ marker — sticks with `PLAN_M` if still active, else the median by price
/// (`n // 2`), so a 2-plan catalog stars the upper one and a 5-plan catalog stars
/// the middle one.
fn pick_popular(plans: &[&Value]) -> Option<String> {
    if plans.is_empty() {
        return None;
    }
    let has_default = plans
        .iter()
        .any(|p| p.get("id").and_then(Value::as_str) == Some(POPULAR_PLAN_DEFAULT));
    if has_default {
        return Some(POPULAR_PLAN_DEFAULT.to_string());
    }
    plans[plans.len() / 2].get("id").map(scalar_str)
}

/// Stringify a single allowance row (data/voice/sms) as human units.
fn allowance_str(allowances: &[Value], kind: &str) -> String {
    for a in allowances {
        let atype = a
            .get("allowanceType")
            .or_else(|| a.get("type"))
            .map(scalar_str)
            .unwrap_or_default();
        if atype != kind {
            continue;
        }
        // Python: `a.get("quantity") if "quantity" in a else a.get("total")` —
        // presence of the KEY decides, so an explicit `quantity: null` wins over
        // `total` and renders "unlimited".
        let qty = if a.get("quantity").is_some() {
            a.get("quantity")
        } else {
            a.get("total")
        };
        let unit = a.get("unit").and_then(Value::as_str).unwrap_or("");
        let qty_num = qty.and_then(Value::as_f64);
        if qty.is_none()
            || matches!(qty, Some(Value::Null))
            || qty.and_then(Value::as_str) == Some("unlimited")
            || qty_num == Some(-1.0)
        {
            return "unlimited".to_string();
        }
        // Prettify MB → GB once we hit the GB threshold.
        if unit == "mb" {
            if let Some(q) = qty_num.filter(|q| *q >= 1024.0) {
                return format!("{} GB", fmt_g(q / 1024.0));
            }
        }
        let qty_s = qty.map(fmt_g_value).unwrap_or_default();
        if unit == "min" || unit == "minutes" {
            return format!("{qty_s} min");
        }
        if unit == "sms" || unit == "count" {
            return format!("{qty_s} sms");
        }
        return format!("{qty_s} {unit}").trim().to_string();
    }
    "—".to_string()
}

/// `voice`, falling back to the `voice_minutes` spelling.
fn voice_str(allowances: &[Value]) -> String {
    let v = allowance_str(allowances, "voice");
    if v == "—" {
        allowance_str(allowances, "voice_minutes")
    } else {
        v
    }
}

/// SGD price string (no currency prefix).
fn price_str(p: &Value) -> String {
    if let Some(value) = p
        .get("productOfferingPrice")
        .and_then(Value::as_array)
        .and_then(|pops| pops.first())
        .and_then(|first| first.get("price"))
        .and_then(|price| price.get("taxIncludedAmount"))
        .and_then(|amt| amt.get("value"))
    {
        if !value.is_null() {
            return fmt_g_value(value);
        }
    }
    // Python: `str(flat)` — NOT %g, so a float 15.0 renders "15.0" here while the
    // TMF path renders "15". Faithfully different.
    match p.get("price").or_else(|| p.get("monthlyPrice")) {
        Some(v) if !v.is_null() => py_str(v),
        _ => "?".to_string(),
    }
}

/// Python's `str()` of a JSON scalar — floats keep their `.0`.
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Number(n) => match n.as_f64() {
            // serde_json keeps int/float distinct; `str(15.0)` is "15.0".
            Some(f) if n.is_f64() => {
                if f.fract() == 0.0 {
                    format!("{f:.1}")
                } else {
                    format!("{f}")
                }
            }
            _ => n.to_string(),
        },
        Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
        Value::Null => "None".to_string(),
        other => other.to_string(),
    }
}

/// All active sellable plans, cheapest first. New catalog entries appear
/// automatically — no source edit required (#36).
fn ordered_plans(offerings: &[Value]) -> Vec<&Value> {
    let mut plans: Vec<&Value> = offerings.iter().filter(|o| is_sellable_plan(o)).collect();
    // Python's `sorted` is STABLE, and so is `sort_by` — equal-priced plans keep
    // their catalog order.
    plans.sort_by(|a, b| {
        price_value(a)
            .partial_cmp(&price_value(b))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    plans
}

/// Stringify a VAS allowance for the table row.
fn vas_allowance_str(v: &Value) -> String {
    let qty = v.get("allowanceQuantity");
    let unit = v.get("allowanceUnit").and_then(Value::as_str).unwrap_or("");
    let qty_num = qty.and_then(Value::as_f64);
    if qty.is_none()
        || matches!(qty, Some(Value::Null))
        || qty.and_then(Value::as_str) == Some("unlimited")
        || qty_num == Some(-1.0)
    {
        return format!("unlimited {unit}").trim().to_string();
    }
    if unit == "mb" {
        if let Some(q) = qty_num.filter(|q| *q >= 1024.0) {
            return format!("{} GB", fmt_g(q / 1024.0));
        }
    }
    format!("{} {unit}", qty.map(fmt_g_value).unwrap_or_default())
        .trim()
        .to_string()
}

/// v0.19 — render the `catalog.list_vas` response as an ASCII table.
///
/// Wired into the REPL post-processor so the agent can't fabricate VAS
/// prices/names from prompt context (which was happening pre-v0.19 because the
/// dispatcher only had `catalog.list_offerings`).
pub fn render_vas_list(vas: &[Value]) -> String {
    if vas.is_empty() {
        return "(no VAS offerings in catalog)".to_string();
    }
    let rows: Vec<[String; 5]> = vas
        .iter()
        .map(|v| {
            let vid = v
                .get("id")
                .map(scalar_str)
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "?".to_string());
            let name = v.get("name").map(scalar_str).unwrap_or_default();
            let ccy = v
                .get("currency")
                .map(scalar_str)
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "SGD".to_string());
            let amount = match v.get("priceAmount") {
                None | Some(Value::Null) => "?".to_string(),
                Some(a) => scalar_str(a),
            };
            let expiry = v.get("expiryHours").and_then(Value::as_f64);
            // Python: `f"{expiry}h" if expiry else "—"` — 0 is falsy → the dash.
            let expiry_s = match expiry.filter(|e| *e != 0.0) {
                Some(e) => format!("{}h", fmt_g(e)),
                None => "—".to_string(),
            };
            [
                vid,
                name,
                format!("{ccy} {amount}"),
                vas_allowance_str(v),
                expiry_s,
            ]
        })
        .collect();

    let width_of = |i: usize, header: &str| -> usize {
        rows.iter()
            .map(|r| r[i].chars().count())
            .max()
            .unwrap_or(0)
            .max(header.chars().count())
    };
    let cols = [
        width_of(0, "id"),
        width_of(1, "name"),
        width_of(2, "price"),
        width_of(3, "allowance"),
        width_of(4, "expiry"),
    ];
    let inner: usize = cols.iter().sum::<usize>() + 4 * 3 + 2;

    let row = |c: &[&str; 5]| -> String {
        format!(
            "│ {} │ {} │ {} │ {} │ {} │",
            ljust(c[0], cols[0]),
            ljust(c[1], cols[1]),
            ljust(c[2], cols[2]),
            ljust(c[3], cols[3]),
            ljust(c[4], cols[4]),
        )
    };

    let title = " VAS Offerings ";
    let mut out = vec![format!(
        "┌─{title}{}┐",
        "─".repeat(inner.saturating_sub(title.chars().count() + 2).max(2))
    )];
    out.push(row(&["id", "name", "price", "allowance", "expiry"]));
    let rules: Vec<String> = cols.iter().map(|w| "─".repeat(*w)).collect();
    out.push(row(&[
        &rules[0], &rules[1], &rules[2], &rules[3], &rules[4],
    ]));
    for r in &rows {
        out.push(row(&[&r[0], &r[1], &r[2], &r[3], &r[4]]));
    }
    out.push(format!("└{}┘", "─".repeat(inner)));
    out.join("\n")
}

/// N-column plan comparison, cheapest-first (#36 — was hardcoded to PLAN_S/M/L;
/// now renders every active sellable plan).
pub fn render_catalog(offerings: &[Value]) -> String {
    let plans = ordered_plans(offerings);
    if plans.is_empty() {
        return "(no plans in catalog)".to_string();
    }
    let popular = pick_popular(&plans);

    let cols: Vec<Vec<String>> = plans
        .iter()
        .map(|p| {
            let id = p.get("id").map(scalar_str).unwrap_or_default();
            let name = match p.get("name") {
                Some(n) if !n.is_null() => scalar_str(n),
                _ => p
                    .get("id")
                    .map(scalar_str)
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "?".to_string()),
            };
            let price = price_str(p);
            let allowances = p
                .get("bundleAllowance")
                .filter(|v| !v.is_null())
                .or_else(|| p.get("allowances"))
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let marker = if Some(&id) == popular.as_ref() {
                " ★"
            } else {
                ""
            };
            vec![
                format!("{id}{marker}  {name}"),
                format!("SGD {price} /mo"),
                String::new(),
                format!("Data    {}", allowance_str(&allowances, "data")),
                format!("Voice   {}", voice_str(&allowances)),
                format!("SMS     {}", allowance_str(&allowances, "sms")),
                // v0.17 — show roaming when the plan carries any (PLAN_S has 0
                // and renders "—"; PLAN_M/L show their bundled MB).
                format!("Roaming {}", allowance_str(&allowances, "data_roaming")),
            ]
        })
        .collect();

    const COL_WIDTH: usize = 22;
    const SEP: &str = "  ";
    let n = plans.len();
    let inner_content = COL_WIDTH * n + SEP.len() * n.saturating_sub(1);
    let inner = inner_content + 4; // "  " left pad + " │" right pad spacing
    let title = "Product Offerings";
    let mut out = vec![format!(
        "┌─ {title} {}┐",
        "─".repeat(inner.saturating_sub(title.chars().count() + 4).max(2))
    )];
    let depth = cols.iter().map(Vec::len).max().unwrap_or(0);
    for i in 0..depth {
        let row = cols
            .iter()
            .map(|col| ljust(col.get(i).map(String::as_str).unwrap_or(""), COL_WIDTH))
            .collect::<Vec<_>>()
            .join(SEP);
        out.push(format!("│  {}  │", ljust(&row, inner - 4)));
    }
    out.push(format!(
        "│  {}  │",
        ljust("Prices in SGD, GST inclusive.", inner - 4)
    ));
    out.push(format!("└{}┘", "─".repeat(inner)));
    out.join("\n")
}

/// Expanded card for a single plan — `bss catalog show PLAN_M`.
pub fn render_catalog_show(offering: &Value) -> String {
    let pid = offering
        .get("id")
        .map(scalar_str)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "?".to_string());
    let name = match offering.get("name") {
        Some(n) if !n.is_null() => scalar_str(n),
        _ => pid.clone(),
    };
    let price = price_str(offering);
    let allowances = offering
        .get("bundleAllowance")
        .filter(|v| !v.is_null())
        .or_else(|| offering.get("allowances"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let data = allowance_str(&allowances, "data");
    let voice = voice_str(&allowances);
    let sms = allowance_str(&allowances, "sms");
    // v0.17 — additive roaming bucket, shown only when the plan has quota.
    let roaming = allowance_str(&allowances, "data_roaming");

    let marker = if pid == POPULAR_PLAN_DEFAULT {
        "  ★ MOST POPULAR"
    } else {
        ""
    };
    let title = format!("{pid}  {name}{marker}");
    const WIDTH: usize = 60;

    // Each row is hand-padded: `line + " " * max(0, WIDTH - len(line) - 1) + "│"`.
    let pad_row = |line: String| -> String {
        let fill = WIDTH.saturating_sub(line.chars().count() + 1);
        format!("{line}{}│", " ".repeat(fill))
    };

    let mut rows = vec![format!(
        "┌─ {title} {}┐",
        "─".repeat(WIDTH.saturating_sub(title.chars().count() + 4))
    )];
    rows.push(pad_row(format!(
        "│ Price       SGD {price} / month  (GST inclusive)"
    )));
    rows.push(format!("│ {}│", " ".repeat(WIDTH - 3)));
    rows.push(format!(
        "│ Bundle (every 30 days):{}│",
        " ".repeat(WIDTH.saturating_sub(26))
    ));
    rows.push(pad_row(format!("│   Data        {data}")));
    rows.push(pad_row(format!("│   Voice       {voice}")));
    rows.push(pad_row(format!("│   SMS         {sms}")));
    if roaming != "—" {
        rows.push(pad_row(format!("│   Roaming     {roaming}")));
    }
    rows.push(format!("│ {}│", " ".repeat(WIDTH - 3)));
    rows.push(format!(
        "│ Block-on-exhaust. Top up via VAS or wait for renewal.{}│",
        " ".repeat(WIDTH.saturating_sub(56))
    ));
    rows.push(format!("└{}┘", "─".repeat(WIDTH - 1)));
    rows.join("\n")
}
