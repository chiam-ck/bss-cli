//! Admin router factory for clock control.
//!
//! Each service mounts this under `/admin-api/v1/clock` so scenarios can
//! freeze/advance that process's clock. Gated by `BSS_ALLOW_ADMIN_RESET` — the
//! same flag that gates `reset-operational-data`.

use axum::{
    extract::Json,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};
use chrono::{DateTime, NaiveDateTime, TimeZone, Utc};
use serde_json::{json, Value};

use crate::clock;

fn is_allowed() -> bool {
    matches!(
        std::env::var("BSS_ALLOW_ADMIN_RESET")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

/// FastAPI-style error body: `{"detail": {"code": ..., "message": ...}}`.
fn error(status: StatusCode, code: &str, message: &str) -> Response {
    (
        status,
        Json(json!({"detail": {"code": code, "message": message}})),
    )
        .into_response()
}

fn guard() -> Option<Response> {
    if is_allowed() {
        None
    } else {
        Some(error(
            StatusCode::FORBIDDEN,
            "ADMIN_CLOCK_DISABLED",
            "Clock control is gated behind BSS_ALLOW_ADMIN_RESET. Set it to \
             'true' in the service environment (scenario runs and developer \
             REPLs only).",
        ))
    }
}

fn serialise(s: &clock::ClockState) -> Value {
    json!({
        "mode": s.mode.as_str(),
        "now": s.now.to_rfc3339(),
        "offsetSeconds": s.offset_seconds,
        "frozenAt": s.frozen_at.map(|d| d.to_rfc3339()),
    })
}

/// Parse an ISO-8601 instant. A naive (offset-less) value is assumed UTC,
/// mirroring the Python `datetime.fromisoformat` + `tzinfo is None` handling.
fn parse_at(raw: &str) -> Option<DateTime<Utc>> {
    if let Ok(dt) = DateTime::parse_from_rfc3339(raw) {
        return Some(dt.with_timezone(&Utc));
    }
    for fmt in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"] {
        if let Ok(naive) = NaiveDateTime::parse_from_str(raw, fmt) {
            return Some(Utc.from_utc_datetime(&naive));
        }
    }
    // Date-only ("%Y-%m-%d") parses as NaiveDate, not NaiveDateTime — try that.
    if let Ok(date) = chrono::NaiveDate::parse_from_str(raw, "%Y-%m-%d") {
        if let Some(naive) = date.and_hms_opt(0, 0, 0) {
            return Some(Utc.from_utc_datetime(&naive));
        }
    }
    None
}

/// Build the `/clock/*` admin router.
///
/// - `GET  /clock/now`      — public, unguarded (read-only)
/// - `POST /clock/freeze`   — guarded
/// - `POST /clock/unfreeze` — guarded
/// - `POST /clock/advance`  — guarded
pub fn clock_admin_router() -> Router {
    Router::new()
        .route("/clock/now", get(get_now))
        .route("/clock/freeze", post(post_freeze))
        .route("/clock/unfreeze", post(post_unfreeze))
        .route("/clock/advance", post(post_advance))
}

async fn get_now() -> Response {
    Json(serialise(&clock::state())).into_response()
}

async fn post_freeze(payload: Option<Json<Value>>) -> Response {
    if let Some(resp) = guard() {
        return resp;
    }
    let body = payload.map(|Json(v)| v).unwrap_or_else(|| json!({}));
    let at = match body.get("at") {
        None | Some(Value::Null) => None,
        Some(raw) => {
            let raw_str = raw
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| raw.to_string());
            match parse_at(&raw_str) {
                Some(dt) => Some(dt),
                None => {
                    return error(
                        StatusCode::UNPROCESSABLE_ENTITY,
                        "INVALID_AT",
                        &format!("'at' must be ISO-8601, got {raw_str:?}"),
                    )
                }
            }
        }
    };
    clock::freeze(at);
    Json(serialise(&clock::state())).into_response()
}

async fn post_unfreeze() -> Response {
    if let Some(resp) = guard() {
        return resp;
    }
    clock::unfreeze();
    Json(serialise(&clock::state())).into_response()
}

async fn post_advance(payload: Option<Json<Value>>) -> Response {
    if let Some(resp) = guard() {
        return resp;
    }
    let body = payload.map(|Json(v)| v).unwrap_or_else(|| json!({}));
    let duration = match body.get("duration").and_then(Value::as_str) {
        Some(d) => d.to_owned(),
        None => {
            return error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "INVALID_DURATION",
                "'duration' is required (e.g. '30d', '2h').",
            )
        }
    };
    let delta = match clock::parse_duration(&duration) {
        Ok(d) => d,
        Err(e) => {
            return error(
                StatusCode::UNPROCESSABLE_ENTITY,
                "INVALID_DURATION",
                &e.to_string(),
            )
        }
    };
    if let Err(e) = clock::advance(delta) {
        return error(
            StatusCode::UNPROCESSABLE_ENTITY,
            "INVALID_DURATION",
            &e.to_string(),
        );
    }
    Json(serialise(&clock::state())).into_response()
}
