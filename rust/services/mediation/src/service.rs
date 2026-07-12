//! Mediation orchestration — port of `app.services.mediation_service.MediationService`.
//!
//! The block-at-edge flow: cheap policies → Subscription enrichment → post-enrich
//! policies → persist + inline-publish. A rejected event leaves **no**
//! `usage_event` row; its only trace is a `usage.rejected` audit row (+ MQ
//! message), so the attempt is observable without corrupting the CDR stream.
//!
//! Unlike rating's consumer, mediation runs under a real request context — the
//! audit rows are stamped from [`bss_context::current`] (actor/channel/tenant/
//! service_identity), not `RequestCtx::default`.

use bss_clients::ClientError;
use bss_context::RequestCtx;
use bss_events::{stage_event, MqChannel};
use serde_json::Value;
use sqlx::postgres::PgConnection;

use crate::domain::{
    check_msisdn_matches, check_positive_quantity, check_subscription_active,
    check_valid_event_type, rejection_payload, subscription_not_found, usage_recorded_payload,
    UsageEvent,
};
use crate::error::ApiError;
use crate::repo::{self, UsageEventRow};
use crate::state::AppState;

/// The validated ingress request (already parsed from the TMF body).
pub struct IngestRequest {
    pub msisdn: String,
    pub event_type: String,
    pub event_time: chrono::DateTime<chrono::Utc>,
    pub quantity: i64,
    pub unit: String,
    pub source: Option<String>,
    pub raw_cdr_ref: Option<String>,
    pub roaming_indicator: bool,
}

/// Run the full ingest. Returns the persisted row on success; on a policy
/// violation returns `ApiError::Policy` (after recording the rejection where the
/// oracle does); on an upstream fault returns `ApiError::Upstream`.
pub async fn ingest(
    state: &AppState,
    ctx: &RequestCtx,
    req: IngestRequest,
) -> Result<UsageEventRow, ApiError> {
    // Policies that don't require enrichment.
    check_positive_quantity(req.quantity)?;
    check_valid_event_type(&req.event_type)?;

    // Enrich via Subscription. NotFound → record rejection + policy 422; any
    // other client error → 500 (the Python middleware's `ServerError` catch).
    let sub = match state.subscription.get_by_msisdn(&req.msisdn).await {
        Ok(sub) => sub,
        Err(ClientError::NotFound(_)) => {
            let violation = subscription_not_found(&req.msisdn);
            record_rejection(state, ctx, &req, None, None, &violation.rule).await;
            return Err(ApiError::Policy(violation));
        }
        Err(_) => return Err(ApiError::Upstream),
    };

    // Defensive — no rejection row for a mismatch (matches the oracle: this
    // check raises without `_record_rejection`).
    check_msisdn_matches(&sub, &req.msisdn)?;

    // Block-at-edge: non-active subscriptions are rejected + audited.
    if let Err(violation) = check_subscription_active(&sub) {
        let sub_id = sub.get("id").and_then(Value::as_str);
        let state_s = sub.get("state").and_then(Value::as_str);
        record_rejection(state, ctx, &req, sub_id, state_s, &violation.rule).await;
        return Err(ApiError::Policy(violation));
    }

    // Persist — no row existed until every policy passed.
    let subscription_id = sub.get("id").and_then(Value::as_str).map(|s| s.to_string());
    let offering_id = sub.get("offeringId").and_then(Value::as_str);

    let mut tx = state.pool.begin().await.map_err(db_to_upstream)?;
    let id = repo::next_id(&mut tx).await.map_err(db_to_upstream)?;

    let evt = UsageEvent {
        id: id.clone(),
        msisdn: req.msisdn.clone(),
        subscription_id: subscription_id.clone(),
        event_type: req.event_type.clone(),
        event_time: req.event_time,
        quantity: req.quantity,
        unit: req.unit.clone(),
        source: req.source.clone(),
        raw_cdr_ref: req.raw_cdr_ref.clone(),
        roaming_indicator: req.roaming_indicator,
    };
    repo::insert(&mut tx, &evt, &ctx.tenant)
        .await
        .map_err(db_to_upstream)?;

    let payload = usage_recorded_payload(&evt, offering_id);
    stage_publish_audit(
        &mut tx,
        state.mq.as_deref(),
        ctx,
        "usage.recorded",
        "usage",
        &id,
        payload,
    )
    .await
    .map_err(db_to_upstream)?;

    tx.commit().await.map_err(db_to_upstream)?;

    tracing::info!(
        usage_event_id = %id,
        subscription_id = subscription_id.as_deref().unwrap_or(""),
        event_type = %req.event_type,
        quantity = req.quantity,
        "usage.recorded"
    );

    Ok(UsageEventRow {
        id,
        msisdn: evt.msisdn,
        subscription_id: evt.subscription_id,
        event_type: evt.event_type,
        event_time: evt.event_time,
        quantity: evt.quantity,
        unit: evt.unit,
        source: evt.source,
        raw_cdr_ref: evt.raw_cdr_ref,
        processed: false,
        processing_error: None,
        roaming_indicator: evt.roaming_indicator,
    })
}

/// Write the `usage.rejected` audit row (+ best-effort publish) in its own
/// transaction. Doctrine: no `usage_event` row for a rejection. A DB failure here
/// is swallowed — the rejection audit is best-effort observability, and the
/// caller still returns the original 422 to the client.
async fn record_rejection(
    state: &AppState,
    ctx: &RequestCtx,
    req: &IngestRequest,
    subscription_id: Option<&str>,
    sub_state: Option<&str>,
    reason: &str,
) {
    let payload = rejection_payload(
        &req.msisdn,
        subscription_id,
        sub_state,
        &req.event_type,
        req.event_time,
        req.quantity,
        &req.unit,
        req.source.as_deref(),
        req.raw_cdr_ref.as_deref(),
        reason,
    );
    let aggregate_id = subscription_id.unwrap_or(&req.msisdn).to_string();

    let result = async {
        let mut tx = state.pool.begin().await?;
        stage_publish_audit(
            &mut tx,
            state.mq.as_deref(),
            ctx,
            "usage.rejected",
            "usage",
            &aggregate_id,
            payload,
        )
        .await?;
        tx.commit().await
    }
    .await;

    if let Err(e) = result {
        tracing::warn!(error = %e, reason = reason, "usage.rejected.audit_failed");
    }
}

/// Stage the `audit.domain_event` row on `conn` (joining the caller's tx) and
/// inline-publish. Publish first, then INSERT with the resolved `published_to_mq`
/// flag — the same final state as the Python "stage → publish → set flag →
/// commit". Row context comes from the live request `ctx`.
async fn stage_publish_audit(
    conn: &mut PgConnection,
    mq: Option<&MqChannel>,
    ctx: &RequestCtx,
    event_type: &str,
    aggregate_type: &str,
    aggregate_id: &str,
    payload: Value,
) -> Result<(), sqlx::Error> {
    let ev = stage_event(ctx, event_type, aggregate_type, aggregate_id, Some(payload));

    let mut published = false;
    if let Some(mq) = mq {
        match mq.publish_json(event_type, &ev.payload).await {
            Ok(()) => published = true,
            Err(e) => tracing::warn!(error = %e, event_type = event_type, "mq.publish.failed"),
        }
    }

    let event_uuid = uuid::Uuid::parse_str(&ev.event_id).unwrap_or_else(|_| uuid::Uuid::new_v4());
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
    .bind(published)
    .execute(conn)
    .await?;

    Ok(())
}

/// A DB fault during ingest surfaces as `500 {detail:"Upstream service error"}`,
/// matching the Python middleware which lets non-policy exceptions become 500s.
fn db_to_upstream(e: sqlx::Error) -> ApiError {
    tracing::error!(error = %e, "mediation.db_error");
    ApiError::Upstream
}
