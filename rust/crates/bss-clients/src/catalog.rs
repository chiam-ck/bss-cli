//! `CatalogClient` — the first typed per-service client.
//!
//! Port of `bss_clients.catalog.CatalogClient`. Only the surface Phase 1 (rating)
//! needs is ported: [`CatalogClient::get_offering`]. The rest of the Python
//! client (list/active-price/promotions/admin writes) lands when Catalog itself
//! is ported (P3) or when a consumer first needs a given call — the doctrine's
//! "typed clients land per-phase" rule.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::Value;

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the Catalog service. Wraps [`BssClient`] and speaks the TMF620
/// productOffering surface.
#[derive(Clone)]
pub struct CatalogClient {
    inner: BssClient,
}

impl CatalogClient {
    /// Build a Catalog client for `base_url` with `auth` and the default 5s timeout.
    pub fn new(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
    ) -> Result<Self, ClientError> {
        Self::with_timeout(base_url, auth, DEFAULT_TIMEOUT)
    }

    /// As [`CatalogClient::new`] with an explicit per-request timeout.
    pub fn with_timeout(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
        timeout: Duration,
    ) -> Result<Self, ClientError> {
        Ok(CatalogClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /tmf-api/productCatalogManagement/v4/productOffering/{id}`.
    ///
    /// Returns the raw TMF offering document as JSON (the shape the pure rating
    /// function reads: `bundleAllowance`, `productOfferingPrice`, `id`). A 404
    /// maps to [`ClientError::NotFound`].
    pub async fn get_offering(&self, offering_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/productCatalogManagement/v4/productOffering/{offering_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}
