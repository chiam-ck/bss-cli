//! Live smoke — the Phase-2 analogue of the conformance harness, for SOM.
//! `#[ignore]` so CI skips it; run with the stack up:
//!
//! ```bash
//! set -a; source ../../.env; set +a          # from rust/services/som
//! cargo test -p som --test live_smoke -- --ignored --nocapture
//! ```
//!
//! Checks (all inert / cleaned up):
//! 1. the HTTP stack: health / 401 / serviceOrder 404 / list / audit read;
//! 2. the **outbox relay** end-to-end: stage an inert `audit.domain_event`
//!    (`published_to_mq = false`), run `drain_once`, and confirm the row flips to
//!    published (the deferred P2 lapin/sqlx tick loop, against the live broker).
//!    The full consumer/decompose chain is exercised by the hero scenarios.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{InventoryClient, TokenAuthProvider};
use bss_events::MqChannel;
use bss_middleware::TokenMap;
use serde_json::{json, Value};
use som::config::{normalize_db_url, Settings};
use som::state::AppState;

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn token() -> String {
    env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set")
}

async fn live_pool() -> sqlx::PgPool {
    let url = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    bss_db::connect(&url).await.expect("connect live Postgres")
}

fn crm_url() -> String {
    env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string())
}

async fn spawn_app() -> (String, sqlx::PgPool) {
    let pool = live_pool().await;
    let auth = Arc::new(TokenAuthProvider::new(token()).unwrap());
    let state = AppState {
        pool: pool.clone(),
        inventory: InventoryClient::new(crm_url(), auth).unwrap(),
        settings: Settings::from_env(),
    };
    let token_map = Arc::new(TokenMap::single(&token()));
    let app = som::create_app(state, token_map);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    (format!("http://{addr}"), pool)
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn http_surface_end_to_end() {
    let (base, _pool) = spawn_app().await;
    let http = reqwest::Client::new();
    let tok = token();

    let r = http.get(format!("{base}/health")).send().await.unwrap();
    assert_eq!(r.status(), 200);
    assert_eq!(r.json::<Value>().await.unwrap()["service"], "som");

    // ServiceOrder read without token → 401.
    let r = http
        .get(format!(
            "{base}/tmf-api/serviceOrderingManagement/v4/serviceOrder/SO-9999"
        ))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 401);

    // Unknown ServiceOrder → 404.
    let r = http
        .get(format!(
            "{base}/tmf-api/serviceOrderingManagement/v4/serviceOrder/SO-999999"
        ))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 404);

    // List by commercial order → 200 array.
    let r = http
        .get(format!(
            "{base}/tmf-api/serviceOrderingManagement/v4/serviceOrder?commercialOrderId=ORD-NONE"
        ))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    assert!(r.json::<Value>().await.unwrap().is_array());

    // Audit read.
    let r = http
        .get(format!("{base}/audit-api/v1/events?limit=1"))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    assert!(r.json::<Value>().await.unwrap().get("events").is_some());

    println!("[ok] HTTP surface (health/401/404/list/audit) against live infra");
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn relay_drains_staged_row() {
    let pool = live_pool().await;
    let mq_url = env("BSS_MQ_URL").expect("BSS_MQ_URL must be set");
    let mq = Arc::new(MqChannel::connect(&mq_url).await.expect("connect broker"));

    // Stage an inert unpublished event (routing key with no consumer bound → the
    // broker drops the published message; we only assert the row flips).
    let agg = format!("SOMRELAY-{}", chrono::Utc::now().timestamp_millis());
    let event_id = uuid::Uuid::new_v4();
    sqlx::query(
        "INSERT INTO audit.domain_event \
         (event_id, event_type, aggregate_type, aggregate_id, occurred_at, actor, channel, \
          tenant_id, service_identity, payload, schema_version, published_to_mq) \
         VALUES ($1,'conformance.ping','conformance',$2, now(),'system','system','DEFAULT','default',$3,1,false)",
    )
    .bind(event_id)
    .bind(&agg)
    .bind(sqlx::types::Json(json!({ "probe": agg })))
    .execute(&pool)
    .await
    .expect("stage inert event");

    // Drive the relay drain directly. Other (Python) relays may race via SKIP
    // LOCKED, so retry a few ticks; the invariant we assert is that the row ends
    // up published (the relay pipeline delivered it).
    for _ in 0..20 {
        let _ = bss_events::drain_once(&pool, &mq, 100).await;
        let published: Option<bool> = sqlx::query_scalar(
            "SELECT published_to_mq FROM audit.domain_event WHERE event_id = $1",
        )
        .bind(event_id)
        .fetch_optional(&pool)
        .await
        .unwrap();
        if published == Some(true) {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }

    let published: bool =
        sqlx::query_scalar("SELECT published_to_mq FROM audit.domain_event WHERE event_id = $1")
            .bind(event_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    // Clean up before asserting.
    let _ = sqlx::query("DELETE FROM audit.domain_event WHERE event_id = $1")
        .bind(event_id)
        .execute(&pool)
        .await;

    assert!(published, "relay should have flipped published_to_mq=true");
    println!("[ok] outbox relay drained a staged row → published ({agg})");
}
