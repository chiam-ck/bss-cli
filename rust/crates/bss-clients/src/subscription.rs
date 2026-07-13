//! `SubscriptionClient` — typed client for the Subscription service.
//!
//! Port of `bss_clients.subscription.SubscriptionClient`. Only the surface Phase 2
//! (mediation) needs is ported: [`SubscriptionClient::get_by_msisdn`], the
//! block-at-edge enrichment lookup. The rest of the Python client
//! (create/get/balance/vas/renew/terminate/plan-change/migrate) lands when the
//! Subscription service itself is ported (P4) or when a consumer first needs a
//! given call — the doctrine's "typed clients land per-phase" rule.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::{json, Value};

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the Subscription service. Wraps [`BssClient`].
#[derive(Clone)]
pub struct SubscriptionClient {
    inner: BssClient,
}

impl SubscriptionClient {
    /// Build a Subscription client for `base_url` with `auth` and the default 5s timeout.
    pub fn new(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
    ) -> Result<Self, ClientError> {
        Self::with_timeout(base_url, auth, DEFAULT_TIMEOUT)
    }

    /// As [`SubscriptionClient::new`] with an explicit per-request timeout.
    pub fn with_timeout(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
        timeout: Duration,
    ) -> Result<Self, ClientError> {
        Ok(SubscriptionClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /subscription-api/v1/subscription/by-msisdn/{msisdn}`.
    ///
    /// Returns the enriched subscription document (the shape mediation's policies
    /// read: `id`, `msisdn`, `state`, `offeringId`). A 404 maps to
    /// [`ClientError::NotFound`], which mediation turns into the
    /// `usage.record.subscription_must_exist` policy violation.
    pub async fn get_by_msisdn(&self, msisdn: &str) -> Result<Value, ClientError> {
        let path = format!("/subscription-api/v1/subscription/by-msisdn/{msisdn}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `POST /subscription-api/v1/subscription/{id}/terminate` — terminate a
    /// subscription. `body` carries `{reason, releaseInventory}`; the MNP port-out
    /// flow passes `releaseInventory=false` (the donor MSISDN is already terminal).
    pub async fn terminate(
        &self,
        subscription_id: &str,
        body: &Value,
    ) -> Result<Value, ClientError> {
        let path = format!("/subscription-api/v1/subscription/{subscription_id}/terminate");
        let resp = self
            .inner
            .request(Method::POST, &path, Some(body), None)
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /subscription-api/v1/subscription/{id}` — the full subscription
    /// document (crm's `find_by_msisdn` reads `customerId` off it). A 404 maps to
    /// [`ClientError::NotFound`].
    pub async fn get(&self, subscription_id: &str) -> Result<Value, ClientError> {
        let path = format!("/subscription-api/v1/subscription/{subscription_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /subscription-api/v1/subscription?customerId={id}` — the customer's
    /// subscriptions (crm's close policy checks for active ones). JSON array.
    pub async fn list_for_customer(&self, customer_id: &str) -> Result<Value, ClientError> {
        let path = format!("/subscription-api/v1/subscription?customerId={customer_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /subscription-api/v1/subscription/{id}/balance` — bundle balances. A
    /// 404 maps to [`ClientError::NotFound`]. Backs `subscription.get_balance`.
    pub async fn get_balance(&self, subscription_id: &str) -> Result<Value, ClientError> {
        let path = format!("/subscription-api/v1/subscription/{subscription_id}/balance");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// Resolve the eSIM activation payload for a subscription. Convenience
    /// composite (no dedicated endpoint): reads the subscription, then projects
    /// `{subscriptionId, iccid, msisdn, activationCode, imsi}` — the shape the
    /// first-time-QR / re-download flow wants. Key order matches the Python
    /// client's dict-literal insertion order (preserved on the wire via D9's
    /// `serde_json` `preserve_order`). Missing fields project to `null`, mirroring
    /// Python's `sub.get(...)`. Backs `subscription.get_esim_activation`.
    pub async fn get_esim_activation(&self, subscription_id: &str) -> Result<Value, ClientError> {
        let sub = self.get(subscription_id).await?;
        let field = |k: &str| sub.get(k).cloned().unwrap_or(Value::Null);
        Ok(json!({
            "subscriptionId": subscription_id,
            "iccid": field("iccid"),
            "msisdn": field("msisdn"),
            "activationCode": field("activationCode"),
            "imsi": field("imsi"),
        }))
    }

    /// `POST /subscription-api/v1/subscription` — create and activate. `body`
    /// carries `customerId`/`offeringId`/`msisdn`/`iccid`/`paymentMethodId` plus
    /// the optional `priceSnapshot` and `commercialOrderId` (the idempotency key —
    /// a redelivered `service_order.completed` returns the existing subscription
    /// rather than charging the card twice). Returns the created subscription.
    pub async fn create(&self, body: &Value) -> Result<Value, ClientError> {
        let resp = self
            .inner
            .request(
                Method::POST,
                "/subscription-api/v1/subscription",
                Some(body),
                None,
            )
            .await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}
