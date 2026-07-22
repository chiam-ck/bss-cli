//! Deterministic action registry — port of `cli/bss_cli/scenarios/actions.py`.
//!
//! A scenario `action:` maps 1:1 onto an orchestrator tool for the common case
//! (`customer.create` → the registry). The scenario-only verbs — operational fan-outs
//! and hero-scenario helpers not appropriate for the LLM tool surface — are handled
//! here instead and shadow any same-named tool:
//!
//! * `admin.reset_operational_data`, `clock.{freeze,unfreeze,advance}` — fan-outs.
//! * `audit.{events,count}_by_identity` — perimeter-identity audit pivots (v0.9).
//! * `portal.write_demo_contact` — a named-token (`portal_self_serve`) write (v0.9).
//! * `portal.{link_identity_to_customer,mint_test_session}` — portal_auth DB seeding
//!   (v0.10/v0.12) so chat/post-login scenarios skip the OTP funnel.
//! * `portal.run_chat_turn` — one chat turn end-to-end (POST → SSE drain) (v0.12).
//!
//! Unknown actions error (the runner surfaces it as a step failure).

use std::sync::Arc;

use bss_clients::{AdminClient, AuthProvider, NamedTokenAuthProvider, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolRegistry};
use fancy_regex::Regex;
use serde_json::{json, Map, Value};

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

/// A required string arg, or `None` (the caller maps to a scenario error).
fn str_arg(args: &Map<String, Value>, key: &str) -> Option<String> {
    args.get(key).and_then(Value::as_str).map(str::to_string)
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
            "audit.events_by_identity" => {
                let events = self.audit_events(args).await?;
                Ok(Value::Array(events))
            }
            "audit.count_by_identity" => {
                let events = self.audit_events(args).await?;
                let identity = str_arg(args, "service_identity").unwrap_or_default();
                Ok(json!({ "identity": identity, "count": events.len() }))
            }
            "portal.write_demo_contact" => self.portal_write_demo_contact(args).await,
            "portal.link_identity_to_customer" => self.portal_link_identity(args).await,
            "portal.mint_test_session" => self.portal_mint_test_session(args).await,
            "portal.run_chat_turn" => self.portal_run_chat_turn(args).await,
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

    // ── v0.9 audit pivots (scenario-only) ────────────────────────────────────

    /// `GET {crm}/audit-api/v1/events` filtered by `serviceIdentity` (+ optional
    /// aggregate / event-type-prefix). `audit.domain_event` is one shared table, so any
    /// service's audit router exposes the full log — CRM by convention. Returns the
    /// `events` array so `assert: any_match:` can pivot on individual rows.
    async fn audit_events(&self, args: &Map<String, Value>) -> Result<Vec<Value>, String> {
        let identity = str_arg(args, "service_identity")
            .ok_or_else(|| "audit.*_by_identity requires `service_identity`".to_string())?;
        let limit = args.get("limit").and_then(Value::as_i64).unwrap_or(200);
        let mut params: Vec<(String, String)> = vec![
            ("limit".to_string(), limit.to_string()),
            ("serviceIdentity".to_string(), identity),
        ];
        if let Some(v) = str_arg(args, "aggregate_type") {
            params.push(("aggregateType".to_string(), v));
        }
        if let Some(v) = str_arg(args, "aggregate_id") {
            params.push(("aggregateId".to_string(), v));
        }
        if let Some(v) = str_arg(args, "event_type_prefix") {
            params.push(("eventTypePrefix".to_string(), v));
        }
        let url = format!(
            "{}/audit-api/v1/events",
            env_or("BSS_CRM_URL", "http://localhost:8002")
        );
        let resp = self
            .http
            .get(&url)
            .header("X-BSS-API-Token", &self.token)
            .query(&params)
            .send()
            .await
            .map_err(|e| format!("audit query: {e}"))?;
        if !resp.status().is_success() {
            return Err(format!(
                "audit query: {} {}",
                resp.status().as_u16(),
                resp.text().await.unwrap_or_default()
            ));
        }
        let body: Value = resp.json().await.map_err(|e| format!("audit query: {e}"))?;
        Ok(body
            .get("events")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    // ── v0.9 named-token write (scenario-only) ───────────────────────────────

    /// A single contact-medium add carrying the `portal_self_serve` named token, so
    /// the receiving CRM resolves `service_identity="portal_self_serve"` — demonstrating
    /// distinct audit attribution. No fallback: failing loud is the point.
    async fn portal_write_demo_contact(&self, args: &Map<String, Value>) -> Result<Value, String> {
        let customer_id = str_arg(args, "customer_id")
            .ok_or_else(|| "portal.write_demo_contact requires `customer_id`".to_string())?;
        let medium_type = str_arg(args, "medium_type").unwrap_or_else(|| "email".to_string());
        let value =
            str_arg(args, "value").unwrap_or_else(|| "portal-demo@bss-cli.local".to_string());
        let auth = NamedTokenAuthProvider::from_env(
            "portal_self_serve",
            "BSS_PORTAL_SELF_SERVE_API_TOKEN",
            None,
        )
        .map_err(|e| e.to_string())?;
        let url = format!(
            "{}/tmf-api/customerManagement/v4/customer/{customer_id}/contactMedium",
            env_or("BSS_CRM_URL", "http://localhost:8002")
        );
        let mut req = self
            .http
            .post(&url)
            .json(&json!({ "mediumType": medium_type, "value": value }));
        for (k, v) in auth.headers() {
            req = req.header(k, v);
        }
        let resp = req.send().await.map_err(|e| e.to_string())?;
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        if !status.is_success() {
            return Err(format!(
                "portal-token contactMedium add failed: {} {text}",
                status.as_u16()
            ));
        }
        let result: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
        Ok(json!({
            "identity": auth.identity(),
            "customerId": customer_id,
            "result": result,
        }))
    }

    // ── v0.10/v0.12 portal DB helpers (scenario-only) ────────────────────────

    /// Seed / re-link a verified identity to a CRM customer (idempotent on email).
    /// Mirrors `bss_portal_auth.service.link_to_customer` but driven directly so a
    /// scenario can skip the agent-mediated signup funnel.
    async fn portal_link_identity(&self, args: &Map<String, Value>) -> Result<Value, String> {
        let email = str_arg(args, "email")
            .ok_or_else(|| "portal.link_identity_to_customer requires `email`".to_string())?;
        let customer_id = str_arg(args, "customer_id")
            .ok_or_else(|| "portal.link_identity_to_customer requires `customer_id`".to_string())?;
        let pool = db_pool().await?;
        let now = bss_clock::now();
        let existing: Option<String> =
            sqlx::query_scalar("SELECT id FROM portal_auth.identity WHERE email = $1")
                .bind(&email)
                .fetch_optional(&pool)
                .await
                .map_err(|e| e.to_string())?;
        let id = if let Some(id) = existing {
            sqlx::query(
                "UPDATE portal_auth.identity SET customer_id = $1, \
                 email_verified_at = COALESCE(email_verified_at, $2), status = 'registered' \
                 WHERE id = $3",
            )
            .bind(&customer_id)
            .bind(now)
            .bind(&id)
            .execute(&pool)
            .await
            .map_err(|e| e.to_string())?;
            id
        } else {
            let id = identity_id();
            sqlx::query(
                "INSERT INTO portal_auth.identity \
                 (id, email, customer_id, email_verified_at, status, created_at, last_login_at) \
                 VALUES ($1, $2, $3, $4, 'registered', $5, $5)",
            )
            .bind(&id)
            .bind(&email)
            .bind(&customer_id)
            .bind(now)
            .bind(now)
            .execute(&pool)
            .await
            .map_err(|e| e.to_string())?;
            id
        };
        Ok(json!({ "identityId": id, "email": email, "customerId": customer_id }))
    }

    /// Mint a verified linked-customer session in one call and return its id — the
    /// chat hero scenarios use this to skip the OTP round-trip. Not idempotent on email
    /// (re-running against a dirty DB fails to seed — use a fresh reset).
    async fn portal_mint_test_session(&self, args: &Map<String, Value>) -> Result<Value, String> {
        let email = str_arg(args, "email")
            .ok_or_else(|| "portal.mint_test_session requires `email`".to_string())?;
        let customer_id = str_arg(args, "customer_id")
            .ok_or_else(|| "portal.mint_test_session requires `customer_id`".to_string())?;
        let pool = db_pool().await?;
        let now = bss_clock::now();
        let ttl = env_or("BSS_PORTAL_SESSION_TTL_S", "86400")
            .parse::<i64>()
            .unwrap_or(86_400);
        let identity = identity_id();
        sqlx::query(
            "INSERT INTO portal_auth.identity \
             (id, email, customer_id, email_verified_at, status, created_at, last_login_at) \
             VALUES ($1, $2, $3, $4, 'registered', $5, $5)",
        )
        .bind(&identity)
        .bind(&email)
        .bind(&customer_id)
        .bind(now)
        .bind(now)
        .execute(&pool)
        .await
        .map_err(|e| e.to_string())?;

        let session = session_id();
        let expires = now + chrono::Duration::seconds(ttl);
        sqlx::query(
            "INSERT INTO portal_auth.session \
             (id, identity_id, issued_at, expires_at, last_seen_at) \
             VALUES ($1, $2, $3, $4, $3)",
        )
        .bind(&session)
        .bind(&identity)
        .bind(now)
        .bind(expires)
        .execute(&pool)
        .await
        .map_err(|e| e.to_string())?;

        Ok(json!({
            "session_id": session,
            "identity_id": identity,
            "email": email,
            "customer_id": customer_id,
        }))
    }

    // ── v0.12 chat turn (scenario-only) ──────────────────────────────────────

    /// Drive one chat turn end-to-end: POST `/chat/message`, follow the 303 to either a
    /// cap-trip or a live session, then drain the SSE log and pull the tool names the
    /// agent invoked. Returns the captured outcome a scenario asserts on.
    async fn portal_run_chat_turn(&self, args: &Map<String, Value>) -> Result<Value, String> {
        let portal_base = str_arg(args, "portal_base")
            .ok_or_else(|| "portal.run_chat_turn requires `portal_base`".to_string())?;
        let session_cookie = str_arg(args, "session_cookie")
            .ok_or_else(|| "portal.run_chat_turn requires `session_cookie`".to_string())?;
        let message = str_arg(args, "message")
            .ok_or_else(|| "portal.run_chat_turn requires `message`".to_string())?;
        let cookie = format!("bss_portal_session={session_cookie}");

        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|e| e.to_string())?;
        let post = client
            .post(format!("{portal_base}/chat/message"))
            .header("Cookie", &cookie)
            .form(&[("message", &message)])
            .send()
            .await
            .map_err(|e| e.to_string())?;
        if post.status().as_u16() != 303 {
            return Ok(json!({
                "ok": false, "session_id": Value::Null, "cap_tripped": Value::Null,
                "stream_status": "post_failed", "tool_calls": [],
                "error": format!("POST /chat/message returned {}", post.status().as_u16()),
            }));
        }
        let location = post
            .headers()
            .get("location")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        if location.contains("cap_tripped=") {
            let reason = capture_group(&location, r"cap_tripped=([^&]+)")
                .unwrap_or_else(|| "unknown".to_string());
            return Ok(json!({
                "ok": true, "session_id": Value::Null, "cap_tripped": reason,
                "stream_status": "cap_tripped", "tool_calls": [],
            }));
        }
        let Some(sid) = capture_group(&location, r"session=([0-9a-f]+)") else {
            return Ok(json!({
                "ok": false, "session_id": Value::Null, "cap_tripped": Value::Null,
                "stream_status": "no_session_in_redirect", "tool_calls": [],
                "error": format!("unexpected location: {}", &location.chars().take(80).collect::<String>()),
            }));
        };

        // Drain the SSE log (reqwest `.text()` reads to EOF = stream close on done/error).
        let body = client
            .get(format!("{portal_base}/chat/events/{sid}"))
            .header("Cookie", &cookie)
            .send()
            .await
            .map_err(|e| e.to_string())?
            .text()
            .await
            .map_err(|e| e.to_string())?;
        let (tool_calls, stream_status) = parse_chat_stream(&body);
        Ok(json!({
            "ok": stream_status == "done",
            "session_id": sid,
            "cap_tripped": Value::Null,
            "stream_status": stream_status,
            "tool_calls": tool_calls,
        }))
    }

    /// Fan out `reset-operational-data` to every service that owns operational data.
    /// Hard-fails on the first error — a half-reset is worse than none.
    async fn admin_reset(&self, reset_sequences: bool) -> Result<Value, String> {
        // Fires on the direct `bss scenario run-all` path too, where the Makefile's
        // banner never printed. By design, but it should never be a surprise.
        eprintln!(
            "⚠  admin.reset_operational_data: TRUNCATING all operational data \
             (customers, orders, subscriptions, payments, portal logins) — \
             audit history kept. This is by design; see the scenarios target in the Makefile."
        );
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
        let portal_auth = self.reset_portal_auth().await?;
        Ok(json!({
            "resetSequences": reset_sequences,
            "services": services,
            "portalAuth": portal_auth,
        }))
    }

    /// Clear the portal's login state as part of the same reset.
    ///
    /// `portal_auth` belongs to the portals, which are public web apps and
    /// deliberately expose no admin surface — so this goes through the same
    /// scenario-only `BSS_DB_URL` seam as the other `portal.*` verbs rather than
    /// putting a "delete every login" endpoint on a customer-facing port.
    ///
    /// Skipping it is not an option: `identity.customer_id` points into
    /// `crm.customer`, which the fan-out above truncates. Leaving logins behind
    /// produced identities bound to customers that no longer existed, which
    /// bricked signup for those accounts until v2.1.1 taught it to self-heal
    /// (see DECISIONS 2026-07-23). A partial wipe is a broken state, not a
    /// conservative one.
    ///
    /// `portal_action` is deliberately kept — it is the portal's audit trail, and
    /// audit history survives a reset by the same rule that spares
    /// `audit.domain_event` (callers filter on `occurred_at >= resetAt`).
    async fn reset_portal_auth(&self) -> Result<Value, String> {
        let pool = db_pool()
            .await
            .map_err(|e| format!("admin.reset_operational_data cannot clear portal_auth: {e}"))?;
        // One statement so FK order is Postgres's problem; CASCADE stays inside
        // portal_auth (every FK into these tables is a sibling).
        let truncated = [
            "step_up_pending_action",
            "email_change_pending",
            "login_token",
            "login_attempt",
            "session",
            "identity",
        ];
        let sql = format!(
            "TRUNCATE TABLE {} RESTART IDENTITY CASCADE",
            truncated
                .iter()
                .map(|t| format!("portal_auth.\"{t}\""))
                .collect::<Vec<_>>()
                .join(", ")
        );
        sqlx::query(&sql)
            .execute(&pool)
            .await
            .map_err(|e| format!("admin.reset_operational_data failed on portal_auth: {e}"))?;
        Ok(json!({ "schema": "portal_auth", "truncated": truncated, "kept": ["portal_action"] }))
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

// ── free helpers for the scenario-only verbs ─────────────────────────────────

/// Connect to Postgres for the portal DB verbs. `BSS_DB_URL` is required.
async fn db_pool() -> Result<bss_db::PgPool, String> {
    let url = env_or("BSS_DB_URL", "");
    if url.is_empty() {
        return Err("BSS_DB_URL must be set for portal.* DB actions".to_string());
    }
    bss_db::connect(&url).await.map_err(|e| e.to_string())
}

/// `IDT-<12 hex>` — the portal identity id shape (Python's `_identity_id`).
fn identity_id() -> String {
    format!("IDT-{:012x}", rand::random::<u64>() & 0xffff_ffff_ffff)
}

/// A 32-char opaque session id (cookie value).
fn session_id() -> String {
    format!("{:032x}", rand::random::<u128>())
}

/// The first capture group of `pattern` in `text`, if any.
fn capture_group(text: &str, pattern: &str) -> Option<String> {
    let re = Regex::new(pattern).ok()?;
    re.captures(text)
        .ok()
        .flatten()
        .and_then(|c| c.get(1))
        .map(|m| m.as_str().to_string())
}

/// Parse the drained chat SSE body: pull dotted tool names out of the agent-log
/// `class="title">` spans (in first-seen order) and detect the `done` / `error`
/// terminal dot. Port of `portal_run_chat_turn`'s frame loop.
fn parse_chat_stream(body: &str) -> (Vec<String>, String) {
    let mut tool_calls: Vec<String> = Vec::new();
    let mut status = "stream_closed".to_string();
    let title_re = Regex::new(r#"class="title">([a-z_.]+(?:_(?:mine|for_me))?)"#).ok();
    for frame in body.split("\n\n") {
        let Some((_, data)) = frame.split_once("data: ") else {
            continue;
        };
        if let Some(re) = &title_re {
            for caps in re.captures_iter(data).flatten() {
                if let Some(m) = caps.get(1) {
                    let candidate = m.as_str().to_string();
                    if candidate.contains('.') && !tool_calls.contains(&candidate) {
                        tool_calls.push(candidate);
                    }
                }
            }
        }
        if data.contains("done") && data.contains(r#"class="dot done""#) {
            status = "done".to_string();
            break;
        }
        if data.contains("error") && data.contains(r#"class="dot error""#) {
            status = "error".to_string();
            break;
        }
    }
    (tool_calls, status)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn capture_group_extracts_first() {
        assert_eq!(
            capture_group("/signup?session=deadbeef&x=1", r"session=([0-9a-f]+)").unwrap(),
            "deadbeef"
        );
        assert_eq!(
            capture_group("/x?cap_tripped=hourly", r"cap_tripped=([^&]+)").unwrap(),
            "hourly"
        );
        assert!(capture_group("/nope", r"session=([0-9a-f]+)").is_none());
    }

    #[test]
    fn parse_chat_stream_pulls_tools_and_done() {
        let body = concat!(
            "event: log\n",
            "data: <div class=\"title\">customer.get_mine</div>\n\n",
            "event: log\n",
            "data: <div class=\"title\">subscription.list_mine</div>\n\n",
            "event: status\n",
            "data: <span class=\"dot done\"></span>done\n\n",
        );
        let (tools, status) = parse_chat_stream(body);
        assert_eq!(tools, vec!["customer.get_mine", "subscription.list_mine"]);
        assert_eq!(status, "done");
    }

    #[test]
    fn parse_chat_stream_detects_error_and_dedups() {
        let body = concat!(
            "data: <div class=\"title\">customer.get_mine</div>\n\n",
            "data: <div class=\"title\">customer.get_mine</div>\n\n",
            "data: <span class=\"dot error\"></span>error\n\n",
        );
        let (tools, status) = parse_chat_stream(body);
        assert_eq!(tools, vec!["customer.get_mine"]);
        assert_eq!(status, "error");
    }
}
