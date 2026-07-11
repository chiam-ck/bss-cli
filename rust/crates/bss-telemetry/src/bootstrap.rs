//! OTel + tracing bootstrap — port of `bss_telemetry.bootstrap.configure_telemetry`.
//!
//! Builds a `TracerProvider` with an OTLP/HTTP-protobuf exporter to the same
//! Jaeger the Python stack uses, bridges `tracing` spans to it, and installs a
//! JSON log layer. Never panics — observability must not gate startup: on any
//! failure (or `BSS_OTEL_ENABLED=false`) it falls back to JSON logs only.
//!
//! Rust has no auto-instrumentors; spans are added at the platform-crate seams
//! (bss-clients/bss-db/bss-events) per the tech-mapping. This bootstrap is the
//! provider + bridge those seams feed into.

use opentelemetry::{global, trace::TracerProvider as _, KeyValue};
use opentelemetry_otlp::{Protocol, WithExportConfig};
use opentelemetry_sdk::{runtime, trace::Sampler, trace::TracerProvider, Resource};
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

/// Reads `BSS_OTEL_*` from the environment (the bootstrap is a config seam).
struct OtelConfig {
    enabled: bool,
    endpoint: String,
    service_name: String,
    service_version: String,
    sampling_ratio: f64,
}

impl OtelConfig {
    fn from_env(service: &str) -> Self {
        let prefix = env_or("BSS_OTEL_SERVICE_NAME_PREFIX", "bss");
        OtelConfig {
            enabled: env_bool("BSS_OTEL_ENABLED", true),
            endpoint: env_or("BSS_OTEL_EXPORTER_OTLP_ENDPOINT", "http://tech-vm:4318"),
            service_name: format!("{prefix}-{service}"),
            service_version: env_or("BSS_OTEL_SERVICE_VERSION", "0.2.0"),
            sampling_ratio: env_f64("BSS_OTEL_SAMPLING_RATIO", 1.0),
        }
    }
}

/// Held for the process lifetime. On drop it shuts the provider down, flushing
/// queued spans — important for short-lived processes (CLI, the conformance
/// harness) that would otherwise exit before the batch interval.
pub struct TelemetryGuard {
    provider: Option<TracerProvider>,
}

impl TelemetryGuard {
    /// Force-export any queued spans now (best-effort).
    pub fn force_flush(&self) {
        if let Some(provider) = &self.provider {
            for result in provider.force_flush() {
                let _ = result;
            }
        }
    }
}

impl Drop for TelemetryGuard {
    fn drop(&mut self) {
        if let Some(provider) = &self.provider {
            let _ = provider.shutdown();
        }
    }
}

/// Initialise tracing for `service` (the Jaeger service becomes `bss-<service>`).
/// Always returns a guard; never panics.
pub fn init_telemetry(service: &str) -> TelemetryGuard {
    let cfg = OtelConfig::from_env(service);
    let fmt_layer = tracing_subscriber::fmt::layer().json();

    if !cfg.enabled {
        let _ = tracing_subscriber::registry().with(fmt_layer).try_init();
        return TelemetryGuard { provider: None };
    }

    match build_provider(&cfg) {
        Ok(provider) => {
            let tracer = provider.tracer("bss-telemetry");
            let otel_layer = tracing_opentelemetry::layer().with_tracer(tracer);
            let _ = tracing_subscriber::registry()
                .with(fmt_layer)
                .with(otel_layer)
                .try_init();
            global::set_tracer_provider(provider.clone());
            TelemetryGuard {
                provider: Some(provider),
            }
        }
        Err(err) => {
            // Never gate startup on observability — log to stderr, keep JSON logs.
            eprintln!("telemetry.setup_failed service={service} error={err}");
            let _ = tracing_subscriber::registry().with(fmt_layer).try_init();
            TelemetryGuard { provider: None }
        }
    }
}

/// Emit a single span with operation name `operation` and return its trace id
/// (32-char hex), so a caller can look the trace up in Jaeger. Returns `None`
/// when there is no active provider or the span wasn't sampled. Used by the
/// conformance harness to prove traces reach Jaeger.
pub fn emit_probe_span(operation: &str) -> Option<String> {
    use opentelemetry::trace::TraceContextExt;
    use tracing_opentelemetry::OpenTelemetrySpanExt;

    let span = tracing::info_span!("conformance.probe", otel.name = operation);
    let _enter = span.enter();
    let context = span.context();
    let span_ref = context.span();
    let span_context = span_ref.span_context();
    span_context
        .is_valid()
        .then(|| span_context.trace_id().to_string())
}

fn build_provider(cfg: &OtelConfig) -> Result<TracerProvider, Box<dyn std::error::Error>> {
    let exporter = opentelemetry_otlp::SpanExporter::builder()
        .with_http()
        .with_endpoint(format!("{}/v1/traces", cfg.endpoint.trim_end_matches('/')))
        .with_protocol(Protocol::HttpBinary)
        .build()?;

    let resource = Resource::new([
        KeyValue::new("service.name", cfg.service_name.clone()),
        KeyValue::new("service.version", cfg.service_version.clone()),
    ]);

    Ok(TracerProvider::builder()
        .with_batch_exporter(exporter, runtime::Tokio)
        .with_sampler(Sampler::TraceIdRatioBased(cfg.sampling_ratio))
        .with_resource(resource)
        .build())
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_bool(key: &str, default: bool) -> bool {
    match std::env::var(key) {
        Ok(v) => matches!(
            v.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        Err(_) => default,
    }
}

fn env_f64(key: &str, default: f64) -> f64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}
