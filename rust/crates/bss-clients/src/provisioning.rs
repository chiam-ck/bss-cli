//! `ProvisioningClient` — typed client for the provisioning simulator.
//!
//! Port of the read surface of `bss_clients.provisioning.ProvisioningClient`. Only
//! the orchestrator's provisioning read tools are ported here:
//! [`ProvisioningClient::get_task`] and [`ProvisioningClient::list_tasks`]. The
//! resolve/retry/fault-injection writes land with the provisioning-write slice.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::Value;

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the provisioning-sim service. Wraps [`BssClient`].
#[derive(Clone)]
pub struct ProvisioningClient {
    inner: BssClient,
}

impl ProvisioningClient {
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
        Ok(ProvisioningClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /provisioning-api/v1/task/{id}`. A 404 maps to
    /// [`ClientError::NotFound`]. Backs `provisioning.get_task`.
    pub async fn get_task(&self, task_id: &str) -> Result<Value, ClientError> {
        let path = format!("/provisioning-api/v1/task/{task_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /provisioning-api/v1/task` filtered by optional `serviceId` / `state`
    /// (sent only when present). Returns a JSON array. Backs
    /// `provisioning.list_tasks`.
    pub async fn list_tasks(
        &self,
        service_id: Option<&str>,
        state: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut params: Vec<String> = Vec::new();
        if let Some(s) = service_id.filter(|s| !s.is_empty()) {
            params.push(format!("serviceId={s}"));
        }
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            params.push(format!("state={s}"));
        }
        let mut path = "/provisioning-api/v1/task".to_string();
        if !params.is_empty() {
            path.push('?');
            path.push_str(&params.join("&"));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}
