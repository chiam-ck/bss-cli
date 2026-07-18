//! Deterministic action registry — port of `cli/bss_cli/scenarios/actions.py`.
//!
//! A scenario `action:` maps 1:1 onto an orchestrator tool for the common case
//! (`customer.create` → the registry). A few scenario-only verbs — operational
//! fan-outs not appropriate for the LLM tool surface — are handled here instead:
//! `admin.reset_operational_data` and `clock.freeze` / `clock.unfreeze` /
//! `clock.advance`. Unknown actions error (the runner surfaces it as a step failure).
//!
//! **This slice:** the reset + clock fan-outs and tool passthrough (the deterministic
//! core). `audit.*` (perimeter-identity pivots) and the `portal.*` verbs (portal/DB
//! helpers for the HTTP hero scenarios) land with the HTTP-step slice; until then they
//! return a clear "not wired yet" error so a scenario using them fails at that step.

use std::sync::Arc;

use bss_clients::{AdminClient, AuthProvider, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolRegistry};
use serde_json::{json, Map, Value};

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

/// A service that owns operational data or a scenario clock: label + base URL.
struct Target {
    label: &'static str,
    url: String,
}

fn reset_targets() -> Vec<Target> {
    [
        ("mediation", "BSS_MEDIATION_URL", "http://localhost:8007"),
        (
            "subscription",
            "BSS_SUBSCRIPTION_URL",
            "http://localhost:8006",
        ),
        ("som", "BSS_SOM_URL", "http://localhost:8005"),
        ("com", "BSS_COM_URL", "http://localhost:8004"),
        (
            "provisioning-sim",
            "BSS_PROVISIONING_URL",
            "http://localhost:8010",
        ),
        ("payment", "BSS_PAYMENT_URL", "http://localhost:8003"),
        ("crm", "BSS_CRM_URL", "http://localhost:8002"),
    ]
    .into_iter()
    .map(|(label, var, def)| Target {
        label,
        url: env_or(var, def),
    })
    .collect()
}

fn clock_targets() -> Vec<Target> {
    [
        ("crm", "BSS_CRM_URL", "http://localhost:8002"),
        ("payment", "BSS_PAYMENT_URL", "http://localhost:8003"),
        ("com", "BSS_COM_URL", "http://localhost:8004"),
        ("som", "BSS_SOM_URL", "http://localhost:8005"),
        (
            "subscription",
            "BSS_SUBSCRIPTION_URL",
            "http://localhost:8006",
        ),
        ("mediation", "BSS_MEDIATION_URL", "http://localhost:8007"),
        ("rating", "BSS_RATING_URL", "http://localhost:8008"),
        (
            "provisioning-sim",
            "BSS_PROVISIONING_URL",
            "http://localhost:8010",
        ),
        ("catalog", "BSS_CATALOG_URL", "http://localhost:8001"),
    ]
    .into_iter()
    .map(|(label, var, def)| Target {
        label,
        url: env_or(var, def),
    })
    .collect()
}

/// The scenario action surface: the orchestrator tool registry plus the scenario-only
/// fan-out verbs. Built once per `run_scenario`; `actor` carries the `scenario:<name>`
/// attribution onto each tool call's [`ToolCtx`].
pub struct Actions {
    registry: ToolRegistry,
    actor: String,
    tenant: String,
    token: String,
    http: reqwest::Client,
}

impl Actions {
    pub fn new(registry: ToolRegistry, actor: String, tenant: String) -> Self {
        Actions {
            registry,
            actor,
            tenant,
            token: env_or("BSS_API_TOKEN", ""),
            http: reqwest::Client::new(),
        }
    }

    /// Whether `name` resolves to any action (scenario verb or registered tool) —
    /// used by the runner to fail a step loud on an unknown action name.
    pub fn is_known(&self, name: &str) -> bool {
        matches!(
            name,
            "admin.reset_operational_data"
                | "clock.freeze"
                | "clock.unfreeze"
                | "clock.advance"
                | "audit.events_by_identity"
                | "audit.count_by_identity"
                | "portal.write_demo_contact"
                | "portal.link_identity_to_customer"
                | "portal.run_chat_turn"
                | "portal.mint_test_session"
        ) || self.registry.get(name).is_some()
    }

    /// Run an action by name with interpolated `args`. Scenario verbs win over
    /// same-named tools (Python's `_SCENARIO_ACTIONS` shadow the registry). Errors are
    /// display strings the runner reports.
    pub async fn run(&self, name: &str, args: &Map<String, Value>) -> Result<Value, String> {
        match name {
            "admin.reset_operational_data" => {
                let reset_sequences = args
                    .get("reset_sequences")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                self.admin_reset(reset_sequences).await
            }
            "clock.freeze" => {
                let payload = match args.get("at").and_then(Value::as_str) {
                    Some(at) if !at.is_empty() => json!({ "at": at }),
                    _ => json!({}),
                };
                self.clock_fanout("/clock/freeze", payload).await
            }
            "clock.unfreeze" => self.clock_fanout("/clock/unfreeze", json!({})).await,
            "clock.advance" => {
                let duration = args
                    .get("duration")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "clock.advance requires `duration`".to_string())?;
                self.clock_fanout("/clock/advance", json!({ "duration": duration }))
                    .await
            }
            "audit.events_by_identity"
            | "audit.count_by_identity"
            | "portal.write_demo_contact"
            | "portal.link_identity_to_customer"
            | "portal.run_chat_turn"
            | "portal.mint_test_session" => Err(format!(
                "action {name:?} is not wired yet — it lands with the HTTP/portal \
                 scenario slice"
            )),
            _ => self.run_tool(name, args).await,
        }
    }

    /// Dispatch a registered orchestrator tool. Attributes the call to the scenario
    /// via [`ToolCtx`]; the ambient `bss_context` scope (set by the runner) carries
    /// `channel="scenario"` onto the outbound HTTP.
    async fn run_tool(&self, name: &str, args: &Map<String, Value>) -> Result<Value, String> {
        let tool = self
            .registry
            .get(name)
            .ok_or_else(|| format!("unknown action: {name:?}"))?;
        let ctx = ToolCtx {
            actor: self.actor.clone(),
            channel: "scenario".to_string(),
            tenant: self.tenant.clone(),
            transcript: String::new(),
        };
        (tool.func)(Value::Object(args.clone()), ctx)
            .await
            .map_err(|e| e.to_observation())
    }

    /// Fan out `reset-operational-data` to every service that owns operational data.
    /// Hard-fails on the first error — a half-reset is worse than none.
    async fn admin_reset(&self, reset_sequences: bool) -> Result<Value, String> {
        let auth: Arc<dyn AuthProvider> =
            Arc::new(TokenAuthProvider::new(self.token.clone()).map_err(|e| e.to_string())?);
        let mut services = Vec::new();
        for target in reset_targets() {
            let client = AdminClient::new(target.url, auth.clone()).map_err(|e| e.to_string())?;
            let body = client.reset_operational_data().await.map_err(|e| {
                format!(
                    "admin.reset_operational_data failed on {}: {e}",
                    target.label
                )
            })?;
            services.push(json!({ "service": target.label, "ok": true, "body": body }));
        }
        Ok(json!({ "resetSequences": reset_sequences, "services": services }))
    }

    /// POST `/admin-api/v1{path}` to every clock-equipped service. Hard-fails on any
    /// non-2xx so a partial clock state can't silently skew later steps.
    async fn clock_fanout(&self, path: &str, payload: Value) -> Result<Value, String> {
        let mut per_service = Vec::new();
        for target in clock_targets() {
            let url = format!("{}/admin-api/v1{path}", target.url);
            let resp = self
                .http
                .post(&url)
                .header("X-BSS-API-Token", &self.token)
                .json(&payload)
                .send()
                .await
                .map_err(|e| format!("{} {path}: {e}", target.label))?;
            let status = resp.status();
            let body_text = resp.text().await.unwrap_or_default();
            if !status.is_success() {
                return Err(format!(
                    "{} {path}: {} {body_text}",
                    target.label,
                    status.as_u16()
                ));
            }
            let body: Value = serde_json::from_str(&body_text).unwrap_or(Value::Null);
            per_service.push(json!({ "service": target.label, "body": body }));
        }
        Ok(json!({ "path": path, "payload": payload, "services": per_service }))
    }
}
