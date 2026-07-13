//! `ComClient` — typed client for the COM (Commercial Order Management) service.
//!
//! Port of the read surface of `bss_clients.com.COMClient` (TMF622 productOrder).
//! Only the calls the orchestrator's order read tools need are ported here:
//! [`ComClient::get_order`], [`ComClient::list_orders`], and the
//! [`ComClient::wait_until`] polling helper. The order create/submit/cancel writes
//! land with the order-write slice.

use std::sync::Arc;
use std::time::{Duration, Instant};

use reqwest::Method;
use serde_json::Value;

use crate::auth::AuthProvider;
use crate::base::{BssClient, DEFAULT_TIMEOUT};
use crate::errors::ClientError;

/// Client for the COM service. Wraps [`BssClient`].
#[derive(Clone)]
pub struct ComClient {
    inner: BssClient,
}

impl ComClient {
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
        Ok(ComClient {
            inner: BssClient::with_auth(base_url, auth, timeout)?,
        })
    }

    /// `GET /tmf-api/productOrderingManagement/v4/productOrder/{id}`. A 404 maps to
    /// [`ClientError::NotFound`]. Backs `order.get`.
    pub async fn get_order(&self, order_id: &str) -> Result<Value, ClientError> {
        let path = format!("/tmf-api/productOrderingManagement/v4/productOrder/{order_id}");
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// `GET /tmf-api/productOrderingManagement/v4/productOrder?customerId={id}` —
    /// a customer's orders, newest first (`customerId` sent only when present).
    /// Backs `order.list`.
    pub async fn list_orders(&self, customer_id: Option<&str>) -> Result<Value, ClientError> {
        let mut path = "/tmf-api/productOrderingManagement/v4/productOrder".to_string();
        if let Some(c) = customer_id.filter(|s| !s.is_empty()) {
            path.push_str(&format!("?customerId={c}"));
        }
        let resp = self.inner.request(Method::GET, &path, None, None).await?;
        resp.json()
            .await
            .map_err(|e| ClientError::Transport(e.to_string()))
    }

    /// Poll `get_order` until `state == target_state` or the deadline elapses.
    /// Returns early on a terminal non-target state (`failed`/`cancelled`). On
    /// timeout returns [`ClientError::Timeout`] carrying the last observed state —
    /// matching the Python client's `Timeout` (the graph maps it to a 504-shaped
    /// observation). Wall-clock polling (`Instant` + `tokio::time::sleep`),
    /// deliberately not the virtual clock — this mirrors Python's `time.monotonic`
    /// + `asyncio.sleep`.
    pub async fn wait_until(
        &self,
        order_id: &str,
        target_state: &str,
        timeout_s: f64,
        poll_interval_s: f64,
    ) -> Result<Value, ClientError> {
        let deadline = Instant::now() + Duration::from_secs_f64(timeout_s);
        let mut last = Value::Null;
        while Instant::now() < deadline {
            last = self.get_order(order_id).await?;
            let state = last.get("state").and_then(Value::as_str);
            if state == Some(target_state) {
                return Ok(last);
            }
            if matches!(state, Some("failed") | Some("cancelled")) {
                return Ok(last);
            }
            tokio::time::sleep(Duration::from_secs_f64(poll_interval_s)).await;
        }
        let last_state = last.get("state").and_then(Value::as_str).unwrap_or("null");
        Err(ClientError::Timeout(format!(
            "Order {order_id} did not reach state={target_state} within {timeout_s}s \
             (last state={last_state:?})"
        )))
    }
}
