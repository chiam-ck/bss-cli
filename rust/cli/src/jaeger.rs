//! Minimal Jaeger HTTP API client for `bss trace`. Port of
//! `packages/bss-telemetry/bss_telemetry/jaeger.py` (Python keeps it in bss-telemetry
//! so the orchestrator's `trace.*` tools can share it; the Rust orchestrator doesn't
//! yet carry those tools, so it lives CLI-local for now, matching the `bss_cli.jaeger`
//! re-export site — it moves to a shared crate when the orchestrator needs it).
//!
//! Reads `BSS_JAEGER_UI_URL` (default `http://tech-vm:16686`). The CLI talks to
//! Jaeger's UI port for the JSON API — the same host that serves the web UI exposes
//! `/api/services` and `/api/traces`. No auth token: Jaeger's query API is unguarded
//! inside the perimeter.

use std::time::Duration;

use serde_json::Value;

/// Jaeger returned a non-2xx or unparseable response. Mirrors Python's `JaegerError`.
#[derive(Debug)]
pub struct JaegerError(pub String);

impl std::fmt::Display for JaegerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for JaegerError {}

fn ui_url() -> String {
    let raw = std::env::var("BSS_JAEGER_UI_URL")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| "http://tech-vm:16686".to_string());
    raw.trim_end_matches('/').to_string()
}

pub struct JaegerClient {
    base_url: String,
    client: reqwest::Client,
}

impl JaegerClient {
    /// Build the client from `BSS_JAEGER_UI_URL` (5s timeout, matching Python).
    pub fn new() -> Result<Self, JaegerError> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .map_err(|e| JaegerError(e.to_string()))?;
        Ok(Self {
            base_url: ui_url(),
            client,
        })
    }

    /// `GET /api/services` → the exporting-service names (`body.data`).
    pub async fn list_services(&self) -> Result<Vec<String>, JaegerError> {
        let url = format!("{}/api/services", self.base_url);
        let resp = self
            .client
            .get(&url)
            .send()
            .await
            .map_err(|e| JaegerError(e.to_string()))?;
        if resp.status().as_u16() != 200 {
            return Err(JaegerError(format!(
                "GET /api/services -> {}",
                resp.status().as_u16()
            )));
        }
        let body: Value = resp.json().await.map_err(|e| JaegerError(e.to_string()))?;
        Ok(string_list(body.get("data")))
    }

    /// `GET /api/traces/{id}` → the first (only) raw Jaeger v1 trace. 404 / empty
    /// data map to the same messages Python raises.
    pub async fn get_trace(&self, trace_id: &str) -> Result<Value, JaegerError> {
        let url = format!("{}/api/traces/{trace_id}", self.base_url);
        let resp = self
            .client
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

    /// `GET /api/traces?service=…&limit=…[&operation=…]` → recent traces (`body.data`).
    async fn find_traces(
        &self,
        service: &str,
        operation: Option<&str>,
        limit: i64,
    ) -> Result<Vec<Value>, JaegerError> {
        let url = format!("{}/api/traces", self.base_url);
        let mut query: Vec<(&str, String)> = vec![
            ("service", service.to_string()),
            ("limit", limit.to_string()),
        ];
        if let Some(op) = operation {
            query.push(("operation", op.to_string()));
        }
        let resp = self
            .client
            .get(&url)
            .query(&query)
            .send()
            .await
            .map_err(|e| JaegerError(e.to_string()))?;
        if resp.status().as_u16() != 200 {
            return Err(JaegerError(format!(
                "GET /api/traces -> {}",
                resp.status().as_u16()
            )));
        }
        let body: Value = resp.json().await.map_err(|e| JaegerError(e.to_string()))?;
        Ok(body
            .get("data")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    /// The `traceID` of the most recent `bss.ask` invocation, if any.
    pub async fn latest_ask_trace_id(&self) -> Result<Option<String>, JaegerError> {
        let traces = self
            .find_traces("bss-orchestrator", Some("bss.ask"), 1)
            .await?;
        Ok(traces
            .first()
            .and_then(|t| t.get("traceID"))
            .and_then(Value::as_str)
            .map(str::to_string))
    }
}

/// `list(body.get("data", []))` restricted to string entries.
fn string_list(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}
