//! Inbound HTTP server-span layer — the extract half of trace propagation.
//!
//! Rust has no FastAPI auto-instrumentor, so this stands in for it: one span per
//! request, adopting the caller's `traceparent` as its parent so the trace stays
//! continuous across an S2S hop, and recording the response status. Crucially,
//! this span is the *current* span for the whole handler, so `bss-events` stamps
//! its trace id onto every `audit.domain_event` the request stages — which is what
//! lets `trace.for_order` resolve the aggregate's trace later.
//!
//! Wire it as the outermost layer (`from_fn(otel_http_span)`) so it wraps the
//! token + context layers and the handler. Health probes are skipped to keep
//! Jaeger free of heartbeat-span spam.

use axum::{extract::Request, middleware::Next, response::Response};
use tracing::Instrument;

/// Skip span creation for the liveness/readiness probes (they fire every few
/// seconds and would drown Jaeger in heartbeat traces).
fn is_health(path: &str) -> bool {
    matches!(path, "/health" | "/health/ready" | "/health/live")
}

/// Middleware fn for `axum::middleware::from_fn`.
pub async fn otel_http_span(req: Request, next: Next) -> Response {
    if is_health(req.uri().path()) {
        return next.run(req).await;
    }

    let method = req.method().clone();
    let path = req.uri().path().to_string();
    let traceparent = req
        .headers()
        .get(bss_telemetry::TRACEPARENT)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);

    let span = tracing::info_span!(
        "http.server",
        otel.name = format!("{method} {path}"),
        otel.kind = "server",
        http.method = %method,
        http.route = path,
        http.status_code = tracing::field::Empty,
    );
    bss_telemetry::continue_trace(&span, traceparent.as_deref());

    async move {
        let resp = next.run(req).await;
        tracing::Span::current().record("http.status_code", resp.status().as_u16());
        resp
    }
    .instrument(span)
    .await
}
