//! `SomClient` — typed client for the SOM service.
//!
//! Port of `bss_clients.som.SOMClient`. Only the surface Phase 3 (com) needs:
//! [`SomClient::list_for_order`] (the cancel-after-SOM-started guard).

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::Value;

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the SOM service. Wraps [`BssClient`].
#[derive(Clone)]
pub struct SomClient {
    inner: BssClient,
}

impl SomClient {
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
        Ok(SomClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /tmf-api/serviceOrderingManagement/v4/serviceOrder?commercialOrderId={id}`
    /// → JSON array of ServiceOrders for the commercial order.
    pub async fn list_for_order(&self, commercial_order_id: &str) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/serviceOrderingManagement/v4/serviceOrder?commercialOrderId={commercial_order_id}"
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/serviceOrderingManagement/v4/serviceOrder/{id}` — a single
    /// ServiceOrder. Backs `service_order.get`.
    pub async fn get_service_order(&self, service_order_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/serviceOrderingManagement/v4/serviceOrder/{service_order_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/serviceInventoryManagement/v4/service/{id}` — a single CFS/RFS
    /// service. Backs `service.get`.
    pub async fn get_service(&self, service_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/serviceInventoryManagement/v4/service/{service_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/serviceInventoryManagement/v4/service?subscriptionId={id}` →
    /// JSON array of services for the subscription. Backs
    /// `service.list_for_subscription`.
    pub async fn list_services_for_subscription(
        &self,
        subscription_id: &str,
    ) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/serviceInventoryManagement/v4/service?subscriptionId={subscription_id}"
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}
