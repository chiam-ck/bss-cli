//! Reconciliation sweeper (v1.2) — surface stranded orders to the operator.
//!
//! An order left `in_progress` past `order_stuck_threshold_seconds` gets a single
//! `order.stuck` event (guarded by `stuck_flagged_at`) so it shows up on the
//! cockpit instead of sitting invisible. It does NOT auto-cancel/complete —
//! resolving is an operator decision. Time is the deterministic clock; the
//! threshold compares against `order_date` (frozen-clock-safe).

use bss_context::RequestCtx;
use chrono::Duration;
use serde_json::json;
use sqlx::{PgPool, Row};

use crate::events::stage;

/// Flag orders stuck in_progress past the threshold. Returns count flagged.
pub async fn sweep_once(pool: &PgPool, threshold_seconds: i64) -> Result<usize, sqlx::Error> {
    let cutoff = bss_clock::now() - Duration::seconds(threshold_seconds);
    let ctx = RequestCtx::default();

    let rows = sqlx::query(
        "SELECT id, customer_id, state, order_date FROM order_mgmt.product_order \
         WHERE state = 'in_progress' AND stuck_flagged_at IS NULL AND order_date < $1",
    )
    .bind(cutoff)
    .fetch_all(pool)
    .await?;

    let mut count = 0;
    for row in &rows {
        let id: String = row.try_get("id")?;
        let customer_id: String = row.try_get("customer_id")?;
        let state: String = row.try_get("state")?;
        let order_date: Option<chrono::DateTime<chrono::Utc>> = row.try_get("order_date")?;

        let mut tx = pool.begin().await?;
        stage(
            &mut tx,
            &ctx,
            "order.stuck",
            "ProductOrder",
            &id,
            json!({
                "commercialOrderId": id,
                "customerId": customer_id,
                "state": state,
                "stuckSinceOrderDate": order_date.map(bss_clock::isoformat),
            }),
        )
        .await?;
        sqlx::query("UPDATE order_mgmt.product_order SET stuck_flagged_at = $2, updated_at = now() WHERE id = $1")
            .bind(&id)
            .bind(bss_clock::now())
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        tracing::warn!(commercial_order_id = id, customer_id, "order.stuck.flagged");
        count += 1;
    }
    Ok(count)
}

/// Background loop — sweep every `interval_seconds`. A tick failure is logged, not
/// fatal.
pub async fn tick_loop(pool: PgPool, threshold_seconds: i64, interval_seconds: u64) {
    let mut ticker = tokio::time::interval(std::time::Duration::from_secs(interval_seconds));
    // Skip the immediate first tick (Python sleeps before the first sweep).
    ticker.tick().await;
    loop {
        ticker.tick().await;
        if let Err(e) = sweep_once(&pool, threshold_seconds).await {
            tracing::warn!(error = %e, "reconciliation.tick_failed");
        }
    }
}
