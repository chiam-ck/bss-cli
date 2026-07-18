//! Provisioning worker — the heart of the simulator. Port of `app.domain.worker`.
//!
//! Processes one provisioning task with configurable fault injection
//! (`fail_always` / `fail_first_attempt` / `slow` / `stuck`) and the eSIM SM-DP+
//! seam, emitting `provisioning.task.{completed,failed,stuck}` at the terminal
//! state. The Python session flushes intermediate states but only *commits* at a
//! terminal state, so externally the task row appears once, in its final state —
//! this port mutates an in-memory [`Task`] through the loop and persists once
//! (row + audit + publish) at the terminal state, which is externally identical.
//!
//! Audit rows omit `service_identity` (the worker's `DomainEvent` does too — the
//! column default `"default"` fills it, regardless of the calling context).

use bss_context::RequestCtx;
use bss_db::PgPool;
use bss_events::MqChannel;
use serde_json::Value;
use std::time::Duration;

use crate::domain::{should_fire, slow_multiplier, task_duration, task_event_payload, Task};
use crate::esim::EsimProvider;
use crate::repo;

/// Parameters for one task run — the destructured `provisioning.task.created`
/// message (or the payload of a resolve/retry re-process).
pub struct TaskRequest {
    pub service_id: String,
    pub service_order_id: String,
    pub commercial_order_id: String,
    pub task_type: String,
    pub payload: Value,
}

/// The inbox-dedup consumer identity — matches the `provisioning.task.created`
/// consumer tag, so a claim is scoped to this consumer (mirrors `bind_consumer`).
pub(crate) const INBOX_CONSUMER: &str = "provisioning-sim.task.created";

/// Process a single provisioning task with fault injection. Errors are DB/MQ
/// faults only; fault-injection "failures" are modelled as task states, not
/// `Err`. `ctx` stamps the audit rows (consumer → `RequestCtx::default`, HTTP
/// resolve/retry → the live request context).
///
/// `event_id` is the relay's AMQP `message_id` on the `task.created` delivery
/// (`None` for the HTTP resolve/retry path, which is operator-initiated, not an
/// event redelivery). When present it is claimed in the terminal persist
/// transaction so a redelivery of the same event is skipped by the consumer's
/// pre-check — closing the duplicate-re-run storm without holding a tx across the
/// simulated-work sleep.
pub async fn process_task(
    pool: &PgPool,
    mq: Option<&MqChannel>,
    esim: EsimProvider,
    ctx: &RequestCtx,
    req: TaskRequest,
    event_id: Option<&str>,
) -> Result<(), sqlx::Error> {
    let task_id = repo::task_next_id(pool).await?;
    let mut task = Task {
        id: task_id.clone(),
        service_id: req.service_id,
        task_type: req.task_type.clone(),
        state: "pending".into(),
        attempts: 0,
        max_attempts: 3,
        payload: req.payload.clone(),
        last_error: None,
        started_at: None,
        completed_at: None,
    };

    let fault = repo::fault_get_active(pool, &req.task_type).await?;
    let fault_type = fault.as_ref().map(|f| f.fault_type.as_str());
    let fault_prob = fault.as_ref().map(|f| f.probability).unwrap_or(0.0);

    // Stuck fault — never auto-retry.
    if fault_type == Some("stuck") && should_fire(fault_prob) {
        task.state = "stuck".into();
        task.started_at = Some(bss_clock::now());
        persist_and_publish(
            pool,
            mq,
            ctx,
            "provisioning.task.stuck",
            &task,
            &req.service_order_id,
            &req.commercial_order_id,
            false,
            event_id,
        )
        .await?;
        tracing::info!(task_id = %task_id, task_type = %req.task_type, "task.stuck");
        return Ok(());
    }

    while task.attempts < task.max_attempts {
        task.attempts += 1;
        task.state = "running".into();
        if task.started_at.is_none() {
            task.started_at = Some(bss_clock::now());
        }

        // fail_always
        if fault_type == Some("fail_always") && should_fire(fault_prob) {
            task.last_error = Some(format!("Simulated fail_always for {}", req.task_type));
            if task.attempts >= task.max_attempts {
                task.state = "failed".into();
                persist_and_publish(
                    pool,
                    mq,
                    ctx,
                    "provisioning.task.failed",
                    &task,
                    &req.service_order_id,
                    &req.commercial_order_id,
                    true,
                    event_id,
                )
                .await?;
                tracing::info!(task_id = %task_id, attempts = task.attempts, "task.failed.permanent");
                return Ok(());
            }
            task.state = "failed".into();
            tracing::info!(task_id = %task_id, attempts = task.attempts, "task.failed.retrying");
            continue;
        }

        // fail_first_attempt
        if fault_type == Some("fail_first_attempt") && task.attempts == 1 && should_fire(fault_prob)
        {
            task.state = "failed".into();
            task.last_error = Some(format!(
                "Simulated fail_first_attempt for {}",
                req.task_type
            ));
            tracing::info!(task_id = %task_id, "task.failed.first_attempt");
            continue;
        }

        // Simulate work (slow fault stretches the latency 2x–5x).
        let mut duration = task_duration(&req.task_type);
        if fault_type == Some("slow") && should_fire(fault_prob) {
            duration *= slow_multiplier();
        }
        tokio::time::sleep(Duration::from_secs_f64(duration)).await;

        // eSIM SM-DP+ seam — the sim provider returns success without extra
        // latency, preserving v0.13/v0.14 timing.
        if req.task_type == "ESIM_PROFILE_PREPARE" {
            let iccid = str_field(&req.payload, "iccid");
            let imsi = str_field(&req.payload, "imsi");
            let msisdn = str_field(&req.payload, "msisdn");
            match esim.order_profile(iccid, imsi, msisdn).await {
                Ok(result) if !result.success => {
                    task.state = "failed".into();
                    task.last_error = Some(format!(
                        "SM-DP+ provider declined profile order: {}",
                        result
                            .provider_reference
                            .as_deref()
                            .unwrap_or("no reference")
                    ));
                    if task.attempts >= task.max_attempts {
                        persist_and_publish(
                            pool,
                            mq,
                            ctx,
                            "provisioning.task.failed",
                            &task,
                            &req.service_order_id,
                            &req.commercial_order_id,
                            true,
                            event_id,
                        )
                        .await?;
                        return Ok(());
                    }
                    continue;
                }
                Ok(_) => {}
                // A stub provider (onbglobal/esim_access) raising is a hard fault,
                // not a simulated one — surface it as a permanent failure with the
                // pointer message rather than looping.
                Err(msg) => {
                    task.state = "failed".into();
                    task.last_error = Some(msg);
                    persist_and_publish(
                        pool,
                        mq,
                        ctx,
                        "provisioning.task.failed",
                        &task,
                        &req.service_order_id,
                        &req.commercial_order_id,
                        true,
                        event_id,
                    )
                    .await?;
                    return Ok(());
                }
            }
        }

        // Success.
        task.state = "completed".into();
        task.completed_at = Some(bss_clock::now());
        persist_and_publish(
            pool,
            mq,
            ctx,
            "provisioning.task.completed",
            &task,
            &req.service_order_id,
            &req.commercial_order_id,
            false,
            event_id,
        )
        .await?;
        tracing::info!(task_id = %task_id, task_type = %req.task_type, attempts = task.attempts, "task.completed");
        return Ok(());
    }

    // Safety net — should be unreachable (the loop returns at every terminal).
    task.state = "failed".into();
    task.last_error = Some("max_attempts exhausted".into());
    persist_and_publish(
        pool,
        mq,
        ctx,
        "provisioning.task.failed",
        &task,
        &req.service_order_id,
        &req.commercial_order_id,
        true,
        event_id,
    )
    .await?;
    Ok(())
}

/// Persist the terminal task row + its audit event and inline-publish. Publish
/// first, then INSERT both rows in one transaction with the resolved
/// `published_to_mq` — the same final state as the Python session's
/// flush-then-commit. `service_identity`/`trace_id` are omitted (DB defaults),
/// matching the worker's `DomainEvent`.
#[allow(clippy::too_many_arguments)]
async fn persist_and_publish(
    pool: &PgPool,
    mq: Option<&MqChannel>,
    ctx: &RequestCtx,
    event_type: &str,
    task: &Task,
    service_order_id: &str,
    commercial_order_id: &str,
    permanent: bool,
    event_id: Option<&str>,
) -> Result<(), sqlx::Error> {
    let payload = task_event_payload(task, service_order_id, commercial_order_id, permanent);

    let mut published = false;
    if let Some(mq) = mq {
        match mq.publish_json(event_type, &payload).await {
            Ok(()) => published = true,
            Err(e) => {
                tracing::warn!(error = %e, event_type = event_type, task_id = %task.id, "mq.publish.failed")
            }
        }
    }

    let mut tx = pool.begin().await?;
    repo::task_insert(&mut tx, task, &ctx.tenant).await?;
    sqlx::query(
        "INSERT INTO audit.domain_event \
         (event_id, event_type, aggregate_type, aggregate_id, occurred_at, actor, channel, \
          tenant_id, payload, schema_version, published_to_mq) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,1,$10)",
    )
    .bind(uuid::Uuid::new_v4())
    .bind(event_type)
    .bind("provisioning_task")
    .bind(&task.id)
    .bind(bss_clock::now())
    .bind(&ctx.actor)
    .bind(&ctx.channel)
    .bind(&ctx.tenant)
    .bind(sqlx::types::Json(payload))
    .bind(published)
    .execute(&mut *tx)
    .await?;
    // Inbox claim — committed atomically with the task/event rows so a crash can't
    // leave an event claimed-but-unprocessed. `ON CONFLICT DO NOTHING` keeps the
    // operator resolve/retry path (which passes `event_id = None`) and any race a
    // no-op. The consumer's pre-check skips a claimed event before it reaches here.
    if let Some(uuid) = event_id.and_then(|e| uuid::Uuid::parse_str(e).ok()) {
        sqlx::query(
            "INSERT INTO provisioning.processed_event (event_id, consumer, processed_at) \
             VALUES ($1, $2, now()) ON CONFLICT (event_id, consumer) DO NOTHING",
        )
        .bind(uuid)
        .bind(INBOX_CONSUMER)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(())
}

fn str_field<'a>(payload: &'a Value, key: &str) -> &'a str {
    payload.get(key).and_then(Value::as_str).unwrap_or("")
}
