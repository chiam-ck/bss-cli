//! `POST /webhooks/stripe` — port of `app.api.webhooks`.
//!
//! Exempt from the token perimeter (the middleware exempts `/webhooks/`); auth is
//! the Stripe signature only. Webhook is the **secondary** source of truth: a
//! terminal event that contradicts the row emits `payment.attempt_state_drift`
//! (never overwrites — the synchronous charge response wins). Refunds/disputes
//! are **record-only** (motto #1). Every accepted event persists to
//! `integrations.webhook_event` idempotently on `(provider, event_id)`.

use axum::{
    body::Bytes,
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Extension, Json,
};
use bss_context::RequestCtx;
use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::Sha256;
use sqlx::PgConnection;

use crate::events::stage;
use crate::repo::get_attempt_by_provider_call_id;
use crate::state::AppState;

const PROVIDER: &str = "stripe";
const MAX_SKEW_SECONDS: i64 = 300;

const TERMINAL_CHARGE_EVENTS: [&str; 3] = [
    "charge.succeeded",
    "charge.failed",
    "payment_intent.payment_failed",
];
const REFUND_EVENT: &str = "charge.refunded";
const DISPUTE_EVENT: &str = "charge.dispute.created";

// ── signature verification (port of bss_webhooks _verify_stripe) ─────

/// A `(code, message)` signature failure, mirroring `WebhookSignatureError`.
pub struct SigError {
    pub code: &'static str,
    pub message: String,
}

fn to_hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// Port of `_verify_stripe` + `_check_timestamp`. Signed payload is
/// `f"{t}.{body}"`, HMAC-SHA256 hex, any `v1=` entry matches; timestamp must be
/// within `MAX_SKEW_SECONDS`.
pub fn verify_stripe_signature(
    secret: &str,
    body: &[u8],
    sig_header: Option<&str>,
    now_unix: i64,
) -> Result<(), SigError> {
    let header = sig_header.ok_or(SigError {
        code: "missing_header",
        message: "Stripe-Signature header required".into(),
    })?;

    let mut timestamp: Option<i64> = None;
    let mut candidates: Vec<String> = Vec::new();
    for part in header.split(',') {
        let Some((k, v)) = part.split_once('=') else {
            continue;
        };
        match k.trim() {
            "t" => timestamp = v.trim().parse::<i64>().ok(),
            "v1" => candidates.push(v.trim().to_string()),
            _ => {}
        }
    }

    let ts = timestamp.ok_or(SigError {
        code: "malformed_header",
        message: "Stripe-Signature missing 't=' field".into(),
    })?;
    if candidates.is_empty() {
        return Err(SigError {
            code: "malformed_header",
            message: "Stripe-Signature missing v1 entries".into(),
        });
    }

    // _check_timestamp: values > 1e12 are millis; ours are seconds.
    let ts_seconds = if ts > 1_000_000_000_000 {
        ts / 1000
    } else {
        ts
    };
    if (now_unix - ts_seconds).abs() > MAX_SKEW_SECONDS {
        return Err(SigError {
            code: "replay_window",
            message: format!(
                "timestamp skew {}s exceeds {MAX_SKEW_SECONDS}s",
                (now_unix - ts_seconds).abs()
            ),
        });
    }

    let mut mac = <Hmac<Sha256>>::new_from_slice(secret.as_bytes()).map_err(|_| SigError {
        code: "malformed_header",
        message: "bad secret".into(),
    })?;
    let mut signed = format!("{ts}.").into_bytes();
    signed.extend_from_slice(body);
    mac.update(&signed);
    let expected_hex = to_hex(&mac.finalize().into_bytes());

    let matched = candidates.iter().any(|cand| {
        use subtle::ConstantTimeEq;
        cand.len() == expected_hex.len() && cand.as_bytes().ct_eq(expected_hex.as_bytes()).into()
    });
    if matched {
        Ok(())
    } else {
        Err(SigError {
            code: "signature_mismatch",
            message: "no v1 signature matched".into(),
        })
    }
}

// ── diagnostic logging helpers (Track-3 day-1 requirement) ───────────

fn candidate_headers(headers: &HeaderMap) -> Value {
    let mut m = serde_json::Map::new();
    for (k, v) in headers {
        let lk = k.as_str().to_lowercase();
        let val = if lk.contains("secret") || lk.contains("token") || lk.contains("authorization") {
            "[redacted]".to_string()
        } else {
            let s = v.to_str().unwrap_or("<binary>");
            if s.len() > 80 {
                format!("{}…", &s[..80])
            } else {
                s.to_string()
            }
        };
        m.insert(k.as_str().to_string(), Value::String(val));
    }
    Value::Object(m)
}

fn body_preview(body: &[u8]) -> String {
    let s = String::from_utf8_lossy(body);
    if s.len() > 500 {
        format!("{}…", &s[..500])
    } else {
        s.to_string()
    }
}

fn json_response(status: StatusCode, body: Value) -> Response {
    (status, Json(body)).into_response()
}

// ── handler ──────────────────────────────────────────────────────────

pub async fn webhook_stripe(
    State(state): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let secret = &state.settings.payment_stripe_webhook_secret;
    if secret.is_empty() {
        tracing::warn!(
            provider = PROVIDER,
            reason = "webhook_secret_unset",
            "payment.webhook.misconfigured"
        );
        return json_response(
            StatusCode::UNAUTHORIZED,
            json!({ "code": "webhook_secret_unset" }),
        );
    }

    let sig_header = headers
        .get("stripe-signature")
        .and_then(|v| v.to_str().ok());
    let now_unix = bss_clock::now().timestamp();
    if let Err(e) = verify_stripe_signature(secret, &body, sig_header, now_unix) {
        tracing::warn!(
            provider = PROVIDER,
            reason = e.code,
            detail = e.message,
            candidate_headers = %candidate_headers(&headers),
            body_preview = %body_preview(&body),
            "payment.webhook.signature_invalid"
        );
        return json_response(StatusCode::UNAUTHORIZED, json!({ "code": e.code }));
    }

    let payload: Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!(provider = PROVIDER, error = %e, body_preview = %body_preview(&body), "payment.webhook.malformed_body");
            return json_response(StatusCode::BAD_REQUEST, json!({ "code": "malformed_body" }));
        }
    };

    let event_id = payload.get("id").and_then(Value::as_str);
    let event_type = payload
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let data_object = payload
        .get("data")
        .and_then(|d| d.get("object"))
        .cloned()
        .unwrap_or(Value::Null);

    let Some(event_id) = event_id else {
        tracing::warn!(
            provider = PROVIDER,
            event_type,
            "payment.webhook.missing_event_id"
        );
        return json_response(
            StatusCode::BAD_REQUEST,
            json!({ "code": "missing_event_id" }),
        );
    };

    let mut tx = match state.pool.begin().await {
        Ok(t) => t,
        Err(e) => {
            return json_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({ "detail": e.to_string() }),
            )
        }
    };

    let inserted = match persist_event(&mut tx, event_id, event_type, &payload).await {
        Ok(b) => b,
        Err(e) => {
            return json_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({ "detail": e.to_string() }),
            )
        }
    };

    if !inserted {
        let _ = tx.commit().await;
        tracing::info!(
            provider = PROVIDER,
            event_id,
            event_type,
            "payment.webhook.duplicate"
        );
        return json_response(StatusCode::OK, json!({ "received": true, "deduped": true }));
    }

    let (outcome, domain_event): (&str, Option<&str>) =
        if TERMINAL_CHARGE_EVENTS.contains(&event_type) {
            match route_terminal_charge(&mut tx, &ctx, event_type, &data_object).await {
                Ok(r) => r,
                Err(e) => {
                    return json_response(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        json!({ "detail": e.to_string() }),
                    )
                }
            }
        } else if event_type == REFUND_EVENT {
            match route_refund(&mut tx, &ctx, &data_object).await {
                Ok(r) => r,
                Err(e) => {
                    return json_response(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        json!({ "detail": e.to_string() }),
                    )
                }
            }
        } else if event_type == DISPUTE_EVENT {
            match route_dispute(&mut tx, &ctx, &data_object).await {
                Ok(r) => r,
                Err(e) => {
                    return json_response(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        json!({ "detail": e.to_string() }),
                    )
                }
            }
        } else {
            ("noop", None)
        };

    if let Err(e) = mark_processed(&mut tx, event_id, outcome).await {
        return json_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "detail": e.to_string() }),
        );
    }
    if let Err(e) = tx.commit().await {
        return json_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "detail": e.to_string() }),
        );
    }

    tracing::info!(
        provider = PROVIDER,
        event_id,
        event_type,
        outcome,
        domain_event,
        "payment.webhook.received"
    );
    json_response(StatusCode::OK, json!({ "received": true }))
}

// ── WebhookEventStore (port of bss_webhooks.store) ───────────────────

async fn persist_event(
    conn: &mut PgConnection,
    event_id: &str,
    event_type: &str,
    body: &Value,
) -> Result<bool, sqlx::Error> {
    let res = sqlx::query(
        "INSERT INTO integrations.webhook_event (provider, event_id, event_type, body, signature_valid) \
         VALUES ($1,$2,$3,$4,true) ON CONFLICT (provider, event_id) DO NOTHING",
    )
    .bind(PROVIDER)
    .bind(event_id)
    .bind(event_type)
    .bind(sqlx::types::Json(body.clone()))
    .execute(conn)
    .await?;
    Ok(res.rows_affected() == 1)
}

async fn mark_processed(
    conn: &mut PgConnection,
    event_id: &str,
    outcome: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE integrations.webhook_event SET process_outcome = $3, processed_at = $4 \
         WHERE provider = $1 AND event_id = $2",
    )
    .bind(PROVIDER)
    .bind(event_id)
    .bind(outcome)
    .bind(bss_clock::now())
    .execute(conn)
    .await?;
    Ok(())
}

// ── routers ──────────────────────────────────────────────────────────

async fn route_terminal_charge(
    conn: &mut PgConnection,
    ctx: &RequestCtx,
    event_type: &str,
    data_object: &Value,
) -> Result<(&'static str, Option<&'static str>), sqlx::Error> {
    let pi_id = data_object
        .get("payment_intent")
        .and_then(Value::as_str)
        .or_else(|| data_object.get("id").and_then(Value::as_str));
    let Some(pi_id) = pi_id else {
        return Ok(("noop", None));
    };
    let expected_status = if event_type == "charge.succeeded" {
        "approved"
    } else {
        "declined"
    };

    let attempt = get_attempt_by_provider_call_id(conn, pi_id)
        .await
        .map_err(sqlx_from_api)?;
    let Some((attempt_id, row_status)) = attempt else {
        return Ok(("noop", None));
    };
    if row_status == expected_status {
        return Ok(("reconciled", None));
    }
    // Drift — emit an ops event; never overwrite the synchronous truth.
    stage(
        conn,
        ctx,
        "payment.attempt_state_drift",
        "payment_attempt",
        &attempt_id,
        json!({
            "row_status": row_status,
            "webhook_status": expected_status,
            "stripe_event_type": event_type,
            "provider_call_id": pi_id,
        }),
    )
    .await?;
    Ok(("drift", Some("payment.attempt_state_drift")))
}

async fn route_refund(
    conn: &mut PgConnection,
    ctx: &RequestCtx,
    data_object: &Value,
) -> Result<(&'static str, Option<&'static str>), sqlx::Error> {
    let pi_id = data_object
        .get("payment_intent")
        .and_then(Value::as_str)
        .or_else(|| data_object.get("id").and_then(Value::as_str));
    let Some(pi_id) = pi_id else {
        return Ok(("noop", None));
    };
    let amount_refunded = data_object
        .get("amount_refunded")
        .cloned()
        .unwrap_or(json!(0));

    let attempt_id = get_attempt_by_provider_call_id(conn, pi_id)
        .await
        .map_err(sqlx_from_api)?
        .map(|(id, _)| id);

    let reason = data_object
        .get("refunds")
        .and_then(|r| r.get("data"))
        .and_then(Value::as_array)
        .and_then(|a| a.first())
        .and_then(|r| r.get("reason").cloned())
        .unwrap_or(Value::Null);

    stage(
        conn,
        ctx,
        "payment.refunded",
        "payment_attempt",
        attempt_id.as_deref().unwrap_or(pi_id),
        json!({
            "provider_call_id": pi_id,
            "amount_refunded_minor": amount_refunded,
            "currency": data_object.get("currency"),
            "reason": reason,
        }),
    )
    .await?;
    Ok(("reconciled", Some("payment.refunded")))
}

async fn route_dispute(
    conn: &mut PgConnection,
    ctx: &RequestCtx,
    data_object: &Value,
) -> Result<(&'static str, Option<&'static str>), sqlx::Error> {
    let charge_id = data_object.get("charge").and_then(Value::as_str);
    let pi_id = data_object.get("payment_intent").and_then(Value::as_str);
    let dispute_id = data_object.get("id").and_then(Value::as_str);

    let attempt_id = match pi_id {
        Some(pi) => get_attempt_by_provider_call_id(conn, pi)
            .await
            .map_err(sqlx_from_api)?
            .map(|(id, _)| id),
        None => None,
    };
    let aggregate_id = attempt_id
        .as_deref()
        .or(dispute_id)
        .or(charge_id)
        .unwrap_or("unknown");

    stage(
        conn,
        ctx,
        "payment.dispute_opened",
        "payment_attempt",
        aggregate_id,
        json!({
            "stripe_dispute_id": dispute_id,
            "stripe_charge_id": charge_id,
            "provider_call_id": pi_id,
            "amount_minor": data_object.get("amount"),
            "currency": data_object.get("currency"),
            "reason": data_object.get("reason"),
            "status": data_object.get("status"),
        }),
    )
    .await?;
    Ok(("reconciled", Some("payment.dispute_opened")))
}

/// The repo helpers return `ApiError`; inside the webhook we're already in an
/// `sqlx::Error` result — collapse an `ApiError::Internal` back to a DB error
/// string (only DB faults reach here in practice).
fn sqlx_from_api(e: crate::error::ApiError) -> sqlx::Error {
    match e {
        crate::error::ApiError::Internal(m) => sqlx::Error::Protocol(m),
        other => sqlx::Error::Protocol(format!("{other:?}")),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn sign(secret: &str, ts: i64, body: &[u8]) -> String {
        let mut mac = <Hmac<Sha256>>::new_from_slice(secret.as_bytes()).unwrap();
        let mut signed = format!("{ts}.").into_bytes();
        signed.extend_from_slice(body);
        mac.update(&signed);
        to_hex(&mac.finalize().into_bytes())
    }

    #[test]
    fn valid_signature_accepts() {
        let body = br#"{"id":"evt_1","type":"charge.succeeded"}"#;
        let ts = 1_700_000_000_i64;
        let sig = sign("whsec_test", ts, body);
        let header = format!("t={ts},v1={sig}");
        assert!(verify_stripe_signature("whsec_test", body, Some(&header), ts).is_ok());
    }

    #[test]
    fn tampered_body_rejected() {
        let body = br#"{"id":"evt_1"}"#;
        let ts = 1_700_000_000_i64;
        let sig = sign("whsec_test", ts, body);
        let header = format!("t={ts},v1={sig}");
        let err = verify_stripe_signature("whsec_test", b"{}", Some(&header), ts).unwrap_err();
        assert_eq!(err.code, "signature_mismatch");
    }

    #[test]
    fn stale_timestamp_rejected() {
        let body = br#"{}"#;
        let ts = 1_700_000_000_i64;
        let sig = sign("whsec_test", ts, body);
        let header = format!("t={ts},v1={sig}");
        // now is 10 minutes later → beyond the 300s window.
        let err = verify_stripe_signature("whsec_test", body, Some(&header), ts + 600).unwrap_err();
        assert_eq!(err.code, "replay_window");
    }

    #[test]
    fn missing_header_rejected() {
        let err = verify_stripe_signature("whsec_test", b"{}", None, 0).unwrap_err();
        assert_eq!(err.code, "missing_header");
    }

    #[test]
    fn missing_v1_rejected() {
        let err = verify_stripe_signature("whsec_test", b"{}", Some("t=123"), 123).unwrap_err();
        assert_eq!(err.code, "malformed_header");
    }
}
