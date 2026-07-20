//! Open-order expiry sweep worker (v-reservation phase 4).
//!
//! In-process tick loop — the ONLY automatic expiry trigger (the "My open order"
//! Cancel is the customer escape hatch). Mirrors the subscription renewal worker's
//! discipline: no cron / no Celery, `FOR UPDATE SKIP LOCKED` for multi-replica
//! safety, `bss_clock::now()` (never wall clock) so a clock-advance scenario is
//! deterministic.
//!
//! One pass = one transaction: lock the expired batch, release each held MSISDN
//! back to the pool, mark the order `expired`, emit events, commit. The batch is
//! small and the work is a few UPDATEs, so holding the row locks for the pass is
//! fine — and SKIP LOCKED means a peer replica just takes a different batch.

use bss_context::RequestCtx;
use serde_json::json;

use crate::error::ApiError;
use crate::events::stage;
use crate::repo;
use crate::state::AppState;

const BATCH_LIMIT: i64 = 100;

fn worker_ctx() -> RequestCtx {
    RequestCtx {
        actor: "system:open_order_sweep".to_string(),
        channel: "system".to_string(),
        ..RequestCtx::default()
    }
}

/// Expire every open order whose 24h hold window has passed, releasing its MSISDN.
pub async fn sweep_expired(st: &AppState) -> Result<(), ApiError> {
    let now = bss_clock::now();
    let tenant = st.settings.tenant_default.clone();
    let ctx = worker_ctx();

    let mut tx = st.pool.begin().await?;
    let ids = repo::lock_expired_open_orders(&mut tx, now, BATCH_LIMIT, &tenant).await?;
    if ids.is_empty() {
        return Ok(());
    }
    for id in &ids {
        // Release the soft hold(s) owned by this order → available.
        let released = repo::release_holds_for(&mut tx, id, now).await?;
        for m in &released {
            stage(
                &mut tx,
                &ctx,
                "inventory.msisdn.released",
                "msisdn",
                m,
                json!({ "msisdn": m, "reserved_for": id }),
            )
            .await?;
        }
        repo::set_open_order_state(&mut tx, id, None, Some("expired"), None, now).await?;
        stage(
            &mut tx,
            &ctx,
            "open_order.expired",
            "open_order",
            id,
            json!({ "open_order_id": id, "released": released }),
        )
        .await?;
    }
    tx.commit().await?;
    tracing::info!(count = ids.len(), "open_order.sweep.expired");
    Ok(())
}

/// One sweep pass — the deterministic unit the admin `sweep-now` route drives,
/// and the loop body.
pub async fn tick_once(st: &AppState) {
    if let Err(e) = sweep_expired(st).await {
        tracing::error!(error = ?e, "open_order.sweep.crashed");
    }
}

/// Forever loop: sweep, then sleep the interval (sweep-then-sleep cadence).
pub async fn tick_loop(st: AppState, interval_seconds: u64) {
    tracing::info!(
        interval_seconds,
        batch_limit = BATCH_LIMIT,
        "open_order.sweep.started"
    );
    loop {
        tick_once(&st).await;
        tokio::time::sleep(std::time::Duration::from_secs(interval_seconds)).await;
    }
}
