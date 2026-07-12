//! `InventoryClient` — typed client for CRM's Inventory sub-domain.
//!
//! Port of `bss_clients.inventory.InventoryClient`. Inventory lives inside CRM
//! (port 8002) under `/inventory-api/v1/`; this client isolates callers from that
//! hosting detail. Only the surface Phase 2 (SOM decomposition + failure release)
//! needs is ported: atomic MSISDN + eSIM reservation and their releases. The rest
//! lands when CRM/Inventory itself is ported.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::{json, Value};

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the Inventory sub-domain (hosted on CRM).
#[derive(Clone)]
pub struct InventoryClient {
    inner: BssClient,
}

impl InventoryClient {
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
        Ok(InventoryClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `POST /inventory-api/v1/msisdn/reserve-next` — atomic auto-pick. Body
    /// carries `{preference}` only when a preference is supplied (matching the
    /// Python client). Returns the reserved MSISDN record (`{msisdn, ...}`).
    pub async fn reserve_next_msisdn(
        &self,
        preference: Option<&str>,
    ) -> Result<Value, ClientError> {
        let body = preference.map(|p| json!({ "preference": p }));
        self.post("/inventory-api/v1/msisdn/reserve-next", body.as_ref())
            .await
    }

    /// `POST /inventory-api/v1/esim/reserve` — reserve the next eSIM profile
    /// (`{iccid, imsi, activationCode, ...}`).
    pub async fn reserve_esim(&self) -> Result<Value, ClientError> {
        self.post("/inventory-api/v1/esim/reserve", None).await
    }

    /// `GET /inventory-api/v1/msisdn/{msisdn}` — the MSISDN record (`{status, ...}`),
    /// read by the create policy to confirm it's reserved/assigned.
    pub async fn get_msisdn(&self, msisdn: &str) -> Result<Value, ClientError> {
        let path = format!("/inventory-api/v1/msisdn/{msisdn}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /inventory-api/v1/esim/{iccid}` — the eSIM record (`{profileState, ...}`).
    pub async fn get_esim(&self, iccid: &str) -> Result<Value, ClientError> {
        let path = format!("/inventory-api/v1/esim/{iccid}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `POST /inventory-api/v1/msisdn/{msisdn}/assign` — reserved → assigned.
    pub async fn assign_msisdn(&self, msisdn: &str) -> Result<Value, ClientError> {
        let path = format!("/inventory-api/v1/msisdn/{msisdn}/assign");
        self.post(&path, None).await
    }

    /// `POST /inventory-api/v1/esim/{iccid}/assign-msisdn` — link the MSISDN onto
    /// the reserved eSIM profile.
    pub async fn assign_msisdn_to_esim(
        &self,
        iccid: &str,
        msisdn: &str,
    ) -> Result<Value, ClientError> {
        let path = format!("/inventory-api/v1/esim/{iccid}/assign-msisdn");
        let body = json!({ "msisdn": msisdn });
        self.post(&path, Some(&body)).await
    }

    /// `POST /inventory-api/v1/esim/{iccid}/recycle` — activated → recycled (on
    /// termination). Distinct from `release_esim` (reserved → available).
    pub async fn recycle_esim(&self, iccid: &str) -> Result<Value, ClientError> {
        let path = format!("/inventory-api/v1/esim/{iccid}/recycle");
        self.post(&path, None).await
    }

    /// `POST /inventory-api/v1/msisdn/{msisdn}/release` — reserved → available.
    pub async fn release_msisdn(&self, msisdn: &str) -> Result<Value, ClientError> {
        let path = format!("/inventory-api/v1/msisdn/{msisdn}/release");
        self.post(&path, None).await
    }

    /// `POST /inventory-api/v1/esim/{iccid}/release` — reserved → available.
    pub async fn release_esim(&self, iccid: &str) -> Result<Value, ClientError> {
        let path = format!("/inventory-api/v1/esim/{iccid}/release");
        self.post(&path, None).await
    }

    async fn post(&self, path: &str, body: Option<&Value>) -> Result<Value, ClientError> {
        let resp = self.inner.request(Method::POST, path, body, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}
