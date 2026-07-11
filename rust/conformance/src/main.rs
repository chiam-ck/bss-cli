//! Phase 0 conformance harness — wires the platform crates against the **live**
//! stack (the same Postgres/RabbitMQ/Jaeger the Python services use) and checks
//! the Phase-0 exit criteria. Run manually:
//!
//! ```bash
//! set -a; source ../.env; set +a
//! cargo run -p conformance
//! ```
//!
//! It never runs in CI (CI has no infra). The one write it does is an inert
//! `conformance.ping` audit row — no consumer binds that key — which it deletes
//! after confirming the Python relay published it.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use axum::{middleware::from_fn, middleware::from_fn_with_state, routing::get, Extension, Router};
use bss_context::{propagate_context, RequestCtx};
use bss_middleware::{require_api_token, TokenMap};
use sqlx::Row;
use uuid::Uuid;

type Err = Box<dyn std::error::Error>;

#[tokio::main]
async fn main() -> Result<(), Err> {
    println!("── Phase 0 conformance (live stack) ──\n");
    let mut failures = 0;

    let db_url = std::env::var("BSS_DB_URL")
        .map_err(|_| "BSS_DB_URL not set (run: set -a; source ../.env; set +a)")?;
    // sqlx speaks plain postgres:// — drop the SQLAlchemy async dialect suffix.
    let db_url = db_url
        .replace("postgresql+asyncpg://", "postgres://")
        .replace("postgresql://", "postgres://");
    let token = std::env::var("BSS_API_TOKEN").unwrap_or_default();

    let pool = bss_db::connect(&db_url).await?;

    // ── Check 1: sqlx connectivity ──────────────────────────────────────────
    let version: String = sqlx::query_scalar("SELECT version()")
        .fetch_one(&pool)
        .await?;
    let short = version
        .split_whitespace()
        .take(2)
        .collect::<Vec<_>>()
        .join(" ");
    report(
        &mut failures,
        "sqlx connects to the live Postgres",
        true,
        &short,
    );

    // ── Check 2: audit.domain_event schema matches the Rust mapping ──────────
    let rows = sqlx::query(
        "SELECT column_name FROM information_schema.columns \
         WHERE table_schema = 'audit' AND table_name = 'domain_event'",
    )
    .fetch_all(&pool)
    .await?;
    let cols: Vec<String> = rows
        .iter()
        .map(|r| r.try_get::<String, _>("column_name"))
        .collect::<Result<_, _>>()?;
    let expected = [
        "id",
        "event_id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "occurred_at",
        "trace_id",
        "actor",
        "channel",
        "tenant_id",
        "service_identity",
        "payload",
        "schema_version",
        "published_to_mq",
        "published_attempts",
        "last_publish_error",
    ];
    let missing: Vec<&str> = expected
        .iter()
        .copied()
        .filter(|c| !cols.contains(&c.to_string()))
        .collect();
    report(
        &mut failures,
        "audit.domain_event schema matches bss_events::DomainEvent",
        missing.is_empty(),
        &if missing.is_empty() {
            format!("{} columns present", expected.len())
        } else {
            format!("missing: {missing:?}")
        },
    );

    // ── Check 3: relay interop — Python relay publishes a Rust-written row ────
    let ctx = RequestCtx {
        actor: "conformance".to_string(),
        channel: "system".to_string(),
        service_identity: "default".to_string(),
        ..RequestCtx::default()
    };
    let agg_id = format!("CONF-{}", chrono::Utc::now().timestamp_millis());
    let ev = bss_events::stage_event(
        &ctx,
        "conformance.ping",
        "Conformance",
        agg_id,
        Some(serde_json::json!({"note": "phase0 rust conformance — inert, no consumer bound"})),
    );
    let event_uuid = Uuid::parse_str(&ev.event_id)?;

    sqlx::query(
        "INSERT INTO audit.domain_event \
         (event_id, event_type, aggregate_type, aggregate_id, occurred_at, actor, channel, \
          tenant_id, service_identity, payload, schema_version, published_to_mq) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
    )
    .bind(event_uuid)
    .bind(&ev.event_type)
    .bind(&ev.aggregate_type)
    .bind(&ev.aggregate_id)
    .bind(ev.occurred_at)
    .bind(&ev.actor)
    .bind(&ev.channel)
    .bind(&ev.tenant_id)
    .bind(&ev.service_identity)
    .bind(sqlx::types::Json(ev.payload.clone()))
    .bind(ev.schema_version as i16)
    .bind(ev.published_to_mq)
    .execute(&pool)
    .await?;

    // Poll for the Python relay (250ms tick) to flip published_to_mq.
    let mut published = false;
    for _ in 0..40 {
        let flag: Option<bool> = sqlx::query_scalar(
            "SELECT published_to_mq FROM audit.domain_event WHERE event_id = $1",
        )
        .bind(event_uuid)
        .fetch_optional(&pool)
        .await?;
        if flag == Some(true) {
            published = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    // Clean up the inert row regardless of outcome.
    let deleted = sqlx::query("DELETE FROM audit.domain_event WHERE event_id = $1")
        .bind(event_uuid)
        .execute(&pool)
        .await?
        .rows_affected();
    report(
        &mut failures,
        "Python relay published a Rust-written audit row (cross-language outbox)",
        published,
        &if published {
            format!("published within poll window; cleaned up ({deleted} row)")
        } else {
            "not published within 10s — is the stack's relay running?".to_string()
        },
    );

    // ── Check 4: token middleware over real HTTP, using the live token ───────
    if token.is_empty() {
        report(
            &mut failures,
            "token middleware end-to-end",
            false,
            "BSS_API_TOKEN not set — skipped",
        );
    } else {
        let base = spawn_guarded_app(&token).await?;
        let http = reqwest::Client::new();
        let health = http.get(format!("{base}/health")).send().await?.status();
        let no_tok = http.get(format!("{base}/protected")).send().await?.status();
        let good = http
            .get(format!("{base}/protected"))
            .header("X-BSS-API-Token", &token)
            .send()
            .await?;
        let good_status = good.status();
        let identity = good.text().await?;
        let ok = health == 200 && no_tok == 401 && good_status == 200 && identity == "default";
        report(
            &mut failures,
            "token middleware end-to-end (health 200 / no-token 401 / live-token 200)",
            ok,
            &format!(
                "health={health} no_token={no_tok} with_token={good_status} identity={identity:?}"
            ),
        );
    }

    println!(
        "\n── OTel trace → Jaeger: deferred (tracing/otel bootstrap is the last Phase-0 item) ──"
    );
    println!(
        "\n{}",
        if failures == 0 {
            "ALL CHECKS PASSED ✓"
        } else {
            "SOME CHECKS FAILED ✗"
        }
    );
    if failures > 0 {
        std::process::exit(1);
    }
    Ok(())
}

fn report(failures: &mut u32, name: &str, ok: bool, detail: &str) {
    if ok {
        println!("[PASS] {name}\n       {detail}");
    } else {
        *failures += 1;
        println!("[FAIL] {name}\n       {detail}");
    }
}

/// Spawn the platform middleware stack (token gate + context) on a loopback port.
async fn spawn_guarded_app(token: &str) -> Result<String, Err> {
    let map = Arc::new(TokenMap::single(token));
    let app = Router::new()
        .route("/health", get(|| async { "ok" }))
        .route(
            "/protected",
            get(|Extension(ctx): Extension<RequestCtx>| async move { ctx.service_identity }),
        )
        .layer(from_fn(propagate_context))
        .layer(from_fn_with_state(map, require_api_token));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let addr: SocketAddr = listener.local_addr()?;
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    Ok(format!("http://{addr}"))
}
