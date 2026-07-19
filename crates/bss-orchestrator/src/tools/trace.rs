//! Trace observability tools — Jaeger trace summaries + audit-event resolution.
//! Port of the trace slice of `orchestrator/bss_orchestrator/tools/ops.py`.
//!
//! `trace.get` summarizes a Jaeger v1 trace; `trace.for_order` /
//! `trace.for_subscription` resolve the most-recent trace id for an aggregate via
//! `audit.domain_event` (queried through the owning service's audit API) and then
//! summarize it. A Jaeger miss returns a structured `JAEGER_ERROR` observation (not
//! a turn failure); no recorded trace id returns the `NO_TRACE_RECORDED` sentinel.

use std::collections::BTreeSet;
use std::sync::Arc;

use bss_clients::{AuditClient, JaegerClient};
use futures_util::future::FutureExt;
use serde_json::{json, Value};

use super::{map_client_err as map_err, req_str, RegisteredTool, ToolRegistry};

const DESC_GET: &str = include_str!("desc/trace_get.txt");
const DESC_FOR_ORDER: &str = include_str!("desc/trace_for_order.txt");
const DESC_FOR_SUBSCRIPTION: &str = include_str!("desc/trace_for_subscription.txt");

/// Reduce a Jaeger v1 trace to the LLM/scenario-friendly summary fields — a port of
/// Python's `_summarize_trace` (key order preserved via D9's preserve_order).
fn summarize_trace(trace: &Value) -> Value {
    let empty: Vec<Value> = Vec::new();
    let spans = trace
        .get("spans")
        .and_then(Value::as_array)
        .unwrap_or(&empty);
    let processes = trace.get("processes");

    // Unique service names, sorted (BTreeSet == Python's `sorted({...})`).
    let mut services: BTreeSet<String> = BTreeSet::new();
    for s in spans {
        let pid = s.get("processID").and_then(Value::as_str).unwrap_or("");
        let name = processes
            .and_then(|p| p.get(pid))
            .and_then(|p| p.get("serviceName"))
            .and_then(Value::as_str)
            .unwrap_or("?");
        services.insert(name.to_string());
    }

    // Count error TAGS across all spans (`sum(1 for s for tag if key==error and
    // value is True)` — a tag count, not a span count, despite the field name).
    let error_count: usize = spans
        .iter()
        .map(|s| {
            s.get("tags").and_then(Value::as_array).map_or(0, |tags| {
                tags.iter()
                    .filter(|t| {
                        t.get("key").and_then(Value::as_str) == Some("error")
                            && t.get("value") == Some(&Value::Bool(true))
                    })
                    .count()
            })
        })
        .sum();

    let total_us: i64 = if spans.is_empty() {
        0
    } else {
        let start = spans
            .iter()
            .map(|s| s.get("startTime").and_then(Value::as_i64).unwrap_or(0))
            .min()
            .unwrap_or(0);
        let end = spans
            .iter()
            .map(|s| {
                s.get("startTime").and_then(Value::as_i64).unwrap_or(0)
                    + s.get("duration").and_then(Value::as_i64).unwrap_or(0)
            })
            .max()
            .unwrap_or(0);
        end - start
    };

    let services: Vec<Value> = services.into_iter().map(Value::String).collect();
    json!({
        "traceId": trace.get("traceID").and_then(Value::as_str).unwrap_or(""),
        "spanCount": spans.len(),
        "serviceCount": services.len(),
        "services": services,
        "errorSpanCount": error_count,
        "totalMs": round2(total_us as f64 / 1000.0),
    })
}

/// 2-decimal round. Python's `round(x, 2)` is half-to-even; `totalMs` is derived
/// from live span timings and is never fixture-pinned, so half-away-from-zero is
/// sufficient here.
fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

/// Most-recent recorded trace id across an event list — `reversed(events)`, first
/// truthy `traceId`/`trace_id`. Port of `_latest_trace_id`.
fn latest_trace_id(events: &Value) -> Option<String> {
    let arr = events.as_array()?;
    for ev in arr.iter().rev() {
        let tid = ev
            .get("traceId")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .or_else(|| {
                ev.get("trace_id")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
            });
        if let Some(t) = tid {
            return Some(t.to_string());
        }
    }
    None
}

/// Fetch + summarize a trace, mapping a Jaeger miss to the structured
/// `JAEGER_ERROR` observation (returned as Ok — the tool does not fail the turn).
async fn trace_summary(jaeger: &JaegerClient, trace_id: &str) -> Value {
    match jaeger.get_trace(trace_id).await {
        Ok(trace) => summarize_trace(&trace),
        Err(e) => json!({
            "error": "JAEGER_ERROR",
            "message": e.to_string(),
            "traceId": trace_id,
        }),
    }
}

/// Register the three `trace.*` tools. `audit_com` resolves order traces (COM's
/// audit surface); `audit_sub` resolves subscription traces.
pub fn register_trace_tools(
    registry: &mut ToolRegistry,
    jaeger: JaegerClient,
    audit_com: AuditClient,
    audit_sub: AuditClient,
) {
    let j = jaeger.clone();
    registry.register(RegisteredTool {
        name: "trace.get".to_string(),
        description: DESC_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let j = j.clone();
            async move {
                let trace_id = req_str(&args, "trace_id")?;
                Ok(trace_summary(&j, &trace_id).await)
            }
            .boxed()
        }),
    });

    let j = jaeger.clone();
    let a = audit_com;
    registry.register(RegisteredTool {
        name: "trace.for_order".to_string(),
        description: DESC_FOR_ORDER.to_string(),
        func: Arc::new(move |args, _ctx| {
            let j = j.clone();
            let a = a.clone();
            async move {
                let order_id = req_str(&args, "order_id")?;
                let events = a
                    .list_events(Some("ProductOrder"), Some(&order_id), 20)
                    .await
                    .map_err(map_err)?;
                match latest_trace_id(&events) {
                    None => Ok(json!({
                        "error": "NO_TRACE_RECORDED",
                        "message": format!("no trace_id on any audit event for {order_id}"),
                        "orderId": order_id,
                    })),
                    Some(tid) => {
                        let mut summary = trace_summary(&j, &tid).await;
                        if let Some(obj) = summary.as_object_mut() {
                            obj.insert("orderId".to_string(), json!(order_id));
                        }
                        Ok(summary)
                    }
                }
            }
            .boxed()
        }),
    });

    let j = jaeger;
    let a = audit_sub;
    registry.register(RegisteredTool {
        name: "trace.for_subscription".to_string(),
        description: DESC_FOR_SUBSCRIPTION.to_string(),
        func: Arc::new(move |args, _ctx| {
            let j = j.clone();
            let a = a.clone();
            async move {
                let subscription_id = req_str(&args, "subscription_id")?;
                let events = a
                    .list_events(Some("subscription"), Some(&subscription_id), 20)
                    .await
                    .map_err(map_err)?;
                match latest_trace_id(&events) {
                    None => Ok(json!({
                        "error": "NO_TRACE_RECORDED",
                        "message": format!("no trace_id on any audit event for {subscription_id}"),
                        "subscriptionId": subscription_id,
                    })),
                    Some(tid) => {
                        let mut summary = trace_summary(&j, &tid).await;
                        if let Some(obj) = summary.as_object_mut() {
                            obj.insert("subscriptionId".to_string(), json!(subscription_id));
                        }
                        Ok(summary)
                    }
                }
            }
            .boxed()
        }),
    });
}
