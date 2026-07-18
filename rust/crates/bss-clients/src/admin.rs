//! `AdminClient` — service-to-service client for a single service's `/admin-api/v1`
//! surface. Port of `bss_clients.admin.AdminClient`.
//!
//! Only `bss admin reset` (and the scenario runner) use this — deliberately NOT on the
//! LLM tool surface. Every call hits an endpoint gated by `BSS_ALLOW_ADMIN_RESET`, so a
//! misconfigured deployment returns 403 instead of wiping data.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::Value;

use crate::auth::AuthProvider;
use crate::base::BssClient;
use crate::errors::ClientError;

/// `bss admin reset` can TRUNCATE ten-plus tables per service — well above the default
/// 5s client timeout (Python bumps it to 30s).
const ADMIN_TIMEOUT: Duration = Duration::from_secs(30);

/// Client for a service's `/admin-api/v1` surface. Wraps [`BssClient`].
#[derive(Clone)]
pub struct AdminClient {
    inner: BssClient,
}

impl AdminClient {
    pub fn new(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
    ) -> Result<Self, ClientError> {
        Ok(AdminClient {
            inner: BssClient::with_auth(base_url, auth, ADMIN_TIMEOUT)?,
        })
    }

    /// `POST /admin-api/v1/reset-operational-data` → `{service, schemas: [{schema,
    /// truncated, updated}], resetAt}`. Returns `ClientError` (403 when the target has
    /// `BSS_ALLOW_ADMIN_RESET` unset, 404 when the admin router isn't mounted).
    pub async fn reset_operational_data(&self) -> Result<Value, ClientError> {
        let resp = self
            .inner
            .request(
                Method::POST,
                "/admin-api/v1/reset-operational-data",
                None,
                None,
            )
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}
