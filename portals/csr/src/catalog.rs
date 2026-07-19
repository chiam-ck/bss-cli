//! Catalog screens — plans, VAS, promotions + offering detail (v1.6 cockpit CRM).
//! Port of `bss_csr.routes.catalog`.
//!
//! v1.6.1 (operator directive) — catalog admin CRUD is direct: add an offering,
//! add a price row, set a validity window, retire; the same `admin_*` client
//! surface the `bss admin catalog` CLI uses, policy-gated server-side. Promotion
//! lifecycle stays chat/CLI-only because `bss promo assign` composes loyalty
//! pairing (v1.3) on top of the catalog write — a bare UI form would silently skip
//! the loyalty mint (CLAUDE.md carve-out).

use axum::extract::{Form, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use bss_clients::ClientError;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::routes::{back_to, render};
use crate::views::{field_str, fmt_dt, offering_allowance, offering_price};
use crate::AppState;

/// Card/detail view of a plan offering. `isSellable`/`isBundle` default true.
fn plan_view(o: &Value) -> Value {
    json!({
        "id": o.get("id").and_then(Value::as_str).unwrap_or("?"),
        "name": o.get("name").and_then(Value::as_str).unwrap_or(""),
        "price": offering_price(Some(o)),
        "lifecycle": field_str(Some(o), &["lifecycle_status"], "active"),
        "sellable": o.get("isSellable").and_then(Value::as_bool).unwrap_or(true),
        "data": offering_allowance(o, "data"),
        "voice": offering_allowance(o, "voice"),
        "sms": offering_allowance(o, "sms"),
        "roaming": offering_allowance(o, "data_roaming"),
    })
}

// ── GET /catalog ─────────────────────────────────────────────────────

#[derive(Deserialize, Default)]
pub struct FlashQuery {
    #[serde(default)]
    flash: String,
    #[serde(default)]
    err: String,
}

pub async fn catalog_index(State(state): State<AppState>, Query(q): Query<FlashQuery>) -> Response {
    let mut plans: Vec<Value> = Vec::new();
    let mut vas_views: Vec<Value> = Vec::new();
    let mut promo_views: Vec<Value> = Vec::new();

    if let Some(clients) = &state.clients {
        let offerings = match clients.catalog.list_offerings().await {
            Ok(v) => v.as_array().cloned().unwrap_or_default(),
            Err(e) => {
                tracing::warn!(status = e.status_code(), "csr.catalog.list_failed");
                Vec::new()
            }
        };
        plans = offerings
            .iter()
            // isBundle defaults true.
            .filter(|o| o.get("isBundle").and_then(Value::as_bool).unwrap_or(true))
            .map(plan_view)
            .collect();

        let vas = clients
            .catalog
            .list_vas()
            .await
            .ok()
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default();
        vas_views = vas
            .iter()
            .map(|v| {
                let allowance = format!(
                    "{} {}",
                    v.get("allowanceQuantity")
                        .and_then(Value::as_str)
                        .unwrap_or("—"),
                    v.get("allowanceUnit").and_then(Value::as_str).unwrap_or(""),
                )
                .trim()
                .to_string();
                let expiry = match v.get("expiryHours") {
                    Some(h) if !h.is_null() => format!("{}h", scalar(h)),
                    _ => "—".to_string(),
                };
                json!({
                    "id": v.get("id").and_then(Value::as_str).unwrap_or("?"),
                    "name": v.get("name").and_then(Value::as_str).unwrap_or(""),
                    "price": format!(
                        "{} {}",
                        v.get("currency").and_then(Value::as_str).unwrap_or("SGD"),
                        v.get("priceAmount").map(scalar).unwrap_or_else(|| "?".to_string()),
                    ),
                    "allowance": allowance,
                    "expiry": expiry,
                })
            })
            .collect();

        let promotions = clients
            .catalog
            .list_promotions(None, 50, 0)
            .await
            .ok()
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default();
        promo_views = promotions
            .iter()
            .map(|p| {
                let discount = format!(
                    "{} {}",
                    field_str(Some(p), &["discount_type"], ""),
                    field_str(Some(p), &["discount_value"], ""),
                )
                .trim()
                .to_string();
                json!({
                    "id": p.get("id").and_then(Value::as_str).unwrap_or("?"),
                    "name": field_str(Some(p), &["display_name", "name"], ""),
                    "code": field_str(Some(p), &["code"], "—"),
                    "state": field_str(Some(p), &["state"], "?"),
                    "discount": if discount.is_empty() { "—".to_string() } else { discount },
                    "audience": field_str(Some(p), &["audience"], ""),
                    "valid_to": fmt_dt(&field_str(Some(p), &["valid_to"], "")),
                })
            })
            .collect();
    }

    render(
        &state,
        "catalog_index.html",
        minijinja::Value::from_serialize(json!({
            "active_page": "catalog",
            "model": "(env default)",
            "plans": plans,
            "vas": vas_views,
            "promotions": promo_views,
            "flash": q.flash,
            "err": q.err.chars().take(300).collect::<String>(),
        })),
    )
}

/// A JSON scalar as the string Python's f-string would render — an integer without
/// the `.0` serde would add for a float, a bare string without quotes.
fn scalar(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.to_string()
            } else {
                n.to_string()
            }
        }
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

// ── writes ───────────────────────────────────────────────────────────

/// datetime-local input (`2026-06-10T00:00`) → Python's `datetime.fromisoformat()
/// .isoformat()` string, or an error carrying Python's exact `ValueError` text.
///
/// Empty → `Ok(None)` (the field is omitted). A browser datetime-local widget
/// always emits a parseable value; the error path exists only to reproduce the
/// oracle's flash text for a hand-crafted bad value.
fn parse_dt(raw: &str) -> Result<Option<String>, String> {
    use chrono::{NaiveDate, NaiveDateTime};
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(None);
    }
    let parsed = NaiveDateTime::parse_from_str(raw, "%Y-%m-%dT%H:%M:%S")
        .or_else(|_| NaiveDateTime::parse_from_str(raw, "%Y-%m-%dT%H:%M"))
        .ok()
        // Date-only → midnight, matching `fromisoformat("2026-06-10")`.
        .or_else(|| {
            NaiveDate::parse_from_str(raw, "%Y-%m-%d")
                .ok()
                .and_then(|d| d.and_hms_opt(0, 0, 0))
        });
    let dt = parsed.ok_or_else(|| format!("Invalid isoformat string: '{raw}'"))?;
    // Python's isoformat drops seconds only when microseconds are present; for our
    // inputs it always yields `YYYY-MM-DDTHH:MM:SS`.
    Ok(Some(dt.format("%Y-%m-%dT%H:%M:%S").to_string()))
}

/// `_back(url, …)` — the catalog screen flashes back to an arbitrary URL (the
/// index for add-offering, the offering detail for the rest).
fn back(url: &str, flash: &str, err: &str) -> Response {
    back_to(url, flash, err)
}

/// Catalog client errors are worded "Catalog rejected the {thing}: {err}", so this
/// screen keeps its own error mapping rather than the CRM-worded `write_result`.
fn catalog_result(url: &str, thing: &str, action: &str, r: Result<Value, ClientError>) -> Response {
    match r {
        Ok(_) => back(url, action, ""),
        Err(ClientError::Policy(p)) => back(url, "", &p.message),
        Err(e) => back(url, "", &format!("Catalog rejected the {thing}: {e}")),
    }
}

#[derive(Deserialize)]
pub struct OfferingForm {
    offering_id: String,
    name: String,
    amount: String,
    #[serde(default)]
    data_mb: Option<i64>,
    #[serde(default)]
    voice_minutes: Option<i64>,
    #[serde(default)]
    sms_count: Option<i64>,
    #[serde(default)]
    data_roaming_mb: Option<i64>,
}

/// `POST /catalog/offering` — add an offering, then land on its detail.
pub async fn add_offering(
    State(state): State<AppState>,
    Form(form): Form<OfferingForm>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let offering_id = form.offering_id.trim().to_string();
    let r = clients
        .catalog
        .admin_add_offering(
            &offering_id,
            form.name.trim(),
            form.amount.trim(),
            "SGD",                 // currency default
            "SPEC_MOBILE_PREPAID", // spec_id default
            None,
            None,
            form.data_mb,
            form.voice_minutes,
            form.sms_count,
            form.data_roaming_mb,
        )
        .await;
    match r {
        Ok(_) => back(&format!("/catalog/{offering_id}"), "offering_added", ""),
        Err(ClientError::Policy(p)) => back("/catalog", "", &p.message),
        Err(e) => back(
            "/catalog",
            "",
            &format!("Catalog rejected the offering: {e}"),
        ),
    }
}

#[derive(Deserialize)]
pub struct PriceForm {
    price_id: String,
    amount: String,
    #[serde(default)]
    valid_from: String,
    #[serde(default)]
    retire_current: String,
}

/// `POST /catalog/{offering_id}/price`.
pub async fn add_price(
    State(state): State<AppState>,
    Path(offering_id): Path<String>,
    Form(form): Form<PriceForm>,
) -> Response {
    let url = format!("/catalog/{offering_id}");
    let valid_from = match parse_dt(&form.valid_from) {
        Ok(v) => v,
        Err(msg) => return back(&url, "", &format!("Catalog rejected the price: {msg}")),
    };
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .catalog
        .admin_add_price(
            &offering_id,
            form.price_id.trim(),
            form.amount.trim(),
            "SGD",
            valid_from.as_deref(),
            None,
            form.retire_current == "yes",
        )
        .await;
    catalog_result(&url, "price", "price_added", r)
}

#[derive(Deserialize)]
pub struct WindowForm {
    #[serde(default)]
    valid_from: String,
    #[serde(default)]
    valid_to: String,
}

/// `POST /catalog/{offering_id}/window`.
pub async fn set_window(
    State(state): State<AppState>,
    Path(offering_id): Path<String>,
    Form(form): Form<WindowForm>,
) -> Response {
    let url = format!("/catalog/{offering_id}");
    let valid_from = match parse_dt(&form.valid_from) {
        Ok(v) => v,
        Err(msg) => return back(&url, "", &format!("Catalog rejected the window: {msg}")),
    };
    let valid_to = match parse_dt(&form.valid_to) {
        Ok(v) => v,
        Err(msg) => return back(&url, "", &format!("Catalog rejected the window: {msg}")),
    };
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients
        .catalog
        .admin_set_offering_window(&offering_id, valid_from.as_deref(), valid_to.as_deref())
        .await;
    catalog_result(&url, "window", "window_set", r)
}

#[derive(Deserialize, Default)]
pub struct ConfirmForm {
    #[serde(default)]
    confirm: String,
}

/// `POST /catalog/{offering_id}/retire` — **confirm-gated**. Note the copy differs
/// from the other screens: the catalog retire uses its own checkbox message.
pub async fn retire_offering(
    State(state): State<AppState>,
    Path(offering_id): Path<String>,
    Form(form): Form<ConfirmForm>,
) -> Response {
    let url = format!("/catalog/{offering_id}");
    if form.confirm != "yes" {
        return back(&url, "", "Check the confirm box to retire this offering.");
    }
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let r = clients.catalog.admin_retire_offering(&offering_id).await;
    catalog_result(&url, "retire", "offering_retired", r)
}

// ── GET /catalog/{offering_id} ───────────────────────────────────────

pub async fn offering_detail(
    State(state): State<AppState>,
    Path(offering_id): Path<String>,
    Query(q): Query<FlashQuery>,
) -> Response {
    let Some(clients) = &state.clients else {
        return (StatusCode::SERVICE_UNAVAILABLE, "clients unavailable").into_response();
    };
    let offering = match clients.catalog.get_offering(&offering_id).await {
        Ok(o) => o,
        Err(ClientError::NotFound(_)) => {
            return (
                StatusCode::NOT_FOUND,
                format!("Offering {offering_id} not found"),
            )
                .into_response()
        }
        Err(e) => {
            tracing::error!(error = %e, "csr.offering.get_failed");
            return (StatusCode::BAD_GATEWAY, "Catalog error").into_response();
        }
    };

    let active_price_id = clients
        .catalog
        .get_active_price(&offering_id)
        .await
        .ok()
        .and_then(|p| p.get("id").and_then(Value::as_str).map(str::to_string))
        .unwrap_or_default();

    let prices: Vec<Value> = offering
        .get("productOfferingPrice")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|p| {
            let tia = p
                .get("price")
                .and_then(|pr| pr.get("taxIncludedAmount"))
                .cloned()
                .unwrap_or_else(|| json!({}));
            json!({
                "id": p.get("id").and_then(Value::as_str).unwrap_or(""),
                "value": format!(
                    "{} {}",
                    tia.get("unit").and_then(Value::as_str).unwrap_or("SGD"),
                    tia.get("value").map(scalar).unwrap_or_else(|| "?".to_string()),
                ),
                "valid_from": fmt_dt(&field_str(Some(p), &["valid_from"], "")),
                "valid_to": fmt_dt(&field_str(Some(p), &["valid_to"], "")),
            })
        })
        .collect();

    let mut offering_view = plan_view(&offering);
    if let Some(obj) = offering_view.as_object_mut() {
        obj.insert(
            "description".to_string(),
            json!(offering
                .get("description")
                .and_then(Value::as_str)
                .unwrap_or("")),
        );
        obj.insert(
            "valid_from".to_string(),
            json!(fmt_dt(&field_str(Some(&offering), &["valid_from"], ""))),
        );
        obj.insert(
            "valid_to".to_string(),
            json!(fmt_dt(&field_str(Some(&offering), &["valid_to"], ""))),
        );
        obj.insert(
            "is_bundle".to_string(),
            json!(offering
                .get("isBundle")
                .and_then(Value::as_bool)
                .unwrap_or(true)),
        );
    }

    render(
        &state,
        "offering_detail.html",
        minijinja::Value::from_serialize(json!({
            "active_page": "catalog",
            "model": "(env default)",
            "offering": offering_view,
            "prices": prices,
            "active_price_id": active_price_id,
            "flash": q.flash,
            "err": q.err.chars().take(300).collect::<String>(),
        })),
    )
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    /// Captured from the live oracle (`datetime.fromisoformat().isoformat()`).
    #[test]
    fn parse_dt_matches_python_isoformat() {
        assert_eq!(parse_dt("").unwrap(), None);
        assert_eq!(parse_dt("   ").unwrap(), None);
        // datetime-local (no seconds) gains `:00`.
        assert_eq!(
            parse_dt("2026-06-10T00:00").unwrap(),
            Some("2026-06-10T00:00:00".to_string())
        );
        assert_eq!(
            parse_dt("2026-06-10T08:30:15").unwrap(),
            Some("2026-06-10T08:30:15".to_string())
        );
        // Date-only → midnight.
        assert_eq!(
            parse_dt("2026-06-10").unwrap(),
            Some("2026-06-10T00:00:00".to_string())
        );
        // The exact Python ValueError text.
        assert_eq!(
            parse_dt("bad").unwrap_err(),
            "Invalid isoformat string: 'bad'"
        );
    }

    #[test]
    fn scalar_renders_ints_without_dot_zero() {
        assert_eq!(scalar(&json!(24)), "24");
        assert_eq!(scalar(&json!("SGD")), "SGD");
        assert_eq!(scalar(&json!(null)), "");
        assert_eq!(scalar(&json!(1.5)), "1.5");
    }
}
