//! `PaymentClient` — typed client for the Payment service.
//!
//! Port of `bss_clients.payment.PaymentClient`. Only the surface Phase 3 (com)
//! needs: [`PaymentClient::list_methods`] (card-on-file check + the
//! `paymentMethodId` for the submit event). The rest lands with Payment (P4).

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::Value;

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the Payment service. Wraps [`BssClient`].
#[derive(Clone)]
pub struct PaymentClient {
    inner: BssClient,
}

impl PaymentClient {
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
        Ok(PaymentClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /tmf-api/paymentMethodManagement/v4/paymentMethod?customerId={id}` →
    /// JSON array of payment methods (empty when none on file).
    pub async fn list_methods(&self, customer_id: &str) -> Result<Value, ClientError> {
        let path =
            format!("/tmf-api/paymentMethodManagement/v4/paymentMethod?customerId={customer_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}
