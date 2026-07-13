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

    /// `GET /tmf-api/productCatalogManagement/v4/productOffering` — every offering
    /// (plans + VAS), no time filter. Backs the `catalog.list_offerings` tool.
    pub async fn list_offerings(&self) -> Result<Value, ClientError> {
        let path = "/tmf-api/productCatalogManagement/v4/productOffering";
        let resp = self.inner.request(Method::GET, path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /vas/offering` — every VAS offering. Backs the `catalog.list_vas` tool.
    pub async fn list_vas(&self) -> Result<Value, ClientError> {
        let resp = self
            .inner
            .request(Method::GET, "/vas/offering", None, None)
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/productCatalogManagement/v4/productOfferingPrice/active/{id}`.
    ///
    /// The lowest active recurring price at the current moment (no-`at` variant —
    /// the server defaults to now). A 422 carrying `catalog.price.no_active_row`
    /// maps to [`ClientError::Policy`].
    pub async fn get_active_price(&self, offering_id: &str) -> Result<Value, ClientError> {
        self.get_active_price_at(offering_id, None).await
    }

    /// As [`CatalogClient::get_active_price`] but at an explicit moment. Sends the
    /// `activeAt` query only when `active_at` is `Some` — matching the Python
    /// client's `params["activeAt"] = at.isoformat()` gate exactly.
    pub async fn get_active_price_at(
        &self,
        offering_id: &str,
        active_at: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut path = format!(
            "/tmf-api/productCatalogManagement/v4/productOfferingPrice/active/{offering_id}"
        );
        if let Some(at) = active_at {
            path.push_str(&format!("?activeAt={}", encode(at)));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/productCatalogManagement/v4/productOfferingPrice/{id}` —
    /// direct price-row lookup, no time filter (the snapshot remembers a retired
    /// row). Used by renewal's pending-pivot + price migration.
    pub async fn get_offering_price(&self, price_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/productCatalogManagement/v4/productOfferingPrice/{price_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/productCatalogManagement/v4/productOffering?activeAt={iso}` —
    /// sellable-now offerings, sorted by lowest price. `active_at` is the caller's
    /// clock moment (frozen-clock-safe); the Python client defaults it to
    /// `clock_now()`, so the subscription service passes `bss_clock::now()`.
    pub async fn list_active_offerings(&self, active_at: &str) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/productCatalogManagement/v4/productOffering?activeAt={}",
            encode(active_at)
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /vas/offering/{id}` — a value-added-service offering (top-up spec). A
    /// 404 maps to [`ClientError::NotFound`].
    pub async fn get_vas(&self, vas_id: &str) -> Result<Value, ClientError> {
        let path = format!("/vas/offering/{vas_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /promo/validate?code&offering[&customerId]` — full order-time discount
    /// terms com stamps onto the order item. `customer_id` gates a targeted code
    /// on eligibility.
    pub async fn validate_promo(
        &self,
        code: &str,
        offering: &str,
        customer_id: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut path = format!(
            "/promo/validate?code={}&offering={}",
            encode(code),
            encode(offering)
        );
        if let Some(cid) = customer_id {
            path.push_str(&format!("&customerId={}", encode(cid)));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /promo/resolve-eligible?customerId&offering` — best targeted promo the
    /// customer is eligible for + terms + the promo's code (`{valid:false}` if none).
    pub async fn resolve_eligible_promo(
        &self,
        customer_id: &str,
        offering: &str,
    ) -> Result<Value, ClientError> {
        let path = format!(
            "/promo/resolve-eligible?customerId={}&offering={}",
            encode(customer_id),
            encode(offering)
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}

/// Minimal query-value encoding for the characters that appear in ids/codes
/// (space + `&`/`+`/`=`/`%`/`#`). Ids are `CUST-001`-shaped, so this is a
/// safety net, not a general URL encoder.
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
