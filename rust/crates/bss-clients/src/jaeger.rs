//! `JaegerClient` — minimal async client for the few Jaeger v1 HTTP endpoints the
//! `trace.*` tools need. Port of `bss_telemetry.jaeger.JaegerClient`.
//!
//! Unlike the BSS service clients, Jaeger's query API is **not** behind the BSS
//! token perimeter, so this uses a plain `reqwest::Client` (no `AuthProvider`, no
//! context propagation). Reads `BSS_JAEGER_UI_URL` (default `http://tech-vm:16686`).

use std::time::Duration;

use serde_json::Value;

/// Raised when Jaeger returns a non-2xx or unparseable response — the port of
/// Python's `JaegerError`. The `trace.get` tool converts this into a structured
/// `{"error": "JAEGER_ERROR", ...}` observation rather than failing the turn.
#[derive(Debug, Clone)]
pub struct JaegerError(pub String);

impl std::fmt::Display for JaegerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for JaegerError {}

/// Default Jaeger UI/query host (same port serves the UI and the JSON API).
pub const DEFAULT_JAEGER_URL: &str = "http://tech-vm:16686";

/// Async wrapper for the Jaeger v1 HTTP endpoints `trace.get` needs.
#[derive(Clone)]
pub struct JaegerClient {
    base_url: String,
    http: reqwest::Client,
}

impl JaegerClient {
    /// Build a client for `base_url` (trailing slash trimmed) with a 5s timeout.
    pub fn new(base_url: impl Into<String>) -> Result<Self, JaegerError> {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .map_err(|e| JaegerError(e.to_string()))?;
        Ok(JaegerClient {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            http,
        })
    }

    /// Build from `BSS_JAEGER_UI_URL` (default [`DEFAULT_JAEGER_URL`]).
    pub fn from_env() -> Result<Self, JaegerError> {
        let url = std::env::var("BSS_JAEGER_UI_URL")
            .ok()
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| DEFAULT_JAEGER_URL.to_string());
        Self::new(url)
    }

    /// `GET /api/traces/{trace_id}` — a single trace's raw Jaeger v1 shape.
    /// Mirrors the Python client's error mapping: 404 → not-found, non-200 →
    /// status, empty `data` → empty. Returns the first (only) trace in `data`.
    pub async fn get_trace(&self, trace_id: &str) -> Result<Value, JaegerError> {
        let url = format!("{}/api/traces/{trace_id}", self.base_url);
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .map_err(|e| JaegerError(e.to_string()))?;
        let status = resp.status().as_u16();
        if status == 404 {
            return Err(JaegerError(format!("trace {trace_id} not found in Jaeger")));
        }
        if status != 200 {
            return Err(JaegerError(format!(
                "GET /api/traces/{trace_id} -> {status}"
            )));
        }
        let body: Value = resp.json().await.map_err(|e| JaegerError(e.to_string()))?;
        let traces = body.get("data").and_then(Value::as_array);
        match traces.and_then(|t| t.first()) {
            Some(t) => Ok(t.clone()),
            None => Err(JaegerError(format!("trace {trace_id} returned empty data"))),
        }
    }
}
