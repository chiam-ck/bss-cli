//! Audit-events query router — port of `bss_events.router.audit_events_router`.
//!
//! Each service mounts this under `/audit-api/v1` to expose a filtered read over
//! `audit.domain_event`. Filters are optional and AND together; results are
//! ordered `occurred_at ASC, id ASC`, bounded by `limit` (default 100, max 1000).
//! Unguarded — read-only over an append-only log, no secret material (RBAC is the
//! retired Phase 12). The scenario runner asserts on what a run emitted here.

use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::get,
    Json, Router,
};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::{PgPool, QueryBuilder, Row};

const MAX_LIMIT: i64 = 1000;
const DEFAULT_LIMIT: i64 = 100;

/// Build the `/events` router already bound to `pool`. Mount under `/audit-api/v1`.
pub fn audit_events_router(pool: PgPool) -> Router {
    Router::new()
        .route("/events", get(list_events))
        .with_state(pool)
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct EventFilters {
    aggregate_type: Option<String>,
    aggregate_id: Option<String>,
    event_type: Option<String>,
    event_type_prefix: Option<String>,
    occurred_since: Option<String>,
    occurred_until: Option<String>,
    service_identity: Option<String>,
    limit: Option<i64>,
}

async fn list_events(State(pool): State<PgPool>, Query(f): Query<EventFilters>) -> Response {
    let since = match parse_iso_opt(f.occurred_since.as_deref(), "occurredSince") {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let until = match parse_iso_opt(f.occurred_until.as_deref(), "occurredUntil") {
        Ok(v) => v,
        Err(resp) => return resp,
    };
    let limit = f.limit.unwrap_or(DEFAULT_LIMIT).clamp(1, MAX_LIMIT);

    let mut qb: QueryBuilder<sqlx::Postgres> = QueryBuilder::new(
        "SELECT event_id, event_type, aggregate_type, aggregate_id, occurred_at, \
         trace_id, actor, channel, tenant_id, service_identity, payload, \
         schema_version, published_to_mq FROM audit.domain_event",
    );
    let mut first = true;
    let mut where_clause = |qb: &mut QueryBuilder<sqlx::Postgres>| {
        qb.push(if first { " WHERE " } else { " AND " });
        first = false;
    };
    if let Some(v) = &f.aggregate_type {
        where_clause(&mut qb);
        qb.push("aggregate_type = ").push_bind(v.clone());
    }
    if let Some(v) = &f.aggregate_id {
        where_clause(&mut qb);
        qb.push("aggregate_id = ").push_bind(v.clone());
    }
    if let Some(v) = &f.event_type {
        where_clause(&mut qb);
        qb.push("event_type = ").push_bind(v.clone());
    }
    if let Some(v) = &f.event_type_prefix {
        where_clause(&mut qb);
        qb.push("event_type LIKE ").push_bind(format!("{v}%"));
    }
    if let Some(v) = since {
        where_clause(&mut qb);
        qb.push("occurred_at >= ").push_bind(v);
    }
    if let Some(v) = until {
        where_clause(&mut qb);
        qb.push("occurred_at <= ").push_bind(v);
    }
    if let Some(v) = &f.service_identity {
        where_clause(&mut qb);
        qb.push("service_identity = ").push_bind(v.clone());
    }
    qb.push(" ORDER BY occurred_at ASC, id ASC LIMIT ")
        .push_bind(limit);

    let rows = match qb.build().fetch_all(&pool).await {
        Ok(rows) => rows,
        Err(e) => {
            tracing::warn!(error = %e, "audit.query.failed");
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "detail": "audit query failed" })),
            )
                .into_response();
        }
    };

    let events: Vec<Value> = rows.iter().map(row_to_event).collect();
    let count = events.len();
    Json(json!({ "events": events, "count": count })).into_response()
}

fn row_to_event(r: &sqlx::postgres::PgRow) -> Value {
    let event_id: uuid::Uuid = r.get("event_id");
    let occurred_at: DateTime<Utc> = r.get("occurred_at");
    let payload: Option<Value> = r.try_get("payload").ok().flatten();
    json!({
        "eventId": event_id.to_string(),
        "eventType": r.get::<String, _>("event_type"),
        "aggregateType": r.get::<String, _>("aggregate_type"),
        "aggregateId": r.get::<String, _>("aggregate_id"),
        "occurredAt": occurred_at.to_rfc3339(),
        "traceId": r.get::<Option<String>, _>("trace_id"),
        "actor": r.get::<Option<String>, _>("actor"),
        "channel": r.get::<Option<String>, _>("channel"),
        "tenantId": r.get::<String, _>("tenant_id"),
        "serviceIdentity": r.get::<String, _>("service_identity"),
        "payload": payload.unwrap_or_else(|| json!({})),
        "schemaVersion": r.get::<i16, _>("schema_version"),
        "publishedToMq": r.get::<bool, _>("published_to_mq"),
    })
}

/// Parse an optional ISO-8601 timestamp, mirroring `_parse_iso_or_400`: `None`
/// stays `None`; a bad value becomes a 422 `INVALID_TIMESTAMP` response. The Err
/// variant is a full `Response` (a rare, one-per-request error path — the size
/// lint doesn't earn a `Box` here).
#[allow(clippy::result_large_err)]
fn parse_iso_opt(raw: Option<&str>, field: &str) -> Result<Option<DateTime<Utc>>, Response> {
    match raw {
        None => Ok(None),
        Some(s) => match DateTime::parse_from_rfc3339(s) {
            Ok(dt) => Ok(Some(dt.with_timezone(&Utc))),
            Err(_) => Err((
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({
                    "detail": {
                        "code": "INVALID_TIMESTAMP",
                        "message": format!("'{field}' must be ISO-8601, got '{s}'"),
                    }
                })),
            )
                .into_response()),
        },
    }
}
