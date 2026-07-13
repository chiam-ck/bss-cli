//! `MediationClient` — typed client for the Mediation service (TMF635 usage).
//!
//! Port of the read surface of `bss_clients.mediation.MediationClient`. Only
//! [`MediationClient::list_usage`] is ported here — it backs the orchestrator's
//! `usage.history` (operator) and, later, the `usage.history_mine` chat wrapper.
//! The `submit_usage`/`get_usage` calls land when a consumer needs them.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::{json, Value};

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the Mediation service. Wraps [`BssClient`].
#[derive(Clone)]
pub struct MediationClient {
    inner: BssClient,
}

impl MediationClient {
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
        Ok(MediationClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /tmf-api/usageManagement/v4/usage` — usage events, newest first.
    /// `limit` always sent first (Python seeds `params={"limit": limit}`), then
    /// optional `subscriptionId` / `msisdn` / `type` (from `event_type`) / `since`.
    /// Returns a JSON array. Backs `usage.history`.
    pub async fn list_usage(
        &self,
        subscription_id: Option<&str>,
        msisdn: Option<&str>,
        event_type: Option<&str>,
        since: Option<&str>,
        limit: i64,
    ) -> Result<Value, ClientError> {
        let mut params: Vec<String> = vec![format!("limit={limit}")];
        if let Some(s) = subscription_id.filter(|s| !s.is_empty()) {
            params.push(format!("subscriptionId={}", encode(s)));
        }
        if let Some(m) = msisdn.filter(|s| !s.is_empty()) {
            params.push(format!("msisdn={}", encode(m)));
        }
        if let Some(t) = event_type.filter(|s| !s.is_empty()) {
            params.push(format!("type={}", encode(t)));
        }
        if let Some(s) = since.filter(|s| !s.is_empty()) {
            params.push(format!("since={}", encode(s)));
        }
        let path = format!("/tmf-api/usageManagement/v4/usage?{}", params.join("&"));
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}

// ── writes ──────────────────────────────────────────────────────────────────
impl MediationClient {
    /// `POST /tmf-api/usageManagement/v4/usage` — submit one usage event.
    /// `roamingIndicator` is included only when true (matching the Python client).
    /// Backs `usage.simulate` (LLM-hidden). Returns the usage doc.
    pub async fn submit_usage(
        &self,
        msisdn: &str,
        event_type: &str,
        event_time: &str,
        quantity: i64,
        unit: &str,
        roaming_indicator: bool,
    ) -> Result<Value, ClientError> {
        let mut m = serde_json::Map::new();
        m.insert("msisdn".to_string(), json!(msisdn));
        m.insert("eventType".to_string(), json!(event_type));
        m.insert("eventTime".to_string(), json!(event_time));
        m.insert("quantity".to_string(), json!(quantity));
        m.insert("unit".to_string(), json!(unit));
        if roaming_indicator {
            m.insert("roamingIndicator".to_string(), json!(true));
        }
        let resp = self
            .inner
            .request(
                Method::POST,
                "/tmf-api/usageManagement/v4/usage",
                Some(&Value::Object(m)),
                None,
            )
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}

/// Minimal query-value encoding (mirrors `catalog::encode`). `since` carries an ISO
/// timestamp — its `+` is escaped (a bare `+` would decode to a space); `:` is
/// query-legal and left as-is, matching the catalog client's `activeAt` handling.
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
