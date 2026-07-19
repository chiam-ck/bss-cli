//! Destructive-operation gating for LLM tool calls. Port of
//! `orchestrator/bss_orchestrator/safety.py`.
//!
//! Some tools can't be undone with a follow-up call. If the human hasn't passed
//! `allow_destructive`, they return a *structured* error rather than executing:
//!
//! ```json
//! {"error": "DESTRUCTIVE_OPERATION_BLOCKED", "tool": "...", "message": "..."}
//! ```
//!
//! v1.5 autonomy: when `allow_destructive=true`, `batched` authorises the whole
//! loop after the first destructive fires; `granular` (the cockpit default)
//! re-gates each destructive so the operator must `/confirm` for each one.

use serde_json::{json, Value};

/// Every destructive tool in the registry (dotted LLM-facing name). Adding a name
/// here is a doctrine decision (requires reviewing the safety contract).
pub const DESTRUCTIVE_TOOLS: &[&str] = &[
    "customer.close",
    "customer.remove_contact_medium",
    "case.close",
    "ticket.cancel",
    "payment.remove_method",
    "order.cancel",
    "subscription.terminate",
    // v0.12 — chat-surface wrapper around subscription.terminate. Gating at the
    // wrapper too keeps the destructive contract honest.
    "subscription.terminate_mine",
    "provisioning.set_fault_injection",
    "admin.reset_operational_data",
    "admin.force_state",
];

/// Autonomy mode for destructive gating when `allow_destructive=true`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutonomyMode {
    /// One authorisation covers the whole loop (pre-v1.5 behaviour).
    Batched,
    /// Re-gate each destructive after the first fires (cockpit default).
    Granular,
}

/// True if `tool_name` (dotted) is gated.
pub fn is_destructive(tool_name: &str) -> bool {
    DESTRUCTIVE_TOOLS.contains(&tool_name)
}

/// Per-graph mutable state shared across destructive gating in one loop so
/// granular mode can observe "has any destructive fired yet?".
#[derive(Debug, Default)]
pub struct LoopState {
    pub destructive_executed: u32,
}

/// The structured block the LLM sees when a destructive tool is refused.
pub fn blocked_response(tool_name: &str) -> Value {
    json!({
        "error": "DESTRUCTIVE_OPERATION_BLOCKED",
        "tool": tool_name,
        "message": format!(
            "Tool {tool_name:?} is destructive and requires operator /confirm. \
             Propose this tool to the operator by stopping after your proposal; \
             the next /confirm-bracketed turn will execute it."
        ),
    })
}

/// Decide whether a destructive tool call may execute this turn, mutating
/// `state` to record a fire. Non-destructive tools always pass. Returns `Err`
/// carrying the structured block when refused.
///
/// Mirrors `wrap_destructive`'s runtime check: `allow_destructive=false` always
/// blocks; in granular mode a second destructive re-blocks.
pub fn gate_destructive(
    tool_name: &str,
    allow_destructive: bool,
    autonomy: AutonomyMode,
    state: &mut LoopState,
) -> Result<(), Value> {
    if !is_destructive(tool_name) {
        return Ok(());
    }
    if !allow_destructive {
        return Err(blocked_response(tool_name));
    }
    if autonomy == AutonomyMode::Granular && state.destructive_executed >= 1 {
        return Err(blocked_response(tool_name));
    }
    state.destructive_executed += 1;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_destructive_always_passes() {
        let mut st = LoopState::default();
        assert!(gate_destructive("customer.get", false, AutonomyMode::Granular, &mut st).is_ok());
    }

    #[test]
    fn blocked_when_not_allowed() {
        let mut st = LoopState::default();
        let r = gate_destructive(
            "subscription.terminate",
            false,
            AutonomyMode::Batched,
            &mut st,
        );
        assert!(r.is_err());
        assert_eq!(st.destructive_executed, 0);
    }

    #[test]
    fn batched_authorises_the_whole_loop() {
        let mut st = LoopState::default();
        // Every destructive fires once allowed.
        assert!(gate_destructive(
            "subscription.terminate",
            true,
            AutonomyMode::Batched,
            &mut st
        )
        .is_ok());
        assert!(gate_destructive("order.cancel", true, AutonomyMode::Batched, &mut st).is_ok());
        assert_eq!(st.destructive_executed, 2);
    }

    #[test]
    fn granular_regates_after_first_fire() {
        let mut st = LoopState::default();
        // First destructive passes; the second re-blocks until a fresh /confirm.
        assert!(gate_destructive(
            "subscription.terminate",
            true,
            AutonomyMode::Granular,
            &mut st
        )
        .is_ok());
        assert!(gate_destructive("order.cancel", true, AutonomyMode::Granular, &mut st).is_err());
        assert_eq!(st.destructive_executed, 1);
    }
}
