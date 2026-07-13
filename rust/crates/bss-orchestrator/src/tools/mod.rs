//! Tool registry + profiles. Port of `orchestrator/bss_orchestrator/tools/`
//! (`_registry.py` + `_profiles.py`) and the `_LLM_HIDDEN_TOOLS` set from
//! `graph.py`.
//!
//! Tools stay *dumb*: no retries, no business logic — each wraps a downstream
//! call. A tool is an async function `Fn(Value, ToolCtx) -> Result<Value,
//! ToolError>`; the registry maps the dotted LLM name to it, matching the names
//! used by [`crate::safety::DESTRUCTIVE_TOOLS`] and `TOOL_SURFACE.md`.

pub mod catalog;
pub mod clock;
pub mod customer;
pub mod payment;
pub mod subscription;

use std::collections::BTreeMap;
use std::sync::Arc;

use bss_clients::ClientError;
use futures_util::future::BoxFuture;
use serde_json::{json, Value};

/// Request context threaded into every tool call (actor/channel/tenant for
/// downstream attribution). Unused by pure tools like `clock.*`.
#[derive(Debug, Clone)]
pub struct ToolCtx {
    pub actor: String,
    pub channel: String,
    pub tenant: String,
}

impl Default for ToolCtx {
    fn default() -> Self {
        Self {
            actor: "system".to_string(),
            channel: "system".to_string(),
            tenant: "DEFAULT".to_string(),
        }
    }
}

/// A structured tool failure, converted to an LLM-readable observation string.
/// Port of `graph._tool_error_to_observation`.
#[derive(Debug, Clone)]
pub enum ToolError {
    Policy { rule: String, detail: Value },
    Client { status: i64, detail: Value },
    Other { kind: String, detail: String },
}

impl ToolError {
    /// The observation string the ReAct loop feeds back as the tool result. The
    /// `"error":"<CODE>"` fragment is what the loop's failure-bail counter keys on.
    pub fn to_observation(&self) -> String {
        match self {
            ToolError::Policy { rule, detail } => json!({
                "error": "POLICY_VIOLATION", "rule": rule, "detail": detail
            })
            .to_string(),
            ToolError::Client { status, detail } => json!({
                "error": "CLIENT_ERROR", "status": status, "detail": detail
            })
            .to_string(),
            ToolError::Other { kind, detail } => json!({
                "error": kind, "detail": detail
            })
            .to_string(),
        }
    }
}

/// Map a `ClientError` to the structured observation the LLM reads, matching
/// `graph._tool_error_to_observation`: policy violations surface `rule` + detail;
/// everything else surfaces `CLIENT_ERROR` + the HTTP status. Shared by every
/// client-backed tool family.
pub(crate) fn map_client_err(e: ClientError) -> ToolError {
    match e {
        ClientError::Policy(pv) => ToolError::Policy {
            rule: pv.rule.clone(),
            detail: pv.to_wire(),
        },
        other => ToolError::Client {
            status: other.status_code() as i64,
            detail: Value::String(other.to_string()),
        },
    }
}

/// A required string arg, or a structured `BadArgs` observation when absent.
pub(crate) fn req_str(args: &Value, key: &str) -> Result<String, ToolError> {
    args.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| ToolError::Other {
            kind: "BadArgs".to_string(),
            detail: format!("missing required argument {key:?}"),
        })
}

/// An optional non-empty string arg.
pub(crate) fn opt_str(args: &Value, key: &str) -> Option<String> {
    args.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// A registered tool: dotted name, LLM-facing description (the semantic contract),
/// and the async implementation.
pub type ToolFn =
    Arc<dyn Fn(Value, ToolCtx) -> BoxFuture<'static, Result<Value, ToolError>> + Send + Sync>;

#[derive(Clone)]
pub struct RegisteredTool {
    pub name: String,
    pub description: String,
    pub func: ToolFn,
}

/// The LLM-visible spec for a tool (what the model sees). Schema derivation
/// (schemars, D5) lands with the typed arg structs in a later slice.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
}

/// The single chokepoint collecting every LLM-callable function.
#[derive(Default, Clone)]
pub struct ToolRegistry {
    tools: BTreeMap<String, RegisteredTool>,
}

impl ToolRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register `tool` under its dotted name. Panics on a duplicate (Python
    /// raises at import time — same fail-fast intent).
    pub fn register(&mut self, tool: RegisteredTool) {
        if self.tools.contains_key(&tool.name) {
            panic!("Duplicate tool registration: {:?}", tool.name);
        }
        self.tools.insert(tool.name.clone(), tool);
    }

    pub fn get(&self, name: &str) -> Option<&RegisteredTool> {
        self.tools.get(name)
    }

    /// All registered names, sorted (BTreeMap iterates sorted).
    pub fn list(&self) -> Vec<&str> {
        self.tools.keys().map(String::as_str).collect()
    }

    /// The LLM-visible tool specs: registered ∩ (profile or all) minus the
    /// hidden set, sorted. Mirrors `graph.build_tools`.
    pub fn surface(&self, profile: Option<&str>) -> Vec<ToolSpec> {
        let allowed: Option<&[&str]> = profile.map(profile_tools);
        self.tools
            .values()
            .filter(|t| !LLM_HIDDEN_TOOLS.contains(&t.name.as_str()))
            .filter(|t| allowed.map_or(true, |set| set.contains(&t.name.as_str())))
            .map(|t| ToolSpec {
                name: t.name.clone(),
                description: t.description.clone(),
            })
            .collect()
    }
}

/// Tools present in the registry (so scenarios can call them) but intentionally
/// NOT shown to the LLM (scenario-scaffolding + catalog/price admin writes).
/// Port of `graph._LLM_HIDDEN_TOOLS`.
pub const LLM_HIDDEN_TOOLS: &[&str] = &[
    "usage.simulate",
    "catalog.add_offering",
    "catalog.add_price",
    "catalog.window_offering",
    "subscription.migrate_to_new_price",
];

/// Return the tool-name set for a profile (`customer_self_serve` /
/// `operator_cockpit`), or an empty slice for an unknown profile. Port of
/// `_profiles.TOOL_PROFILES`. The full 109-tool coverage validation
/// (`validate_profiles`) lands when the tool families do; `surface()` already
/// intersects with the registered set so a partial registry is safe.
pub fn profile_tools(profile: &str) -> &'static [&'static str] {
    match profile {
        "customer_self_serve" => CUSTOMER_SELF_SERVE,
        "operator_cockpit" => OPERATOR_COCKPIT,
        _ => &[],
    }
}

/// The chat-surface profile — public catalog reads + `*.mine`/`*_for_me`
/// ownership-bound wrappers only.
pub const CUSTOMER_SELF_SERVE: &[&str] = &[
    "catalog.list_vas",
    "catalog.list_active_offerings",
    "catalog.get_offering",
    "subscription.list_mine",
    "subscription.get_mine",
    "subscription.get_balance_mine",
    "subscription.get_lpa_mine",
    "usage.history_mine",
    "customer.get_mine",
    "payment.method_list_mine",
    "payment.charge_history_mine",
    "vas.purchase_for_me",
    "subscription.schedule_plan_change_mine",
    "subscription.cancel_pending_plan_change_mine",
    "subscription.terminate_mine",
    "case.open_for_me",
    "case.list_for_me",
];

/// The operator cockpit profile — full registry coverage minus the customer-side
/// `*.mine` wrappers. A coverage assertion, not a restriction set.
pub const OPERATOR_COCKPIT: &[&str] = &[
    // reads
    "customer.get",
    "customer.list",
    "customer.find_by_msisdn",
    "customer.find_by_email",
    "customer.get_kyc_status",
    "case.get",
    "case.list",
    "case.show_transcript_for",
    "ticket.get",
    "ticket.list",
    "interaction.list",
    "catalog.list_active_offerings",
    "catalog.list_offerings",
    "catalog.get_offering",
    "catalog.get_active_price",
    "catalog.list_vas",
    "catalog.get_vas",
    "subscription.list_for_customer",
    "subscription.get",
    "subscription.get_balance",
    "subscription.get_esim_activation",
    "service.get",
    "service.list_for_subscription",
    "order.get",
    "order.list",
    "order.wait_until",
    "service_order.get",
    "service_order.list_for_order",
    "payment.list_methods",
    "payment.list_attempts",
    "payment.get_attempt",
    "inventory.msisdn.list_available",
    "inventory.msisdn.count",
    "inventory.msisdn.get",
    "inventory.esim.list_available",
    "inventory.esim.get_activation",
    "provisioning.get_task",
    "provisioning.list_tasks",
    "usage.history",
    "trace.get",
    "trace.for_order",
    "trace.for_subscription",
    "events.list",
    "agents.list",
    "clock.now",
    // writes
    "customer.create",
    "customer.update_contact",
    "customer.add_contact_medium",
    "customer.remove_contact_medium",
    "customer.attest_kyc",
    "customer.close",
    "interaction.log",
    "case.open",
    "case.close",
    "case.add_note",
    "case.transition",
    "case.update_priority",
    "ticket.open",
    "ticket.assign",
    "ticket.transition",
    "ticket.resolve",
    "ticket.close",
    "ticket.cancel",
    "catalog.add_offering",
    "catalog.add_price",
    "catalog.window_offering",
    "promo.create",
    "promo.assign",
    "promo.show",
    "subscription.terminate",
    "subscription.schedule_plan_change",
    "subscription.cancel_pending_plan_change",
    "subscription.migrate_to_new_price",
    "subscription.purchase_vas",
    "subscription.renew_now",
    "subscription.tick_renewals_now",
    "order.create",
    "order.cancel",
    "payment.add_card",
    "payment.remove_method",
    "payment.charge",
    "inventory.msisdn.add_range",
    "port_request.list",
    "port_request.get",
    "port_request.create",
    "port_request.approve",
    "port_request.reject",
    "provisioning.resolve_stuck",
    "provisioning.retry_failed",
    "provisioning.set_fault_injection",
    "clock.advance",
    "clock.freeze",
    "clock.unfreeze",
    "usage.simulate",
    // knowledge (operator_cockpit only; gated on BSS_KNOWLEDGE_ENABLED in Python)
    "knowledge.search",
    "knowledge.get",
];
