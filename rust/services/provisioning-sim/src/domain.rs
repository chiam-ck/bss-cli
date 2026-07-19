//! Pure provisioning domain — task-duration table, fault RNG, the in-memory task,
//! and the `provisioning.task.*` event payload builder.
//!
//! Ports the constants and `_audit_and_publish`'s payload shape from
//! `app.domain.worker`. The stateful retry loop + DB/MQ I/O stays in
//! [`crate::worker`]; the shapes and the (per-state) payload keys live here so
//! they're CI-testable without infra.

use bss_context::RequestCtx;
use bss_db::PolicyViolation;
use chrono::{DateTime, Utc};
use serde_json::{json, Value};

/// `provisioning_task.retry.max_attempts` — a task must not have exhausted its
/// retry budget (port of `check_retry_allowed`).
pub fn check_retry_allowed(
    attempts: i16,
    max_attempts: i16,
    task_id: &str,
) -> Result<(), PolicyViolation> {
    if attempts >= max_attempts {
        return Err(PolicyViolation::with_context(
            "provisioning_task.retry.max_attempts",
            format!("Task {task_id} has exhausted all {max_attempts} attempts"),
            json!({ "task_id": task_id, "attempts": attempts, "max_attempts": max_attempts }),
        ));
    }
    Ok(())
}

/// `provisioning.resolve_stuck.requires_note` — resolving a stuck task requires a
/// non-empty operator note (port of `check_resolve_has_note`).
pub fn check_resolve_has_note(note: &str, task_id: &str) -> Result<(), PolicyViolation> {
    if note.trim().is_empty() {
        return Err(PolicyViolation::with_context(
            "provisioning.resolve_stuck.requires_note",
            format!("Resolving stuck task {task_id} requires a non-empty note"),
            json!({ "task_id": task_id }),
        ));
    }
    Ok(())
}

/// `provisioning.set_fault_injection.admin_only` — fault-injection changes need
/// the manage permission. In v0.1 the default context carries `*`, so this always
/// passes (port of `check_fault_injection_permission`; Phase 12 enforces it).
pub fn check_fault_injection_permission(ctx: &RequestCtx) -> Result<(), PolicyViolation> {
    if !ctx.has_permission("provisioning.fault_injection.manage") {
        return Err(PolicyViolation::new(
            "provisioning.set_fault_injection.admin_only",
            "Fault injection management requires admin permission",
        ));
    }
    Ok(())
}

/// Simulated per-task-type work duration in seconds (network-element latency),
/// default 0.5s — port of `TASK_DURATIONS`.
pub fn task_duration(task_type: &str) -> f64 {
    match task_type {
        "HLR_PROVISION" => 0.5,
        "PCRF_POLICY_PUSH" => 0.3,
        "OCS_BALANCE_INIT" => 0.2,
        "ESIM_PROFILE_PREPARE" => 0.4,
        "HLR_DEPROVISION" => 0.4,
        _ => 0.5,
    }
}

/// `random.random() < probability` — port of `_should_fire`.
pub fn should_fire(probability: f64) -> bool {
    rand::random::<f64>() < probability
}

/// `random.uniform(2.0, 5.0)` — the `slow` fault latency multiplier.
pub fn slow_multiplier() -> f64 {
    use rand::Rng;
    rand::thread_rng().gen_range(2.0..5.0)
}

/// The in-memory provisioning task the worker mutates through its lifecycle,
/// mirroring the `ProvisioningTask` ORM columns the worker touches. Persisted at
/// terminal states only (matching the Python session's flush-then-commit).
#[derive(Debug, Clone)]
pub struct Task {
    pub id: String,
    pub service_id: String,
    pub task_type: String,
    pub state: String,
    pub attempts: i16,
    pub max_attempts: i16,
    pub payload: Value,
    pub last_error: Option<String>,
    pub started_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
}

/// Build the `provisioning.task.*` event payload — port of `_audit_and_publish`.
/// The base keys are always present; state-specific keys are added for
/// completed/failed/stuck exactly as the oracle does.
pub fn task_event_payload(
    task: &Task,
    service_order_id: &str,
    commercial_order_id: &str,
    permanent: bool,
) -> Value {
    let mut obj = serde_json::Map::new();
    obj.insert("taskId".into(), json!(task.id));
    obj.insert("serviceId".into(), json!(task.service_id));
    obj.insert("serviceOrderId".into(), json!(service_order_id));
    obj.insert("commercialOrderId".into(), json!(commercial_order_id));
    obj.insert("taskType".into(), json!(task.task_type));
    obj.insert("attempts".into(), json!(task.attempts));
    obj.insert("maxAttempts".into(), json!(task.max_attempts));
    match task.state.as_str() {
        "completed" => {
            obj.insert(
                "completedAt".into(),
                task.completed_at
                    .map(bss_clock::isoformat)
                    .map_or(Value::Null, Value::String),
            );
        }
        "failed" => {
            obj.insert(
                "lastError".into(),
                Value::String(task.last_error.clone().unwrap_or_default()),
            );
            obj.insert("permanent".into(), Value::Bool(permanent));
        }
        "stuck" => {
            obj.insert(
                "startedAt".into(),
                task.started_at
                    .map(bss_clock::isoformat)
                    .map_or(Value::Null, Value::String),
            );
        }
        _ => {}
    }
    Value::Object(obj)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn base_task(state: &str) -> Task {
        Task {
            id: "PTK-0001".into(),
            service_id: "SVC-0001".into(),
            task_type: "HLR_PROVISION".into(),
            state: state.into(),
            attempts: 1,
            max_attempts: 3,
            payload: json!({"msisdn": "90000001"}),
            last_error: None,
            started_at: None,
            completed_at: None,
        }
    }

    #[test]
    fn durations_match_oracle() {
        assert_eq!(task_duration("HLR_PROVISION"), 0.5);
        assert_eq!(task_duration("PCRF_POLICY_PUSH"), 0.3);
        assert_eq!(task_duration("OCS_BALANCE_INIT"), 0.2);
        assert_eq!(task_duration("ESIM_PROFILE_PREPARE"), 0.4);
        assert_eq!(task_duration("HLR_DEPROVISION"), 0.4);
        assert_eq!(task_duration("UNKNOWN"), 0.5); // default
    }

    #[test]
    fn should_fire_bounds() {
        assert!(!should_fire(0.0)); // random() in [0,1) is never < 0
        assert!(should_fire(1.0)); // random() in [0,1) is always < 1
    }

    #[test]
    fn completed_payload_has_completed_at() {
        let mut t = base_task("completed");
        t.completed_at = Some("2026-07-11T21:00:00+00:00".parse().unwrap());
        let p = task_event_payload(&t, "SO-0001", "ORD-0001", false);
        assert_eq!(p["taskId"], "PTK-0001");
        assert_eq!(p["serviceOrderId"], "SO-0001");
        assert_eq!(p["commercialOrderId"], "ORD-0001");
        assert_eq!(p["completedAt"], "2026-07-11T21:00:00+00:00");
        assert!(p.get("lastError").is_none());
        assert!(p.get("permanent").is_none());
    }

    #[test]
    fn failed_payload_has_last_error_and_permanent() {
        let mut t = base_task("failed");
        t.attempts = 3;
        t.last_error = Some("Simulated fail_always for HLR_PROVISION".into());
        let p = task_event_payload(&t, "SO-0001", "ORD-0001", true);
        assert_eq!(p["lastError"], "Simulated fail_always for HLR_PROVISION");
        assert_eq!(p["permanent"], true);
        assert_eq!(p["attempts"], 3);
        assert!(p.get("completedAt").is_none());
    }

    #[test]
    fn stuck_payload_has_started_at() {
        let mut t = base_task("stuck");
        t.started_at = Some("2026-07-11T21:00:00+00:00".parse().unwrap());
        let p = task_event_payload(&t, "SO-0001", "ORD-0001", false);
        assert_eq!(p["startedAt"], "2026-07-11T21:00:00+00:00");
        assert!(p.get("permanent").is_none());
    }

    #[test]
    fn failed_payload_empty_error_when_none() {
        let t = base_task("failed");
        let p = task_event_payload(&t, "SO-0001", "ORD-0001", false);
        assert_eq!(p["lastError"], "");
        assert_eq!(p["permanent"], false);
    }

    // ── policies — port test_task_api.py's policy assertions ────────────────

    #[test]
    fn retry_allowed_passes_under_budget() {
        check_retry_allowed(0, 3, "PTK-1").unwrap();
        check_retry_allowed(2, 3, "PTK-1").unwrap();
    }

    #[test]
    fn retry_allowed_rejects_at_budget() {
        let err = check_retry_allowed(3, 3, "PTK-1").unwrap_err();
        assert_eq!(err.rule, "provisioning_task.retry.max_attempts");
        assert_eq!(err.context["attempts"], 3);
    }

    #[test]
    fn resolve_note_required() {
        check_resolve_has_note("fixed it", "PTK-1").unwrap();
        for empty in ["", "   ", "\t"] {
            let err = check_resolve_has_note(empty, "PTK-1").unwrap_err();
            assert_eq!(err.rule, "provisioning.resolve_stuck.requires_note");
        }
    }

    #[test]
    fn fault_permission_passes_with_default_wildcard_ctx() {
        // Default ctx has permissions=["*"] → the v0.1 stub always passes.
        check_fault_injection_permission(&RequestCtx::default()).unwrap();
    }

    #[test]
    fn fault_permission_rejects_without_wildcard() {
        let ctx = RequestCtx {
            permissions: vec!["some.other.perm".into()],
            ..RequestCtx::default()
        };
        let err = check_fault_injection_permission(&ctx).unwrap_err();
        assert_eq!(err.rule, "provisioning.set_fault_injection.admin_only");
    }
}
