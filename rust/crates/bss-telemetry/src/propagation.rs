//! W3C trace-context propagation helpers for the platform seams.
//!
//! Rust has no auto-instrumentors, so the trace has to be stitched by hand at the
//! four seams the pipeline crosses: the inbound HTTP span (bss-middleware), the
//! outbound S2S call (bss-clients), and the MQ publish/consume boundary
//! (bss-events relay + consumer). All of the OpenTelemetry API surface for that
//! stitching lives here, so those seam crates only need `tracing` + this crate.
//!
//! The model: every seam works off the *current* `tracing` span (which the
//! `tracing-opentelemetry` bridge backs with a live OTel span context). Outbound
//! seams serialize the current span into a `traceparent` header; inbound seams
//! create a span and adopt the incoming `traceparent` as its parent. The one
//! exception is the outbox relay, which publishes out of request context — it
//! re-seeds the trace from the `audit.domain_event.trace_id` it stored at write
//! time via [`traceparent_for_trace_id`].

use std::collections::HashMap;

use opentelemetry::propagation::{Extractor, Injector, TextMapPropagator};
use opentelemetry::trace::TraceContextExt;
use opentelemetry_sdk::propagation::TraceContextPropagator;
use opentelemetry_sdk::trace::{IdGenerator, RandomIdGenerator};
use tracing::Span;
use tracing_opentelemetry::OpenTelemetrySpanExt;

/// The W3C trace-context header / MQ message-header name.
pub const TRACEPARENT: &str = "traceparent";

/// A one-key carrier used for injecting/extracting a single `traceparent`.
#[derive(Default)]
struct MapCarrier(HashMap<String, String>);

impl Injector for MapCarrier {
    fn set(&mut self, key: &str, value: String) {
        self.0.insert(key.to_string(), value);
    }
}

impl Extractor for MapCarrier {
    fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).map(String::as_str)
    }
    fn keys(&self) -> Vec<&str> {
        self.0.keys().map(String::as_str).collect()
    }
}

fn propagator() -> TraceContextPropagator {
    TraceContextPropagator::new()
}

/// The current tracing span's trace id as 32-char lowercase hex, or `None` when
/// there is no valid/sampled OTel span (e.g. no provider installed in unit
/// tests). Backs `audit.domain_event.trace_id` stamping at event-stage time.
pub fn current_trace_id() -> Option<String> {
    let cx = Span::current().context();
    let span = cx.span();
    let sc = span.span_context();
    sc.is_valid().then(|| sc.trace_id().to_string())
}

/// The current span serialized as a W3C `traceparent`, for outbound injection
/// (bss-clients, inline MQ publishes). `None` when there is no valid span.
pub fn current_traceparent() -> Option<String> {
    let cx = Span::current().context();
    if !cx.span().span_context().is_valid() {
        return None;
    }
    let mut carrier = MapCarrier::default();
    propagator().inject_context(&cx, &mut carrier);
    carrier.0.remove(TRACEPARENT)
}

/// Build a `traceparent` from a stored 32-hex `trace_id` — the relay path, which
/// publishes out of request context and re-seeds the trace from the trace id it
/// persisted on the outbox row. Synthesizes a random (phantom) parent span id
/// with the sampled flag set; Jaeger groups on the trace id, so the phantom
/// parent only costs a broken span reference, not trace fragmentation. `None`
/// unless `trace_id` is exactly 32 hex digits.
pub fn traceparent_for_trace_id(trace_id: &str) -> Option<String> {
    if trace_id.len() != 32 || !trace_id.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    let span_id = RandomIdGenerator::default().new_span_id();
    Some(format!("00-{}-{span_id}-01", trace_id.to_ascii_lowercase()))
}

/// Adopt an inbound `traceparent` as `span`'s parent so the span continues the
/// remote trace. No-op when the header is absent or unparseable (the span then
/// starts a fresh trace). Used by the inbound HTTP layer and the MQ consumer.
pub fn continue_trace(span: &Span, traceparent: Option<&str>) {
    let Some(tp) = traceparent.filter(|s| !s.is_empty()) else {
        return;
    };
    let mut carrier = MapCarrier::default();
    carrier.0.insert(TRACEPARENT.to_string(), tp.to_string());
    let cx = propagator().extract(&carrier);
    if cx.span().span_context().is_valid() {
        span.set_parent(cx);
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn traceparent_for_trace_id_validates_shape() {
        // 32 hex → a well-formed traceparent seeded with that trace id.
        let tid = "0af7651916cd43dd8448eb211c80319c";
        let tp = traceparent_for_trace_id(tid).expect("valid trace id");
        assert!(tp.starts_with(&format!("00-{tid}-")));
        assert!(tp.ends_with("-01"));
        // Wrong length / non-hex → None.
        assert!(traceparent_for_trace_id("tooshort").is_none());
        assert!(traceparent_for_trace_id("zz7651916cd43dd8448eb211c80319c!").is_none());
    }

    #[test]
    fn current_helpers_none_without_provider() {
        // No OTel provider installed in the unit-test process → no valid span.
        assert!(current_trace_id().is_none());
        assert!(current_traceparent().is_none());
    }
}
