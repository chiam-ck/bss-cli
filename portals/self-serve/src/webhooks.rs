//! `/webhooks/*` — inbound provider webhooks. Port of the Didit half of
//! `bss_self_serve.routes.webhooks`.
//!
//! `POST /webhooks/didit` is the **trust anchor** for v0.15 KYC: an HMAC-verified
//! body writes/updates a row in `integrations.kyc_webhook_corroboration`, which
//! `DiditKycAdapter.fetch_attestation` (and the CRM `check_attestation_signature`
//! policy) reads to authenticate the forwarded attestation.
//!
//! Doctrine:
//! * The route is **exempt from any perimeter token** — auth is the Didit HMAC
//!   signature only (`bss_webhooks::verify_signature`, scheme `DiditHmac`). It is
//!   on the portal's public allowlist (`security::PUBLIC_PATH_PREFIXES`).
//! * Persist every accepted event into `integrations.webhook_event`, idempotent
//!   on `(provider, event_id)` — provider retries dedupe at the DB.
//! * Tampered signature → 401, never persist. Malformed body → 400. Missing
//!   session id → 400. Unset secret → 401 (loud log). No DB → 200 ack (dev only).

use std::collections::HashMap;

use axum::{
    body::Bytes,
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use bss_webhooks::{verify_signature_default, SignatureScheme};
use sha2::{Digest, Sha256};

use crate::AppState;

const PROVIDER_DIDIT: &str = "didit";

fn json_response(status: StatusCode, body: &str) -> Response {
    (
        status,
        [("content-type", "application/json")],
        body.to_string(),
    )
        .into_response()
}

/// `POST /webhooks/didit` — receive Didit HMAC-signed verification webhooks.
pub async fn webhook_didit(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let secret = &state.settings.kyc_didit_webhook_secret;
    if secret.is_empty() {
        tracing::warn!(
            provider = PROVIDER_DIDIT,
            reason = "webhook_secret_unset",
            "portal_auth.webhook.misconfigured"
        );
        return json_response(
            StatusCode::UNAUTHORIZED,
            r#"{"code":"webhook_secret_unset"}"#,
        );
    }

    // Case-insensitive header map for the signature verifier.
    let hdrs: HashMap<String, String> = headers
        .iter()
        .filter_map(|(k, v)| {
            v.to_str()
                .ok()
                .map(|s| (k.as_str().to_string(), s.to_string()))
        })
        .collect();

    if let Err(e) = verify_signature_default(secret, &body, &hdrs, SignatureScheme::DiditHmac) {
        tracing::warn!(
            provider = PROVIDER_DIDIT,
            reason = %e.code,
            "portal_auth.webhook.signature_invalid"
        );
        return json_response(
            StatusCode::UNAUTHORIZED,
            &format!(r#"{{"code":"{}"}}"#, e.code),
        );
    }

    let payload: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => {
            tracing::warn!(provider = PROVIDER_DIDIT, error = %e, "portal_auth.webhook.malformed_body");
            return json_response(StatusCode::BAD_REQUEST, r#"{"code":"malformed_body"}"#);
        }
    };

    let data = payload.get("data");
    let str_at = |top: &str, nested: &str| -> String {
        payload
            .get(top)
            .and_then(|v| v.as_str())
            .or_else(|| data.and_then(|d| d.get(nested)).and_then(|v| v.as_str()))
            .unwrap_or_default()
            .to_string()
    };

    let session_id = str_at("session_id", "session_id");
    // Didit Webhooks v3.0 discriminator is `webhook_type` (fallbacks for drafts).
    let event_type = payload
        .get("webhook_type")
        .or_else(|| payload.get("type"))
        .or_else(|| payload.get("event"))
        .and_then(|v| v.as_str())
        .unwrap_or("unknown")
        .to_string();
    let decision_status = str_at("status", "status");

    if session_id.is_empty() {
        tracing::warn!(
            provider = PROVIDER_DIDIT,
            event_type = %event_type,
            "portal_auth.webhook.missing_session_id"
        );
        return json_response(StatusCode::BAD_REQUEST, r#"{"code":"missing_session_id"}"#);
    }

    // Per-event id: distinct across the status progression for the same session,
    // so the (provider, event_id) PK dedupes retries but admits progression.
    let event_id = payload
        .get("event_id")
        .or_else(|| payload.get("id"))
        .and_then(|v| v.as_str())
        .map(String::from)
        .or_else(|| {
            headers
                .get("x-didit-event-id")
                .and_then(|v| v.to_str().ok())
                .map(String::from)
        })
        .unwrap_or_else(|| {
            let ts = payload
                .get("timestamp")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            format!("{session_id}:{event_type}:{ts}")
        });

    let body_digest = {
        let mut h = Sha256::new();
        h.update(&body);
        let d = h.finalize();
        let mut hex = String::with_capacity(64);
        for b in d.iter() {
            hex.push_str(&format!("{b:02x}"));
        }
        hex
    };

    let Some(pool) = &state.db else {
        tracing::warn!(
            provider = PROVIDER_DIDIT,
            event_id = %event_id,
            event_type = %event_type,
            "portal_auth.webhook.no_db"
        );
        return json_response(StatusCode::OK, r#"{"received":true,"persisted":false}"#);
    };

    // Idempotent insert into webhook_event (forensic + dedupe).
    let inserted = sqlx::query(
        "INSERT INTO integrations.webhook_event (provider, event_id, event_type, body, signature_valid) \
         VALUES ($1,$2,$3,$4,true) ON CONFLICT (provider, event_id) DO NOTHING",
    )
    .bind(PROVIDER_DIDIT)
    .bind(&event_id)
    .bind(&event_type)
    .bind(&payload)
    .execute(pool)
    .await
    .map(|r| r.rows_affected() > 0)
    .unwrap_or(false);

    // Upsert the corroboration row so the LATEST decision_status lands (the state
    // progresses Not Started → In Progress → Approved across webhooks). Keyed on
    // (provider, provider_session_id).
    if !decision_status.is_empty() {
        let _ = sqlx::query(
            "INSERT INTO integrations.kyc_webhook_corroboration \
             (provider, provider_session_id, webhook_event_provider, webhook_event_id, \
              decision_status, decision_body_digest) \
             VALUES ($1,$2,$1,$3,$4,$5) \
             ON CONFLICT (provider, provider_session_id) DO UPDATE \
             SET decision_status = EXCLUDED.decision_status, \
                 decision_body_digest = EXCLUDED.decision_body_digest, \
                 received_at = now()",
        )
        .bind(PROVIDER_DIDIT)
        .bind(&session_id)
        .bind(&event_id)
        .bind(&decision_status)
        .bind(&body_digest)
        .execute(pool)
        .await;
    }

    if !inserted {
        tracing::info!(
            provider = PROVIDER_DIDIT,
            event_id = %event_id,
            event_type = %event_type,
            "portal_auth.webhook.duplicate"
        );
        return json_response(StatusCode::OK, r#"{"received":true,"deduped":true}"#);
    }

    tracing::info!(
        provider = PROVIDER_DIDIT,
        event_id = %event_id,
        event_type = %event_type,
        session_id = %session_id,
        decision_status = %decision_status,
        "portal_auth.webhook.received"
    );
    json_response(StatusCode::OK, r#"{"received":true}"#)
}
