//! `AuditClient` — typed client for the shared `audit.domain_event` query surface.
//!
//! Port of the read surface of `bss_clients.audit.AuditClient`. Each BSS service
//! exposes `/audit-api/v1/events` over its own schema; the client is pointed at a
//! specific service's base URL (com for orders, subscription for subscriptions).
//! Only [`AuditClient::list_events`] is ported — it backs the orchestrator's
//! `trace.for_order` / `trace.for_subscription` trace-resolution tools.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::Value;

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for a service's audit-event query API. Wraps [`BssClient`].
#[derive(Clone)]
pub struct AuditClient {
    inner: BssClient,
}

impl AuditClient {
    pub fn new(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
    ) -> Result<Self, ClientError> {
        Self::with_timeout(base_url, auth, DEFAULT_TIMEOUT)
    }

    pub fn with_timeout(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
        timeout: Duration,
    ) -> Result<Self, ClientError> {
        Ok(AuditClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /audit-api/v1/events` with optional `aggregateType` / `aggregateId`
    /// filters (`limit` always sent first, matching Python's `params={"limit":
    /// limit}` seed). Returns the **unwrapped** event list (`body["events"]`),
    /// ordered by `occurredAt` ascending. Backs the trace-resolution tools; the
    /// wider filter set (eventType/occurredSince/serviceIdentity/…) lands when a
    /// consumer needs it.
    pub async fn list_events(
        &self,
        aggregate_type: Option<&str>,
        aggregate_id: Option<&str>,
        limit: i64,
    ) -> Result<Value, ClientError> {
        let mut params = vec![format!("limit={limit}")];
        if let Some(t) = aggregate_type.filter(|s| !s.is_empty()) {
            params.push(format!("aggregateType={}", encode(t)));
        }
        if let Some(i) = aggregate_id.filter(|s| !s.is_empty()) {
            params.push(format!("aggregateId={}", encode(i)));
        }
        let path = format!("/audit-api/v1/events?{}", params.join("&"));
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        let body: Value = resp
            .json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))?;
        // Unwrap the envelope: `{"events": [...]}` → the array (empty when absent).
        Ok(body
            .get("events")
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new())))
    }
}

/// Minimal query-value encoding (mirrors `catalog::encode`).
fn encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'%' => out.push_str("%25"),
            b'&' => out.push_str("%26"),
            b'+' => out.push_str("%2B"),
            b'=' => out.push_str("%3D"),
            b'#' => out.push_str("%23"),
            b' ' => out.push_str("%20"),
            _ => out.push(b as char),
        }
    }
    out
}
