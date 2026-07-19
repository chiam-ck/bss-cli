//! Automated renewal worker (v0.18) — port of `app.workers.renewal`.
//!
//! In-process tick loop; the ONLY automatic renewal trigger (manual `renew_now` /
//! CLI / scenario are the operator escape hatches). Two sweeps per tick:
//!
//! 1. `sweep_due` — `active` + period-elapsed subs. **Mark-before-dispatch**:
//!    the SELECT-FOR-UPDATE-SKIP-LOCKED batch commits `last_renewal_attempted_at`
//!    BEFORE releasing the row locks, so a peer replica skips the marked row the
//!    instant the lock is gone (no double-charge). Each `renew()` then runs in its
//!    own transaction so one sub's failure doesn't poison the batch.
//! 2. `sweep_skipped` — `blocked` + overdue subs get a single
//!    `subscription.renewal_skipped` signal (dedup on the same column); no renew.
//!
//! The v0.18 upcoming-renewal *reminder* sweep is intentionally NOT ported here —
//! it requires the portal email adapter (lands with portals in P6). This mirrors
//! the Python path when `email_adapter is None`: the sweep is simply disabled, so
//! `renewal_reminder_sent_at` is left untouched (not an API-observable field).

use bss_context::RequestCtx;
use serde_json::json;

use crate::error::ApiError;
use crate::events::stage;
use crate::repo;
use crate::service;
use crate::state::AppState;

const BATCH_LIMIT: i64 = 100;

fn worker_ctx() -> RequestCtx {
    RequestCtx {
        actor: "system:renewal_worker".to_string(),
        channel: "system".to_string(),
        ..RequestCtx::default()
    }
}

/// Find active+due subs, mark each (committed before lock release), dispatch
/// `renew()` per id in its own transaction.
pub async fn sweep_due(st: &AppState) -> Result<(), ApiError> {
    let now = bss_clock::now();
    let tenant = st.settings.tenant_default.clone();

    // Txn 1: select + mark + commit.
    let ids = {
        let mut tx = st.pool.begin().await?;
        let ids = repo::due_for_renewal(&mut tx, now, BATCH_LIMIT, &tenant).await?;
        if ids.is_empty() {
            return Ok(());
        }
        repo::mark_renewal_attempted(&mut tx, &ids, now).await?;
        tx.commit().await?;
        ids
    };

    // Txn 2 (one per id): dispatch via the canonical service::renew.
    let ctx = worker_ctx();
    for sub_id in ids {
        match service::renew(st, &ctx, &sub_id).await {
            Ok(_) => tracing::info!(subscription_id = sub_id, "renewal.worker.dispatched"),
            Err(ApiError::Policy(pv)) => tracing::warn!(
                subscription_id = sub_id,
                rule = pv.rule,
                message = pv.message,
                "renewal.worker.policy_violation"
            ),
            Err(e) => {
                tracing::error!(subscription_id = sub_id, error = ?e, "renewal.worker.dispatch_failed")
            }
        }
    }
    Ok(())
}

/// Find blocked+overdue subs, emit `subscription.renewal_skipped`, mark. No
/// `renew()` — blocked subs need an explicit operator intervention. Single
/// transaction: emit all events + bulk mark + commit.
pub async fn sweep_skipped(st: &AppState) -> Result<(), ApiError> {
    let now = bss_clock::now();
    let tenant = st.settings.tenant_default.clone();
    let ctx = worker_ctx();

    let mut tx = st.pool.begin().await?;
    let ids = repo::overdue_blocked(&mut tx, now, BATCH_LIMIT, &tenant).await?;
    if ids.is_empty() {
        return Ok(());
    }
    for sub_id in &ids {
        stage(
            &mut tx,
            &ctx,
            "subscription.renewal_skipped",
            "subscription",
            sub_id,
            json!({
                "subscriptionId": sub_id,
                "reason": "blocked",
                "skippedAt": bss_clock::isoformat(now),
            }),
        )
        .await?;
    }
    repo::mark_renewal_attempted(&mut tx, &ids, now).await?;
    tx.commit().await?;
    tracing::info!(count = ids.len(), "renewal.worker.skipped_emitted");
    Ok(())
}

/// One sweep pass (due + skipped) — the deterministic unit the admin `tick-now`
/// route drives, and the loop body.
pub async fn tick_once(st: &AppState) {
    if let Err(e) = sweep_due(st).await {
        tracing::error!(error = ?e, "renewal.worker.sweep_due_crashed");
    }
    if let Err(e) = sweep_skipped(st).await {
        tracing::error!(error = ?e, "renewal.worker.sweep_skipped_crashed");
    }
}

/// Forever loop: sweep, then sleep the interval (matching the Python
/// sweep-then-`asyncio.sleep` cadence — a fixed delay, not a fixed rate).
pub async fn tick_loop(st: AppState, interval_seconds: u64) {
    tracing::info!(
        interval_seconds,
        batch_limit = BATCH_LIMIT,
        "renewal.worker.started"
    );
    loop {
        tick_once(&st).await;
        tokio::time::sleep(std::time::Duration::from_secs(interval_seconds)).await;
    }
}
