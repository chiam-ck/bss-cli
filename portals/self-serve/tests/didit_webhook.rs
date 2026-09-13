#![allow(clippy::unwrap_used, clippy::expect_used)]
use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use bss_self_serve::{build_router, build_state};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::sync::Arc;
use tower::ServiceExt;

fn request(payload: &serde_json::Value, tamper: bool) -> Request<Body> {
    let body = serde_json::to_vec(payload).unwrap();
    let mut mac = Hmac::<Sha256>::new_from_slice(b"didit-test-secret").unwrap();
    mac.update(&body);
    let sig: String = mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    Request::builder()
        .method("POST")
        .uri("/webhooks/didit")
        .header("x-signature", sig)
        .header("x-timestamp", chrono::Utc::now().timestamp().to_string())
        .body(Body::from(if tamper { b"{}".to_vec() } else { body }))
        .unwrap()
}

#[tokio::test]
async fn didit_rejects_tampering_and_retries_without_database() {
    let mut state = build_state();
    state.db = None;
    Arc::make_mut(&mut state.settings).kyc_didit_webhook_secret = "didit-test-secret".into();
    let payload = serde_json::json!({"event_id":"test", "session_id":"test", "status":"Approved", "webhook_type":"status.updated"});
    for (tamper, expected) in [
        (true, StatusCode::UNAUTHORIZED),
        (false, StatusCode::SERVICE_UNAVAILABLE),
    ] {
        let response = build_router(state.clone())
            .oneshot(request(&payload, tamper))
            .await
            .unwrap();
        assert_eq!(response.status(), expected);
    }
}

#[tokio::test]
#[ignore = "requires BSS_TEST_DB_URL pointing to migrated local development database"]
async fn didit_persists_envelope_and_duplicate_does_not_roll_back_status() {
    let pool = bss_db::connect(&std::env::var("BSS_TEST_DB_URL").unwrap())
        .await
        .unwrap();
    let mut state = build_state();
    state.db = Some(pool.clone());
    Arc::make_mut(&mut state.settings).kyc_didit_webhook_secret = "didit-test-secret".into();
    let session = format!("setup-test-{}", uuid::Uuid::new_v4());
    let early = serde_json::json!({"event_id":format!("{session}-1"), "session_id":session, "status":"In Progress", "webhook_type":"status.updated", "decision":{"full_name":"PRIVATE NAME", "image":"PRIVATE IMAGE"}});
    let mut approved = early.clone();
    approved["event_id"] = format!("{session}-2").into();
    approved["status"] = "Approved".into();
    for payload in [&early, &approved, &early] {
        assert_eq!(
            build_router(state.clone())
                .oneshot(request(payload, false))
                .await
                .unwrap()
                .status(),
            StatusCode::OK
        );
    }
    let row: (String, String) = sqlx::query_as("SELECT decision_status, webhook_event_id FROM integrations.kyc_webhook_corroboration WHERE provider='didit' AND provider_session_id=$1").bind(&session).fetch_one(&pool).await.unwrap();
    assert_eq!(row, ("Approved".into(), format!("{session}-2")));
    let stored: serde_json::Value = sqlx::query_scalar(
        "SELECT body FROM integrations.webhook_event WHERE provider='didit' AND event_id=$1",
    )
    .bind(format!("{session}-2"))
    .fetch_one(&pool)
    .await
    .unwrap();
    assert!(stored.get("decision").is_none());
    assert!(!stored.to_string().contains("PRIVATE"));
    sqlx::query("DELETE FROM integrations.kyc_webhook_corroboration WHERE provider='didit' AND provider_session_id=$1").bind(&session).execute(&pool).await.unwrap();
    for suffix in [1, 2] {
        sqlx::query(
            "DELETE FROM integrations.webhook_event WHERE provider='didit' AND event_id=$1",
        )
        .bind(format!("{session}-{suffix}"))
        .execute(&pool)
        .await
        .unwrap();
    }
}

#[tokio::test]
#[ignore = "requires BSS_TEST_DB_URL pointing to migrated local development database"]
async fn didit_missing_event_id_preserves_progress_and_dedupes_restamped_retry() {
    let pool = bss_db::connect(&std::env::var("BSS_TEST_DB_URL").unwrap())
        .await
        .unwrap();
    let mut state = build_state();
    state.db = Some(pool.clone());
    Arc::make_mut(&mut state.settings).kyc_didit_webhook_secret = "didit-test-secret".into();
    let session = format!("setup-test-{}", uuid::Uuid::new_v4());
    let early = serde_json::json!({"session_id":session, "status":"In Progress", "webhook_type":"status.updated", "timestamp":1000});
    let mut approved = early.clone();
    approved["status"] = "Approved".into();
    approved["timestamp"] = 1001.into();
    let mut retry = early.clone();
    retry["timestamp"] = 1002.into();
    for payload in [&early, &approved, &retry] {
        assert_eq!(
            build_router(state.clone())
                .oneshot(request(payload, false))
                .await
                .unwrap()
                .status(),
            StatusCode::OK
        );
    }
    let status: String = sqlx::query_scalar("SELECT decision_status FROM integrations.kyc_webhook_corroboration WHERE provider='didit' AND provider_session_id=$1").bind(&session).fetch_one(&pool).await.unwrap();
    assert_eq!(status, "Approved");
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM integrations.webhook_event WHERE provider='didit' AND body->>'session_id'=$1").bind(&session).fetch_one(&pool).await.unwrap();
    assert_eq!(count, 2);
    sqlx::query("DELETE FROM integrations.kyc_webhook_corroboration WHERE provider='didit' AND provider_session_id=$1").bind(&session).execute(&pool).await.unwrap();
    sqlx::query(
        "DELETE FROM integrations.webhook_event WHERE provider='didit' AND body->>'session_id'=$1",
    )
    .bind(&session)
    .execute(&pool)
    .await
    .unwrap();
}
