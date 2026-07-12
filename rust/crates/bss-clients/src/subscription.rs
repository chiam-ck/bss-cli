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
use serde_json::Value;

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
}
