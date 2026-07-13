//! `CrmClient` — typed client for the CRM service.
//!
//! Port of `bss_clients.crm.CRMClient`. Only the surface Phase 3 (com) needs is
//! ported: [`CrmClient::get_customer`] (order-create existence check). The rest
//! lands when CRM itself is ported (P4) or when a consumer first needs it.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Method;
use serde_json::Value;

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the CRM service. Wraps [`BssClient`].
#[derive(Clone)]
pub struct CrmClient {
    inner: BssClient,
}

impl CrmClient {
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
        Ok(CrmClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /tmf-api/customerManagement/v4/customer/{id}`. A 404 maps to
    /// [`ClientError::NotFound`] (com turns it into `order.create.customer_not_found`).
    pub async fn get_customer(&self, customer_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/customerManagement/v4/customer/{customer_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/customerManagement/v4/customer/by-msisdn/{msisdn}` — resolve
    /// MSISDN → subscription → customer in one hop. A 404 (number unassigned or
    /// owning customer deleted) maps to [`ClientError::NotFound`]. Backs
    /// `customer.find_by_msisdn`.
    pub async fn find_customer_by_msisdn(&self, msisdn: &str) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/customerManagement/v4/customer/by-msisdn/{}",
            encode(msisdn)
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/customerManagement/v4/customer/by-email?email={email}` — exact
    /// match on a live email contact medium (query param so `+` addressing survives
    /// encoding). A 404 maps to [`ClientError::NotFound`]. Backs
    /// `customer.find_by_email`.
    pub async fn find_customer_by_email(&self, email: &str) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/customerManagement/v4/customer/by-email?email={}",
            encode(email)
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/customerManagement/v4/customer` with optional `status` /
    /// `name` filters (Python maps `state`→`status`, `name_contains`→`name`; each
    /// sent only when present). Returns a JSON array. Backs `customer.list`.
    pub async fn list_customers(
        &self,
        state: Option<&str>,
        name_contains: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut params: Vec<String> = Vec::new();
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            params.push(format!("status={}", encode(s)));
        }
        if let Some(n) = name_contains.filter(|s| !s.is_empty()) {
            params.push(format!("name={}", encode(n)));
        }
        let mut path = "/tmf-api/customerManagement/v4/customer".to_string();
        if !params.is_empty() {
            path.push('?');
            path.push_str(&params.join("&"));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /crm-api/v1/customer/{id}/kyc-status`. Backs `customer.get_kyc_status`.
    pub async fn get_kyc_status(&self, customer_id: &str) -> Result<Value, ClientError> {
        let path = format!("/crm-api/v1/customer/{customer_id}/kyc-status");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /crm-api/v1/case` filtered by optional `customerId` / `state` (sent
    /// only when present). Returns a JSON array. Backs the `customer.get` 360
    /// composite and (later) `case.list_for_me`.
    pub async fn list_cases(
        &self,
        customer_id: Option<&str>,
        state: Option<&str>,
    ) -> Result<Value, ClientError> {
        let mut params: Vec<String> = Vec::new();
        if let Some(c) = customer_id.filter(|s| !s.is_empty()) {
            params.push(format!("customerId={}", encode(c)));
        }
        if let Some(s) = state.filter(|s| !s.is_empty()) {
            params.push(format!("state={}", encode(s)));
        }
        let mut path = "/crm-api/v1/case".to_string();
        if !params.is_empty() {
            path.push('?');
            path.push_str(&params.join("&"));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/customerInteractionManagement/v1/interaction?customerId&limit`
    /// — a customer's interaction log, newest first. Backs `interaction.list` and
    /// the `customer.get` 360 composite.
    pub async fn list_interactions(
        &self,
        customer_id: &str,
        limit: i64,
    ) -> Result<Value, ClientError> {
        let path = format!(
            "/tmf-api/customerInteractionManagement/v1/interaction?customerId={}&limit={limit}",
            encode(customer_id)
        );
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }
}

/// Minimal query-value encoding for the characters that appear in ids/emails
/// (space + `&`/`+`/`=`/`%`/`#`). Mirrors `catalog::encode`; ids are
/// `CUST-001`-shaped so this is a safety net, not a general URL encoder — except
/// email `+` addressing, which it does cover.
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
