//! `CrmClient` — typed client for the CRM service.
//!
//! Port of `bss_clients.crm.CRMClient`. Only the surface Phase 3 (com) needs is
//! ported: [`CrmClient::get_customer`] (order-create existence check). The rest
//! lands when CRM itself is ported (P4) or when a consumer first needs it.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::Value;

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the CRM service. Wraps [`BssClient`].
#[derive(Clone)]
pub struct CrmClient {
    inner: BssClient,
}

impl CrmClient {
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
        Ok(CrmClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /tmf-api/customerManagement/v4/customer/{id}`. A 404 maps to
    /// [`ClientError::NotFound`] (com turns it into `order.create.customer_not_found`).
    pub async fn get_customer(&self, customer_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/customerManagement/v4/customer/{customer_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}
