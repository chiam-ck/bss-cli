//! `BssClient` — the reqwest base for service-to-service calls.
//!
//! Port of `bss_clients.base.BSSClient`. Doctrine, preserved:
//! - timeouts are mandatory and per-request; **no automatic retries**;
//! - typed errors (404 ≠ 5xx ≠ POLICY_VIOLATION ≠ timeout);
//! - context propagation: `X-BSS-Actor` / `X-BSS-Channel` / `X-Request-ID` read
//!   from [`bss_context::current`] (the task-local set by the server middleware),
//!   applied with set-default semantics so they never clobber an explicit header;
//! - the [`AuthProvider`] is consulted on every request.

use std::sync::Arc;
use std::time::Duration;

use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use reqwest::Method;
use serde_json::Value;

use crate::auth::{AuthProvider, NoAuthProvider};
use crate::errors::ClientError;
use bss_db::PolicyViolation;

/// Default per-request timeout (matches the Python 5.0s default).
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(5);

/// HTTP base client. Cheap to clone (shares the underlying reqwest pool).
#[derive(Clone)]
pub struct BssClient {
    base_url: String,
    http: reqwest::Client,
    auth: Arc<dyn AuthProvider>,
}

impl BssClient {
    /// Build a client with no auth and the default timeout.
    pub fn new(base_url: impl Into<String>) -> Result<Self, ClientError> {
        Self::with_auth(base_url, Arc::new(NoAuthProvider), DEFAULT_TIMEOUT)
    }

    /// Build a client with an auth provider and a default per-request timeout.
    pub fn with_auth(
        base_url: impl Into<String>,
        auth: Arc<dyn AuthProvider>,
        timeout: Duration,
    ) -> Result<Self, ClientError> {
        let http = reqwest::Client::builder()
            .timeout(timeout)
            .build()
            .map_err(|e| ClientError::Transport(e.to_string()))?;
        Ok(BssClient {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            http,
            auth,
        })
    }

    /// Send a request and map the outcome to a typed result. `body` is sent as
    /// JSON when present. `timeout` overrides the default for this call.
    pub async fn request(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
        timeout: Option<Duration>,
    ) -> Result<reqwest::Response, ClientError> {
        let url = format!("{}{}", self.base_url, path);
        let mut builder = self.http.request(method, url);

        builder = builder.headers(self.build_headers());
        if let Some(t) = timeout {
            builder = builder.timeout(t);
        }
        if let Some(json) = body {
            builder = builder.json(json);
        }

        let resp = builder.send().await.map_err(map_send_error)?;
        handle_response(resp).await
    }

    /// Auth headers + context propagation (set-default: context never clobbers an
    /// auth or explicit header).
    fn build_headers(&self) -> HeaderMap {
        let mut headers = HeaderMap::new();
        for (name, value) in self.auth.headers() {
            insert_str(&mut headers, &name, &value, /* overwrite */ true);
        }
        let ctx = bss_context::current();
        for (name, value) in ctx.outbound_headers() {
            insert_str(&mut headers, name, &value, /* overwrite */ false);
        }
        headers
    }
}

/// Insert a `str`-typed header, skipping invalid names/values. When `overwrite`
/// is false, an existing header of the same name is kept (set-default).
fn insert_str(headers: &mut HeaderMap, name: &str, value: &str, overwrite: bool) {
    let (Ok(name), Ok(value)) = (
        HeaderName::from_bytes(name.as_bytes()),
        HeaderValue::from_str(value),
    ) else {
        return;
    };
    if !overwrite && headers.contains_key(&name) {
        return;
    }
    headers.insert(name, value);
}

fn map_send_error(err: reqwest::Error) -> ClientError {
    if err.is_timeout() {
        ClientError::Timeout(err.to_string())
    } else {
        ClientError::Transport(err.to_string())
    }
}

/// Status → typed error, mirroring `_handle_response`.
async fn handle_response(resp: reqwest::Response) -> Result<reqwest::Response, ClientError> {
    let status = resp.status().as_u16();
    match status {
        404 => Err(ClientError::NotFound(text_of(resp).await)),
        422 => {
            let body = text_of(resp).await;
            if let Ok(json) = serde_json::from_str::<Value>(&body) {
                if let Some(pv) = PolicyViolation::from_wire(&json) {
                    return Err(ClientError::Policy(pv));
                }
            }
            Err(ClientError::Http {
                status: 422,
                detail: body,
            })
        }
        s if s >= 500 => Err(ClientError::Server {
            status: s,
            detail: text_of(resp).await,
        }),
        s if s >= 400 => Err(ClientError::Http {
            status: s,
            detail: text_of(resp).await,
        }),
        _ => Ok(resp),
    }
}

async fn text_of(resp: reqwest::Response) -> String {
    resp.text().await.unwrap_or_default()
}
