//! SOM orchestration handlers — port of `app.services.som_service.SOMService`.
//!
//! These run on the consumer's transaction (`&mut PgConnection`). The CFS is read
//! `FOR UPDATE`, so concurrent `provisioning.task.completed` events serialize on
//! the `pendingTasks` read-modify-write. The Python oracle reads the CFS without a
//! lock and its aio-pika callbacks run concurrently, so simultaneous task
//! completions can lose a `pendingTasks` update and never flip the order to
//! `completed` (the P1 `order.stuck`). Serializing here is the correct
//! implementation of the intended behaviour — noted for a Python backport.

use bss_clients::InventoryClient;
use bss_context::RequestCtx;
use serde_json::{json, Map, Value};
use sqlx::postgres::PgConnection;

use crate::domain::{check_service_order_transition, check_service_transition};
use crate::events::stage;
use crate::repo;

/// `provisioning.task.completed` — mark the task done; when *all* pending tasks
/// are complete, activate the CFS + RFS, complete the ServiceOrder, and stage
/// `service_order.completed` for COM.
pub async fn handle_task_completed(
    conn: &mut PgConnection,
    ctx: &RequestCtx,
    service_id: &str,
    task_type: &str,
    service_order_id: &str,
    commercial_order_id: &str,
) -> Result<(), String> {
    let Some(cfs) = repo::get_service_for_update(conn, service_id)
        .await
        .map_err(db)?
    else {
        tracing::error!(service_id, "task.completed.cfs_not_found");
        return Ok(());
    };

    // Idempotency: a `task.completed` for an already-`activated` CFS is a duplicate
    // or late redelivery — all tasks were already done and the service activated on
    // the final one. Ack-and-skip. Without this, the handler recomputes
    // all_completed=true and attempts `activated → activated`, which the service FSM
    // rejects (`Allowed: ['terminated']`); under at-least-once delivery that failure
    // then storms the retry/park queues. The `service` FSM allows only
    // `activated → terminated`, so `activated` is terminal for the provisioning path.
    if cfs.state == "activated" {
        tracing::info!(service_id, task_type, "task.completed.already_activated_noop");
        return Ok(());
    }

    let (chars, pending) = mark_pending(&cfs.characteristics, task_type, "completed");

    let all_completed = pending.values().all(|v| v == "completed");
    if !all_completed {
        repo::set_service_characteristics(conn, service_id, &chars)
            .await
            .map_err(db)?;
        tracing::info!(service_id, task_type, "task.completed.updated");
        return Ok(());
    }

    let now = bss_clock::now();

    // CFS → activated (persist the pending update + the transition together).
    check_service_transition(&cfs.state, "activated").map_err(policy)?;
    repo::set_service_characteristics(conn, service_id, &chars)
        .await
        .map_err(db)?;
    repo::activate_service(conn, service_id, now)
        .await
        .map_err(db)?;
    repo::add_state_history(
        conn,
        service_id,
        Some("reserved"),
        "activated",
        &ctx.actor,
        "all provisioning tasks completed",
        &ctx.tenant,
    )
    .await
    .map_err(db)?;

    // RFS children → activated.
    for (child_id, child_state) in repo::child_states(conn, service_id).await.map_err(db)? {
        check_service_transition(&child_state, "activated").map_err(policy)?;
        repo::activate_service(conn, &child_id, now)
            .await
            .map_err(db)?;
        repo::add_state_history(
            conn,
            &child_id,
            Some("reserved"),
            "activated",
            &ctx.actor,
            "parent CFS activated",
            &ctx.tenant,
        )
        .await
        .map_err(db)?;
    }

    // ServiceOrder → completed.
    if let Some(so_state) = repo::service_order_state(conn, service_order_id)
        .await
        .map_err(db)?
    {
        check_service_order_transition(&so_state, "completed").map_err(policy)?;
        repo::set_service_order_state(conn, service_order_id, "completed", None, Some(now))
            .await
            .map_err(db)?;
    }

    // Stage service_order.completed with the full payload COM needs.
    let mut payload = json!({
        "serviceOrderId": service_order_id,
        "commercialOrderId": non_empty(commercial_order_id).unwrap_or_else(|| cv_str(&chars, "commercialOrderId")),
        "customerId": cv_str(&chars, "customerId"),
        "offeringId": cv_str(&chars, "offeringId"),
        "msisdn": cv_str(&chars, "msisdn"),
        "iccid": cv_str(&chars, "iccid"),
        "paymentMethodId": cv_str(&chars, "paymentMethodId"),
        "cfsServiceId": cfs.id,
    });
    if let Some(snap) = chars.get("priceSnapshot") {
        if !snap.is_null() {
            if let Some(obj) = payload.as_object_mut() {
                obj.insert("priceSnapshot".into(), snap.clone());
            }
        }
    }
    stage(
        conn,
        ctx,
        "service_order.completed",
        "ServiceOrder",
        service_order_id,
        payload,
    )
    .await
    .map_err(db)?;

    tracing::info!(service_order_id, cfs_id = %cfs.id, "service_order.completed");
    Ok(())
}

/// `provisioning.task.failed` — on a permanent failure, fail the CFS, release the
/// reserved inventory, fail the ServiceOrder, and stage `service_order.failed`.
#[allow(clippy::too_many_arguments)]
pub async fn handle_task_failed(
    conn: &mut PgConnection,
    inventory: &InventoryClient,
    ctx: &RequestCtx,
    service_id: &str,
    task_type: &str,
    service_order_id: &str,
    commercial_order_id: &str,
    permanent: bool,
) -> Result<(), String> {
    if !permanent {
        tracing::info!(service_id, task_type, "task.failed.transient");
        return Ok(());
    }

    let Some(cfs) = repo::get_service_for_update(conn, service_id)
        .await
        .map_err(db)?
    else {
        tracing::error!(service_id, "task.failed.cfs_not_found");
        return Ok(());
    };

    let (chars, _pending) = mark_pending(&cfs.characteristics, task_type, "failed");

    check_service_transition(&cfs.state, "failed").map_err(policy)?;
    repo::set_service_characteristics(conn, service_id, &chars)
        .await
        .map_err(db)?;
    repo::set_service_state(conn, service_id, "failed")
        .await
        .map_err(db)?;
    repo::add_state_history(
        conn,
        service_id,
        Some("reserved"),
        "failed",
        &ctx.actor,
        &format!("provisioning task {task_type} failed permanently"),
        &ctx.tenant,
    )
    .await
    .map_err(db)?;

    // Release inventory (best-effort — external, not transactional).
    if let Some(msisdn) = cfs.characteristics.get("msisdn").and_then(Value::as_str) {
        if let Err(e) = inventory.release_msisdn(msisdn).await {
            tracing::warn!(error = %e, "inventory.release.msisdn.failed");
        }
    }
    if let Some(iccid) = cfs.characteristics.get("iccid").and_then(Value::as_str) {
        if let Err(e) = inventory.release_esim(iccid).await {
            tracing::warn!(error = %e, "inventory.release.esim.failed");
        }
    }

    // ServiceOrder → failed.
    if let Some(so_state) = repo::service_order_state(conn, service_order_id)
        .await
        .map_err(db)?
    {
        check_service_order_transition(&so_state, "failed").map_err(policy)?;
        repo::set_service_order_state(
            conn,
            service_order_id,
            "failed",
            None,
            Some(bss_clock::now()),
        )
        .await
        .map_err(db)?;
    }

    let reason = format!("provisioning task {task_type} failed permanently");
    let comm = non_empty(commercial_order_id).unwrap_or_else(|| {
        cfs.characteristics
            .get("commercialOrderId")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    });
    stage(
        conn,
        ctx,
        "service_order.failed",
        "ServiceOrder",
        service_order_id,
        json!({ "serviceOrderId": service_order_id, "commercialOrderId": comm, "reason": reason }),
    )
    .await
    .map_err(db)?;

    tracing::info!(service_order_id, cfs_id = %cfs.id, task_type, "service_order.failed");
    Ok(())
}

/// `provisioning.task.stuck` — record the stuck task on the CFS; manual
/// intervention needed (no state transition).
pub async fn handle_task_stuck(
    conn: &mut PgConnection,
    service_id: &str,
    task_type: &str,
    service_order_id: &str,
) -> Result<(), String> {
    let Some(cfs) = repo::get_service_for_update(conn, service_id)
        .await
        .map_err(db)?
    else {
        tracing::error!(service_id, "task.stuck.cfs_not_found");
        return Ok(());
    };
    let (chars, _pending) = mark_pending(&cfs.characteristics, task_type, "stuck");
    repo::set_service_characteristics(conn, service_id, &chars)
        .await
        .map_err(db)?;
    tracing::warn!(
        service_id,
        task_type,
        service_order_id,
        "task.stuck.manual_intervention_needed"
    );
    Ok(())
}

/// Clone the characteristics, set `pendingTasks[task_type] = status`, and return
/// both the updated characteristics and the pending map (for the all-completed
/// check). Missing `pendingTasks` starts empty (matching the Python `.get({})`).
fn mark_pending(
    characteristics: &Value,
    task_type: &str,
    status: &str,
) -> (Value, Map<String, Value>) {
    let mut chars = characteristics.as_object().cloned().unwrap_or_default();
    let mut pending = chars
        .get("pendingTasks")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    pending.insert(task_type.to_string(), json!(status));
    chars.insert("pendingTasks".into(), Value::Object(pending.clone()));
    (Value::Object(chars), pending)
}

/// Read a string field out of the characteristics `Value` (default `""`).
fn cv_str(chars: &Value, key: &str) -> String {
    chars
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn non_empty(s: &str) -> Option<String> {
    if s.is_empty() {
        None
    } else {
        Some(s.to_string())
    }
}

fn db(e: sqlx::Error) -> String {
    format!("db error: {e}")
}

fn policy(p: bss_db::PolicyViolation) -> String {
    format!("policy {}: {}", p.rule, p.message)
}
