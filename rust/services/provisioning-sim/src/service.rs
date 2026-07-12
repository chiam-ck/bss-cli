//! Provisioning orchestration — port of `app.services.provisioning_service`.
//!
//! The HTTP-facing operations: resolve a stuck task, retry a failed one, and the
//! fault-rule CRUD. `resolve`/`retry` reset the existing row then call the worker,
//! which creates a *fresh* task row for the re-process (faithful to the oracle,
//! which returns the reset original row, not the new one).

use bss_context::RequestCtx;
use serde_json::Value;

use crate::domain::{
    check_fault_injection_permission, check_resolve_has_note, check_retry_allowed,
};
use crate::error::ApiError;
use crate::repo;
use crate::state::AppState;
use crate::worker::{process_task, TaskRequest};

use bss_db::PolicyViolation;

/// `POST /task/{id}/resolve` — policy-check the note, transition stuck→pending,
/// re-process. Returns the (reset) original task, matching the oracle.
pub async fn resolve_stuck(
    state: &AppState,
    ctx: &RequestCtx,
    task_id: &str,
    note: &str,
) -> Result<Value, ApiError> {
    check_resolve_has_note(note, task_id)?;

    let task = repo::task_get(&state.pool, task_id)
        .await?
        .ok_or_else(|| not_found(task_id))?;

    if task.state != "stuck" {
        return Err(ApiError::Policy(PolicyViolation::with_context(
            "provisioning_task.resolve.requires_stuck_state",
            format!("Task {task_id} is in state '{}', not 'stuck'", task.state),
            serde_json::json!({ "task_id": task_id, "state": task.state }),
        )));
    }

    repo::task_update_state(
        &state.pool,
        task_id,
        "pending",
        0,
        Some(&format!("Resolved by operator: {note}")),
    )
    .await?;

    reprocess(state, ctx, &task).await?;

    refetch(state, task_id).await
}

/// `POST /task/{id}/retry` — policy-check the failed state + retry budget, then
/// re-process. Returns the (reset) original task.
pub async fn retry_failed(
    state: &AppState,
    ctx: &RequestCtx,
    task_id: &str,
) -> Result<Value, ApiError> {
    let task = repo::task_get(&state.pool, task_id)
        .await?
        .ok_or_else(|| not_found(task_id))?;

    if task.state != "failed" {
        return Err(ApiError::Policy(PolicyViolation::with_context(
            "provisioning_task.retry.requires_failed_state",
            format!("Task {task_id} is in state '{}', not 'failed'", task.state),
            serde_json::json!({ "task_id": task_id, "state": task.state }),
        )));
    }

    check_retry_allowed(task.attempts, task.max_attempts, task_id)?;

    repo::task_update_state(&state.pool, task_id, "pending", 0, None).await?;

    reprocess(state, ctx, &task).await?;

    refetch(state, task_id).await
}

/// `GET /fault-injection`.
pub async fn list_fault_rules(state: &AppState) -> Result<Vec<Value>, ApiError> {
    let rules = repo::fault_list_all(&state.pool).await?;
    Ok(rules.iter().map(repo::FaultRow::to_response).collect())
}

/// `PATCH /fault-injection/{id}` — apply the partial update after the admin
/// permission check.
pub async fn update_fault_rule(
    state: &AppState,
    ctx: &RequestCtx,
    fault_id: &str,
    enabled: Option<bool>,
    probability: Option<f64>,
    fault_type: Option<String>,
) -> Result<Value, ApiError> {
    check_fault_injection_permission(ctx)?;

    let mut fault = repo::fault_get(&state.pool, fault_id)
        .await?
        .ok_or_else(|| {
            ApiError::Policy(PolicyViolation::with_context(
                "provisioning.fault_injection.not_found",
                format!("Fault injection rule {fault_id} not found"),
                serde_json::json!({ "fault_id": fault_id }),
            ))
        })?;

    if let Some(e) = enabled {
        fault.enabled = e;
    }
    if let Some(p) = probability {
        fault.probability = p;
    }
    if let Some(ft) = fault_type {
        fault.fault_type = ft;
    }

    repo::fault_update(&state.pool, &fault).await?;
    tracing::info!(
        fault_id = %fault.id,
        enabled = fault.enabled,
        fault_type = %fault.fault_type,
        "fault_injection.updated"
    );
    Ok(fault.to_response())
}

/// Re-run the worker for a reset task, pulling the order ids out of its payload.
async fn reprocess(
    state: &AppState,
    ctx: &RequestCtx,
    task: &repo::TaskRow,
) -> Result<(), ApiError> {
    let payload = task
        .payload
        .clone()
        .unwrap_or_else(|| serde_json::json!({}));
    let service_order_id = payload
        .get("serviceOrderId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let commercial_order_id = payload
        .get("commercialOrderId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    process_task(
        &state.pool,
        state.mq.as_deref(),
        state.esim,
        ctx,
        TaskRequest {
            service_id: task.service_id.clone(),
            service_order_id,
            commercial_order_id,
            task_type: task.task_type.clone(),
            payload,
        },
    )
    .await?;
    Ok(())
}

async fn refetch(state: &AppState, task_id: &str) -> Result<Value, ApiError> {
    repo::task_get(&state.pool, task_id)
        .await?
        .map(|t| t.to_response())
        .ok_or_else(|| ApiError::NotFound(format!("Task {task_id} not found after operation")))
}

fn not_found(task_id: &str) -> ApiError {
    ApiError::Policy(PolicyViolation::with_context(
        "provisioning_task.not_found",
        format!("Task {task_id} not found"),
        serde_json::json!({ "task_id": task_id }),
    ))
}
