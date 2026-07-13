//! `PaymentClient` — typed client for the Payment service.
//!
//! Port of `bss_clients.payment.PaymentClient`. Only the surface Phase 3 (com)
//! needs: [`PaymentClient::list_methods`] (card-on-file check + the
//! `paymentMethodId` for the submit event). The rest lands with Payment (P4).

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::{json, Value};

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

    /// `GET /tmf-api/paymentManagement/v4/payment/{attempt_id}` — a single payment
    /// attempt (TMF676). A 404 maps to [`ClientError::NotFound`]. Backs
    /// `payment.get_attempt`.
    pub async fn get_payment(&self, attempt_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/paymentManagement/v4/payment/{attempt_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/paymentManagement/v4/payment` — payment attempts, optionally
    /// filtered by `customerId` / `paymentMethodId`. `limit` is always sent (Python
    /// seeds `params={"limit": limit}` first, then the optional filters — query
    /// order preserved). Backs `payment.list_attempts`.
    pub async fn list_payments(
        &self,
        customer_id: Option<&str>,
        payment_method_id: Option<&str>,
        limit: i64,
    ) -> Result<Value, ClientError> {
        let mut params: Vec<String> = vec![format!("limit={limit}")];
        if let Some(c) = customer_id.filter(|s| !s.is_empty()) {
            params.push(format!("customerId={}", encode(c)));
        }
        if let Some(m) = payment_method_id.filter(|s| !s.is_empty()) {
            params.push(format!("paymentMethodId={}", encode(m)));
        }
        let path = format!("/tmf-api/paymentManagement/v4/payment?{}", params.join("&"));
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `POST /tmf-api/paymentManagement/v4/payment` — charge the card-on-file.
    /// `amount` is the already-stringified effective charge (`str(Decimal)` on the
    /// Python side — the caller does the discount math and passes `"22.50"`).
    /// Returns the payment-attempt document (`{id, status, declineReason, ...}`).
    pub async fn charge(
        &self,
        customer_id: &str,
        payment_method_id: &str,
        amount: &str,
        currency: &str,
        purpose: &str,
    ) -> Result<Value, ClientError> {
        let body = json!({
            "customerId": customer_id,
            "paymentMethodId": payment_method_id,
            "amount": amount,
            "currency": currency,
            "purpose": purpose,
        });
        let resp = self
            .inner
            .request(
                Method::POST,
                "/tmf-api/paymentManagement/v4/payment",
                Some(&body),
                None,
            )
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}

// ── writes ────────────────────────────────────────────────────────────────
impl PaymentClient {
    /// `POST /tmf-api/paymentMethodManagement/v4/paymentMethod` — attach a
    /// pre-tokenized card (the sandbox path the `payment.add_card` tool uses:
    /// `tokenizationProvider="sandbox"`, `expMonth=12`/`expYear=2030`/`country="SG"`
    /// defaults). No PAN on the wire. Backs `payment.add_card`.
    pub async fn create_payment_method(
        &self,
        customer_id: &str,
        card_token: &str,
        last4: &str,
        brand: &str,
    ) -> Result<Value, ClientError> {
        let body = json!({
            "customerId": customer_id,
            "type": "card",
            "tokenizationProvider": "sandbox",
            "providerToken": card_token,
            "cardSummary": {
                "brand": brand,
                "last4": last4,
                "expMonth": 12,
                "expYear": 2030,
                "country": "SG",
            },
        });
        let resp = self
            .inner
            .request(
                Method::POST,
                "/tmf-api/paymentMethodManagement/v4/paymentMethod",
                Some(&body),
                None,
            )
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `DELETE /tmf-api/paymentMethodManagement/v4/paymentMethod/{id}` — returns the
    /// server body when present, else `{id, removed:true}` (empty-body case). Backs
    /// `payment.remove_method` (DESTRUCTIVE — safety-gated at the tool).
    pub async fn remove_method(&self, method_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/paymentMethodManagement/v4/paymentMethod/{method_id}");
        let resp = self
            .inner
            .request(Method::DELETE, &path, None, None)
            .await?;
        let bytes = resp
            .bytes()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))?;
        if bytes.is_empty() {
            return Ok(json!({"id": method_id, "removed": true}));
        }
        serde_json::from_slice(&bytes).map_err(|e| ClientError::Transport(e.to_string()))
    }
}

/// Minimal query-value encoding for the id characters that need it (mirrors
/// `catalog::encode`). Ids are `CUST-001`/`PM-NNNN`-shaped, so this is a safety net.
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
