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

    /// `POST /inventory-api/v1/msisdn/{msisdn}/hold` — soft-hold a specific number
    /// for `reserved_for` (the holder / open-order id) with a TTL (default 24h
    /// when `ttl_secs` is `None`). Atomic + self-healing server-side; a
    /// `msisdn.hold.unavailable` policy violation means someone else holds it.
    /// Phase 1 of the reservation feature.
    pub async fn hold_msisdn(
        &self,
        msisdn: &str,
        reserved_for: &str,
        ttl_secs: Option<i64>,
    ) -> Result<Value, ClientError> {
        let mut body = json!({ "reserved_for": reserved_for });
        if let Some(t) = ttl_secs {
            body["ttl_secs"] = json!(t);
        }
        let path = format!("/inventory-api/v1/msisdn/{msisdn}/hold");
        self.post(&path, Some(&body)).await
    }

    /// `POST /inventory-api/v1/msisdn/release-hold` — release every soft hold owned
    /// by `reserved_for` back to the pool. Idempotent (returns `{released: [...]}`).
    pub async fn release_msisdn_hold(&self, reserved_for: &str) -> Result<Value, ClientError> {
        let body = json!({ "reserved_for": reserved_for });
        self.post("/inventory-api/v1/msisdn/release-hold", Some(&body))
            .await
    }

    /// `POST /inventory-api/v1/open-order` — create (holding the number) or resume
    /// the owner's open order. Blocks with `open_order.already_open` if they have
    /// one for a different number. Returns the record incl. `resumed: bool`.
    pub async fn create_open_order(
        &self,
        identity: &str,
        plan_code: &str,
        msisdn: &str,
    ) -> Result<Value, ClientError> {
        let body = json!({ "identity": identity, "plan_code": plan_code, "msisdn": msisdn });
        self.post("/inventory-api/v1/open-order", Some(&body)).await
    }

    /// `GET /inventory-api/v1/open-order/by-identity?identity=…` — the owner's
    /// current open order, or `null`.
    pub async fn open_order_by_identity(&self, identity: &str) -> Result<Value, ClientError> {
        let path = format!(
            "/inventory-api/v1/open-order/by-identity?identity={}",
            encode(identity)
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `POST /inventory-api/v1/open-order/{id}/link-customer`.
    pub async fn link_open_order_customer(
        &self,
        id: &str,
        customer_id: &str,
    ) -> Result<Value, ClientError> {
        let body = json!({ "customer_id": customer_id });
        self.post(
            &format!("/inventory-api/v1/open-order/{id}/link-customer"),
            Some(&body),
        )
        .await
    }

    /// `POST /inventory-api/v1/open-order/{id}/advance` — record the funnel step.
    pub async fn advance_open_order(&self, id: &str, step: &str) -> Result<Value, ClientError> {
        let body = json!({ "step": step });
        self.post(
            &format!("/inventory-api/v1/open-order/{id}/advance"),
            Some(&body),
        )
        .await
    }

    /// `POST /inventory-api/v1/open-order/{id}/complete` — funnel finished.
    pub async fn complete_open_order(&self, id: &str) -> Result<Value, ClientError> {
        self.post(&format!("/inventory-api/v1/open-order/{id}/complete"), None)
            .await
    }

    /// `POST /inventory-api/v1/open-order/{id}/cancel` — release the hold + close.
    /// Sends an empty object so the (defaulted) request body deserializes.
    pub async fn cancel_open_order(&self, id: &str) -> Result<Value, ClientError> {
        let body = json!({});
        self.post(
            &format!("/inventory-api/v1/open-order/{id}/cancel"),
            Some(&body),
        )
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

    /// `GET /inventory-api/v1/msisdn` — pool listing. `limit` always sent (Python
    /// seeds `params={"limit": limit}` first), then optional `status` (from `state`)
    /// / `prefix`. Backs `inventory.msisdn.list_available`.
    pub async fn list_msisdns(
        &self,
        state: Option<&str>,
        prefix: Option<&str>,
        limit: i64,
    ) -> Result<Value, ClientError> {
        let mut params: Vec<String> = vec![format!("limit={limit}")];
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            params.push(format!("status={}", encode(s)));
        }
        if let Some(p) = prefix.filter(|s| !s.is_empty()) {
            params.push(format!("prefix={}", encode(p)));
        }
        self.get(&format!("/inventory-api/v1/msisdn?{}", params.join("&")))
            .await
    }

    /// `GET /inventory-api/v1/msisdn/count` — group-by-status pool count, optional
    /// `prefix`. Backs `inventory.msisdn.count`.
    pub async fn count_msisdns(&self, prefix: Option<&str>) -> Result<Value, ClientError> {
        let mut path = "/inventory-api/v1/msisdn/count".to_string();
        if let Some(p) = prefix.filter(|s| !s.is_empty()) {
            path.push_str(&format!("?prefix={}", encode(p)));
        }
        self.get(&path).await
    }

    /// `GET /inventory-api/v1/esim` — eSIM pool listing. `limit` always sent, then
    /// optional `status` (from `state`). Backs `inventory.esim.list_available`.
    pub async fn list_esims(&self, state: Option<&str>, limit: i64) -> Result<Value, ClientError> {
        let mut params: Vec<String> = vec![format!("limit={limit}")];
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            params.push(format!("status={}", encode(s)));
        }
        self.get(&format!("/inventory-api/v1/esim?{}", params.join("&")))
            .await
    }

    /// `GET /inventory-api/v1/esim/{iccid}/activation` — the LPA activation record.
    /// Backs `inventory.esim.get_activation`.
    pub async fn get_activation_code(&self, iccid: &str) -> Result<Value, ClientError> {
        self.get(&format!("/inventory-api/v1/esim/{iccid}/activation"))
            .await
    }

    /// `POST /inventory-api/v1/msisdn/add-range` with `{prefix, count}` — v0.17
    /// operator bulk pool extension. Backs `inventory.msisdn.add_range`.
    pub async fn add_msisdn_range(&self, prefix: &str, count: i64) -> Result<Value, ClientError> {
        self.post(
            "/inventory-api/v1/msisdn/add-range",
            Some(&json!({"prefix": prefix, "count": count})),
        )
        .await
    }

    async fn get(&self, path: &str) -> Result<Value, ClientError> {
        let resp = self.inner.request(Method::GET, path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    async fn post(&self, path: &str, body: Option<&Value>) -> Result<Value, ClientError> {
        let resp = self.inner.request(Method::POST, path, body, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
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
