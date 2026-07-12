//! Live smoke — the Phase-2 analogue of the conformance harness, for
//! provisioning-sim. Proves the Rust service against the **live** stack.
//! `#[ignore]` so CI skips it; run with the stack up:
//!
//! ```bash
//! set -a; source ../../.env; set +a          # from rust/services/provisioning-sim
//! cargo test -p provisioning-sim --test live_smoke -- --ignored --nocapture
//! ```
//!
//! Checks (all inert / cleaned up — never touches seeded reference data):
//! 1. the full HTTP stack: health / 401 / task 404 / task list / fault-injection
//!    list (reads seeded rules) / audit read;
//! 2. the worker end-to-end with `mq=None`: an `HLR_PROVISION` task runs to
//!    `completed`, writing the task row + a `provisioning.task.completed` audit
//!    row, both scoped to a unique `serviceId` and deleted afterwards;
//! 3. (container cutover, extra `#[ignore]`) the consumer as the sole drainer of
//!    the live `provisioning.task.created` queue.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use bss_context::RequestCtx;
use bss_events::MqChannel;
use bss_middleware::TokenMap;
use provisioning_sim::config::{normalize_db_url, Settings};
use provisioning_sim::esim::EsimProvider;
use provisioning_sim::state::AppState;
use provisioning_sim::worker::{process_task, TaskRequest};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn token() -> String {
    env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set for the live smoke")
}

async fn live_pool() -> sqlx::PgPool {
    let url =
        normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set for the live smoke"));
    bss_db::connect(&url).await.expect("connect live Postgres")
}

async fn spawn_app() -> (String, sqlx::PgPool) {
    let pool = live_pool().await;
    let state = AppState {
        pool: pool.clone(),
        settings: Settings::from_env(),
        esim: EsimProvider::Sim,
        mq: None,
    };
    let token_map = Arc::new(TokenMap::single(&token()));
    let app = provisioning_sim::create_app(state, token_map);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    (format!("http://{addr}"), pool)
}

/// Delete the task row + its audit rows for a scoped serviceId (cleanup).
async fn cleanup(pool: &sqlx::PgPool, service_id: &str) {
    let ids: Vec<String> =
        sqlx::query_scalar("SELECT id FROM provisioning.provisioning_task WHERE service_id = $1")
            .bind(service_id)
            .fetch_all(pool)
            .await
            .unwrap();
    for id in &ids {
        let _ = sqlx::query("DELETE FROM audit.domain_event WHERE aggregate_id = $1")
            .bind(id)
            .execute(pool)
            .await;
    }
    let _ = sqlx::query("DELETE FROM provisioning.provisioning_task WHERE service_id = $1")
        .bind(service_id)
        .execute(pool)
        .await;
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn http_surface_end_to_end() {
    let (base, _pool) = spawn_app().await;
    let http = reqwest::Client::new();
    let tok = token();

    // /health — exempt.
    let r = http.get(format!("{base}/health")).send().await.unwrap();
    assert_eq!(r.status(), 200);
    assert_eq!(
        r.json::<Value>().await.unwrap()["service"],
        "provisioning-sim"
    );

    // task list without token → 401.
    let r = http
        .get(format!("{base}/provisioning-api/v1/task"))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 401);

    // unknown task → 404.
    let r = http
        .get(format!("{base}/provisioning-api/v1/task/PTK-999999"))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 404);

    // task list with token → 200 array.
    let r = http
        .get(format!("{base}/provisioning-api/v1/task"))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    assert!(r.json::<Value>().await.unwrap().is_array());

    // fault-injection list → 200 array (reads the seeded rules).
    let r = http
        .get(format!("{base}/provisioning-api/v1/fault-injection"))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    assert!(r.json::<Value>().await.unwrap().is_array());

    // audit read.
    let r = http
        .get(format!("{base}/audit-api/v1/events?limit=1"))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    assert!(r.json::<Value>().await.unwrap().get("events").is_some());

    println!("[ok] HTTP surface (health/401/404/task-list/fault-list/audit) against live infra");
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn worker_completes_task_inert() {
    let pool = live_pool().await;
    let service_id = format!("SVC-SMOKE-{}", chrono::Utc::now().timestamp_millis());

    // mq=None → stage the audit row without publishing (published_to_mq=false).
    process_task(
        &pool,
        None,
        EsimProvider::Sim,
        &RequestCtx::default(),
        TaskRequest {
            service_id: service_id.clone(),
            service_order_id: "SO-SMOKE".into(),
            commercial_order_id: "ORD-SMOKE".into(),
            task_type: "HLR_PROVISION".into(),
            payload: json!({ "msisdn": "90000042" }),
        },
    )
    .await
    .expect("process_task (inert)");

    // The task row reached `completed`.
    let (task_id, state): (String, String) = sqlx::query_as(
        "SELECT id, state FROM provisioning.provisioning_task WHERE service_id = $1",
    )
    .bind(&service_id)
    .fetch_one(&pool)
    .await
    .expect("task row exists");
    assert_eq!(state, "completed");

    // A provisioning.task.completed audit row was staged for it.
    let (event_type, published, payload): (String, bool, Value) = sqlx::query_as(
        "SELECT event_type, published_to_mq, payload FROM audit.domain_event \
         WHERE aggregate_id = $1",
    )
    .bind(&task_id)
    .fetch_one(&pool)
    .await
    .expect("audit row exists");
    assert_eq!(event_type, "provisioning.task.completed");
    assert!(!published); // mq=None
    assert_eq!(payload["serviceOrderId"], "SO-SMOKE");
    assert_eq!(payload["taskType"], "HLR_PROVISION");
    assert!(payload.get("completedAt").is_some());

    cleanup(&pool, &service_id).await;
    println!(
        "[ok] worker completed HLR_PROVISION → provisioning.task.completed (inert: {task_id})"
    );
}

#[tokio::test]
#[ignore = "container cutover: run ONLY with the Python provisioning-sim container stopped"]
async fn consumer_cutover_end_to_end() {
    // The Rust worker, as the sole consumer of `provisioning.task.created` on the
    // LIVE broker, turns a task.created into a `provisioning.task.completed` (row
    // + published to MQ). Prereq: `docker stop bss-cli-provisioning-sim-1`.
    let pool = live_pool().await;
    let mq_url = env("BSS_MQ_URL").expect("BSS_MQ_URL must be set");
    let mq = Arc::new(MqChannel::connect(&mq_url).await.expect("connect broker"));

    let consumer = tokio::spawn(provisioning_sim::consumer::run(
        mq.clone(),
        pool.clone(),
        EsimProvider::Sim,
    ));
    tokio::time::sleep(Duration::from_millis(700)).await;

    let service_id = format!("SVC-CUT-{}", chrono::Utc::now().timestamp_millis());
    let created = json!({
        "serviceId": service_id,
        "serviceOrderId": "SO-CUT",
        "commercialOrderId": "ORD-CUT",
        "taskType": "HLR_PROVISION",
        "payload": { "msisdn": "90000042" },
    });
    mq.publish_json("provisioning.task.created", &created)
        .await
        .expect("publish task.created");

    // Poll for the completed task.
    let mut found: Option<(String, bool)> = None;
    for _ in 0..40 {
        let row = sqlx::query_as::<_, (String, bool)>(
            "SELECT t.id, e.published_to_mq FROM provisioning.provisioning_task t \
             JOIN audit.domain_event e ON e.aggregate_id = t.id \
             WHERE t.service_id = $1 AND t.state = 'completed' \
               AND e.event_type = 'provisioning.task.completed'",
        )
        .bind(&service_id)
        .fetch_optional(&pool)
        .await
        .expect("query");
        if let Some(r) = row {
            found = Some(r);
            break;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }

    cleanup(&pool, &service_id).await;
    consumer.abort();

    let (task_id, published) = found.expect("Rust worker did not complete the task within 10s");
    assert!(
        published,
        "provisioning.task.completed should be published_to_mq=true"
    );
    println!("[ok] consumer cutover: task.created → Rust worker → provisioning.task.completed ({task_id})");
}
