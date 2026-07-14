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
use serde_json::{json, Map, Value};

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

    /// `GET /tmf-api/promotionManagement/v4/promotion/{id}` — a single TMF671
    /// promotion. A 404 maps to [`ClientError::NotFound`]. Backs `promo.show`.
    pub async fn get_promotion(&self, promotion_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/promotionManagement/v4/promotion/{promotion_id}");
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

    /// `GET /promo/customer-offers` — targeted-offer entitlements for a customer
    /// (`{offers: [...]}`), optionally filtered by `state`. Backs the dashboard's
    /// assigned-offer block.
    pub async fn list_customer_offers(
        &self,
        customer_id: &str,
        state: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut path = format!("/promo/customer-offers?customerId={}", encode(customer_id));
        if let Some(s) = state {
            path.push_str(&format!("&state={}", encode(s)));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /promo/preview` — portal live-preview of a typed promo code:
    /// `{valid, label, base, effective, reason}`. `customer_id` gates a targeted
    /// code on eligibility. Backs the signup form's promo field.
    pub async fn preview_promo(
        &self,
        code: &str,
        offering: &str,
        customer_id: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut path = format!(
            "/promo/preview?code={}&offering={}",
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
}

// ── writes (promo saga + admin) ─────────────────────────────────────────────
impl CatalogClient {
    /// `POST /tmf-api/promotionManagement/v4/promotion` — the create-promotion saga
    /// (BSS money terms + loyalty code). Optional fields sent only when present;
    /// `valid_from`/`valid_to` are ISO strings sent verbatim. Backs `promo.create`.
    #[allow(clippy::too_many_arguments)]
    pub async fn create_promotion(
        &self,
        promotion_id: &str,
        discount_type: &str,
        discount_value: &str,
        duration_kind: &str,
        audience: &str,
        currency: &str,
        code: Option<&str>,
        promo_code_kind: Option<&str>,
        applicable_offering_ids: Option<&[String]>,
        periods_total: Option<i64>,
        valid_from: Option<&str>,
        valid_to: Option<&str>,
        display_name: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut m = Map::new();
        m.insert("promotionId".to_string(), json!(promotion_id));
        m.insert("discountType".to_string(), json!(discount_type));
        m.insert("discountValue".to_string(), json!(discount_value));
        m.insert("durationKind".to_string(), json!(duration_kind));
        m.insert("audience".to_string(), json!(audience));
        m.insert("currency".to_string(), json!(currency));
        if let Some(v) = code {
            m.insert("code".to_string(), json!(v));
        }
        if let Some(v) = promo_code_kind {
            m.insert("promoCodeKind".to_string(), json!(v));
        }
        if let Some(v) = applicable_offering_ids {
            m.insert("applicableOfferingIds".to_string(), json!(v));
        }
        if let Some(v) = periods_total {
            m.insert("periodsTotal".to_string(), json!(v));
        }
        if let Some(v) = valid_from {
            m.insert("validFrom".to_string(), json!(v));
        }
        if let Some(v) = valid_to {
            m.insert("validTo".to_string(), json!(v));
        }
        if let Some(v) = display_name {
            m.insert("displayName".to_string(), json!(v));
        }
        self.send(Method::POST, PROMO_PATH, Some(&Value::Object(m)))
            .await
    }

    /// `POST …/promotion/{id}/assign` with `{customerIds}`. Backs `promo.assign`.
    pub async fn assign_promotion(
        &self,
        promotion_id: &str,
        customer_ids: &[String],
    ) -> Result<Value, ClientError> {
        let path = format!("{PROMO_PATH}/{promotion_id}/assign");
        self.send(
            Method::POST,
            &path,
            Some(&json!({"customerIds": customer_ids})),
        )
        .await
    }

    /// `POST /admin/catalog/offering` — add a new offering + its opening price.
    /// Optional allowances/window sent only when present. Backs `catalog.add_offering`
    /// (LLM-hidden).
    #[allow(clippy::too_many_arguments)]
    pub async fn admin_add_offering(
        &self,
        offering_id: &str,
        name: &str,
        amount: &str,
        currency: &str,
        spec_id: &str,
        valid_from: Option<&str>,
        valid_to: Option<&str>,
        data_mb: Option<i64>,
        voice_minutes: Option<i64>,
        sms_count: Option<i64>,
        data_roaming_mb: Option<i64>,
    ) -> Result<Value, ClientError> {
        let mut m = Map::new();
        m.insert("offeringId".to_string(), json!(offering_id));
        m.insert("name".to_string(), json!(name));
        m.insert("specId".to_string(), json!(spec_id));
        m.insert("amount".to_string(), json!(amount));
        m.insert("currency".to_string(), json!(currency));
        for (k, v) in [
            ("validFrom", valid_from.map(|s| json!(s))),
            ("validTo", valid_to.map(|s| json!(s))),
            ("dataMb", data_mb.map(|v| json!(v))),
            ("voiceMinutes", voice_minutes.map(|v| json!(v))),
            ("smsCount", sms_count.map(|v| json!(v))),
            ("dataRoamingMb", data_roaming_mb.map(|v| json!(v))),
        ] {
            if let Some(val) = v {
                m.insert(k.to_string(), val);
            }
        }
        self.send(
            Method::POST,
            "/admin/catalog/offering",
            Some(&Value::Object(m)),
        )
        .await
    }

    /// `POST /admin/catalog/offering/{id}/price` — add a price row. `retire_current`
    /// stamps existing open rows so the new one takes over. Backs `catalog.add_price`
    /// (LLM-hidden).
    #[allow(clippy::too_many_arguments)]
    pub async fn admin_add_price(
        &self,
        offering_id: &str,
        price_id: &str,
        amount: &str,
        currency: &str,
        valid_from: Option<&str>,
        valid_to: Option<&str>,
        retire_current: bool,
    ) -> Result<Value, ClientError> {
        let mut m = Map::new();
        m.insert("priceId".to_string(), json!(price_id));
        m.insert("amount".to_string(), json!(amount));
        m.insert("currency".to_string(), json!(currency));
        m.insert("retireCurrent".to_string(), json!(retire_current));
        if let Some(v) = valid_from {
            m.insert("validFrom".to_string(), json!(v));
        }
        if let Some(v) = valid_to {
            m.insert("validTo".to_string(), json!(v));
        }
        let path = format!("/admin/catalog/offering/{offering_id}/price");
        self.send(Method::POST, &path, Some(&Value::Object(m)))
            .await
    }

    /// `PATCH /admin/catalog/offering/{id}/window` — set the validity window
    /// (`validFrom`/`validTo`, each sent when present). Backs `catalog.window_offering`
    /// (LLM-hidden).
    pub async fn admin_set_offering_window(
        &self,
        offering_id: &str,
        valid_from: Option<&str>,
        valid_to: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut m = Map::new();
        if let Some(v) = valid_from {
            m.insert("validFrom".to_string(), json!(v));
        }
        if let Some(v) = valid_to {
            m.insert("validTo".to_string(), json!(v));
        }
        let path = format!("/admin/catalog/offering/{offering_id}/window");
        self.send(Method::PATCH, &path, Some(&Value::Object(m)))
            .await
    }

    async fn send(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
    ) -> Result<Value, ClientError> {
        let resp = self.inner.request(method, path, body, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}

/// The TMF671 promotion collection path (mirrors the Python client's `_PROMO`).
const PROMO_PATH: &str = "/tmf-api/promotionManagement/v4/promotion";

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
